"""v1.2.2 批次 C1/C2 测试：per-片语境 sidecar 与噪声闸门的管线级契约。

覆盖：
- sidecar 文件解析（存在/不存在两态、小节骨架、注释与畸形行降级）；
- 剧情摘要块进入 A/B 两阶段提示词（FakeClient 断言冻结措辞逐字在场）；
- 误听怀疑词条按当前批次源文命中注入（未命中不注入该条）；
- cfg.context_sidecar 键禁用生效（含 user_settings.json / 环境变量分层）；
- 质量报告【误听疑似改写】小节渲染（有/无/上限截断/小节顺序）；
- 质量报告"纯假名实义保留"统计行（N/M 口径、键缺失不显示）；
- 端到端：终稿生成后误听疑似改写留痕进报告（假客户端，不联网）。
"""

import json

import pytest

from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine.config import RefineConfig, StageConfig
from subtransjav.refine.quality_report import build_quality_report
from subtransjav.translate.llm_client import BatchResult

# 冻结措辞（与任务冻结口径逐字一致；pipeline_v2 中的常量必须能逐字对上）
SUMMARY_NOTICE = ("以下为剧情背景参考，仅用于消解歧义；与单句字面义和术语表"
                  "冲突时，以句子本身和术语表为准；不得改写原文中没有的信息。")
MISHEAR_NOTICE = ("该词在 ASR 转写中曾出现误听（ペソ→疑为おへそ）。"
                  "仅当上下文无法按字面义解读、且存在语义更合理的解读时方可"
                  "按疑似义翻译；可两可时一律照字面译。")


# ---------------------------------------------------------------------------
# 工具（与 test_pipeline_v2 同款假实现，本文件自包含不跨文件导入）
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path, profile="cloud", **kw):
    cfg = RefineConfig(inputs=[], tm_enabled=False, v2_profile=profile, **kw)
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "fake-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "lmstudio", "fake-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    return cfg


def _entries(*texts):
    return [{"index": i, "timing": f"00:00:0{i},000 --> 00:00:0{i},500",
             "text": t} for i, t in enumerate(texts, 1)]


class FakeClient:
    """按 index 脚本化返回译文的假客户端，并捕获两阶段提示词。"""

    def __init__(self):
        self.calls = []
        self.system_texts = []
        self.user_prompts = []

    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          progress=None):
        self.calls.append([e["index"] for e in entries])
        self.system_texts.append(system_text)
        self.user_prompts.append(user_prompt)
        is_stage_b = any("|||" in e["text"] for e in entries)
        translations = {e["index"]: (f"审{e['index']}" if is_stage_b
                                     else f"译{e['index']}") for e in entries}
        return BatchResult(translations=translations, deleted=set(), failed=[])


def _write_sidecar(dir_path, stem, content):
    p = dir_path / f"{stem}.context.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _reset_grammar_cache():
    pv._GRAMMAR_CACHE.clear()
    yield
    pv._GRAMMAR_CACHE.clear()


# ---------------------------------------------------------------------------
# sidecar 文件加载与解析
# ---------------------------------------------------------------------------

def test_load_context_sidecar_missing_returns_none(tmp_path):
    assert pv.load_context_sidecar(str(tmp_path / "demo.srt")) is None


def test_load_context_sidecar_parses_two_sections(tmp_path):
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nペソ\n",
                      encoding="utf-8")
    _write_sidecar(tmp_path, "demo",
                   "# 模板注释行\n"
                   "【剧情摘要】\n海边度假背景。\n两名姐妹登场。\n"
                   "\n"
                   "【误听怀疑】\nペソ => おへそ\n"
                   "畸形行没有箭头\n"
                   "らめ=>だめ\n")          # 紧凑写法也应解析
    sc = pv.load_context_sidecar(str(in_srt))
    assert sc["summary"] == ["海边度假背景。", "两名姐妹登场。"]
    assert sc["mishear"] == [("ペソ", "おへそ"), ("らめ", "だめ")]


def test_load_context_sidecar_same_dir_same_stem(tmp_path):
    """与输入 srt 同目录同名、后缀 .context.md（子目录中的同名 srt 互不串扰）"""
    sub = tmp_path / "nested"
    sub.mkdir()
    in_srt = sub / "movie.srt"
    in_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
    assert pv.load_context_sidecar(str(in_srt)) is None
    _write_sidecar(sub, "movie", "【剧情摘要】\n背景。\n")
    sc = pv.load_context_sidecar(str(in_srt))
    assert sc["summary"] == ["背景。"]
    assert sc["mishear"] == []


# ---------------------------------------------------------------------------
# 注入块组装：剧情摘要恒注入 / 误听词条按命中注入
# ---------------------------------------------------------------------------

