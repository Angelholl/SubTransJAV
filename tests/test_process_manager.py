"""process_manager 真实子进程测试（P3-10 测试补薄）。

全部用例以 ``sys.executable -c "import time; time.sleep(60)"`` 起真实子进程，
并在 finally 中自清理，确保不残留任何进程。
"""
import contextlib
import os
import subprocess
import sys
import time

import pytest

import subtransjav.utils.process_manager as pm


def _sleep_proc(seconds: int = 60) -> subprocess.Popen:
    """起一个安静睡眠的子进程（模拟翻译子进程）。"""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ensure_dead(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """兜底清理：保证测试进程树不外泄。

    venv 的 python.exe 是 launcher（真实解释器是其子进程），只杀 launcher
    会把子进程变成孤儿继续睡眠——因此先整树击杀，再回收 launcher 本体。
    """
    try:
        import psutil
        for child in psutil.Process(proc.pid).children(recursive=True):
            with contextlib.suppress(Exception):
                child.kill()
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=timeout)
    except Exception:
        pass


def _wait_alive(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """等待子进程真正起来（避免杀了个还没注册的 pid）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pm.is_process_alive(proc.pid):
            return
        if proc.poll() is not None:
            raise AssertionError("子进程提前退出，测试环境异常")
        time.sleep(0.05)
    raise AssertionError("子进程未在超时内进入存活状态")


# ---------------------------------------------------------------------------
# terminate_process_tree：有 psutil（正常路径）
# ---------------------------------------------------------------------------

def test_terminate_process_tree_kills_real_child():
    proc = _sleep_proc()
    try:
        _wait_alive(proc)
        result = pm.terminate_process_tree(proc.pid, timeout=5.0)
        assert result["success"] is True
        assert result["errors"] == []
        assert proc.pid in result["terminated"] or proc.pid in result["killed"]
        # 子进程必须真的退出
        assert proc.wait(timeout=10) is not None
        assert pm.is_process_alive(proc.pid) is False
    finally:
        _ensure_dead(proc)


def test_terminate_process_tree_kills_grandchildren():
    """父子两层进程树：terminate 后孙进程也不残留。"""
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
         "time.sleep(60)\n"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_alive(parent)
        tree = pm.get_process_tree(parent.pid)
        assert tree, "应能枚举出至少一个子进程（psutil 正常路径）"
        result = pm.terminate_process_tree(parent.pid, timeout=5.0)
        assert result["success"] is True
        # 收集到的子孙 pid 全部命中终止结果
        handled = set(result["terminated"]) | set(result["killed"]) \
            | set(result["already_dead"])
        assert set(tree) <= handled
        assert parent.wait(timeout=10) is not None
        for pid in tree:
            assert pm.is_process_alive(pid) is False, f"孙进程 {pid} 残留"
    finally:
        for pid in pm.get_process_tree(parent.pid):
            try:
                import psutil
                psutil.Process(pid).kill()
            except Exception:
                pass
        _ensure_dead(parent)


def test_terminate_process_tree_include_parent_false_spares_parent():
    """include_parent=False：根 pid 不进终止列表，只清子孙。

    注意 venv 的 python.exe 是 launcher（会派生真实解释器子进程），
    杀光子孙后 launcher 自然退出，故这里只断言列表语义；无子孙根的
    存活语义由下方用例（基础解释器直启）单独固化。
    """
    proc = _sleep_proc()
    try:
        _wait_alive(proc)
        result = pm.terminate_process_tree(proc.pid, timeout=5.0,
                                           include_parent=False)
        assert result["success"] is True
        assert proc.pid not in result["terminated"]
        assert proc.pid not in result["killed"]
    finally:
        _ensure_dead(proc)


def test_terminate_process_tree_include_parent_false_childless_root_alive():
    """include_parent=False 且根无子孙：根进程必须原样存活。

    用 sys._base_executable 直启（绕开 venv launcher 的父子间接），
    若该环境解释器仍自带子进程则跳过（无干净无子孙场景）。
    """
    base = getattr(sys, "_base_executable", None) or sys.executable
    proc = subprocess.Popen([base, "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        _wait_alive(proc)
        if pm.get_process_tree(proc.pid):
            pytest.skip("该环境解释器自带子进程（如重定向器），"
                        "无干净无子孙根可测")
        result = pm.terminate_process_tree(proc.pid, timeout=5.0,
                                           include_parent=False)
        assert result["success"] is True
        assert result["terminated"] == [] and result["killed"] == []
        assert pm.is_process_alive(proc.pid) is True
    finally:
        _ensure_dead(proc)


# ---------------------------------------------------------------------------
# kill_process_tree：立即强杀
# ---------------------------------------------------------------------------

def test_kill_process_tree_immediate_kill():
    proc = _sleep_proc()
    try:
        _wait_alive(proc)
        result = pm.kill_process_tree(proc.pid)
        assert result["success"] is True
        assert proc.pid in result["killed"]
        assert result["terminated"] == []
        assert proc.wait(timeout=10) is not None
    finally:
        _ensure_dead(proc)


# ---------------------------------------------------------------------------
# 已死 pid：already_dead 路径
# ---------------------------------------------------------------------------

def _dead_pid() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proc.wait(timeout=10)  # 确认已退出且被回收
    return proc


def test_terminate_process_tree_reports_already_dead():
    proc = _dead_pid()
    result = pm.terminate_process_tree(proc.pid)
    assert proc.pid in result["already_dead"]
    assert result["success"] is True
    assert result["terminated"] == [] and result["killed"] == []


def test_kill_process_tree_reports_already_dead():
    proc = _dead_pid()
    result = pm.kill_process_tree(proc.pid)
    assert proc.pid in result["already_dead"]
    assert result["success"] is True


def test_is_process_alive_false_for_dead_pid():
    proc = _dead_pid()
    assert pm.is_process_alive(proc.pid) is False


def test_is_process_alive_true_for_running_child():
    proc = _sleep_proc()
    try:
        _wait_alive(proc)
        assert pm.is_process_alive(proc.pid) is True
    finally:
        _ensure_dead(proc)


# ---------------------------------------------------------------------------
# 无 psutil：回退路径（ctypes _pid_alive / 明确的成功=False 契约）
# ---------------------------------------------------------------------------

def test_no_psutil_terminate_reports_failure_and_caller_fallback(monkeypatch):
    """PSUTIL_AVAILABLE=False 时 terminate_process_tree 返回失败契约，
    调用方（api.cancel_translation）据此回退 proc.terminate()——一并验证回退有效。"""
    monkeypatch.setattr(pm, "PSUTIL_AVAILABLE", False)
    proc = _sleep_proc()
    try:
        _wait_alive(proc)
        result = pm.terminate_process_tree(proc.pid)
        assert result["success"] is False
        assert any("psutil" in e.lower() for e in result["errors"])
        assert pm.is_process_alive(proc.pid) is True  # 无 psutil 不做任何终止
        # 调用方回退：proc.terminate() 后进程必须退出
        proc.terminate()
        assert proc.wait(timeout=10) is not None
    finally:
        _ensure_dead(proc)


def test_no_psutil_is_process_alive_uses_pid_alive_fallback(monkeypatch):
    """无 psutil 时 is_process_alive 走 _pid_alive（Windows ctypes 查询）。"""
    monkeypatch.setattr(pm, "PSUTIL_AVAILABLE", False)
    proc = _sleep_proc()
    try:
        _wait_alive(proc)
        assert pm.is_process_alive(proc.pid) is True
    finally:
        _ensure_dead(proc)
    # 死 pid
    dead = _dead_pid()
    assert pm.is_process_alive(dead.pid) is False


# ---------------------------------------------------------------------------
# 超时参数：子进程忽略 SIGTERM 的宽限场景（Windows TerminateProcess 无法
# 被捕获，仅 POSIX 可复现；Windows 环境跳过——强杀语义已由上方用例覆盖）
# ---------------------------------------------------------------------------

def test_terminate_timeout_grace_then_force_kill():
    if os.name == "nt":
        pytest.skip("Windows TerminateProcess 无法被进程捕获，"
                    "SIGTERM 宽限超时场景仅 POSIX 可复现")
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        _wait_alive(proc)
        result = pm.terminate_process_tree(proc.pid, timeout=0.5)
        assert proc.wait(timeout=15) is not None
        assert proc.pid in result["killed"], "忽略 SIGTERM 的进程应在超时后被强杀"
    finally:
        _ensure_dead(proc, timeout=15.0)
