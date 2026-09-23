"""
LM Studio 预检与自动加载
========================
解决：中途停止任务并卸载模型后，续跑因"模型未加载"而失败且无提示。

策略：
  1. 查询 /v1/models（仅含已加载模型），目标模型在列 → 检查引擎参数是否对齐
  2. 未对齐（未加载 / ctx 不符 / 并发不符）→
     先 `lms unload --all` 清场（16GB 单卡装不下两个大模型），再
     `lms load` 带参加载（-y --gpu max -c <ctx> --parallel <并发>）
  3. lms 不可用 → 给出明确的人工处理指引

引擎参数以管线配置为唯一事实来源（ctx=v2_ctx_local、parallel=v2_concurrency），
GUI 侧保存的同名参数会在加载时被覆盖——两侧同步由本函数构造性保证。
"""

import json
import os
import shutil
import subprocess

import requests

_LMS_CANDIDATES = (
    os.path.expanduser(r"~\.lmstudio\bin\lms.exe"),
    r"C:\Program Files\LM Studio\lms.exe",
)


def _root_from_endpoint(endpoint: str) -> str:
    """'http://localhost:1234/v1' -> 'http://localhost:1234'"""
    root = (endpoint or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root


def _find_lms() -> str:
    found = shutil.which("lms")
    if found:
        return found
    for cand in _LMS_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return ""


def _loaded_ids(root: str) -> set:
    """/v1/models 只含已加载模型；服务不可达抛异常由调用方处理。"""
    r = requests.get(f"{root}/v1/models", timeout=5)
    return {m.get("id") for m in r.json().get("data", [])}


def _loaded_ctx(root: str, model: str, log=None) -> int:
    """读取已载模型的实际上下文长度；拿不到（旧版无 v0 API / 字段缺失）返回 0。

    v0 API 字段名无正式兼容承诺，按 observed 命名取值并全面容错：
    读不到 ≠ 0 不对齐，宁可放过也不误判重载。
    """
    warn = log or (lambda m: None)
    try:
        r = requests.get(f"{root}/api/v0/models", timeout=5)
        for m in r.json().get("data", []):
            if m.get("id") != model:
                continue
            for key in ("loaded_context_length", "context_length"):
                try:
                    v = int(m.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    return v
            warn("   ⚠️ ctx 检测跳过: /api/v0/models 条目无有效上下文长度，无法核对")
            return 0
        warn("   ⚠️ ctx 检测跳过: /api/v0/models 未返回该模型条目，无法核对")
    except Exception:
        warn("   ⚠️ ctx 检测跳过: /api/v0/models 不可达或响应异常，无法核对")
    return 0


def _loaded_parallel(lms: str, model: str, timeout: float, log=None) -> int:
    """读取已载模型的实际并发数（lms ps --json 条目顶层 parallel 字段）。

    ps schema 无正式兼容承诺，全面容错：条目未找到 / 字段缺失 /
    值非数值（null/字符串/布尔）/ 非正数 / JSON 解析失败 / 超时 /
    非零退出码 → 一律返回 0（拿不到 ≠ 失配，宁可放过也不误判重载）。
    """
    warn = log or (lambda m: None)
    if not lms:
        warn("   ⚠️ 并发检测跳过: 未找到 lms CLI，无法读取引擎并发实值")
        return 0
    try:
        proc = _run_lms(lms, ["ps", "--json"], timeout)
        if proc.returncode != 0:
            warn(f"   ⚠️ 并发检测跳过: lms ps 退出码非零 (exit {proc.returncode})")
            return 0
        data = json.loads(proc.stdout or "[]")
        if isinstance(data, dict):
            data = data.get("data")
        for entry in data or []:
            if not isinstance(entry, dict):
                continue
            if model not in {str(entry.get(k, "")) for k in
                             ("identifier", "modelKey",
                              "indexedModelIdentifier")}:
                continue
            v = entry.get("parallel")
            if isinstance(v, bool):
                warn(f"   ⚠️ 并发检测跳过: ps 条目 parallel 值非法 ({v!r})")
                return 0
            try:
                v = int(v)
            except (TypeError, ValueError):
                warn(f"   ⚠️ 并发检测跳过: ps 条目 parallel 值非法 ({v!r})")
                return 0
            if v <= 0:
                warn(f"   ⚠️ 并发检测跳过: ps 条目 parallel 非正数 ({v})")
                return 0
            return v
        warn("   ⚠️ 并发检测跳过: lms ps 未找到该模型的在载条目，无法核对")
    except subprocess.TimeoutExpired:
        warn("   ⚠️ 并发检测跳过: lms ps 超时，无法核对")
    except Exception as e:
        warn(f"   ⚠️ 并发检测跳过: lms ps 输出无法解析 ({e})")
    return 0


def _run_lms(lms: str, args: list, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run([lms, *args], capture_output=True, text=True,
                          timeout=timeout)


def ensure_lmstudio_model(endpoint: str, model: str,
                          load_timeout: float = 600,
                          log=None,
                          ctx_tokens: int | None = None,
                          parallel: int | None = None,
                          gpu: str = "max",
                          evict_others: bool = True) -> tuple:
    """确保 LM Studio 已按管线配置加载指定模型（含槽位切换自动卸载）。

    返回 (ok: bool, message: str)。失败时 message 为可直接展示的原因。
    对齐判定：未在载 / 已载但 ctx 与 ctx_tokens 不符 / 已载但并发与
    parallel 不符，满足任一即"卸载全部 → 带参加载"。
    """
    log = log or (lambda m: None)
    root = _root_from_endpoint(endpoint)
    if not root:
        return False, "LM Studio 接口地址为空"

    # 1) 服务器可达性 + 已加载检查
    try:
        loaded = _loaded_ids(root)
    except Exception:
        return False, f"LM Studio 未运行或无法连接（{root}）"

    # 2) 模型是否已下载（v0 API 含全部已下载模型）
    downloaded = set()
    try:
        r0 = requests.get(f"{root}/api/v0/models", timeout=5)
        downloaded = {m.get("id") for m in r0.json().get("data", [])}
    except Exception:
        pass  # 旧版无 v0 API 时跳过该检查，交给 lms load 报错

    if downloaded and model not in downloaded:
        return False, (f"模型 {model} 未在 LM Studio 中下载，"
                        f"可用模型: {', '.join(sorted(downloaded)) or '（未知）'}")

    # 3) 对齐判定（未载 / ctx 不符 / 并发不符）
    need_load = model not in loaded
    ctx_mismatch = False
    if (not need_load) and ctx_tokens:
        actual = _loaded_ctx(root, model, log=log)
        ctx_mismatch = actual > 0 and actual != int(ctx_tokens)
    parallel_mismatch = False
    parallel_actual = 0
    if (not need_load) and parallel is not None:
        parallel_actual = _loaded_parallel(_find_lms(), model, load_timeout,
                                           log=log)
        parallel_mismatch = (parallel_actual > 0
                             and parallel_actual != int(parallel))

    if not (need_load or ctx_mismatch or parallel_mismatch):
        return True, "模型已加载"

    # 4) 清场 + 带参加载
    lms = _find_lms()
    if not lms:
        return False, (f"模型 {model} 未按配置就绪，且未找到 lms CLI 无法自动加载。"
                        f"请在 LM Studio 中手动加载该模型，"
                        f"或开启 Just-in-Time 自动加载")
    try:
        if evict_others and loaded:
            _run_lms(lms, ["unload", "--all"], load_timeout)
            log(f"   ♻️ 已卸载在载模型（清场换载）: {', '.join(sorted(x for x in loaded if x))}")
        load_args = ["load", model, "-y", "--gpu", gpu]
        if ctx_tokens:
            load_args += ["-c", str(int(ctx_tokens))]
        if parallel:
            load_args += ["--parallel", str(int(parallel))]
        reload_reason = ""
        if parallel_mismatch:
            reload_reason = f"（并发 {parallel_actual}≠{int(parallel)}，重载对齐）"
        log(f"   ⏳ LM Studio 引擎对齐: 加载 {model} "
            f"(ctx={ctx_tokens}, parallel={parallel}, gpu={gpu}) {reload_reason}...")
        proc = _run_lms(lms, load_args, load_timeout)
    except subprocess.TimeoutExpired:
        return False, f"自动加载 {model} 超时（>{load_timeout:.0f}s），请手动加载"
    except OSError as e:
        return False, f"lms CLI 调用失败: {e}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return False, f"自动加载 {model} 失败: {tail or f'exit {proc.returncode}'}"

    # 5) 复核
    try:
        loaded = _loaded_ids(root)
        if model in loaded:
            if parallel is not None:
                p_actual = _loaded_parallel(lms, model, load_timeout, log=log)
                if p_actual > 0 and p_actual != int(parallel):
                    log(f"   ⚠️ 引擎未兑现并发配置: 请求 parallel={int(parallel)}，"
                        f"实际 {p_actual}（可能被引擎钳制），不二次重载")
            return True, "模型已按管线配置加载"
    except Exception:
        pass
    return False, f"自动加载命令已执行但模型仍未就绪: {model}"
