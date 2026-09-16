"""P2-7 GUI 体验包测试：字符串表 / 心跳阈值配置化 / 快捷方式安装链路。

覆盖：
- ``webview_gui.strings``：MSG 表存在性与 msg() 取值/格式化/回退行为；
- api.py / main.py 源码无英文残留文案（防回归，与验收 grep 同口径）；
- ``EventStreamParser`` 心跳超时阈值参数化（默认 15.0 行为不变）；
- ``refine.config`` 新增字段 ``heartbeat_stale_s`` 的默认值与分层覆盖
  （用户配置文件 / 环境变量 SUBTRANSJAV_HEARTBEAT_STALE_S）；
- ``首次安装.bat``（GBK 编码）调用 create_shortcut.py 的失败兜底与引号包裹；
- ``create_shortcut.py`` 不再使用 os.execv（Windows 空格路径缺陷）且
  WorkingDirectory 指向仓库根。
"""

import time
from pathlib import Path

import pytest

from subtransjav.refine import config as rc
from subtransjav.webview_gui.event_stream import (
    HEARTBEAT_STALE_S_DEFAULT,
    EventStreamParser,
)
from subtransjav.webview_gui.strings import MSG, msg

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBVIEW_GUI_DIR = REPO_ROOT / "subtransjav" / "webview_gui"


# ---------------------------------------------------------------------------
# strings.py：字符串表与 msg()
# ---------------------------------------------------------------------------

def test_msg_covers_required_keys():
    required = {
        "no_active_window", "no_folder_selected", "no_files_selected",
        "no_srt_in_folder", "folder_opened", "cannot_open_folder",
        "translation_in_progress", "translation_started",
        "translation_cancelled", "no_translation_in_progress",
        "translation_still_starting", "process_exit_code",
        "log_success", "log_cancelled", "log_exit_code",
        "version_load_failed", "window_created", "starting_webview",
        "dom_events_bound", "dom_events_bind_failed", "gui_banner",
        "appusermodelid_failed", "asset_not_found", "gui_start_failed",
    }
    assert required.issubset(MSG.keys())


def test_msg_values_are_chinese():
    for key, text in MSG.items():
        assert any("\u4e00" <= ch <= "\u9fff" for ch in text), \
            f"MSG[{key!r}] 应为中文文案: {text!r}"


def test_msg_plain_and_formatted():
    assert msg("no_active_window") == "无活动窗口"
    assert msg("translation_started", n=3) == "翻译已启动，共 3 个文件"
    assert msg("cannot_open_folder", e=ValueError("boom")) == \
        "无法打开文件夹：boom"
    assert msg("process_exit_code", code=7) == "翻译进程已退出，退出码 7"


def test_msg_fallbacks_never_raise():
    assert msg("__missing_key__") == "__missing_key__"      # 未知键回退键名
    assert msg("translation_started") == "翻译已启动，共 {n} 个文件"  # 缺参回退原文


# ---------------------------------------------------------------------------
# 英文残留防回归（与验收 grep 同口径）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel_path", [
    "api.py",
    "main.py",
    "strings.py",
])
def test_python_sources_have_no_english_ui_residue(rel_path):
    banned = [
        "No active window",
        "No folder selected",
        "No files selected",
        "No .srt files found in selected folder",
        "Folder opened",
        "Cannot open folder",
        "Translation already in progress",
        "Translation started with",
        "Translation cancelled",
        "No translation in progress",
        "Translation is still starting, try again",
        "Translation process exited with code",
        "Could not load version information",
        "Window created successfully",
        "Starting PyWebView",
        "drag-drop events bound successfully",
        "Could not bind DOM events",
        "Could not set AppUserModelID",
        "Asset file not found!",
        "Failed to start GUI!",
    ]
    text = (WEBVIEW_GUI_DIR / rel_path).read_text(encoding="utf-8")
    hits = [s for s in banned if s in text]
    assert not hits, f"{rel_path} 存在英文残留文案: {hits}"


def test_sentinel_prefixes_kept_english_in_api():
    """日志哨兵前缀保留英文标记（前端/测试依赖），后缀为中文文案。"""
    text = (WEBVIEW_GUI_DIR / "api.py").read_text(encoding="utf-8")
    assert "[SUCCESS]" in text and "msg('log_success')" in text
    assert "[CANCELLED]" in text and "msg('log_cancelled')" in text
    assert "[ERROR]" in text


# ---------------------------------------------------------------------------
# EventStreamParser：心跳阈值参数化
# ---------------------------------------------------------------------------

def test_heartbeat_stale_default_is_45():
    parser = EventStreamParser()
    assert parser.heartbeat_stale_s == pytest.approx(45.0)
    assert pytest.approx(45.0) == HEARTBEAT_STALE_S_DEFAULT
    snap = parser.snapshot()
    assert snap["heartbeat_stale_s"] == pytest.approx(45.0)
    assert snap["heartbeat_stale"] is False          # 无事件时不判超时


def test_heartbeat_stale_respects_custom_threshold():
    parser = EventStreamParser(heartbeat_stale_s=30.0)
    parser.last_event_ts = time.time() - 20.0        # 20s 前有活动
    snap = parser.snapshot()
    assert snap["heartbeat_age"] >= 19.9
    assert snap["heartbeat_stale"] is False          # 20s < 自定义 30s

    parser.heartbeat_stale_s = 15.0
    assert parser.snapshot()["heartbeat_stale"] is True


def test_snapshot_keeps_legacy_keys():
    snap = EventStreamParser().snapshot()
    for key in ("stage", "current_file", "progress", "risks", "risk_count",
                "error", "heartbeat_age", "ndjson_mode",
                "untranslated_majority"):
        assert key in snap


