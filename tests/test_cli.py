"""cli v2 接线测试（--s1-* 落槽0、--s3-* 落槽2，槽1/3 禁用）"""
import pytest

import subtransjav.refine.config as refine_config
import subtransjav.refine.pipeline_v2 as pv
from subtransjav.refine.cli import build_parser, config_from_args, main


def test_config_from_args_builds_v2_slots(tmp_path):
    args = build_parser().parse_args([
        "-i", str(tmp_path / "x.srt"),
        "--s1-provider", "lmstudio",
        "--s1-model", "qwen-a",
        "--s3-provider", "deepseek",
        "--s3-model", "deepseek-v4-flash",
    ])
    cfg = config_from_args(args)
    assert len(cfg.stages) == 4
    assert [s.index for s in cfg.stages] == [0, 1, 2, 3]
    # --s1-* 落槽0（阶段A），--s3-* 落槽2（阶段B）
    assert cfg.stages[0].provider == "lmstudio"
    assert cfg.stages[0].model == "qwen-a"
    assert cfg.stages[2].provider == "deepseek"
    assert cfg.stages[2].model == "deepseek-v4-flash"
    # 槽0/2 启用（阶段A/B），槽1/3 为 v2 未用占位禁用槽
    assert cfg.stages[0].enabled and cfg.stages[2].enabled
    assert not cfg.stages[1].enabled and not cfg.stages[3].enabled


def test_cli_draft_model_flags_land_in_stage_slots(tmp_path):
    """--s1/s3-draft-model 落对应槽位 engine_draft_model；缺省为空（不挂 draft）。"""
    args = build_parser().parse_args([
        "-i", str(tmp_path / "x.srt"),
        "--s1-draft-model", "qwen3.5-0.8b",
        "--s3-draft-model", "draft-b",
    ])
    cfg = config_from_args(args)
    assert cfg.stages[0].engine_draft_model == "qwen3.5-0.8b"
    assert cfg.stages[2].engine_draft_model == "draft-b"
    cfg_default = config_from_args(build_parser().parse_args(
        ["-i", str(tmp_path / "x.srt")]))
    assert cfg_default.stages[0].engine_draft_model == ""
    assert cfg_default.stages[2].engine_draft_model == ""


def test_cli_multi_input_accumulates(tmp_path):
    """回归：GUI 文件夹模式发多个 -i，必须累积而非覆盖（曾只翻译最后一个文件）。"""
    args = build_parser().parse_args([
        "-i", str(tmp_path / "a.srt"),
        "-i", str(tmp_path / "b.srt"),
        "-i", str(tmp_path / "c.srt"),
    ])
    assert len(args.input) == 3
    cfg = config_from_args(args)
    assert len(cfg.inputs) == 3


# ---------------------------------------------------------------------------
# P0：断点续跑 / 事件格式参数 + 退出码映射
# ---------------------------------------------------------------------------

def test_cli_new_resume_and_event_args(tmp_path):
    args = build_parser().parse_args([
        "-i", str(tmp_path / "x.srt"),
        "--resume", "--force-resume",
        "--event-format", "ndjson",
        "--heartbeat-interval", "5.5",
    ])
    cfg = config_from_args(args)
    assert cfg.resume is True
    assert cfg.force_resume is True
    assert cfg.event_format == "ndjson"
    assert cfg.heartbeat_interval == 5.5


def test_cli_force_resume_implies_resume(tmp_path):
    """O4 回归：仅传 --force-resume（不带 --resume）时 resume 隐含生效。"""
    args = build_parser().parse_args([
        "-i", str(tmp_path / "x.srt"),
        "--force-resume",
    ])
    cfg = config_from_args(args)
    assert cfg.force_resume is True
    assert cfg.resume is True


def test_cli_default_args_keep_legacy_behavior(tmp_path):
    """默认值保持旧行为：不复用、text 事件、20s 心跳。"""
    args = build_parser().parse_args(["-i", str(tmp_path / "x.srt")])
    cfg = config_from_args(args)
    assert cfg.resume is False
    assert cfg.force_resume is False
    assert cfg.event_format == "text"
    assert cfg.heartbeat_interval == 20.0


