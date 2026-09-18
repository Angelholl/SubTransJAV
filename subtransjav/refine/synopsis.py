"""
剧情自摘要（v1.2.2 Beta 特性）
====================
在闸门0+预合并后的条目序列中过滤纯噪声行、按时间分桶均匀抽样剧情行，
以一次独立 LLM 调用生成 3-5 行中文剧情梗概，供 pipeline_v2 注入 A/B
两阶段提示词（消解指代与歧义）。

红线（验收口径，勿破坏）：
- 摘要文本只注入提示词，绝不写入输出目录/终稿/质量报告；
- 摘要缓存使用独立目录 Temp/synopsis_cache/，与翻译记忆库（TM）完全隔离；
- 任何失败（异常/空输出）返回 None，调用方静默跳过（管线照常）；
- 摘要内容与缓存不参与 manifest 指纹（与 sidecar 内容同款已知边界）；
- 摘要调用独立 max_tokens（默认 300），不走阶段A 的批级输出预算；
  超时独立收敛为 min(timeout_llm, 300s)（由调用方在 client 上设置）。
"""

import hashlib
import math
import os
import re
import threading
from pathlib import Path

from .config import TEMP_DIR
from .source_hallucination import is_source_counting_noise

# 缓存键成分：提示词要素变化时递增，旧缓存自然失效（不会误命中）
PROMPT_VERSION = "v1"

# 独立缓存目录（严禁放 translation_memory / TM 库所在位置）
SYNOPSIS_CACHE_DIR = os.path.join(TEMP_DIR, "synopsis_cache")

# 摘要调用独立预算：不继承阶段A 批级 max_tokens 预算（compute_max_output_tokens）
SYNOPSIS_MAX_TOKENS = 300
# 摘要调用超时上限（秒）：min(timeout_llm, 本值)；timeout_llm 默认见 config
SYNOPSIS_TIMEOUT_CAP_S = 300.0
SYNOPSIS_TIMEOUT_DEFAULT_S = 900.0

# 桶采样常量：桶数 = max(3, min(8, ceil(总字符数/750)))
_BUCKET_CHAR_PER = 750
_BUCKET_MIN = 3
_BUCKET_MAX = 8

# 摘要逃生口措辞（提示词约束要求模型信息不足时写"信息不足"，日志统计该词计数）
_INSUFFICIENT_MARK = "信息不足"

# system 提示词：角色 + 抽样不连续声明 + 禁止补足 + 忽略噪声行
SYNOPSIS_SYSTEM_PROMPT = (
    "你是一名剧情分析员。"
    "以下片段来自整片不同位置的抽样，时间上不连续；"
    "信息跨片段缺失属正常，禁止补足跳跃段内容；忽略纯噪声行。")

# user 提示词任务段：3-5 行概括 + 只输出梗概 + 禁止编造 + 信息不足逃生口
_SYNOPSIS_TASK_PROMPT = (
    "任务：根据以上抽样片段，用 3-5 行中文概括整部影片：\n"
    "- 主要人物及关系\n"
    "- 核心剧情线（威胁/冲突等）\n"
    "- 场景构成\n"
    "约束：只输出梗概本身；禁止编造源文没有的内容；"
    f"某项信息不足就在该行写「{_INSUFFICIENT_MARK}」。")

# 时间轴解析（ synopsis 模块自实现，不依赖 pipeline_v2，避免反向依赖）
_TIMING_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _start_sec(timing: str) -> float:
    """解析时间轴起始秒；解析失败返回 inf（排序时稳定沉底）。"""
    m = _TIMING_RE.match((timing or "").strip())
    if not m:
        return float("inf")
    g = list(map(int, m.groups()))
    return g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0


