"""refine 管线性能基准（P1-6：先基准 → 优化 → 再验证）
======================================================

合成 2000 条日文假文字幕，计量四项：

  [1] TM 精确查询 2000 次：逐条 lookup_exact（基线路径） vs 批量 exact_map
      （优化前 exact_map 未实现时输出 N/A，作为"优化前基线"）。
  [2] 语法提示 500 条：冷缓存（首次真实分析） vs 热缓存（跨阶段缓存命中）。
      优化前两次耗时接近（无缓存）；优化后热缓存应显著下降。
  [3] fake client 全管线单文件端到端（2000 条，零 LLM 延迟）：
      暴露编排层（解析/预合并/语法提示/TM/兜底/落盘）的真实开销。
  [4] 输出对比表。

只依赖标准库 + 项目包，不联网；运行：python tools/bench_refine.py
"""

import os
import shutil
import sys
import tempfile
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

TOTAL = 2000           # 合成字幕条数
TM_HITS = 800          # 入库命中的条数（前 800 条）
GRAMMAR_N = 500        # 语法提示测量条数
REPEAT_EVERY = 5       # 每 5 条重复一次（模拟 ASR 重复行，放大缓存收益）


def _fake_ja(i: int) -> str:
    """合成日文假文：确定性生成，含周期性重复行。"""
    if i % REPEAT_EVERY == 0:
        return f"これはテスト用の繰り返し行です ({i // REPEAT_EVERY})"
    return f"字幕テスト行その{i}、今日は良い天気ですね。番号は{i}です。"


def _timings(i: int) -> str:
    s = i
    return (f"00:{s // 60:02d}:{s % 60:02d},000 --> "
            f"00:{s // 60:02d}:{s % 60:02d},500")


def make_entries(n: int) -> list:
    return [{"index": i + 1, "timing": _timings(i), "text": _fake_ja(i)}
            for i in range(n)]


def bench_tm_exact(workdir: str) -> dict:
    """[1] TM 精确查询：逐条 vs 批量。"""
    from subtransjav.refine.tm import TranslationMemory

    entries = make_entries(TOTAL)
    sources = [e["text"] for e in entries]
    db = os.path.join(workdir, "tm_bench.db")
    tm = TranslationMemory(db)
    for src in sources[:TM_HITS]:
        tm.store(src, f"译-{src}", 1)

    t0 = time.perf_counter()
    per_entry = {}
    for src in sources:
        hit = tm.lookup_exact(src, 1)
        if hit and hit.strip() != src:
            per_entry[src] = hit
    t_per = time.perf_counter() - t0

    batch_fn = getattr(tm, "exact_map", None)
    t_batch = None
    n_batch = None
    if batch_fn is not None:
        t0 = time.perf_counter()
        raw = batch_fn(sources, 1)
        t_batch = time.perf_counter() - t0
        n_batch = sum(1 for src in sources
                      if raw.get(src) is not None
                      and raw[src].strip() != src)
    hits = sum(1 for src in sources
               if per_entry.get(src) is not None
               and per_entry[src].strip() != src)
    tm.close()
    return {"t_per": t_per, "t_batch": t_batch, "hits": hits,
            "n_batch": n_batch}


def bench_grammar(workdir: str) -> dict:
    """[2] 语法提示：500 条冷/热缓存（复用同一批条目与上下文）。"""
    from subtransjav.refine import pipeline_v2 as pv

    entries = make_entries(GRAMMAR_N)
    t0 = time.perf_counter()
    cold = pv._collect_grammar_hints(entries, entries, verbose=False)
    t_cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    hot = pv._collect_grammar_hints(entries, entries, verbose=False)
    t_hot = time.perf_counter() - t0
    return {"t_cold": t_cold, "t_hot": t_hot, "n_cold": len(cold),
            "n_hot": len(hot)}


class _FakeBenchClient:
    """零延迟假客户端：按编号协议回填全部译文。"""

    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          progress=None):
        from subtransjav.translate.llm_client import BatchResult
        return BatchResult(
            translations={e["index"]: f"译-{e['index']}" for e in entries},
            deleted=set(), failed=[])


