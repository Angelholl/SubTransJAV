"""
GUI 侧 NDJSON 事件流解析（webview-free 可测模块，仿 security.py 模式）。

消费 ``subtransjav.refine.cli`` 子进程 stdout 的逐行输出：
  - 结构化事件行（NDJSON）交由 ``refine.events.parse_event_line`` 解析并
    折算成进度/阶段/风险等聚合状态；
  - 非事件行进入"遗留兼容层"：内置原 GUI 的 5 条正则与错误字面量检测
    （过渡期双轨，旧版 CLI 无 --event-format 时仍可显示进度）。

仅依赖标准库 + 可 import ``refine.events``；断点恢复的 stem 归一化通过
延迟 import ``refine.pipeline_support.strip_lang_suffix`` 复用单一实现。
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from subtransjav.refine.events import parse_event_line

from .strings import msg

# 风险清单去重上限（超出后新的唯一风险仍计数，但明细列表只保留最近 100 条）
RISKS_CAP = 100
# snapshot 返回的风险明细条数（GUI 轮询展示用）
RISKS_SNAPSHOT = 50
# 心跳超时默认阈值（秒；≈2.25×心跳间隔 20s，可由 RefineConfig.heartbeat_stale_s 覆盖后构造传入）
HEARTBEAT_STALE_S_DEFAULT = 45.0

# 阶段标签归一：事件 phase 短标签 → strings.MSG 键（未知值原样透传）
_STAGE_LABEL_KEYS = {
    "a": "stage_a",
    "1": "stage_a",
    "phase_a": "stage_a",
    "s1": "stage_a",
    "stage_a": "stage_a",
    "b": "stage_b",
    "2": "stage_b",
    "3": "stage_b",
    "phase_b": "stage_b",
    "s3": "stage_b",
    "stage_b": "stage_b",
}


def stage_label(phase) -> str:
    """把事件 phase 归一为可展示的阶段名；空值返回空串。"""
    if phase is None:
        return ""
    key = str(phase).strip().lower()
    if not key:
        return ""
    label_key = _STAGE_LABEL_KEYS.get(key)
    return msg(label_key) if label_key else str(phase).strip()


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _extract_message(payload: dict[str, Any]) -> str:
    """从事件 payload 中提取人类可读消息。"""
    for key in ("message", "error", "reason", "detail", "text"):
        v = payload.get(key)
        if v:
            return str(v)
    try:
        return json.dumps(payload, ensure_ascii=False)[:200]
    except (TypeError, ValueError):
        return str(payload)[:200]


def format_event_line(event: dict[str, Any]) -> str | None:
    """把结构化事件格式化成 "[事件] ..." 风格的人类可读日志行。

    返回 None 表示该事件不进入日志队列（如心跳，避免刷屏）。
    """
    if not isinstance(event, dict):
        return None
    etype = event.get("type") or ""
    phase = stage_label(event.get("phase"))
    payload = event.get("payload") or {}

    if etype == "task_started":
        return _join(msg("ev_tag"), msg("ev_task_started"))
    if etype == "phase_started":
        return (_join(msg("ev_tag"), phase, msg("ev_phase_started"))
                if phase else _join(msg("ev_tag"), msg("ev_phase_started_generic")))
    if etype == "phase_progress":
        done = payload.get("done", payload.get("lines_done"))
        total = payload.get("total", payload.get("lines_total"))
        batch = payload.get("batch", payload.get("batch_no"))
        batch_total = payload.get("batch_total")
        if batch is not None and batch_total:
            core = msg("ev_batch", done=batch, total=batch_total)
        elif done is not None and total:
            core = msg("ev_lines", done=done, total=total)
        else:
            core = ""
        if core:
            return _join(msg("ev_tag"), phase, core, msg("ev_in_progress"))
        return _join(msg("ev_tag"), phase, msg("ev_in_progress"))
    if etype == "phase_finished":
        return (_join(msg("ev_tag"), phase, msg("ev_phase_finished"))
                if phase else _join(msg("ev_tag"), msg("ev_phase_finished_generic")))
    if etype == "warning":
        return f"{msg('ev_tag')} {msg('ev_warning', e=_extract_message(payload))}"
    if etype == "degraded":
        return f"{msg('ev_tag')} {msg('ev_degraded', e=_extract_message(payload))}"
    if etype == "error":
        return f"{msg('ev_tag')} {msg('ev_error', e=_extract_message(payload))}"
    if etype == "task_finished":
        return _join(msg("ev_tag"), msg("ev_task_finished"))
    return None    # heartbeat 及未知类型不输出


# ---------------------------------------------------------------------------
# 遗留兼容层：原 api._stream_translation_output 的正则与错误字面量
# ---------------------------------------------------------------------------

_RE_TRANSLATING_LINES = re.compile(r"Translating (\d+) lines")
_RE_V2_TOTAL = re.compile(r"共 (\d+) 条，分 (\d+) 批")
_RE_V2_BATCH = re.compile(r"批次 (\d+)/(\d+)")
_RE_SCENE_BATCH = re.compile(
    r"Scene (\d+) batch (\d+): (\d+) lines and (\d+) untranslated")
_RE_STAGE = re.compile(r"\[STAGE\]\s*(.+)")


def _legacy_error_of(line: str) -> str | None:
    """旧版错误字面量检测（与原 api 逻辑一致，含优先级顺序）。"""
    if "TRANSLATION FAILED" in line:
        return "Translation failed — no subtitles were translated"
    if line.startswith("Failed:"):
        return line.strip()
    if "Batch processing finished with" in line and "error" in line:
        return line.strip()
    if "[refine] 执行失败" in line:
        return line.strip()
    return None


# ---------------------------------------------------------------------------
# 断点恢复状态判定（纯函数，exists/strip_stem 可注入便于测试）
# ---------------------------------------------------------------------------

def _default_strip_stem(stem: str) -> str:
    """延迟 import 单一实现，避免本模块 import 期拉起 pipeline_support 依赖链。"""
    try:
        from subtransjav.refine.pipeline_support import strip_lang_suffix
        return strip_lang_suffix(stem)
    except Exception:
        return stem


def resume_state_for_path(path: str, exists=os.path.exists,
                          strip_stem=None) -> dict[str, Any]:
    """计算单个输入 srt 的断点恢复状态。

    - 有 ``{stem}_final_cn.srt``        → completed（整文件已完成）
    - 有 ``{stem}_manifest.json`` 无终稿 → resumable（可复用已完成阶段）
    - 两者皆无                           → none
    """
    strip = strip_stem or _default_strip_stem
    p = Path(path)
    stem = strip(p.stem)
    parent = p.parent
    has_final = bool(exists(str(parent / f"{stem}_final_cn.srt")))
    has_manifest = bool(exists(str(parent / f"{stem}_manifest.json")))
    if has_final:
        state = "completed"
    elif has_manifest:
        state = "resumable"
    else:
        state = "none"
    return {"path": str(path), "stem": stem, "state": state}


# ---------------------------------------------------------------------------
# 事件流解析器
# ---------------------------------------------------------------------------

class EventStreamParser:
    """逐行喂入子进程输出，聚合出 GUI 轮询所需的状态快照。

    线程安全：feed 由 reader 线程调用，snapshot 由 GUI 线程调用。
    """

    def __init__(self, heartbeat_stale_s: float = HEARTBEAT_STALE_S_DEFAULT):
        self.heartbeat_stale_s = float(heartbeat_stale_s)
        self.current_stage = ""
        self.current_file: str | None = None
        self.lines_total = 0
        self.lines_done = 0
        self.batch_done = 0
        self.batch_total = 0
        self.risks: list[dict[str, Any]] = []
        self.risk_count = 0
        self.error: str | None = None
        self.task_summary: dict[str, Any] | None = None
        self.last_event_ts: float | None = None
        self.heartbeat_last_ts: float | None = None
        self.ndjson_mode = False
        self.untranslated_majority = False
        self._risk_keys: set[str] = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def feed(self, line: str) -> dict[str, Any] | None:
        """喂入一行输出。返回本次解析出的事件；非事件行返回 None。"""
        event = parse_event_line(line)
        if event is None:
            with self._lock:
                self._feed_legacy(line)
            return None
        with self._lock:
            self._feed_event(event)
        return event

    # ------------------------------------------------------------------
    def _feed_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type") or ""
        phase = stage_label(event.get("phase"))
        payload = event.get("payload") or {}

        self.ndjson_mode = True
        now = time.time()
        self.last_event_ts = now
        if etype == "heartbeat":
            self.heartbeat_last_ts = now
            return

        if etype == "phase_started":
            self.current_stage = phase or self.current_stage
            self.lines_done = 0
            self.lines_total = 0
        elif etype == "phase_progress":
            if phase:
                self.current_stage = phase
            done = payload.get("done", payload.get("lines_done"))
            total = payload.get("total", payload.get("lines_total"))
            batch = payload.get("batch", payload.get("batch_no"))
            batch_total = payload.get("batch_total")
            try:
                if total is not None:
                    self.lines_total = int(total)
                if done is not None:
                    self.lines_done = int(done)
            except (TypeError, ValueError):
                pass
            try:
                if batch is not None:
                    self.batch_done = int(batch)
                if batch_total is not None:
                    self.batch_total = int(batch_total)
            except (TypeError, ValueError):
                pass
            self._refresh_progress_text()
        elif etype in ("warning", "degraded"):
            self._add_risk(event)
        elif etype == "error":
            msg = _extract_message(payload)
            self.error = msg
            self._add_risk(event)
        elif etype == "task_finished":
            self.task_summary = dict(payload)
            if payload.get("untranslated_majority"):
                self.untranslated_majority = True

    def _add_risk(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") or {}
        entry = {
            "type": event.get("type"),
            "phase": stage_label(event.get("phase")),
            "file": event.get("file"),
            "message": _extract_message(payload),
            "payload": payload,
        }
        key = json.dumps(
            {k: entry[k] for k in ("type", "phase", "file", "message")},
            ensure_ascii=True, sort_keys=True)
        if key in self._risk_keys:
            return
        self._risk_keys.add(key)
        self.risk_count += 1
        self.risks.append(entry)
        if len(self.risks) > RISKS_CAP:
            self.risks = self.risks[-RISKS_CAP:]

    def _refresh_progress_text(self) -> None:
        """与遗留层一致的 current_file 文案。"""
        if self.lines_total:
            label = self.current_stage or msg("processing")
            if self.batch_total:
                label = _join(label, msg("ev_batch",
                                         done=self.batch_done,
                                         total=self.batch_total))
            self.current_file = msg("progress_text",
                                    done=self.lines_done,
                                    total=self.lines_total,
                                    label=label)

    # ------------------------------------------------------------------
    def _feed_legacy(self, line: str) -> None:
        """遗留兼容层：原 5 条正则 + 错误字面量（过渡期双轨）。"""
        mt = _RE_TRANSLATING_LINES.search(line)
        if mt:
            self.lines_total = int(mt.group(1))
            self.lines_done = 0
        mv = _RE_V2_TOTAL.search(line)
        if mv:
            self.lines_total = int(mv.group(1))
            self.lines_done = 0
            self.batch_total = int(mv.group(2))
            self.batch_done = 0
        mb2 = _RE_V2_BATCH.search(line)
        if mb2 and self.lines_total:
            batch_no, batch_total = int(mb2.group(1)), int(mb2.group(2))
            self.batch_done, self.batch_total = batch_no, batch_total
            per_batch = self.lines_total / max(1, batch_total)
            self.lines_done = min(int(per_batch * batch_no), self.lines_total)
            self._refresh_progress_text()
        mb = _RE_SCENE_BATCH.search(line)
        if mb:
            done = int(mb.group(3))
            if int(mb.group(4)) == 0:
                total = self.lines_total
                self.lines_done = min(self.lines_done + done, total)
                self._refresh_progress_text()
        ms = _RE_STAGE.search(line)
        if ms:
            self.current_stage = ms.group(1).strip()
            self.lines_done = 0
        err = _legacy_error_of(line)
        if err and not self.error:
            self.error = err

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """GUI 轮询用聚合快照。"""
        with self._lock:
            progress = 0
            if self.lines_total > 0:
                progress = int(100 * self.lines_done / self.lines_total)
                progress = max(0, min(100, progress))
            heartbeat_age = (
                round(time.time() - self.last_event_ts, 1)
                if self.last_event_ts is not None else None)
            heartbeat_stale = (
                heartbeat_age is not None
                and heartbeat_age > self.heartbeat_stale_s)
            return {
                "stage": self.current_stage or None,
                "current_file": self.current_file,
                "progress": progress,
                "done": self.lines_done,
                "total": self.lines_total,
                "risks": list(self.risks[-RISKS_SNAPSHOT:]),
                "risk_count": self.risk_count,
                "error": self.error,
                "task_summary": self.task_summary,
                "heartbeat_age": heartbeat_age,
                "heartbeat_stale_s": self.heartbeat_stale_s,
                "heartbeat_stale": heartbeat_stale,
                "ndjson_mode": self.ndjson_mode,
                "untranslated_majority": self.untranslated_majority,
            }
