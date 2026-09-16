"""风险事件模型契约测试：样本截断 / content_degraded 语义 / 事件镜像 / 报告落盘。"""

import io
import json

from subtransjav.refine.events import EventEmitter, parse_event_line
from subtransjav.refine.risk import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    RiskCollector,
    RiskEvent,
)

# ---------------------------------------------------------------------------
# RiskEvent 基本语义
# ---------------------------------------------------------------------------

def test_risk_event_defaults_and_timestamp():
    ev = RiskEvent(stage="A", file="x.srt")
    assert ev.entry_range == "" and ev.reason == "" and ev.action == ""
    assert ev.affected_count == 0 and ev.samples == [] and ev.suggestion == ""
    assert ev.severity == SEVERITY_WARNING
    # 自动秒级 ISO 时间戳
    assert len(ev.timestamp) == 19 and ev.timestamp[10] == "T"


def test_samples_truncated_to_three():
    c = RiskCollector()
    ev = c.add(stage="A", file="x.srt", reason="r",
               samples=["原文1", "原文2", "原文3", "原文4", "原文5"])
    assert ev.samples == ["原文1", "原文2", "原文3"]
    # 少于 3 条不截断、None 安全
    assert c.add(stage="A", file="x.srt", samples=["仅一条"]).samples == ["仅一条"]
    assert c.add(stage="A", file="x.srt").samples == []


def test_severity_constants():
    assert SEVERITY_INFO == "info"
    assert SEVERITY_WARNING == "warning"
    assert SEVERITY_CRITICAL == "critical"


# ---------------------------------------------------------------------------
# content_degraded 语义
# ---------------------------------------------------------------------------

def test_info_severity_not_content_degraded():
    c = RiskCollector()
    c.add(stage="A", file="x.srt", reason="提示性信息", action="保留原文",
          affected_count=2, severity=SEVERITY_INFO)
    assert c.has_risks is True            # info 也算风险记录
    assert c.content_degraded is False    # 但不算内容降级


def test_content_degraded_requires_action_and_count():
    c = RiskCollector()
    c.add(stage="A", file="x.srt", reason="r", action="", affected_count=5)      # 无动作
    c.add(stage="A", file="x.srt", reason="r", action="保留原文", affected_count=0)  # 无影响
    assert c.content_degraded is False
    c.add(stage="A", file="x.srt", reason="r", action="保留原文",
          affected_count=3, severity=SEVERITY_WARNING)
    assert c.content_degraded is True


# ---------------------------------------------------------------------------
# 事件流镜像（attach_emitter）
# ---------------------------------------------------------------------------

def _events_of(buf: io.StringIO) -> list:
    return [e for e in (parse_event_line(ln) for ln in buf.getvalue().splitlines()) if e]


def test_attach_emitter_info_maps_to_warning_event():
    buf = io.StringIO()
    c = RiskCollector()
    c.attach_emitter(EventEmitter(stream=buf, task_id="t1"))
    c.add(stage="A", file="x.srt", entry_range="3-4", reason="轻微迟疑未处理",
          severity=SEVERITY_INFO)
    events = _events_of(buf)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "warning"
    assert ev["task_id"] == "t1"
    assert ev["phase"] == "A"
    assert ev["file"] == "x.srt"
    # payload = 风险事件字段 dict
    assert ev["payload"]["severity"] == "info"
    assert ev["payload"]["entry_range"] == "3-4"
    assert ev["payload"]["reason"] == "轻微迟疑未处理"


def test_attach_emitter_warning_critical_map_to_degraded_event():
    buf = io.StringIO()
    c = RiskCollector()
    c.attach_emitter(EventEmitter(stream=buf, task_id="t1"))
    c.add(stage="B", file="y.srt", reason="审校失败", action="保留原文",
          affected_count=2, severity=SEVERITY_WARNING)
    c.add(stage="final", file="y.srt", reason="误译拦截", action="回退阶段A译文",
          affected_count=1, severity=SEVERITY_CRITICAL)
    events = _events_of(buf)
    assert [e["type"] for e in events] == ["degraded", "degraded"]
    assert [e["seq"] for e in events] == [1, 2]
    assert [e["phase"] for e in events] == ["B", "final"]


