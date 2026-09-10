"""
双引擎分歧离线重算与阈值矩阵验收
================================
对已产出的 ``*.merged.subtransjav.srt`` 批量离线重算双引擎分歧（从 SRT 现算，
不读历史 JSON），并按 160 行人工标注验收伪影判定阈值：

1. 标注匹配：标注行（sample160.txt + labeled_sample.json，只读）按
   "文件 stem 前 10 字符" 分桶 + 时间轴起点 ±0.5s 匹配新版分歧行；
2. 匹配率 <90%：写未匹配清单并以非零码退出，不跑指标；
3. 阈值矩阵：iou × offset 共 16 组组合，用 judge_artifact 重算
   A/B 存活率与 C 清除率，写 matrix.txt 对照表；
4. 每部写离线版质量报告（复用 render_disagreement_section）与
   分歧复核 CSV（复用 write_divergence_review_csv）。

安全约束：
- 所有输出只进 ``{out_root}/run_YYYYMMDD_HHMMSS/``，绝不写入字幕目录；
- .analysis_tmp 下既有旧文件（divergence_all.json / labeled_sample.json /
  sample160.txt）只读，绝不修改。

用法：
    python tools/recompute_divergence.py                  # 全量（--input-dir 递归）
    python tools/recompute_divergence.py --files a.srt b.srt
"""

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine.pass_disagreement import (
    IOU_THRESHOLD,
    OFFSET_THRESHOLD,
    REVIEW_OPTIONAL_THRESHOLD,
    REVIEW_STRICT_THRESHOLD,
    _timing_span,
    collect_disagreement,
    judge_artifact,
    probe_disagreement_mode,
)
from subtransjav.refine.filters import parse_srt
from subtransjav.refine.quality_report import (
    _weight,
    render_disagreement_section,
    write_divergence_review_csv,
)

# 标注数据（只读，位于 .analysis_tmp）
_SAMPLE160 = "sample160.txt"
_LABELED_JSON = "labeled_sample.json"

# 阈值矩阵扫描网格
_IOU_GRID = (0.20, 0.25, 0.30, 0.35)
_OFFSET_GRID = (1.5, 2.0, 2.5, 3.0)

# 标注匹配参数
_MATCH_TOLERANCE_SEC = 0.5      # 时间轴起点匹配容差（秒）
_MATCH_RATE_PASS = 0.90         # 匹配率及格线

# 匹配不到新行时未匹配清单中 P1/P2 摘要截断长度
_SUMMARY_CLIP = 30


