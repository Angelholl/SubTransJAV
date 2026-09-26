"""闸门0——送翻前源侧幻觉检测器测试。

覆盖：
1. 七类别正/负样本 × default/strict/off 三档行为差异；
2. keep_list 白名单最高优先级；
3. 保险阀降级（只计数模式）与阈值可配；
4. 假阳性交叉守卫（language_validator 六集合逐词零删除）；
5. keep_list 与 _WHITELIST_JA 口径一致性（R7）；
6. 规则库加载（用户覆盖整文件替换 / 损坏显式报错 / 证据样本）；
7. 删除条目归档与静默容错。
"""

import os
import types

import pytest
import yaml

from subtransjav.refine import source_hallucination as sh
from subtransjav.refine.source_hallucination import (
    SourceRulesLoadError,
    apply_source_filter,
    gate0_rules_sha1,
    load_source_rules,
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _t(sec):
    """可控时间轴：起始 sec 秒，时长 1 秒（无重叠、递增）。"""
    m, s = divmod(sec, 60)
    return f"00:{m:02d}:{s:02d},000 --> 00:{m:02d}:{s + 1:02d},000"


def _entries(*texts):
    return [{"index": i, "timing": _t(2 * i), "text": t}
            for i, t in enumerate(texts, 1)]


def _entries_at(pairs):
    """(起始秒, 文本) 列表 → 条目（片尾窗口等需要可控时间轴的场景）。"""
    return [{"index": i, "timing": _t(sec), "text": t}
            for i, (sec, t) in enumerate(pairs, 1)]


def _cfg(mode="default", valve=50):
    cfg = types.SimpleNamespace(v2_source_filter=mode,
                                v2_source_filter_valve_pct=valve)
    return cfg


def _minimal_rules():
    """满足全部必填键的最小规则对象（用户覆盖/加载测试用）。"""
    return {
        "schema_version": 1,
        "keep_list": ["カスタム"],
        "pure_punctuation": {"example": "。。。"},
        "exclamation": {"min_run": 2, "example": "！！！"},
        "unpronounceable": {"min_len": 2, "example": "sssss"},
        "repeat_loop": {"min_run": 4, "min_norm_len": 2,
                        "example": "みんな"},
        "end_meta": {"window_ratio": 0.1,
                     "words_ja": ["チャンネル登録"],
                     "words_en": ["subscribe"],
                     "example": "チャンネル登録お願いします"},
        "isolated_response": {"words": ["うんうん"],
                              "strict_min_run": 4,
                              "strict_max_ratio": 0.5,
                              "example": "うんうん"},
        "nonsense_syllables": {"min_len": 3, "intra_repeat_min": 6,
                               "unit_repeat_min": 4,
                               "example": "あじゃあじゃ"},
    }


@pytest.fixture
def isolated_rules(tmp_path, monkeypatch):
    """把包内默认规则库重定向到临时文件（隔离真实默认库，测试语义内容指纹）。"""
    rules_file = tmp_path / "source_hallucination.yaml"
    state = {"n": 0}

    def _write(rules):
        state["n"] += 1
        rules_file.write_text(
            yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
        # 显式递增 mtime，绕开 _load_cached 的 (path, mtime) 缓存
        stamp = 1_700_000_000 + state["n"] * 100
        os.utime(str(rules_file), (stamp, stamp))

    monkeypatch.setattr(sh, "_resolve_rules_path",
                        lambda config_dir=None: str(rules_file))
    return _write


# ---------------------------------------------------------------------------
# 1. 七类别 × 三档位
# ---------------------------------------------------------------------------

def test_category_pure_punctuation_modes():
    entries = _entries("こんにちは", "。。。", "はい。")
    # default：删（负样本近邻 はい。 保留）
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert [e["text"] for e in kept] == ["こんにちは", "はい。"]
    assert stats["categories"]["纯标点行"] == {"detected": 1, "deleted": 1}
    # strict：删
    kept2, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert len(kept2) == 2
    assert stats2["categories"]["纯标点行"]["deleted"] == 1
    # off：不处理
    kept3, stats3 = apply_source_filter(entries, _cfg("off"))
    assert [e["text"] for e in kept3] == ["こんにちは", "。。。", "はい。"]
    assert stats3["detected_total"] == 0


def test_category_exclamation_modes():
    entries = _entries("そうですか", "！！！")
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert [e["text"] for e in kept] == ["そうですか"]
    assert stats["categories"]["!串"]["deleted"] == 1
    _, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert stats2["categories"]["!串"]["deleted"] == 1
    kept3, stats3 = apply_source_filter(entries, _cfg("off"))
    assert len(kept3) == 2 and stats3["detected_total"] == 0


def test_category_exclamation_single_bang_not_deleted():
    """单个 ！（无 ≥2 连）且含实义内容 → 保留。"""
    entries = _entries("そうですね！")
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert len(kept) == 1
    assert stats["categories"]["!串"]["detected"] == 0


def test_category_unpronounceable_modes():
    entries = _entries("えっ", "kkkk")
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert [e["text"] for e in kept] == ["えっ"]
    assert stats["categories"]["不可发音辅音串"]["deleted"] == 1
    _, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert stats2["categories"]["不可发音辅音串"]["deleted"] == 1
    kept3, _ = apply_source_filter(entries, _cfg("off"))
    assert len(kept3) == 2


def test_category_unpronounceable_kana_and_acronyms_exempt():
    """假名串不适用（kk 类只对拉丁字母）；全大写缩略词（TV/OK）豁免。"""
    entries = _entries("えっか", "TV", "OK")
    _, stats = apply_source_filter(entries, _cfg("default"))
    assert stats["categories"]["不可发音辅音串"]["detected"] == 0


def test_category_repeat_loop_modes():
    normals = ["こんにちは", "さようなら", "また明日", "寒いね",
               "そうだね", "熱くなる", "水泳部"]
    entries = _entries("みんな", "みんな", "みんな", "みんな", *normals)
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert [e["text"] for e in kept] == normals
    assert stats["categories"]["重复循环"] == {"detected": 4, "deleted": 4}
    _, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert stats2["categories"]["重复循环"]["deleted"] == 4
    kept3, stats3 = apply_source_filter(entries, _cfg("off"))
    assert len(kept3) == 11 and stats3["detected_total"] == 0


def test_category_repeat_loop_run_below_threshold_kept():
    """同文 3 连（< 4）→ 保留。"""
    entries = _entries("みんな", "みんな", "みんな", "ありがとう")
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert len(kept) == 4
    assert stats["categories"]["重复循环"]["detected"] == 0


def test_category_end_meta_dual_gate():
    """双闸：时间轴末 10% 区间 + 元信息词，缺一不删。"""
    total = 200
    entries = _entries_at([
        (2, "チャンネル登録お願いします"),      # 元信息词但在开头 → 保留
        (total - 2, "チャンネル登録お願いします"),  # 末尾 + 元信息词 → 删
        (total - 2, "本当にありがとう"),         # 末尾但无元信息词 → 保留
        (total - 4, "さようなら"),
    ])
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert [e["text"] for e in kept].count("チャンネル登録お願いします") == 1
    assert stats["categories"]["片尾元信息"] == {"detected": 1, "deleted": 1}
    _, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert stats2["categories"]["片尾元信息"]["deleted"] == 1


def test_category_end_meta_english_words():
    entries = _entries_at([
        (2, "Thanks for watching!"),
        (198, "Please subscribe"),
        (199, "普通の台詞"),
    ])
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert [e["text"] for e in kept] == ["Thanks for watching!", "普通の台詞"]
    assert stats["categories"]["片尾元信息"]["deleted"] == 1


def test_category_isolated_response_modes():
    """孤立应答词：default 只计数；strict 占比超限才删；off 不处理。"""
    entries = _entries("うんうん", "こんにちは", "うんうん",
                       "ありがとう", "うんうん")   # 3/5 = 60% > 50%
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert len(kept) == 5                       # 只计数，不删
    assert stats["categories"]["孤立应答词"] == {"detected": 3, "deleted": 0}
    kept2, stats2 = apply_source_filter(entries, _cfg("strict", valve=80))
    assert [e["text"] for e in kept2] == ["こんにちは", "ありがとう"]
    assert stats2["categories"]["孤立应答词"] == {"detected": 3, "deleted": 3}
    kept3, stats3 = apply_source_filter(entries, _cfg("off"))
    assert len(kept3) == 5 and stats3["detected_total"] == 0


def test_category_isolated_response_low_density_kept_in_strict():
    """strict 但占比未超限、无同词 4 连 → 只计数不删。"""
    entries = _entries("うんうん", "こんにちは", "さようなら",
                       "ありがとう", "また明日")
    kept, stats = apply_source_filter(entries, _cfg("strict"))
    assert len(kept) == 5
    assert stats["categories"]["孤立应答词"] == {"detected": 1, "deleted": 0}


def test_category_nonsense_syllable_modes():
    """无意义音节连缀：default 只计数；strict 叠加启发式（单元平铺）删除。"""
    entries = _entries("あじゃあじゃあじゃあじゃ", "こんにちは")
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert len(kept) == 2
    assert stats["categories"]["无意义音节连缀"] == {"detected": 1, "deleted": 0}
    kept2, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert [e["text"] for e in kept2] == ["こんにちは"]
    assert stats2["categories"]["无意义音节连缀"]["deleted"] == 1
    kept3, _ = apply_source_filter(entries, _cfg("off"))
    assert len(kept3) == 2


def test_category_nonsense_long_kana_repeat_exempt():
    """あああ/ううう 等长假名重复是真实台词形态：不检出不删除。"""
    entries = _entries("あああ", "ううう", "えええ", "ああああああ")
    _, stats = apply_source_filter(entries, _cfg("default"))
    assert stats["categories"]["无意义音节连缀"]["detected"] == 0
    kept, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert len(kept) == 4
    assert stats2["categories"]["无意义音节连缀"]["detected"] == 0


def test_category_nonsense_intra_entry_repeat():
    """补充检测：条目内重复（嵌入长连缀）归入无意义音节连缀处置。"""
    entries = _entries("んああああああ", "こんにちは")
    _, stats = apply_source_filter(entries, _cfg("default"))
    assert stats["categories"]["无意义音节连缀"]["detected"] == 1
    kept, stats2 = apply_source_filter(entries, _cfg("strict"))
    assert [e["text"] for e in kept] == ["こんにちは"]
    assert stats2["categories"]["无意义音节连缀"]["deleted"] == 1


# ---------------------------------------------------------------------------
# 2. keep_list 白名单最高优先级
# ---------------------------------------------------------------------------

def test_keep_list_protects_from_repeat_loop():
    """白名单词（了解）即使 4 连同文也不删不计数（任何档位）。"""
    entries = _entries("了解", "了解", "了解", "了解")
    for mode in ("default", "strict"):
        kept, stats = apply_source_filter(entries, _cfg(mode))
        assert len(kept) == 4
        assert stats["detected_total"] == 0


def test_keep_list_protects_with_punctuation_decoration():
    """白名单词带句读（はい！）仍整条保护（规范化后精确匹配），
    且按计数类计入检出统计（可见性），不删除。"""
    entries = _entries("はい！")
    kept, stats = apply_source_filter(entries, _cfg("strict"))
    assert len(kept) == 1
    assert stats["deleted"] == 0
    assert stats["categories"]["孤立应答词"] == {"detected": 1, "deleted": 0}


def test_whitelisted_response_words_counted_not_deleted():
    """白名单应答词（うん。/はい。）任何档位不删除，但计入检出统计。

    真实语料回归：某批上游输出中 うん。823 条/はい。445 条，
    default 档归零计数会让裁决点1 的可见性诉求落空（实弹测试发现）。
    """
    entries = _entries("うん。", "こんにちは", "はい。", "さようなら")
    for mode in ("default", "strict"):
        kept, stats = apply_source_filter(entries, _cfg(mode))
        assert len(kept) == 4                       # 白名单保护，任何档位不删
        assert stats["deleted"] == 0
        assert stats["categories"]["孤立应答词"] == {"detected": 2, "deleted": 0}


# ---------------------------------------------------------------------------
# 3. 保险阀
# ---------------------------------------------------------------------------

def test_valve_trips_and_downgrades_to_count_only(capsys):
    """拦截率 >50% → 全文件降级只计数（不删除）+ 显著警告。"""
    entries = _entries("。。。", "！！", "kkkk", "。。。。。",
                       "？？", "…", "こんにちは", "ありがとう",
                       "さようなら", "また明日")   # 6/10 = 60%
    kept, stats = apply_source_filter(entries, _cfg("default", valve=50))
    assert len(kept) == 10                       # 无删除
    assert stats["valve_tripped"] is True
    assert stats["deleted"] == 0
    assert stats["detected_total"] >= 6          # 只计数
    out = capsys.readouterr().out
    assert "闸门0 保险阀触发（拦截率 60% > 50%），本文件降级为只计数模式" in out


def test_valve_not_tripped_on_normal_ratio():
    entries = _entries("。。。", "こんにちは", "ありがとう", "さようなら",
                       "また明日", "ほんとに", "部長でエースで",
                       " water", "水泳部", "熱くなる")
    kept, stats = apply_source_filter(entries, _cfg("default", valve=50))
    assert stats["valve_tripped"] is False
    assert stats["deleted"] == 1                 # 正常删除


def test_valve_threshold_configurable():
    """阈值可配：valve=10 时 2/15（13%）也触发降级。"""
    normals = ["こんにちは", "ありがとう", "さようなら", "また明日",
               "ほんとに", "部長でエースで", "水泳部", "熱くなる",
               "寒いね", "そうだね", "わかった", "bles", "milk",]
    entries = _entries("。。。", "！！", *normals)   # 2/15 ≈ 13%
    _, stats = apply_source_filter(entries, _cfg("default", valve=10))
    assert stats["valve_tripped"] is True
    assert stats["deleted"] == 0


# ---------------------------------------------------------------------------
# 4. 假阳性交叉守卫（R7：language_validator 白名单零误杀）
# ---------------------------------------------------------------------------

def test_whitelist_words_survive_default_gate():
    """六集合全部成员逐词作为条目跑 default 档 → 零删除（白名单最高优先）；
    计数类命中仅计入检出统计（可见性），不影响删除。"""
    from subtransjav.refine.language_validator import _WHITELIST_JA
    rules = load_source_rules()
    iso_words = {str(w).strip() for w in
                 (rules.get("isolated_response") or {}).get("words") or []}
    for word in sorted(_WHITELIST_JA):
        entries = _entries(word)
        kept, stats = apply_source_filter(entries, _cfg("default"))
        assert len(kept) == 1, f"白名单词被误删: {word!r}"
        assert stats["deleted"] == 0, f"白名单词被误删: {word!r}"
        expected = 1 if word in iso_words else 0
        assert stats["detected_total"] == expected, \
            f"计数口径漂移: {word!r}"


def test_keep_list_matches_language_validator_whitelist():
    """口径一致性：keep_list 默认值与 _WHITELIST_JA 并集完全一致
    （防"一处删一处保"漂移；任一侧更新必须同步）。"""
    from subtransjav.refine.language_validator import _WHITELIST_JA
    rules = load_source_rules()
    assert set(rules["keep_list"]) == set(_WHITELIST_JA)


# ---------------------------------------------------------------------------
# 5. 规则库加载
# ---------------------------------------------------------------------------

def test_rules_loadable_with_schema_and_examples():
    rules = load_source_rules()
    assert rules["schema_version"] == 1
    assert isinstance(rules["keep_list"], list) and rules["keep_list"]
    for key in ("pure_punctuation", "exclamation", "unpronounceable",
                "repeat_loop", "end_meta", "isolated_response",
                "nonsense_syllables"):
        assert rules[key].get("example"), f"类别 {key} 缺少证据样本 example"


def test_user_rules_whole_file_replacement(tmp_path, monkeypatch):
    """用户目录规则存在 → 整文件替换包内默认（非合并）。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    user_rules = _minimal_rules()
    (rules_dir / "source_hallucination.yaml").write_text(
        yaml.safe_dump(user_rules, allow_unicode=True), encoding="utf-8")
    loaded = load_source_rules(str(tmp_path))
    assert loaded["keep_list"] == ["カスタム"]   # 用户副本生效
    # 白名单外的词不再受保护 → repeat_loop 恢复删除（配足正常条目防保险阀触发）
    normals = ["こんにちは", "さようなら", "また明日", "寒いね",
               "そうだね", "熱くなる"]
    kept, stats = apply_source_filter(
        _entries("はい", "はい", "はい", "はい", *normals),
        _cfg("default"), config_dir=str(tmp_path))
    assert [e["text"] for e in kept] == normals
    assert stats["categories"]["重复循环"]["deleted"] == 4


def test_missing_user_dir_falls_back_to_package(tmp_path):
    loaded = load_source_rules(str(tmp_path / "nonexistent"))
    assert loaded["schema_version"] == 1
    assert "はい" in loaded["keep_list"]


def test_corrupt_rules_yaml_raises(tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "source_hallucination.yaml").write_text(
        "keep_list: [ broken", encoding="utf-8")
    with pytest.raises(SourceRulesLoadError):
        load_source_rules(str(tmp_path))


def test_rules_missing_required_key_raises(tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "source_hallucination.yaml").write_text(
        "schema_version: 1\n", encoding="utf-8")
    with pytest.raises(SourceRulesLoadError):
        load_source_rules(str(tmp_path))


def test_gate0_rules_sha1_semantic_and_stable():
    rules = load_source_rules()
    h1 = gate0_rules_sha1(rules)
    assert h1 == gate0_rules_sha1(rules)         # 同对象稳定
    assert len(h1) == 40
    import copy
    changed = copy.deepcopy(rules)
    changed["keep_list"].append("テスト用語")
    assert gate0_rules_sha1(changed) != h1       # 语义内容变化 → 哈希变化


def test_config_hash_tracks_gate0_rules_semantic_content(
        tmp_path, isolated_rules, monkeypatch):
    """manifest 指纹消费规则库语义内容：内容变 → 指纹变；内容还原 → 指纹还原。"""
    from subtransjav.refine.manifest import compute_config_hash
    rules = _minimal_rules()
    isolated_rules(rules)
    h1 = compute_config_hash(types.SimpleNamespace(stages=[]))
    rules["keep_list"] = ["カスタム", "追加語"]
    isolated_rules(rules)
    h2 = compute_config_hash(types.SimpleNamespace(stages=[]))
    assert h2 != h1
    isolated_rules(_minimal_rules())             # 内容还原（路径不变）
    assert compute_config_hash(types.SimpleNamespace(stages=[])) == h1


# ---------------------------------------------------------------------------
# 6. 归档与静默容错 / stats 结构
# ---------------------------------------------------------------------------

def test_dropped_entries_archived_with_gate0_reason(tmp_path):
    """删除条目追加归档，reason 用「闸门0-类别名」。"""
    errs = tmp_path / "Errors"
    entries = _entries("。。。", "こんにちは", "ありがとう", "さようなら",
                       "また明日")
    apply_source_filter(entries, _cfg("default"), source_name="demo.srt",
                        errors_dir=str(errs))
    log = (errs / "dropped_entries.log").read_text(encoding="utf-8")
    assert "原因=闸门0-纯标点行" in log
    assert "来源=demo.srt" in log


def test_count_only_categories_not_archived(tmp_path):
    """计数类类别（default 档）只在 stats 计数，不写归档日志。"""
    errs = tmp_path / "Errors"
    entries = _entries("あじゃあじゃあじゃあじゃ", "こんにちは")
    apply_source_filter(entries, _cfg("default"), errors_dir=str(errs))
    assert not (errs / "dropped_entries.log").exists()


def test_archive_silent_on_oserror(tmp_path):
    """errors_dir 指向普通文件（makedirs 必败）→ 静默容错不崩溃。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    entries = _entries("。。。", "こんにちは", "ありがとう", "さようなら",
                       "また明日")
    kept, stats = apply_source_filter(entries, _cfg("default"),
                                      errors_dir=str(blocker))
    assert stats["deleted"] == 1 and len(kept) == 4   # 检测照常，归档静默放弃


def test_stats_structure_contract():
    """stats 字段契约（H3 结构化报告将消费；H5 只增三键不改原七键）。"""
    entries = _entries("。。。", "こんにちは")
    _, stats = apply_source_filter(entries, _cfg("default"))
    assert set(stats) == {"mode", "total", "deleted", "detected_total",
                          "valve_tripped", "valve_pct", "categories",
                          "quarantine_candidates", "count_positions",
                          "noise_left_empty"}
    assert set(stats["categories"]) == {
        "!串", "纯标点行", "不可发音辅音串", "重复循环", "片尾元信息",
        "孤立应答词", "无意义音节连缀"}
    for v in stats["categories"].values():
        assert set(v) == {"detected", "deleted"}


def test_off_mode_short_circuits_without_rules_load(monkeypatch):
    """off 档不加载规则库、原样返回（防无效开销）。"""
    def _boom(*a, **k):
        raise AssertionError("off 档不得加载规则库")
    monkeypatch.setattr(sh, "load_source_rules", _boom)
    entries = _entries("。。。", "こんにちは")
    kept, stats = apply_source_filter(entries, _cfg("off"))
    assert [e["text"] for e in kept] == ["。。。", "こんにちは"]
    assert stats["mode"] == "off" and stats["deleted"] == 0


def test_unknown_mode_falls_back_to_default():
    entries = _entries("。。。", "こんにちは")
    cfg = types.SimpleNamespace(v2_source_filter="bogus",
                                v2_source_filter_valve_pct=50)
    _, stats = apply_source_filter(entries, cfg)
    assert stats["mode"] == "default"


# ---------------------------------------------------------------------------
# 8. H4a：上游 ASR 信号收紧（tighten）
# ---------------------------------------------------------------------------

def test_tighten_block_present_in_default_rules():
    """包内默认规则库携带 tighten 收紧覆盖块（repeat_loop/end_meta）。"""
    rules = load_source_rules()
    assert rules["tighten"]["repeat_loop"]["min_run"] == 3
    assert rules["tighten"]["end_meta"]["window_ratio"] == 0.2


def test_tighten_off_three_run_kept():
    """无信号（默认）：3 连同文不删（min_run=4）。"""
    entries = _entries("みんな", "みんな", "みんな", "こんにちは",
                       "さようなら", "また明日", "寒いね", "そうだね")
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert len(kept) == 8
    assert stats["categories"]["重复循环"] == {"detected": 0, "deleted": 0}


def test_tighten_deletes_three_run():
    """tighten：min_run 4→3，3 连同文删除。"""
    entries = _entries("みんな", "みんな", "みんな", "こんにちは",
                       "さようなら", "また明日", "寒いね", "そうだね")
    kept, stats = apply_source_filter(entries, _cfg("default"), tighten=True)
    assert [e["text"] for e in kept] == ["こんにちは", "さようなら",
                                         "また明日", "寒いね", "そうだね"]
    assert stats["categories"]["重复循环"] == {"detected": 3, "deleted": 3}


def test_tighten_ignored_in_strict_mode():
    """tighten 只作用于 default 档删五类；strict 档不受影响（min_run 仍 4）。"""
    entries = _entries("みんな", "みんな", "みんな", "こんにちは",
                       "さようなら", "また明日", "寒いね", "そうだね")
    kept, stats = apply_source_filter(entries, _cfg("strict"), tighten=True)
    assert len(kept) == 8
    assert stats["categories"]["重复循环"]["deleted"] == 0


def test_tighten_ignored_in_off_mode():
    entries = _entries("みんな", "みんな", "みんな")
    kept, stats = apply_source_filter(entries, _cfg("off"), tighten=True)
    assert len(kept) == 3 and stats["detected_total"] == 0


def test_tighten_widens_end_meta_window():
    """tighten：window_ratio 0.1→0.2（末 20% 区间命中，末 10% 区间外）。"""
    pairs = [(2, "こんにちは"), (20, "さようなら"), (40, "また明日"),
             (60, "寒いね"), (80, "そうだね"), (100, "熱くなる"),
             (120, "水泳部"), (140, "ほんとに"),
             (170, "チャンネル登録お願いします"),   # 末 20% 内、末 10% 外
             (195, "部長でエースで")]
    entries = _entries_at(pairs)
    _, stats = apply_source_filter(entries, _cfg("default"))
    assert stats["categories"]["片尾元信息"] == {"detected": 0, "deleted": 0}
    kept2, stats2 = apply_source_filter(entries, _cfg("default"), tighten=True)
    assert stats2["categories"]["片尾元信息"] == {"detected": 1, "deleted": 1}
    assert all("チャンネル登録" not in e["text"] for e in kept2)


def test_tighten_never_unlocks_count_only_categories():
    """契约：计数类（孤立应答词/无意义音节连缀）在任何信号下不解锁为删除，
    tighten 覆盖块也刻意不提供这两类的收紧项。"""
    entries = _entries("うんうん", "こんにちは", "あじゃあじゃあじゃあじゃ",
                       "ありがとう", "うんうん")
    kept, stats = apply_source_filter(entries, _cfg("default"), tighten=True)
    assert len(kept) == 5
    assert stats["categories"]["孤立应答词"] == {"detected": 2, "deleted": 0}
    assert stats["categories"]["无意义音节连缀"] == {"detected": 1, "deleted": 0}


# ---------------------------------------------------------------------------
# 8b. H4b：条目级阈值自适应（tighten_entry_predicate）
# ---------------------------------------------------------------------------

def test_adaptive_predicate_none_byte_identical_to_legacy_call():
    """缺省路径零变化：tighten_entry_predicate=None 时与旧签名调用逐字节
    一致（含全局 tighten 两态）。"""
    entries = _entries("みんな", "みんな", "みんな", "こんにちは",
                       "さようなら", "また明日", "寒いね", "そうだね",
                       "。。。", "！！")
    for kw in ({}, {"tighten": True}):
        k1, s1 = apply_source_filter(entries, _cfg("default"), **kw)
        k2, s2 = apply_source_filter(entries, _cfg("default"),
                                     tighten_entry_predicate=None, **kw)
        assert k1 == k2 and s1 == s2


def test_adaptive_repeat_group_tightened_by_any_low_trust_member():
    """critic 钉①（跨分区 repeat 组就紧）：3 连组横跨低信任/默认条目 →
    整组按 tighten 参数评估删除（防低信任条目渗漏进默认组漏删，
    也防默认条目被牵连误放）。"""
    entries = _entries("みんな", "みんな", "みんな",   # 3 连 < base min_run 4
                       "こんにちは", "さようなら",
                       "また明日", "寒いね", "そうだね", "熱くなる")

    def pred(e):
        return e.get("index") == 1                    # 仅组首条目低信任

    kept, stats = apply_source_filter(entries, _cfg("default"),
                                      tighten_entry_predicate=pred)
    assert [e["text"] for e in kept] == ["こんにちは", "さようなら",
                                         "また明日", "寒いね", "そうだね",
                                         "熱くなる"]
    assert stats["categories"]["重复循环"] == {"detected": 3, "deleted": 3}
    # positions 恒为全局输入索引（组内默认条目一并删除，无渗漏）
    assert stats["count_positions"] == []


def test_adaptive_predicate_false_everywhere_behaves_like_base():
    """谓词恒 False（低信任集为空）→ 参数与 base 变体一致：3 连保留。"""
    entries = _entries("みんな", "みんな", "みんな", "こんにちは",
                       "さようなら", "また明日")

    def pred(e):
        return False

    kept, stats = apply_source_filter(entries, _cfg("default"),
                                      tighten_entry_predicate=pred)
    assert len(kept) == 6
    assert stats["categories"]["重复循环"] == {"detected": 0, "deleted": 0}


def test_adaptive_end_meta_window_anchored_to_full_file_span():
    """critic 钉②（end_meta 全文件 span 锚定）：低信任条目自身位于末 20%
    带（末 10% 外）→ 按全文件 span 的 0.2 窗口检出删除；若实现按低信任
    分区局部重锚（窗口锚到低信任子集的 span），该条检不出。"""
    pairs = [(2, "こんにちは"), (20, "さようなら"), (40, "また明日"),
             (60, "寒いね"), (80, "そうだね"), (100, "熱くなる"),
             (120, "水泳部"), (140, "ほんとに"),
             (170, "チャンネル登録お願いします"),   # 末 20% 内、末 10% 外
             (195, "部長でエースで")]
    entries = _entries_at(pairs)

    def pred(e):
        return e.get("index") == 9               # 仅末 20% 带内的条目低信任

    _, stats = apply_source_filter(entries, _cfg("default"),
                                   tighten_entry_predicate=pred)
    assert stats["categories"]["片尾元信息"] == {"detected": 1, "deleted": 1}


def test_adaptive_end_meta_tight_window_only_for_low_trust_entries():
    """同一 window 内逐条目选参数：末 20% 带内的非低信任条目不删，
    末 10% 带内的条目（无论谓词）按 base 窗口删除。"""
    pairs = [(2, "こんにちは"), (100, "ほんとに"),
             (170, "チャンネル登録お願いします"),   # 仅 0.2 带内、谓词 False
             (195, "チャンネル登録お願いします")]   # 0.1 带内 → base 删
    entries = _entries_at(pairs)

    def pred(e):
        return e.get("index") == 2               # 仅中段条目低信任

    kept, stats = apply_source_filter(entries, _cfg("default"),
                                      tighten_entry_predicate=pred)
    assert [e["text"] for e in kept].count("チャンネル登録お願いします") == 1
    assert stats["categories"]["片尾元信息"] == {"detected": 1, "deleted": 1}


def test_adaptive_predicate_never_unlocks_count_categories():
    """计数类判定路径完全不感知谓词：全部条目低信任也不解锁删除。"""
    entries = _entries("うんうん", "こんにちは", "あじゃあじゃあじゃあじゃ",
                       "ありがとう", "うんうん")
    kept, stats = apply_source_filter(entries, _cfg("default"),
                                      tighten_entry_predicate=lambda e: True)
    assert len(kept) == 5
    assert stats["categories"]["孤立应答词"] == {"detected": 2, "deleted": 0}
    assert stats["categories"]["无意义音节连缀"] == {"detected": 1, "deleted": 0}


def test_adaptive_predicate_ignored_in_strict_mode():
    """谓词只作用于 default 档删五类；strict 档不受影响（min_run 仍 4）。"""
    entries = _entries("みんな", "みんな", "みんな", "こんにちは",
                       "さようなら", "また明日", "寒いね", "そうだね")
    kept, stats = apply_source_filter(entries, _cfg("strict"),
                                      tighten_entry_predicate=lambda e: True)
    assert len(kept) == 8
    assert stats["categories"]["重复循环"]["deleted"] == 0


def test_adaptive_valve_still_counts_full_batch_with_predicate():
    """阀门按全量 rate 计（谓词收紧后的待删集合为输入）：超阈值照常降级。"""
    entries = _entries("みんな", "みんな", "みんな", "。。。", "！！",
                       "kkkk", "こんにちは", "さようなら", "また明日",
                       "ほんとに", "部長でエースで", "水泳部", "熱くなる",
                       "寒いね", "そうだね", "わかった", "やっていく")
    def pred(e):
        return True                            # 全量低信任：3 连也入待删

    kept, stats = apply_source_filter(entries, _cfg("default", valve=10),
                                      tighten_entry_predicate=pred)
    assert stats["valve_tripped"] is True and stats["deleted"] == 0
    assert len(kept) == len(entries)


def test_samples_limit_appends_deleted_entry_samples():
    """samples_limit>0 时 stats 附带已删条目样本（编号/类别/原文）。"""
    entries = _entries("。。。", "こんにちは", "！！", "さようなら")
    _, stats = apply_source_filter(entries, _cfg("default"),
                                   samples_limit=50)
    assert stats["samples"] == [
        {"number": 1, "category": "纯标点行", "text": "。。。"},
        {"number": 3, "category": "!串", "text": "！！"},
    ]


def test_samples_limit_caps_size():
    entries = _entries("。。。", "！！", "kkkk", "こんにちは", "さようなら",
                       "また明日", "ほんとに", "部長でエースで")
    _, stats = apply_source_filter(entries, _cfg("default"), samples_limit=2)
    assert len(stats["samples"]) == 2                  # 上限截断，防报告爆体积


def test_no_samples_key_without_limit():
    """默认不附带 samples 键（保持既有 stats 字段契约不变）。"""
    entries = _entries("。。。", "こんにちは")
    _, stats = apply_source_filter(entries, _cfg("default"))
    assert "samples" not in stats


# ---------------------------------------------------------------------------
# 7. 配置接入
# ---------------------------------------------------------------------------

def test_config_validate_rejects_bad_source_filter():
    from subtransjav.refine.config import RefineConfig
    cfg = RefineConfig()
    cfg.v2_source_filter = "bogus"
    errs = cfg.validate()
    assert any("v2_source_filter" in e for e in errs)


@pytest.mark.parametrize("bad", [0, 101, -5, "abc"])
def test_config_validate_rejects_bad_valve_pct(bad):
    from subtransjav.refine.config import RefineConfig
    cfg = RefineConfig()
    cfg.v2_source_filter_valve_pct = bad
    errs = cfg.validate()
    assert any("v2_source_filter_valve_pct" in e for e in errs)


def test_config_defaults():
    from subtransjav.refine.config import DEFAULT_V2_SOURCE_FILTER_VALVE_PCT, RefineConfig
    cfg = RefineConfig()
    assert cfg.v2_source_filter == "default"
    assert cfg.v2_source_filter_valve_pct == 50
    assert DEFAULT_V2_SOURCE_FILTER_VALVE_PCT == 50


# ---------------------------------------------------------------------------
# 9. H5 翻译后回捞：隔离区候选暴露 + quarantine_review
# ---------------------------------------------------------------------------

def test_quarantine_candidates_only_on_valve_degradation():
    """H5：候选仅产生于保险阀降级路径；正常删除与 off 档恒为空。"""
    entries = _entries("。。。", "！！", "kkkk", "こんにちは",
                       "さようなら", "また明日", "寒いね", "そうだね",
                       "ほんとに", "部長でエースで")
    # 3/10 = 30% ≤ 50%：正常删除，无候选
    kept, stats = apply_source_filter(entries, _cfg("default"))
    assert stats["deleted"] == 3
    assert stats["quarantine_candidates"] == []
    # 阈值收到 20%：触发降级 → 3 条候选（position 为本次检测输入的原始下标）
    kept2, stats2 = apply_source_filter(entries, _cfg("default", valve=20))
    assert stats2["valve_tripped"] is True and stats2["deleted"] == 0
    assert stats2["quarantine_candidates"] == [
        {"position": 0, "category": "纯标点行", "text": "。。。"},
        {"position": 1, "category": "!串", "text": "！！"},
        {"position": 2, "category": "不可发音辅音串", "text": "kkkk"},
    ]
    assert [e["text"] for e in kept2] == [e["text"] for e in entries]
    # off 档：不做候选（用户显式关闭即自负其责）
    _, stats3 = apply_source_filter(entries, _cfg("off"))
    assert stats3["quarantine_candidates"] == []


def test_count_positions_includes_whitelist_hits():
    """H5-7：计数类检出位置入 stats（含白名单保护命中）；计数类不进候选。"""
    entries = _entries("うん", "こんにちは", "あじゃあじゃあじゃあじゃ",
                       "さようなら")
    _, stats = apply_source_filter(entries, _cfg("default"))
    assert stats["count_positions"] == [0, 2]      # 白名单命中 + 常规检出
    assert stats["quarantine_candidates"] == []    # 计数类永不进隔离区
    assert stats["noise_left_empty"] == 0          # 恒 0，由管线回填


def test_quarantine_review_moves_fluent_zh_only():
    """流畅中文候选条目移入隔离区；[未翻译]/原文回退/非候选保留主稿。"""
    final = [
        {"index": 1, "timing": _t(0), "text": "你好"},            # 候选+流畅 → 隔离
        {"index": 2, "timing": _t(2), "text": "早上好"},          # 非候选 → 主稿
        {"index": 3, "timing": _t(4), "text": "！"},              # 候选+非流畅 → 主稿
        {"index": 4, "timing": _t(6), "text": "[未翻译] 。。。"},  # 候选+未翻译 → 主稿
        {"index": 5, "timing": _t(8), "text": "チャンネル登録",    # 候选+原文回退 → 主稿
         "_keep_original": True},
    ]
    cands = [{"position": 0, "category": "纯标点行", "text": "。。。"},
             {"position": 2, "category": "纯标点行", "text": "！"},
             {"position": 3, "category": "纯标点行", "text": "。。。"},
             {"position": 4, "category": "片尾元信息", "text": "チャンネル登録"}]
    lookup = {0: 1, 2: 3, 3: 4, 4: 5}
    main, quarantine = sh.quarantine_review(final, cands, lookup)
    assert [e["index"] for e in main] == [2, 3, 4, 5]
    assert [e["index"] for e in quarantine] == [1]
    # 只移动不删除：主稿+隔离区并集 == 原 final
    assert sorted(main + quarantine, key=lambda e: e["index"]) == final


def test_quarantine_review_missing_lookup_keeps_main():
    """position 无编号映射（防御性保守放行）或候选为空 → 全部保留主稿。"""
    final = [{"index": 1, "timing": _t(0), "text": "你好"}]
    cands = [{"position": 0, "category": "纯标点行", "text": "。。。"}]
    main, quarantine = sh.quarantine_review(final, cands, {})
    assert main == final and quarantine == []
    main2, quarantine2 = sh.quarantine_review(final, [], {})
    assert main2 == final and quarantine2 == []


def test_is_fluent_zh_boundary():
    """流畅中文判定代理边界：≥2 汉字且非 [未翻译] 前缀。"""
    assert sh.is_fluent_zh("早上好") is True
    assert sh.is_fluent_zh("早啊!") is True          # 汉字数达标，标点不碍判定
    assert sh.is_fluent_zh("译1") is False             # 仅 1 个汉字
    assert sh.is_fluent_zh("[未翻译] こんにちは") is False
    assert sh.is_fluent_zh("") is False
    assert sh.is_fluent_zh("ABC") is False
    assert sh.is_fluent_zh("チャンネルとうろく") is False  # 纯假名零汉字


# ---------------------------------------------------------------------------
# 10. D5 乱码强译复核：strong_garble_signal 纯函数
# ---------------------------------------------------------------------------

def test_strong_garble_signal_hits():
    """强信号命中：同假名连打≥6 / 2-4 字单元平铺≥4（与 _is_nonsense 同源）。"""
    assert sh.strong_garble_signal("あじゃあじゃあじゃあじゃ") == "无意义音节连缀"
    assert sh.strong_garble_signal("んああああああ") == "无意义音节连缀"
    assert sh.strong_garble_signal("ぱにぱにぱにぱに") == "无意义音节连缀"


def test_strong_garble_signal_negative():
    """负侧：真实台词/拖长音豁免/不足阈值/空串/含汉字。"""
    assert sh.strong_garble_signal("こんにちは") is None          # 真实台词
    assert sh.strong_garble_signal("あああああああ") is None      # 单假名拖长音豁免
    assert sh.strong_garble_signal("ぱにぱにぱに") is None        # 平铺×3 不足
    assert sh.strong_garble_signal("んあああああ") is None        # 连打×5 不足
    assert sh.strong_garble_signal("また明日") is None            # 含汉字非纯假名
    assert sh.strong_garble_signal("") is None
    assert sh.strong_garble_signal(None) is None


# ---------------------------------------------------------------------------
# 11. v1.2.2 C2 源侧计数类噪声判定：is_source_counting_noise 纯函数
# ---------------------------------------------------------------------------

def test_is_source_counting_noise_keep_list_words_are_not_noise():
    """keep_list 白名单词不算噪声（はい/うん/やめて 是实义应答），
    含汉字文本不算噪声（实义行）。"""
    assert sh.is_source_counting_noise("はい") is False
    assert sh.is_source_counting_noise("うん") is False
    assert sh.is_source_counting_noise("やめて") is False
    assert sh.is_source_counting_noise("うん。") is False        # 带句读规范化后仍白名单
    assert sh.is_source_counting_noise("また明日") is False      # 含汉字
    assert sh.is_source_counting_noise("気持ちいい") is False    # 含汉字


def test_is_source_counting_noise_repeat_features():
    """重复连打/单元平铺命中 → True（阈值与闸门0 计数类同源）。"""
    assert sh.is_source_counting_noise("ああああああ") is True        # 连打×6
    assert sh.is_source_counting_noise("んああああああ") is True      # 内嵌连打
    assert sh.is_source_counting_noise("あじゃあじゃあじゃあじゃ") is True  # 单元×4


def test_is_source_counting_noise_negatives():
    """负侧：空串/纯标点/普通台词/未达阈值不判噪（保守：无证据不判噪）。"""
    assert sh.is_source_counting_noise("") is False
    assert sh.is_source_counting_noise("。。。") is False
    assert sh.is_source_counting_noise("こんにちは") is False     # 普通台词
    assert sh.is_source_counting_noise("ああああ") is False       # 连打×4 不足
    assert sh.is_source_counting_noise("あああああ") is False     # 连打×5 不足
    assert sh.is_source_counting_noise("ぱにぱにぱに") is False   # 平铺×3 不足
    assert sh.is_source_counting_noise(None) is False


def test_is_source_counting_noise_rules_override():
    """rules 显式传入时使用该规则库（自定义 keep_list 白名单生效）。"""
    rules = sh.load_source_rules()
    assert sh.is_source_counting_noise("ああああああ", rules=rules) is True
    custom = dict(rules)
    custom["keep_list"] = list(rules["keep_list"]) + ["ああああああ"]
    assert sh.is_source_counting_noise("ああああああ", rules=custom) is False
