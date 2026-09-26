"""
TM 学习准入门槛测试
===================
覆盖 scan_learn_defect 单测 + _learn_to_tm 行为单测（gate 开/关）。
"""

from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine.post_validate import scan_learn_defect
from subtransjav.refine.tm import TranslationMemory

# ---------------------------------------------------------------------------
# scan_learn_defect 单测
# ---------------------------------------------------------------------------

class TestScanLearnDefect:
    """审计实证样例 + 放行组。"""

    def test_leak(self):
        """Translation 标记泄漏。"""
        assert scan_learn_defect(
            "歩いてる後ろ姿がエロくて…",
            "背影好色…… #687 Translation> 不行") == "leak"

    def test_kana(self):
        """tgt 含假名（且 tgt!=src）。"""
        assert scan_learn_defect(
            "…んぅ、こっちも",
            "……嗯，ピスしたい") == "kana"

    def test_src_prefix(self):
        """源文前缀残留：tgt 以 src 开头且更长（纯汉字用例避免 kana 规则先命中）。"""
        assert scan_learn_defect(
            "今天天气很好",
            "今天天气很好 出去走走吧") == "src_prefix"

    def test_placeholder(self):
        """占位符。"""
        assert scan_learn_defect(
            "…時間は。",
            "（无对应条目）") == "placeholder"

    def test_untranslated_with_space(self):
        """[未翻译] 占位（生成侧形态，带尾空格）。"""
        assert scan_learn_defect(
            "こんにちは",
            "[未翻译] Chicks。") == "untranslated"

    def test_untranslated_no_space(self):
        """[未翻译] 占位（提示词模板形态，无尾空格）。"""
        assert scan_learn_defect(
            "こんにちは",
            "[未翻译]こんにちは") == "untranslated"

    def test_untranslated_mark_inside_text_not_flagged(self):
        """前缀在译文中部的正常引用：不判 untranslated（其余规则可能
        命中其他类别，此处仅断言非 untranslated）。"""
        assert scan_learn_defect(
            "こんにちは",
            "あの[未翻译]って何？") != "untranslated"

    def test_len_ratio_short_tgt(self):
        """长度比离群：长源文 + 极短译文。"""
        src = "学校のプールで水泳部の練習があって、みんな一生懸命頑張っている"
        tgt = "泳池"
        assert scan_learn_defect(src, tgt) == "len_ratio"

    def test_len_ratio_reasonable_expansion(self):
        """长度比合理扩写（比例约1.5）→ None。"""
        src = "学校のプールで水泳部の練習があって"
        tgt = "在学校的游泳池里，游泳部正在进行训练"
        assert scan_learn_defect(src, tgt) is None

    def test_pass_short_source_exempt(self):
        """短源文豁免（w_src < 4）：叹词合法映射。"""
        assert scan_learn_defect("っ!?", "！？") is None

    def test_pass_normal(self):
        """正常翻译放行。"""
        assert scan_learn_defect("うん。", "嗯。") is None

    def test_pass_same_text(self):
        """同文残留：tgt==src 时 kana 规则豁免（上游防线已挡）。"""
        assert scan_learn_defect("はい。", "はい。") is None

    def test_pass_short_loanword(self):
        """外来词短译放行。"""
        assert scan_learn_defect("ザコ", "杂鱼") is None

    def test_clean_long_pair(self):
        """长源文正常翻译放行。"""
        assert scan_learn_defect(
            "今日はいい天気ですね、散歩しましょう",
            "今天天气真好，去散步吧") is None


# ---------------------------------------------------------------------------
# _learn_to_tm 行为单测
# ---------------------------------------------------------------------------

def _make_entries(texts):
    """构造条目列表：index 从1开始，时间轴间隔1秒。"""
    return [{"index": i, "timing": f"00:00:0{i},000 --> 00:00:0{i},500",
             "text": t} for i, t in enumerate(texts, 1)]


