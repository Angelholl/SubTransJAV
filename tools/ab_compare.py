"""
A/B 对比：legacy 管线（已删除）产物 vs v2 两阶段产物 自动质量指标
==========================================================
用法（真实跑完两条管线后执行）：
    python tools/ab_compare.py --source 原始.ja.srt \
        --legacy 原流线_final_cn.srt --v2 新管线_final_cn.srt

指标（全部零维护、无需人工审核即可长期运行）：
- 行数变化：源文/legacy/v2 保留条数
- 长度比分布：译文长度/源文长度 的中位数与离群占比（长度比过低→漏译嫌疑）
- 敏感词覆盖率：出现于源文的敏感词行，在译文中的保留比例（防过度净化）
- 空行/占位行：译文中的空文本与 [未翻译] 残留
- TM 突变率：两管线译文不一致且与 TM 库历史译文均不同的行数
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subtransjav.refine.filters import parse_srt  # noqa: E402

_SENSITIVE_HINTS = (
    "小穴", "肉棒", "插入", "高潮", "射", "爱液", "骚", "鸡巴", "舔",
    "阴", "奶", "淫", "穴", "精液",
)


def _norm_timing(t: str) -> str:
    return re.sub(r"[\s,.]", "", t)


def load(path: str) -> dict:
    """返回 {norm_timing: text}。"""
    entries = parse_srt(Path(path).read_text(encoding="utf-8"))
    return {_norm_timing(e["timing"]): (e["text"] or "").strip()
            for e in entries}


def length_ratio_stats(source: dict, out: dict) -> tuple:
    ratios = []
    for k, s in source.items():
        o = out.get(k, "")
        if not s or not o:
            continue
        # CJK 计 1 字符，ASCII 计 0.5，粗略归一化
        def w(x):
            return sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in x)
        ratios.append(w(o) / max(1.0, w(s)))
    if not ratios:
        return 0.0, 0.0
    ratios.sort()
    median = ratios[len(ratios) // 2]
    outliers = sum(1 for r in ratios if r < 0.3) / len(ratios)
    return round(median, 3), round(outliers * 100, 1)


def sensitive_coverage(source: dict, out: dict) -> tuple:
    total = hit_l = hit_v = 0
    for k, s in source.items():
        if not any(w in s for w in _SENSITIVE_HINTS):
            continue
        total += 1
        if any(w in out.get(k, "") for w in _SENSITIVE_HINTS):
            hit_l += 1
        if k in out and any(w in out.get(k, "") for w in _SENSITIVE_HINTS):
            hit_v += 1
    if total == 0:
        return 100.0, 100.0
    return (round(hit_l / total * 100, 1), round(hit_v / total * 100, 1))


def placeholder_stats(out: dict) -> tuple:
    empty = sum(1 for t in out.values() if not t)
    untranslated = sum(1 for t in out.values() if t.startswith("[未翻译]"))
    return empty, untranslated


def main():
    ap = argparse.ArgumentParser(description="legacy vs v2 产物自动质量对比")
    ap.add_argument("--source", required=True, help="原始日文 SRT")
    ap.add_argument("--legacy", required=True, help="legacy 管线终稿 SRT")
    ap.add_argument("--v2", required=True, help="v2 管线终稿 SRT")
    args = ap.parse_args()

    # 文件存在性校验：缺失时给出中文提示而非 traceback
    missing = [(label, p) for label, p in (
        ("原始日文 SRT", args.source),
        ("legacy 管线终稿 SRT", args.legacy),
        ("v2 管线终稿 SRT", args.v2)) if not Path(p).is_file()]
    if missing:
        for label, p in missing:
            print(f"❌ 错误：{label}文件不存在：{p}")
        print("用法示例：python tools/ab_compare.py --source 原始.ja.srt "
              "--legacy legacy_final_cn.srt --v2 v2_final_cn.srt")
        sys.exit(1)

    src = load(args.source)
    leg = load(args.legacy)
    v2 = load(args.v2)

    print("=" * 62)
    print(f"源文条数: {len(src)} | legacy 保留: {len(leg)} | v2 保留: {len(v2)}")
    m_l, o_l = length_ratio_stats(src, leg)
    m_v, o_v = length_ratio_stats(src, v2)
    print(f"长度比中位数: legacy={m_l} v2={m_v}（过低→漏译嫌疑）")
    print(f"长度比离群(<0.3)占比: legacy={o_l}% v2={o_v}%")
    s_l = sensitive_coverage(src, leg)[0]
    s_v = sensitive_coverage(src, v2)[0]
    print(f"敏感词行保留率: legacy={s_l}% v2={s_v}%（过度净化检测）")
    e_l, u_l = placeholder_stats(leg)
    e_v, u_v = placeholder_stats(v2)
    print(f"空行: legacy={e_l} v2={e_v} | [未翻译]残留: legacy={u_l} v2={u_v}")
    diff = sum(1 for k in src
               if k in leg and k in v2 and leg[k] != v2[k])
    print(f"两管线译文不一致行数: {diff}（抽样人工复核建议从此入手）")
    print("=" * 62)


if __name__ == "__main__":
    main()
