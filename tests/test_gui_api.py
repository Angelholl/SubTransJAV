"""webview_gui.api 浅层测试（P3-10/P3-11）。

不启动任何窗口：
- ``_build_refine_args`` 为模块级纯函数，直接调用；
- ``scan_resume_states`` 不依赖实例状态，用 ``object.__new__`` 构造实例，
  规避 ``__init__`` 的副作用（Documents 建目录 / atexit 注册）；
- URL/endpoint 守卫入口在发起任何网络请求之前即短路，无网络副作用。
"""
import os
import shutil
import uuid
from pathlib import Path

import pytest

pytest.importorskip("webview", reason="pywebview 为可选 gui extra，未安装时跳过 GUI API 测试", exc_type=ImportError)

from subtransjav.webview_gui.api import (
    SESSION_SELECTED_PATHS,
    TranslateAPI,
    _build_refine_args,
    register_session_paths,
)


@pytest.fixture()
def gui_api_obj():
    """无副作用的 TranslateAPI 实例 + 干净的会话路径登记表。"""
    SESSION_SELECTED_PATHS.clear()
    yield object.__new__(TranslateAPI)
    SESSION_SELECTED_PATHS.clear()


# ---------------------------------------------------------------------------
# _build_refine_args：CLI 参数装配（模块级纯函数）
# ---------------------------------------------------------------------------

def test_build_refine_args_resume_flag():
    args = _build_refine_args({"inputs": ["a.srt"], "resume": True})
    assert "--resume" in args


def test_build_refine_args_no_resume_by_default():
    args = _build_refine_args({"inputs": ["a.srt"]})
    assert "--resume" not in args


def test_build_refine_args_always_ndjson_event_format():
    """GUI 子进程恒以 ndjson 事件流输出（GUI 侧解析依赖）。"""
    for options in ({"inputs": ["a.srt"]},
                    {"inputs": ["a.srt"], "resume": True},
                    {"inputs": ["a.srt"], "verbose": True}):
        args = _build_refine_args(options)
        i = args.index("--event-format")
        assert args[i + 1] == "ndjson"


def test_build_refine_args_dash_prefixed_input_guard():
    """以 '-' 开头的文件名必须改用 --input= 形式，防止被解析成 CLI 旗标。"""
    p = "-weird-name.srt"
    args = _build_refine_args({"inputs": [p, "normal.srt"]})
    assert f"--input={p}" in args
    assert "-i" in args
    assert args[args.index("-i") + 1] == "normal.srt"


# ---------------------------------------------------------------------------
# scan_resume_states：断点恢复三态 + 信任边界（未登记路径跳过）
# ---------------------------------------------------------------------------

def _prepare_states(tmp_path):
    """造三个输入：ep01 有终稿（completed）、ep02 有清单（resumable）、
    ep03 什么都没有（none）。返回对应的 srt 路径列表。"""
    (tmp_path / "ep01_final_cn.srt").write_text("终稿", encoding="utf-8")
    (tmp_path / "ep02_manifest.json").write_text("{}", encoding="utf-8")
    return [str(tmp_path / f"ep0{n}.srt") for n in (1, 2, 3)]


def test_scan_resume_states_three_states(tmp_path, gui_api_obj):
    paths = _prepare_states(tmp_path)
    register_session_paths(paths)
    results = gui_api_obj.scan_resume_states(paths)
    by_stem = {r["stem"]: r["state"] for r in results}
    assert by_stem == {"ep01": "completed", "ep02": "resumable", "ep03": "none"}


def test_scan_resume_states_skips_unregistered_paths(tmp_path, gui_api_obj):
    """信任边界：未经理受信入口登记的路径一律跳过，不做任意路径解析。"""
    paths = _prepare_states(tmp_path)  # ep01 本可返回 completed
    assert gui_api_obj.scan_resume_states(paths) == []


def test_scan_resume_states_skips_invalid_entries(tmp_path, gui_api_obj):
    """None / 空串等非法条目静默跳过，不抛异常。"""
    good = str(tmp_path / "ep01.srt")
    (tmp_path / "ep01_final_cn.srt").write_text("终稿", encoding="utf-8")
    register_session_paths([good])
    results = gui_api_obj.scan_resume_states([None, "", 123, good])
    assert [r["stem"] for r in results] == ["ep01"]


# ---------------------------------------------------------------------------
# URL / endpoint 守卫（P3-11 回归）：入口在触网前短路
# 设计决策：仅校验 scheme（http/https），不拦截 localhost/私有地址——
# 连接本地 LM Studio/Ollama 是本工具的核心功能。
# ---------------------------------------------------------------------------

def test_open_url_rejects_unsafe_schemes(gui_api_obj):
    for url in ("file:///C:/Windows/System32/calc.exe",
                "javascript:alert(1)",
                "ftp://example.com/x", ""):
        result = gui_api_obj.open_url(url)
        assert result["success"] is False, url
        assert "http/https" in result["error"]


