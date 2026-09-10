"""
post_validate.py 补充测试：覆盖误替换守卫与双检测组合场景。
"""

from subtransjav.refine.post_validate import check_and_fix_translation_errors


def test_zawei_no_de_in_src_kept():
    """目标含"作为"但源文不含「で」时，不应触发替换（false positive 守卫）。"""
    src = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "彼女は美人だ"}]
    tgt = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "作为一个女人"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0, f"不应修正但 got fixes={fixes}"
    assert tgt[0]["text"] == "作为一个女人", "文本不应被改动"
    assert not warnings
    assert not flagged


def test_zawei_with_de_and_toshite_kept():
    """源文同时含「として」时，即使也含「で」，"作为"应保留（として优先）。"""
    src = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "東京で医者として有名です"}]
    tgt = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "作为医生在东京很有名"}]
    fixes, _, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0, "源含「として」时不应触发で误译修正"
    assert "作为" in tgt[0]["text"]


def test_detection1_then_detection2_both_fire():
    """
    检测1与检测2同时触发：检测1的"作为→是"修正应保留，检测2仅告警不改动译文。
    构造：src 含「で」+「僕たち」，tgt 以"作为"开头且含"我是"。
    """
    src = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "学校で僕たちが有名です"}]
    tgt = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "我作为学校有名"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    # 检测1 修正 "作为"→"是"（+1）；检测2 仅告警不计数、不改动文本
    assert fixes == 1, f"仅检测1应计为修正，got fixes={fixes}"
    text = tgt[0]["text"]
    assert "作为" not in text, "检测1应已替换'作为'"
    assert not text.startswith("[待复核:主语]"), "检测2不应向译文写入前缀"
    assert "我是" in text, "检测1的'我是'修正应保留"
    assert len(warnings) == 2, "两条检测都应产生告警"
    assert 1 in flagged


# ---------------------------------------------------------------------------
# subject_misjudge target_pattern（YAML 2c 正则）五样例
# ---------------------------------------------------------------------------

def test_subject_target_pattern_samples():
    """re.match 语义（从头匹配）下的五个验收样例。"""
    from subtransjav.refine.post_validate import _get_compiled
    pat = _get_compiled()["subject"]["target"]
    assert pat.match("我是游泳部的部长"), "「我是…」应命中"
    assert pat.match("我们，是游泳部的部长"), "「我们，是…」应命中"
    assert pat.match("我们是游泳部的部长"), "「我们是…」应命中"
    assert not pat.match("是我们的游泳部部长"), "「是…」开头不命中"
    assert not pat.match("她是我们的部长"), "「她…」开头不命中"


def test_subject_source_pattern_adnominal_only():
    """source_pattern 收紧为定语构造：僕たち+…+で 命中，僕たちは部長だ 不误伤。"""
    from subtransjav.refine.post_validate import _get_compiled
    pat = _get_compiled()["subject"]["source"]
    assert pat.search("ボクたち、水泳部の部長で")
    assert pat.search("僕たち水泳部の部長で")
    assert not pat.search("僕たちは部長だ")     # 「我们是部长」是正确翻译
    assert not pat.search("僕たちは優しい")
    assert not pat.search("僕たちは部長で")     # (?!は)：主题标记は 后不触发


def test_subject_topic_wa_correct_translation_no_warning():
    """源「僕たちは部長で、エースだ」译「我们是部长，也是王牌」属正确翻译，
    不应告警（(?!は) 盲区修复）。"""
    src = [{"index": 1, "timing": _TIMING, "text": "僕たちは部長で、エースだ"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "我们是部长，也是王牌"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert warnings == []
    assert not flagged


def test_subject_adnominal_with_comma_warns():
    """源「僕たち、水泳部の部長で…」译「我们，是游泳部的部长」→ 应告警。"""
    src = [{"index": 1, "timing": _TIMING, "text": "ボクたち、水泳部の部長で…"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "我们，是游泳部的部长"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0                          # 仅告警，不改动译文
    assert len(warnings) == 1
    assert "主语误判" in warnings[0]
    assert tgt[0]["text"] == "我们，是游泳部的部长"
    assert 1 in flagged


# ---------------------------------------------------------------------------
# warn_only 行为（YAML 单一数据源）
# ---------------------------------------------------------------------------

_TIMING = "00:00:01,000 --> 00:00:02,000"


def test_warn_only_true_default_behavior():
    """warn_only=true（YAML 默认）：主语误判仅告警，不改动译文、无硬性前缀。"""
    src = [{"index": 1, "timing": _TIMING, "text": "ボクたち、水泳部の部長で"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "我是游泳部的部长"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert len(warnings) == 1
    assert "[硬性]" not in warnings[0]
    assert tgt[0]["text"] == "我是游泳部的部长"   # 译文不被改动
    assert 1 in flagged


def test_warn_only_false_hard_warning(monkeypatch):
    """warn_only=false：告警加 [硬性] 前缀并 logger.error；
    dewei 仍自动修正；subject 无可靠自动修正手段，不发明改写逻辑。"""
    from subtransjav.refine import post_validate as pvm

    compiled = pvm._get_compiled()
    monkeypatch.setitem(compiled, "dewei",
                        {**compiled["dewei"], "warn_only": False})
    monkeypatch.setitem(compiled, "subject",
                        {**compiled["subject"], "warn_only": False})

    errors = []
    monkeypatch.setattr(pvm.logger, "error",
                        lambda msg, *a, **k: errors.append(msg))

    src = [
        {"index": 1, "timing": _TIMING, "text": "部長で"},
        {"index": 2, "timing": _TIMING, "text": "ボクたち、水泳部の部長で"},
    ]
    tgt = [
        {"index": 1, "timing": _TIMING, "text": "作为部长"},
        {"index": 2, "timing": _TIMING, "text": "我是游泳部的部长"},
    ]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    # dewei：可安全自动修正 → 仍修正，且告警升级为硬性
    assert fixes == 1
    assert tgt[0]["text"] == "是部长"
    assert any("[硬性]" in w and "で误译修正" in w for w in warnings)
    # subject：只硬性告警，绝不自动改写
    assert tgt[1]["text"] == "我是游泳部的部长"
    assert any("[硬性]" in w and "主语误判" in w for w in warnings)
    assert len(errors) == len(warnings)      # 每条硬性告警均 logger.error
    assert 1 in flagged and 2 in flagged
