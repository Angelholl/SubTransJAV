"""
质量报告（人工复核工单）
========================
每次 v2 任务结束后对 终稿 vs 预合并后期望时间轴 生成"复核工单"式报告
并落盘，供无人值守流程结束后快速定位需要人工介入的具体条目。

设计原则：
- 对齐率以 预合并后期望时间轴 为基准（原始 span 集合会因预合并/
  规则清洗天然对不上，产生 97-98% 的噪声）；
- 假名残留逐条列出（仅陈述事实，不做幻觉/未译断言，交人工确认）；
- 实义漏覆盖逐条列出（无门槛，上限 20 条）；
- post_validate 告警逐条进入复核清单，warn_only=false 的硬性告警一并呈现。
"""

import csv
import re
from datetime import datetime
from pathlib import Path

from .pass_disagreement import (
    REVIEW_OPTIONAL_THRESHOLD,
    REVIEW_STRICT_THRESHOLD,
    _timing_span as _disagree_span,
)

_KANA_RE = re.compile(r"[ぁ-ゖァ-ヺー]")
_KANJI_RE = re.compile(r"[一-鿿々]")
_KANA_SEQ_RE = re.compile(r"[ぁ-ゖァ-ヺー]{2,}")   # 假名连续串（残留分类用）
_DASH_RE = re.compile(r"^\s*-\s")
_IDX_IN_WARNING_RE = re.compile(r"#(\d+)")

# 敏感词提示集（过度净化检测用；与角色卡分级表非一一对应，仅做存在性检查）
_SENSITIVE_HINTS = (
    "小穴", "肉棒", "插入", "高潮", "射", "爱液", "骚", "鸡巴", "舔",
    "阴", "奶", "淫", "穴", "精液",
)

# 漏覆盖清单上限（超出注明"其余 N 条略"）
_MISS_LIST_LIMIT = 20


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