# ---------------------------------------------------------------------------
# config：heartbeat_stale_s 分层（默认 < 用户配置文件 < 环境变量）
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SUBTRANSJAV_HEARTBEAT_STALE_S", raising=False)
    yield tmp_path


def test_heartbeat_stale_s_default_value(_isolated_config):
    cfg = rc.RefineConfig()
    assert cfg.heartbeat_stale_s == pytest.approx(45.0)
    assert rc.resolve_tunable("heartbeat_stale_s") == pytest.approx(45.0)


def test_heartbeat_stale_s_env_override(_isolated_config, monkeypatch):
    monkeypatch.setenv("SUBTRANSJAV_HEARTBEAT_STALE_S", "30")
    assert rc.resolve_tunable("heartbeat_stale_s") == pytest.approx(30.0)


def test_heartbeat_stale_s_user_file_override(_isolated_config):
    (Path(_isolated_config) / "user_settings.json").write_text(
        '{"heartbeat_stale_s": 45}', encoding="utf-8")
    assert rc.resolve_tunable("heartbeat_stale_s") == pytest.approx(45.0)


def test_heartbeat_stale_s_not_in_manifest_fingerprint():
    """GUI 观察参数不参与产物指纹：不在 manifest 白名单字段中。"""
    from subtransjav.refine.manifest import _CONFIG_FIELDS
    assert "heartbeat_stale_s" not in _CONFIG_FIELDS


# ---------------------------------------------------------------------------
# 首次安装.bat / create_shortcut.py 安装链路
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bat_text() -> str:
    """bat 为 GBK 编码（无 BOM），按 GBK 解码。"""
    return (REPO_ROOT / "首次安装.bat").read_bytes().decode("gbk")


def test_bat_invokes_shortcut_after_install(bat_text):
    assert 'if exist "%~dp0create_shortcut.py" (' in bat_text
    assert '"%PY%" "%~dp0create_shortcut.py"' in bat_text


def test_bat_shortcut_failure_does_not_abort_install(bat_text):
    """失败分支仅 echo [WARN] 提示手动运行，不 exit /b。"""
    assert "if errorlevel 1 (" in bat_text
    assert "[WARN] 桌面快捷方式创建失败" in bat_text
    assert "可手动运行" in bat_text
    done_block = bat_text.split(":done", 1)[1]
    assert "exit /b" not in done_block


def test_bat_quotes_paths_for_spaces(bat_text):
    assert 'set "PY=.venv\\Scripts\\python.exe"' in bat_text
    assert '"%PY%" "%~dp0create_shortcut.py"' in bat_text


def test_bat_done_hint_matches_actual_shortcut_name(bat_text):
    """完成提示与 create_shortcut.py 实际创建的 SubTransJAV.lnk 一致。"""
    assert '桌面"SubTransJAV"快捷方式' in bat_text


@pytest.mark.skipif(
    not (REPO_ROOT / "create_shortcut.py").exists(),
    reason="create_shortcut.py 为内部仓安装辅助，不随公开仓发布（首次安装.bat 有 if exist 兜底）",
)
def test_create_shortcut_avoids_execv_and_sets_workdir():
    src = (REPO_ROOT / "create_shortcut.py").read_text(encoding="utf-8")
    assert "os.execv(" not in src                  # Windows 空格路径缺陷（不再调用）
    assert "subprocess.call([sys.executable] + sys.argv)" in src
    assert "shortcut.WorkingDirectory = str(project_root)" in src
    assert 'desktop / "SubTransJAV.lnk"' in src     # 幂等：固定名覆盖保存
    assert "shortcut.save()" in src


# ---------------------------------------------------------------------------
# uninstall.bat 卸载脚本（GBK 编码；全部可选清理，不删安装文件夹本身）
# ---------------------------------------------------------------------------

# create_shortcut.py 中清理的历史遗留快捷方式名（照抄源码精确字符串，
# 第二项中的分隔符为 U+00B7 中点，与源码一致）
LEGACY_LNK_NAMES = [
    "净语翻译.lnk",
    "净语翻译 · WhisperJAV Translate.lnk",
    "WhisperJAV Translate.lnk",
    "wjtranslate-gui.lnk",
]


@pytest.fixture(scope="module")
def uninstall_bat_text() -> str:
    """uninstall.bat 为 GBK 编码（无 BOM），按 GBK 解码。"""
    return (REPO_ROOT / "uninstall.bat").read_bytes().decode("gbk")


def test_uninstall_bat_exists():
    assert (REPO_ROOT / "uninstall.bat").is_file()


def test_uninstall_bat_is_gbk_decodable(uninstall_bat_text):
    """对齐首次安装.bat 的断言口径：read_bytes().decode("gbk") 可解码。"""
    assert uninstall_bat_text.startswith("@echo off")


def test_uninstall_bat_covers_all_desktop_shortcuts(uninstall_bat_text):
    assert "SubTransJAV.lnk" in uninstall_bat_text
    for name in LEGACY_LNK_NAMES:
        assert name in uninstall_bat_text, f"缺少历史遗留快捷方式名: {name}"


def test_uninstall_bat_mentions_cleanup_and_backup_targets(uninstall_bat_text):
    assert "numba_cache" in uninstall_bat_text
    assert "api_keys.bin" in uninstall_bat_text
    assert "tm.db" in uninstall_bat_text


def test_uninstall_bat_keeps_pause_for_backup_notice(uninstall_bat_text):
    """结尾必须 pause，保证用户能看到备份提醒。"""
    assert "pause" in uninstall_bat_text
