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

pytestmark = [pytest.mark.gui]

pytest.importorskip("webview", reason="pywebview 为可选 gui extra，未安装时跳过 GUI API 测试", exc_type=ImportError)

from subtransjav.webview_gui.api import (  # noqa: E402  须在 importorskip 之后
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


def test_build_refine_args_ctx_passthrough():
    """上下文窗口经 GUI 透传为对应 CLI 旗标（D2026-0923-01）。"""
    args = _build_refine_args({"inputs": ["a.srt"], "v2_ctx": 22272})
    assert args[args.index("--v2-ctx") + 1] == "22272"


def test_build_refine_args_no_ctx_by_default():
    args = _build_refine_args({"inputs": ["a.srt"]})
    assert "--v2-ctx" not in args


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


def test_build_refine_args_new_safety_params_absent_by_default():
    """四个安全参数缺省时均不产生 CLI 旗标。"""
    args = _build_refine_args({"inputs": ["a.srt"]})
    for flag in ("--source-filter", "--no-auto-synopsis", "--dry-run"):
        assert flag not in args


def test_build_refine_args_source_filter_non_default():
    """source_filter 传合法非默认值时产生 --source-filter；默认值不传。"""
    args = _build_refine_args({"inputs": ["a.srt"], "source_filter": "strict"})
    i = args.index("--source-filter")
    assert args[i + 1] == "strict"
    args = _build_refine_args({"inputs": ["a.srt"], "source_filter": "default"})
    assert "--source-filter" not in args


def test_build_refine_args_auto_synopsis_inverted_only_when_false():
    """auto_synopsis 仅显式 False 时产生 --no-auto-synopsis。"""
    for v in (None, True):
        options = {"inputs": ["a.srt"]}
        if v is not None:
            options["auto_synopsis"] = v
        assert "--no-auto-synopsis" not in _build_refine_args(options)
    args = _build_refine_args({"inputs": ["a.srt"], "auto_synopsis": False})
    assert "--no-auto-synopsis" in args


def test_build_refine_args_dry_run_flag():
    args = _build_refine_args({"inputs": ["a.srt"], "dry_run": True})
    assert "--dry-run" in args
    assert "--dry-run" not in _build_refine_args({"inputs": ["a.srt"]})


def test_build_refine_args_tm_params_passthrough():
    args = _build_refine_args({
        "inputs": ["a.srt"],
        "no_tm": True,
        "tm_db": "D:/tm/tm.db",
        "tm_threshold": "0.75",
    })
    assert "--no-tm" in args
    assert args[args.index("--tm-db") + 1] == "D:/tm/tm.db"
    assert args[args.index("--tm-threshold") + 1] == "0.75"


def test_build_refine_args_tm_params_absent_by_default():
    args = _build_refine_args({"inputs": ["a.srt"]})
    assert "--no-tm" not in args
    assert "--tm-db" not in args
    assert "--tm-threshold" not in args


def test_build_refine_args_no_tm_absent_when_tm_enabled():
    args = _build_refine_args({"inputs": ["a.srt"], "no_tm": False, "tm_db": "D:/tm/tm.db"})
    assert "--no-tm" not in args
    assert "--tm-db" in args


# ---------------------------------------------------------------------------
# force 覆盖确认（D2026-0925-01 D6 终选）：
# - _build_refine_args 仅在 options["force"] 为真时追加 --force；
# - start_translation 检测到已完成终稿且未带 force 时不启动进程，
#   返回 needs_confirm + existing 产物清单。
# ---------------------------------------------------------------------------

def test_build_refine_args_force_only_when_true():
    """critic 要求的可回归约束：未带 force 不拼 --force，带 force 才含。"""
    args = _build_refine_args({"inputs": ["a.srt"]})
    assert "--force" not in args
    args = _build_refine_args({"inputs": ["a.srt"], "force": False})
    assert "--force" not in args
    args = _build_refine_args({"inputs": ["a.srt"], "force": True})
    assert "--force" in args


def test_start_translation_needs_confirm_when_final_exists(tmp_path, gui_api_obj):
    """已完成终稿（{stem}_final_cn.srt 存在）且未带 force：不启动进程，
    返回结构化 needs_confirm 与 existing 产物文件名清单。"""
    (tmp_path / "ep01_final_cn.srt").write_text("终稿", encoding="utf-8")
    result = gui_api_obj.start_translation({"inputs": [str(tmp_path / "ep01.srt")]})
    assert result["success"] is False
    assert result["needs_confirm"] is True
    assert result["existing"] == ["ep01_final_cn.srt"]
    # 未启动任何子进程
    assert getattr(gui_api_obj, "_translate_process", None) is None


# ---------------------------------------------------------------------------
# D6 遗留：force-resume 通道 + 学习闸开关（--glossary-learn /
# --glossary-conflict-block；tm_learn_gate 默认 True，GUI 不设开关）
# ---------------------------------------------------------------------------

def test_build_refine_args_force_resume_flag():
    """勾选 refineForceResume 才拼 --force-resume；隐含 resume 由
    RefineConfig.__post_init__ 不变式保证，此处不重复拼 --resume。"""
    args = _build_refine_args({"inputs": ["a.srt"], "force_resume": True})
    assert "--force-resume" in args


def test_build_refine_args_no_force_resume_by_default():
    args = _build_refine_args({"inputs": ["a.srt"]})
    assert "--force-resume" not in args


def test_build_refine_args_glossary_learn_flag():
    args = _build_refine_args({"inputs": ["a.srt"], "glossary_learn": True})
    assert "--glossary-learn" in args


def test_build_refine_args_glossary_learn_absent_by_default():
    args = _build_refine_args({"inputs": ["a.srt"]})
    assert "--glossary-learn" not in args


def test_build_refine_args_glossary_conflict_block_flag():
    args = _build_refine_args({"inputs": ["a.srt"],
                               "glossary_conflict_block": True})
    assert "--glossary-conflict-block" in args


def test_build_refine_args_glossary_conflict_block_absent_by_default():
    args = _build_refine_args({"inputs": ["a.srt"]})
    assert "--glossary-conflict-block" not in args


class _StopLaunch(Exception):
    """fake Popen 哨兵：捕获拼好的 args 后终止启动流程。"""


def _capture_popen(monkeypatch, captured, api_obj):
    import threading

    import subtransjav.webview_gui.api as api_mod

    # _translate_lock 由 __init__ 创建；object.__new__ 实例需手动补齐
    api_obj._translate_lock = threading.Lock()

    def fake_popen(args, **kwargs):
        captured["args"] = list(args)
        raise _StopLaunch("stop-before-spawn")

    monkeypatch.setattr(api_mod.subprocess, "Popen", fake_popen)


def test_start_translation_without_force_args_have_no_force_flag(
        tmp_path, gui_api_obj, monkeypatch):
    """无终稿走正常启动路径：mock 进程层捕获 args，未带 force 则不含 --force。"""
    captured = {}
    _capture_popen(monkeypatch, captured, gui_api_obj)
    result = gui_api_obj.start_translation({"inputs": [str(tmp_path / "ep02.srt")]})
    assert result["success"] is False  # _StopLaunch 被启动异常分支吞掉
    assert "--force" not in captured["args"]


def test_start_translation_with_force_args_include_force_flag(
        tmp_path, gui_api_obj, monkeypatch):
    """确认后带 force=True 重调：拼出的 args 含 --force（确认路径可达）。"""
    captured = {}
    _capture_popen(monkeypatch, captured, gui_api_obj)
    result = gui_api_obj.start_translation(
        {"inputs": [str(tmp_path / "ep01.srt")], "force": True})
    assert result["success"] is False
    assert "--force" in captured["args"]


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


# ---------------------------------------------------------------------------
# read_output_artifact：质量报告导读查看器（W1b）
# ---------------------------------------------------------------------------

def test_read_output_artifact_valid_guide(gui_api_obj, tmp_path):
    """合法导读 JSON：success=True 且 data 透传（含 basis 字段）。"""
    p = tmp_path / "EP01_质量报告导读.json"
    p.write_text(
        '{"version":"1.0","source":"a.srt","stem":"EP01",'
        '"generated_at":"2026-09-25T00:00:00","basis":"基于本次运行",'
        '"conclusions":["c1"],"sections":[{"title":"t","note":"n"}],'
        '"companions":{"a.srt":true}}',
        encoding="utf-8",
    )
    got = gui_api_obj.read_output_artifact(str(p))
    assert got["success"] is True
    assert got["data"]["basis"] == "基于本次运行"
    assert got["path"] == str(p)


def test_read_output_artifact_rejects_arbitrary_json(gui_api_obj, tmp_path):
    """非导读后缀的任意 json 一律拒绝（防任意 json 读取）。"""
    p = tmp_path / "other.json"
    p.write_text('{"a":1}', encoding="utf-8")
    got = gui_api_obj.read_output_artifact(str(p))
    assert got["success"] is False
    assert "导读" in got["error"]


def test_read_output_artifact_missing_or_system_path(gui_api_obj, tmp_path):
    """不存在的导读路径与系统目录路径均拒绝。"""
    got = gui_api_obj.read_output_artifact(str(tmp_path / "none_质量报告导读.json"))
    assert got["success"] is False
    assert "不存在" in got["error"]
    import os
    win_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    bad = gui_api_obj.read_output_artifact(
        os.path.join(win_root, "x_质量报告导读.json"))
    assert bad["success"] is False
    got_empty = gui_api_obj.read_output_artifact("")
    assert got_empty["success"] is False


def test_read_output_artifact_corrupt_json(gui_api_obj, tmp_path):
    """损坏 JSON：报"文件损坏"类错误而非抛异常。"""
    p = tmp_path / "bad_质量报告导读.json"
    p.write_text("{not json", encoding="utf-8")
    got = gui_api_obj.read_output_artifact(str(p))
    assert got["success"] is False
    assert "损坏" in got["error"]


# ---------------------------------------------------------------------------
# C1（D2026-0925-01）：stages[].model 持久化档位（直存直读，不进分层）
# ---------------------------------------------------------------------------
def test_refine_stage_settings_saves_model(gui_api_obj, tmp_path, monkeypatch):
    """保存含 model 的 stages 后，文件与读取接口均回传 model。"""
    path = tmp_path / "refine_stage_settings.json"
    monkeypatch.setattr(gui_api_obj, "_refine_stage_settings_path",
                        lambda: str(path))
    r = gui_api_obj.refine_save_stage_settings(
        stages=[{"stage": 1, "provider": "zen", "endpoint": "https://x",
                 "model": "glm-4"},
                {"stage": 3, "provider": "custom", "endpoint": "https://y",
                 "model": "m2"}],
        settings={"v2_concurrency": 3, "v2_ctx": 16384})
    assert r["success"] is True
    assert r["endpoints_saved"] == 2

    got = gui_api_obj.refine_get_stage_settings()
    assert got["success"] is True
    by_stage = {s.get("stage"): s for s in got["stages"]}
    assert by_stage[1]["model"] == "glm-4"
    assert by_stage[3]["model"] == "m2"
    # settings 键集不变
    assert got["settings"]["v2_concurrency"] == 3
    assert got["settings"]["v2_ctx"] == 16384

    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {s["stage"]: s.get("model") for s in data["stages"]} == \
        {1: "glm-4", 3: "m2"}


def test_refine_stage_settings_model_incremental_merge(gui_api_obj, tmp_path,
                                                       monkeypatch):
    """缺 model 的增量保存不抹掉已有 model；缺省 model 读取不报错。"""
    path = tmp_path / "refine_stage_settings.json"
    monkeypatch.setattr(gui_api_obj, "_refine_stage_settings_path",
                        lambda: str(path))
    r1 = gui_api_obj.refine_save_stage_settings(
        stages=[{"stage": 1, "provider": "zen", "endpoint": "https://x",
                 "model": "glm-4"}])
    assert r1["success"] is True
    r2 = gui_api_obj.refine_save_stage_settings(
        stages=[{"stage": 1, "endpoint": "https://z"}])
    assert r2["success"] is True

    got = gui_api_obj.refine_get_stage_settings()
    entry = next(s for s in got["stages"] if s.get("stage") == 1)
    assert entry["model"] == "glm-4"
    assert entry["endpoint"] == "https://z"
