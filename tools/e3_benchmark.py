"""E3 跑批基准：事后解析 Logs/ 运行日志，输出一次跑批的阶段耗时 / 批耗时分布 / 吞吐
================================================================================

解析对象（真实日志，样例见 Logs/9-23.txt）：

  阶段标记行：  [00:36:58] [STAGE] 阶段A 净语+翻译
  批次运行头：  [00:36:59] [llm] 共 1672 条，分 56 批（并发=1）
  批次心跳行：  [01:42:48]    ⏳ 批次 41/56

阶段 A/B 区分依据：优先用日志里的 ``[STAGE] 阶段A/阶段B`` 标记行；若日志无该标记，
则按同一文件内批次运行头的先后序列推断（第一段=阶段A、第二段=阶段B），并在输出中注明。

用法：
    python tools/e3_benchmark.py Logs/9-23.txt [Logs/9-19.txt ...] [--json]

多个文件按命令行给出的顺序拼接解析（用于跨文件的时段切片）；无批次运行的文件跳过。
吞吐口径为"条/秒"（日志无 token 数，不估算 tok/s）。

时间口径说明：批次行可能因日志缓冲写而延迟落盘，行时间戳与真实处理时序有偏差；
本工具定位为事后解析（跑完后对完整文件解析）。所有时间为日志时间戳口径，缓冲写
可能导致 ±秒级偏差；精确口径请对齐输出产物 mtime。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

# ---------------------------------------------------------------- 正则

_TS_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")
_STAGE_RE = re.compile(r"\[STAGE\]\s*阶段([AB])")
_HEADER_RE = re.compile(r"\[llm\]\s*共\s*(\d+)\s*条，分\s*(\d+)\s*批（并发=(\d+)）")
_HB_RE = re.compile(r"⏳\s*批次\s*(\d+)\s*/\s*(\d+)")

_MIDNIGHT_WINDOW = 12 * 3600  # 跨零点判定窗口：时间戳回退超过 12h 视为跨天

DISCLAIMER = (
    "注：所有时间为日志时间戳口径；日志缓冲写可能导致 ±秒级偏差，"
    "精确口径请对齐输出产物 mtime。"
)


# ---------------------------------------------------------------- 解析

def _ts_to_seconds(h: str, m: str, s: str) -> int:
    return int(h) * 3600 + int(m) * 60 + int(s)


def _iter_file_lines(path: Path):
    """逐行产出 (文件名, 行文本)；读失败抛异常由上层处理。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        yield path.name, line


