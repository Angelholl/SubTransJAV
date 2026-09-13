"""
WebView GUI 安全护栏（纯函数，无 webview/GUI 依赖，便于单元测试与 CI）。

将路径校验与 URL 协议校验从 ``api.py`` 中抽出，避免测试因依赖 pywebview
而无法在无 GUI 后端的环境（如 Linux CI）中运行。
"""

import os
from pathlib import Path, PureWindowsPath
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
    raw = path.replace("\\", "/")
    # 跨平台越界判定：Windows 盘符绝对路径（如 D:/x.csv）在非 Windows
    # 语义下不是绝对路径，resolve() 会把它折进 cwd；先按越界拒绝。
    # 本机原生绝对路径交由下方白名单裁决（Windows 行为不变）。
    if PureWindowsPath(raw).is_absolute() and not Path(raw).is_absolute():
        raise ValueError(f"路径不在允许的目录下: {raw}")
    resolved = Path(raw).resolve()

    # 锚点逃逸判定：字面锚定于仓库根的路径折叠 .. 后不得逃出仓库根，
    # 即使逃出落点仍在 home 白名单内（CI 工作区常嵌套于 home 之下）。
    anchored = REPO_ROOT in Path(raw).parents
    if anchored and not resolved.is_relative_to(REPO_ROOT.resolve()):
        raise ValueError(f"路径不在允许的目录下: {resolved}")

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


def _strip_extended_prefix(path: str) -> str:
    """还原 Windows 扩展路径前缀（``\\\\?\\`` / ``\\\\?\\UNC\\``）。

    ``Path.resolve()`` 对 ``\\\\?\\C:\\...`` 的折叠在部分 Python 版本上会
    畸变为盘符相对路径（如 ``C:Windows``），导致黑名单前缀判定漏判逃逸；
    统一先还原为常规路径再 resolve（指向同一目标，语义不变）。
    """
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _norm_case_key(p: Path) -> str:
    """路径的大小写/分隔符归一化键。

    Windows 文件系统大小写不敏感，而 ``Path.is_relative_to`` 的逐段比较
    对大小写敏感且语义随 Python 版本变化；显式做 ``normcase + casefold``
    字符串前缀判定，不依赖 pathlib 的版本相关行为（任何平台一致）。
    """
    return os.path.normcase(str(p)).casefold()


def _validate_user_directory(path: str) -> str:
    """校验用户选择的目录：允许任意磁盘目录，仅阻止系统目录与可执行文件。

    与 ``_resolve_safe_path`` 不同，此函数不限制为 home/项目根，
    而是放行用户自行选择的任意目录（含 D: 盘等），只拦截危险路径：
      1. 系统目录（SystemRoot / Program Files / ProgramData 等）；
      2. 直接指向可执行文件的路径（``os.startfile`` 会执行而非浏览）。

    加固（P3-11）：黑名单判定一律在 ``Path.resolve()``（strict=False）之后
    进行——junction/符号链接、8.3 短路径、大小写变体、``\\\\?\\`` 扩展前缀
    都先折叠/还原为真实落点，再用大小写不敏感的字符串前缀比较，
    杜绝经由链接或大小写差异绕过黑名单。

    Raises ``ValueError`` 当路径位于系统目录下，或指向可执行文件。
    """
    p = Path(_strip_extended_prefix(path)).resolve()
    system_roots = [
        os.environ.get("SystemRoot", r"C:\Windows"),  # noqa: SIM112  Windows 规范环境变量名，改大小写即行为变更
        os.environ.get("ProgramFiles", r"C:\Program Files"),  # noqa: SIM112  Windows 规范环境变量名
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),  # noqa: SIM112  Windows 规范环境变量名
        os.environ.get("ProgramData", r"C:\ProgramData"),  # noqa: SIM112  Windows 规范环境变量名
    ]
    p_key = _norm_case_key(p)
    for root in system_roots:
        if not root:
            continue
        try:
            root_key = _norm_case_key(Path(root).resolve())
        except OSError:
            continue
        prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
        if p_key == root_key or p_key.startswith(prefix):
            raise ValueError(f"不允许访问系统目录: {p}")
    if p.is_file() and p.suffix.lower() in _EXECUTABLE_EXTS:
        raise ValueError(f"不允许打开可执行文件: {p}")
    return str(p)


def is_safe_url_scheme(url: str) -> bool:
    """仅允许 http/https 链接（阻止 file://、javascript: 等）。"""
    return urlparse(url or "").scheme.lower() in ("http", "https")
