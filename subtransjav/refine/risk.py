"""
风险事件模型（v1.1 P0 前置共用模块）。

- 收集管线各阶段的降级/风险事件，供任务结束时的汇总输出与风险清单报告使用。
- 可选挂载 EventEmitter：add() 时同步向事件流发 warning/degraded 事件。
- 仅依赖标准库；通过 duck typing 使用 emitter（不 import events 模块）。
"""

import contextlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# 严重度档位
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

# 原文样本上限（防止风险清单被长文本撑爆）
_MAX_SAMPLES = 3


def _iso_now() -> str:
    """秒级 ISO 时间戳（与 events/manifest 模块保持同款格式）。"""
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RiskEvent:
    """单条风险记录。"""

    stage: str
    file: str
    entry_range: str = ""          # 受影响的条目范围，如 "12-15"
    reason: str = ""               # 风险原因（人类可读）
    action: str = ""               # 已执行的降级动作（空 = 未影响正文内容）
    affected_count: int = 0        # 受影响条数
    samples: list = field(default_factory=list)   # ≤3 条原文样本
    suggestion: str = ""           # 处理建议
    severity: str = SEVERITY_WARNING
    timestamp: str = field(default_factory=_iso_now)
    # additive 诊断字段（D11）：只进 asdict 路径（风险清单.json 与事件
    # payload）；md 表格（_render_markdown）/ summary_lines /
    # content_degraded 均不感知，旧调用点零影响。
    timing_range: str = ""         # 首条→末条 timing，如 "t0→t1"（单条时两段相同）
    entry_timings: list = field(default_factory=list)  # 受影响条目的 timing 全列表