def test_learn_gate_filters_defects_and_flagged(tmp_path):
    """gate=True 时：缺陷行 + flagged 行被跳过，干净行入库。"""
    db_path = str(tmp_path / "test_tm.db")
    tm = TranslationMemory(db_path)

    # 5 行：2 行缺陷、1 行 flagged、2 行干净
    orig = _make_entries([
        "うん。",                           # 1: 干净
        "歩いてる後ろ姿がエロくて…",          # 2: leak 缺陷（src）
        "…んぅ、こっちも",                   # 3: kana 缺陷（src）
        "今日はいい天気だ。",                  # 4: 干净
        "さようなら。",                      # 5: 干净（被 flagged）
    ])
    final = _make_entries([
        "嗯。",                             # 1: 干净
        "背影好色…… Translation>",          # 2: leak 缺陷
        "……嗯，ピスしたい",                  # 3: kana 缺陷
        "今天天气真好。",                     # 4: 干净
        "再见。",                            # 5: flagged 行
    ])

    flagged = {5}  # index 5 被 validator 标记
    pv._learn_to_tm(tm, orig, final, flagged=flagged, gate=True)

    # 干净行 (1, 4) 入库，缺陷行 (2, 3) 和 flagged 行 (5) 不入库
    assert tm.has_exact("うん。", stage=1)
    assert tm.has_exact("今日はいい天気だ。", stage=1)
    assert not tm.has_exact("歩いてる後ろ姿がエロくて…", stage=1)
    assert not tm.has_exact("…んぅ、こっちも", stage=1)
    assert not tm.has_exact("さようなら。", stage=1)

    tm.close()


def test_learn_gate_off_all_pass(tmp_path):
    """gate=False 时：全部入库（A/B 验证模式）。"""
    db_path = str(tmp_path / "test_tm.db")
    tm = TranslationMemory(db_path)

    orig = _make_entries([
        "うん。",
        "歩いてる後ろ姿がエロくて…",
        "…んぅ、こっちも",
        "今日はいい天気だ。",
        "さようなら。",
    ])
    final = _make_entries([
        "嗯。",
        "背影好色…… Translation>",
        "……嗯，ピスしたい",
        "今天天气真好。",
        "再见。",
    ])

    flagged = {5}
    pv._learn_to_tm(tm, orig, final, flagged=flagged, gate=False)

    # gate=False：全部入库
    assert tm.has_exact("うん。", stage=1)
    assert tm.has_exact("歩いてる後ろ姿がエロくて…", stage=1)
    assert tm.has_exact("…んぅ、こっちも", stage=1)
    assert tm.has_exact("今日はいい天気だ。", stage=1)
    assert tm.has_exact("さようなら。", stage=1)

    tm.close()


def test_validator_antonym_warning_blocks_tm_learn(tmp_path):
    """validator 告警 → 不入 TM（直接用例）：antonym_yamete 命中的翻译对
    经 check_and_fix_translation_errors 产出 flagged_indexes，传入
    _learn_to_tm 后该行被学习门槛阻断（既有机制，零新代码）；干净行照常入库。

    全部为中性合成文本。
    """
    from subtransjav.refine.post_validate import check_and_fix_translation_errors

    orig = _make_entries([
        "やめて、やめてよ…",          # 1: antonym_yamete 命中（→别停）
        "今日はいい天気だ。",          # 2: 干净
    ])
    final = _make_entries([
        "别停，别停呀…",               # 1: 反义误译（仅告警，文本不改）
        "今天天气真好。",              # 2: 干净
    ])
    _fixes, warnings, flagged, _structured = check_and_fix_translation_errors(orig, final)
    assert 1 in flagged
    assert any("antonym_yamete" in w for w in warnings)

    db_path = str(tmp_path / "test_tm.db")
    tm = TranslationMemory(db_path)
    try:
        pv._learn_to_tm(tm, orig, final, flagged=flagged, gate=True)
        assert not tm.has_exact("やめて、やめてよ…", stage=1)   # 告警行不入库
        assert tm.has_exact("今日はいい天気だ。", stage=1)       # 干净行入库
    finally:
        tm.close()
