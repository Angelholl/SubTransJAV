"""
Refine 词库：加载 / 保存 / 命中匹配 / 提示词块格式化
CSV 格式（UTF-8）：两列 —— 原文词条,期望译文
"""

import csv
import os


def load_glossary(path: str):
    entries = []
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f):
                    if len(row) >= 2 and row[0].strip() and row[1].strip():
                        src, dst = row[0].strip(), row[1].strip()
                        if (src, dst) not in entries:
                            entries.append((src, dst))
        except Exception as e:
            print(f"⚠️ [refine] 词库读取失败，已忽略：{e}")
    return entries


def save_glossary(path: str, entries):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for src, dst in entries:
            w.writerow([src, dst])


def match_glossary(text: str, glossary):
    """命中匹配：英文词条忽略大小写；其余原样子串匹配"""
    hits = []
    low = text.lower()
    for src, dst in glossary:
        if src.isascii():
            if src.lower() in low:
                hits.append((src, dst))
        elif src in text:
            hits.append((src, dst))
    return hits


def format_glossary_block(hits):
    lines = ["【术语对照表 - 翻译中必须严格采用以下译法】"]
    lines.extend(f"{s} → {d}" for s, d in hits)
    return "\n".join(lines)
