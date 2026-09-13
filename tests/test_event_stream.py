"""GUI 侧事件流解析器测试（webview-free，纯函数）。

用 refine.events.EventEmitter(stream=io.StringIO()) 生成真实 NDJSON 事件行，
喂给 webview_gui.event_stream.EventStreamParser 断言聚合行为；
另覆盖 api 侧抽出的纯函数帮助（format_event_line / resume_state_for_path）。
"""

import io
import time
from pathlib import Path

from subtransjav.refine.events import EventEmitter
from subtransjav.webview_gui.event_stream import (
    RISKS_CAP,
    EventStreamParser,
    format_event_line,
    resume_state_for_path,
    stage_label,
)


def _make_parser_with_events(*events) -> EventStreamParser:
    """用 EventEmitter 生成事件行并逐行喂入 parser，返回 parser。"""
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="testtask")
    for ev in events:
        em.emit(*ev[0], **ev[1])
    parser = EventStreamParser()
    for line in buf.getvalue().splitlines():
        if line.strip():
            parser.feed(line)
    return parser


def _feed_lines(parser: EventStreamParser, text: str) -> list:
    """把多行文本喂入 parser，返回每行 feed 的返回值。"""
    return [parser.feed(ln) for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# 阶段切换
# ---------------------------------------------------------------------------

def test_phase_started_switches_stage_label():
    parser = _make_parser_with_events(
        (("phase_started",), {"phase": "A"}),
        (("phase_started",), {"phase": "B"}),
    )
    snap = parser.snapshot()
    assert snap["stage"] == "阶段B 审校+抛光"
    assert snap["ndjson_mode"] is True


def test_phase_started_resets_progress():
    parser = _make_parser_with_events(
        (("phase_started",), {"phase": "A"}),
        (("phase_progress",), {"phase": "A", "payload": {"done": 40, "total": 100}}),
        (("phase_started",), {"phase": "B"}),
    )
    snap = parser.snapshot()
    assert snap["done"] == 0 and snap["total"] == 0 and snap["progress"] == 0


def test_stage_label_mapping():
    assert stage_label("A") == "阶段A 净语+翻译"
    assert stage_label("s3") == "阶段B 审校+抛光"
    assert stage_label("自定义阶段") == "自定义阶段"    # 未知值透传
    assert stage_label(None) == ""
    assert stage_label("") == ""


# ---------------------------------------------------------------------------
# 进度聚合
# ---------------------------------------------------------------------------

def test_phase_progress_aggregates_done_total():
    parser = _make_parser_with_events(
        (("phase_started",), {"phase": "A"}),
        (("phase_progress",), {"phase": "A",
                               "payload": {"done": 30, "total": 100}}),
    )
    snap = parser.snapshot()
    assert snap["done"] == 30
    assert snap["total"] == 100
    assert snap["progress"] == 30
    assert snap["current_file"] and "已翻译约 30/100" in snap["current_file"]


def test_phase_progress_batch_info_recorded():
    parser = _make_parser_with_events(
        (("phase_progress",), {"phase": "B",
                               "payload": {"done": 60, "total": 200,
                                           "batch": 3, "batch_total": 10}}),
    )
    assert parser.batch_done == 3
    assert parser.batch_total == 10
    assert parser.snapshot()["progress"] == 30


def test_event_progress_overrides_legacy_totals():
    parser = EventStreamParser()
    _feed_lines(parser, "Translating 50 lines in 2 scenes\n")
    assert parser.snapshot()["total"] == 50
    _make = io.StringIO()
    EventEmitter(stream=_make, task_id="t").emit(
        "phase_progress", phase="A", payload={"done": 10, "total": 200})
    _feed_lines(parser, _make.getvalue())
    snap = parser.snapshot()
    assert snap["total"] == 200 and snap["done"] == 10    # 事件优先


def test_progress_clamped_to_100():
    parser = _make_parser_with_events(
        (("phase_progress",), {"phase": "A",
                               "payload": {"done": 150, "total": 100}}),
    )
    assert parser.snapshot()["progress"] == 100


# ---------------------------------------------------------------------------
# 风险累积与上限
# ---------------------------------------------------------------------------

def test_risks_accumulate_and_cap():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="t")
    for i in range(150):
        em.emit("degraded", phase="A", payload={"message": f"risk-{i}"})
    parser = EventStreamParser()
    _feed_lines(parser, buf.getvalue())

    assert parser.risk_count == 150                       # 去重后总数不受截断影响
    assert len(parser.risks) == RISKS_CAP == 100          # 明细上限 100
    snap = parser.snapshot()
    assert len(snap["risks"]) == 50                       # snapshot 只回最近 50
    assert snap["risks"][-1]["message"] == "risk-149"     # 最近 50 条的收尾
    assert snap["risks"][0]["message"] == "risk-100"
    assert snap["risk_count"] == 150


def test_risk_dedup_same_payload():
    parser = _make_parser_with_events(
        (("warning",), {"phase": "A", "payload": {"message": "重复风险"}}),
        (("warning",), {"phase": "A", "payload": {"message": "重复风险"}}),
    )
    assert parser.risk_count == 1
    assert len(parser.risks) == 1


