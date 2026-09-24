"""
v2 兜底规则层：local 档 strict 清洗/误译拦截 + 语言白名单过滤
==============================================================
职责：profile 驱动的兜底规则层（_apply_fallback_rules——local 档
cleaner_rules 清洗 + post_validate 误译拦截）、语言白名单校验与
[未翻译] 标记回填/时间轴排序（_filter_language）。

迁出来源：subtransjav/refine/pipeline_v2.py（1.3.0 pipeline_v2 拆分
批次 2·模块 F）。代码体逐字迁移、零改动；行为等价依据 D2026-0922-03
HRO-2（逐字节等价）与 D2026-0925-01（执行契约）。

依赖说明：本模块只依赖叶子模块（config/filters/risk）与批次 1 的
v2_premerge、v2_outputs，严禁反向依赖 pipeline_v2。logger 刻意沿用
"subtransjav.refine.pipeline_v2" 命名，保证日志名与拆分前逐字节一致。
"""

import logging

from .config import RefineConfig
from .filters import build_srt, parse_srt
from .risk import SEVERITY_WARNING
from .v2_outputs import filter_stage_output_srt
from .v2_premerge import (
    UNTRANSLATED_PREFIX,
    _align_orig_by_timing,
    _timing_span,
)

# 字节级日志名一致（R4）：拆分前后日志输出不含模块名差异
logger = logging.getLogger("subtransjav.refine.pipeline_v2")


def _apply_fallback_rules(cfg: RefineConfig, entries: list,
                          orig_entries: list,
                          collector=None, file_name: str = None) -> tuple:
    """兜底规则层（profile 驱动）：
    local(strict) → cleaner_rules 清洗 + post_validate 误译拦截；
    cloud(lenient) → 跳过（仅保留语言白名单等零维护校验）。
    返回 (entries, validator_warnings, clean_merged, flagged_indexes, clean_stats)：
      validator_warnings — post_validate 告警列表（传给质量报告）；
      clean_merged — cleaner 碎片合并减少的条数（int，lenient 档为 None；
        删除数不再混入，见 clean_stats["deleted"]）；
      flagged_indexes — post_validate 标记的行 index 集合（TM 学习准入用）；
      clean_stats — cleaner 结构化统计 dict（merged/deleted/deleted_by_rule/
        kept_by_source_evidence；lenient 档或清洗失败时为 None）。"""
    if cfg.v2_profile != "local":
        return entries, [], None, set(), None

    # post_validate：で误译修正 + 主语误判告警（YAML 单一数据源驱动）
    validator_warnings = []
    flagged_indexes: set = set()
    try:
        from .post_validate import check_and_fix_translation_errors
        fixes, warnings, flagged_indexes = check_and_fix_translation_errors(
            orig_entries, entries)
        if fixes:
            print(f"   🔍 兜底拦截: 修正 {fixes} 条误译")
        for w in warnings:
            logger.warning("post_validate: %s", w)
            print(f"   {w}")
        validator_warnings = warnings
    except Exception as e:
        print(f"   ⚠️ 兜底拦截失败（忽略）: {e}")
        if collector is not None:
            collector.add(stage="A", file=file_name,
                          reason=f"误译拦截失败: {e}",
                          action="跳过post_validate误译拦截",
                          affected_count=len(entries),
                          severity=SEVERITY_WARNING)

    # cleaner_rules：规则清洗（删除残余噪音/碎片）
    clean_merged = 0
    clean_stats = None
    try:
        from .cleaner_rules import clean_srt
        # 源侧证据门槛（v1.2.1 P0）：清洗前条目按时间轴对齐源文日文后
        # 传入 clean_srt——删除类规则（L3-L12）须源文佐证（任一源文行含
        # 汉字，或证据缺失）才免删；合并条目在 clean_srt 内部继承成员
        # 源文集合。对不齐的条目无证据 → fail-safe 保留。
        pre_aligned = _align_orig_by_timing(entries, orig_entries)
        source_map = {}
        for e, o in zip(entries, pre_aligned, strict=False):
            if o is not None and (o.get("text") or "").strip():
                source_map[e["timing"]] = o["text"]
        cleaned, clean_stats = clean_srt(build_srt(entries),
                                         config_dir=cfg.cleaner_config_dir or None,
                                         source_map=source_map)
        cleaned_entries = parse_srt(cleaned)
        clean_merged = int(clean_stats.get("merged", 0))
        n_deleted = int(clean_stats.get("deleted", 0))
        if clean_merged > 0 or n_deleted > 0:
            print(f"   🧹 兜底清洗: 合并碎片 {clean_merged} 条，"
                  f"规则删除 {n_deleted} 条"
                  f"（源侧证据免删 {int(clean_stats.get('kept_by_source_evidence', 0))} 条）")

        # ⚠️ 身份恢复（防错位的关键步骤）：cleaner 会按输出顺序重新编号，
        # 若不恢复，后续 阶段B 的日文参照与 TM 学习都会整体错位——这正是
        # legacy 管线毒化 TM 的同款路径。按时间轴双指针恢复原始编号
        # （同起点多条按顺序消费，不会互相覆盖）。
        aligned = _align_orig_by_timing(cleaned_entries, orig_entries)
        restored = 0
        for e, o in zip(cleaned_entries, aligned, strict=False):
            if o is not None and e["index"] != o["index"]:
                e["index"] = o["index"]
                restored += 1
        if restored:
            print(f"   🔧 已按时间轴恢复 {restored} 条原始编号（防错位）")
        return cleaned_entries, validator_warnings, clean_merged, flagged_indexes, clean_stats
    except Exception as e:
        print(f"   ⚠️ 兜底清洗失败（忽略）: {e}")
        if collector is not None:
            collector.add(stage="A", file=file_name,
                          reason=f"规则清洗失败: {e}",
                          action="跳过规则清洗",
                          affected_count=len(entries),
                          severity=SEVERITY_WARNING)
        return entries, validator_warnings, 0, flagged_indexes, None


