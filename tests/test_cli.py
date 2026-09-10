# -*- coding: utf-8 -*-
"""cli v2 接线测试（--s1-* 落槽0、--s3-* 落槽2，槽1/3 禁用）"""
from subtransjav.refine.cli import build_parser, config_from_args


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
