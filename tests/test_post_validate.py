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
# subject_misjudge target_pattern（D4 收窄为单数）验收样例
# ---------------------------------------------------------------------------

def test_subject_target_pattern_samples():
    """re.match 语义（从头匹配）下的验收样例：仅单数"我/俺是"命中，
    复数"我们是"为正确方向不命中（负向前瞻排除）。"""
    from subtransjav.refine.post_validate import _get_compiled
    pat = _get_compiled()["subject"]["target"]
    assert pat.match("我是游泳部的部长"), "「我是…」（单数）应命中"
    assert pat.match("俺是部长"), "「俺是…」（单数）应命中"
    assert pat.match("我，是游泳部的部长"), "单数+逗号停顿应命中"
    assert not pat.match("我们是游泳部的部长"), "「我们是…」正确方向不命中"
    assert not pat.match("我们，是游泳部的部长"), "「我们，是…」正确方向不命中"
    assert not pat.match("我們是游泳部的部长"), "繁体复数「我們是」不命中"
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
    """源「僕たち、水泳部の部長で…」译「我，是游泳部的部长」（单数）→ 应告警，
    且文案含实际译文开头（动态引用）与"主语误判"字样。"""
    src = [{"index": 1, "timing": _TIMING, "text": "ボクたち、水泳部の部長で…"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "我，是游泳部的部长"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0                          # 仅告警，不改动译文
    assert len(warnings) == 1
    assert "主语误判" in warnings[0]           # quality_report 归类依赖此四字
    assert "我，是游泳部" in warnings[0]       # 动态引用实际命中的译文开头（前6字）
    assert "以'我是'开头" not in warnings[0]   # 废弃硬编码文案
    assert tgt[0]["text"] == "我，是游泳部的部长"
    assert 1 in flagged


def test_subject_singular_short_head_warns():
    """D4 验收：源「ボクたち、水泳部の部長で…」译「我是部长」（<6 字）→
    触发告警且文案含完整实际译文开头"我是部长"。"""
    src = [{"index": 1, "timing": _TIMING, "text": "ボクたち、水泳部の部長で…"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "我是部长"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert len(warnings) == 1
    assert "主语误判" in warnings[0]
    assert "我是部长" in warnings[0]
    assert 1 in flagged


def test_subject_plural_predicate_translation_no_warning():
    """用户实例回归：源「ボクたち、水泳部の部長で…」终稿以"我们，"开头
    （谓语读法，正确方向）→ 不告警；"我们是…"同样不告警。"""
    for plural_tgt in ("我们，是游泳部的部长", "我们是游泳部的部长"):
        src = [{"index": 1, "timing": _TIMING, "text": "ボクたち、水泳部の部長で…"}]
        tgt = [{"index": 1, "timing": _TIMING, "text": plural_tgt}]
        fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
        assert fixes == 0, plural_tgt
        assert warnings == [], f"复数正确方向不应告警: {plural_tgt}"
        assert not flagged, plural_tgt
        assert tgt[0]["text"] == plural_tgt    # 译文不被改动


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


# ---------------------------------------------------------------------------
# 反义误译规则（批次 B2，双侧锚定 warn_only）
# ---------------------------------------------------------------------------

def test_antonym_yamete_warns():
    """源 やめて/やめてよ + 译文"别停/不要停" → 告警（antonym_yamete），
    仅告警不改译文、无硬性前缀；告警不含"主语误判"（独立归类）。"""
    src = [{"index": 1, "timing": _TIMING, "text": "やめて、やめてよ…"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "别停，别停呀…"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert len(warnings) == 1
    assert "antonym_yamete" in warnings[0]     # 规则标识（主语误判以外的规则标识）
    assert "主语误判" not in warnings[0]
    assert "[硬性]" not in warnings[0]         # warn_only：仅进复核清单
    assert tgt[0]["text"] == "别停，别停呀…"    # 译文不被改动
    assert 1 in flagged                        # flagged_indexes 阻断 TM 学习


def test_antonym_yamete_negative_source_forms_no_warn():
    """源侧负向排除：やめないで/やめるな →"别停"是正确翻译，不得告警。"""
    for neg_src in ("やめないで、続けて…", "やめるなよ"):
        src = [{"index": 1, "timing": _TIMING, "text": neg_src}]
        tgt = [{"index": 1, "timing": _TIMING, "text": "别停下来，继续…"}]
        fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
        assert fixes == 0, neg_src
        assert warnings == [], f"やめないで/やめるな 正译不应告警: {neg_src}"
        assert not flagged, neg_src


def test_antonym_yamete_correct_target_no_warn():
    """目标集锚定："住手/停下"不在"别停/不要停"目标集，不告警。"""
    for good_tgt in ("住手！", "停下、停下……"):
        src = [{"index": 1, "timing": _TIMING, "text": "やめて！"}]
        tgt = [{"index": 1, "timing": _TIMING, "text": good_tgt}]
        fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
        assert fixes == 0, good_tgt
        assert warnings == [], f"'{good_tgt}'为正确方向不应告警"
        assert not flagged, good_tgt


def test_antonym_saitei_warns_and_correct_no_warn():
    """最低=差劲：译"真不错/真棒/不错/挺好"即反义 → 告警（antonym_saitei）；
    译"真差劲"为正确方向 → 不告警。"""
    src = [{"index": 1, "timing": _TIMING, "text": "最低じゃねぇな。"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "真不错啊。"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert len(warnings) == 1
    assert "antonym_saitei" in warnings[0]
    assert "[硬性]" not in warnings[0]
    assert tgt[0]["text"] == "真不错啊。"
    assert 1 in flagged

    src2 = [{"index": 1, "timing": _TIMING, "text": "最低だな。"}]
    tgt2 = [{"index": 1, "timing": _TIMING, "text": "真差劲啊。"}]
    _fixes, warnings2, flagged2 = check_and_fix_translation_errors(src2, tgt2)
    assert warnings2 == [] and not flagged2


def test_antonym_zurui_warns_and_correct_no_warn():
    """ずるい/ずりー=狡猾/不公平，非"滑"：译"滑下去了" → 告警
    （antonym_zurui）；译"狡猾"为正确方向 → 不告警。"""
    src = [{"index": 1, "timing": _TIMING, "text": "ずりーよな"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "滑下去了"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert len(warnings) == 1
    assert "antonym_zurui" in warnings[0]
    assert "[硬性]" not in warnings[0]
    assert tgt[0]["text"] == "滑下去了"
    assert 1 in flagged

    src2 = [{"index": 1, "timing": _TIMING, "text": "ずるいよ、お前。"}]
    tgt2 = [{"index": 1, "timing": _TIMING, "text": "你太狡猾了。"}]
    _fixes, warnings2, flagged2 = check_and_fix_translation_errors(src2, tgt2)
    assert warnings2 == [] and not flagged2


def test_antonym_benign_sentences_no_warn():
    """良性句全不告警（无源文形态命中，或译文目标集未命中）。"""
    pairs = [
        ("水泳で鍛えてるだけあんじゃん。", "不愧是练游泳的啊。"),
        ("やめとこうか。", "就算了吧。"),
        ("今日もいい天気だね。", "今天天气也很好呢。"),
    ]
    for src_text, tgt_text in pairs:
        src = [{"index": 1, "timing": _TIMING, "text": src_text}]
        tgt = [{"index": 1, "timing": _TIMING, "text": tgt_text}]
        fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
        assert fixes == 0, src_text
        assert warnings == [], f"良性句不应告警: {src_text} → {tgt_text}"
        assert not flagged, src_text


# ---------------------------------------------------------------------------
# 批次 B 闭环：身体部位（首=脖子）与イク系变体（双侧锚定 warn_only）
# ---------------------------------------------------------------------------

def test_body_part_kubi_warns():
    """首を触られる→被摸头：身体语境 首=脖子（非头），译"摸头"应告警
    （body_part_kubi）；仅告警不改译文、无硬性前缀，flagged 阻断 TM。"""
    src = [{"index": 1, "timing": _TIMING, "text": "首を触られると落ち着く"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "被摸头就会安静下来"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert len(warnings) == 1
    assert "body_part_kubi" in warnings[0]
    assert "主语误判" not in warnings[0]
    assert "[硬性]" not in warnings[0]
    assert tgt[0]["text"] == "被摸头就会安静下来"
    assert 1 in flagged


def test_body_part_kubi_idiom_no_warn():
    """习语负向排除：首が回らない/首を長くして待って 译良性句不告警。"""
    pairs = [
        ("今月は首が回らない。", "这个月实在周转不开。"),
        ("首を長くして待ってね。", "望眼欲穿地等着呢。"),
    ]
    for src_text, tgt_text in pairs:
        src = [{"index": 1, "timing": _TIMING, "text": src_text}]
        tgt = [{"index": 1, "timing": _TIMING, "text": tgt_text}]
        fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
        assert fixes == 0, src_text
        assert warnings == [], f"习语句不应告警: {src_text} → {tgt_text}"
        assert not flagged, src_text


def test_climax_iku_variant_warns():
    """イっちゃう→要去了：假名变体 + 高潮类目标 → 复核告警
    （climax_iku_variant）；仅告警不改译文，flagged 阻断 TM。"""
    src = [{"index": 1, "timing": _TIMING, "text": "もうイっちゃう！"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "要去了！"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert len(warnings) == 1
    assert "climax_iku_variant" in warnings[0]
    assert "主语误判" not in warnings[0]
    assert "[硬性]" not in warnings[0]
    assert tgt[0]["text"] == "要去了！"
    assert 1 in flagged


def test_climax_iku_kanji_go_no_warn():
    """汉字 行く系 负向排除：行った→去了 属普通"去"，不告警。"""
    src = [{"index": 1, "timing": _TIMING, "text": "昨日も行った。"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "去了。"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert warnings == [], "汉字行った被负向排除，不应告警"
    assert not flagged


def test_climax_iku_glossary_exact_form_no_warn():
    """イク→要去了（glossary 强制译法本身）不告警：精确形态被负向排除，
    与 glossary 互补不冲突（复核旗只留给 glossary 覆盖不到的假名变体）。"""
    src = [{"index": 1, "timing": _TIMING, "text": "イク！"}]
    tgt = [{"index": 1, "timing": _TIMING, "text": "要去了！"}]
    fixes, warnings, flagged = check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert warnings == [], "glossary 精确形态イク不应告警"
    assert not flagged
