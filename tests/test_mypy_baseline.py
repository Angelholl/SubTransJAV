"""mypy_baseline 测试（2026-09-26 审计 M1 项：退出码校验契约）。

不真跑 mypy：monkeypatch tools.mypy_baseline.run_mypy 返回 (rc, output)
元组，基线文件指到 tmp_path。覆盖契约四分支（详见 tools/mypy_baseline.py
docstring）：
1. rc=0 → --check 退出 0；
2. rc=1 + 可解析错误行且基线含全部当前错误 → --check 退出 0；
3. rc=1 + 零错误行 → 退出 1（假绿防护，典型=No module named mypy）；
4. rc=2 → 退出 1，且 --update 拒绝写基线文件。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools.mypy_baseline as mb  # noqa: E402

ARGV_CHECK = ["mypy_baseline.py", "--check"]
ARGV_UPDATE = ["mypy_baseline.py", "--update"]


def _patch(monkeypatch, tmp_path: Path, rc: int, output: str) -> Path:
    """替换 run_mypy 与基线路径，返回 tmp 下的基线文件路径。"""
    monkeypatch.setattr(mb, "run_mypy", lambda: (rc, output))
    baseline = tmp_path / "mypy-baseline.txt"
    monkeypatch.setattr(mb, "BASELINE_FILE", baseline)
    return baseline


def test_check_rc0_passes(monkeypatch, tmp_path):
    """rc=0（无错）→ --check 退出 0。"""
    _patch(monkeypatch, tmp_path, 0, "")
    monkeypatch.setattr(sys, "argv", ARGV_CHECK)
    assert mb.main() == 0


def test_check_rc1_errors_in_baseline_passes(monkeypatch, tmp_path):
    """rc=1 + 可解析错误行且基线含全部当前错误 → --check 退出 0。"""
    output = ("subtransjav/foo.py:1: error: x [attr-defined]\n"
              "subtransjav/bar.py:2: error: y [union-attr]\n")
    baseline = _patch(monkeypatch, tmp_path, 1, output)
    baseline.write_text(
        "subtransjav/foo.py:1: [attr-defined]\n"
        "subtransjav/bar.py:2: [union-attr]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ARGV_CHECK)
    assert mb.main() == 0


def test_check_rc1_zero_error_lines_fails(monkeypatch, tmp_path):
    """rc=1 但零错误行（典型=No module named mypy）→ 假绿防护退出 1。"""
    _patch(monkeypatch, tmp_path, 1, "No module named mypy\n")
    monkeypatch.setattr(sys, "argv", ARGV_CHECK)
    assert mb.main() == 1


def test_rc2_environment_failure_no_baseline_write(monkeypatch, tmp_path):
    """rc=2（环境故障）→ 退出 1，且 --update 拒绝写基线文件。"""
    baseline = _patch(monkeypatch, tmp_path, 2, "boom")
    monkeypatch.setattr(sys, "argv", ARGV_UPDATE)
    assert mb.main() == 1
    assert not baseline.exists()
    monkeypatch.setattr(sys, "argv", ARGV_CHECK)
    assert mb.main() == 1
    assert not baseline.exists()
