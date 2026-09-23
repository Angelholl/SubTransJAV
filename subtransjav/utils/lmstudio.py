"""
LM Studio 预检与自动加载
========================
解决：中途停止任务并卸载模型后，续跑因"模型未加载"而失败且无提示。

策略：
  1. 查询 /v1/models（仅含已加载模型），目标模型在列 → 检查引擎参数是否对齐
  2. 未对齐（未加载 / ctx 不符 / 挂了 draft 但无法确认已生效）→
     先 `lms unload --all` 清场（16GB 单卡装不下两个大模型），再
     `lms load` 带参加载（-y --gpu max -c <ctx> --parallel <并发>，可选
     --speculative-draft-simple --speculative-draft-model <draft>）
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

_PS_TIMEOUT_S = 30.0


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


def _loaded_ctx(root: str, model: str) -> int:
    """读取已载模型的实际上下文长度；拿不到（旧版无 v0 API / 字段缺失）返回 0。

    v0 API 字段名无正式兼容承诺，按 observed 命名取值并全面容错：
    读不到 ≠ 0 不对齐，宁可放过也不误判重载。
    """
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
            break
    except Exception:
        pass
    return 0


# 进程内记忆：本进程刚以 draft 旗标成功加载的 target → draft ID。
# lms ps 是否暴露 draft 信息无 schema 承诺：有此缓存，重载至多每进程一次，
# 不随 ps 探测能力漂移（同进程内只有本模块负责加载模型）。
_DRAFT_LOADED: dict = {}


def _has_draft_trace(obj) -> bool:
    """递归找 spec/draft 相关键（键名含 spec/draft 且值真）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if ("spec" in kl or "draft" in kl) and v:
                return True
            if _has_draft_trace(v):
                return True
    elif isinstance(obj, list):
        return any(_has_draft_trace(x) for x in obj)
    return False


def _spec_draft_trace(lms: str, model: str) -> bool | None:
    """best-effort：lms ps --json 里找该模型的投机解码痕迹。

    返回 True=条目匹配且含 spec/draft 痕迹；False=条目匹配但无痕迹；
    None=ps 不可用/解析失败/找不到该模型条目（schema 无承诺，无法判定）。
    匹配字段不承诺：modelKey/identifier/id/path/displayName 任一包含即命中
    （实测返回 modelKey 且无 id 字段——2026-09-23 ftkd-030 挂载失败教训）。
    """
    if not lms:
        return None
    try:
        proc = subprocess.run([lms, "ps", "--json"], capture_output=True,
                              text=True, timeout=_PS_TIMEOUT_S)
        if proc.returncode != 0:
            return None
        entries = json.loads(proc.stdout or "{}")
        data = entries.get("data") if isinstance(entries, dict) else entries
        for entry in data or []:
            if not isinstance(entry, dict):
                continue
            haystack = " ".join(str(entry.get(k, "")) for k in
                                ("modelKey", "identifier", "id", "path",
                                 "displayName"))
            if model in haystack:
                return _has_draft_trace(entry)
        return None     # 找不到该模型条目：无法判定（不据此放行）
    except Exception:
        return None


def _run_lms(lms: str, args: list, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run([lms, *args], capture_output=True, text=True,
                          timeout=timeout)


def ensure_lmstudio_model(endpoint: str, model: str,
                          load_timeout: float = 600,
                          log=None,
                          ctx_tokens: int | None = None,
                          parallel: int | None = None,
                          gpu: str = "max",
                          draft_model: str = "",
                          evict_others: bool = True) -> tuple:
    """确保 LM Studio 已按管线配置加载指定模型（含槽位切换自动卸载）。

    返回 (ok: bool, message: str)。失败时 message 为可直接展示的原因。
    对齐判定：未在载 / 已载但 ctx 与 ctx_tokens 不符 / 配置了 draft 但
    无法确认已生效（best-effort），满足任一即"卸载全部 → 带参加载"。
    """
    log = log or (lambda m: None)
    root = _root_from_endpoint(endpoint)
    if not root:
        return False, "LM Studio 接口地址为空"
    draft_model = (draft_model or "").strip()

    # 1) 服务器可达性 + 已加载检查
    try:
        loaded = _loaded_ids(root)
    except Exception:
        return False, f"LM Studio 未运行或无法连接（{root}）"

    # 2) 模型/draft 是否已下载（v0 API 含全部已下载模型）
    downloaded = set()
    try:
        r0 = requests.get(f"{root}/api/v0/models", timeout=5)
        downloaded = {m.get("id") for m in r0.json().get("data", [])}
    except Exception:
        pass  # 旧版无 v0 API 时跳过该检查，交给 lms load 报错

    if downloaded and model not in downloaded:
        return False, (f"模型 {model} 未在 LM Studio 中下载，"
                       f"可用模型: {', '.join(sorted(downloaded)) or '（未知）'}")
    if draft_model and downloaded and draft_model not in downloaded:
        log(f"   ⚠️ draft 模型 {draft_model} 未下载，本次不挂投机解码"
            f"（不影响质量，仅无提速）")
        draft_model = ""

    # 3) 对齐判定
    need_load = model not in loaded
    ctx_mismatch = False
    if (not need_load) and ctx_tokens:
        actual = _loaded_ctx(root, model)
        ctx_mismatch = actual > 0 and actual != int(ctx_tokens)
    spec_unverified = False
    if (not need_load) and draft_model:
        if _DRAFT_LOADED.get(model) == draft_model:
            pass    # 本进程刚以此 draft 加载成功，视为已挂载
        else:
            # True=确认已挂；False=确认未挂；None=无法判定——除确认已挂外重载
            spec_unverified = _spec_draft_trace(_find_lms(), model) is not True

    if not (need_load or ctx_mismatch or spec_unverified):
        if not draft_model:
            _DRAFT_LOADED.pop(model, None)      # 配置改回不挂 draft：清缓存防陈旧
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
        if draft_model:
            load_args += ["--speculative-draft-simple",
                          "--speculative-draft-model", draft_model]
        log(f"   ⏳ LM Studio 引擎对齐: 加载 {model} "
            f"(ctx={ctx_tokens}, parallel={parallel}, gpu={gpu}"
            f"{', draft=' + draft_model if draft_model else ''}) ...")
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
            if draft_model:
                _DRAFT_LOADED[model] = draft_model
            else:
                _DRAFT_LOADED.pop(model, None)
            return True, "模型已按管线配置加载"
    except Exception:
        pass
    return False, f"自动加载命令已执行但模型仍未就绪: {model}"
