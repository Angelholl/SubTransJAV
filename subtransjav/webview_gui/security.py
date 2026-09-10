"""
WebView GUI 安全护栏（纯函数，无 webview/GUI 依赖，便于单元测试与 CI）。

将路径校验与 URL 协议校验从 ``api.py`` 中抽出，避免测试因依赖 pywebview
而无法在无 GUI 后端的环境（如 Linux CI）中运行。
"""

import os
from pathlib import Path
from urllib.parse import urlparse

# Project root (subtransjav/webview_gui/security.py -> project root)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_safe_path(path: str) -> Path:
    """Resolve *path* and verify it lives under an allowed root.

    Allowed roots:
      1. The user's home directory (``Path.home()``).
      2. The repository root (``REPO_ROOT``).

    Raises ``ValueError`` when the path is outside every allowed root.
    """
    resolved = Path(path).resolve()

    try:
        resolved.relative_to(Path.home())
        return resolved
    except ValueError:
        pass

    try:
        resolved.relative_to(REPO_ROOT.resolve())
        return resolved
    except ValueError:
        pass

    raise ValueError(f"路径不在允许的目录下: {resolved}")


_EXECUTABLE_EXTS = {".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".ps1", ".vbs", ".js", ".jar"}


def _validate_user_directory(path: str) -> str:
    """校验用户选择的目录：允许任意磁盘目录，仅阻止系统目录与可执行文件。

    与 ``_resolve_safe_path`` 不同，此函数不限制为 home/项目根，
    而是放行用户自行选择的任意目录（含 D: 盘等），只拦截危险路径：
      1. 系统目录（SystemRoot / Program Files / ProgramData 等）；
      2. 直接指向可执行文件的路径（``os.startfile`` 会执行而非浏览）。

    Raises ``ValueError`` 当路径位于系统目录下，或指向可执行文件。
    """
    p = Path(path).resolve()
    system_roots = [
        os.environ.get("SystemRoot", r"C:\Windows"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramData", r"C:\ProgramData"),
    ]
    for root in system_roots:
        if not root:
            continue
        try:
            if p.is_relative_to(root):
                raise ValueError(f"不允许访问系统目录: {p}")
        except OSError:
            pass
    if p.is_file() and p.suffix.lower() in _EXECUTABLE_EXTS:
        raise ValueError(f"不允许打开可执行文件: {p}")
    return str(p)


def is_safe_url_scheme(url: str) -> bool:
    """仅允许 http/https 链接（阻止 file://、javascript: 等）。"""
    return urlparse(url or "").scheme.lower() in ("http", "https")
