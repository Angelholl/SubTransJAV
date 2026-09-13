"""
NDJSON 结构化事件协议（v1.1 P0 前置共用模块）。

- 管线以"每行一个 JSON 对象"的方式向 stdout（或任意流）输出结构化事件，
  GUI 兼容层逐行读取并解析；解析失败的行按普通日志处理。
- 仅依赖标准库，保证 GUI 侧可轻量导入。
- ensure_ascii=True 序列化：防止 Windows GBK 控制台把非 ASCII 字符替换成
  '?' 后破坏 JSON 行的结构。
"""

import json
import sys
import threading
import time
import uuid
from datetime import datetime

# 协议版本：字段布局或语义发生不兼容变更时递增
PROTOCOL_VERSION = 1

# 合法事件类型（emit 传入其他值直接 raise ValueError，属编程错误）
EVENT_TYPES = (
    "task_started",
    "phase_started",
    "phase_progress",
    "phase_finished",
    "warning",
    "degraded",
    "error",
    "heartbeat",
    "task_finished",
)

# 事件行必备关键字段（parse_event_line 据此判断是否为协议行）
_REQUIRED_KEYS = ("protocol_version", "task_id", "seq", "timestamp", "type")


def _iso_now() -> str:
    """秒级 ISO 时间戳（与 manifest/risk 模块保持同款格式）。"""
    return datetime.now().isoformat(timespec="seconds")


class EventEmitter:
    """向单一流输出 NDJSON 事件行；线程安全（心跳线程与主线程并发 emit）。

    - stream 为 None 时在构造瞬间捕获 sys.stdout（而非 emit 时再取，
      便于管线提前重定向后仍保持一致）。
    - enabled=False 为纯文本模式：emit 是无操作，不产出任何输出。
    - 写失败（如 stdout 已被关闭）静默吞掉，但 seq 仍递增，保证序号连续。
    """

    def __init__(self, stream=None, task_id=None, enabled=True,
                 heartbeat_interval=20.0, heartbeat_payload_fn=None):
        self._stream = stream if stream is not None else sys.stdout
        self._task_id = task_id if task_id else uuid.uuid4().hex[:8]
        self._enabled = bool(enabled)
        self._heartbeat_interval = float(heartbeat_interval)
        self._heartbeat_payload_fn = heartbeat_payload_fn
        self._seq = 0
        self._lock = threading.Lock()
        self._last_phase = None                      # 最近一次 emit 的 phase（供心跳上报）
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._heartbeat_start = None                 # time.monotonic() 起点

    # ------------------------------------------------------------------
    def emit(self, event_type, phase=None, file=None, payload=None) -> None:
        """输出一条事件；event_type 非法时 raise ValueError（与 enabled 无关）。"""
        if event_type not in EVENT_TYPES:
            raise ValueError(
                f"未知事件类型: {event_type!r}，合法值: {', '.join(EVENT_TYPES)}")
        if not self._enabled:
            return
        with self._lock:
            self._seq += 1
            if payload is None:
                payload_dict = {}
            elif isinstance(payload, dict):
                payload_dict = dict(payload)
            else:
                payload_dict = {"value": payload}
            if phase is not None:
                self._last_phase = phase
            event = {
                "protocol_version": PROTOCOL_VERSION,
                "task_id": self._task_id,
                "seq": self._seq,
                "timestamp": _iso_now(),
                "type": event_type,
                "phase": phase,
                "file": file,
                "payload": payload_dict,
            }
            try:
                self._stream.write(json.dumps(event, ensure_ascii=True) + "\n")
                self._stream.flush()
            except Exception:
                # stdout 可能已被 GUI/管道关闭；事件丢失可接受，序号必须继续
                pass

    # ------------------------------------------------------------------
    def start_heartbeat(self) -> None:
        """启动守护心跳线程：每 heartbeat_interval 秒发一次 heartbeat 事件。"""
        if not self._enabled or self._heartbeat_thread is not None:
            return
        self._heartbeat_stop.clear()
        self._heartbeat_start = time.monotonic()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="subtransjav-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        interval = max(0.05, self._heartbeat_interval)
        while not self._heartbeat_stop.wait(interval):
            payload = {
                "last_phase": self._last_phase,
                "elapsed_s": round(time.monotonic() - (self._heartbeat_start or 0.0), 3),
                "interval_s": self._heartbeat_interval,
            }
            if self._heartbeat_payload_fn is not None:
                try:
                    extra = self._heartbeat_payload_fn()
                    if isinstance(extra, dict):
                        payload.update(extra)   # 外部 payload_fn 的字段可覆盖默认值
                except Exception:
                    pass                        # payload_fn 异常不应杀死心跳线程
            self.emit("heartbeat", payload=payload)

    def stop_heartbeat(self) -> None:
        """停止心跳线程并等待退出（线程内调用自身时跳过 join 防死锁）。"""
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._heartbeat_interval))
        self._heartbeat_thread = None

    def close(self) -> None:
        """任务收尾：停心跳（不关闭底层流，stdout 归属调用方）。"""
        self.stop_heartbeat()


def parse_event_line(line):
    """解析单行 NDJSON 事件；非 JSON / 缺关键字段 / type 非法 -> None。

    GUI 兼容层据此决定把一行当作结构化事件还是普通日志文本。
    """
    text = (line or "").strip()
    if not text:
        return None
    try:
        event = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    for key in _REQUIRED_KEYS:
        if key not in event:
            return None
    if event.get("type") not in EVENT_TYPES:
        return None
    if not isinstance(event.get("payload", {}), dict):
        return None
    return event
