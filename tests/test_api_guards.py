"""webview_gui 安全护栏单元测试（P1：URL scheme + 路径校验）

这些纯函数位于 webview_gui.security，无 pywebview 依赖，可在无 GUI 后端的
CI（如 ubuntu-latest）中运行。
"""
import contextlib
import os
from pathlib import Path

import pytest

from subtransjav.webview_gui.security import (
    REPO_ROOT,
    _resolve_safe_path,
    _validate_user_directory,
    is_safe_url_scheme,
)


def test_safe_path_allows_home():
    p = _resolve_safe_path(os.path.join(str(Path.home()), "foo", "bar.srt"))
    assert str(p).endswith(os.path.join("foo", "bar.srt"))


def test_safe_path_allows_repo_root():
    p = _resolve_safe_path(str(REPO_ROOT / "config" / "glossary.csv"))
    assert p == (REPO_ROOT / "config" / "glossary.csv").resolve()


def test_safe_path_rejects_traversal():
    with pytest.raises(ValueError):
        _resolve_safe_path(os.path.join(str(REPO_ROOT), "..", "..", "Windows", "System32"))


def test_url_scheme_http_https_allowed():
    assert is_safe_url_scheme("https://example.com")
    assert is_safe_url_scheme("http://example.com")


def test_url_scheme_file_rejected():
    assert not is_safe_url_scheme("file:///C:/Windows/System32/calc.exe")


def test_url_scheme_javascript_rejected():
    assert not is_safe_url_scheme("javascript:alert(1)")


def test_url_scheme_empty_rejected():
    assert not is_safe_url_scheme("")
    assert not is_safe_url_scheme(None)


def test_validate_user_directory_allows_normal_dir(tmp_path):
    d = tmp_path / "movies"
    d.mkdir()
    assert _validate_user_directory(str(d)) == str(d.resolve())


def test_validate_user_directory_blocks_executable(tmp_path):
    exe = tmp_path / "evil.exe"
    exe.write_text("fake", encoding="utf-8")
    with pytest.raises(ValueError):
        _validate_user_directory(str(exe))


def test_validate_user_directory_blocks_system_dir(tmp_path, monkeypatch):
    sysroot = tmp_path / "sysroot"
    sysroot.mkdir()
    monkeypatch.setenv("SystemRoot", str(sysroot))
    sub = sysroot / "System32"
    sub.mkdir()
    with pytest.raises(ValueError):
        _validate_user_directory(str(sub))


def test_validate_user_directory_allows_non_executable_file(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    f = d / "note.txt"
    f.write_text("x", encoding="utf-8")
    assert _validate_user_directory(str(f)) == str(f.resolve())


# ---------------------------------------------------------------------------
# P3-11 守卫回归加固：resolve() 后大小写不敏感黑名单前缀判定
# 覆盖：junction 逃逸 / \\?\ 扩展前缀 / 8.3 短路径 / 大小写变体 / 合法放行
# ---------------------------------------------------------------------------

def test_norm_case_key_is_case_insensitive():
    """归一化键显式大小写不敏感（不依赖 Path.is_relative_to 的版本语义）。"""
    from subtransjav.webview_gui.security import _norm_case_key
    assert _norm_case_key(Path("C:/Windows")) == _norm_case_key(Path("c:/windows"))
    assert _norm_case_key(Path("C:/Windows")) == _norm_case_key(Path("C:\\Windows"))


def test_validate_user_directory_blocks_case_variant_real_sysdir():
    """大小写变体 c:\\WiNdOwS 必须被拒绝（resolve+casefold 双保险）。"""
    if os.name != "nt":
        pytest.skip("系统目录大小写变体仅 Windows 有意义")
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")  # noqa: SIM112  Windows 规范环境变量名
    variant = sysroot.swapcase()
    with pytest.raises(ValueError):
        _validate_user_directory(variant)
    with pytest.raises(ValueError):
        _validate_user_directory(variant + r"\System32")


def test_validate_user_directory_blocks_case_variant_fake_sysdir(tmp_path, monkeypatch):
    """黑名单根目录本身为混合大小写时，任意大小写拼写的子路径都拒绝。"""
    sysroot = tmp_path / "MiXeD-Case-SysRoot"
    sysroot.mkdir()
    monkeypatch.setenv("SystemRoot", str(sysroot))
    sub = sysroot / "drivers"
    sub.mkdir()
    with pytest.raises(ValueError):
        _validate_user_directory(str(sub))
    with pytest.raises(ValueError):
        _validate_user_directory(str(sysroot).swapcase() + os.sep + "no-such-dir")


def test_validate_user_directory_blocks_extended_prefix():
    r"""\\?\ 扩展前缀路径（\\?\C:\Windows）必须拒绝。

    resolve() 对该前缀的折叠存在畸变（可能折成盘符相对路径 C:Windows），
    守卫需先还原前缀再校验，否则前缀判定漏判逃逸。
    """
    if os.name != "nt":
        pytest.skip("扩展路径前缀仅 Windows 有意义")
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")  # noqa: SIM112  Windows 规范环境变量名
    with pytest.raises(ValueError):
        _validate_user_directory("\\\\?\\" + sysroot)
    with pytest.raises(ValueError):
        _validate_user_directory("\\\\?\\" + sysroot + r"\System32")


def test_validate_user_directory_blocks_junction_escape(tmp_path, monkeypatch):
    """指向系统目录的 junction 必须在 resolve() 后被识别并拒绝。"""
    if os.name != "nt":
        pytest.skip("junction 仅 Windows 有意义")
    try:
        import _winapi
    except ImportError:
        pytest.skip("当前环境无 _winapi 模块")
    sysroot = tmp_path / "SysRoot"
    sysroot.mkdir()
    monkeypatch.setenv("SystemRoot", str(sysroot))
    link = tmp_path / "jump_link"
    try:
        _winapi.CreateJunction(str(sysroot), str(link))
    except Exception:
        pytest.skip("当前环境不支持 _winapi.CreateJunction")
    try:
        # junction 本身与 junction 下的子路径都必须拒绝
        with pytest.raises(ValueError):
            _validate_user_directory(str(link))
        with pytest.raises(ValueError):
            _validate_user_directory(str(link / "no-such-sub"))
    finally:
        with contextlib.suppress(Exception):
            _winapi.DeleteJunction(str(link))


def test_validate_user_directory_blocks_short_path():
    """8.3 短路径（PROGRA~1）经 resolve() 展开后必须命中黑名单。"""
    if os.name != "nt":
        pytest.skip("8.3 短路径仅 Windows 有意义")
    import ctypes
    import ctypes.wintypes  # noqa: F401  确保 windll 类型解析完整
    prog = os.environ.get("ProgramFiles", r"C:\Program Files")  # noqa: SIM112  Windows 规范环境变量名
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(prog, buf, 1024)
    if not n or buf.value == prog:
        pytest.skip("8.3 短文件名未启用或未能生成短路径")
    with pytest.raises(ValueError):
        _validate_user_directory(buf.value)


def test_validate_user_directory_still_allows_user_dirs(tmp_path):
    """加固不误伤：普通用户目录（含不存在的子目录）仍放行。"""
    d = tmp_path / "UserVideos"
    d.mkdir()
    assert _validate_user_directory(str(d)) == str(d.resolve())
    # 尚未创建的目录路径（对话框常见：用户手输路径）也放行
    pending = d / "new-season"
    assert _validate_user_directory(str(pending)) == str(pending.resolve())
