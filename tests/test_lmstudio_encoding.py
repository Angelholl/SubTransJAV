"""lmstudio 子进程编码回归测试（48h 审计 L3 收口钉）。

_run_lms 捕获 lms 子进程输出必须显式 encoding="utf-8" + errors="replace"：
缺省时 Windows 按 locale（cp936）解码，模型名/输出含非 GBK 字符会条件性
UnicodeDecodeError，且 UnicodeDecodeError 不被上层 except 捕获。本文件
mock subprocess.run 捕获 kwargs 钉死编码参数，防回退。
"""
import inspect
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.utils import lmstudio as lm  # noqa: E402


def test_run_lms_forces_utf8_replace(monkeypatch):
    """防回退钉：_run_lms 必须以 encoding="utf-8" + errors="replace"
    调 subprocess.run（输出捕获 + 文本模式为前提一并钉住）。"""
    captured = {}

    def _fake_run(args, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lm.subprocess, "run", _fake_run)
    proc = lm._run_lms("lms-fake", ["ps", "--json"], 5.0)
    assert proc.returncode == 0
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert captured.get("capture_output") is True
    assert captured.get("text") is True


def test_loaded_parallel_has_no_dead_timeout_param():
    """顺手钉（48h 审计同一处）：_loaded_parallel 的 timeout 死参数已删除
    （函数体只用模块常量 _PS_TIMEOUT_S，签名不得再收未消费的超时参数）。"""
    assert "timeout" not in inspect.signature(lm._loaded_parallel).parameters
