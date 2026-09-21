"""Refine 包单元测试与集成桩测试"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from subtransjav.refine.config import LOCAL_BATCH_HARD_CAP, RefineConfig, StageConfig
from subtransjav.refine.filters import build_srt, filter_placeholder_file, is_placeholder, parse_srt
from subtransjav.refine.glossary import (
    format_glossary_block,
    load_glossary,
    load_glossary_ex,
    match_glossary,
    save_glossary,
)
from subtransjav.refine.instructions import write_effective_instructions

SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,000
ムラムラする

2
00:00:04,000 --> 00:00:06,000
（削除：呼吸声，无实义）

3
00:00:07,000 --> 00:00:09,000
こんにちは
"""


# ---------------- glossary ----------------
def test_glossary_match_cjk_exact():
    hits = match_glossary("含むムラムラ文本", [("ムラムラ", "心痒"), ("IKU", "去了")])
    assert ("ムラムラ", "心痒") in hits


def test_glossary_match_latin_ci():
    hits = match_glossary("say iKu now", [("iku", "去了")])
    assert hits == [("iku", "去了")]


def test_glossary_roundtrip(tmp_path):
    p = str(tmp_path / "g.csv")
    rows = [("a", "b"), ("c", "d")]
    save_glossary(p, rows)
    assert load_glossary(p) == rows


def test_glossary_save_three_column_roundtrip(tmp_path):
    """三列词条：aliases 经 `|` 写出，load_glossary_ex 读回别名相等。"""
    p = str(tmp_path / "g3.csv")
    rows = [("a", "b", ("x", "y")), ("c", "d", ())]
    save_glossary(p, rows)
    assert load_glossary_ex(p) == [("a", "b", ("x", "y")), ("c", "d", ())]
    # 旧接口读三列文件：忽略第三列，仍返回两列元组
    assert load_glossary(p) == [("a", "b"), ("c", "d")]


def test_glossary_save_two_column_no_third_field(tmp_path):
    """两列词条照旧写两列（无第三列文本），读回 aliases 为空元组。"""
    p = str(tmp_path / "g2.csv")
    save_glossary(p, [("a", "b")])
    assert (tmp_path / "g2.csv").read_text(encoding="utf-8-sig").strip() == "a,b"
    assert load_glossary_ex(p) == [("a", "b", ())]


def test_glossary_block_format():
    blk = format_glossary_block([("a", "b")])
    assert "术语对照表" in blk and "a → b" in blk


# ---------------- filters ----------------
def test_parse_build_roundtrip():
    entries = parse_srt(SAMPLE_SRT)
    assert len(entries) == 3
    assert entries[1]["timing"].startswith("00:00:04")
    rebuilt = build_srt(entries)
    assert len(parse_srt(rebuilt)) == 3


def test_placeholder_detection():
    assert is_placeholder("（削除：呼吸声，无实义）")
    assert is_placeholder("(删除：孤立感叹词)")
    assert not is_placeholder("普通台词です")


def test_chatter_strip():
    from subtransjav.refine.filters import strip_chatter_lines
    dirty = "台词。\n本批次共4条字幕，全部保留。#1为测试\nScene 1 分析"
    assert strip_chatter_lines(dirty) == "台词。"


def test_filter_placeholder_file(tmp_path):
    p = tmp_path / "x.srt"
    p.write_text(SAMPLE_SRT, encoding="utf-8")
    removed = filter_placeholder_file(str(p))
    assert removed == 1
    kept = parse_srt(p.read_text(encoding="utf-8"))
    assert [e["text"] for e in kept] == ["ムラムラする", "こんにちは"]


# ---------------- instructions ----------------
ROLE = "你是日文净语者。删除无意义行。"


def test_write_effective_appends_block(tmp_path):
    src = tmp_path / "r.txt"
    src.write_text("### prompt\nP\n\n### instructions\nBODY\n", encoding="utf-8")
    out = write_effective_instructions(src.read_text(encoding="utf-8"),
                                       "术语对照表 - X → Y",
                                       work_dir=str(tmp_path))
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "X → Y" in content and "BODY" in content


# ---------------- config ----------------
def _cfg(**kw):
    stages = kw.pop("stages", None) or [
        StageConfig(0, True, "lmstudio", "qwen"),
        StageConfig(1, True, "deepseek", "deepseek-v4-flash"),
        StageConfig(2, False, "lmstudio", "qwen"),
    ]
    return RefineConfig(stages=stages, **kw)


def test_batch_caps_local():
    cfg = _cfg(batch_local=50)
    assert cfg.batch_for(cfg.stages[0]) <= LOCAL_BATCH_HARD_CAP


def test_validate_missing_endpoint():
    cfg = _cfg()
    cfg.stages[1].provider = "custom"
    errs = cfg.validate()
    assert any("接口地址" in e for e in errs)


# ---------------- post_validate ----------------
def test_post_validate_zawei_ja_to_zh():
    from subtransjav.refine.post_validate import check_and_fix_translation_errors
    src = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "学校で有名です"}]
    tgt = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "作为学校很有名"}]
    fixes, _, _flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 1
    assert "是" in tgt[0]["text"]
    assert "作为" not in tgt[0]["text"]


def test_post_validate_zawei_toshite_kept():
    from subtransjav.refine.post_validate import check_and_fix_translation_errors
    src = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "医者として有名です"}]
    tgt = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "作为医生很有名"}]
    fixes, _, _flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert tgt[0]["text"] == "作为医生很有名"


def test_post_validate_zh_to_zh_zawei_not_called():
    # legacy 阶段语言表（STAGE_LANGS）已删除；
    # post_validate 函数本身由 test_post_validate.py 覆盖
    pass
