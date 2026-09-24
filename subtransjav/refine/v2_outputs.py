"""1.3.0 pipeline_v2 拆分批次 1·模块 D：输出/临时区辅助层。

职责：原子写文本（_atomic_write_text）、流水线临时工作区删除
（_remove_tmp_dir）、闸门0 报告构造与摘要行（_build_gate0_report /
_gate0_summary_line）、批次进度事件映射（_emit_batch_progress /
_BATCH_PROGRESS_RE / _GATE0_REPORT_SAMPLE_CAP）、阶段输出条目级语言
校验（filter_stage_output_srt）、产物备份与陈旧风险清单清理
（_backup_existing_outputs / _remove_stale_risk_reports）、上游覆盖率
阈值容错读取（_asr_meta_min_coverage_pct）。

迁出来源：subtransjav/refine/pipeline_v2.py（逐字迁移，零逻辑改动）。

行为等价声明：本模块属 HRO-2 行为等价拆分（执行契约），所有符号与
源文件逐字一致。特别注明：风险清单不进 delete_resume_artifacts 的
职责边界语义（D2026-0925-01 A5 用户验收裁定）随迁保持，属行为等价
验收范围，不进有意变更豁免。
"""

import contextlib
import os
import re
from pathlib import Path

from .filters import parse_srt
from .risk import SEVERITY_WARNING

# 闸门0 删除样本上限（供质量报告【处置】章节与归档日志，防大文件撑爆）
_GATE0_REPORT_SAMPLE_CAP = 50

# LLM 客户端 ⏳ 进度文本（"批次 3/63"）→ phase_progress 事件载荷的解析规则
_BATCH_PROGRESS_RE = re.compile(r"批次\s*(\d+)\s*/\s*(\d+)")


def _emit_batch_progress(emitter, phase, message) -> None:
    """把 LLM 客户端的 ⏳ 进度文本映射为 phase_progress 事件（ndjson 模式）。

    无法解析的进度文本静默跳过（人类可读通道照常 print）。
    """
    if emitter is None:
        return
    m = _BATCH_PROGRESS_RE.search(message or "")
    if not m:
        return
    done, total = int(m.group(1)), int(m.group(2))
    emitter.emit("phase_progress", phase=phase,
                 payload={"batch": done, "done": done, "total": total})


def _remove_tmp_dir(tmp_dir: str) -> None:
    """删除本文件的流水线临时工作区（忽略错误，不抛异常）。"""
    import shutil
    if tmp_dir and Path(tmp_dir).is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _atomic_write_text(path: str, text: str):
    """原子写文本文件：同目录临时文件 + os.replace，防止中断留下半截产物。

    参考 filter_stage_output_srt 的 mkstemp+finally 模式；
    成功 replace 后 finally 中的 unlink 因临时文件已不存在而静默跳过。
    """
    import tempfile
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".srt.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _asr_meta_min_coverage_pct(cfg) -> float:
    """上游语音覆盖率告警阈值；非法值静默回退默认 30（该链路一贯容错）。"""
    try:
        return float(getattr(cfg, "v2_asr_meta_min_coverage_pct", 30))
    except (TypeError, ValueError):
        return 30.0


def _build_gate0_report(source_name: str, stats: dict, upstream: dict,
                        samples: list, quarantine: dict = None) -> dict:
    """构造 gate0_summary NDJSON 事件 payload（schema 契约由
    tests/test_pipeline_v2.py 钉住；1.2.1 起 {stem}_幻觉处置报告.json
    不再落盘，本函数仅服务事件通道）。

    gate0_ran 恒为 True：闸门0 在管线头部无条件执行（受信 resume 下
    幂等——指纹校验保证规则/档位/信号语义与原次一致），摘要如实
    记录真实计数，不做归零处理（D2026-0914-01 追记裁决）。

    quarantine 参数：隔离区结论（{"candidates", "quarantined", "file"}）。
    省略时（直调/单测）为"回捞未执行"基线——candidates 按 stats 如实
    计数，quarantined/file 记 null（final 回捞尚未判定）。payload 另含
    noise_left_empty（历史字段：D1 后阶段A 不再留空删条，管线恒
    回填 0，仅为事件 schema 兼容保留）。
    """
    tripped = bool(stats.get("valve_tripped"))
    if quarantine is None:
        quarantine = {"candidates": len(stats.get("quarantine_candidates")
                                        or []),
                      "quarantined": None, "file": None}
    return {
        "report_version": 1,
        "source": source_name,
        "gate0_ran": True,
        "mode": stats.get("mode"),
        "total": stats.get("total", 0),
        "deleted": stats.get("deleted", 0),
        "detected_total": stats.get("detected_total", 0),
        "valve": {
            "tripped": tripped,
            "pct": stats.get("valve_pct"),
            "message": ("拦截率超阈值，本文件降级为只计数模式" if tripped else None),
        },
        "categories": stats.get("categories") or {},
        "samples": list(samples or []),
        "upstream": upstream,
        "quarantine": quarantine,
        "noise_left_empty": stats.get("noise_left_empty", 0),
    }