def test_sidecar_block_summary_and_hit():
    sidecar = {"summary": ["海边度假背景。"], "mishear": [("ペソ", "おへそ")]}
    block = pv._v2_sidecar_block("先頭にペソがある源文", sidecar)
    assert "【剧情摘要 - 语境参考】" in block
    assert SUMMARY_NOTICE in block                    # 冻结措辞逐字在场
    assert "海边度假背景。" in block
    assert MISHEAR_NOTICE in block                    # 冻结措辞逐字在场


def test_sidecar_block_mishear_miss_not_injected():
    """当前批次源文不含疑似词 → 该词条不注入（剧情摘要仍注入）"""
    sidecar = {"summary": ["背景。"], "mishear": [("ペソ", "おへそ")]}
    block = pv._v2_sidecar_block("これはただの台詞", sidecar)
    assert SUMMARY_NOTICE in block
    assert "【误听怀疑对照】" not in block
    assert "ペソ" not in block


def test_sidecar_block_empty_sidecar_returns_empty():
    assert pv._v2_sidecar_block("任意", None) == ""
    assert pv._v2_sidecar_block("任意", {"summary": [], "mishear": []}) == ""


# ---------------------------------------------------------------------------
# A/B 两阶段提示词注入（假客户端断言）
# ---------------------------------------------------------------------------

def test_stage_a_prompt_contains_frozen_wordings(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    entries = _entries("ペソが出る", "こんにちは")
    sidecar = {"summary": ["海边度假背景。"], "mishear": [("ペソ", "おへそ")]}
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    pv._run_stage_a(cfg, entries, None, str(tmp_path), [], sidecar=sidecar)
    assert len(fake.system_texts) == 1
    assert SUMMARY_NOTICE in fake.system_texts[0]
    assert MISHEAR_NOTICE in fake.system_texts[0]


def test_stage_b_prompt_contains_frozen_wordings(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    entries = _entries("ペソが出る")
    a = pv.StageAResult(
        entries=[{"index": 1, "timing": entries[0]["timing"], "text": "译1"}],
        deleted=set(), failed=set(), exact_hits={})
    sidecar = {"summary": ["海边度假背景。"], "mishear": [("ペソ", "おへそ")]}
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    pv._run_stage_b(cfg, a, entries, str(tmp_path), [], sidecar=sidecar)
    assert len(fake.system_texts) == 1
    assert SUMMARY_NOTICE in fake.system_texts[0]
    assert MISHEAR_NOTICE in fake.system_texts[0]


def test_stage_prompt_mishear_miss_only_summary(tmp_path, monkeypatch):
    """批次源文未命中疑似词 → 提示词只有剧情摘要块，无词条（两态之二）"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは")
    sidecar = {"summary": ["海边度假背景。"], "mishear": [("ペソ", "おへそ")]}
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    pv._run_stage_a(cfg, entries, None, str(tmp_path), [], sidecar=sidecar)
    assert SUMMARY_NOTICE in fake.system_texts[0]
    assert "【误听怀疑对照】" not in fake.system_texts[0]


def test_no_sidecar_no_injection(tmp_path, monkeypatch):
    """sidecar 未提供 → A/B 提示词均无注入块（文件不存在=无注入）"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは")
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    a = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    assert SUMMARY_NOTICE not in fake.system_texts[0]
    pv._run_stage_b(cfg, a, entries, str(tmp_path), [])
    assert SUMMARY_NOTICE not in fake.system_texts[1]


# ---------------------------------------------------------------------------
# cfg.context_sidecar 开关（含分层配置）
# ---------------------------------------------------------------------------

def test_context_sidecar_disabled_by_cfg(tmp_path):
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nペソ\n",
                      encoding="utf-8")
    _write_sidecar(tmp_path, "demo", "【剧情摘要】\n背景。\n")
    cfg = _make_cfg(tmp_path)
    assert cfg.context_sidecar is True               # 默认开
    assert pv._load_context_sidecar(cfg, str(in_srt)) is not None
    cfg.context_sidecar = False                      # 键为 false=禁用
    assert pv._load_context_sidecar(cfg, str(in_srt)) is None


