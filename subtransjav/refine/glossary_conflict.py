"""
术语冲突观察闸（v1.2.2 批次 D1）
================================
先观察、不阻断：终稿生成后，对每条终稿条目取时间轴对齐的源文；
若源文命中任一 glossary 源词（≥2 字符，子串，英文忽略大小写）而译文
不含该词主译法也不含任何别名 → 记一条冲突观察。

产出三件套（数据同一次遍历，两用）：
  1. 冲突清单 conflicts —— 落盘 {stem}_术语冲突观察.csv（五元组 +
     created_at），供人工判定是否误伤；
  2. 逐术语统计 term_stats —— 质量报告【术语一致性】章节；
  3. 跨运行累计 —— Temp/translation_memory/glossary_conflict_watch.json
     追加式记录每次运行的 {date, source, per_term}，用于"转阻断"
     评估（只评估不自动切换；评估建议行进质量报告）。

误伤率口径：判定依赖人工在 CSV 上标注，系统先按 0 误伤计（全部视为
疑似真冲突）；watch JSON 每条运行记录预留 ``manual_false_positive``
字段供后续人工回填（值可为 int，或 {"count": n, "category": "人名"/
"解剖词"/"称呼"}——受保护类别出现任何误伤即不满足转阻断条件）。

纯函数 + 显式路径参数，全部可离线单测；不修改 glossary.csv 本身。
"""

import contextlib
import csv
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .config import TEMP_DIR  # noqa: E402  # 延迟导入规避循环依赖
from .pass_disagreement import (  # noqa: E402  # 与 pipeline_v2 同源实现
    _timing_span,
)

# 源词最短长度（≥2 字符才参与冲突判定，过滤单字符误命中）
MIN_TERM_LEN = 2
# 冲突观察 CSV 列（五元组 + created_at = 运行时间）
CONFLICT_CSV_COLUMNS = ["entry_id", "timing", "source_term",
                        "expected_targets", "actual_text", "created_at"]
# 跨运行观察闸 JSON 文件名（位于 Temp/translation_memory/）
WATCH_JSON_NAME = "glossary_conflict_watch.json"

# 转阻断评估阈值（只评估不自动切换；报告输出建议行）
WATCH_MIN_RUNS = 3          # 连续 3 次运行
WATCH_MIN_CANDIDATES = 100  # 最小样本护栏：连续 3 次累计候选 <100 → 样本不足
WATCH_CAND_CUTOFF = 300     # 累计 ≥300 候选样本（与运行次数二选一）
WATCH_FP_RATE_MAX = 0.02    # 误伤率 <2%
# 受保护类别（人名/解剖词/称呼类零误伤）
WATCH_PROTECTED_CATEGORIES = ("人名", "解剖词", "称呼")

# 评估建议行三态文案
ADVICE_INSUFFICIENT = "样本不足，仅观察"
ADVICE_READY = "满足转阻断条件，待用户裁决"
ADVICE_NOT_READY = "未满足转阻断条件，继续观察"


# ---------------------------------------------------------------------------
# 冲突扫描（一次遍历，冲突清单 + 逐术语统计两用）
# ---------------------------------------------------------------------------

def _term_in_text(term: str, text: str) -> bool:
    """源词命中判定：英文词条忽略大小写，其余原样子串（与 match_glossary 同款）。"""
    if term.isascii():
        return term.lower() in (text or "").lower()
    return term in (text or "")


def _align_orig_by_timing(entries: list, orig_entries: list) -> list:
    """按时间轴把 orig 条目顺序对齐到 entries（双指针，容忍同起点多条）。

    与 pipeline_v2._align_orig_by_timing 同款语义（起点相同多条按序消费；
    合并行对齐首行 orig 且不消费指针）。本地实现避免 import pipeline_v2
    （pipeline_v2 -> 本模块，反向导入即循环）。
    """
    aligned = []
    p = 0
    n = len(orig_entries)
    eps = 1e-6
    for e in entries:
        start = _timing_span(e["timing"])[0]
        span = _timing_span(e["timing"])
        while p < n and _timing_span(orig_entries[p]["timing"])[0] < start - eps:
            p += 1
        if p < n and abs(_timing_span(orig_entries[p]["timing"])[0] - start) <= eps:
            aligned.append(orig_entries[p])
            if _timing_span(orig_entries[p]["timing"]) == span:
                p += 1
        else:
            aligned.append(None)
    return aligned