def _read_text(path: Path) -> str:
    """容错读文本（utf-8-sig → utf-8 → gbk）。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _file_prefix(srt_path: Path) -> str:
    """新文件 stem（去掉 .ja.merged.subtransjav.srt）前 10 字符（分桶键）。"""
    stem = srt_path.name
    if stem.lower().endswith(".srt"):
        stem = stem[:-4]
    suffix = ".ja.merged.subtransjav"
    if stem.endswith(suffix):
        stem = stem[:-len(suffix)]
    return stem[:10]


def _hms_to_sec(s: str) -> float:
    """``HH:MM:SS``（截断到秒）转秒数；失败返回 -1.0。"""
    parts = (s or "").strip().split(":")
    if len(parts) != 3:
        return -1.0
    try:
        h, m, sec = (int(x) for x in parts)
    except ValueError:
        return -1.0
    return h * 3600 + m * 60 + sec


def load_labels(analysis_tmp: Path) -> list:
    """加载 160 行人工标注（sample160.txt 时间 + labeled_sample.json label）。

    返回 [{idx, prefix, sim, start_sec, p1, p2, label}]。
    """
    labels = {}
    jpath = analysis_tmp / _LABELED_JSON
    if jpath.is_file():
        data = json.loads(_read_text(jpath))
        for item in data:
            try:
                labels[int(item["idx"])] = str(item.get("label", "")).strip()
            except (KeyError, TypeError, ValueError):
                continue

    rows = []
    tpath = analysis_tmp / _SAMPLE160
    for line in _read_text(tpath).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 5)
        if len(parts) != 6:
            continue
        idx_s, prefix, sim_s, t_s, p1, p2 = parts
        try:
            idx = int(idx_s)
            sim = float(sim_s)
        except ValueError:
            continue
        # P1:/P2: 前缀剥掉；时间截断到秒
        p1 = p1[3:] if p1.startswith("P1:") else p1
        p2 = p2[3:] if p2.startswith("P2:") else p2
        rows.append({
            "idx": idx,
            "prefix": prefix,
            "sim": sim,
            "start_sec": _hms_to_sec(t_s),
            "old_time": t_s,
            "p1": p1,
            "p2": p2,
            "label": labels.get(idx, ""),
        })
    return rows


def _new_start(row: dict) -> float:
    return _timing_span(row.get("timing", ""))[0]


def match_labels(labels: list, rows_by_prefix: dict) -> tuple[list, list]:
    """标注行 ↔ 新分歧行匹配。

    匹配规则：同前缀桶内，|新行起点 - 旧标注起点| ≤ 0.5s，多个取最近。
    补偿规则：sample160 的旧时间为"截断到秒"（向下取整），与真实起点最多差
    1.0s，故 floor(新行起点) == 旧标注起点 也视为命中（确定性等价）。
    返回 (matched, unmatched)：matched 元素为 (label_row, new_row)。
    """
    matched, unmatched = [], []
    for lb in labels:
        bucket = rows_by_prefix.get(lb["prefix"], [])
        best, best_diff = None, None
        for nr in bucket:
            new_start = _new_start(nr)
            diff = abs(new_start - lb["start_sec"])
            hit = diff <= _MATCH_TOLERANCE_SEC or \
                math.floor(new_start) == lb["start_sec"]
            if hit and (best_diff is None or diff < best_diff):
                best, best_diff = nr, diff
        if best is None:
            unmatched.append(lb)
        else:
            matched.append((lb, best))
    return matched, unmatched


def _fmt_pct(n: int, d: int) -> str:
    return f"{(n / d * 100):.1f}% ({n}/{d})" if d else "N/A (0/0)"


def write_matrix(path: Path, matched: list) -> None:
    """阈值矩阵：16 组 (iou, offset) 组合的 A/B/AB 存活率与 C 清除率。"""
    groups = {"A": [], "B": [], "C": []}
    for lb, nr in matched:
        label = lb["label"]
        if label in groups:
            groups[label].append(nr)

    lines = [
        "阈值矩阵（judge_artifact 对已匹配标注行重算；存活 = 判为非伪影）",
        f"标注行数：A={len(groups['A'])} B={len(groups['B'])} C={len(groups['C'])}",
        f"生产默认：IOU_THRESHOLD={IOU_THRESHOLD} OFFSET_THRESHOLD={OFFSET_THRESHOLD}",
        "-" * 88,
    ]
    ab_all = groups["A"] + groups["B"]
    for iou_thr in _IOU_GRID:
        for off_thr in _OFFSET_GRID:
            mark = "  <=生产默认" if (iou_thr, off_thr) == \
                (IOU_THRESHOLD, OFFSET_THRESHOLD) else ""
            cells = []
            for name, pool in (("A", groups["A"]), ("B", groups["B"]),
                               ("A+B", ab_all), ("C", groups["C"])):
                if not pool:
                    cells.append(f"{name}存活 N/A")
                    continue
                alive = sum(1 for r in pool
                            if not judge_artifact(r.get("iou", 0.0),
                                                  r.get("offset", 0.0),
                                                  r.get("pass1", ""),
                                                  r.get("pass2", ""),
                                                  iou_thr, off_thr))
                if name == "C":
                    cleared = len(pool) - alive
                    cells.append(f"C清除 {_fmt_pct(cleared, len(pool))}")
                else:
                    cells.append(f"{name}存活 {_fmt_pct(alive, len(pool))}")
            lines.append(f"iou={iou_thr:.2f} offset={off_thr:.1f} | "
                         + " | ".join(cells) + mark)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bucket_stats(rows: list) -> dict:
    """区间分布（过滤前各桶行数与其中伪影数）。"""
    strict = strict_art = mid = mid_art = high = high_art = 0
    for r in rows:
        sim = r.get("similarity", 0.0)
        art = bool(r.get("artifact"))
        if sim < REVIEW_STRICT_THRESHOLD:
            strict += 1
            strict_art += art
        elif sim < REVIEW_OPTIONAL_THRESHOLD:
            mid += 1
            mid_art += art
        else:
            high += 1
            high_art += art
    return {
        "strict": strict, "strict_art": strict_art,
        "mid": mid, "mid_art": mid_art,
        "high": high, "high_art": high_art,
        "artifact": strict_art + mid_art + high_art,
    }


def _stats_block(title: str, st: dict) -> list:
    """单个统计块的固定格式行（过滤前/后）。"""
    artifact = st["artifact"]
    total = st["strict"] + st["mid"] + st["high"]
    return [
        f"{title}：分歧行 {total}（伪影 {artifact}）",
        f"  sim<{REVIEW_STRICT_THRESHOLD:.2f}：{st['strict']} 行"
        f"（伪影 {st['strict_art']}，过滤后 {st['strict'] - st['strict_art']}）",
        f"  {REVIEW_STRICT_THRESHOLD:.2f}~{REVIEW_OPTIONAL_THRESHOLD:.2f}："
        f"{st['mid']} 行（伪影 {st['mid_art']}，过滤后 "
        f"{st['mid'] - st['mid_art']}）",
        f"  ≥{REVIEW_OPTIONAL_THRESHOLD:.2f}：{st['high']} 行"
        f"（伪影 {st['high_art']}，过滤后 {st['high'] - st['high_art']}）",
    ]


def _offline_recompute_stats(merged_entries: list, final_entries: list) -> list:
    """离线可重算统计（从 merged 与 final_cn 现算）。

    口径与 build_quality_report 一致：对齐率/漏覆盖以 merged 为期望基准，
    长度比 = 终稿字重 / 源文字重。
    """
    merged_spans = {}
    for e in merged_entries:
        span = _timing_span(e["timing"])
        if span[0] >= 0:
            merged_spans.setdefault(span, []).append(e)
    final_spans = {}
    for e in final_entries:
        span = _timing_span(e["timing"])
        if span[0] >= 0:
            final_spans.setdefault(span, []).append(e)

    def _final_text(span):
        return "".join((x["text"] or "").strip()
                       for x in final_spans.get(span, []))

    aligned = sum(1 for e in final_entries
                  if _timing_span(e["timing"]) in merged_spans)
    align_rate = aligned / len(final_entries) * 100 if final_entries else 0.0

    kanji = [e for e in merged_entries
             if any("\u4e00" <= c <= "\u9fff" or c == "々" for c in e["text"] or "")]
    missed = [e for e in kanji
              if not final_spans.get(_timing_span(e["timing"]))]
    miss_rate = len(missed) / len(kanji) * 100 if kanji else 0.0

    ratios = []
    for e in merged_entries:
        span = _timing_span(e["timing"])
        o = _final_text(span)
        s = (e["text"] or "").strip()
        if s and o:
            ratios.append(_weight(o) / max(1.0, _weight(s)))
    ratios.sort()
    median_ratio = ratios[len(ratios) // 2] if ratios else 0.0
    outlier = sum(1 for r in ratios if r < 0.3) / len(ratios) * 100 \
        if ratios else 0.0

    return [
        f"时间轴对齐率（对 merged 原始行）: {align_rate:.1f}%"
        f"（未对齐 {len(final_entries) - aligned} 条）",
        f"实义内容漏覆盖: {len(missed)}/{len(kanji)} ({miss_rate:.1f}%)",
        f"长度比中位数: {median_ratio:.2f}"
        f"（离群<0.3 占比 {outlier:.1f}%）",
    ]


def write_offline_report(out_path: Path, srt_path: Path, mode: str,
                         disag: dict | None, merged_entries: list,
                         final_entries: list | None) -> None:
    """单部离线版质量报告。"""
    n_merged = len(merged_entries)
    lines = [
        "=" * 60,
        "离线质量报告（离线重算版）",
        f"来源: {srt_path.name} | "
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        "离线重算版",
        "=" * 60,
    ]
    lines.extend(render_disagreement_section(disag, mode).splitlines())
    lines.append("-" * 60)
    lines.append("【离线可重算统计】")
    if final_entries is not None:
        lines.extend(_offline_recompute_stats(merged_entries, final_entries))
    else:
        lines.append("（未找到对应 *_final_cn.srt，离线可重算统计不可算）")
    lines.append(f"条目行数：离线近似值（基于 merged 原始行数 {n_merged}，"
                 "非预合并后行数）")
    lines.append("-" * 60)
    lines.append("【需复核清单】离线版不含本小节（需完整翻译流程生成）")
    lines.append("【统计】敏感词行保留率/未翻译残留/对话标记泄漏："
                 "离线版不含（需完整翻译流程生成）")
    lines.append("=" * 60)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="双引擎分歧离线重算 + 阈值矩阵验收（输出只写 run_ 目录）")
    ap.add_argument("--input-dir", default="",
                    help="字幕根目录（必填，除非使用 --files；递归扫描）")
    ap.add_argument("--files", nargs="+", default=[],
                    help="仅处理指定的一个或多个 merged srt（优先于 --input-dir）")
    ap.add_argument("--out-root", default=".analysis_tmp",
                    help="输出根目录（默认当前目录下 .analysis_tmp，其下建 run_时间戳）")
    args = ap.parse_args(argv)

    out_root = Path(args.out_root)
    analysis_tmp = out_root  # 标注数据与输出同根（.analysis_tmp）

    # ---- 收集文件 ----
    if args.files:
        files = [Path(f) for f in args.files]
    else:
        root = Path(args.input_dir)
        files = sorted(root.rglob("*.merged.subtransjav.srt"))
    files = [f for f in files if f.is_file()]
    if not files:
        print("未找到任何 *.merged.subtransjav.srt 输入文件")
        return 1

    # ---- 输出目录（只写新建 run_ 目录，不碰旧文件）----
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = out_root / f"run_{ts}"
    if run_dir.exists():   # 同秒冲突兜底
        run_dir = out_root / f"run_{ts}_1"
    reports_dir = run_dir / "reports"
    csv_dir = run_dir / "csv"
    reports_dir.mkdir(parents=True, exist_ok=True)
    csv_dir.mkdir(parents=True, exist_ok=True)

    # ---- 逐文件重算 ----
    rows_by_prefix = {}
    all_rows = []
    per_file_stats = []
    for f in files:
        mode = probe_disagreement_mode(str(f))
        disag = collect_disagreement(str(f)) if mode == "dual" else None
        rows = (disag or {}).get("rows") or []
        for r in rows:
            rows_by_prefix.setdefault(_file_prefix(f), []).append(r)
        all_rows.extend(rows)

        merged_entries = parse_srt(_read_text(f))
        final_path = f.with_name(f"{f.stem}_final_cn.srt")
        final_entries = None
        if final_path.is_file():
            final_entries = parse_srt(_read_text(final_path))

        stem = f.stem  # 与正式报告同名（{stem}_质量报告.txt）
        write_offline_report(reports_dir / f"{stem}_质量报告.txt", f, mode,
                             disag, merged_entries, final_entries)
        write_divergence_review_csv(
            csv_dir / f"{stem}_分歧复核.csv", rows, f.name,
            final_entries=final_entries)

        st = _bucket_stats(rows)
        st["name"] = f.name
        st["mode"] = mode
        per_file_stats.append(st)
        print(f"[{mode}] {f.name}：对照 "
              f"{(disag or {}).get('matched', 0)}/{(disag or {}).get('total', len(merged_entries))}"
              f"，分歧行 {len(rows)}")

    # ---- stats.txt ----
    stats_lines = ["双引擎分歧区间分布（过滤前/后）", "=" * 60]
    for st in per_file_stats:
        stats_lines.extend(_stats_block(f"[{st['mode']}] {st['name']}", st))
    stats_lines.append("=" * 60)
    stats_lines.append("全量汇总")
    stats_lines.extend(_stats_block("全量", _bucket_stats(all_rows)))
    (run_dir / "stats.txt").write_text("\n".join(stats_lines) + "\n",
                                       encoding="utf-8")

    # ---- 标注匹配与验收 ----
    labels = load_labels(analysis_tmp)
    if args.files:
        prefixes = {_file_prefix(f) for f in files}
        labels = [lb for lb in labels if lb["prefix"] in prefixes]
    matched, unmatched = match_labels(labels, rows_by_prefix)
    rate = len(matched) / len(labels) if labels else 0.0

    mr = [f"标注匹配率：{len(matched)}/{len(labels)} = {rate * 100:.1f}%"
          f"（及格线 {_MATCH_RATE_PASS * 100:.0f}%）",
          f"输入文件数：{len(files)}"
          f"{'（--files 模式，仅统计涉及文件的标注行）' if args.files else ''}"]
    if unmatched:
        mr.append("-" * 60)
        mr.append(f"未匹配标注行 {len(unmatched)} 条：")
        for lb in unmatched:
            bucket = rows_by_prefix.get(lb["prefix"], [])
            nearest, ndiff = None, None
            for nr in bucket:
                diff = abs(_new_start(nr) - lb["start_sec"])
                if ndiff is None or diff < ndiff:
                    nearest, ndiff = nr, diff
            near = (f"桶内最近新行起点 "
                    f"{_timing_span(nearest['timing'])[0]:.3f}s"
                    f"（差 {ndiff:.3f}s）" if nearest else "无")
            mr.append(
                f"  idx={lb['idx']} 文件前缀={lb['prefix']} "
                f"旧时间={lb['old_time']} "
                f"P1={lb['p1'][:_SUMMARY_CLIP]} P2={lb['p2'][:_SUMMARY_CLIP]} "
                f"附近：{near}")
    (run_dir / "match_report.txt").write_text("\n".join(mr) + "\n",
                                              encoding="utf-8")

    if rate < _MATCH_RATE_PASS:
        print(f"❌ 标注匹配率 {rate * 100:.1f}% 低于及格线 "
              f"{_MATCH_RATE_PASS * 100:.0f}%，不跑阈值矩阵。"
              f"未匹配清单见 {run_dir / 'match_report.txt'}")
        print(f"输出目录: {run_dir}")
        return 1

    # ---- 阈值矩阵（匹配率达标才跑）----
    write_matrix(run_dir / "matrix.txt", matched)
    print(f"✅ 匹配率 {rate * 100:.1f}%，阈值矩阵已写入 matrix.txt")

    print(f"输出目录: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
