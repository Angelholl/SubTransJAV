"""
术语冲突观察闸 / glossary 别名列 / learned 治理测试（v1.2.2 批次 D）。

覆盖：
- D-aliases：三列 CSV 解析、两列向后兼容、别名冲突豁免、注入仍用主译法；
- D1：冲突命中/无 glossary 场景、五元组 CSV 行数、观察闸 JSON 两次运行
  累计、转阻断建议行三态、glossary_conflict_block=True 冲突不入 TM；
- D2：【术语冲突观察】/【术语一致性】小节渲染（有/无/截断）、TM 摘要行两态；
- D3：glossary_learn_enabled=False 跳过学习、reset 工具 dry-run/--yes/--keep-term。

全部使用中性合成文本与临时文件；绝不触碰真实 glossary.csv 与真实 TM 库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import glossary_learned_reset as glr  # noqa: E402

from subtransjav.refine import pipeline_v2 as pv  # noqa: E402
from subtransjav.refine.config import RefineConfig  # noqa: E402
from subtransjav.refine.glossary import (  # noqa: E402
    format_glossary_block,
    load_glossary,
    load_glossary_ex,
    match_glossary,
)
from subtransjav.refine.glossary_conflict import (  # noqa: E402
    ADVICE_INSUFFICIENT,
    ADVICE_NOT_READY,
    ADVICE_READY,
    CONFLICT_CSV_COLUMNS,
    append_watch_record,
    evaluate_watch,
    load_watch_records,
    scan_glossary_conflicts,
    write_conflict_csv,
)
from subtransjav.refine.manifest import compute_config_hash as _cch  # noqa: E402
from subtransjav.refine.quality_report import (  # noqa: E402
    build_quality_report,
    render_conflict_section,
    render_term_consistency_section,
)
from subtransjav.refine.tm import TranslationMemory  # noqa: E402
from subtransjav.translate.llm_client import BatchResult  # noqa: E402

# ---------------------------------------------------------------------------
# 构造辅助（中性合成文本）
# ---------------------------------------------------------------------------

def _write_glossary_csv(path: Path, rows) -> Path:
    """rows = (source, target, aliases|None)；手写无表头 CSV 文本。

    注：加载器不跳表头（既有行为，真实 glossary.csv 的表头行同样会
    被载为一个词条），测试数据不写表头以免干扰下标断言。
    """
    lines = []
    for src, dst, aliases in rows:
        row = f"{src},{dst}"
        if aliases:
            row += f",{aliases}"
        lines.append(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def _entries(texts):
    """构造条目列表：index 从1开始，时间轴间隔1秒。"""
    return [{"index": i, "timing": f"00:00:0{i},000 --> 00:00:0{i},500",
             "text": t} for i, t in enumerate(texts, 1)]


GLOSSARY_3COL = [("ムラムラ", "心痒", "燥热|悸动"), ("IKU", "去了", None),
                 ("ドキドキ", "心跳", None)]


# ---------------------------------------------------------------------------
# D-aliases：三列/两列解析 + 注入主译法
# ---------------------------------------------------------------------------

class TestGlossaryAliases:

    def test_three_column_parse(self, tmp_path):
        p = _write_glossary_csv(tmp_path / "g.csv", GLOSSARY_3COL)
        entries = load_glossary_ex(str(p))
        assert entries[0] == ("ムラムラ", "心痒", ("燥热", "悸动"))
        assert entries[1] == ("IKU", "去了", ())
        assert entries[2] == ("ドキドキ", "心跳", ())

    def test_two_column_backward_compat(self, tmp_path):
        p = tmp_path / "g2.csv"
        p.write_text("ムラムラ,心痒\nIKU,去了\n", encoding="utf-8-sig")
        # 旧接口：两列元组，行为不变
        assert load_glossary(str(p)) == [("ムラムラ", "心痒"), ("IKU", "去了")]
        # 新接口：两列文件别名恒为空元组
        assert load_glossary_ex(str(p)) == [
            ("ムラムラ", "心痒", ()), ("IKU", "去了", ())]

    def test_merged_manual_alias_learned_none(self, tmp_path, monkeypatch):
        """merged：人工词库别名保留；learned 自学习词条无别名。"""
        p = _write_glossary_csv(tmp_path / "g.csv", GLOSSARY_3COL)
        learned = tmp_path / "learned.csv"
        learned.write_text("ザーメン,精液\n", encoding="utf-8-sig")
        import subtransjav.refine.pipeline_support as ps
        monkeypatch.setattr(ps, "learned_glossary_path", lambda: str(learned))
        merged = ps.load_glossary_merged(RefineConfig(glossary_path=str(p)))
        assert ("ムラムラ", "心痒", ("燥热", "悸动")) in merged
        assert ("ザーメン", "精液", ()) in merged

    def test_injection_uses_main_target_only(self, tmp_path):
        """命中/注入仍只用主译法：别名不进提示词块。"""
        p = _write_glossary_csv(tmp_path / "g.csv", GLOSSARY_3COL)
        glossary = load_glossary_ex(str(p))
        hits = match_glossary("ムラムラして IKU した", glossary)
        assert ("ムラムラ", "心痒") in hits
        block = format_glossary_block(hits)
        assert "ムラムラ → 心痒" in block
        assert "燥热" not in block and "悸动" not in block


# ---------------------------------------------------------------------------
# D1：冲突扫描
# ---------------------------------------------------------------------------

class TestScanConflicts:

    def _scan(self, final_texts, glossary):
        orig = _entries(["ムラムラしている。", "今日はいい天気だ。"])
        final = _entries(final_texts)
        return scan_glossary_conflicts(final, orig, glossary)

    def test_conflict_hit(self):
        glossary = [("ムラムラ", "心痒", ("悸动",))]
        r = self._scan(["心烦意乱。", "今天天气真好。"], glossary)
        assert len(r["conflicts"]) == 1
        c = r["conflicts"][0]
        assert c["entry_id"] == 1
        assert c["source_term"] == "ムラムラ"
        assert c["expected_targets"] == "心痒|悸动"
        assert c["actual_text"] == "心烦意乱。"
        st = r["term_stats"][0]
        assert (st["hits"], st["with_main"], st["with_alias"],
                st["with_neither"]) == (1, 0, 0, 1)

    def test_alias_exempt(self):
        """译文含别名 → 豁免，不记冲突。"""
        glossary = [("ムラムラ", "心痒", ("悸动",))]
        r = self._scan(["一阵悸动。", "今天天气真好。"], glossary)
        assert r["conflicts"] == []
        assert r["term_stats"][0]["with_alias"] == 1

    def test_main_target_no_conflict(self):
        glossary = [("ムラムラ", "心痒", ())]
        r = self._scan(["心痒难耐。", "今天天气真好。"], glossary)
        assert r["conflicts"] == []
        assert r["term_stats"][0]["with_main"] == 1

    def test_no_glossary(self):
        r = self._scan(["心烦意乱。", "今天天气真好。"], [])
        assert r == {"conflicts": [], "term_stats": []}

    def test_short_term_skipped(self):
        """源词 <2 字符不参与冲突判定。"""
        r = self._scan(["嗯。", "今天天气真好。"], [("ん", "嗯", ())])
        assert r["conflicts"] == []
        assert r["term_stats"][0]["hits"] == 0

    def test_unaligned_entry_skipped(self):
        """终稿条目无对应源文（时间轴对不齐）→ 不参与判定。"""
        glossary = [("ムラムラ", "心痒", ())]
        orig = _entries(["ムラムラしている。"])
        final = [{"index": 9, "timing": "00:09:09,000 --> 00:09:09,500",
                  "text": "心烦意乱。"}]
        r = scan_glossary_conflicts(final, orig, glossary)
        assert r["conflicts"] == []


class TestConflictCsv:

    def test_rows_and_columns(self, tmp_path):
        conflicts = [
            {"entry_id": 1, "timing": "00:00:01,000 --> 00:00:01,500",
             "source_term": "IKU", "expected_targets": "去了",
             "actual_text": "好想去。"},
            {"entry_id": 2, "timing": "00:00:02,000 --> 00:00:02,500",
             "source_term": "IKU", "expected_targets": "去了",
             "actual_text": "想走。"},
        ]
        out = tmp_path / "demo_术语冲突观察.csv"
        write_conflict_csv(str(out), conflicts)
        lines = out.read_text(encoding="utf-8-sig").splitlines()
        assert len(lines) == len(conflicts) + 1      # 表头 + 数据行
        assert lines[0].split(",") == CONFLICT_CSV_COLUMNS
        assert "created_at" in lines[0]
        assert "好想去。" in lines[1]


class TestWatchJson:

    def test_append_accumulates_two_runs(self, tmp_path):
        p = str(tmp_path / "watch.json")
        append_watch_record(p, "a.srt", {"IKU": {"candidates": 5,
                                                 "conflicts": 2}})
        append_watch_record(p, "a.srt", {"IKU": {"candidates": 7,
                                                 "conflicts": 1}})
        records = load_watch_records(p)
        assert len(records) == 2
        for r in records:
            assert set(r.keys()) == {"date", "source", "per_term",
                                     "manual_false_positive"}
        assert records[0]["source"] == "a.srt"
        assert records[0]["per_term"]["IKU"] == {"candidates": 5,
                                                 "conflicts": 2}
        assert records[1]["per_term"]["IKU"] == {"candidates": 7,
                                                 "conflicts": 1}
        assert records[0]["manual_false_positive"] == {}   # 预留字段

    def test_evaluate_three_states(self):
        def runs(cands):
            return [{"date": "d", "source": "s",
                     "per_term": {"x": {"candidates": c, "conflicts": 0}},
                     "manual_false_positive": {}} for c in cands]

        # 连续 3 次累计候选 <100 → 样本不足
        assert evaluate_watch(runs([10, 10, 10])) == ADVICE_INSUFFICIENT
        # 连续 3 次运行 + 0 误伤 → 满足
        assert evaluate_watch(runs([150, 150, 150])) == ADVICE_READY
        # 累计 ≥300 也可满足（与运行次数二选一）
        assert evaluate_watch(runs([150, 150])) == ADVICE_READY
        # 运行/样本都未达线 → 未满足
        assert evaluate_watch(runs([50, 40])) == ADVICE_NOT_READY
        # 受保护类别出现人工误伤 → 未满足
        recs = runs([150, 150, 150])
        recs[0]["manual_false_positive"] = {
            "ムラムラ": {"count": 1, "category": "人名"}}
        assert evaluate_watch(recs) == ADVICE_NOT_READY


# ---------------------------------------------------------------------------
# D1：glossary_conflict_block → 冲突不入 TM（store 层最小验证）
# ---------------------------------------------------------------------------

class TestConflictLearnBlock:

    def _setup(self, tmp_path):
        orig = _entries(["ムラムラしている。", "今日はいい天気だ。"])
        final = _entries(["心烦意乱。", "今天天气真好。"])
        glossary = [("ムラムラ", "心痒", ("悸动",))]
        scanned = scan_glossary_conflicts(final, orig, glossary)
        assert len(scanned["conflicts"]) == 1
        spans = {pv._timing_span(c["timing"])
                 for c in scanned["conflicts"]}
        tm = TranslationMemory(str(tmp_path / "tm.db"))
        return tm, orig, final, spans

    def test_block_true_skips_conflict(self, tmp_path):
        tm, orig, final, spans = self._setup(tmp_path)
        try:
            learned = pv._learn_to_tm(tm, orig, final, gate=True,
                                      conflict_spans=spans,
                                      block_conflicts=True)
            assert learned == 1                      # 仅干净行入库
            assert not tm.has_exact("ムラムラしている。", stage=1)
            assert tm.has_exact("今日はいい天気だ。", stage=1)
        finally:
            tm.close()

    def test_block_false_default_stores(self, tmp_path):
        tm, orig, final, spans = self._setup(tmp_path)
        try:
            learned = pv._learn_to_tm(tm, orig, final, gate=True,
                                      conflict_spans=spans,
                                      block_conflicts=False)
            assert learned == 2                      # 默认仅观察：照常入库
            assert tm.has_exact("ムラムラしている。", stage=1)
        finally:
            tm.close()


# ---------------------------------------------------------------------------
# D2：报告小节渲染
# ---------------------------------------------------------------------------

_CONFLICT = {"entry_id": 2, "timing": "00:00:02,000 --> 00:00:02,500",
             "source_term": "IKU", "expected_targets": "去了|イく",
             "actual_text": "好想去。"}


class TestReportSections:

    def test_conflict_section_states(self):
        assert render_conflict_section(None) == ""
        assert "无样本" in render_conflict_section([])
        text = render_conflict_section([dict(_CONFLICT)],
                                       watch_advice=ADVICE_INSUFFICIENT)
        assert "【术语冲突观察】" in text
        assert "共 1 条" in text
        assert "转阻断评估: 样本不足，仅观察" in text

    def test_conflict_section_truncation(self):
        many = [dict(_CONFLICT, entry_id=i) for i in range(25)]
        text = render_conflict_section(many)
        assert "共 25 条" in text
        assert "（其余 5 条略）" in text

    def test_consistency_section_states(self):
        assert render_term_consistency_section(None) == ""
        assert "无样本" in render_term_consistency_section([])
        stats = [{"term": "IKU", "target": "去了", "aliases": ("イく",),
                  "hits": 3, "with_main": 1, "with_alias": 1,
                  "with_neither": 1,
                  "samples": [{"entry_id": 2,
                               "timing": "00:00:02,000 --> 00:00:02,500",
                               "actual_text": "好想去。"}]}]
        text = render_term_consistency_section(stats)
        assert "【术语一致性】" in text
        assert "源词「IKU」→ 主译法「去了」" in text
        assert "命中 3 | 含主译法 1 | 含别名 1 | 均不含 1" in text

    def test_consistency_section_global_cap(self):
        """全章样本上限 20 条（10 术语 × 5 样本 = 30 候选 → 只显示 20）。"""
        stats = [{"term": f"TERM{i}", "target": "x", "aliases": (),
                  "hits": 5, "with_main": 0, "with_alias": 0,
                  "with_neither": 5,
                  "samples": [{"entry_id": i * 10 + j, "timing": "t",
                               "actual_text": "x"} for j in range(5)]}
                 for i in range(10)]
        text = render_term_consistency_section(stats)
        assert text.count("\n  #") == 20

    def test_section_order_and_defaults(self):
        """小节顺序：误听疑似改写 → 术语冲突观察 → 术语一致性 → 双引擎分歧；
        缺省调用（None）两章节整体省略、TM 行显示无样本。"""
        orig = _entries(["テスト。"])
        final = _entries(["测试。"])
        report = build_quality_report(
            orig, final, "demo", sidecar_review=[
                {"index": 1, "timing": final[0]["timing"], "suspect": "a",
                 "correct": "b", "zh_preview": "测试。"}],
            glossary_conflicts=[dict(_CONFLICT)],
            term_consistency=[{"term": "IKU", "target": "去了",
                               "aliases": (), "hits": 1, "with_main": 0,
                               "with_alias": 0, "with_neither": 1,
                               "samples": []}],
            tm_exact_hits=4, tm_learned_count=2)
        order = [report.index("【误听疑似改写】"),
                 report.index("【术语冲突观察】"),
                 report.index("【术语一致性】"),
                 report.index("【双引擎分歧】")]
        assert order == sorted(order)
        assert "TM: 精确命中 4 条 | 本次学习入库 2 条" in report

        plain = build_quality_report(orig, final, "demo")
        assert "【术语冲突观察】" not in plain
        assert "【术语一致性】" not in plain
        assert "TM: 无样本" in plain


# ---------------------------------------------------------------------------
# D3：learned 学习开关 + reset 工具
# ---------------------------------------------------------------------------

class TestLearnEnabledGate:

    def test_disabled_skips_learn(self, monkeypatch):
        calls = []
        import subtransjav.refine.glossary_learn as gl
        monkeypatch.setattr(gl, "learn_from_s2_output",
                            lambda *a, **k: calls.append((a, k)) or 0)
        cfg_off = RefineConfig(auto_glossary=True, glossary_learn_enabled=False)
        pv._auto_learn_glossary(cfg_off, "in.srt", "out.srt", threads=None)
        assert calls == []

    def test_default_enabled_learns(self, monkeypatch, tmp_path):
        calls = []
        import subtransjav.refine.glossary_learn as gl
        monkeypatch.setattr(gl, "learn_from_s2_output",
                            lambda *a, **k: calls.append((a, k)) or 0)
        cfg_on = RefineConfig(auto_glossary=True)
        assert cfg_on.glossary_learn_enabled is True
        pv._auto_learn_glossary(cfg_on, "in.srt", "out.srt", threads=None)
        assert len(calls) == 1


class TestLearnedResetTool:

    def _make_learned(self, tmp_path, rows=None):
        p = tmp_path / "glossary_learned.csv"
        rows = rows or [("ムラムラ", "心痒"), ("IKU", "去了"),
                        ("ドキドキ", "心跳")]
        p.write_text("\n".join(f"{s},{d}" for s, d in rows) + "\n",
                     encoding="utf-8-sig")
        return p, rows

    def test_dry_run_no_write(self, tmp_path, capsys):
        p, rows = self._make_learned(tmp_path)
        before = p.read_bytes()
        rc = glr.main(["--path", str(p)])
        assert rc == 0
        assert p.read_bytes() == before               # 文件零改动
        assert not list(tmp_path.glob("*.bak-*"))     # 无备份落盘
        assert "dry-run" in capsys.readouterr().out

    def test_yes_backup_then_clear(self, tmp_path):
        p, rows = self._make_learned(tmp_path)
        rc = glr.main(["--path", str(p), "--yes"])
        assert rc == 0
        baks = list(tmp_path.glob("*.bak-*"))
        assert len(baks) == 1                          # 备份先行
        assert glr.load_learned_glossary(str(baks[0])) == rows
        assert glr.load_learned_glossary(str(p)) == []  # 已清空

    def test_yes_keep_term(self, tmp_path):
        p, rows = self._make_learned(tmp_path)
        rc = glr.main(["--path", str(p), "--yes", "--keep-term", "iku"])
        assert rc == 0
        kept = glr.load_learned_glossary(str(p))
        assert kept == [("IKU", "去了")]               # ascii 忽略大小写
        baks = list(tmp_path.glob("*.bak-*"))
        assert glr.load_learned_glossary(str(baks[0])) == rows

    def test_missing_file_exit_2(self, tmp_path):
        assert glr.main(["--path", str(tmp_path / "nope.csv")]) == 2


# ---------------------------------------------------------------------------
# D1/D3：新 config 键默认值 + 参与指纹
# ---------------------------------------------------------------------------

def test_config_defaults_and_fingerprint():
    cfg = RefineConfig()
    assert cfg.glossary_conflict_block is False     # 默认仅观察
    assert cfg.glossary_learn_enabled is True       # 默认开
    base = _cch(cfg)
    assert _cch(RefineConfig(glossary_conflict_block=True)) != base
    assert _cch(RefineConfig(glossary_learn_enabled=False)) != base


# ---------------------------------------------------------------------------
# D1 端到端（假客户端，不联网）：有词库全链路
# ---------------------------------------------------------------------------

class _FakeClient:
    """脚本化假客户端：阶段A/B 返回固定合成译文（不含任何术语译法）。"""

    def __init__(self):
        self.system_texts = []

    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          progress=None):
        self.system_texts.append(system_text)
        is_stage_b = any("|||" in e["text"] for e in entries)
        translations = {e["index"]: ("审校合成稿" if is_stage_b
                                     else "初译合成稿") for e in entries}
        return BatchResult(translations=translations, deleted=set(),
                           failed=[])


def test_run_single_v2_conflict_e2e(tmp_path, monkeypatch):
    """有词库时 _run_single_v2 全链路：冲突扫描 → CSV/观察闸 JSON →
    报告【术语冲突观察】/【术语一致性】与转阻断建议行。观察闸 JSON 经
    monkeypatch 重定向到 tmp，绝不写真实 Temp/translation_memory。"""
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    in_srt = in_dir / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nムラムラしている\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nこんにちは\n", encoding="utf-8")
    glossary = _write_glossary_csv(tmp_path / "g.csv", GLOSSARY_3COL)
    watch_path = tmp_path / "watch" / "glossary_conflict_watch.json"

    cfg = RefineConfig(
        inputs=[str(in_srt)], output_dir=str(in_dir),
        glossary_path=str(glossary), tm_enabled=False,
        v2_profile="cloud", premerge_enabled=False,
        v2_source_filter="off", auto_glossary=False)

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)

    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: _FakeClient())
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    monkeypatch.setattr(pv, "default_watch_path", lambda: str(watch_path))

    out = pv._run_single_v2(cfg, str(in_srt))
    assert out.endswith("demo_final_cn.srt")

    # 冲突 CSV：表头 + 1 条（#1 源文含 ムラムラ，合成译文无主译法/别名）
    csv_lines = (in_dir / "demo_术语冲突观察.csv").read_text(
        encoding="utf-8-sig").splitlines()
    assert len(csv_lines) == 2
    assert "ムラムラ" in csv_lines[1]

    # 观察闸 JSON：一次运行记录，per_term 候选/冲突计数
    records = load_watch_records(str(watch_path))
    assert len(records) == 1
    assert records[0]["source"] == "demo.srt"
    assert records[0]["per_term"]["ムラムラ"] == {"candidates": 1,
                                                 "conflicts": 1}

    # 报告：两个新章节 + 首次运行建议行（未满足）+ TM 行无样本
    report = (in_dir / "demo_质量报告.txt").read_text(encoding="utf-8")
    assert "【术语冲突观察】" in report
    assert "【术语一致性】" in report
    assert f"转阻断评估: {ADVICE_NOT_READY}" in report
    assert "TM: 无样本" in report