def build_synopsis_input(entries: list, max_chars: int = 6000) -> tuple:
    """从条目序列（闸门0+预合并后，含 timing/text）抽样剧情行。

    步骤：
      ① 过滤纯噪声行（source_hallucination.is_source_counting_noise 为
         True 的跳过）与空行；
      ② 按起始时间排序后分桶均匀采样：桶数 = max(3, min(8,
         ceil(总字符数/750)))，每桶预算 = max_chars/桶数，从桶首依序
         取行至预算止（逐行成本计入换行符，保证拼接结果 ≤ max_chars）；
      ③ 总字符 ≤ max_chars 时单桶全量。

    返回 (sampled_text, meta)；无有效条目返回 ("", {"buckets": 0, ...})。
    meta 含桶数（buckets）、各桶条目区间（bucket_ranges：首末 index 与
    起始时间）、实际字符数（chars）与过滤后总字符数（total_chars）。
    """
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = 6000
    if max_chars <= 0:
        max_chars = 1

    kept = []
    for e in entries or []:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        if is_source_counting_noise(text):
            continue
        kept.append({"index": e.get("index"),
                     "timing": e.get("timing") or "", "text": text})
    kept.sort(key=lambda e: _start_sec(e["timing"]))
    total_chars = sum(len(e["text"]) for e in kept)
    if not kept:
        return "", {"buckets": 0, "bucket_ranges": [], "chars": 0,
                    "total_chars": 0}

    def _span(first: dict, last: dict) -> dict:
        return {"first_index": first.get("index"),
                "last_index": last.get("index"),
                "first_start": round(_start_sec(first["timing"]), 3),
                "last_start": round(_start_sec(last["timing"]), 3)}

    # 总字符未超预算：单桶全量
    if total_chars <= max_chars:
        text = "\n".join(e["text"] for e in kept)
        return text, {"buckets": 1,
                      "bucket_ranges": [_span(kept[0], kept[-1])],
                      "chars": len(text), "total_chars": total_chars}

    n_buckets = max(_BUCKET_MIN, min(_BUCKET_MAX, math.ceil(
        total_chars / _BUCKET_CHAR_PER)))
    budget = max_chars / n_buckets
    n = len(kept)
    ranges, parts = [], []
    for i in range(n_buckets):
        s, t = i * n // n_buckets, (i + 1) * n // n_buckets
        bucket = kept[s:t]
        if not bucket:
            continue
        ranges.append(_span(bucket[0], bucket[-1]))
        acc = 0
        for e in bucket:
            cost = len(e["text"]) + 1     # 计入换行符，保证拼接后 ≤ max_chars
            if acc + cost > budget:
                break                     # 从桶首依序取行至预算止
            parts.append(e["text"])
            acc += cost
    text = "\n".join(parts)
    return text, {"buckets": n_buckets, "bucket_ranges": ranges,
                  "chars": len(text), "total_chars": total_chars}


def cache_key(provider: str, model: str, sampled_text: str) -> str:
    """缓存键 = sha1(PROMPT_VERSION + "\\0" + provider + "/" + model
    + "\\0" + sampled_text)。

    换模型（--s1-model）或提示词版本递增都会使旧缓存自然失效。
    """
    raw = f"{PROMPT_VERSION}\x00{provider or ''}/{model or ''}\x00" \
          f"{sampled_text or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# 每键一把 threading.Lock（防多文件并行时同键重复调用/写缓存）
_KEY_LOCKS: dict = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：同目录临时文件 + os.replace，防止中断留下半截缓存。"""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def request_synopsis(client, sampled_text: str, *, provider: str, model: str,
                     cache_dir: str | None = None,
                     max_tokens: int = SYNOPSIS_MAX_TOKENS) -> str | None:
    """一次 LLM 调用生成剧情梗概（带磁盘缓存）。任何失败返回 None。

    - 缓存键见 cache_key；命中（非空缓存文件）直接返回，不触发 LLM；
    - 未命中调用后按 临时文件+os.replace 原子写缓存；
    - max_tokens 独立传入（默认 SYNOPSIS_MAX_TOKENS=300），调用方保证
      client 的超时已按 min(timeout_llm, 300s) 收敛；
    - 任何异常/空输出 → None（调用方静默跳过，管线照常）。
    """
    text = (sampled_text or "").strip()
    if not text:
        return None
    key = cache_key(provider or "", model or "", text)
    cpath = Path(cache_dir or SYNOPSIS_CACHE_DIR) / f"{key}.txt"
    with _key_lock(key):
        try:
            if cpath.is_file():
                cached = cpath.read_text(encoding="utf-8")
                if cached.strip():
                    return cached
        except OSError:
            pass                       # 缓存读取失败按未命中处理
        try:
            out = client._chat(SYNOPSIS_SYSTEM_PROMPT,
                               f"{text}\n\n{_SYNOPSIS_TASK_PROMPT}",
                               max_tokens=max_tokens)
        except Exception:
            return None
        out = (out or "").strip()
        if not out:
            return None
        try:
            _atomic_write_text(cpath, out)
        except OSError:
            pass                       # 缓存写失败不影响本次摘要可用性
        return out