def _filter_language(cfg: RefineConfig, entries: list, stage_idx: int) -> list:
    """语言白名单校验（零维护通用校验，两档 profile 均启用）。

    D1：非中文/乱码条目不再从终稿物理删除——改加 [未翻译] 前缀保留；
    已带 [未翻译] 标记的条目跳过校验（_keep_original 条目由调用方预先
    分流，不经此处）。明细仍归档 Errors/dropped_entries.log。
    """
    normal, marked = [], []
    for e in entries:
        if (e.get("text") or "").startswith(UNTRANSLATED_PREFIX):
            marked.append(e)         # 已标记条目跳过校验（防二次加标）
        else:
            normal.append(e)
    srt = build_srt(normal)
    kept_entries, dropped = filter_stage_output_srt(srt, stage_idx, "zh")
    # build_srt 会重排序号：按时间轴（条目的真实身份标识）映射回原条目——
    # 有效条目恢复原 index；无效条目加 [未翻译] 前缀后并回产物（不丢行）。
    by_timing = {e["timing"]: e for e in normal}
    kept_timings = set()
    for e in kept_entries:
        if e["timing"] in by_timing:
            e["index"] = by_timing[e["timing"]]["index"]
        else:
            # LLM 把「序号+时间码」写进条目正文时，_SRT_BLOCK 的前瞻会把
            # 它拆成独立伪条目（Errors/dropped_entries.log 实证
            # '1002\n01:37:10,439 --> 01:37:11,899' 形态），其 timing 不在
            # 由 normal 构建的 by_timing 中。此时回退保留 parse_srt 给出的
            # 临时编号（kept 条目必有 index 字段，缺省 0），不再抛 KeyError
            # ——否则异常经 _run_single_v2 穿透 _process_file 兜底 except，
            # 升级为整文件失败。产物排序按时间轴（_timing_span），不受
            # 临时编号影响。
            e["index"] = e.get("index", 0)
            logger.warning(
                "语言过滤：timing %r 不在原条目中（疑似 LLM 正文内嵌"
                "「序号+时间码」伪条目），保留临时编号 %s",
                e["timing"], e["index"])
        kept_timings.add(e["timing"])
    for e in normal:
        if e["timing"] not in kept_timings:
            kept_entries.append({"index": e["index"], "timing": e["timing"],
                                 "text": UNTRANSLATED_PREFIX + (e["text"] or "")})
    kept_entries.extend(marked)
    kept_entries.sort(key=lambda e: _timing_span(e["timing"])[0])
    if dropped:
        print(f"   🧹 乱码/幻觉残留：{dropped} 条加 [未翻译] 标记保留"
              f"（不删除，明细 -> Errors/dropped_entries.log）")
    return kept_entries
