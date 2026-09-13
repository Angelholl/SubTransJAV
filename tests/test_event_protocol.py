"""NDJSON 结构化事件协议契约测试：行格式 / seq / task_id / 心跳 / 解析容错。"""

import io
import re
import types

import pytest

from subtransjav.refine.events import EVENT_TYPES, PROTOCOL_VERSION, EventEmitter, parse_event_line

# 协议行完整键集合（冻结契约，缺一不可、不可增减）
EXPECTED_KEYS = {"protocol_version", "task_id", "seq", "timestamp", "type", "phase", "file", "payload"}
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _lines(buf: io.StringIO) -> list:
    return [ln for ln in buf.getvalue().splitlines() if ln.strip()]


def _events(buf: io.StringIO) -> list:
    return [parse_event_line(ln) for ln in _lines(buf)]


# ---------------------------------------------------------------------------
# 行格式与键完整性
# ---------------------------------------------------------------------------

def test_line_format_keys_complete():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="abcd1234")
    em.emit("phase_started", phase="A", file="x.srt", payload={"batch": 1})
    lines = _lines(buf)
    assert len(lines) == 1
    ev = parse_event_line(lines[0])
    assert set(ev.keys()) == EXPECTED_KEYS
    assert ev["protocol_version"] == PROTOCOL_VERSION == 1
    assert ev["task_id"] == "abcd1234"
    assert ev["seq"] == 1
    assert ISO_TS_RE.match(ev["timestamp"])
    assert ev["type"] == "phase_started"
    assert ev["phase"] == "A"
    assert ev["file"] == "x.srt"
    assert ev["payload"] == {"batch": 1}


def test_seq_monotonic_from_1():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="t")
    for i in range(5):
        em.emit("phase_progress", phase="A", payload={"n": i})
    assert [e["seq"] for e in _events(buf)] == [1, 2, 3, 4, 5]


def test_task_id_threaded_through_all_events():
    buf = io.StringIO()
    em = EventEmitter(stream=buf)          # 不指定 task_id -> 默认 uuid4().hex[:8]
    em.emit("task_started")
    em.emit("phase_started", phase="A")
    em.emit("task_finished")
    events = _events(buf)
    assert len({e["task_id"] for e in events}) == 1
    assert re.match(r"^[0-9a-f]{8}$", events[0]["task_id"])


def test_custom_task_id_respected():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="deadbeef")
    em.emit("task_started")
    assert _events(buf)[0]["task_id"] == "deadbeef"


# ---------------------------------------------------------------------------
# ensure_ascii 与编码安全
# ---------------------------------------------------------------------------

def test_ensure_ascii_escapes_non_ascii():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="t")
    em.emit("warning", phase="A", file="中文名.srt", payload={"样本": "中文文本"})
    line = _lines(buf)[0]
    # 原始行内不允许出现非 ASCII 字符（防 GBK 控制台替换破坏 JSON）
    assert line.isascii()
    assert "\\u4e2d" in line                      # "中" 的转义形式
    ev = parse_event_line(line)
    assert ev["payload"]["样本"] == "中文文本"      # 解析后还原
    assert ev["file"] == "中文名.srt"


# ---------------------------------------------------------------------------
# parse_event_line 往返与容错
# ---------------------------------------------------------------------------

def test_parse_roundtrip_all_event_types():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="t")
    for t in EVENT_TYPES:
        em.emit(t, phase="A", payload={"k": "v"})
    lines = _lines(buf)
    assert len(lines) == len(EVENT_TYPES)
    for line, expected_type in zip(lines, EVENT_TYPES, strict=False):
        ev = parse_event_line(line)
        assert ev is not None
        assert ev["type"] == expected_type


def test_parse_invalid_lines_return_none():
    assert parse_event_line("") is None
    assert parse_event_line("   \n") is None
    assert parse_event_line("这是普通日志文本") is None
    assert parse_event_line("[WARN] 2026-09-12 something happened") is None
    assert parse_event_line('{"broken": json') is None            # 非 JSON
    assert parse_event_line('[1, 2, 3]') is None                  # 非 dict
    assert parse_event_line('{"foo": 1}') is None                 # 缺关键字段
    assert parse_event_line(                                      # 缺 seq
        '{"protocol_version":1,"task_id":"t","timestamp":"2026-01-01T00:00:00","type":"error"}') is None
    assert parse_event_line(                                      # type 非法
        '{"protocol_version":1,"task_id":"t","seq":1,"timestamp":"2026-01-01T00:00:00","type":"nope"}') is None


