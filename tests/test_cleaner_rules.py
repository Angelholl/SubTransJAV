# -*- coding: utf-8 -*-
"""中文净语者规则引擎单元测试"""
import pytest

from subtransjav.refine.cleaner_rules import (
    ChineseCleaner, Subtitle, parse_srt, format_srt, clean_srt
)


def _make_items(texts, gap_ms=1000, start_ms=0):
    items = []
    t = start_ms
    for i, text in enumerate(texts):
        items.append(Subtitle(index=i + 1, start=t, end=t + 800, text=text))
        t += gap_ms
    return items


@pytest.fixture
def config_dir(tmp_path):
    hardened_yaml = (
        "keywords:\n"
        "- 紧\n"
        "- 舒服\n"
        "- 爽\n"
        "patterns:\n"
        "- '好[紧松舒服爽]'\n"
    )
    hallucination_yaml = (
        "video_meta:\n"
        "  strong:\n"
        "  - 感谢观看\n"
        "  - 感谢收看\n"
        "  - 下期再见\n"
        "  weak:\n"
        "  - 再见\n"
        "  - 拜拜\n"
        "  - 明天见\n"
        "garbage:\n"
        "- '^[，。、！？\\s,\\.!?…~～…—\\-]+$'\n"
        "parenthetical: '^[（\\(][^）\\)]+[）\\)]$'\n"
    )
    (tmp_path / "hardened_phrases.yaml").write_text(hardened_yaml, encoding="utf-8")
    (tmp_path / "hallucination_patterns.yaml").write_text(hallucination_yaml, encoding="utf-8")
    return str(tmp_path)


@pytest.fixture
def cleaner(config_dir):
    return ChineseCleaner(config_dir)


# ---- SRT解析/格式化 ----

def test_parse_srt_basic():
    srt = "1\n00:00:01,000 --> 00:00:03,000\n你好\n\n2\n00:00:04,000 --> 00:00:06,000\n世界\n"
    items = parse_srt(srt)
    assert len(items) == 2
    assert items[0].text == "你好"
    assert items[0].start == 1000
    assert items[0].end == 3000
    assert items[1].text == "世界"


def test_format_srt_basic():
    items = [Subtitle(index=1, start=1000, end=3000, text="你好")]
    result = format_srt(items)
    assert "00:00:01,000 --> 00:00:03,000" in result
    assert "你好" in result


def test_parse_srt_multiline():
    srt = "1\n00:00:01,000 --> 00:00:03,000\n第一行\n第二行\n"
    items = parse_srt(srt)
    assert len(items) == 1
    assert items[0].text == "第一行\n第二行"


# ---- CQS ----

def test_cqs_consecutive_3_plus(cleaner):
    items = _make_items(["啊", "嗯", "哦"], gap_ms=500)
    cqs = cleaner._mark_cqs_indices(items)
    assert cqs == {0, 1, 2}


def test_cqs_isolated_delete(cleaner):
    items = _make_items(["啊"])
    cqs = cleaner._mark_cqs_indices(items)
    assert len(cqs) == 0


def test_cqs_gap_breaks(cleaner):
    items = _make_items(["啊", "嗯", "哦"], gap_ms=5000)
    cqs = cleaner._mark_cqs_indices(items)
    assert len(cqs) == 0


def test_cqs_substantive_breaks(cleaner):
    items = _make_items(["啊", "你好世界", "哦"], gap_ms=500)
    cqs = cleaner._mark_cqs_indices(items)
    assert len(cqs) == 0


# ---- 视频元信息 ----

def test_meta_strong_delete(cleaner):
    assert cleaner._is_video_meta("感谢观看")


def test_meta_weak_no_strong(cleaner):
    assert not cleaner._is_video_meta("再见，明天见")


def test_meta_weak_with_strong(cleaner):
    assert cleaner._is_video_meta("感谢观看，再见")


def test_meta_normal_dialogue(cleaner):
    assert not cleaner._is_video_meta("我很感谢你")


# ---- 括号描述 ----

def test_parenthetical_delete(cleaner):
    assert cleaner._is_parenthetical_meta("（笑声）")


def test_parenthetical_hardened(cleaner):
    assert not cleaner._is_parenthetical_meta("（舒服）")


# ---- 垃圾符号 ----

def test_garbage_punctuation(cleaner):
    assert cleaner._is_garbage("，。！")


def test_garbage_normal_short(cleaner):
    assert not cleaner._is_garbage("好啊")


