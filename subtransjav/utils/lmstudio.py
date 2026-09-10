"""
LM Studio 预检与自动加载
========================
解决：中途停止任务并卸载模型后，续跑因"模型未加载"而失败且无提示。

策略：
  1. 查询 /v1/models（仅含已加载模型），目标模型在列 → 直接通过
  2. 未加载 → 优先用官方 `lms load` CLI 自动加载（LM Studio 桌面版自带）
  3. lms 不可用 → 给出明确的人工处理指引
"""

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


def ensure_lmstudio_model(endpoint: str, model: str,
                          load_timeout: float = 600,
                          log=None) -> tuple:
    """确保 LM Studio 已加载指定模型。

    返回 (ok: bool, message: str)。失败时 message 为可直接展示的原因。
    """
    log = log or (lambda m: None)
    root = _root_from_endpoint(endpoint)
    if not root:
        return False, "LM Studio 接口地址为空"

    # 1) 服务器可达性 + 已加载检查
    try:
        r = requests.get(f"{root}/v1/models", timeout=5)
        loaded = {m.get("id") for m in r.json().get("data", [])}
    except Exception:
        return False, f"LM Studio 未运行或无法连接（{root}）"

    if model in loaded:
        return True, "模型已加载"

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

    # 3) lms load 自动加载
    lms = _find_lms()
    if not lms:
        return False, (f"模型 {model} 未加载，且未找到 lms CLI 无法自动加载。"
                       f"请在 LM Studio 中手动加载该模型，"
                       f"或开启 Just-in-Time 自动加载")
    log(f"   ⏳ 检测到模型未加载，自动加载 {model} ...")
    try:
        proc = subprocess.run([lms, "load", model], capture_output=True,
                              text=True, timeout=load_timeout)
    except subprocess.TimeoutExpired:
        return False, f"自动加载 {model} 超时（>{load_timeout:.0f}s），请手动加载"
    except OSError as e:
        return False, f"lms CLI 调用失败: {e}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return False, f"自动加载 {model} 失败: {tail or f'exit {proc.returncode}'}"

    # 4) 复核
    try:
        r = requests.get(f"{root}/v1/models", timeout=5)
        loaded = {m.get("id") for m in r.json().get("data", [])}
        if model in loaded:
            return True, "模型已自动加载"
    except Exception:
        pass
    return False, f"自动加载命令已执行但模型仍未就绪: {model}"
