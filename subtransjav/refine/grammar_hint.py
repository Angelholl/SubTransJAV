"""
语法提示注入模块 - SudachiPy 轻量句法分析辅助层

为 S2（日→中翻译）阶段提供形态素解析提示，改善翻译质量。
在 SRT 条目文本中嵌入【语法提示】段落，供 LLM 翻译时参考。
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------------------
# Lazy singleton tokenizer
# ---------------------------------------------------------------------------

_tokenizer_instance: Any = None
_sudachi_available: bool | None = None
# D4：SudachiPy 底层为 Rust 实现，Dictionary/Tokenizer 非线程安全——
# 多线程并发冷缓存 miss 时同时初始化单例、或并发调用同一 Tok 实例的
# tokenize，会抛 RuntimeError: Already borrowed。初始化与 tokenize 分别
# 加锁（热路径 lru_cache 命中不经过锁，无明显劣化）。
_init_lock = threading.Lock()
_tokenize_lock = threading.Lock()


def _get_tokenizer():
    """懒加载 SudachiPy Dictionary 单例（双重检查锁，防并发重复初始化）。"""
    global _tokenizer_instance, _sudachi_available
    if _tokenizer_instance is not None:
        return _tokenizer_instance
    with _init_lock:
        if _tokenizer_instance is not None:   # 等锁期间可能已被其他线程初始化
            return _tokenizer_instance
        try:
            from sudachipy import Dictionary
            _tokenizer_instance = Dictionary().create()
            _sudachi_available = True
        except Exception:
            _tokenizer_instance = None
            _sudachi_available = False
        return _tokenizer_instance


def is_grammar_hint_available() -> bool:
    """检查 SudachiPy 是否可用。"""
    if _sudachi_available is None:
        _get_tokenizer()
    return bool(_sudachi_available)


# ---------------------------------------------------------------------------
# Cached tokenization
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4096)
def _tokenize_cached(text: str):
    """带缓存的形态素解析。返回 token 列表或 None。"""
    tok = _get_tokenizer()
    if tok is None:
        return None
    with _tokenize_lock:    # 同一 Tok 实例并发 tokenize 会 Already borrowed
        return list(tok.tokenize(text))


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------

def _get_context_entries(
    entries: list[dict], current_pos: int, before: int = 3, after: int = 3
) -> tuple[list[dict], list[dict]]:
    """提取当前条目前后的上下文条目。

    Parameters
    ----------
    entries : list[dict]
        预解析的条目列表
    current_pos : int
        当前条目在列表中的位置（索引）
    before : int
        向前取的条目数
    after : int
        向后取的条目数
    """
    if current_pos < 0 or current_pos >= len(entries):
        return [], []

    start = max(0, current_pos - before)
    end = min(len(entries), current_pos + after + 1)
    ctx_before = entries[start:current_pos]
    ctx_after = entries[current_pos + 1 : end]
    return ctx_before, ctx_after


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

# 名词相关 POS 前缀
_NOUN_POS_PREFIXES = ("名詞",)


def _is_noun(token) -> bool:
    """检查 token 是否为名词。"""
    pos = token.part_of_speech()
    return pos[0] in _NOUN_POS_PREFIXES


def _is_verb(token) -> bool:
    """检查 token 是否为动词。"""
    return token.part_of_speech()[0] == "動詞"


def _is_adjective(token) -> bool:
    """检查 token 是否为形容詞。"""
    return token.part_of_speech()[0] == "形容詞"


def _is_connective_particle(token) -> bool:
    """检查 token 是否为接续助词（が/けど/けれども）。

    关键：が 可以是格助词（主语标记）或接続助词（转折铺垫）。
    通过 pos[1] 区分：接続助詞 vs 格助詞。
    """
    surface = token.surface()
    pos = token.part_of_speech()
    if pos[0] != "助詞":
        return False
    # けど/けれど/けれども 总是接続助詞
    if surface in ("けど", "けれど", "けれども"):
        return True
    # が 需要区分：接続助詞(转折) vs 格助詞(主语)
    if surface == "が":
        return len(pos) > 1 and pos[1] == "接続助詞"
    return False


def _is_particle(token, particle: str) -> bool:
    """检查 token 是否为指定助词。"""
    return token.surface() == particle and token.part_of_speech()[0] == "助詞"


def _is_case_particle(token) -> bool:
    """检查 token 是否为格助词（を/に/で/へ等）。"""
    pos = token.part_of_speech()
    return pos[0] == "助詞" and len(pos) > 1 and pos[1] == "格助詞"


def _is_da_renyokei_de(token) -> bool:
    """检查 token 是否为助動詞「だ」的連用形「で」（前接名词/形容动词）。

    区别于格助詞的「で」（POS[0]=="助詞"）：判断助动词的「で」
    POS[0]=="助動詞"（Sudachi 实测：('助動詞','*','*','*','助動詞-ダ',
    '連用形-一般')）。
    """
    pos = token.part_of_speech()
    return token.surface() == "で" and pos[0] == "助動詞"


def _is_clause_final_de(tokens, j: int) -> bool:
    """检查 tokens[j]（surface 为「で」）是否处于句末/句中顿位置。

    Sudachi 実測：句末「名詞+で…」的「で」可能被标注为格助詞而非
    助動詞（如「部長で…」「部長で…誰もが一目置くエース。」）。
    判定：で 之后要么直接到条目结尾，要么紧随的连续标点串中含
    句点类（…/。）标记——即 で 后没有实义内容直接紧随。
    """
    k = j + 1
    has_kuten = False
    while k < len(tokens) and tokens[k].part_of_speech()[0] == "補助記号":
        if len(tokens[k].part_of_speech()) > 1 \
                and tokens[k].part_of_speech()[1] == "句点":
            has_kuten = True
        k += 1
    return k == len(tokens) or has_kuten


def _is_plural_pronoun_at(tokens, i: int) -> bool:
    """检查 tokens[i] 是否为复数人称代词（僕たち/私たち/俺たち/我々等）。

    Sudachi 实测两种分词形态均需覆盖：
    - 「僕たち」整体一个 token（代名詞，surface 以「たち/ら」结尾）；
    - 「ボク」(代名詞) + 「たち」(接尾辞) 拆分为两个 token。
    """
    tok = tokens[i]
    pos = tok.part_of_speech()
    if pos[0] != "代名詞":
        return False
    surface = tok.surface()
    if surface.endswith(("たち", "ら", "達")):
        return True
    if i + 1 < len(tokens):
        nxt = tokens[i + 1]
        if nxt.surface() == "たち" \
                and nxt.part_of_speech()[0] in ("接尾辞", "接尾詞", "代名詞"):
            return True
    return False


# 规则⑦提示文案（Sudachi 路径与 fallback 路径共用，保证口径一致）
_HINT_PLURAL_ADNOMINAL = (
    "僕たち/私たち+名词+で：复数代词是定语（\"我们的…\"），不是主语；"
    "主语从上下文补出。禁止译成\"我是…/我们是…\"开头。")
# 规则⑧提示文案
_HINT_DE_RENTOU = (
    "で=判断助动词だ的中顿（句中停顿/并列）：译\"是……也是……\"或并入前项"
    "定语，严禁\"作为\"；若 で 是格助詞（场所/手段）则按\"在/用\"处理。")


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

def _detect_rules(text: str, context_before: list[dict], context_after: list[dict]) -> list[str]:
    """检测日語語法结构，返回提示列表。"""
    hints: list[str] = []
    tokens = _tokenize_cached(text)
    if tokens is None:
        # Fallback: 简单正则检测
        return _detect_rules_fallback(text, context_before, context_after)

    # Rule 1: sentential_copula - Noun+は/が+Noun without verb/adjective predicate
    # 「NはN」「NがN」且无动词/形容词谓语 → 补出"是"
    has_predicate = any(_is_verb(t) or _is_adjective(t) for t in tokens)
    if not has_predicate:
        for i, tok in enumerate(tokens):
            # 检查后续是否有名词
            if (_is_noun(tok) and i + 1 < len(tokens)
                    and (_is_particle(tokens[i + 1], "は") or _is_particle(tokens[i + 1], "が"))
                    and i + 2 < len(tokens) and _is_noun(tokens[i + 2])):
                hints.append("Noun+は/が+Noun 结构，无谓语 → 补出\"是\"")
                break

    # Rule 2: topic_marker - は 前有名词 → 主题标记
    for i, tok in enumerate(tokens):
        if _is_particle(tok, "は") and i > 0 and _is_noun(tokens[i - 1]):
            noun_surface = tokens[i - 1].surface()
            hints.append(f"「{noun_surface}は」→ は前名词为主题")
            break

    # Rule 3: subject_marker - が 前有名词 → 主语标记
    for i, tok in enumerate(tokens):
        if _is_particle(tok, "が") and i > 0 and _is_noun(tokens[i - 1]):
            noun_surface = tokens[i - 1].surface()
            hints.append(f"「{noun_surface}が」→ が前名词为主语")
            break

    # Rule 4: connective_particle - が/けど/けれども → 转折
    for tok in tokens:
        if _is_connective_particle(tok):
            hints.append(f"「{tok.surface()}」→ 转折/铺垫，译为\"不过/但是\"")
            break

    # Rule 5: possessive_no - XのY where X,Y are nouns
    for i, tok in enumerate(tokens):
        if (_is_particle(tok, "の") and i > 0 and i + 1 < len(tokens)
                and _is_noun(tokens[i - 1]) and _is_noun(tokens[i + 1])):
            x = tokens[i - 1].surface()
            y = tokens[i + 1].surface()
            hints.append(f"「{x}の{y}」→ {x}的{y}（修饰关系）")
            break

    # Rule 6: context_ellipsis - 无主语/主题标记 + 无格助词 + 不完整结尾
    has_wa_or_ga = any(
        (_is_particle(t, "は") or _is_particle(t, "が"))
        for t in tokens
    )
    has_case_particle = any(_is_case_particle(t) for t in tokens)
    # 不完整结尾：句末为助词、接续词、顿号等
    last_surface = tokens[-1].surface() if tokens else ""
    incomplete_endings = ("で", "けど", "が", "し", "ね", "よ", "な", "わ", "、", "…")
    is_incomplete = any(last_surface.endswith(e) for e in incomplete_endings)

    if not has_wa_or_ga and not has_case_particle and is_incomplete:
        # 检查上下文是否有可推断的主语
        ctx_surfaces = " ".join(e["text"] for e in context_before)
        has_ctx_subject = bool(re.search(r"[はが]", ctx_surfaces))
        if has_ctx_subject:
            hints.append("省略主语或主题，请结合前句内容补全省略成分")

    # Rule 7: plural_pronoun_adnominal - 僕たち/私たち/俺たち + 名词 + で
    # 复数代词作定语（"我们的…"）而非主语。で 的判定：
    # ① 助動詞「だ」の連用形「で」（POS[0]=="助動詞"，区别于格助詞）；
    # ② Sudachi 実測：句末「名詞+で…」的「で」可能被标注为格助詞
    #    （如「部長で…」），若 で 前接名词且其后仅剩标点（中顿收尾），
    #    同样按定语+中顿处理。
    for i, tok in enumerate(tokens):
        if _is_plural_pronoun_at(tokens, i):
            # 跳过与代词连成一体的「たち」，从下一个 token 开始找 で
            start = i + 1
            if not tok.surface().endswith(("たち", "ら", "達")) \
                    and start < len(tokens) \
                    and tokens[start].surface() == "たち":
                start += 1
            for j in range(start, len(tokens)):
                t = tokens[j]
                if t.surface() != "で":
                    continue
                pos = t.part_of_speech()
                if pos[0] == "助動詞" \
                        or (pos[0] == "助詞" and _is_clause_final_de(tokens, j)):
                    hints.append(_HINT_PLURAL_ADNOMINAL)
                    break
            break

    # Rule 8: de_rentou - 助動詞「だ」の連用形「で」中顿（与规则⑦同一条目
    # 可同时触发，如「ボクたち、水泳部の部長で…」）
    if any(_is_da_renyokei_de(t) for t in tokens):
        hints.append(_HINT_DE_RENTOU)

    return hints


def _detect_rules_fallback(
    text: str, context_before: list[dict], context_after: list[dict]
) -> list[str]:
    """SudachiPy 不可用时的简单正则回退检测。"""
    hints: list[str] = []

    # Rule 4 fallback: が/けど/けれども 转折
    if re.search(r"(?<![一-鿿])[がけどけれども]+", text):
        m = re.search(r"(?<![一-鿿])(が|けど|けれども)", text)
        if m:
            hints.append(f"「{m.group(1)}」→ 转折/铺垫，译为\"不过/但是\"")

    # Rule 5 fallback: XのY
    m = re.search(r"(\S+)の(\S+)", text)
    if m:
        hints.append(f"「{m.group(1)}の{m.group(2)}」→ {m.group(1)}的{m.group(2)}（修饰关系）")

    # Rule 6 fallback: 省略检测 - 不完整结尾
    if re.search(r"[でがしよねなわ、…]$", text.strip()):
        ctx_surfaces = " ".join(e["text"] for e in context_before)
        if re.search(r"[はが]", ctx_surfaces):
            hints.append("省略主语或主题，请结合前句内容补全省略成分")

    # Rule 7 fallback: 僕たち/私たち/俺たち + 名词 + で 定语结构。
    # 覆盖片假名变体（ボク/ワタシ/オレ，ASR 字幕常见）；间隔允许顿号/
    # の但排除 は/が/を（僕たちは部長だ→"我们是部长"是正确翻译，不误伤；
    # 僕たちが公園で…为主语句，同样不触发）。
    # 规则⑧在无 Sudachi 时跳过：纯正则区分不了格助詞的「で」与
    # 助動詞「だ」連用形的「で」，宁缺毋滥。
    if re.search(r"(?:僕|私|俺|ボク|ワタシ|オレ)たち[^はがを。！？]{0,12}で",
                 text):
        hints.append(_HINT_PLURAL_ADNOMINAL)

    return hints


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_grammar_hints(
    srt_content: str,
    current_index: int,
    entries: list[dict] | None = None,
) -> str:
    """
    为指定 SRT 条目生成语法提示。

    Parameters
    ----------
    srt_content : str
        完整 SRT 文件内容。
    current_index : int
        当前条目的序号。
    entries : list[dict] | None
        预解析的条目列表（优化：避免重复解析）。

    Returns
    -------
    str
        语法提示文本（格式：「【语法提示】\\n- detail1\\n- detail2」），
        无提示时返回空字符串。
    """
    if entries is None:
        from .filters import parse_srt
        entries = parse_srt(srt_content)

    # 找到当前条目及其位置（一次扫描）
    current_pos = None
    current_entry = None
    for i, e in enumerate(entries):
        if e["index"] == current_index:
            current_pos = i
            current_entry = e
            break
    if current_entry is None or current_pos is None:
        return ""

    text = current_entry["text"]
    if not text.strip():
        return ""

    # 提取上下文（使用位置索引，O(1) 查找）
    ctx_before, ctx_after = _get_context_entries(entries, current_pos)

    # 检测规则
    hints = _detect_rules(text, ctx_before, ctx_after)
    if not hints:
        return ""

    # 格式化输出
    lines = ["【语法提示】"]
    for h in hints:
        lines.append(f"- {h}")
    return "\n".join(lines)
