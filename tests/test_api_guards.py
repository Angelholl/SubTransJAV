# -*- coding: utf-8 -*-
"""webview_gui 安全护栏单元测试（P1：URL scheme + 路径校验）

这些纯函数位于 webview_gui.security，无 pywebview 依赖，可在无 GUI 后端的
CI（如 ubuntu-latest）中运行。
"""
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
