"""一次性 A/B 对比脚本：对比两份（三份）日语 SRT 的质量差异。

用法（仓库根目录下）：
    python tools/ab_compare_srt.py

对比对象：
  A       旧引擎 faster-whisper pass1
  B1      Anime-Whisper 单 pass（B 组 pass1，同位对比）
  Bmerged Anime-Whisper+Qwen3-ASR 合并（新配方成品）

输出打印到 stdout（UTF-8，Windows 控制台无法编码的字符用 ? 替换），
并写入 Temp/ab_start405/对比报告.txt。
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NAME = "[START-405][] 雑魚チ〇ポを貶しながらも何度だってヌいてくれて本番までさせてくれる！日本一回転"

PATH_A = os.path.join(ROOT, "测试文件", NAME + ".ja.pass1.srt")
PATH_B1 = os.path.join(ROOT, "Temp", "ab_start405", NAME + ".ja.pass1.srt")
PATH_BM = os.path.join(ROOT, "Temp", "ab_start405", NAME + ".ja.merged.subtransjav.srt")

OUT_TXT = os.path.join(ROOT, "Temp", "ab_start405", "对比报告.txt")

TIMEWIN = [(93, 104), (187, 240), (600, 660)]


# ---------------------------------------------------------------- SRT 解析
TS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def ts_to_sec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(path):
    """解析 SRT，返回 [(start, end, text)]，文本多行合并为单行。"""
    for enc in ("utf-8-sig", "utf-8", "cp932", "gb18030"):
        try:
            with open(path, encoding=enc) as f:
                raw = f.read()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        raise RuntimeError(f"无法解码文件: {path}")

    subs = []
    block_re = re.compile(r"\r?\n\s*\r?\n")
    for block in block_re.split(raw.strip()):
        lines = [ln for ln in block.strip().splitlines()]
        if not lines:
            continue
        # 找时间码行
        ti = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                ti = i
                break
        if ti is None:
            continue
        m = TS_RE.search(lines[ti])
        if not m:
            continue
        g = m.groups()
        start = ts_to_sec(g[0], g[1], g[2], g[3])
        end = ts_to_sec(g[4], g[5], g[6], g[7])
        text = " ".join(ln.strip() for ln in lines[ti + 1:] if ln.strip())
        text = re.sub(r"<[^>]+>", "", text)  # 去 HTML 标签
        if text:
            subs.append((start, end, text))
    return subs


# ---------------------------------------------------------------- 文本工具
KANA_RE = re.compile(r"[\u3041-\u309F\u30A0-\u30FFー]")
KANJI_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\u3005\u3006]")
PUNCT_RE = re.compile(r"[、。！？!?\-―ー…・「」『』（）()，,\.\s\u3000]")


def count_ja_chars(text):
    """日文字符数：假名 + 汉字 + 长音记号。"""
    return len(KANA_RE.findall(text)) + len(KANJI_RE.findall(text))


def total_speech_time(subs):
    """时间轴跨度去重叠后求和（秒）。"""
    if not subs:
        return 0.0
    ivs = sorted((s, e) for s, e, _ in subs if e > s)
    total = 0.0
    cur_s, cur_e = ivs[0]
    for s, e in ivs[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total


def frag_ratio(subs):
    """碎片率：长度<=3 且纯假名（无汉字）的条目占比。"""
    if not subs:
        return 0.0, 0
    n = sum(
        1 for _, _, t in subs
        if len(t.strip()) <= 3 and not KANJI_RE.search(t)
    )
    return n / len(subs), n


def dup_pairs(subs):
    """连续重复率：相邻条目文本完全相同的对数。"""
    return sum(
        1 for i in range(1, len(subs))
        if subs[i][2] == subs[i - 1][2]
    )


def garbled_counts(subs):
    """高疑似乱码行计数，返回 {模式名: 命中行数}。"""
    pats = {
        "「を覚ません」": re.compile(r"を覚ません"),
        "「ピンソロ」": re.compile(r"ピンソロ"),
        "「アナー」": re.compile(r"アナー"),
        "连续3个以上相同假名": re.compile(r"(.)\1{3,}"),
    }
    counts = {k: 0 for k in pats}
    counts["纯假名>95%且长度>=8无汉字无标点"] = 0
    for _, _, t in subs:
        for k, re_ in pats.items():
            if re_.search(t):
                counts[k] += 1
        stripped = PUNCT_RE.sub("", t)
        if not stripped:
            continue
        if len(stripped) >= 8 and not KANJI_RE.search(stripped):
            kana_n = len(KANA_RE.findall(stripped))
            if kana_n / len(stripped) > 0.95:
                counts["纯假名>95%且长度>=8无汉字无标点"] += 1
    return counts


def match_count(ref, other, window=2.0):
    """ref 中距 other 任意条目中点 window 秒内无对应条目的数量，及反向。"""
    def mids(s):
        return [(s + e) / 2.0 for s, e, _ in s] if False else [(st + en) / 2.0 for st, en, _ in s]

    om = sorted(mids(other))
    import bisect

    def unmatched(a, b_mids):
        miss = 0
        for st, en, _ in a:
            mid = (st + en) / 2.0
            i = bisect.bisect_left(b_mids, mid)
            ok = False
            for j in (i - 1, i):
                if 0 <= j < len(b_mids) and abs(b_mids[j] - mid) <= window:
                    ok = True
                    break
            if not ok:
                miss += 1
        return miss

    return unmatched(ref, om), unmatched(other, mids(ref))


def fmt_ts(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def sample_rows(subs, w0, w1):
    """返回与时间窗 [w0,w1] 有重叠的条目。"""
    return [(s, e, t) for s, e, t in subs if s < w1 and e > w0]


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def write_out(buf_lines, f_stdout):
    """同时写入 stdout 与报告文件（UTF-8）。"""
    for ln in buf_lines:
        try:
            f_stdout.write(ln + "\n")
        except UnicodeEncodeError:
            f_stdout.write(ln.encode("utf-8", errors="replace").decode("ascii", errors="replace") + "\n")


# ---------------------------------------------------------------- 主流程
def main():
    subs_a = parse_srt(PATH_A)
    subs_b1 = parse_srt(PATH_B1)
    subs_bm = parse_srt(PATH_BM)

    datasets = [("A 旧引擎 fw-pass1", subs_a), ("B1 AnimeWhisper-pass1", subs_b1), ("BM 合并成品", subs_bm)]

    lines = []
    out = lines.append

    out("=" * 100)
    out("SRT 质量对比报告  A/B 对比  (一次性脚本 tools/ab_compare_srt.py)")
    out("=" * 100)
    out(f"A  : {PATH_A}")
    out(f"B1 : {PATH_B1}")
    out(f"BM : {PATH_BM}")
    out("")

    # ---- 1. 基础统计
    out("## 1. 基础统计")
    out("-" * 100)
    header = f"{'组别':<24} {'总条数':>8} {'有效发言秒':>14} {'日文字符数':>16} {'字符/分钟':>16}"
    out(header)
    for name, subs in datasets:
        dur = total_speech_time(subs)
        chars = sum(count_ja_chars(t) for _, _, t in subs)
        cpm = chars / (dur / 60.0) if dur > 0 else 0.0
        out(f"{name:<24} {len(subs):>8} {dur:>14.1f} {chars:>16} {cpm:>16.1f}")
    out("")

    # ---- 2. 碎片率
    out("## 2. 碎片率（长度<=3 且纯假名、无汉字的条目占比）")
    out("-" * 100)
    for name, subs in datasets:
        ratio, n = frag_ratio(subs)
        out(f"{name:<24} 碎片段数={n:>5} / {len(subs):>5}  = {ratio * 100:>5.1f}%")
    out("")

    # ---- 3. 连续重复
    out("## 3. 连续重复率（相邻条目文本完全相同的对数）")
    out("-" * 100)
    for name, subs in datasets:
        n = dup_pairs(subs)
        pct = n / max(1, len(subs) - 1) * 100
        out(f"{name:<24} 重复对={n:>5} / {max(1, len(subs) - 1):>5} 对  = {pct:>5.1f}%")
    out("")

    # ---- 4. 乱码模式
    out("## 4. 高疑似乱码行计数")
    out("-" * 100)
    all_counts = {}
    for name, subs in datasets:
        all_counts[name] = garbled_counts(subs)
    keys = list(next(iter(all_counts.values())).keys())
    out(f"{'模式':<34} {'A':>14} {'B1':>14} {'BM':>14}")
    for k in keys:
        out(f"{k:<34} {all_counts['A 旧引擎 fw-pass1'][k]:>14} "
            f"{all_counts['B1 AnimeWhisper-pass1'][k]:>14} "
            f"{all_counts['BM 合并成品'][k]:>14}")
    out("")

    # ---- 5. 抽样对照
    out("## 5. 抽样对照（同一时间窗，A / B1 / BM 三版本并排）")
    for w0, w1 in TIMEWIN:
        out("")
        out(f"### 时间窗 [{w0} s, {w1} s]")
        out("-" * 100)
        sa = sample_rows(subs_a, w0, w1)
        sb1 = sample_rows(subs_b1, w0, w1)
        sbm = sample_rows(subs_bm, w0, w1)
        out(f"A  条目数={len(sa)}  B1 条目数={len(sb1)}  BM 条目数={len(sbm)}")
        out("")
        # 按 A 的条目为基准，找 B1/BM 中重叠最大的条目并排
        used_b1 = set()
        used_bm = set()
        for s0, e0, t0 in sa:
            out(f"[{fmt_ts(s0)} --> {fmt_ts(e0)}] A : {t0}")
            for label, coll, used in (("B1", sb1, used_b1), ("BM", sbm, used_bm)):
                best, best_ov = None, 0.0
                for j, (s1, e1, t1) in enumerate(coll):
                    if j in used:
                        continue
                    ov = overlap(s0, e0, s1, e1)
                    if ov > best_ov:
                        best_ov, best = ov, (s1, e1, t1, j)
                if best and best_ov > 0.2:
                    used.add(best[3])
                    out(f"    [{fmt_ts(best[0])} --> {fmt_ts(best[1])}] {label}: {best[2]}")
                else:
                    out(f"    (无时间重叠的 {label} 条目)")
            out("")
        # B1/BM 有而 A 无的条目也列出
        for label, coll, used in (("B1", sb1, used_b1), ("BM", sbm, used_bm)):
            extra = [(s, e, t) for j, (s, e, t) in enumerate(coll) if j not in used]
            if extra:
                out(f"  （{label} 中未被 A 对上的条目）")
                for s, e, t in extra:
                    out(f"    [{fmt_ts(s)} --> {fmt_ts(e)}] {label}: {t}")
                out("")

    # ---- 6. 漏行对照
    out("## 6. 漏行对照（中点 2 秒内无对方条目即算无对应）")
    out("-" * 100)
    a_miss, b_miss = match_count(subs_a, subs_bm, 2.0)
    out(f"A 中无 BM 对应的条目数（A 漏行方向）: {a_miss} / {len(subs_a)}")
    out(f"BM 中无 A 对应的条目数（B 新增方向）: {b_miss} / {len(subs_bm)}")
    a_miss1, b1_miss = match_count(subs_a, subs_b1, 2.0)
    out(f"（参考）A 中无 B1 对应条目数: {a_miss1}；B1 中无 A 对应条目数: {b1_miss}")
    out("")
    out("=" * 100)
    out("报告结束")

    # 输出
    txt = "\n".join(lines) + "\n"
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write(txt)

    safe = txt.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace")
    sys.stdout.write(safe)
    sys.stdout.write(f"\n[报告已写入] {OUT_TXT}\n")


if __name__ == "__main__":
    main()