def _write_input(tmp_path):
    inp = tmp_path / "x.srt"
    inp.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n",
                   encoding="utf-8")
    return str(inp)


def _isolate_logs(tmp_path, monkeypatch):
    """把运行日志目录隔离到临时目录（避免污染仓库 Logs/）。"""
    monkeypatch.setattr(refine_config, "LOGS_DIR", str(tmp_path / "Logs"))


def _fake_run_v2(monkeypatch, sink=None, error=None):
    def _run(cfg, *, summary_sink=None, event_stream=None):
        if error is not None:
            raise error
        if summary_sink is not None and sink is not None:
            summary_sink.update(dict(sink))
        return "out_final_cn.srt"
    monkeypatch.setattr(pv, "run_v2", _run)


def test_cli_exit_code_success(tmp_path, monkeypatch):
    _isolate_logs(tmp_path, monkeypatch)
    _fake_run_v2(monkeypatch, sink={
        "files_ok": 1, "files_degraded": 0, "files_failed": 0,
        "untranslated_majority": False, "risk_count": 0,
        "summary_lines": []})
    with pytest.raises(SystemExit) as ei:
        main(["-i", _write_input(tmp_path)])
    assert ei.value.code == 0


def test_cli_exit_code_partial_degradation(tmp_path, monkeypatch, capsys):
    """存在降级/风险但部分成功 → 退出码 3 + 部分降级状态行 + 风险摘要收尾。"""
    _isolate_logs(tmp_path, monkeypatch)
    _fake_run_v2(monkeypatch, sink={
        "files_ok": 1, "files_degraded": 1, "files_failed": 0,
        "untranslated_majority": False, "risk_count": 2,
        "summary_lines": ["⚠️ [warning] B | 降级动作: 回退A译文"]})
    with pytest.raises(SystemExit) as ei:
        main(["-i", _write_input(tmp_path)])
    assert ei.value.code == 3
    # 风险摘要收尾打印到 stdout
    assert "回退A译文" in capsys.readouterr().out
    # 部分降级状态行写入运行日志（⚠️ 开头会归档到 Errors，符合预期）
    logs = list((tmp_path / "Logs").glob("*.txt"))
    assert logs
    assert "部分降级（成功1/降级1/失败0）" in logs[0].read_text(encoding="utf-8")


def test_cli_exit_code_all_failed(tmp_path, monkeypatch):
    """全部失败走现有 RefineError 路径 → 退出码 1。"""
    from subtransjav.refine.pipeline_support import RefineError
    _isolate_logs(tmp_path, monkeypatch)
    _fake_run_v2(monkeypatch, error=RefineError("全部 1 个文件均处理失败"))
    with pytest.raises(SystemExit) as ei:
        main(["-i", _write_input(tmp_path)])
    assert ei.value.code == 1


def test_cli_ndjson_stdout_stays_pure(tmp_path, monkeypatch, capsys):
    """D1 回归：ndjson 模式下 stdout 每一行都必须能被 json.loads——
    收尾的人类可读页脚（📄 运行日志已保存 等）一律改走 stderr。"""
    import json
    _isolate_logs(tmp_path, monkeypatch)
    _fake_run_v2(monkeypatch, sink={
        "files_ok": 1, "files_degraded": 0, "files_failed": 0,
        "untranslated_majority": False, "risk_count": 0,
        "summary_lines": ["   汇总行"]})
    with pytest.raises(SystemExit) as ei:
        main(["-i", _write_input(tmp_path), "--event-format", "ndjson"])
    assert ei.value.code == 0
    captured = capsys.readouterr()
    for line in captured.out.splitlines():
        if line.strip():
            json.loads(line)            # 混入人类可读行会在此抛错
    assert "运行日志已保存" not in captured.out
    assert "运行日志已保存" in captured.err


def test_cli_text_mode_footer_still_on_stdout(tmp_path, monkeypatch, capsys):
    """D1 对照：text 模式（默认）收尾页脚仍打印到 stdout，行为保持不变。"""
    _isolate_logs(tmp_path, monkeypatch)
    _fake_run_v2(monkeypatch, sink={
        "files_ok": 1, "files_degraded": 0, "files_failed": 0,
        "untranslated_majority": False, "risk_count": 0,
        "summary_lines": []})
    with pytest.raises(SystemExit):
        main(["-i", _write_input(tmp_path)])
    assert "运行日志已保存" in capsys.readouterr().out


