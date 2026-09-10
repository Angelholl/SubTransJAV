"""
双引擎分歧采集
================
从 ASR 双引擎（pass1 / pass2）产物中，对照合并产物逐条计算两条引擎
对同一时间轴片段的文本相似度，供质量报告"双引擎分歧"章节使用。

自包含模块：不 import pipeline_v2（避免循环依赖），仅复用同包内
``filters.parse_srt``。输入文件命名约定：
    {基名}.{语言码}.pass1.srt
    {基名}.{语言码}.pass2.srt
    {基名}.{语言码}.merged.subtransjav.srt
"""

import re
from difflib import SequenceMatcher
from pathlib import Path

from .filters import parse_srt

# 语言后缀（剥掉后用于重建兄弟文件名）
_LANG_RE = re.compile(r"\.(ja|japanese|zh|chinese|translated)$")
# 阶段/产物标记后缀（merged 可带可选 .subtransjav 后缀）
_MARKER_RE = re.compile(r"\.(pass1|pass2|merged(?:\.subtransjav)?)$")

# 归一化时剔除的空白与常见标点
_NORMALIZE_RE = re.compile(
    r"[\s，。、！？…!?~〜·「」『』,.!?：:；;（）()【】《》〈〉\[\]\"'“”‘’—－_／/]+"
)

# ======================================================================
# 伪影判定阈值（调参入口：直接改这里的常量即可全局生效）
# ----------------------------------------------------------------------
# IOU_THRESHOLD     时间轴重叠率（IoU=交集/并集）低于该值视为"时间轴不合"；
# OFFSET_THRESHOLD  两侧起始/结束时间差的最大绝对值（秒）超过该值视为"错位"；
# 两者同时成立才判为伪影（双条件与，避免单条件误杀）。
# REVIEW_STRICT_THRESHOLD    相似度低于该值 → 报告"必看"区；
# REVIEW_OPTIONAL_THRESHOLD  相似度处于 [strict, optional) → 报告"可选"区。
# ======================================================================
IOU_THRESHOLD = 0.20
OFFSET_THRESHOLD = 3.0
REVIEW_STRICT_THRESHOLD = 0.30
REVIEW_OPTIONAL_THRESHOLD = 0.50

# 第二级文本判据（V3，2026-09-07 实测定版）：两侧归一化后，较短侧长度
# ≤ ARTIFACT_SHORT_MAX 且 较长侧长度 ≥ ARTIFACT_LONG_MIN 且 相似度 <
# REVIEW_STRICT_THRESHOLD → 判伪影（"极短碎片 vs 实义长句"的错配行）。
# 依据：160 行标注实测 A+B 存活 95.7% / C 清除 46.5%，为唯一存活≥95%的变体。
ARTIFACT_SHORT_MAX = 3
ARTIFACT_LONG_MIN = 4

# 极简应和/感叹白名单：两侧归一化后较短一侧 ≤4 字符且精确命中其中的，
# 视为无实义伪影（判定逻辑见 judge_artifact 第二级）。
_INTERJECTION_WHITELIST = frozenset({
    "うん", "うんうん", "うんっ", "はい", "ね", "ねえ", "な", "ん",
    "ああ", "あー", "えっ", "ええ", "ううん", "ふふ", "おっ", "おお", "はぁ",
})


def _sibling_paths(in_path: str) -> tuple[Path, Path, Path]:
    """按命名约定算出 in_path 的 base/lang 与 pass1/pass2 兄弟路径。

    对文件名 stem 剥掉标记后缀（pass1/pass2/merged[.subtransjav]）与语言
    后缀，得到公共基名 base；语言码缺省 ``ja``；拼出
    ``{base}.{lang}.pass1.srt`` 与 ``{base}.{lang}.pass2.srt``。
    只拼路径不检查存在性。解析失败抛异常，由调用方兜底。
    """
    p = Path(in_path)
    stem = p.stem
    lang = None

    # 先剥标记后缀（merged.subtransjav / pass1 / pass2 位于语言码右侧）
    m = _MARKER_RE.search(stem)
    if m:
        stem = stem[:m.start()]

    # 再剥语言后缀，记录语言码
    m = _LANG_RE.search(stem)
    if m:
        lang = m.group(1)
        stem = stem[:m.start()]

    base = stem
    if lang is None:
        lang = "ja"

    d = p.parent
    return (p, d / f"{base}.{lang}.pass1.srt", d / f"{base}.{lang}.pass2.srt")


def find_pass_siblings(in_path: str) -> tuple[Path | None, Path | None]:
    """在 in_path 同目录下查找 pass1 / pass2 兄弟文件。

    两者都存在才返回，否则返回 ``(None, None)``。
    任何异常都静默吞掉，绝不抛出。
    """
    try:
        _, p1, p2 = _sibling_paths(in_path)
        if p1.is_file() and p2.is_file():
            return (p1, p2)
        return (None, None)
    except Exception:
        return (None, None)


def probe_disagreement_mode(in_path: str) -> str:
    """探测 in_path 的分歧对照模式（不读文件内容，只查兄弟文件存在性）。

    返回值：
        "dual"           pass1/pass2 齐全，可对照；
        "missing_pass1"  仅缺 pass1；
        "missing_pass2"  仅缺 pass2；
        "none"           两个兄弟文件都缺失（或路径解析失败）。
    """
    try:
        _, p1, p2 = _sibling_paths(in_path)
        has1, has2 = p1.is_file(), p2.is_file()
        if has1 and has2:
            return "dual"
        if has2:
            return "missing_pass1"
        if has1:
            return "missing_pass2"
        return "none"
    except Exception:
        return "none"