def test_context_sidecar_user_settings_layering(tmp_path, monkeypatch):
    from subtransjav.refine import config as rc
    monkeypatch.setattr(rc, "CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SUBTRANSJAV_CONTEXT_SIDECAR", raising=False)
    (tmp_path / "user_settings.json").write_text(
        json.dumps({"context_sidecar": False}), encoding="utf-8")
    assert RefineConfig().context_sidecar is False   # 用户文件覆盖默认
    # CLI/GUI 显式赋值最高优先级（非默认值不会被用户文件覆盖）
    assert RefineConfig(context_sidecar=False).context_sidecar is False
    # 环境变量（用户文件清空后单独验证白名单字面量）
    (tmp_path / "user_settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SUBTRANSJAV_CONTEXT_SIDECAR", "false")
    assert RefineConfig().context_sidecar is False
    monkeypatch.setenv("SUBTRANSJAV_CONTEXT_SIDECAR", "true")
    assert RefineConfig().context_sidecar is True
    monkeypatch.setenv("SUBTRANSJAV_CONTEXT_SIDECAR", "bogus")
    assert RefineConfig().context_sidecar is True    # 非法值回退默认


def test_config_hash_sensitive_to_context_sidecar():
    """context_sidecar 参与 manifest 配置指纹（开关变化使旧产物失效）"""
    import types

    from subtransjav.refine.manifest import compute_config_hash
    fields = dict(profile=None,
                  stages=[StageConfig(0, True, "lmstudio", "m")],
                  endpoints={}, batch_local=30, batch_cloud=30,
                  batch_size_stable=True, v2_profile="local",
                  v2_concurrency=1, v2_ctx_local=32768,
                  v2_keep_untranslated="original", premerge_enabled=True,
                  tm_enabled=True, tm_threshold=0.85, tm_fuzzy_inject=True,
                  tm_fuzzy_threshold=0.98, tm_learn_gate=True,
                  apply_glossary_stage1=True, apply_glossary_stage2=True,
                  context_sidecar=True, fallback_local=False,
                  fallback_model="", templates_dir="", cleaner_config_dir="")
    h1 = compute_config_hash(types.SimpleNamespace(**fields))
    fields["context_sidecar"] = False
    h2 = compute_config_hash(types.SimpleNamespace(**fields))
    assert h1 != h2


# ---------------------------------------------------------------------------
# 质量报告：【误听疑似改写】小节 + "纯假名实义保留"统计行
# ---------------------------------------------------------------------------

_T1 = "00:00:01,000 --> 00:00:02,000"
_T2 = "00:00:03,000 --> 00:00:04,000"


def _report_fixtures():
    orig = [{"index": 1, "timing": _T1, "text": "ペソが出る"},
            {"index": 2, "timing": _T2, "text": "こんにちは"}]
    final = [{"index": 1, "timing": _T1, "text": "肚脐出来了"},
             {"index": 2, "timing": _T2, "text": "你好"}]
    return orig, final


def test_mishear_review_section_rendered():
    orig, final = _report_fixtures()
    review = [{"index": 1, "timing": _T1, "suspect": "ペソ",
               "correct": "おへそ", "zh_preview": "肚脐出来了"}]
    report = build_quality_report(orig, final, "demo", sidecar_review=review)
    assert "【误听疑似改写】" in report
    assert "共 1 条" in report
    assert "#1 00:00:01,000 --> 00:00:02,000 [ペソ→疑为おへそ]" in report
    assert "译: 肚脐出来了" in report


def test_mishear_review_section_empty_and_none():
    orig, final = _report_fixtures()
    report0 = build_quality_report(orig, final, "demo", sidecar_review=[])
    lines0 = report0.splitlines()
    i0 = lines0.index("【误听疑似改写】")
    assert lines0[i0 + 1] == "无样本"
    # 未提供（sidecar 未启用/无误听表）：整节省略
    assert "【误听疑似改写】" not in build_quality_report(orig, final, "demo")


def test_mishear_review_section_order_and_cap():
    orig, final = _report_fixtures()
    garble = [{"index": 2, "timing": _T2, "src_preview": "んああああああ",
               "zh_preview": "你好呀", "signal": "闸门0计数类检出"}]
    review = [{"index": 1, "timing": _T1, "suspect": "ペソ",
               "correct": "おへそ", "zh_preview": "肚脐出来了"}]
    report = build_quality_report(orig, final, "demo",
                                  gate0_deletions={"total": 0, "by_category": {},
                                                   "samples": []},
                                  garble_review=garble,
                                  sidecar_review=review)
    # 小节顺序：【处置】 → 【乱码强译复核】 → 【误听疑似改写】 → 【双引擎分歧】
    assert report.index("【处置】") < report.index("【乱码强译复核】") \
        < report.index("【误听疑似改写】") < report.index("【双引擎分歧】")
    # 上限 25 条 → 列 20 条 + "其余 5 条略"
    many = [{"index": i, "timing": _T1, "suspect": "ペソ",
             "correct": "おへそ", "zh_preview": "预览"} for i in range(25)]
    report25 = build_quality_report(orig, final, "demo", sidecar_review=many)
    assert "（其余 5 条略）" in report25
    assert report25.count("\n  #") == 20


