"""
Refine 词库：加载 / 保存 / 命中匹配 / 提示词块格式化
CSV 格式（UTF-8）：两列 —— 原文词条,期望译文；
可选第三列 target_aliases（v1.2.2 批次 D）：`|` 分隔多个候选译法，
缺列/空 = 无别名（完全向后兼容，注入仍只用主译法）。
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


def load_glossary_ex(path: str):
    """加载词库（含可选别名列）：返回 [(src, dst, aliases), ...]。

    aliases 为第三列按 `|` 拆分后的非空译法元组；两列行/缺列/全空白
    别名列 → 空元组（无别名）。其余判定与 load_glossary 完全一致
    （utf-8-sig、两列起有效、按 src+dst 去重保序）。
    """
    entries = []
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f):
                    if len(row) >= 2 and row[0].strip() and row[1].strip():
                        src, dst = row[0].strip(), row[1].strip()
                        aliases = tuple(
                            a.strip() for a in (row[2].split("|")
                                                if len(row) >= 3 else [])
                            if a.strip())
                        if (src, dst, aliases) not in entries:
                            entries.append((src, dst, aliases))
        except Exception as e:
            print(f"⚠️ [refine] 词库读取失败，已忽略：{e}")
    return entries


def save_glossary(path: str, entries):
    """保存词库（UTF-8 BOM CSV）。

    entries 兼容两列 (src, dst) 与三列 (src, dst, aliases) 词条：
    aliases 为非空元组/列表时写第三列 `|` 分隔别名；否则只写两列
    （两列行为与旧版完全一致，向后兼容）。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for entry in entries:
            if len(entry) >= 3 and entry[2]:
                w.writerow([entry[0], entry[1], "|".join(entry[2])])
            else:
                w.writerow([entry[0], entry[1]])


def match_glossary(text: str, glossary):
    """命中匹配：英文词条忽略大小写；其余原样子串匹配。

    兼容两列 (src, dst) 与三列 (src, dst, aliases) 词条（v1.2.2 D：
    别名不参与命中判定，命中只看源词）。
    """
    hits = []
    low = text.lower()
    for entry in glossary:
        src, dst = entry[0], entry[1]
        if src.isascii():
            if src.lower() in low:
                hits.append((src, dst))
        elif src in text:
            hits.append((src, dst))
    return hits


def format_glossary_block(hits):
    """词库命中块（注入提示词）：只呈现主译法（dst），别名不注入
    （v1.2.2 D：别名仅供冲突判定/一致性统计豁免，不进提示词）。"""
    lines = ["【术语对照表 - 翻译中必须严格采用以下译法】"]
    lines.extend(f"{h[0]} → {h[1]}" for h in hits)
    return "\n".join(lines)