# ---------------------------------------------------------------------------
# mark_untranslated_majority
# ---------------------------------------------------------------------------

def test_mark_untranslated_majority():
    c = RiskCollector()
    assert c.has_risks is False
    c.mark_untranslated_majority(file="z.srt", total=100, kept=70)
    assert c.untranslated_majority is True
    assert c.content_degraded is True
    assert c.has_risks is True
    payload = c.to_payload()
    assert payload["untranslated_majority"] is True
    assert payload["risk_count"] == 1
    lines = c.summary_lines()
    assert lines and any("未翻译" in ln for ln in lines)
    # 落盘的事件也体现
    ev = payload["events"][0]
    assert ev["severity"] == SEVERITY_CRITICAL
    assert ev["affected_count"] == 30
    assert "70/100" in ev["reason"]


# ---------------------------------------------------------------------------
# summary_lines / to_payload
# ---------------------------------------------------------------------------

def test_summary_lines_empty_when_no_risks():
    assert RiskCollector().summary_lines() == []
    assert RiskCollector().to_payload() == {"risk_count": 0, "untranslated_majority": False, "events": []}


def test_summary_lines_format_and_counts():
    c = RiskCollector()
    c.add(stage="A", file="x.srt", reason="云端超时", action="本地接管",
          affected_count=4, suggestion="检查网络", severity=SEVERITY_WARNING)
    lines = c.summary_lines()
    assert len(lines) == 2
    assert lines[0].startswith("⚠️")
    assert "云端超时" in lines[0] and "本地接管" in lines[0]
    assert "共 1 项" in lines[-1]


def test_add_summary_line_is_info_only_channel():
    """R8 信息行通道：随 summary_lines 输出，但不计入风险事件
    （has_risks / to_payload / 风险清单落盘均不受影响）。"""
    c = RiskCollector()
    c.add_summary_line("🚪 闸门0：删除 1/原始 3，检出计数 1（default 档，保险阀未触发）")
    lines = c.summary_lines()
    assert lines == ["🚪 闸门0：删除 1/原始 3，检出计数 1（default 档，保险阀未触发）"]
    assert c.has_risks is False
    assert c.to_payload()["risk_count"] == 0
    # 风险事件与信息行共存：风险行在前，信息行追加其后
    c.add(stage="gate0", file="x.srt", reason="r", action="a", affected_count=1)
    c.add_summary_line("第二行")
    lines = c.summary_lines()
    assert lines[-1] == "第二行" and lines[0].startswith("⚠️")
    assert "共 1 项" in lines[1]
    # 空行防御
    c.add_summary_line("")
    c.add_summary_line(None)
    assert len(c.summary_lines()) == 4


# ---------------------------------------------------------------------------
# write_reports
# ---------------------------------------------------------------------------

def test_write_reports_generates_md_and_json(tmp_path):
    c = RiskCollector()
    c.add(stage="A", file="x.srt", entry_range="12-15", reason="云端超时",
          action="本地接管", affected_count=4, samples=["原1", "原2"],
          suggestion="重试", severity=SEVERITY_WARNING)
    result = c.write_reports(str(tmp_path), "ep01")
    assert result is not None
    md_path, json_path = result["md"], result["json"]
    with open(md_path, encoding="utf-8") as f:
        md_text = f.read()
    # Markdown 表格八列齐全，且内容体现
    for header in ("阶段", "文件", "条目范围", "原因", "降级动作", "影响条数", "建议", "严重度"):
        assert header in md_text
    assert "云端超时" in md_text and "12-15" in md_text
    with open(json_path, encoding="utf-8") as f:
        data = json.loads(f.read())
    assert data["risk_count"] == 1
    assert data["events"][0]["reason"] == "云端超时"
    assert data["events"][0]["samples"] == ["原1", "原2"]


def test_write_reports_no_risks_writes_nothing(tmp_path):
    c = RiskCollector()
    result = c.write_reports(str(tmp_path), "ep01")
    assert result is None
    assert list(tmp_path.iterdir()) == []    # 不写任何文件
