"""
质量报告（人工复核工单）
========================
每次 v2 任务结束后对 终稿 vs 预合并后期望时间轴 生成"复核工单"式报告
并落盘，供无人值守流程结束后快速定位需要人工介入的具体条目。

设计原则：
- 对齐率以 预合并后期望时间轴 为基准（原始 span 集合会因预合并/
  规则清洗天然对不上，产生 97-98% 的噪声）；
- 假名残留按条目归并（同一条目只出一条，其下汇总全部假名片段；
  仅陈述事实，不做幻觉/未译断言，交人工确认）；
- [未翻译] 条目单列一节（D1：不再删除、带前缀保留在终稿）；
- 闸门0 删除台账单列【处置】一节（总数/分类/样本，全量见归档日志）；
- 实义漏覆盖逐条列出（无门槛，上限 20 条；拆"整条缺失/条目在但未译"
  两口径，总数 = 之和）；
- post_validate 告警逐条进入复核清单，warn_only=false 的硬性告警一并呈现；
- 统计段含条数核对恒等式（原文 = 闸门0删除 + 预合并合并 + 规则清洗
  合并 + 规则清洗删除 + 隔离区移出 + 终稿），不平则以 ⚠️ 显式呈现，
  绝不静默。
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from .pass_disagreement import (
    REVIEW_OPTIONAL_THRESHOLD,
    REVIEW_STRICT_THRESHOLD,
)
from .pass_disagreement import (
    _timing_span as _disagree_span,
)

_KANA_RE = re.compile(r"[ぁ-ゖァ-ヺー]")
_KANJI_RE = re.compile(r"[一-鿿々]")
_KANA_SEQ_RE = re.compile(r"[ぁ-ゖァ-ヺー]{2,}")   # 假名连续串（残留分类用）
_DASH_RE = re.compile(r"^\s*-\s")
_IDX_IN_WARNING_RE = re.compile(r"#(\d+)")

# 敏感词提示集（过度净化检测用；与角色卡分级表非一一对应，仅做存在性检查）
_SENSITIVE_HINTS: tuple[str, ...] = (
)

# 漏覆盖清单上限（超出注明"其余 N 条略"）
_MISS_LIST_LIMIT = 20
# 未翻译小节列出上限（超出注明"其余 N 条略"）
_UNTRANSLATED_LIST_LIMIT = 20
# 假名残留片段清单上限（同一条目内；超出显示"等 N 段"）
_KANA_FRAG_LIMIT = 5
# 处置章节逐条样本上限
_DISPOSAL_SAMPLE_LIMIT = 20
# 处置章节"主要删除规则"分布最多列出的规则个数
_RULE_DIST_LIMIT = 3
# 乱码强译复核小节逐条列出上限（超出注明"其余 N 条略"）
_GARBLE_LIST_LIMIT = 20
# 误听疑似改写小节逐条列出上限（超出注明"其余 N 条略"）
_MISHEAR_LIST_LIMIT = 20
# 术语冲突观察小节逐条列出上限（超出注明"其余 N 条略"）
_CONFLICT_LIST_LIMIT = 20
# 术语冲突观察小节"按术语分布"最多列出的术语个数
_CONFLICT_TERM_DIST_LIMIT = 10
# 术语一致性章节逐条样本上限：每术语 3 条 / 全章 20 条
_CONSISTENCY_TERM_SAMPLE_LIMIT = 3
_CONSISTENCY_SAMPLE_LIMIT = 20


def _weight(text: str) -> float:
    """粗略字重：CJK 记 1，其余记 0.5。"""
    return sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in text)


def _fmt_timing(timing: str) -> str:
    """规整时间轴显示（去多余空白）。"""
    return re.sub(r"\s+", " ", (timing or "").strip())


def _clip(text: str, limit: int = 60) -> str:
    """超长文本截断加省略号（双引擎分歧行显示用）。"""
    t = (text or "").strip()
    if len(t) > limit:
        return t[:limit] + "…"
    return t


def _is_untranslated(text: str) -> bool:
    """判定文本是否为 [未翻译] 残留（实义漏覆盖"条目在但未译"口径专用）。

    同时容忍两种前缀形态："[未翻译] "（带尾空格，pipeline_v2 的
    UNTRANSLATED_PREFIX 现值）与 "[未翻译]"（无空格，提示词教出的形态，
    与本文件既有硬编码一致）；前缀后的残译文（如 "[未翻译] Chicks。"）
    不影响判定——仍是未译。
    """
    return (text or "").startswith("[未翻译]")


# 分歧"可选"区展示上限（超出部分注明见分歧复核 CSV）
_DISAGREE_OPTIONAL_LIMIT = 50
# 分歧复核 CSV "可选"区收录上限
_DISAGREE_CSV_OPTIONAL_LIMIT = 100


def _split_disagreement_rows(rows: list) -> tuple[list, list, list]:
    """把分歧行分为 (必看, 可选, 伪影) 三组。

    阈值取自 pass_disagreement 模块常量（不得在此硬编码）：
    - similarity < REVIEW_STRICT_THRESHOLD 且非伪影 → 必看；
    - REVIEW_STRICT_THRESHOLD ≤ similarity < REVIEW_OPTIONAL_THRESHOLD
      且非伪影 → 可选；
    - artifact=True → 已过滤伪影；
    - similarity ≥ REVIEW_OPTIONAL_THRESHOLD 且非伪影 → 正常行，不属于任何复核区。
    """
    must_see, optional, artifacts = [], [], []
    for r in rows:
        sim = r.get("similarity", 0.0)
        if r.get("artifact"):
            artifacts.append(r)
        elif sim < REVIEW_STRICT_THRESHOLD:
            must_see.append(r)
        elif sim < REVIEW_OPTIONAL_THRESHOLD:
            optional.append(r)
    return must_see, optional, artifacts


def _final_text_for_span(timing: str, final_entries: list | None) -> str:
    """按时间轴最大重叠在 final_entries 中匹配终稿译文；找不到返回空串。"""
    if not final_entries:
        return ""
    s0, e0 = _disagree_span(timing)
    if s0 < 0 or e0 < 0:
        return ""
    best, best_ov = "", 0.0
    for e in final_entries:
        s1, e1 = _disagree_span(e.get("timing", ""))
        if s1 < 0 or e1 < 0:
            continue
        ov = min(e0, e1) - max(s0, s1)
        if ov > best_ov:
            best_ov = ov
            best = (e.get("text") or "").strip()
    return best


def render_disagreement_section(pass_disagreement: dict | None,
                                pass_mode: str = "dual") -> str:
    """渲染「双引擎分歧」章节为文本（纯函数，供报告与离线重算脚本复用）。

    pass_mode: "dual" | "missing_pass1" | "missing_pass2" | "none"。
    非 dual 模式整段降级，只输出模式行。
    """
    lines = ["【双引擎分歧】"]
    if pass_mode != "dual":
        if pass_mode == "missing_pass1":
            lines.append("分歧模式：单引擎（缺少 pass1），本章节降级")
        elif pass_mode == "missing_pass2":
            lines.append("分歧模式：单引擎（缺少 pass2），本章节降级")
        else:
            lines.append("分歧模式：无兄弟 pass 文件，本章节不可用")
        return "\n".join(lines)

    rows = (pass_disagreement or {}).get("rows") or []
    total = (pass_disagreement or {}).get("total", 0)
    matched = (pass_disagreement or {}).get("matched", len(rows))
    lines.append(f"分歧模式：双引擎（可对照 {matched}/{total} 行）")
    if not rows:
        lines.append(f"无分歧行（双引擎匹配 {matched}/{total}）。")
        return "\n".join(lines)

    must_see, optional, artifacts = _split_disagreement_rows(rows)
    lines.append("分歧行按相似度升序排列（最分歧在前）：")

    # 必看区：全量列出
    lines.append(f"【必看 <{REVIEW_STRICT_THRESHOLD:.2f}】共 {len(must_see)} 行")
    for r in must_see:
        lines.append(f"  [{_fmt_timing(r.get('timing', ''))}] "
                     f"相似度 {r.get('similarity', 0.0):.2f}")
        lines.append(f"    pass1: {_clip(r.get('pass1', ''))}")
        lines.append(f"    pass2: {_clip(r.get('pass2', ''))}")

    # 可选区：最多 _DISAGREE_OPTIONAL_LIMIT 行
    lines.append(f"【可选 {REVIEW_STRICT_THRESHOLD:.2f}-{REVIEW_OPTIONAL_THRESHOLD:.2f}】"
                 f"共 {len(optional)} 行")
    for r in optional[:_DISAGREE_OPTIONAL_LIMIT]:
        lines.append(f"  [{_fmt_timing(r.get('timing', ''))}] "
                     f"相似度 {r.get('similarity', 0.0):.2f}")
        lines.append(f"    pass1: {_clip(r.get('pass1', ''))}")
        lines.append(f"    pass2: {_clip(r.get('pass2', ''))}")
    if len(optional) > _DISAGREE_OPTIONAL_LIMIT:
        lines.append(f"（其余 {len(optional) - _DISAGREE_OPTIONAL_LIMIT} 行"
                     f"见分歧复核 CSV）")

    # 已过滤伪影区：固定规则样例（iou 升序、同 iou 按 offset 降序取前 5）
    from .pass_disagreement import ARTIFACT_LONG_MIN, ARTIFACT_SHORT_MAX
    lines.append(f"【已过滤伪影 {len(artifacts)} 行】"
                 f"（判定规则：极短应和/感叹词，或 短侧≤{ARTIFACT_SHORT_MAX}字 "
                 f"且 长侧≥{ARTIFACT_LONG_MIN}字 且 相似度<"
                 f"{REVIEW_STRICT_THRESHOLD:.2f} 的错配）")
    if artifacts:
        sample = sorted(artifacts,
                        key=lambda r: (r.get("iou", 0.0),
                                       -r.get("offset", 0.0)))[:5]
        for r in sample:
            lines.append(f"  [{_fmt_timing(r.get('timing', ''))}] "
                         f"相似度 {r.get('similarity', 0.0):.2f} | "
                         f"iou {r.get('iou', 0.0):.2f} | "
                         f"offset {r.get('offset', 0.0):.2f}s")
            lines.append(f"    pass1: {_clip(r.get('pass1', ''))}")
            lines.append(f"    pass2: {_clip(r.get('pass2', ''))}")
        lines.append("（完整被过滤清单见分歧复核 CSV）")
    return "\n".join(lines)


def render_disposal_section(gate0_deletions: dict | None) -> str:
    """渲染「处置」章节（闸门0 送翻前删除台账）为文本（纯函数）。

    gate0_deletions 结构（由管线从闸门0 结果组装，None 表示未采集，
    整节省略）：
      {"total": 已删条目总数,
       "by_category": {删除原因: 条数}（0 计数类别渲染时跳过）,
       "samples": [{"index", "timing", "reason", "text"}, ...]}
    全量台账固定归档在 Errors/dropped_entries.log（跨文件共用日志）。
    """
    if gate0_deletions is None:
        return ""
    total = int(gate0_deletions.get("total", 0) or 0)
    lines = ["【处置】"]
    lines.append(f"送翻前检测（闸门0）删除 {total} 条"
                 f"（判定为源侧转写噪音，删除明细如下）")
    by_cat = {k: int(v or 0) for k, v in
              (gate0_deletions.get("by_category") or {}).items() if v}
    if by_cat:
        lines.append("按删除原因: " + "、".join(f"{k} {v} 条"
                                              for k, v in by_cat.items()))
    samples = list(gate0_deletions.get("samples") or [])
    if samples:
        lines.append(f"删除样本（前 {_DISPOSAL_SAMPLE_LIMIT} 条）:"
                     if len(samples) > _DISPOSAL_SAMPLE_LIMIT else "删除样本:")
        for s in samples[:_DISPOSAL_SAMPLE_LIMIT]:
            lines.append(f"  #{s.get('index')} {_fmt_timing(s.get('timing', ''))}"
                         f" [{s.get('reason', '')}] {(s.get('text') or '')[:40]}")
    if total > 0:
        lines.append("全量台账见 Errors/dropped_entries.log")
    return "\n".join(lines)


def render_garble_review_section(garble_review: list | None) -> str:
    """渲染「乱码强译复核」章节（D5 语义反转防护）为文本（纯函数）。

    garble_review 结构（由管线在终稿生成后组装，None 表示未采集，
    整节省略）：
      [{"index", "timing", "src_preview", "zh_preview", "signal"}, ...]
    命中口径：源文疑似转写乱码（闸门0计数类/强乱码信号）× 译文流畅
    中文 × 源文不含汉字——只提示人工复核语义方向是否被反转，
    不自动改稿。0 条时显示"无样本"。
    """
    if garble_review is None:
        return ""
    total = len(garble_review)
    lines = ["【乱码强译复核】"]
    if not total:
        lines.append("无样本")
        return "\n".join(lines)
    lines.append(f"共 {total} 条：源文疑似转写乱码但译文通顺，"
                 f"请对照视频复核语义是否被反转"
                 f"（拒绝↔邀请、停止↔继续、否定↔肯定等）")
    for s in garble_review[:_GARBLE_LIST_LIMIT]:
        lines.append(f"  #{s.get('index')} {_fmt_timing(s.get('timing', ''))}"
                     f" [{s.get('signal', '')}]")
        lines.append(f"    源: {s.get('src_preview', '')}")
        lines.append(f"    译: {s.get('zh_preview', '')}")
    if total > _GARBLE_LIST_LIMIT:
        lines.append(f"（其余 {total - _GARBLE_LIST_LIMIT} 条略）")
    return "\n".join(lines)


def render_mishear_review_section(sidecar_review: list | None) -> str:
    """渲染「误听疑似改写」小节（v1.2.2 C1 改写留痕）为文本（纯函数）。

    sidecar_review 结构（由管线在终稿生成后组装；None 表示本片无误听
    怀疑表或 sidecar 未启用，整节省略）：
      [{"index", "timing", "suspect", "correct", "zh_preview"}, ...]
    命中口径：源文命中任一误听疑似词的终稿条目全部列入——命中即列，
    不做是否改写的语义判定（保守审计口径，防改写不留痕的审计缺口）。
    0 条时显示"无样本"；超上限注明"其余 N 条略"。
    """
    if sidecar_review is None:
        return ""
    total = len(sidecar_review)
    lines = ["【误听疑似改写】"]
    if not total:
        lines.append("无样本")
        return "\n".join(lines)
    lines.append(f"共 {total} 条：源文命中误听疑似词（命中即列的保守审计口径，"
                 f"不代表已按疑似义改写），请对照上下文复核译义方向")
    for s in sidecar_review[:_MISHEAR_LIST_LIMIT]:
        lines.append(f"  #{s.get('index')} {_fmt_timing(s.get('timing', ''))}"
                     f" [{s.get('suspect', '')}→疑为{s.get('correct', '')}]")
        lines.append(f"    译: {s.get('zh_preview', '')}")
    if total > _MISHEAR_LIST_LIMIT:
        lines.append(f"（其余 {total - _MISHEAR_LIST_LIMIT} 条略）")
    return "\n".join(lines)


def render_conflict_section(glossary_conflicts: list | None,
                            watch_advice: str | None = None) -> str:
    """渲染「术语冲突观察」小节（v1.2.2 D1 观察闸）为文本（纯函数）。

    glossary_conflicts 结构（由 pipeline 经 glossary_conflict.
    scan_glossary_conflicts 在终稿生成后组装；None 表示无词库/未采集，
    整节省略；空列表显示"无样本"）：
      [{"entry_id", "timing", "source_term", "expected_targets",
        "actual_text"}, ...]
    命中口径：源文命中词库源词（≥2 字符子串）而译文既无主译法也无
    任何别名。观察闸不阻断翻译；watch_advice 为跨运行评估建议行文案
    （evaluate_watch 三态），None 时不输出该行。
    """
    if glossary_conflicts is None:
        return ""
    total = len(glossary_conflicts)
    lines = ["【术语冲突观察】"]
    if not total:
        lines.append("无样本")
        return "\n".join(lines)
    lines.append(f"共 {total} 条：源文命中术语表源词，但译文既无主译法"
                 f"也无别名（观察闸，不阻断；请人工判定是否误伤）")
    dist: dict[str, int] = {}
    for c in glossary_conflicts:
        dist[c.get("source_term", "")] = \
            dist.get(c.get("source_term", ""), 0) + 1
    top = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))[
        :_CONFLICT_TERM_DIST_LIMIT]
    if top:
        lines.append("按术语分布: " + "、".join(f"{k} {v} 条"
                                               for k, v in top))
    for c in glossary_conflicts[:_CONFLICT_LIST_LIMIT]:
        lines.append(f"  #{c.get('entry_id')} {_fmt_timing(c.get('timing', ''))}"
                     f" [{c.get('source_term', '')}]"
                     f" 期望: {c.get('expected_targets', '')}")
        lines.append(f"    译: {c.get('actual_text', '')}")
    if total > _CONFLICT_LIST_LIMIT:
        lines.append(f"（其余 {total - _CONFLICT_LIST_LIMIT} 条略）")
    lines.append("（逐条五元组清单见同目录 *_术语冲突观察.csv）")
    if watch_advice:
        lines.append(f"转阻断评估: {watch_advice}（只评估不自动切换，"
                     f"是否阻断由用户裁决）")
    return "\n".join(lines)


def render_term_consistency_section(term_stats: list | None) -> str:
    """渲染「术语一致性」章节（v1.2.2 D2）为文本（纯函数）。

    term_stats 结构（与冲突扫描同一次遍历产出；None 表示无词库/未采集，
    整节省略；空列表显示"无样本"）：
      [{"term", "target", "aliases", "hits", "with_main", "with_alias",
        "with_neither", "samples": [{"entry_id", "timing",
        "actual_text"}]}, ...]
    每术语一行四档计数；均不含（疑似冲突/未译出档）行下挂样本，
    每术语最多 _CONSISTENCY_TERM_SAMPLE_LIMIT 条、全章最多
    _CONSISTENCY_SAMPLE_LIMIT 条。
    """
    if term_stats is None:
        return ""
    lines = ["【术语一致性】"]
    hit_terms = [s for s in term_stats if s.get("hits", 0)]
    if not hit_terms:
        lines.append("无样本")
        return "\n".join(lines)
    lines.append(f"词库命中 {len(hit_terms)} 个源词"
                 f"（按均不含行数降序；样本上限 "
                 f"每术语 {_CONSISTENCY_TERM_SAMPLE_LIMIT} 条/"
                 f"全章 {_CONSISTENCY_SAMPLE_LIMIT} 条）")
    shown = 0
    for s in sorted(hit_terms,
                    key=lambda s: (-s.get("with_neither", 0),
                                   -s.get("hits", 0), s.get("term", ""))):
        lines.append(
            f"源词「{s.get('term', '')}」→ 主译法「{s.get('target', '')}」: "
            f"命中 {s.get('hits', 0)} | 含主译法 {s.get('with_main', 0)} | "
            f"含别名 {s.get('with_alias', 0)} | "
            f"均不含 {s.get('with_neither', 0)}")
        for sample in (s.get("samples") or [])[
                :_CONSISTENCY_TERM_SAMPLE_LIMIT]:
            if shown >= _CONSISTENCY_SAMPLE_LIMIT:
                break
            lines.append(f"  #{sample.get('entry_id')} "
                         f"{_fmt_timing(sample.get('timing', ''))} "
                         f"译: {sample.get('actual_text', '')}")
            shown += 1
    return "\n".join(lines)


def write_divergence_review_csv(out_path: str, rows: list, file_label: str,
                                final_entries: list | None = None) -> None:
    """分歧复核 CSV 落盘（独立可调用，输出路径由调用方指定）。

    行范围：必看区全部 + 可选区前 _DISAGREE_CSV_OPTIONAL_LIMIT 行
    + 全部被过滤伪影行。编码 utf-8-sig（Excel 直开不乱码）。
    final_entries 提供时按时间轴重叠匹配终稿译文，无则该列留空。
    """
    must_see, optional, artifacts = _split_disagreement_rows(rows)
    picked = ([(r, "必看") for r in must_see]
              + [(r, "可选") for r in optional[:_DISAGREE_CSV_OPTIONAL_LIMIT]]
              + [(r, "已过滤") for r in artifacts])
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["文件名", "分组", "时间轴", "相似度", "iou", "offset",
                    "artifact", "pass1", "pass2", "final_cn译文"])
        for r, group in picked:
            w.writerow([
                file_label,
                group,
                _fmt_timing(r.get("timing", "")),
                f"{r.get('similarity', 0.0):.4f}",
                f"{r.get('iou', 0.0):.4f}",
                f"{r.get('offset', 0.0):.3f}",
                "是" if r.get("artifact") else "否",
                r.get("pass1", ""),
                r.get("pass2", ""),
                _final_text_for_span(r.get("timing", ""), final_entries),
            ])


# 章节标题 → 白话注解（组装层后处理用）。注意：只在 build_quality_report
# 的组装层追加注解行，render_* 纯函数本身不感知——离线报告
# （tools/recompute_divergence.py）与 render 直调不受影响（离线报告
# 无这些标题自然无注解）。
_SECTION_NOTES: tuple[tuple[str, str], ...] = (
    ("【需复核清单】",
     "　（本节逐条列出待人工确认的条目，处理时对照下方统计区总数"
     "即可核对有无遗漏）"),
    ("【未翻译】",
     "　（这些行以占位前缀保留原文未删除，可人工补译或接受现状）"),
    ("【处置】",
     "　（送翻前质量闸门拦下的条目：原因与去向摘要，机器全量台账"
     "另见日志文件）"),
    ("【乱码强译复核】",
     "　（译文出现乱码腔或与原文语义脱节的行，请对照原文逐条确认）"),
    ("【误听疑似改写】",
     "　（上游语音识别可能误听导致的改写行，请对照音频确认）"),
    ("【术语冲突观察】",
     "　（译文与词表规定译法不一致的观察记录，供术语口径统一时裁定）"),
    ("【术语一致性】",
     "　（各源词在译文中的覆盖情况统计）"),
    ("【双引擎分歧】",
     "　（两遍引擎译法不同的行，可选抽查；引擎降级时此处仅显示模式）"),
    ("【统计】",
     "　（本次运行全部可核对指标；条数恒等式在本区，白话导读数字"
     "与本区同源）"),
)


def _apply_section_notes(lines: list[str]) -> list[str]:
    """组装层后处理：章节标题行之后插入一行全角空格开头的白话注解。

    纯函数，不改入参；标题匹配用 startswith（【未翻译】标题行带条数
    后缀，非单行精确标题）。
    """
    out: list[str] = []
    for ln in lines:
        out.append(ln)
        for prefix, note in _SECTION_NOTES:
            if ln.startswith(prefix):
                out.append(note)
                break
    return out


def _render_plain_guide(items_count: int,
                        ts: str,
                        n_src: int,
                        rhs_total: int,
                        f_final: int,
                        missed_total: int,
                        untranslated_count: int) -> list[str]:
    """渲染报告头部的白话导读区（恒有，两分支），纯函数、内部不抛。

    全部数字与报告其他区域同源：items_count 即【结论】行的 len(items)；
    n_src/rhs_total 与条数核对恒等式同源；missed_total/untranslated_count
    与【统计】区同源。文案守黑名单（无"疑似"、无【】章节字面引用）。
    """
    lines = [f"【白话导读】基于本次运行（时间: {ts}）："]
    if items_count:
        lines.append(f"　① 总体：需人工复核 {items_count} 处，"
                     "逐条见下方复核清单")
    else:
        lines.append("　① 总体：未发现需人工复核的条目，终稿可直接使用")
    if n_src == rhs_total:
        lines.append(f"　② 条目链路：原文 {n_src} 条 → 终稿 {f_final} 条，"
                     "条数核对已平衡")
    else:
        lines.append(f"　② 条目链路：原文 {n_src} 条 → 终稿 {f_final} 条，"
                     "条数核对不平（详见统计区）")
    if missed_total:
        lines.append(f"　③ 漏覆盖：实义内容漏覆盖 {missed_total} 条")
    else:
        lines.append("　③ 漏覆盖：未发现实义内容漏覆盖")
    if untranslated_count:
        lines.append(f"　④ 未翻译残留：{untranslated_count} 条以占位形式"
                     "保留，可人工补译")
    lines.append("　⑤ 看报告顺序建议：先看本区 → 再逐条看复核清单 → "
                 "最后用统计区核对总数")
    return lines


def resolve_final_block(final_entries: list[dict], index: int) -> dict | None:
    """报告 index → 终稿块的官方映射（首次精确 index 匹配）。

    契约（D11）：
    - 精确 index 匹配为主，不做 timing 猜测；
    - 合并块 index 非单射：v2_premerge 合并保留首条 index，被并条目
      index 成空洞；同 index 出现多块（如语言过滤伪条目 index 缺省 0
      与真实 #0 重号）时取列表顺序首个；
    - 禁止算术外推：找不到精确命中的 index 时返回 None（调用方按
      "不可自动重翻" 处理），绝不 ±1 猜测相邻块。
    """
    for e in final_entries or []:
        if e.get("index") == index:
            return e
    return None


def build_quality_report(orig_entries: list, final_entries: list,
                         source_name: str = "",
                         expected_entries: list | None = None,
                         merge_stats: dict | None = None,
                         validator_warnings: list | None = None,
                         pass_disagreement: dict | None = None,
                         pass_mode: str = "dual",
                         gate0_deletions: dict | None = None,
                         garble_review: list | None = None,
                         sidecar_review: list | None = None,
                         orig_total: int | None = None,
                         profile: str = "",
                         glossary_conflicts: list | None = None,
                         term_consistency: list | None = None,
                         conflict_watch_advice: str | None = None,
                         tm_exact_hits: int | None = None,
                         tm_learned_count: int | None = None,
                         guide_sink: dict | None = None,
                         structured_warnings: list[dict] | None = None) -> str:
    """对比 期望条目（预合并后） 与 终稿条目，返回复核工单式报告文本。

    Parameters
    ----------
    orig_entries : list
        原始条目（统计口径参考）。
    final_entries : list
        终稿条目。
    source_name : str
        来源文件名。
    expected_entries : list | None
        预合并后的期望条目列表；None 时退回用 orig_entries。
    merge_stats : dict | None
        合并统计，如 {"premerge_merged": 154, "clean_merged": 12,
        "clean_deleted": 3, "clean_deleted_by_rule": {"L8-...": 2},
        "clean_kept_by_evidence": 2}；
        缺省键按 0 处理，clean_merged 键缺失视为"规则清洗未运行"。
        v1.2.2 C2 追加键：clean_kept_by_noise_gate（L7/L8/L11 因无噪声
        证据而免删的条数）与 clean_kept_by_noise_gate_timings（对应条目
        时间轴串列表）——键存在时统计段渲染"纯假名实义保留"行；键缺失
        （规则清洗未运行/旧调用方）不显示该行。
    validator_warnings : list | None
        post_validate 返回的告警字符串列表，逐条进入复核清单。
    pass_mode : str
        双引擎分歧模式：dual | missing_pass1 | missing_pass2 | none
        （由 pass_disagreement.probe_disagreement_mode 探测）。
    gate0_deletions : dict | None
        闸门0 送翻前删除台账 {"total", "by_category", "samples"}；
        None 时【处置】章节整体省略。
    garble_review : list | None
        乱码强译复核候选 [{"index", "timing", "src_preview",
        "zh_preview", "signal"}, ...]（D5：源文疑似乱码 × 译文通顺）；
        None 时【乱码强译复核】章节整体省略，空列表显示"无样本"。
    sidecar_review : list | None
        误听疑似改写留痕 [{"index", "timing", "suspect", "correct",
        "zh_preview"}, ...]（v1.2.2 C1：源文命中误听疑似词的终稿条目，
        命中即列的保守审计口径）；None 时【误听疑似改写】章节整体省略，
        空列表显示"无样本"。
    orig_total : int | None
        闸门0 前原始条目总数（恒等式"原文 N"取值）；缺省时按
        len(orig_entries) + 闸门0删除 + 预合并合并 推算。
    glossary_conflicts : list | None
        术语冲突观察清单 [{"entry_id", "timing", "source_term",
        "expected_targets", "actual_text"}, ...]（v1.2.2 D1 观察闸：
        源文命中词库源词而译文无主译法也无别名）；None 时【术语冲突
        观察】章节整体省略，空列表显示"无样本"。
    term_consistency : list | None
        逐术语一致性统计 [{"term", "target", "aliases", "hits",
        "with_main", "with_alias", "with_neither", "samples"}, ...]
        （v1.2.2 D2，与冲突扫描同一次遍历产出）；None 时【术语一致性】
        章节整体省略，空列表显示"无样本"。
    conflict_watch_advice : str | None
        跨运行转阻断评估建议文案（glossary_conflict.evaluate_watch
        三态）；None 不输出建议行。观察闸只评估不自动切换。
    tm_exact_hits : int | None
        阶段A TM 精确命中条数（【统计】段 TM 摘要行 H）；None 取不到
        （TM 未启用/阶段A 复用）时该侧显示"无样本"。
    tm_learned_count : int | None
        本次 _learn_to_tm 实际入库条数（TM 摘要行 L）；None 取不到时
        该侧显示"无样本"。

    条数核对恒等式（统计段"条数核对"行）各项定义：
      原文 N —— 闸门0 前原始条目总数（orig_total，缺省按上述推算）；
      闸门0删除 D —— gate0_deletions["total"]（送翻前源侧噪音删除）；
      预合并合并 M —— merge_stats["premerge_merged"]（断句预合并减少的条数）；
      规则清洗合并 C —— merge_stats["clean_merged"]（未运行按 0）；
      规则清洗删除 E —— merge_stats["clean_deleted"]（未运行按 0）；
      隔离区移出 Q —— merge_stats["quarantine_moved"]（H5 隔离区回捞把
        存疑条目移出主稿进 *_隔离区.srt 的条数；键缺失/为 0（无隔离）
        时该项不显示，恒等式退回五项形式——故选"不显示"而非"+0"）；
      终稿 F —— len(final_entries)。
    成立（N = D+M+C+E+Q+F）→ 前缀 ✅；不成立 → 前缀 ⚠️ 并显示两侧数值
    （通常意味着存在未入账的条目减少，如漏覆盖，绝不静默）。
    v1.2.2 C2：噪声闸门免删的条目（clean_kept_by_noise_gate）是"保留"
    而非"减少"——这些行在"终稿"侧计入 F，恒等式结构不变。
    profile : str
        档位名（"local"=strict / "cloud"=lenient），仅用于规则清洗
        未运行时的展示措辞。
    structured_warnings : list[dict] | None
        post_validate 的结构化告警（check_and_fix_translation_errors
        第四返回值，与 validator_warnings 同序等长的 dict 列表）。
        提供时进入导读 json 的 items[]（version 2）；None（旧调用方）
        时 items 仅含 untranslated 条目。items 的 current_text 经
        resolve_final_block 按精确 index 映射终稿块，映射不到（被并/
        被删/被隔离移出）即为 null——语义即"不可自动重翻"，不另设
        字段。
    """
    # 拆分后不再反向依赖 facade（pipeline_v2）：_timing_span 以叶子模块
    # v2_premerge 为准（与 v2_rules/v2_outputs 同源）
    from .v2_premerge import _timing_span

    expected = expected_entries if expected_entries is not None else orig_entries
    merge_stats = merge_stats or {}
    validator_warnings = validator_warnings or []
    premerged = merge_stats.get("premerge_merged", 0)
    clean_merged = merge_stats.get("clean_merged")

    # span → 条目列表 映射（同一时间轴可能多条，不能用单值 dict）
    def _index_by_span(entries: list) -> dict:
        d: dict[tuple, list] = {}
        for e in entries:
            span = _timing_span(e["timing"])
            if span[0] >= 0:
                d.setdefault(span, []).append(e)
        return d

    expected_by_span = _index_by_span(expected)
    final_by_span = _index_by_span(final_entries)

    def _span_text(span) -> str:
        return "".join((e["text"] or "").strip()
                       for e in final_by_span.get(span, []))

    def _expected_src(span) -> str:
        """终稿条目对应期望条目的源文（找不到返回空串）。"""
        return "".join((e["text"] or "").strip()
                       for e in expected_by_span.get(span, []))

    # ------------------------------------------------------------------
    # 复核清单收集（条目为多行文本块，序号在汇总时统一编号）
    # ------------------------------------------------------------------
    items = []           # [(类别标签, [行...])]
    concl = []           # 结论段计数片段

    # 1) 假名残留按条目归并（[未翻译] 条目走独立小节，不进入本章；
    #    同一条目只出一条复核项，其下汇总全部假名片段；
    #    残留成因难自动判定，仅陈述事实交人工确认）
    kana_total = 0
    for e in final_entries:
        text = (e["text"] or "").strip()
        if not text or text.startswith("[未翻译]"):
            continue
        seqs = _KANA_SEQ_RE.findall(text)
        if not seqs:
            continue
        kana_total += 1          # 语义：残留"条目数"（非片段数）
        span = _timing_span(e["timing"])
        src = _expected_src(span)
        src_show = src if src else "（无对应期望条目）"
        hit = [s for s in seqs if s in src]
        if hit and len(hit) == len(seqs):
            src_note = "源文对应条目含相同串"
        elif hit:
            src_note = "源文对应条目含其中部分串"
        else:
            src_note = "源文对应条目中未出现该串"
        # 片段清单：最多列 _KANA_FRAG_LIMIT 段；≥2 段时以"等 N 段"
        # 收尾标注总段数（公文列举用法；1 段时不加，避免"等 1 段"）
        frag = "、".join(f"「{s}」" for s in seqs[:_KANA_FRAG_LIMIT])
        if len(seqs) >= 2:
            frag += f" 等 {len(seqs)} 段"
        items.append(("假名残留", [
            f"[假名残留·需人工确认] #{e['index']} {_fmt_timing(e['timing'])}",
            f"   源: {src_show[:60]}",
            f"   译: {text[:60]}",
            f"   残留假名串（共 {len(seqs)} 段）: {frag}"
            f"（终稿该条仍含假名串；{src_note}）",
        ]))
    if kana_total:
        concl.append(f"假名残留 {kana_total} 条")

    # 2) [未翻译] 条目（D1：不再删除，带前缀保留在终稿）——单列小节，
    #    不进入需复核清单（与假名残留章互不重复计数）
    untranslated = [e for e in final_entries
                    if (e["text"] or "").startswith("[未翻译]")]
    untranslated_lines = []
    for e in untranslated:
        span = _timing_span(e["timing"])
        src = _expected_src(span)
        raw = (e["text"] or "")[len("[未翻译]"):].strip()
        src_show = src if src else (raw or "（无对应期望条目）")
        untranslated_lines.append(
            f"  #{e['index']} {_fmt_timing(e['timing'])} 原文: {src_show[:60]}")

    # 3) 实义内容漏覆盖（含汉字原文行），按终稿成因拆两个口径：
    #    - missing_entry（整条缺失）：终稿该时间轴 span 完全无条目（原口径）；
    #    - untranslated_content（条目在但未译）：span 有条目，但命中条目
    #      文本均以 [未翻译] 前缀开头（兼容带/不带尾空格两种形态，
    #      前缀后的残译文不影响判定——仍是未译）。
    #    两口径互斥，总数 = 之和；无门槛全部列出，上限 _MISS_LIST_LIMIT 条。
    kanji_orig = [e for e in expected if _KANJI_RE.search(e["text"] or "")]
    missed = []               # 口径1：整条缺失
    missed_untranslated = []  # 口径2：条目在但未译
    for e in kanji_orig:
        hits = final_by_span.get(_timing_span(e["timing"])) or []
        if not hits:
            missed.append(e)
        elif all(_is_untranslated(h.get("text") or "") for h in hits):
            missed_untranslated.append(e)
    missed_total = len(missed) + len(missed_untranslated)
    missed_review = ([(e, False) for e in missed]
                     + [(e, True) for e in missed_untranslated])
    for e, untranslated_hit in missed_review[:_MISS_LIST_LIMIT]:
        if untranslated_hit:
            items.append(("漏覆盖", [
                f"[实义漏覆盖·条目在但未译] #{e['index']} "
                f"{_fmt_timing(e['timing'])}",
                f"   源: {(e['text'] or '')[:60]}",
                "   （终稿条目在，但译文为 [未翻译] 残留）",
            ]))
        else:
            items.append(("漏覆盖", [
                f"[实义漏覆盖] #{e['index']} {_fmt_timing(e['timing'])}",
                f"   源: {(e['text'] or '')[:60]}",
                "   （终稿中无对应条目或译文为空）",
            ]))
    if missed_total:
        concl.append(f"漏覆盖 {missed_total}（整条缺失 {len(missed)}"
                     f" + 条目在但未译 {len(missed_untranslated)}）")

    # 4) 校验告警（post_validate；含"主语误判"的单独归类）
    subject_warns = [w for w in validator_warnings if "主语误判" in w]
    other_warns = [w for w in validator_warnings if "主语误判" not in w]
    final_idx = {e["index"]: e for e in final_entries}
    for w in validator_warnings:
        m = _IDX_IN_WARNING_RE.search(w)
        loc = ""
        if m:
            fe = final_idx.get(int(m.group(1)))
            if fe:
                loc = f" #{fe['index']} {_fmt_timing(fe['timing'])}"
        label = "主语误判" if w in subject_warns else "校验告警"
        items.append((label, [
            f"[{label}]{loc}",
            f"   {w}",
        ]))
    if subject_warns:
        concl.append(f"主语误判 {len(subject_warns)}")
    if other_warns:
        concl.append(f"校验告警 {len(other_warns)}")

    # 5) 时间轴未对齐（对预合并后期望时间轴）
    misaligned = [e for e in final_entries
                  if _timing_span(e["timing"])[0] >= 0
                  and _timing_span(e["timing"]) not in expected_by_span]
    for e in misaligned:
        span = _timing_span(e["timing"])
        exp_hits = [x for x in expected
                    if _timing_span(x["timing"])[0] == span[0]]
        exp_timing = _fmt_timing(exp_hits[0]["timing"]) if exp_hits \
            else "（无同起点期望条目）"
        items.append(("未对齐", [
            f"[时间轴未对齐] #{e['index']} 期望 {exp_timing} / "
            f"实际 {_fmt_timing(e['timing'])}",
            f"   译: {(e['text'] or '')[:60]}",
        ]))
    if misaligned:
        concl.append(f"未对齐 {len(misaligned)}")

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    aligned = sum(1 for e in final_entries
                  if _timing_span(e["timing"]) in expected_by_span)
    align_rate = (aligned / len(final_entries) * 100) if final_entries else 0.0
    miss_rate = (missed_total / len(kanji_orig) * 100) if kanji_orig else 0.0

    # 长度比分布（对期望条目）
    ratios = []
    for e in expected:
        span = _timing_span(e["timing"])
        o = _span_text(span)
        s = (e["text"] or "").strip()
        if s and o:
            ratios.append(_weight(o) / max(1.0, _weight(s)))
    ratios.sort()
    median_ratio = ratios[len(ratios) // 2] if ratios else 0.0
    outlier_rate = (sum(1 for r in ratios if r < 0.3) / len(ratios) * 100) \
        if ratios else 0.0

    # 敏感词行保留率（过度净化检测）
    sens_total = sens_kept = 0
    for e in expected:
        s = e["text"] or ""
        if not any(w in s for w in _SENSITIVE_HINTS):
            continue
        sens_total += 1
        if any(w in _span_text(_timing_span(e["timing"]))
               for w in _SENSITIVE_HINTS):
            sens_kept += 1
    sens_rate = (sens_kept / sens_total * 100) if sens_total else -1

    dash_leak = sum(1 for e in final_entries
                    if _DASH_RE.match(e["text"] or ""))

    # 条目统计行（预合并条数来自 merge_stats）
    gate0_del_total = int((gate0_deletions or {}).get("total", 0) or 0)
    # 原文总数口径：闸门0 前（入参优先；缺省按 闸门0后 + 闸门0删除
    # + 预合并合并 推算，两条统计行保持同一口径）
    n_src = int(orig_total) if orig_total else (
        len(orig_entries) + gate0_del_total + premerged)
    if premerged or expected_entries is not None:
        entry_line = (f"条目: 原文 {n_src} → 预合并后 {len(expected)} → "
                      f"终稿 {len(final_entries)}（预合并合并 {premerged} 处）")
    else:
        entry_line = f"条目: 原文 {n_src} → 终稿 {len(final_entries)}"

    # 规则清洗统计行（如实拆分：合并/删除分开呈现，clean_merged 键缺失
    # 视为 cleaner 未运行）
    clean_deleted = merge_stats.get("clean_deleted")
    clean_kept = merge_stats.get("clean_kept_by_evidence")
    clean_by_rule = merge_stats.get("clean_deleted_by_rule") or {}
    if clean_merged is not None:
        clean_line = (f"规则清洗: 合并 {clean_merged} · "
                      f"删除 {int(clean_deleted or 0)}"
                      f"（源侧证据免删 {int(clean_kept or 0)}）")
        top_rules = sorted(clean_by_rule.items(),
                           key=lambda kv: -kv[1])[:_RULE_DIST_LIMIT]
        if top_rules:
            clean_line += "｜主要删除规则: " + " ".join(
                f"{k}×{v}" for k, v in top_rules)
    else:
        profile_label = {"cloud": "lenient", "local": "strict"}.get(
            str(profile or "").strip().lower())
        clean_line = ("规则清洗: 未运行（lenient 档）" if profile_label == "lenient"
                      else "规则清洗: 未运行"
                           + (f"（{profile_label} 档）" if profile_label else ""))

    # v1.2.2 C2：纯假名实义保留统计行（噪声闸门免删条目）。N=免删条数；
    # M=这些保留条目中终稿文本带 [未翻译] 前缀的数量（按时间轴对回终稿
    # 统计，对不齐计 0）。键缺失（规则清洗未运行/旧调用方）不显示该行。
    noise_line = None
    if "clean_kept_by_noise_gate" in merge_stats:
        n_noise_kept = int(merge_stats.get("clean_kept_by_noise_gate", 0) or 0)
        m_marked = 0
        for t in merge_stats.get("clean_kept_by_noise_gate_timings") or []:
            hits = final_by_span.get(_timing_span(t)) or []
            if hits and (hits[0].get("text") or "").startswith("[未翻译]"):
                m_marked += 1
        noise_line = (f"纯假名实义保留: {n_noise_kept}"
                      f"（其中 [未翻译] 标记 {m_marked}）")

    # v1.2.2 D2：TM 摘要行（H=阶段A 精确命中数、L=本次学习入库数；
    # 两侧均取不到时整行显示"无样本"）
    if tm_exact_hits is None and tm_learned_count is None:
        tm_line = "TM: 无样本"
    else:
        h_show = f"{tm_exact_hits} 条" if tm_exact_hits is not None else "无样本"
        l_show = (f"{tm_learned_count} 条" if tm_learned_count is not None
                  else "无样本")
        tm_line = f"TM: 精确命中 {h_show} | 本次学习入库 {l_show}"

    # 条数核对恒等式：原文N = 闸门0删除D + 预合并合并M + 规则清洗合并C
    #                + 规则清洗删除E + 隔离区移出Q + 终稿F
    #                （各项定义见 docstring；与条目行的"原文"同口径，
    #                直接复用 n_src。Q 无隔离时不显示——避免常态报告被
    #                "+0"噪音拉长，见 docstring"隔离区移出 Q"条目）
    c_clean = int(clean_merged) if clean_merged is not None else 0
    e_clean = int(clean_deleted or 0)
    q_quar = int(merge_stats.get("quarantine_moved", 0) or 0)
    f_final = len(final_entries)
    rhs_terms = (f"闸门0删除 {gate0_del_total} + 预合并合并 {premerged}"
                 f" + 规则清洗合并 {c_clean} + 规则清洗删除 {e_clean}"
                 + (f" + 隔离区移出 {q_quar}" if q_quar else "")
                 + f" + 终稿 {f_final}")
    rhs_total = (gate0_del_total + premerged + c_clean + e_clean
                 + q_quar + f_final)
    if n_src == rhs_total:
        check_line = f"条数核对: ✅ 原文 {n_src} = {rhs_terms}"
    else:
        check_line = (f"条数核对: ⚠️ 不平（原文 {n_src} ≠ "
                      f"右侧合计 {rhs_total}）：{rhs_terms}"
                      f"（存在未入账的条目减少，请人工核查）")

    # ------------------------------------------------------------------
    # 组装报告
    # ------------------------------------------------------------------
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 60,
        "质量报告",
        f"来源: {source_name} | 时间: {now_str} | ",
        "=" * 60,
    ]
    if items:
        lines.append(f"【结论】⚠️ 需人工复核 {len(items)} 处：{' · '.join(concl)}")
    else:
        lines.append("【结论】✅ 通过，无待复核项")
    # 白话导读区（恒有，两分支）：紧随【结论】行、分隔线之前；
    # 数字全部与本次组装同源（len(items)/n_src/rhs_total/missed_total/
    # untranslated），不另起口径。
    lines.extend(_render_plain_guide(
        items_count=len(items),
        ts=now_str,
        n_src=n_src,
        rhs_total=rhs_total,
        f_final=f_final,
        missed_total=missed_total,
        untranslated_count=len(untranslated),
    ))
    lines.append("-" * 60)
    if items:
        lines.append("【需复核清单】")
        for i, (_, block) in enumerate(items, 1):
            lines.append(f"{i}. " + block[0])
            lines.extend(block[1:])
        if missed_total > _MISS_LIST_LIMIT:
            lines.append(
                f"（漏覆盖其余 {missed_total - _MISS_LIST_LIMIT} 条略）")
        lines.append("-" * 60)
    # [未翻译] 小节（D1：带前缀保留在终稿；逐条列出，上限 20 条）
    if untranslated:
        lines.append(f"【未翻译】共 {len(untranslated)} 条"
                     f"（原文以 [未翻译] 前缀保留在终稿，翻译失败未删除）")
        lines.extend(untranslated_lines[:_UNTRANSLATED_LIST_LIMIT])
        if len(untranslated) > _UNTRANSLATED_LIST_LIMIT:
            lines.append(f"（其余 {len(untranslated) - _UNTRANSLATED_LIST_LIMIT}"
                         f" 条略）")
        lines.append("-" * 60)
    # 处置章节：闸门0 送翻前删除台账（gate0_deletions 未提供时整节省略）
    disposal_text = render_disposal_section(gate0_deletions)
    if disposal_text:
        lines.extend(disposal_text.splitlines())
        lines.append("-" * 60)
    # 乱码强译复核章节（D5）：置于【处置】之后（garble_review 未提供时
    # 整节省略），渲染逻辑提为纯函数 render_garble_review_section
    garble_text = render_garble_review_section(garble_review)
    if garble_text:
        lines.extend(garble_text.splitlines())
        lines.append("-" * 60)
    # 误听疑似改写章节（v1.2.2 C1 改写留痕）：置于【乱码强译复核】之后
    # （sidecar_review 未提供时整节省略），渲染逻辑提为纯函数
    # render_mishear_review_section
    mishear_text = render_mishear_review_section(sidecar_review)
    if mishear_text:
        lines.extend(mishear_text.splitlines())
        lines.append("-" * 60)
    # 术语冲突观察小节（v1.2.2 D1 观察闸）：置于【误听疑似改写】之后
    # （glossary_conflicts 未提供时整节省略），渲染逻辑提为纯函数
    # render_conflict_section
    conflict_text = render_conflict_section(glossary_conflicts,
                                            conflict_watch_advice)
    if conflict_text:
        lines.extend(conflict_text.splitlines())
        lines.append("-" * 60)
    # 术语一致性章节（v1.2.2 D2）：置于【术语冲突观察】之后
    # （term_consistency 未提供时整节省略），渲染逻辑提为纯函数
    # render_term_consistency_section
    consistency_text = render_term_consistency_section(term_consistency)
    if consistency_text:
        lines.extend(consistency_text.splitlines())
        lines.append("-" * 60)
    # 双引擎分歧章节：无条件输出（pass_mode 决定完整/降级展示），
    # 渲染逻辑提为纯函数 render_disagreement_section（离线重算脚本复用）
    lines.extend(render_disagreement_section(pass_disagreement,
                                             pass_mode).splitlines())
    lines.append("【统计】")
    lines.append(entry_line)
    lines.append(check_line)
    lines.append(clean_line)
    if noise_line:
        lines.append(noise_line)
    lines.append(tm_line)
    lines.append(f"时间轴对齐率（对预合并后期望时间轴）: {align_rate:.1f}%"
                 f"（未对齐 {len(misaligned)} 条"
                 f"{'，见清单' if misaligned else ''}）")
    miss_line = (f"实义内容漏覆盖: {missed_total}/{len(kanji_orig)} "
                 f"({miss_rate:.1f}%)")
    if missed_total:
        miss_line += (f"（整条缺失 {len(missed)}"
                      f" + 条目在但未译 {len(missed_untranslated)}）")
    lines.append(miss_line)
    lines.append(f"长度比中位数: {median_ratio:.2f}"
                 f"（离群<0.3 占比 {outlier_rate:.1f}%）")
    lines.append(
        f"敏感词行保留率: {sens_rate:.1f}%（{sens_kept}/{sens_total}）"
         if sens_total > 0 else "敏感词行保留率: 无样本")
    lines.append(f"[未翻译] 残留: {len(untranslated)} 条 | "
                 f"对话标记泄漏: {dash_leak} 条")
    lines.append("=" * 60)
    # 组装层统一后处理：章节标题后插入白话注解行（render_* 纯函数
    # 不感知；离线报告 tools/recompute_divergence.py 与 render 直调
    # 不受影响——离线报告无这些标题自然无注解）。
    lines = _apply_section_notes(lines)
    # 导读 json 采集（W1a）：guide_sink 由调用方传入时，把白话导读的
    # 同源数据快照写入 sink（返回类型与返回值不变——不传 sink 时此处
    # 逐字节等价于原实现，直调方零影响）。
    if guide_sink is not None:
        conclusions = [
            (f"总体：需人工复核 {len(items)} 处，逐条见复核清单"
             if items else "总体：未发现需人工复核的条目，终稿可直接使用"),
            (f"条目链路：原文 {n_src} 条 → 终稿 {f_final} 条，"
             + ("条数核对已平衡" if n_src == rhs_total else "条数核对不平"))
        ]
        if missed_total:
            conclusions.append(f"漏覆盖：实义内容漏覆盖 {missed_total} 条")
        if len(untranslated):
            conclusions.append(f"未翻译残留：{len(untranslated)} 条以占位"
                               "形式保留，可人工补译")
        conclusions.append("阅读顺序：先看白话导读 → 再逐条看复核清单 → "
                           "最后用统计区核对总数")
        known_titles = {prefix for prefix, _ in _SECTION_NOTES}
        sections: list = []
        for ln in lines:
            if not ln.startswith("【"):
                continue
            hit_prefix = next((p for p in known_titles
                               if ln.startswith(p)), None)
            if hit_prefix and all(s["title"] != hit_prefix for s in sections):
                sections.append({"title": hit_prefix,
                                 "note": dict(_SECTION_NOTES)[hit_prefix]})
        # 导读 items[]（version 2，D11）：机器可消费的结构化复核条目。
        # current_text=None 即"不可自动重翻"（终稿无该 index 的官方块，
        # 如被并/被删/被隔离移出），不另设字段。txt 渲染路径不感知本段。
        src_by_idx: dict = {}
        for e in expected:
            src_by_idx.setdefault(e.get("index"), e)   # 同 index 取列表首个
        guide_items: list[dict] = []
        # 来源 A：post_validate 结构化告警（None 则空，additive）
        for sw in (structured_warnings or []):
            sw_idx = sw.get("index")
            fe = resolve_final_block(final_entries, sw_idx)
            se = src_by_idx.get(sw_idx)
            guide_items.append({
                "index": sw_idx,
                "timing": sw.get("timing") or "",
                "category": sw.get("category") or "",
                "message": sw.get("message") or "",
                "current_text": (fe or {}).get("text"),
                "source_excerpt": ((se or {}).get("text") or "")[:40],
                "status": "open",
                "severity": None,
            })
        # 来源 B：untranslated 条目全量纳入（不设 20 上限；txt 小节的
        # 列举上限不约束此处）
        for e in untranslated:
            se = src_by_idx.get(e.get("index"))
            guide_items.append({
                "index": e.get("index"),
                "timing": e.get("timing") or "",
                "category": "untranslated",
                "message": "整段未翻译",
                "current_text": e.get("text") or "",   # 终稿块全文（含前缀）
                "source_excerpt": ((se or {}).get("text") or "")[:40],
                "status": "open",
                "severity": None,
            })
        # index 升序稳定排序（None 防御：无 index 的排末尾）
        guide_items.sort(key=lambda it: (it["index"] is None,
                                         it["index"] if it["index"] is not None else 0))
        guide_sink.update({
            "version": 2,
            "source": source_name,
            "generated_at": now_str,
            "basis": "基于本次运行",
            "conclusions": conclusions,
            "sections": sections,
            "extras": {
                "对齐率": f"{align_rate:.1f}%",
                "漏覆盖率": f"{miss_rate:.1f}%",
            },
            "items": guide_items,
        })
    return "\n".join(lines)


def write_quality_report(out_dir: str, stem: str, report: str) -> str:
    """报告落盘到输出目录，返回路径。"""
    p = Path(out_dir) / f"{stem}_质量报告.txt"
    p.write_text(report + "\n", encoding="utf-8")
    return str(p)


def write_guide_json(out_dir: str, stem: str, guide: dict) -> str:
    """白话导读 json（{stem}_质量报告导读.json）落盘，返回路径。

    guide 为空 dict（报告构建时未采集到快照/旧调用方）时不落盘，
    返回空串。写前补全 stem 与伴生成品存在性表（companions）；
    文件名与 pipeline_v2 的陈旧清理/备份表双钉（契约测试防漂移）。
    """
    if not guide:
        return ""
    guide["stem"] = stem
    guide["companions"] = {
        f"{stem}{suffix}": (Path(out_dir) / f"{stem}{suffix}").is_file()
        for suffix in ("_final_cn.srt", "_质量报告.txt", "_分歧复核.csv",
                       "_风险清单.md", "_风险清单.json",
                       "_术语冲突观察.csv")
    }
    p = Path(out_dir) / f"{stem}_质量报告导读.json"
    p.write_text(json.dumps(guide, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return str(p)