def test_error_event_sets_error_and_counts_as_risk():
    parser = _make_parser_with_events(
        (("error",), {"phase": "A", "payload": {"message": "boom"}}),
    )
    snap = parser.snapshot()
    assert snap["error"] == "boom"
    assert snap["risk_count"] == 1


# ---------------------------------------------------------------------------
# task_finished 摘要
# ---------------------------------------------------------------------------

def test_task_finished_summary_captured():
    parser = _make_parser_with_events(
        (("task_finished",), {"payload": {"exit_code": 0,
                                          "untranslated_majority": True}}),
    )
    snap = parser.snapshot()
    assert snap["task_summary"] == {"exit_code": 0, "untranslated_majority": True}
    assert snap["untranslated_majority"] is True


def test_task_finished_without_majority():
    parser = _make_parser_with_events(
        (("task_finished",), {"payload": {"exit_code": 0}}),
    )
    snap = parser.snapshot()
    assert snap["task_summary"] == {"exit_code": 0}
    assert snap["untranslated_majority"] is False


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------

def test_heartbeat_age_advances():
    parser = EventStreamParser()
    assert parser.snapshot()["heartbeat_age"] is None    # 无事件时为 None

    buf = io.StringIO()
    EventEmitter(stream=buf, task_id="t").emit("heartbeat", payload={"elapsed_s": 1})
    _feed_lines(parser, buf.getvalue())
    snap = parser.snapshot()
    assert snap["heartbeat_age"] is not None
    assert snap["heartbeat_age"] < 5                     # 刚发生的事件

    parser.last_event_ts -= 30                           # 白盒回拨验证推进
    assert parser.snapshot()["heartbeat_age"] >= 29


def test_heartbeat_does_not_disturb_progress_state():
    parser = _make_parser_with_events(
        (("phase_progress",), {"phase": "A",
                               "payload": {"done": 5, "total": 10}}),
        (("heartbeat",), {"payload": {"elapsed_s": 2}}),
    )
    snap = parser.snapshot()
    assert snap["done"] == 5 and snap["total"] == 10
    assert snap["heartbeat_age"] is not None


# ---------------------------------------------------------------------------
# 非事件行 → 遗留兼容层
# ---------------------------------------------------------------------------

def test_invalid_line_returns_none_and_legacy_regex_applies():
    parser = EventStreamParser()
    rets = _feed_lines(
        parser,
        "这是普通日志文本\n"
        "[WARN] 2026-09-12 something\n"
        "Translating 1875 lines in 2 scenes\n"
        "[STAGE] 阶段1 日译中翻译\n",
    )
    assert all(r is None for r in rets)                  # 全部不是事件
    snap = parser.snapshot()
    assert snap["total"] == 1875
    assert snap["stage"] == "阶段1 日译中翻译"
    assert snap["ndjson_mode"] is False                  # 未见到事件


def test_chinese_batch_regex_still_works():
    parser = EventStreamParser()
    _feed_lines(parser, "[llm] 共 100 条，分 5 批（并发=2）\n")
    assert parser.lines_total == 100
    assert parser.batch_total == 5
    _feed_lines(parser, "⏳ 批次 3/5\n")
    snap = parser.snapshot()
    assert parser.batch_done == 3
    assert snap["done"] == 60
    assert snap["progress"] == 60
    assert snap["current_file"] and "批次 3/5" in snap["current_file"]


def test_scene_batch_legacy_regex():
    parser = EventStreamParser()
    _feed_lines(
        parser,
        "Translating 20 lines in 1 scenes\n"
        "Scene 1 batch 1: 10 lines and 0 untranslated.\n"
        "Scene 1 batch 2: 5 lines and 1 untranslated.\n"   # 有未翻译不计入
        "Scene 1 batch 3: 10 lines and 0 untranslated.\n",
    )
    snap = parser.snapshot()
    assert snap["done"] == 20                             # 10 + 0 + 10


def test_legacy_error_literals():
    parser = EventStreamParser()
    _feed_lines(parser, "TRANSLATION FAILED\n")
    assert parser.snapshot()["error"] == \
        "Translation failed — no subtitles were translated"

    parser2 = EventStreamParser()
    _feed_lines(parser2, "[refine] 执行失败：RegionError\n")
    assert "执行失败" in parser2.snapshot()["error"]

    parser3 = EventStreamParser()
    _feed_lines(parser3, "Failed: connection refused\n")
    assert "connection refused" in parser3.snapshot()["error"]


def test_ndjson_mode_flips_on_first_event():
    parser = EventStreamParser()
    assert parser.snapshot()["ndjson_mode"] is False
    _feed_lines(parser, "普通日志先行\n")
    assert parser.snapshot()["ndjson_mode"] is False      # 遗留行不翻转
    buf = io.StringIO()
    EventEmitter(stream=buf, task_id="t").emit("task_started")
    _feed_lines(parser, buf.getvalue())
    assert parser.snapshot()["ndjson_mode"] is True
    _feed_lines(parser, "事件之后再来的普通日志\n")
    assert parser.snapshot()["ndjson_mode"] is True       # 双轨期间保持 True