def _timing_span(timing: str) -> tuple[float, float]:
    """解析 ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` 为 (start_sec, end_sec)。

    兼容毫秒逗号/点号；解析失败返回 ``(-1.0, -1.0)``。
    """
    m = re.match(
        r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
        (timing or "").strip())
    if not m:
        return (-1.0, -1.0)
    g = list(map(int, m.groups()))
    start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
    end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
    return (round(start, 3), round(end, 3))


def _normalize(text: str) -> str:
    """去空白与常见标点，返回用于相似度比较的纯字符串。"""
    return _NORMALIZE_RE.sub("", text or "")


def _similarity(a: str, b: str) -> float:
    """基于 difflib.SequenceMatcher 的文本相似度；任一归一化后为空返回 0.0。"""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _best_overlap(span: tuple[float, float], entries: list) -> dict | None:
    """在 entries 中找与 span 时间区间正重叠最大者；无重叠/负坐标返回 None。"""
    s0, e0 = span
    if s0 < 0 or e0 < 0:
        return None
    best = None
    best_overlap = 0.0
    for e in entries:
        s1, e1 = _timing_span(e["timing"])
        if s1 < 0 or e1 < 0:
            continue
        # 正重叠：max(start) < min(end)
        if not (max(s0, s1) < min(e0, e1)):
            continue
        overlap = min(e0, e1) - max(s0, s1)
        if overlap > best_overlap:
            best_overlap = overlap
            best = e
    return best


def judge_artifact(iou: float, offset: float, text1: str, text2: str,
                   iou_threshold: float, offset_threshold: float,
                   similarity: float = -1.0) -> bool:
    """判定一对分歧行是否为"伪影"（不值得人工复核的行）。

    生产判据（2026-09-07 实测定版，选项2）：仅文本判据——
    ① 白名单兜底：短侧归一化后命中应和/感叹白名单 → 判伪影；
    ② V3 长度组合门槛：短侧 ≤ ``ARTIFACT_SHORT_MAX`` 且 长侧 ≥
    ``ARTIFACT_LONG_MIN`` 且 ``similarity < REVIEW_STRICT_THRESHOLD``
    → "极短碎片 vs 实义长句"错配，判伪影。

    时间轴一级规则已按实测移除：iou/offset 参数保留在签名中仅为兼容
    离线矩阵脚本（tools/recompute_divergence.py 需按参数扫描一级规则
    的历史效果），本函数内不再使用（160 行标注实测其单用 A+B 存活
    仅 85.5%~90.6%，与文本判据叠加会把存活拖到 88.9%，是误杀主因）。
    similarity 未知时传 -1.0：跳过 V3 门槛，仅保留白名单兜底。
    """
    n1, n2 = _normalize(text1), _normalize(text2)
    shorter, longer = sorted((len(n1), len(n2)))
    short_str = n1 if len(n1) <= len(n2) else n2
    # ① 白名单兜底
    if short_str and short_str in _INTERJECTION_WHITELIST:
        return True
    # ② V3 长度组合门槛
    if (similarity >= 0
            and shorter <= ARTIFACT_SHORT_MAX
            and longer >= ARTIFACT_LONG_MIN
            and similarity < REVIEW_STRICT_THRESHOLD):
        return True
    return False


def collect_disagreement(in_path: str) -> dict | None:
    """采集 in_path（合并产物）中双引擎分歧行。

    无 pass1/pass2 兄弟文件时返回 None；否则返回
    ``{"total": int, "matched": int, "rows": [...]}``，rows 按 similarity
    升序（最分歧在前）。
    """
    p1, p2 = find_pass_siblings(in_path)
    if p1 is None or p2 is None:
        return None

    own = parse_srt(Path(in_path).read_text(encoding="utf-8"))
    pass1 = parse_srt(p1.read_text(encoding="utf-8"))
    pass2 = parse_srt(p2.read_text(encoding="utf-8"))

    rows = []
    for e in own:
        span = _timing_span(e["timing"])
        hit1 = _best_overlap(span, pass1)
        hit2 = _best_overlap(span, pass2)
        if hit1 is None or hit2 is None:
            continue
        s1, e1 = _timing_span(hit1["timing"])
        s2, e2 = _timing_span(hit2["timing"])
        # 时间轴重叠率 IoU = 交集 / 并集（防御性处理并集为 0 / 负坐标）
        overlap = max(0.0, min(e1, e2) - max(s1, s2))
        union = (e1 - s1) + (e2 - s2) - overlap
        iou = (overlap / union) if union > 0 else 0.0
        # 错位幅度：两侧起始差与结束差的绝对值取最大（秒）
        offset = max(abs(s1 - s2), abs(e1 - e2))
        rows.append({
            "index": e["index"],
            "timing": e["timing"],
            "pass1_timing": hit1["timing"],
            "pass2_timing": hit2["timing"],
            "pass1": hit1["text"],
            "pass2": hit2["text"],
            "similarity": _similarity(hit1["text"], hit2["text"]),
            "iou": round(iou, 4),
            "offset": round(offset, 3),
            "artifact": judge_artifact(iou, offset, hit1["text"], hit2["text"],
                                       IOU_THRESHOLD, OFFSET_THRESHOLD,
                                       similarity=_similarity(hit1["text"],
                                                               hit2["text"])),
        })

    rows.sort(key=lambda r: r["similarity"])
    return {"total": len(own), "matched": len(rows), "rows": rows}