def test_noise_gate_stats_line():
    """统计段新增行：N=噪声闸门免删条数，M=其中终稿带 [未翻译] 前缀的数量"""
    final = [{"index": 1, "timing": _T1, "text": "不要。"},
             {"index": 2, "timing": _T2, "text": "[未翻译] うん"}]
    orig = [{"index": 1, "timing": _T1, "text": "やめて"},
            {"index": 2, "timing": _T2, "text": "うん"}]
    merge_stats = {"premerge_merged": 0, "clean_merged": 0,
                   "clean_kept_by_noise_gate": 2,
                   "clean_kept_by_noise_gate_timings": [_T1, _T2]}
    report = build_quality_report(orig, final, "demo", merge_stats=merge_stats)
    assert "纯假名实义保留: 2（其中 [未翻译] 标记 1）" in report


def test_noise_gate_stats_line_absent_without_key():
    """规则清洗未运行/旧调用方（键缺失）→ 不显示该行"""
    orig, final = _report_fixtures()
    report = build_quality_report(orig, final, "demo",
                                  merge_stats={"premerge_merged": 0,
                                               "clean_merged": 0})
    assert "纯假名实义保留" not in report


def test_noise_gate_stats_line_unaligned_counts_zero():
    """时间轴对不齐的保留条目 → [未翻译] 标记计 0（保守口径）"""
    final = [{"index": 1, "timing": _T1, "text": "不要。"}]
    orig = [{"index": 1, "timing": _T1, "text": "やめて"}]
    merge_stats = {"premerge_merged": 0, "clean_merged": 0,
                   "clean_kept_by_noise_gate": 1,
                   "clean_kept_by_noise_gate_timings": [
                       "00:00:09,000 --> 00:00:09,500"]}
    report = build_quality_report(orig, final, "demo", merge_stats=merge_stats)
    assert "纯假名实义保留: 1（其中 [未翻译] 标记 0）" in report


# ---------------------------------------------------------------------------
# 端到端（假客户端）：sidecar 注入两态 + 终稿改写留痕进报告
# ---------------------------------------------------------------------------

def _setup_e2e(tmp_path, monkeypatch, with_sidecar=True, disable=False):
    in_dir = tmp_path / "in"
    in_dir.mkdir(exist_ok=True)
    in_srt = in_dir / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nペソが出る\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nこんにちは\n", encoding="utf-8")
    if with_sidecar:
        _write_sidecar(in_dir, "demo",
                       "【剧情摘要】\n海边度假背景。\n\n【误听怀疑】\n"
                       "ペソ => おへそ\n")
    cfg = _make_cfg(tmp_path)
    if disable:
        cfg.context_sidecar = False

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    return cfg, str(in_srt), in_dir, fake


def test_run_single_v2_sidecar_injection_and_review(tmp_path, monkeypatch):
    cfg, in_path, in_dir, fake = _setup_e2e(tmp_path, monkeypatch)
    out = pv._run_single_v2(cfg, in_path)
    assert out.endswith("demo_final_cn.srt")
    # A/B 两阶段提示词均含冻结措辞（命中词条 + 摘要块）
    assert len(fake.system_texts) == 2
    for st in fake.system_texts:
        assert SUMMARY_NOTICE in st
        assert MISHEAR_NOTICE in st
    # 终稿生成后：源文命中疑似词的条目列入【误听疑似改写】（改写留痕）
    report = (in_dir / "demo_质量报告.txt").read_text(encoding="utf-8")
    assert "【误听疑似改写】" in report
    assert "共 1 条" in report
    assert "[ペソ→疑为おへそ]" in report
    assert "译: 审1" in report


def test_run_single_v2_sidecar_disabled_no_section(tmp_path, monkeypatch):
    cfg, in_path, in_dir, fake = _setup_e2e(tmp_path, monkeypatch,
                                            disable=True)
    out = pv._run_single_v2(cfg, in_path)
    assert out.endswith("demo_final_cn.srt")
    assert len(fake.system_texts) == 2
    for st in fake.system_texts:
        assert SUMMARY_NOTICE not in st
    report = (in_dir / "demo_质量报告.txt").read_text(encoding="utf-8")
    assert "【误听疑似改写】" not in report


def test_run_single_v2_no_sidecar_file_no_section(tmp_path, monkeypatch):
    cfg, in_path, in_dir, _fake = _setup_e2e(tmp_path, monkeypatch,
                                             with_sidecar=False)
    out = pv._run_single_v2(cfg, in_path)
    assert out.endswith("demo_final_cn.srt")
    report = (in_dir / "demo_质量报告.txt").read_text(encoding="utf-8")
    assert "【误听疑似改写】" not in report
