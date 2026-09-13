"""
SubTransJAV PyWebView GUI Entry Point

Standalone desktop GUI for the two-stage SRT refinement pipeline
(stage A: cleanup + translation; stage B: review + polish).

Requires the [gui] extra: pip install subtransjav[gui]
"""

# ===========================================================================
# EARLY SETUP - Must be before any library imports
# ===========================================================================
import os
import subprocess
import sys
from pathlib import Path

from subtransjav.utils.console import (
    print_missing_extra_error,
    setup_console,
)

setup_console()

import platform  # noqa: E402

from subtransjav.webview_gui.strings import msg  # noqa: E402  文案表零依赖


def _deps_ok():
    """检查 GUI 核心依赖是否可导入"""
    try:
        import webview  # noqa: F401
        if platform.system() == "Windows":
            import clr  # noqa: F401
        return True
    except (ImportError, RuntimeError, SystemError):
        return False


def _auto_setup():
    """首次运行自动创建 venv 并安装依赖，然后重启到 venv 环境"""
    # PyInstaller 打包版不应走自动安装流程
    if getattr(sys, "frozen", False):
        return
    # 已在 venv 中且依赖完整 → 正常继续
    if sys.prefix != sys.base_prefix and _deps_ok():
        return

    project_root = Path(__file__).resolve().parent.parent.parent
    # Issue#5: 平台感知的 venv python 路径
    if os.name == "nt":
        venv_python = project_root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = project_root / ".venv" / "bin" / "python"

    try:
        # 创建虚拟环境（如果不存在）
        if not venv_python.exists():
            print("[SETUP] 首次运行，正在创建虚拟环境...")
            subprocess.run(
                [sys.executable, "-m", "venv", str(project_root / ".venv")],
                check=True
            )

        # Issue#4: venv已存在且依赖完整 → 跳过安装直接重启
        if venv_python.exists():
            try:
                probe = "import webview; import clr" if platform.system() == "Windows" else "import webview"
                subprocess.run(
                    [str(venv_python), "-c", probe],
                    check=True, capture_output=True, timeout=10
                )
                # 依赖已完整，直接重启
                print("[SETUP] 环境就绪，正在启动程序...")
                os.execv(str(venv_python), [str(venv_python), "-m", "subtransjav.webview_gui.main"])
            except Exception:
                pass  # 依赖不完整，继续安装

        # 安装依赖（使用 venv 中的 pip）
        print("[SETUP] 正在安装依赖，请稍候...")
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-e", ".[gui]"],
            cwd=str(project_root),
            check=True
        )

        # 安装完成后重启到 venv 环境
        print("[SETUP] 安装完成，正在重启程序...")
        try:
            os.execv(str(venv_python), [str(venv_python), "-m", "subtransjav.webview_gui.main"])
        except Exception:
            # Issue#3: fallback Popen 增加 cwd
            subprocess.Popen(
                [str(venv_python), "-m", "subtransjav.webview_gui.main"],
                cwd=str(project_root)
            )
            sys.exit(0)
    # Issue#2: 异常处理 — 友好中文错误 + 暂停
    except subprocess.CalledProcessError as e:
        print(f"\n[SETUP] 环境初始化失败: {e}")
        print("请尝试手动运行: pip install subtransjav[gui]")
        input("按回车键退出...")
        sys.exit(1)
    except Exception as e:
        print(f"\n[SETUP] 发生未知错误: {e}")
        print("请尝试手动运行: pip install subtransjav[gui]")
        input("按回车键退出...")
        sys.exit(1)


def _check_gui_dependencies():
    """Check if GUI dependencies are installed."""
    missing = []

    try:
        import webview  # noqa: F401
    except ImportError:
        missing.append("pywebview")

    if platform.system() == "Windows":
        try:
            import clr  # noqa: F401  # pythonnet 可用性探测
        except ImportError:
            missing.append("pythonnet")

    if missing:
        print_missing_extra_error(
            extra_name="gui",
            missing_packages=missing,
            feature_description="PyWebView GUI interface"
        )
        if platform.system() == "Windows":
            print("Note: On Windows, WebView2 runtime is also required.")
            print("Download from: https://developer.microsoft.com/en-us/microsoft-edge/webview2/")
        sys.exit(1)


# Issue#1: _auto_setup() 必须在 _check_gui_dependencies() 之前执行，
# 否则首次运行缺依赖时直接 sys.exit(1)，自动安装永远不可达。
# ===========================================================================
# 命令行参数（在 venv 自举与依赖检查之前解析，--help/--version 直接退出）
# ===========================================================================
import json  # noqa: E402