class RiskCollector:
    """任务级风险收集器：聚合 RiskEvent，并可同步镜像到事件流。"""

    def __init__(self):
        self.events = []                 # list[RiskEvent]
        self._emitter = None             # EventEmitter（duck typing）
        self._untranslated_majority = False
        self._info_lines = []            # 非风险信息行（闸门0 计数等），随 summary_lines 输出

    # ------------------------------------------------------------------
    def attach_emitter(self, emitter) -> None:
        """挂载事件发射器；此后每次 add() 同步发一条 warning/degraded 事件。"""
        self._emitter = emitter

    def add_summary_line(self, line) -> None:
        """追加一条非风险信息行（如 R8 的闸门0 计数行），随 summary_lines 聚合输出。

        信息行不计入风险事件：不影响 risk_count / content_degraded /
        风险清单落盘（write_reports 仍以 has_risks 为准）。
        """
        if line:
            self._info_lines.append(str(line))

    def add(self, stage=None, file=None, entry_range=None, reason=None, action=None,
            affected_count=0, samples=None, suggestion=None, severity="warning",
            timing_range="", entry_timings=None) -> RiskEvent:
        """记录一条风险；samples 自动截断到 3 条，None 入参归一为空串/0。

        timing_range/entry_timings 为 additive 诊断字段（仅进 asdict
        路径），缺省归一为空串/空列表——既有调用点零破坏。
        """
        event = RiskEvent(
            stage=stage or "",
            file=file or "",
            entry_range=entry_range or "",
            reason=reason or "",
            action=action or "",
            affected_count=int(affected_count or 0),
            samples=list(samples or [])[:_MAX_SAMPLES],
            suggestion=suggestion or "",
            severity=severity or SEVERITY_WARNING,
            timing_range=timing_range or "",
            entry_timings=list(entry_timings or []),
        )
        self.events.append(event)
        if self._emitter is not None:
            # info 级仅提示（warning 事件）；warning/critical 属真实降级（degraded 事件）
            event_type = "warning" if event.severity == SEVERITY_INFO else "degraded"
            # 事件流故障不应阻断翻译主流程
            with contextlib.suppress(Exception):
                self._emitter.emit(event_type, phase=event.stage, file=event.file,
                                   payload=asdict(event))
        return event

    def add_entry_ranged(self, *, stage, file, entries, reason,
                         action="", severity=SEVERITY_WARNING,
                         suggestion="") -> RiskEvent:
        """按条目集合记录一条风险：范围/时间轴字段由本 helper 统一推导
        （调用点禁止手拼 "12-15" 范围串）。

        - entries：条目 dict 列表（含 index/timing）；空列表安全（各范围
          字段记空串、affected_count=0，不抛异常）；
        - entry_range 按 "12-15" 惯例取 index min-max（单条/同值记 "N"，
          index 缺失的条目不参与）；
        - timing_range = 首条→末条 timing；entry_timings = 全部 timing
          列表（additive 字段，只进 asdict 路径）；
        - affected_count = len(entries)。
        闸门0 不用本 helper、不建风险事件：闸门0 计数信息走
        add_summary_line（信息行不进风险清单、不影响 content_degraded），
        防止同一批删除在风险事件与信息行双计（D11 ⑥）。
        """
        entries = list(entries or [])
        timings = [(e.get("timing") or "") for e in entries]
        indexes = [e.get("index") for e in entries
                   if e.get("index") is not None]
        if not indexes:
            entry_range = ""
        elif min(indexes) == max(indexes):
            entry_range = str(min(indexes))
        else:
            entry_range = f"{min(indexes)}-{max(indexes)}"
        timing_range = f"{timings[0]}→{timings[-1]}" if timings else ""
        return self.add(stage=stage, file=file, entry_range=entry_range,
                        reason=reason, action=action,
                        affected_count=len(entries), severity=severity,
                        suggestion=suggestion, timing_range=timing_range,
                        entry_timings=timings)

    def mark_untranslated_majority(self, file=None, total=0, kept=0) -> None:
        """记录"整段未翻译"标志（保留日文原文占比过高/全部未译）。

        同时落一条 critical 事件，保证 summary 与 payload 都能体现。
        """
        self._untranslated_majority = True
        total_n = int(total or 0)
        kept_n = int(kept or 0)
        self.add(
            stage="final",
            file=file or "",
            reason=f"整段未翻译：保留日文原文 {kept_n}/{total_n} 条（占比过高或全部未译）",
            action="保留日文原文",
            affected_count=max(0, total_n - kept_n),
            suggestion="检查本地模型是否在线/模型质量，或调小批次后重跑",
            severity=SEVERITY_CRITICAL,
        )

    # ------------------------------------------------------------------
    @property
    def content_degraded(self) -> bool:
        """是否存在影响正文内容的降级：action 非空且 affected_count>0，或整段未翻译。

        info 级仅为提示信息，不计入内容降级。
        """
        if self._untranslated_majority:
            return True
        return any(e.severity != SEVERITY_INFO and e.action and e.affected_count > 0
                   for e in self.events)

    @property
    def untranslated_majority(self) -> bool:
        return self._untranslated_majority

    @property
    def has_risks(self) -> bool:
        return bool(self.events) or self._untranslated_majority

    # ------------------------------------------------------------------
    def summary_lines(self) -> list:
        """人类可读的摘要行：风险行（每行 "⚠️ ..." 风格）+ 末行计数汇总，
        再拼接 add_summary_line 追加的非风险信息行（闸门0 计数等）；
        无风险且无信息行返回 []。"""
        lines = []
        for e in self.events:
            seg = f"⚠️ [{e.severity}] {e.stage or '-'}"
            if e.file:
                seg += f" | 文件: {e.file}"
            if e.entry_range:
                seg += f" | 条目: {e.entry_range}"
            if e.reason:
                seg += f" | {e.reason}"
            if e.action:
                seg += f" | 降级动作: {e.action}"
            if e.affected_count:
                seg += f" | 影响 {e.affected_count} 条"
            if e.suggestion:
                seg += f" | 建议: {e.suggestion}"
            lines.append(seg)
        if self.has_risks:
            n_info = sum(1 for e in self.events if e.severity == SEVERITY_INFO)
            n_warn = sum(1 for e in self.events if e.severity == SEVERITY_WARNING)
            n_crit = sum(1 for e in self.events if e.severity == SEVERITY_CRITICAL)
            lines.append(f"⚠️ 风险汇总：共 {len(self.events)} 项"
                         f"（info {n_info} / warning {n_warn} / critical {n_crit}）")
        lines.extend(self._info_lines)
        return lines

    def to_payload(self) -> dict:
        """序列化为可嵌入事件 payload / JSON 报告的 dict。"""
        return {
            "risk_count": len(self.events),
            "untranslated_majority": self._untranslated_majority,
            "events": [asdict(e) for e in self.events],
        }

    # ------------------------------------------------------------------
    def write_reports(self, out_dir, stem):
        """有风险时写 {stem}_风险清单.md + .json，返回 {"md":路径,"json":路径}；无风险不写文件返回 None。"""
        if not self.has_risks:
            return None
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        md_path = out / f"{stem}_风险清单.md"
        json_path = out / f"{stem}_风险清单.json"
        md_path.write_text(self._render_markdown(), encoding="utf-8")
        json_path.write_text(
            json.dumps(self.to_payload(), ensure_ascii=True, indent=2), encoding="utf-8")
        return {"md": str(md_path), "json": str(json_path)}

    def _render_markdown(self) -> str:
        lines = [
            "# 风险清单",
            "",
            f"- 生成时间：{_iso_now()}",
            f"- 风险条数：{len(self.events)}",
            f"- 整段未翻译：{'是' if self._untranslated_majority else '否'}",
            "",
            "| 阶段 | 文件 | 条目范围 | 原因 | 降级动作 | 影响条数 | 建议 | 严重度 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for e in self.events:
            cells = [e.stage, e.file, e.entry_range, e.reason,
                     e.action, str(e.affected_count), e.suggestion, e.severity]
            lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
        lines.append("")
        return "\n".join(lines)


def _md_cell(text) -> str:
    """Markdown 表格单元格转义：竖线/换行会破坏表格结构。"""
    return str(text or "").replace("|", "/").replace("\n", " ")