def bench_e2e(workdir: str) -> dict:
    """[3] fake client 全管线单文件端到端。"""
    from subtransjav.refine import pipeline_v2 as pv
    from subtransjav.refine.config import RefineConfig, StageConfig
    from subtransjav.refine.filters import build_srt

    in_path = os.path.join(workdir, "bench.japanese.srt")
    with open(in_path, "w", encoding="utf-8") as f:
        f.write(build_srt(make_entries(TOTAL)))

    cfg = RefineConfig(
        tm_enabled=False, quality_report=False, auto_glossary=False,
        v2_profile="cloud", event_format="text")
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "bench-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "lmstudio", "bench-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    cfg.inputs = [in_path]
    cfg.output_dir = os.path.join(workdir, "out")
    os.makedirs(cfg.output_dir, exist_ok=True)

    real_make_client = pv._make_client
    pv._make_client = lambda c, tag: _FakeBenchClient()   # 零 LLM 延迟
    try:
        t0 = time.perf_counter()
        out = pv._run_single_v2(cfg, in_path)
        elapsed = time.perf_counter() - t0
    finally:
        pv._make_client = real_make_client
    return {"t_e2e": elapsed, "output": out}


def main() -> int:
    print("=" * 64)
    print("SubTransJAV refine 性能基准 "
          f"(entries={TOTAL}, tm_hits={TM_HITS}, grammar={GRAMMAR_N})")
    print("=" * 64)

    workdir = tempfile.mkdtemp(prefix="bench_refine_")
    try:
        r1 = bench_tm_exact(workdir)
        r2 = bench_grammar(workdir)
        r3 = bench_e2e(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    def fmt(v, unit="s"):
        return f"{v:8.3f} {unit}" if v is not None else "     N/A"

    print("\n[1] TM 精确查询 2000 次（库内命中 800）")
    print(f"    逐条 lookup_exact : {fmt(r1['t_per'])}   <- 优化前基线路径")
    tag = "" if r1["t_batch"] is not None else "（未实现，优化前基线）"
    print(f"    批量 exact_map    : {fmt(r1['t_batch'])} {tag}")
    print(f"    命中对齐: 逐条 {r1['hits']} 条 / 批量 {r1['n_batch']} 条")

    print("\n[2] 语法提示 500 条")
    print(f"    冷缓存: {fmt(r2['t_cold'])} (提示 {r2['n_cold']} 条)")
    print(f"    热缓存: {fmt(r2['t_hot'])} (提示 {r2['n_hot']} 条)")
    speedup = (r2['t_cold'] / r2['t_hot']) if r2['t_hot'] else 0.0
    print(f"    热/冷耗时比: {speedup:.1f}x（无缓存时约 1.0x）")

    print("\n[3] fake client 全管线端到端（2000 条单文件）")
    print(f"    耗时: {fmt(r3['t_e2e'])}")

    print("\n[4] 对比表")
    print("    " + "-" * 52)
    print(f"    {'指标':<26}{'优化前基线':>12}{'当前':>12}")
    print("    " + "-" * 52)
    print(f"    {'TM逐条2000次':<24}{fmt(r1['t_per']):>12}"
          f"{'(保留为回退)':>12}")
    print(f"    {'TM批量exact_map':<23}{'N/A':>12}{fmt(r1['t_batch']):>12}")
    print(f"    {'语法提示冷500条':<23}{fmt(r2['t_cold']):>12}"
          f"{fmt(r2['t_cold']):>12}")
    print(f"    {'语法提示热500条':<23}{fmt(r2['t_hot']):>12}"
          f"{fmt(r2['t_hot']):>12}")
    print(f"    {'端到端单文件2000条':<22}{fmt(r3['t_e2e']):>12}"
          f"{fmt(r3['t_e2e']):>12}")
    print("    " + "-" * 52)
    print("\n说明：脚本内同时计量新旧两条路径；\"优化前基线\"= 逐条/无缓存"
          "路径在本机的实测值。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