def test_mixed_stream_event_and_legacy_lines():
    """事件与遗留行混合输入（过渡期真实形态）。"""
    parser = EventStreamParser()
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="t")
    em.emit("phase_started", phase="A")
    text = "some chatty log line\n" + buf.getvalue() + "Translating 80 lines\n"
    rets = _feed_lines(parser, text)
    assert rets[0] is None
    assert rets[1]["type"] == "phase_started"
    assert rets[2] is None
    snap = parser.snapshot()
    assert snap["stage"] == "阶段A 净语+翻译"
    assert snap["total"] == 80
    assert snap["ndjson_mode"] is True


def test_empty_line_returns_none():
    assert EventStreamParser().feed("") is None
    assert EventStreamParser().feed("   \n") is None


# ---------------------------------------------------------------------------
# api 侧纯函数帮助：format_event_line
# ---------------------------------------------------------------------------

def test_format_event_line_styles():
    assert format_event_line({"type": "task_started", "payload": {}}) == "[事件] 任务开始"
    assert format_event_line({"type": "task_finished", "payload": {}}) == "[事件] 任务结束"
    assert format_event_line(
        {"type": "phase_started", "phase": "A", "payload": {}}) == \
        "[事件] 阶段A 净语+翻译 开始"
    assert format_event_line(
        {"type": "phase_progress", "phase": "B",
         "payload": {"batch": 3, "batch_total": 10}}) == \
        "[事件] 阶段B 审校+抛光 批次 3/10 进行中"
    assert format_event_line(
        {"type": "phase_progress", "phase": "A",
         "payload": {"done": 30, "total": 100}}) == \
        "[事件] 阶段A 净语+翻译 30/100 行 进行中"
    assert format_event_line(
        {"type": "phase_finished", "phase": "A", "payload": {}}) == \
        "[事件] 阶段A 净语+翻译 完成"
    assert format_event_line(
        {"type": "warning", "phase": "A", "payload": {"message": "限流"}}) == \
        "[事件] ⚠ 警告：限流"
    assert format_event_line(
        {"type": "degraded", "phase": "A", "payload": {"message": "切换本地"}}) == \
        "[事件] ⚠ 降级：切换本地"
    assert format_event_line(
        {"type": "error", "phase": "A", "payload": {"message": "宕机"}}) == \
        "[事件] ✗ 错误：宕机"


def test_format_event_line_heartbeat_and_garbage_return_none():
    assert format_event_line({"type": "heartbeat", "payload": {}}) is None
    assert format_event_line(None) is None
    assert format_event_line("not a dict") is None


# ---------------------------------------------------------------------------
# api 侧纯函数帮助：resume_state_for_path
# ---------------------------------------------------------------------------

def test_resume_state_none_when_no_artifacts(tmp_path: Path):
    srt = tmp_path / "movie.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nあ\n", encoding="utf-8")
    result = resume_state_for_path(str(srt))
    assert result == {"path": str(srt), "stem": "movie", "state": "none"}


def test_resume_state_resumable_with_manifest(tmp_path: Path):
    srt = tmp_path / "movie.srt"
    srt.write_text("x", encoding="utf-8")
    (tmp_path / "movie_manifest.json").write_text("{}", encoding="utf-8")
    result = resume_state_for_path(str(srt))
    assert result["state"] == "resumable"
    assert result["stem"] == "movie"


def test_resume_state_completed_with_final(tmp_path: Path):
    srt = tmp_path / "movie.srt"
    srt.write_text("x", encoding="utf-8")
    (tmp_path / "movie_manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "movie_final_cn.srt").write_text("x", encoding="utf-8")
    assert resume_state_for_path(str(srt))["state"] == "completed"


def test_resume_state_strips_lang_suffix(tmp_path: Path):
    """stem 归一与 refine.pipeline_support.strip_lang_suffix 一致（.japanese 后缀）。"""
    srt = tmp_path / "movie.japanese.srt"
    srt.write_text("x", encoding="utf-8")
    (tmp_path / "movie_manifest.json").write_text("{}", encoding="utf-8")
    result = resume_state_for_path(str(srt))
    assert result["stem"] == "movie"
    assert result["state"] == "resumable"


def test_resume_state_injected_helpers(tmp_path: Path):
    """exists / strip_stem 注入点可用（不触碰文件系统也能测）。"""
    result = resume_state_for_path(
        "/whatever/ep01.chinese.srt",
        exists=lambda p: p.endswith("_final_cn.srt"),
        strip_stem=lambda s: s.replace(".chinese", ""),
    )
    assert result["stem"] == "ep01"
    assert result["state"] == "completed"


def test_heartbeat_age_uses_wall_clock():
    """heartbeat_age 基于真实时间推进（喂入后短暂等待单调不减）。"""
    parser = EventStreamParser()
    buf = io.StringIO()
    EventEmitter(stream=buf, task_id="t").emit("heartbeat")
    _feed_lines(parser, buf.getvalue())
    age1 = parser.snapshot()["heartbeat_age"]
    time.sleep(0.05)
    age2 = parser.snapshot()["heartbeat_age"]
    assert age2 >= age1
