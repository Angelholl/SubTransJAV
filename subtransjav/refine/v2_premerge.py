"""
v2 预合并/时间轴/未翻译标记 纯函数层
====================================
职责：时间轴解析与对齐（_timing_span / _align_orig_by_timing）、
代码层预合并断句修复（_premerge_entries 及其正则/阈值常量）、
[未翻译] 标记规范化（UNTRANSLATED_PREFIX / _UNTRANSLATED_MARK_LOCAL /
_normalize_untranslated_marker）。

迁出来源：pipeline_v2 拆分批次 1·模块 A（自 subtransjav/refine/pipeline_v2.py
逐字迁移，符号体零改动）。

行为等价声明：
- D2026-0922-03 HRO-2：重构前后产物逐字节等价；
- D2026-0925-01：拆分执行契约。
"""

import re

from .config import DEFAULT_PREMERGE_MAX_ITEMS, RefineConfig

UNTRANSLATED_PREFIX = "[未翻译] "

# [未翻译] 标记裸形态（无尾空格，与 post_validate._UNTRANSLATED_MARK 同值；
# 因循环导入风险本地再声明，判定口径统一走 is_untranslated_text）。
_UNTRANSLATED_MARK_LOCAL = "[未翻译]"


def _normalize_untranslated_marker(text: str, orig_text: str) -> str:
    """A2 残译清洗：[未翻译] 前缀后跟非空残译文（如 "[未翻译] Chicks。"）
    的污染形态规范化。

    - 原文可得 → UNTRANSLATED_PREFIX + 日文原文（与阶段A 失败回退一致）；
    - 原文不可得 → 剥掉残译文只留纯前缀 "[未翻译]"；
    - 纯占位形态（前缀后无内容）原样返回，防二次加标；
    - 只做 strip 后开头匹配，正文中部合法出现 "[未翻译]" 的译文不碰。
    """
    s = (text or "").strip()
    if not s.startswith(_UNTRANSLATED_MARK_LOCAL):
        return text
    residue = s[len(_UNTRANSLATED_MARK_LOCAL):].strip()
    if not residue:
        return text                    # 纯占位：保持现状
    orig = (orig_text or "").strip()
    return UNTRANSLATED_PREFIX + orig if orig else UNTRANSLATED_PREFIX.rstrip()


# ---------------------------------------------------------------------------
# 代码层预合并（断句修复）：ASR 常把一句话切成多条碎片。
# 规则来自「日文净语者」角色卡的预处理标准（时间相邻 ≤0.5s 且语义断裂、
# 或助词/省略号结尾 ≤1.0s 强制合并；完整句/一问一答/超长不合并）。
# 合并后条目时间轴 = 首条起点 ~ 末条终点，文本直接拼接。
# ---------------------------------------------------------------------------
_PREMERGE_AUX_END = re.compile(
    r"(?:は|が|を|に|で|て|し|から|ので|って|けど|けども|ー)\s*$")
_PREMERGE_INCOMPLETE_START = re.compile(
    r"^(?:あの|その|えっと|なんか|でも|だから|それで|そして|つまり|"
    r"けど|が|で|ちょっと|でもね|それでね)")
_PREMERGE_COMPLETE_END = re.compile(
    r"(?:です|ます|んだ|のだ|よね|ない|た|だ|か[。？?]?|[。！？?！])\s*$")
# 省略号/波浪线收尾：刻意的戏剧停顿，不参与 ≤1.0s 强制合并档
_PREMERGE_TRAILING = re.compile(r"(?:…|⋯|〜|~)\s*$")
# 连续的尾部省略号/波浪线（可重叠多个）：剥离后使句末判定作用于真实句尾
_PREMERGE_PAUSE_TAIL = re.compile(r"(?:…|⋯|〜|~)+\s*$")
# P1-5：阈值收口到 config（模块常量仅为无 cfg 直调时保留历史默认值）
_PREMERGE_MAX_SPAN = 5.0              # 无 cfg 直调时的回退跨度上限（秒），与 premerge_max_span_ms 默认 5000ms 对齐；premerge_max_gap_s 不再兼任此职
_PREMERGE_MAX_SPAN_MS = 5000          # 无 cfg 直调时的回退跨度硬上限（毫秒，= _PREMERGE_MAX_SPAN×1000）
_PREMERGE_MAX_COUNT = DEFAULT_PREMERGE_MAX_ITEMS  # 合并条数上限
_PREMERGE_MAX_CHARS = 80              # 无 cfg 直调时的回退合并文本字符上限
_PREMERGE_MIN_FRAGMENT_CHARS = 6      # 无 cfg 直调时的回退语义断裂档短碎片阈值（字符）


def _strip_trailing_pause(text):
    """剥离尾部连续省略号/波浪线（戏剧停顿标记），使句末判定作用于真实句尾。"""
    return _PREMERGE_PAUSE_TAIL.sub("", text)