def _parse_args(argv=None):
    """解析 GUI 命令行参数（中文 help）。

    必须在 _auto_setup() / _check_gui_dependencies() 之前调用，
    保证 ``--help`` / ``--version`` 不触发 venv 自举与依赖检查。
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="subtransjav-gui",
        description="净语翻译 · SubTransJAV 桌面 GUI"
                    "（两阶段字幕流水线：阶段A 净语+翻译 → 阶段B 审校+抛光）")
    parser.add_argument(
        "--debug", action="store_true",
        help="以调试模式启动 WebView（可打开开发者工具）")
    parser.add_argument(
        "--version", action="store_true",
        help="打印程序版本号后退出")
    return parser.parse_args(argv)


def _print_version() -> str:
    """返回版本展示字符串（无法加载版本信息时返回 unknown）。"""
    try:
        from subtransjav.__version__ import __version_display__
        return __version_display__
    except ImportError:
        return "unknown"


APP_TITLE = "净语翻译 · SubTransJAV Translate"


def on_drop_event(e):
    """
    Handle file drops from OS into WebView.

    Uses PyWebView's pywebviewFullPath to get absolute file paths,
    bypassing browser security restrictions.
    """
    import webview
    files = e.get('dataTransfer', {}).get('files', [])
    if len(files) == 0:
        return

    paths = []
    for file in files:
        full_path = file.get('pywebviewFullPath')
        if full_path and str(full_path).lower().endswith('.srt'):
            paths.append(full_path)

    if not paths:
        return

    # 登记拖放路径，纳入 scan_resume_states 的会话信任边界
    from .api import register_session_paths
    register_session_paths(paths)

    try:
        window = webview.windows[0]
        paths_json = json.dumps(paths)
        window.evaluate_js(f"FileListManager.addDroppedFiles({paths_json})")
    except Exception as ex:
        print(msg("drop_event_error", e=ex))


def bind_dom_events(window):
    """Bind drag-drop events to window DOM after creation."""
    from webview.dom import DOMEventHandler  # noqa: E402  延迟导入 GUI 依赖
    try:
        window.dom.document.events.dragenter += DOMEventHandler(lambda e: None, True, True)
        window.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True)
        window.dom.document.events.drop += DOMEventHandler(on_drop_event, True, True)
        print(msg("dom_events_bound"))
    except Exception as ex:
        print(msg("dom_events_bind_failed", e=ex))
        print(msg("dom_events_fallback"))


def get_asset_path(relative_path: str) -> Path:
    """Get absolute path to an asset file (dev mode or PyInstaller bundle)."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS)
        asset_path = base_path / "webview_gui_assets" / relative_path
    else:
        base_path = Path(__file__).parent
        asset_path = base_path / "assets" / relative_path

    if not asset_path.exists():
        raise FileNotFoundError(
            f"Asset file not found: {asset_path}\n"
            f"Relative path requested: {relative_path}"
        )
    return asset_path


def check_webview2_windows():
    """Check if WebView2 runtime is installed on Windows."""
    if platform.system() != 'Windows':
        return True

    try:
        import winreg
        key_paths = [
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        ]
        for key_path in key_paths:
            try:
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path).Close()
                return True
            except FileNotFoundError:
                continue
        return False
    except Exception as e:
        print(msg("webview2_check_failed", e=e))
        return True


def show_webview2_error():
    """Show user-friendly error dialog if WebView2 is missing."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "WebView2 Required",
            "WebView2 Runtime is required but not installed.\n\n"
            "Please download and install it from:\n"
            "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
            "After installation, restart the application."
        )
        root.destroy()
    except Exception:
        print("\nERROR: WebView2 Runtime Required.")
        print("Download: https://go.microsoft.com/fwlink/p/?LinkId=2124703")


def create_window():
    """Create and configure the PyWebView window."""
    import webview

    from .api import TranslateAPI

    html_path = get_asset_path("index.html")
    api = TranslateAPI()

    icon_path = None
    if os.getenv('SUBTRANSJAV_NO_ICON', '').lower() not in ('1', 'true', 'yes'):
        icon_file = Path(__file__).parent / "assets" / "icon.ico"
        if icon_file.exists():
            icon_path = icon_file

    width_s, height_s = int(1920 * 0.63), int(1080 * 0.85)

    window_kwargs = {
        'title': APP_TITLE,
        'url': str(html_path),
        'js_api': api,
        'width': width_s,
        'height': height_s,
        'resizable': True,
        'frameless': False,
        'easy_drag': True,
        'text_select': True,
        'min_size': (820, 600)
    }

    if icon_path:
        try:
            import inspect
            if 'icon' in inspect.signature(webview.create_window).parameters:
                window_kwargs['icon'] = str(icon_path)
        except Exception:
            pass

    window = webview.create_window(**window_kwargs)
    return window


def main():
    """Entry point for subtransjav-gui."""
    # --help / --version 在此直接退出，不触发 venv 自举与依赖检查
    args = _parse_args()
    if args.version:
        print(_print_version())
        return

    _auto_setup()
    _check_gui_dependencies()

    # 延迟导入：--help/--version 路径不要求 GUI 依赖已安装
    import logging

    import webview  # noqa: E402

    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    logging.getLogger('bottle').setLevel(logging.ERROR)

    version = _print_version()
    print(msg("gui_banner", version=version))
    print("=" * 50)

    if not check_webview2_windows():
        show_webview2_error()
        sys.exit(1)

    # Set Windows AppUserModelID so the taskbar groups this app separately
    if platform.system() == 'Windows':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'SubTransJAV.Translate.GUI.v1')
        except Exception as e:
            print(msg("appusermodelid_failed", e=e))

    try:
        window = create_window()
        print(msg("window_created"))

        # --debug 透传 webview.start；环境变量 SUBTRANSJAV_DEBUG 仍然有效
        debug_mode = args.debug or os.getenv(
            'SUBTRANSJAV_DEBUG', '').lower() in ('1', 'true', 'yes')
        print(msg("starting_webview", debug=debug_mode))

        # private_mode=True avoids WebView2 disk-cache staleness;
        # all user settings persist via backend files, not localStorage.
        webview.start(debug=debug_mode, private_mode=True,
                      func=lambda: bind_dom_events(window))

    except FileNotFoundError as e:
        print(f"\n{msg('asset_not_found')}")
        print(str(e))
        sys.exit(1)
    except Exception as e:
        print(f"\n{msg('gui_start_failed')}")
        print(f"{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