# ---------------------------------------------------------------------------
# enabled=False（text 模式）
# ---------------------------------------------------------------------------

def test_disabled_emitter_is_noop():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="t", enabled=False)
    em.emit("task_started")
    em.emit("error", phase="A", payload={"x": 1})
    em.start_heartbeat()                      # 不应启动线程、不应有输出
    em.stop_heartbeat()
    em.close()
    assert buf.getvalue() == ""


# ---------------------------------------------------------------------------
# 非法事件类型
# ---------------------------------------------------------------------------

def test_invalid_event_type_raises_value_error():
    em = EventEmitter(stream=io.StringIO(), task_id="t")
    with pytest.raises(ValueError):
        em.emit("bogus_type")
    with pytest.raises(ValueError):
        em.emit("")
    # 非法类型不应产生任何输出
    assert em._seq == 0


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------

def test_heartbeat_emits_and_stops():
    buf = io.StringIO()
    em = EventEmitter(stream=buf, task_id="t", heartbeat_interval=0.05)
    em.emit("phase_started", phase="A")       # 心跳应上报 last_phase="A"
    em.start_heartbeat()
    import time
    time.sleep(0.30)
    em.stop_heartbeat()
    beats = [e for e in _events(buf) if e["type"] == "heartbeat"]
    assert len(beats) >= 2
    for b in beats:
        assert set(b["payload"].keys()) >= {"last_phase", "elapsed_s", "interval_s"}
        assert b["payload"]["last_phase"] == "A"
        assert b["payload"]["elapsed_s"] >= 0
        assert b["payload"]["interval_s"] == 0.05
    # seq 与主事件共享同一条单调序列
    seqs = [e["seq"] for e in _events(buf)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # 停止后不再发心跳
    count_after_stop = len(_lines(buf))
    time.sleep(0.15)
    assert len(_lines(buf)) == count_after_stop


def test_heartbeat_payload_fn_merged():
    buf = io.StringIO()

    def _fn():
        return {"progress_hint": 42, "interval_s": 999}   # 自定义字段可覆盖默认值

    em = EventEmitter(stream=buf, task_id="t", heartbeat_interval=0.05,
                      heartbeat_payload_fn=_fn)
    em.start_heartbeat()
    import time
    time.sleep(0.18)
    em.stop_heartbeat()
    beats = [e for e in _events(buf) if e["type"] == "heartbeat"]
    assert beats, "心跳线程未产生事件"
    assert all(b["payload"]["progress_hint"] == 42 for b in beats)
    assert all(b["payload"]["interval_s"] == 999 for b in beats)   # fn 覆盖默认值
    # payload_fn 抛异常不应杀死心跳线程
    buf2 = io.StringIO()
    em2 = EventEmitter(stream=buf2, task_id="t", heartbeat_interval=0.05,
                       heartbeat_payload_fn=lambda: 1 / 0)
    em2.start_heartbeat()
    time.sleep(0.18)
    em2.stop_heartbeat()
    assert any(e["type"] == "heartbeat" for e in _events(buf2))


def test_stream_captured_at_construction_time():
    """stream 缺省时在构造瞬间捕获 sys.stdout，而非 emit 时再取。"""
    early = io.StringIO()
    late = io.StringIO()
    import sys
    real_stdout = sys.stdout
    try:
        sys.stdout = early
        em = EventEmitter(task_id="t")
        sys.stdout = late
        em.emit("task_started")
    finally:
        sys.stdout = real_stdout
    assert "task_started" in early.getvalue()
    assert late.getvalue() == ""


def test_write_failure_swallows_but_seq_increments():
    class BrokenStream(types.SimpleNamespace):
        def write(self, text):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    em = EventEmitter(stream=BrokenStream(), task_id="t")
    em.emit("task_started")                   # 不应抛异常
    em.emit("error", phase="A")
    assert em._seq == 2                       # 写失败但 seq 仍递增