def parse_log_files(paths: list[Path]) -> list[dict]:
    """按时序拼接解析多个日志文件，返回运行（run）列表。

    每个 run 描述一次批次运行头到其批次心跳序列：
      stage        阶段名（'A'/'B'/推断标记）
      stage_source 'marker'（[STAGE] 行）或 'inferred'（按运行头顺序推断）
      entries      运行头的总条数
      n_batches    运行头的分批数
      concurrency  并发数
      start        起始时刻（跨零点校正后的累计秒）
      end          最后一个心跳的累计秒（无心跳为 None）
      batch_durs   相邻心跳差列表（秒）
      seen_batches 实际见到的心跳批次数（取最大批号）
      files        该 run 覆盖的文件名（有序去重）
    """
    runs: list[dict] = []
    current_stage: str | None = None
    stage_source = "marker"
    last_ts: int | None = None  # 跨文件/跨零点的累计时钟
    cur: dict | None = None

    def _stage_label() -> tuple[str, str]:
        if current_stage is not None:
            return current_stage, stage_source
        # 无 [STAGE] 标记：按运行头先后序列推断（第一段=A、之后交替 B/A…）
        idx = len(runs)
        return ("A" if idx % 2 == 0 else "B"), "inferred"

    for path in paths:
        for fname, line in _iter_file_lines(path):
            m = _TS_RE.match(line)
            if not m:
                continue
            ts = _ts_to_seconds(*m.groups())
            if last_ts is not None and ts < last_ts - _MIDNIGHT_WINDOW:
                ts += 24 * 3600 * (1 + (last_ts - _MIDNIGHT_WINDOW - ts) // (24 * 3600))
            last_ts = ts

            sm = _STAGE_RE.search(line)
            if sm:
                current_stage = sm.group(1)
                stage_source = "marker"
                continue

            hm = _HEADER_RE.search(line)
            if hm:
                stage, src = _stage_label()
                cur = {
                    "stage": stage,
                    "stage_source": src,
                    "entries": int(hm.group(1)),
                    "n_batches": int(hm.group(2)),
                    "concurrency": int(hm.group(3)),
                    "start": ts,
                    "end": None,
                    "batch_durs": [],
                    "seen_batches": 0,
                    "files": [fname],
                }
                runs.append(cur)
                # 运行头之后若再无 [STAGE] 行而直接出现新运行头，继续按序列推断
                current_stage = None
                continue

            if cur is None:
                continue
            bm = _HB_RE.search(line)
            if bm:
                idx = int(bm.group(1))
                if idx > cur["seen_batches"]:
                    # 首个心跳相对运行头计时（即首批耗时），其后相邻心跳差
                    cur["batch_durs"].append(ts - (cur["end"] if cur["seen_batches"] else cur["start"]))
                    cur["end"] = ts
                    cur["seen_batches"] = idx
            if fname != cur["files"][-1]:
                cur["files"].append(fname)

    return runs


# ---------------------------------------------------------------- 统计

def _stats(durs: list[int]) -> dict:
    if not durs:
        return {"n": 0}
    return {
        "n": len(durs),
        "min": min(durs),
        "median": statistics.median(durs),
        "mean": statistics.mean(durs),
        "total": sum(durs),
    }


def _fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def summarize(runs: list[dict]) -> dict:
    """把 run 列表聚合成 E3 基准结果（机器可读结构）。"""
    stages: dict[str, dict] = {}
    for r in runs:
        st = stages.setdefault(
            r["stage"],
            {
                "stage_source": sorted({x["stage_source"] for x in []}),
                "runs": [],
                "batch_durs": [],
                "entries": 0,
                "duration": 0.0,
                "start": None,
                "end": None,
            },
        )
        st["runs"].append(r)
        st["batch_durs"].extend(r["batch_durs"])
        st["entries"] += r["entries"]
        if r["end"] is not None:
            st["start"] = r["start"] if st["start"] is None else min(st["start"], r["start"])
            st["end"] = r["end"] if st["end"] is None else max(st["end"], r["end"])
    stage_out = {}
    for name, st in stages.items():
        duration = (st["end"] - st["start"]) if st["end"] is not None else 0.0
        sources = sorted({r["stage_source"] for r in st["runs"]})
        stage_out[name] = {
            "stage_source": "+".join(sources),
            "n_runs": len(st["runs"]),
            "entries": st["entries"],
            "duration_sec": duration,
            "throughput_entries_per_sec": (st["entries"] / duration) if duration > 0 else None,
            "batch_durations": _stats(st["batch_durs"]),
            "runs": [
                {
                    "entries": r["entries"],
                    "n_batches": r["n_batches"],
                    "seen_batches": r["seen_batches"],
                    "complete": r["seen_batches"] >= r["n_batches"],
                    "duration_sec": (r["end"] - r["start"]) if r["end"] is not None else None,
                    "files": r["files"],
                }
                for r in st["runs"]
            ],
        }
    total = 0.0
    if runs:
        starts = [r["start"] for r in runs]
        ends = [r["end"] for r in runs if r["end"] is not None]
        if ends:
            total = max(ends) - min(starts)
    return {"stages": stage_out, "total_duration_sec": total, "n_runs": len(runs)}


# ---------------------------------------------------------------- 输出

def render_human(result: dict, inferred_any: bool) -> str:
    lines: list[str] = []
    if inferred_any:
        lines.append("阶段区分：部分运行无 [STAGE] 标记，按运行头先后序列推断（第一段=阶段A、第二段=阶段B）。")
    for name, st in result["stages"].items():
        lines.append(f"── 阶段{name} 净语+翻译/审校+抛光（{st['stage_source']}，{st['n_runs']} 次运行）──")
        bd = st["batch_durations"]
        if bd.get("n"):
            lines.append(
                f"  批耗时(n={bd['n']}): min={bd['min']}s 中位={bd['median']:.1f}s "
                f"均值={bd['mean']:.1f}s total={_fmt_dur(bd['total'])}"
            )
        else:
            lines.append("  批耗时: 无心跳数据")
        dur = _fmt_dur(st["duration_sec"]) if st["duration_sec"] else "N/A"
        lines.append(f"  阶段耗时: {dur}（首运行头→最后心跳）")
        thr = st["throughput_entries_per_sec"]
        lines.append(f"  吞吐: {thr:.2f} 条/秒（共 {st['entries']} 条；日志无 token 数，不估算 tok/s）" if thr else "  吞吐: N/A")
        for r in st["runs"]:
            tag = "" if r["complete"] else f"  ⚠️ 不完整（已见 {r['seen_batches']}/{r['n_batches']} 批，疑似崩溃/中断）"
            rd = _fmt_dur(r["duration_sec"]) if r["duration_sec"] else "N/A"
            files = ",".join(r["files"])
            lines.append(f"    · 运行 {r['entries']} 条/{r['n_batches']} 批, 运行耗时 {rd}, 文件[{files}]{tag}")
    lines.append(f"总时长（首运行头→最后心跳）: {_fmt_dur(result['total_duration_sec']) if result['total_duration_sec'] else 'N/A'}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="E3 跑批基准：事后解析 Logs/ 运行日志，输出阶段耗时/批耗时分布/条每秒吞吐。"
        "多个文件按命令行顺序拼接；无 token 数据，吞吐为条/秒。"
    )
    ap.add_argument("logs", nargs="+", type=Path, help="日志文件路径（按时间顺序给出）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)

    missing = [str(p) for p in args.logs if not p.is_file()]
    if missing:
        print(f"[e3_benchmark] 文件不存在: {', '.join(missing)}", file=sys.stderr)
        return 2

    runs = parse_log_files(args.logs)
    if not runs:
        skipped = ",".join(p.name for p in args.logs)
        print(f"[e3_benchmark] 未找到批次运行头（共 N 条，分 M 批），跳过文件: {skipped}", file=sys.stderr)
        return 1

    result = summarize(runs)
    inferred_any = any("inferred" in st["stage_source"] for st in result["stages"].values())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_human(result, inferred_any))
    return 0


if __name__ == "__main__":
    sys.exit(main())