def scan_glossary_conflicts(final_entries: list, orig_entries: list,
                            glossary: list) -> dict:
    """终稿 × 源文 × 词库 一次遍历：返回 {"conflicts", "term_stats"}。

    glossary 为 load_glossary_merged 的三元组列表（兼容两列——别名取
    空元组）；空词库返回两个空列表（无 glossary 场景）。

    conflicts 元素（五元组，created_at 落盘时补）：
      {"entry_id", "timing", "source_term", "expected_targets", "actual_text"}
      expected_targets = "主译法|别名1|别名2"（供人工比对豁免口径）。
    term_stats 元素（逐术语，含最多 3 条均不含样本行）：
      {"term", "target", "aliases", "hits", "with_main", "with_alias",
       "with_neither", "samples": [{"entry_id", "timing", "actual_text"}]}
    """
    conflicts: list[dict] = []
    term_stats: list[dict] = []
    if not glossary:
        return {"conflicts": conflicts, "term_stats": term_stats}
    stats_by_term = {}
    for entry in glossary:
        term, target = entry[0], entry[1]
        aliases = tuple(entry[2]) if len(entry) >= 3 else ()
        st = {"term": term, "target": target, "aliases": aliases,
              "hits": 0, "with_main": 0, "with_alias": 0, "with_neither": 0,
              "samples": []}
        stats_by_term[term] = st
        term_stats.append(st)

    expected_all = {st["term"]: "|".join((st["target"],) + st["aliases"])
                    for st in term_stats}
    for fe, oe in zip(final_entries,
                      _align_orig_by_timing(final_entries, orig_entries),
                      strict=False):
        src = (oe.get("text") or "").strip() if oe else ""
        if not src:
            continue
        text = (fe.get("text") or "").strip()
        for st in term_stats:
            term = st["term"]
            if len(term) < MIN_TERM_LEN or not _term_in_text(term, src):
                continue
            st["hits"] += 1
            if st["target"] and st["target"] in text:
                st["with_main"] += 1
            elif any(a and a in text for a in st["aliases"]):
                st["with_alias"] += 1
            else:
                st["with_neither"] += 1
                conflicts.append({
                    "entry_id": fe.get("index"),
                    "timing": fe.get("timing") or "",
                    "source_term": term,
                    "expected_targets": expected_all[term],
                    "actual_text": text[:60],
                })
                if len(st["samples"]) < 3:
                    st["samples"].append({
                        "entry_id": fe.get("index"),
                        "timing": fe.get("timing") or "",
                        "actual_text": text[:60],
                    })
    return {"conflicts": conflicts, "term_stats": term_stats}


def write_conflict_csv(out_path: str, conflicts: list) -> str:
    """冲突观察五元组落盘（utf-8-sig，Excel 直开不乱码）；created_at=落盘时间。"""
    now = datetime.now().isoformat(timespec="seconds")
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CONFLICT_CSV_COLUMNS)
        for c in conflicts:
            w.writerow([
                c.get("entry_id", ""),
                c.get("timing", ""),
                c.get("source_term", ""),
                c.get("expected_targets", ""),
                c.get("actual_text", ""),
                now,
            ])
    return str(p)


# ---------------------------------------------------------------------------
# 跨运行观察闸 JSON（追加式）
# ---------------------------------------------------------------------------

def default_watch_path() -> str:
    """观察闸 JSON 默认路径：<项目根>/Temp/translation_memory/ 下。"""
    return os.path.join(TEMP_DIR, "translation_memory", WATCH_JSON_NAME)


def load_watch_records(path: str) -> list:
    """读取观察闸记录（不存在/损坏 → 空列表，首次运行零门槛）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def append_watch_record(path: str, source: str, per_term: dict) -> dict:
    """追加一次运行的观察记录并落盘；返回写入的记录。

    per_term: {源词: {"candidates": 命中行数, "conflicts": 冲突行数}}；
    manual_false_positive 预留空对象，供人工回填误伤标注（见模块 docstring）。
    """
    record = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "per_term": {k: {"candidates": int(v.get("candidates", 0) or 0),
                         "conflicts": int(v.get("conflicts", 0) or 0)}
                     for k, v in (per_term or {}).items()},
        "manual_false_positive": {},
    }
    records = load_watch_records(path)
    records.append(record)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 原子写（同目录 .tmp + os.replace，防中断留半截 JSON）
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent),
                                    prefix=".watch", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
    return record


def evaluate_watch(records: list) -> str:
    """转阻断评估（只评估不自动切换）：返回三态建议文案。

    - 连续 3 次运行且累计候选 <100 → 样本不足（最小样本护栏）；
    - （连续 3 次运行 或 累计候选 ≥300）且 误伤率 <2% 且
      人名/解剖词/称呼类零误伤 → 满足转阻断条件，待用户裁决；
    - 其余 → 未满足，继续观察。
    误伤率 = 人工标注误伤数 / 冲突总数；未回填时按 0 计。
    """
    runs = len(records)
    candidates = 0
    conflict_total = 0
    fp_total = 0
    protected_fp = 0
    for r in records:
        for v in (r.get("per_term") or {}).values():
            candidates += int(v.get("candidates", 0) or 0)
            conflict_total += int(v.get("conflicts", 0) or 0)
        for _term, fp in (r.get("manual_false_positive") or {}).items():
            if isinstance(fp, dict):
                n = int(fp.get("count", 0) or 0)
                cat = str(fp.get("category", "") or "")
            else:
                n, cat = int(fp or 0), ""
            fp_total += n
            if cat in WATCH_PROTECTED_CATEGORIES:
                protected_fp += n
    rate = (fp_total / conflict_total) if conflict_total else 0.0
    if runs >= WATCH_MIN_RUNS and candidates < WATCH_MIN_CANDIDATES:
        return ADVICE_INSUFFICIENT
    if (runs >= WATCH_MIN_RUNS or candidates >= WATCH_CAND_CUTOFF) \
            and rate < WATCH_FP_RATE_MAX and protected_fp == 0:
        return ADVICE_READY
    return ADVICE_NOT_READY