# ---- 纯感叹词 ----

def test_exclamation_delete(cleaner):
    assert cleaner._is_pure_exclamation("啊")


def test_exclamation_with_content(cleaner):
    assert not cleaner._is_pure_exclamation("啊，流血了")


# ---- 孤立感官感叹 ----

def test_sensory_isolated(cleaner):
    assert cleaner._is_isolated_sensory("好舒服")


def test_sensory_with_request(cleaner):
    assert not cleaner._is_isolated_sensory("好舒服，再用力点")


# ---- 短应答 ----

def test_short_response_delete(cleaner):
    assert cleaner._is_short_response("嗯")


def test_short_response_with_content(cleaner):
    assert not cleaner._is_short_response("好，我马上来")


# ---- 填充词 ----

def test_filler_delete(cleaner):
    assert cleaner._is_filler("其实")


def test_filler_with_content(cleaner):
    assert not cleaner._is_filler("其实，我觉得不对")


# ---- 孤立称呼 ----

def test_address_isolated(cleaner):
    assert cleaner._is_isolated_address("姐姐")


def test_address_with_content(cleaner):
    assert not cleaner._is_isolated_address("姐姐，等等我")


# ---- HARDENED保护 ----

def test_hardened_keep(cleaner):
    assert cleaner._is_hardened("好紧")


def test_hardened_pattern(cleaner):
    assert cleaner._is_hardened("好舒服")


# ---- 60秒去重 ----

def test_dedup_cross_segment(cleaner):
    items = [
        Subtitle(index=1, start=0, end=800, text="好舒服"),
        Subtitle(index=2, start=30000, end=30800, text="好舒服"),
    ]
    result = cleaner._check_60s_dedup(1, items, set())
    assert result is True


def test_dedup_same_segment(cleaner):
    items = [
        Subtitle(index=1, start=0, end=800, text="好舒服"),
        Subtitle(index=2, start=1000, end=1800, text="好舒服"),
    ]
    result = cleaner._check_60s_dedup(1, items, {0, 1})
    assert result is False


# ---- 断句合并 ----

def test_merge_fragments(cleaner):
    items = [
        Subtitle(index=1, start=0, end=800, text="其实，"),
        Subtitle(index=2, start=900, end=2000, text="我觉得不对"),
    ]
    merged = cleaner._merge_fragments(items)
    assert len(merged) == 1
    assert "我觉得不对" in merged[0].text


def test_merge_no_merge(cleaner):
    items = [
        Subtitle(index=1, start=0, end=800, text="你好"),
        Subtitle(index=2, start=2000, end=3000, text="世界"),
    ]
    merged = cleaner._merge_fragments(items)
    assert len(merged) == 2


def test_merge_no_merge_independent_connector_start(cleaner):
    """前条完整句 + 后条以"不过"开头 → 两条独立字幕不得合并"""
    items = [
        Subtitle(index=1, start=0, end=800, text="我家可是没学过M的哦。"),
        Subtitle(index=2, start=900, end=1700, text="不过，是骂倒啊。"),
    ]
    merged = cleaner._merge_fragments(items)
    assert len(merged) == 2
    assert merged[0].text == "我家可是没学过M的哦。"
    assert merged[1].text == "不过，是骂倒啊。"


def test_merge_no_merge_ellipsis_end(cleaner):
    """省略号收尾常是完整句，不再触发合并"""
    items = [
        Subtitle(index=1, start=0, end=800, text="他想说……"),
        Subtitle(index=2, start=900, end=1700, text="后来就走了。"),
    ]
    merged = cleaner._merge_fragments(items)
    assert len(merged) == 2


def test_merge_comma_end_still_merges(cleaner):
    """逗号收尾仍合并（回归保障）"""
    items = [
        Subtitle(index=1, start=0, end=800, text="我觉得，"),
        Subtitle(index=2, start=900, end=1700, text="这样不对。"),
    ]
    merged = cleaner._merge_fragments(items)
    assert len(merged) == 1
    assert "这样不对" in merged[0].text


# ---- 完整流程 ----

def test_clean_srt_e2e(config_dir):
    srt = (
        "1\n00:00:01,000 --> 00:00:03,000\n你好世界\n\n"
        "2\n00:01:30,000 --> 00:01:32,000\n好紧\n\n"
        "3\n00:03:00,000 --> 00:03:02,000\n今天天气真好\n"
    )
    result = clean_srt(srt, config_dir)
    items = parse_srt(result)
    assert len(items) >= 1
    texts = [it.text for it in items]
    assert any("好紧" in t for t in texts)


