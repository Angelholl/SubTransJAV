"""
Refine SRT 工具：解析 / 构建 / 占位行过滤
"""

import re

# 模型把要删的行写成占位说明而非真正删除，例如：
#   （削除：ASR杂音/呼吸声，无实义）   (删除：孤立感叹词)
PLACEHOLDER_PATTERNS = [
    re.compile(r"^[（(]\s*[削删]\s*除"),
    re.compile(r"^[（(]?\s*(?:ASR|杂音|噪声|呼吸声|无实义|孤立)"),
]

# 模型附加的解释/总结性文本行（出现在条目正文尾部）
CHATTER_LINE = re.compile(
    r"本批次|[Ss]cene\s*\d|#\d|硬性豁免特征|无删除条目|全部保留|"
    r"^(?:说明|总结|注[：:]|Note\s*[：:])"
)

# Module-level lazy singleton for OpenCC t2s converter
_opencc_converter = None


def _opencc_t2s_convert(text: str) -> str:
    """Convert traditional Chinese to simplified using a cached OpenCC instance."""
    global _opencc_converter
    if _opencc_converter is None:
        try:
            import opencc
            _opencc_converter = opencc.OpenCC("t2s")
        except ImportError:
            return text
    return _opencc_converter.convert(text)


def strip_chatter_lines(text: str) -> str:
    keep = [ln for ln in text.splitlines() if not CHATTER_LINE.search(ln)]
    return "\n".join(keep).strip()

_SRT_BLOCK = re.compile(
    r"(\d+)\s*\n\s*"
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n"
    r"([\s\S]*?)(?=\n\s*\d+\s*\n\s*\d{2}:\d{2}:\d{2}[,.]\d{3}|\s*$)"
)


def parse_srt(content: str):
    entries = []
    for m in _SRT_BLOCK.finditer(content or ""):
        idx, timing, text = m.group(1), m.group(2), m.group(3).strip()
        if "-->" in timing:
            entries.append({"index": int(idx), "timing": timing.strip(), "text": text})
    return entries


def build_srt(entries) -> str:
    out = []
    for i, e in enumerate(entries, 1):
        out.append(str(i))
        out.append(e["timing"])
        out.append(e["text"])
        out.append("")
    return "\n".join(out)


def is_placeholder(text: str) -> bool:
    t = (text or "").strip()
    return any(p.search(t) for p in PLACEHOLDER_PATTERNS)


def normalize_srt_file(srt_path: str):
    """重写为纯条目格式：剥离模型附加的解释文本与占位条目。
    返回 (保留数, 删除数)。"""
    with open(srt_path, encoding="utf-8") as _f:
        content = _f.read()

    # OpenCC 繁体→简体归一化（模块级懒加载单例）
    content = _opencc_t2s_convert(content)

    entries = parse_srt(content)
    kept, removed = [], 0
    for e in entries:
        e["text"] = strip_chatter_lines(e["text"])
        if not e["text"]:
            removed += 1
            continue
        if is_placeholder(e["text"]):
            removed += 1
            continue
        kept.append(e)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(build_srt(kept))
    return len(kept), removed


def filter_placeholder_file(srt_path: str) -> int:
    """兼容旧接口：仅返回删除数量"""
    _, removed = normalize_srt_file(srt_path)
    return removed