def build_quality_report(orig_entries: list, final_entries: list,
                         source_name: str = "",
                         expected_entries: list | None = None,
                         merge_stats: dict | None = None,
                         validator_warnings: list | None = None,
                         pass_disagreement: dict | None = None,
                         pass_mode: str = "dual") -> str:
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
        合并统计，如 {"premerge_merged": 154, "clean_merged": 12}；
        缺省键按 0 处理，clean_merged 键缺失时统计行省略该段。
    validator_warnings : list | None
        post_validate 返回的告警字符串列表，逐条进入复核清单。
    pass_mode : str
        双引擎分歧模式：dual | missing_pass1 | missing_pass2 | none
        （由 pass_disagreement.probe_disagreement_mode 探测）。
    """
    from .pipeline_v2 import _timing_span

    expected = expected_entries if expected_entries is not None else orig_entries
    merge_stats = merge_stats or {}
    validator_warnings = validator_warnings or []
    premerged = merge_stats.get("premerge_merged", 0)
    clean_merged = merge_stats.get("clean_merged")

    # span → 条目列表 映射（同一时间轴可能多条，不能用单值 dict）
    def _index_by_span(entries: list) -> dict:
        d = {}
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

    # 1) 假名残留逐条列出（[未翻译] 条目单独列，避免重复计数；
    #    残留成因难自动判定，仅陈述事实交人工确认）
    kana_total = 0
    for e in final_entries:
        text = (e["text"] or "").strip()
        if not text or text.startswith("[未翻译]"):
            continue
        span = _timing_span(e["timing"])
        src = _expected_src(span)
        for seq in _KANA_SEQ_RE.findall(text):
            kana_total += 1
            src_show = src if src else "（无对应期望条目）"
            src_note = "含相同串" if seq in src else "中未出现该串"
            items.append(("假名残留", [
                f"[假名残留·需人工确认] #{e['index']} {_fmt_timing(e['timing'])}",
                f"   源: {src_show[:60]}",
                f"   译: {text[:60]}",
                f"   残留假名串: 「{seq}」（终稿该条仍含假名串；源文对应条目{src_note}）",
            ]))
    if kana_total:
        concl.append(f"假名残留 {kana_total}")

    # 2) [未翻译] 残留
    untranslated = [e for e in final_entries
                    if (e["text"] or "").startswith("[未翻译]")]
    for e in untranslated:
        span = _timing_span(e["timing"])
        src = _expected_src(span)
        items.append(("未翻译", [
            f"[未翻译] #{e['index']} {_fmt_timing(e['timing'])}",
            f"   源: {src[:60] if src else '（无对应期望条目）'}",
            f"   译: {(e['text'] or '')[:60]}",
        ]))
    if untranslated:
        concl.append(f"未翻译 {len(untranslated)}")

    # 3) 实义内容漏覆盖（含汉字原文行，终稿无对应条目或译文为空；
    #    无门槛全部列出，上限 _MISS_LIST_LIMIT 条）
    kanji_orig = [e for e in expected if _KANJI_RE.search(e["text"] or "")]
    missed = [e for e in kanji_orig
              if not any(final_by_span.get(_timing_span(e["timing"])) or [])]
    for e in missed[:_MISS_LIST_LIMIT]:
        items.append(("漏覆盖", [
            f"[实义漏覆盖] #{e['index']} {_fmt_timing(e['timing'])}",
            f"   源: {(e['text'] or '')[:60]}",
            "   （终稿中无对应条目或译文为空）",
        ]))
    if missed:
        concl.append(f"漏覆盖 {len(missed)}")

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
    miss_rate = (len(missed) / len(kanji_orig) * 100) if kanji_orig else 0.0

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
    n_src = len(expected) + premerged if premerged else len(orig_entries)
    if premerged or expected_entries is not None:
        seg = f"（预合并合并 {premerged} 处"
        if clean_merged:
            seg += f" | 规则清洗合并 {clean_merged} 处"
        seg += "）"
        entry_line = (f"条目: 原文 {n_src} → 预合并后 {len(expected)} → "
                      f"终稿 {len(final_entries)} {seg}")
    else:
        entry_line = f"条目: 原文 {n_src} → 终稿 {len(final_entries)}"

    # ------------------------------------------------------------------
    # 组装报告
    # ------------------------------------------------------------------
    lines = [
        "=" * 60,
        "质量报告",
        f"来源: {source_name} | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | ",
        "=" * 60,
    ]
    if items:
        lines.append(f"【结论】⚠️ 需人工复核 {len(items)} 处：{' · '.join(concl)}")
    else:
        lines.append("【结论】✅ 通过，无待复核项")
    lines.append("-" * 60)
    if items:
        lines.append("【需复核清单】")
        for i, (_, block) in enumerate(items, 1):
            lines.append(f"{i}. " + block[0])
            lines.extend(block[1:])
        if len(missed) > _MISS_LIST_LIMIT:
            lines.append(f"（漏覆盖其余 {len(missed) - _MISS_LIST_LIMIT} 条略）")
        lines.append("-" * 60)
    # 双引擎分歧章节：无条件输出（pass_mode 决定完整/降级展示），
    # 渲染逻辑提为纯函数 render_disagreement_section（离线重算脚本复用）
    lines.extend(render_disagreement_section(pass_disagreement,
                                             pass_mode).splitlines())
    lines.append("【统计】")
    lines.append(entry_line)
    lines.append(f"时间轴对齐率（对预合并后期望时间轴）: {align_rate:.1f}%"
                 f"（未对齐 {len(misaligned)} 条"
                 f"{'，见清单' if misaligned else ''}）")
    lines.append(f"实义内容漏覆盖: {len(missed)}/{len(kanji_orig)} "
                 f"({miss_rate:.1f}%)")
    lines.append(f"长度比中位数: {median_ratio:.2f}"
                 f"（离群<0.3 占比 {outlier_rate:.1f}%）")
    lines.append(
        (f"敏感词行保留率: {sens_rate:.1f}%（{sens_kept}/{sens_total}）"
         if sens_total > 0 else "敏感词行保留率: 无样本"))
    lines.append(f"[未翻译] 残留: {len(untranslated)} 条 | "
                 f"对话标记泄漏: {dash_leak} 条")
    lines.append("=" * 60)
    return "\n".join(lines)


def write_quality_report(out_dir: str, stem: str, report: str) -> str:
    """报告落盘到输出目录，返回路径。"""
    p = Path(out_dir) / f"{stem}_质量报告.txt"
    p.write_text(report + "\n", encoding="utf-8")
    return str(p)