def test_parse_srt_bom():
    """S2: BOM不导致首条字幕丢失"""
    srt = "\ufeff1\n00:00:01,000 --> 00:00:02,000\n测试内容\n"
    items = parse_srt(srt)
    assert len(items) == 1
    assert items[0].text == "测试内容"


def test_parse_srt_bom_multi():
    """S2: 多个BOM场景"""
    srt = "\ufeff1\n00:00:01,000 --> 00:00:02,000\n第一条\n\n2\n00:00:03,000 --> 00:00:04,000\n第二条\n"
    items = parse_srt(srt)
    assert len(items) == 2
    assert items[0].text == "第一条"
    assert items[1].text == "第二条"


def test_merge_fragments_three_segments():
    """M1: 三段式断句正确合并"""
    srt = (
        "1\n00:00:00,000 --> 00:00:00,800\n其实，\n\n"
        "2\n00:00:00,900 --> 00:00:01,400\n但是\n\n"
        "3\n00:00:01,400 --> 00:00:01,900\n我觉得很好\n"
    )
    items = parse_srt(srt)
    cleaner = ChineseCleaner()
    merged = cleaner._merge_fragments(items)
    # 三段应在500ms间隙内合并为1条
    assert len(merged) == 1
    assert "其实" in merged[0].text
    assert "但是" in merged[0].text
    assert "我觉得很好" in merged[0].text


def test_hardened_address_family():
    """M2: 家庭称呼受HARDENED保护不被删除（单条测试，走L0路径而非CQS）"""
    # 单条"爷爷"——不触发CQS（需≥3条连续），纯L0-hardened保护
    srt_single = "1\n00:00:01,000 --> 00:00:02,000\n爷爷\n"
    result = clean_srt(srt_single)
    assert "爷爷" in result

    # "爸爸"混在正常对话中——验证不被误删
    srt_mixed = (
        "1\n00:00:00,000 --> 00:00:01,000\n今天天气不错\n\n"
        "2\n00:00:01,500 --> 00:00:02,500\n爸爸\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\n我们去玩吧\n"
    )
    result2 = clean_srt(srt_mixed)
    assert "爸爸" in result2


def test_has_substantive_two_char():
    """M3: 两字实义词（你好/我要）被判为有实义"""
    cleaner = ChineseCleaner()
    assert cleaner._has_substantive("你好") is True
    assert cleaner._has_substantive("我要") is True
    assert cleaner._has_substantive("他来") is True


def test_exclamation_isolated_delete():
    """M5确认: L6孤立感叹词正常触发删除（无死条件阻塞）"""
    srt = "1\n00:00:01,000 --> 00:00:02,000\n啊\n"
    result = clean_srt(srt)
    # 孤立感叹词应被删除
    assert "啊" not in result or result.strip() == ""


def test_hardened_removal_no_protect():
    """M4: 被移除的词不再受HARDENED保护"""
    cleaner = ChineseCleaner()
    # 到了/这里/那里/里面/外面 不应在hardened词表中
    assert cleaner._is_hardened("到了") is False
    assert cleaner._is_hardened("这里") is False
    assert cleaner._is_hardened("那里") is False
    assert cleaner._is_hardened("里面") is False
    assert cleaner._is_hardened("外面") is False


def test_hardened_deep_retained():
    """M4: 深处仍受HARDENED保护"""
    cleaner = ChineseCleaner()
    assert cleaner._is_hardened("深处") is True


def test_sensory_isolated_delete():
    """L7: 孤立感官词正常触发删除（非HARDENED保护词）"""
    # "痛苦"含感官根"痛"，不在HARDENED词表中，孤立出现应被删除
    srt = "1\n00:00:01,000 --> 00:00:02,000\n痛苦\n"
    result = clean_srt(srt)
    assert "痛苦" not in result


def test_short_response_delete_flow():
    """L8: 短应答正常触发删除"""
    srt = "1\n00:00:01,000 --> 00:00:02,000\n嗯\n"
    result = clean_srt(srt)
    assert "嗯" not in result


def test_address_isolated_delete():
    """L10: 孤立称呼正常触发删除（非hardened保护词）"""
    # "小姐"不是hardened保护词，孤立出现应被删除
    srt = "1\n00:00:01,000 --> 00:00:02,000\n小姐\n"
    result = clean_srt(srt)
    assert "小姐" not in result