def test_cli_dry_run_exit_codes_unchanged(tmp_path, monkeypatch):
    """dry-run 行为完全不变：配置正确 → 0（保留现状：成功不产 3）。"""
    _isolate_logs(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        main(["-i", _write_input(tmp_path), "--dry-run",
              "--s1-model", "m", "--s3-model", "m"])
    assert ei.value.code == 0
    logs = list((tmp_path / "Logs").glob("*.txt"))
    assert logs
    assert "dry-run" in logs[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P3-10 补薄：未知 provider 的 validate 报错路径 / 非法 --event-format 退出码
# ---------------------------------------------------------------------------

def test_cli_validate_flags_unknown_provider():
    """argparse choices 之外（程序内构造）的未知 provider 必须被 validate 兜住。"""
    from subtransjav.refine.config import RefineConfig, StageConfig
    cfg = RefineConfig(
        inputs=["whatever.srt"],
        stages=[StageConfig(0, True, "no-such-provider", "some-model")],
    )
    errs = cfg.validate()
    assert errs, "未知 provider 必须产生校验错误"
    assert any("no-such-provider" in e for e in errs)
    assert any("缺少接口地址" in e for e in errs), \
        "未知 provider 无默认 endpoint，必须报缺少接口地址"
    assert any("缺少 API Key" in e for e in errs), \
        "未知 provider 解析不到密钥，必须报缺少 API Key"


def test_cli_invalid_event_format_exits_2(tmp_path, monkeypatch):
    """--event-format 非法值：argparse 报错并退出码 2（不进入翻译流程）。"""
    _isolate_logs(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        main(["-i", _write_input(tmp_path), "--event-format", "xml"])
    assert ei.value.code == 2
    # 不应产生运行日志（在解析阶段即终止）
    assert not list((tmp_path / "Logs").glob("*.txt"))


# ---------------------------------------------------------------------------
# 闸门0：--source-filter 三档
# ---------------------------------------------------------------------------

def test_cli_source_filter_modes(tmp_path):
    """--source-filter 三档逐一解析落 cfg.v2_source_filter。"""
    for value in ("strict", "default", "off"):
        args = build_parser().parse_args([
            "-i", str(tmp_path / "x.srt"), "--source-filter", value])
        cfg = config_from_args(args)
        assert cfg.v2_source_filter == value


def test_cli_source_filter_default_keeps_std_gate(tmp_path):
    """默认 default 档（闸门0 开启，仅明确幻觉删除）。"""
    args = build_parser().parse_args(["-i", str(tmp_path / "x.srt")])
    cfg = config_from_args(args)
    assert cfg.v2_source_filter == "default"


def test_cli_invalid_source_filter_exits_2(tmp_path, monkeypatch):
    """--source-filter 非法值：argparse 报错并退出码 2（仿 --event-format）。"""
    _isolate_logs(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        main(["-i", _write_input(tmp_path), "--source-filter", "bogus"])
    assert ei.value.code == 2
    assert not list((tmp_path / "Logs").glob("*.txt"))


# ---------------------------------------------------------------------------
# H4a：--asr-meta（上游 WhisperJAV 运行 manifest）
# ---------------------------------------------------------------------------

def test_cli_asr_meta_default_empty_means_auto_discovery(tmp_path):
    """默认空串：自动发现 SRT 旁车 whisperjav_run.json 的语义。"""
    args = build_parser().parse_args(["-i", str(tmp_path / "x.srt")])
    cfg = config_from_args(args)
    assert cfg.asr_meta == ""


def test_cli_asr_meta_wired_to_config(tmp_path):
    """--asr-meta 解析并接线到 cfg.asr_meta（文件/目录路径透传）。"""
    meta_path = tmp_path / "run" / "whisperjav_run.json"
    meta_path.parent.mkdir()
    meta_path.write_text("{}", encoding="utf-8")
    args = build_parser().parse_args([
        "-i", str(tmp_path / "x.srt"), "--asr-meta", str(meta_path)])
    cfg = config_from_args(args)
    assert cfg.asr_meta == str(meta_path)