def test_refine_list_models_rejects_non_http_endpoint(gui_api_obj):
    result = gui_api_obj.refine_list_models("lmstudio",
                                            endpoint="file:///etc/passwd")
    assert result["success"] is False
    assert "http/https" in result["error"]


def test_refine_test_stage_rejects_non_http_endpoint(gui_api_obj):
    result = gui_api_obj.refine_test_stage("lmstudio", "some-model",
                                           endpoint="javascript:alert(1)")
    assert result["success"] is False
    assert "http/https" in result["error"]


# ---------------------------------------------------------------------------
# 角色卡目录守卫（反路径穿越）：refine_get_template / refine_save_template
# 仅放行 服务端默认目录 或 本会话登记的用户自选目录，其余一律拒绝。
# ---------------------------------------------------------------------------

def test_refine_get_template_rejects_traversal_payload(gui_api_obj):
    """前端传入相对穿越载荷必须在 open() 之前被守卫短路拒绝。"""
    sep = os.sep
    payload = (".." + sep + ".." + sep + "config" + sep
               + ("api_keys" + chr(46) + "bin"))
    result = gui_api_obj.refine_get_template("A", payload)
    assert result["success"] is False


def test_refine_save_template_rejects_abs_beyond_anchor(gui_api_obj):
    """未登记且越出 home/仓库根锚点的绝对路径必须拒绝写入。"""
    outside = os.path.join(os.path.expanduser("~"), os.pardir,
                           os.pardir, "beyond_anchor_cards")
    result = gui_api_obj.refine_save_template("A", "测试内容", outside)
    assert result["success"] is False
    assert not os.path.exists(outside), "守卫应在建目录/写文件之前拒绝"


def test_refine_template_registered_dir_still_works(gui_api_obj):
    """不误伤正常路径：本会话经对话框登记的自选目录读写角色卡仍放行。

    目录锚在用户主目录之下（_resolve_safe_path 的 home 白名单内），
    且测试产物在 finally 中清理，不残留。
    """
    d = Path.home() / ("subtransjav_tpl_test_" + uuid.uuid4().hex[:8])
    register_session_paths([str(d)])
    try:
        saved = gui_api_obj.refine_save_template("A", "卡片内容", str(d))
        assert saved["success"] is True
        loaded = gui_api_obj.refine_get_template("A", str(d))
        assert loaded["success"] is True
        assert loaded["text"] == "卡片内容"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_refine_get_template_default_dir_still_works(gui_api_obj):
    """不误伤主路径：不传目录时走服务端默认 config/templates。"""
    result = gui_api_obj.refine_get_template("A", None)
    assert result["success"] is True


# ---------------------------------------------------------------------------
# 词库保存别名保留（v1.2.2 P2）：GUI 前端只收集两列行，保存时后端必须
# 回填旧库 target_aliases 第三列，不得静默抹掉。词库锚在用户主目录下
# （_resolve_safe_path 的 home 白名单内），测试产物在 finally 中清理。
# ---------------------------------------------------------------------------

def _home_glossary_dir() -> Path:
    d = Path.home() / ("subtransjav_gl_test_" + uuid.uuid4().hex[:8])
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_refine_save_glossary_keeps_existing_aliases(gui_api_obj):
    """旧库含别名：前端传两列行保存后，读回别名仍在第三列。"""
    d = _home_glossary_dir()
    try:
        p = d / "glossary.csv"
        p.write_text("ムラムラ,心痒,燥热|悸动\nIKU,去了\n", encoding="utf-8-sig")
        saved = gui_api_obj.refine_save_glossary(
            [["ムラムラ", "心痒"], ["IKU", "去了"]], str(p))
        assert saved["success"] is True
        assert saved["alias_kept"] == 1          # 仅一条带别名
        got = gui_api_obj.refine_get_glossary(str(p))
        assert got["success"] is True
        assert got["rows"] == [["ムラムラ", "心痒", "燥热|悸动"],
                               ["IKU", "去了", ""]]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_refine_save_glossary_no_alias_writes_two_columns(gui_api_obj):
    """旧库无别名：保存后 CSV 不出现第三列（learned 行格式不受影响）。"""
    d = _home_glossary_dir()
    try:
        p = d / "glossary.csv"
        p.write_text("こんにちは,你好\n", encoding="utf-8-sig")
        saved = gui_api_obj.refine_save_glossary([["こんにちは", "你好"]], str(p))
        assert saved["success"] is True
        assert saved["alias_kept"] == 0
        assert p.read_text(encoding="utf-8-sig").strip() == "こんにちは,你好"
        got = gui_api_obj.refine_get_glossary(str(p))
        assert got["rows"] == [["こんにちは", "你好", ""]]
    finally:
        shutil.rmtree(d, ignore_errors=True)
