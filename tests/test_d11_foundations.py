"""D11 地基契约测试：RiskEvent additive 字段边界 + add_entry_ranged helper。

additive 钉：timing_range/entry_timings 只进 asdict 路径（风险清单.json
与事件 payload）；md 表格（_render_markdown）与 summary_lines 不感知。
"""

import json

from subtransjav.refine.risk import (
    SEVERITY_WARNING,
    RiskCollector,
)

_TIMING_A = "00:00:01,000 --> 00:00:02,000"
_TIMING_B = "00:00:03,000 --> 00:00:04,000"


def test_additive_fields_in_payload_not_in_md_nor_summary(tmp_path):
    """构造带 additive 字段的事件：payload（asdict 路径）含新字段，
    md 表格与 summary_lines 均不含（字段名与字段值都不出现）。"""
    c = RiskCollector()
    c.add(stage="A", file="x.srt", reason="规则清洗删除", action="删除条目",
          affected_count=2, severity=SEVERITY_WARNING,
          timing_range=f"{_TIMING_A}→{_TIMING_B}",
          entry_timings=[_TIMING_A, _TIMING_B])
    ev = c.to_payload()["events"][0]
    assert ev["timing_range"] == f"{_TIMING_A}→{_TIMING_B}"
    assert ev["entry_timings"] == [_TIMING_A, _TIMING_B]
    # md 表格不感知
    md = c._render_markdown()
    assert "timing_range" not in md and "entry_timings" not in md
    assert _TIMING_A not in md
    # summary_lines 不感知
    lines = c.summary_lines()
    assert all("timing_range" not in ln and _TIMING_A not in ln
               for ln in lines)
    # json 落盘与 payload 同路径（asdict），additive 字段随盘写出
    assert c.write_reports(str(tmp_path), "ep01") is not None
    data = json.loads(
        (tmp_path / "ep01_风险清单.json").read_text(encoding="utf-8"))
    assert data["events"][0]["entry_timings"] == [_TIMING_A, _TIMING_B]


def test_add_entry_ranged_multi_entries():
    """多条：entry_range 取 min-max（"12-15" 惯例），timing_range 为
    首条→末条，entry_timings 全量；affected_count=条数。"""
    timing_c = "00:00:05,000 --> 00:00:06,000"
    entries = [
        {"index": 12, "timing": _TIMING_A, "text": "甲"},
        {"index": 14, "timing": _TIMING_B, "text": "乙"},
        {"index": 15, "timing": timing_c, "text": "丙"},
    ]
    c = RiskCollector()
    ev = c.add_entry_ranged(stage="A", file="x.srt", entries=entries,
                            reason="批量删除", action="删除条目")
    assert ev.entry_range == "12-15"
    assert ev.timing_range == f"{_TIMING_A}→{timing_c}"
    assert ev.entry_timings == [_TIMING_A, _TIMING_B, timing_c]
    assert ev.affected_count == 3
    assert ev.severity == SEVERITY_WARNING
    assert c.has_risks is True


def test_add_entry_ranged_single_and_empty():
    """单条 entry_range 记 "N"；空列表安全：各范围字段记空串、
    affected_count=0，不抛异常。"""
    c = RiskCollector()
    ev = c.add_entry_ranged(stage="A", file="x.srt",
                            entries=[{"index": 7, "timing": _TIMING_A}],
                            reason="单条隔离")
    assert ev.entry_range == "7"
    assert ev.timing_range == f"{_TIMING_A}→{_TIMING_A}"
    assert ev.entry_timings == [_TIMING_A]
    ev2 = c.add_entry_ranged(stage="A", file="x.srt", entries=[],
                             reason="空批次")
    assert ev2.entry_range == "" and ev2.timing_range == ""
    assert ev2.entry_timings == [] and ev2.affected_count == 0