def _gate0_summary_line(stats: dict) -> str:
    """R8：每文件闸门0 计数行（经 collector.summary_lines 聚合输出）。"""
    valve_word = "触发降级只计数" if stats.get("valve_tripped") else "保险阀未触发"
    return (f"🚪 闸门0：删除 {stats.get('deleted', 0)}"
            f"/原始 {stats.get('total', 0)}，"
            f"检出计数 {stats.get('detected_total', 0)}"
            f"（{stats.get('mode', 'default')} 档，{valve_word}）")


def filter_stage_output_srt(srt_content: str, stage_index: int,
                            target: str) -> tuple:
    """对条目列表（而非文件路径）执行语言白名单校验。"""
    import tempfile

    from .language_validator import filter_stage_output
    fd, tmp = tempfile.mkstemp(suffix=".srt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(srt_content)
        kept_count, dropped = filter_stage_output(tmp, stage_index, target)
        kept_entries = parse_srt(Path(tmp).read_text(encoding="utf-8"))
        return kept_entries, dropped
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _remove_stale_risk_reports(out_dir: str, stem: str) -> list:
    """写入新风险清单前移除上一轮遗留的 {stem}_风险清单.md/.json。

    主理由（职责边界，D2026-0925-01 A5 用户验收裁定）：风险清单与
    final_cn.srt/质量报告.txt 同属最终交付物，而 delete_resume_artifacts
    的契约是清理可重建的恢复现场——成品不进恢复现场清理清单，故不收编。
    三类清理职责互斥：_backup_existing_outputs 保上一轮成品（备份不删）、
    本函数清"有清单无终稿"的失败残留（写前清，与隔离区"先清后写"同款
    纪律）、delete_resume_artifacts 清恢复现场（不碰成品）。
    "成功路径调用点在 write_reports 之后，收编会误删新报告"仅为当前
    实现下的辅证，不作裁定依据（调用点会随重构漂移）。
    文件名与 risk.py 的 write_reports 双钉，契约测试防漂移；本语义
    在 1.3.0 拆分中属行为等价验收范围，不进"有意变更"豁免清单。
    """
    removed = []
    for suffix in ("_风险清单.md", "_风险清单.json"):
        p = Path(out_dir) / f"{stem}{suffix}"
        if p.is_file():
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass
    return removed


def _backup_existing_outputs(out_dir: str, stem: str, collector=None,
                             file_name: str = None) -> None:
    """--force 重跑前的产物备份（仅精确匹配文件名，存在才备份）。

    对输出目录中确切名为 ``{stem}_final_cn.srt``、``{stem}_质量报告.txt``、
    ``{stem}_分歧复核.csv``、``{stem}_术语冲突观察.csv``、
    ``{stem}_风险清单.md``、``{stem}_风险清单.json`` 的文件，复制为同目录
    ``{原名去扩展}_bak_YYYYMMDD_HHMMSS.{原扩展}``；同一次运行共用同一时间戳。
    精确匹配保证旧的 ``*_bak_*`` 文件不会被再次备份。
    """
    import shutil
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for suffix in ("_final_cn.srt", "_质量报告.txt", "_分歧复核.csv",
                   "_术语冲突观察.csv", "_风险清单.md", "_风险清单.json"):
        p = Path(out_dir) / f"{stem}{suffix}"
        if not p.is_file():
            continue
        bak = p.with_name(f"{p.stem}_bak_{ts}{p.suffix}")
        try:
            shutil.copy2(p, bak)
            print(f"   💾 已备份: {p.name} -> {bak.name}")
        except Exception as e:
            print(f"   ⚠️ 备份失败（忽略）: {p.name} -> {e}")
            if collector is not None:
                collector.add(stage="force", file=file_name,
                              reason=f"产物备份失败: {p.name} -> {e}",
                              action="跳过备份直接覆盖",
                              severity=SEVERITY_WARNING)