def _premerge_entries(entries: list, cfg: RefineConfig = None) -> list:
    """断句修复：按角色卡预处理标准合并被 ASR 错误切割的相邻碎片。

    cfg 提供时使用 cfg.premerge_max_span_ms / cfg.premerge_max_items /
    cfg.premerge_max_chars / cfg.premerge_min_fragment_chars（用户可调）；
    缺省回退模块常量（兼容无 cfg 的直调/单测）。premerge_max_gap_s
    不再参与合并判定（跨度上限职责已移交 premerge_max_span_ms）。
    """
    max_span_ms = _PREMERGE_MAX_SPAN_MS
    max_chars = _PREMERGE_MAX_CHARS
    min_fragment = _PREMERGE_MIN_FRAGMENT_CHARS
    max_count = _PREMERGE_MAX_COUNT
    if cfg is not None:
        max_span_ms = int(getattr(cfg, "premerge_max_span_ms",
                                  _PREMERGE_MAX_SPAN_MS))
        max_chars = int(getattr(cfg, "premerge_max_chars", _PREMERGE_MAX_CHARS))
        min_fragment = int(getattr(cfg, "premerge_min_fragment_chars",
                                   _PREMERGE_MIN_FRAGMENT_CHARS))
        max_count = int(getattr(cfg, "premerge_max_items", _PREMERGE_MAX_COUNT))
    if not entries:
        return entries
    merged = []
    for e in entries:
        prev = merged[-1] if merged else None
        if prev is None:
            merged.append(dict(e))
            continue
        ps = _timing_span(prev["timing"])
        cs = _timing_span(e["timing"])
        if ps[0] < 0 or cs[0] < 0:
            merged.append(dict(e))
            continue
        gap = cs[0] - ps[1]
        merged_dur = cs[1] - ps[0]
        prev_text = (prev["text"] or "").strip()
        cur_text = (e["text"] or "").strip()
        if not prev_text or not cur_text:
            merged.append(dict(e))
            continue
        # 剥离尾部戏剧停顿标记后再做句末/助词判定（RC1：行尾省略号曾使
        # 「です/ます」句末判定失效，导致完整句被误判为断裂而合并）
        prev_core = _strip_trailing_pause(prev_text)

        # 合并判定（角色卡标准）
        allow = False
        if gap <= 1.0 and _PREMERGE_AUX_END.search(prev_core):
            allow = True                       # 助词结尾（剥离停顿后）→ ≤1.0s 强制
        elif gap <= 0.5 and (
                _PREMERGE_INCOMPLETE_START.match(cur_text)
                or (not _PREMERGE_COMPLETE_END.search(prev_core)
                    and len(cur_text) < min_fragment)):
            allow = True                       # 语义断裂 → ≤0.5s 合并（接续词档
                                               # 不限长度；「上行未完成 + 下行短
                                               # 碎片」档要求下行是短碎片）
        # 否决：前条以省略号/波浪线收尾（刻意的戏剧停顿），后条自身是
        # 完整句、或同样以停顿收尾 → 属独立字幕，不是 ASR 碎片（无论
        # gap 多小、走哪个档都不合并；如「ボクたち、水泳部の部長で…」+
        # 「誰もが一目置くエース。」）
        if _PREMERGE_TRAILING.search(prev_text) \
                and (_PREMERGE_COMPLETE_END.search(cur_text)
                     or _PREMERGE_TRAILING.search(cur_text)):
            allow = False
        # 禁止：两条都是完整陈述句（各自语义完整）
        if allow and _PREMERGE_COMPLETE_END.search(prev_text) \
                and _PREMERGE_COMPLETE_END.search(cur_text) and gap > 0.2:
            allow = False
        # 禁止：超长/超条数（RC3：独立硬跨度上限 ms；round() 防浮点毛刺，
        # 精确边界"5000ms 过 / 5001ms 拒"；文本按合并双方字符数之和设上限）
        n_prev = prev.get("_merge_count", 1)
        if round(merged_dur * 1000) > max_span_ms \
                or len(prev_text) + len(cur_text) > max_chars \
                or n_prev >= max_count:
            allow = False

        if allow:
            # 防护：正常情况下走到这里 timing 已通过 _timing_span 校验
            # （必含 "-->"）；万一格式异常则与解析失败同口径——放弃合并
            prev_parts = prev["timing"].split("-->")
            cur_parts = e["timing"].split("-->")
            if len(prev_parts) < 2 or len(cur_parts) < 2:
                merged.append(dict(e))
                continue
            prev["timing"] = prev_parts[0].strip() \
                + " --> " + cur_parts[1].strip()
            prev["text"] = prev_text + cur_text      # 日文碎片直接拼接
            prev["_merge_count"] = n_prev + 1
        else:
            merged.append(dict(e))
    out = []
    for e in merged:
        e.pop("_merge_count", None)
        out.append(e)
    return out


def _timing_span(timing: str) -> tuple:
    """解析时间轴为 (start_sec, end_sec)；解析失败返回 (-1, -1)。

    时间轴是字幕条目的真实身份标识：cleaner 等环节会重新编号，
    index 不可作为跨环节对齐依据，时间轴可以。
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


def _align_orig_by_timing(entries: list, orig_entries: list) -> list:
    """按时间轴把 orig 条目顺序对齐到 entries（双指针，容忍同起点多条）。

    两个列表都必须按时间轴升序（cleaner/流水线产物天然如此）。
    返回与 entries 等长的列表：元素为对应 orig 条目，无法对齐为 None。
    - 起点相同的多条：按顺序逐条消费，不会互相覆盖；
    - 合并行（span 被延长）：对齐到其首行 orig，且不消费指针，
      使被合并的后续 orig 行自然落到 None（不参与对齐/学习）。
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
                p += 1              # 精确 1:1 匹配才消费；合并行不消费
        else:
            aligned.append(None)
    return aligned
