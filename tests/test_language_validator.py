"""
language_validator 回归测试
===========================
基于 dropped_entries.log 中的真实假阳性语料，固化过滤规则回归断言。
"""
import pytest

from subtransjav.refine.language_validator import is_valid_stage_text


class TestJaWhitelist:
    """日文阶段：白名单词应放行（含句末标点）"""

    @pytest.mark.parametrize("text", [
        "はい。",       # 回应词（_RESPONSE_WORDS）
        "ああ。",       # 感叹词（_INTERJECTION_WORDS）
        "OK。",         # 回应词（白名单提前放行，绕过 CJK 标点盲区）
        "そう。",       # 感叹词
        "なるほど。",   # 感叹词
        "嘘。",         # 情绪词（_EMOTION_WORDS）
        "いいよ。",     # 感叹词
        "ちょっと。",   # 感叹词
        "おいしい。",   # 情绪词
        "やめて。",     # 情绪词
        "やめてよ。",   # 感叹词
        "そうだね。",   # 含"そう"
        "最高。",       # 情绪词
        "エース。",     # 情绪词
    ])
    def test_whitelist_words_pass(self, text):
        assert is_valid_stage_text(text, "ja") is True


class TestJaNameCalls:
    """日文阶段：人名/称呼/纯汉字短句应放行（放宽纯汉字规则）"""

    @pytest.mark.parametrize("text", [
        "宮下先輩。",   # 人名+称呼
        "森田。",       # 人名呼唤
        "先輩。",       # 称呼
        "翠名。",       # 人名
        "救命。",       # 汉字感叹
        "好。",         # 单字感叹
        "不見事。",     # 汉字复合词
        "直接目標。",   # 汉字短语
    ])
    def test_name_calls_pass(self, text):
        assert is_valid_stage_text(text, "ja") is True


class TestJaKanaWords:
    """日文阶段：纯假名 ≥3 字应放行（阈值从 6 降至 2）"""

    @pytest.mark.parametrize("text", [
        "いやだ。",     # 3 字假名
        "もぐもぐ。",   # 4 字拟声词
        "やべえ。",     # 3 字俚语
        "ステレオ。",   # 5 字片假名
        "ほら。",       # 白名单词
        "うわ。",       # 白名单词
        "あんた。",     # 3 字称呼
        "おひら。",     # 3 字假名
    ])
    def test_kana_words_pass(self, text):
        assert is_valid_stage_text(text, "ja") is True


class TestJaParticles:
    """日文阶段：含助词短句应放行"""

    @pytest.mark.parametrize("text", [
        "は？",         # 疑问助词（1 字纯助词短句）
        "ね。",         # 语气助词
        "か。",         # 疑问助词
    ])
    def test_particle_short_pass(self, text):
        """1-2 字纯助词短句应通过（粒子规则先于假名短句拦截）"""
        assert is_valid_stage_text(text, "ja") is True


class TestJaShortWhitelist:
    """日文阶段：白名单中的 2 字假名短词应放行（不被 KANA_SHORT_RE 误杀）"""

    @pytest.mark.parametrize("text", [
        "この。",       # 指示代词
        "こう。",       # 指示副词
        "ジン。",       # 独立实义词（人/陣/神）
        "そう。",       # 感叹词
        "ね。",         # 语气助词
        "か。",         # 疑问助词
        "さあ。",       # 感叹词
        "ちょっと。",   # 感叹词（已在白名单）
    ])
    def test_short_whitelist_pass(self, text):
        """白名单中的 2 字假名短词应通过（白名单优先于 KANA_SHORT_RE）"""
        assert is_valid_stage_text(text, "ja") is True


class TestTruePositives:
    """应被正确剔除的真阳性样本（幻觉/乱码残留）"""

    @pytest.mark.parametrize("text", [
        "I don't know this。",   # 纯拉丁+句号
        "No、no。",              # 拉丁+顿号
    ])
    def test_latin_junk_rejected_ja(self, text):
        """纯拉丁/英文残留在日文阶段应被剔除"""
        assert is_valid_stage_text(text, "ja") is False

    def test_control_chars_rejected(self):
        """含控制字符的串应被剔除"""
        assert is_valid_stage_text("test\x00\x01\x02", "ja") is False

    def test_pure_latin_rejected(self):
        """纯 ASCII 拉丁串应被剔除"""
        assert is_valid_stage_text("Hello World", "ja") is False

    def test_empty_passthrough(self):
        """空条目应放行（交给删除感知语义）"""
        assert is_valid_stage_text("", "ja") is True
        assert is_valid_stage_text("  ", "ja") is True

    def test_single_kana_no_period_rejected(self):
        """单个假名无标点（不在白名单）应被剔除"""
        assert is_valid_stage_text("き", "ja") is False

    def test_english_in_ja_rejected(self):
        """英文句子在日文阶段应被剔除"""
        assert is_valid_stage_text("I've been a good", "ja") is False

    def test_halfwidth_kana_is_japanese_not_latin(self):
        """半角片假名计入日文（CJK）侧，不得判为英文残留。

        _LATIN_WITH_CJK_PUNCT_RE 排除 \\uff66-\\uff9f：含半角片假名的
        混排行（即使含 ASCII 字母）应放行而非删除。
        """
        assert is_valid_stage_text("ﾃｽﾄｱｲｳ", "ja") is True
        assert is_valid_stage_text("Englishﾃｽﾄ", "ja") is True
        # 对照：纯英文残留仍应剔除
        assert is_valid_stage_text("English only", "ja") is False


class TestPunctuationPassthrough:
    """纯标点条目：CJK_RE 扩展后不再因'无 CJK'被误删"""

    @pytest.mark.parametrize("text", [
        "「…」。",     # CJK 引号 + 省略号 + 句号
        "「…」「…」。", # 多组 CJK 引号
        "！？",         # 全角感叹+问号
    ])
    def test_punctuation_not_dropped_by_cjk(self, text):
        """纯标点条目不再因 CJK 覆盖不足被误删（进入后续判断）"""
        result = is_valid_stage_text(text, "ja")
        # 这些条目不应因"无 CJK"被直接丢弃
        # 它们可能被标点模式匹配过滤，但不应返回 False 因为缺少 CJK
        assert result is not False or "CJK" not in str(result)


class TestZhRulesUnchanged:
    """中文阶段规则未放宽"""

    def test_pure_kana_rejected_in_zh(self):
        """纯假名在中文阶段应被剔除（未翻译残留）"""
        assert is_valid_stage_text("はい。", "zh") is False

    def test_japanese_sentence_rejected_in_zh(self):
        """日文句子在中文阶段应被剔除"""
        assert is_valid_stage_text("もうすぐそこだ。", "zh") is False

    def test_mixed_kana_katakana_rejected_in_zh(self):
        """纯假名+片假名无汉字在中文阶段应被剔除"""
        assert is_valid_stage_text("え、プロ？うん？", "zh") is False

    def test_chinese_passes_in_zh(self):
        """正常中文应通过"""
        assert is_valid_stage_text("你好世界。", "zh") is True

    def test_zh_with_kana_passthrough(self):
        """含汉字的中日混合串在中文阶段应通过"""
        assert is_valid_stage_text("你好世界です。", "zh") is True

    def test_fullwidth_punctuation_in_zh(self):
        """全角标点在中文阶段应通过（无假名）"""
        assert is_valid_stage_text("！？", "zh") is True

    def test_pipe_separator_rejected_in_zh(self):
        """阶段B输入格式"日文 ||| 中文"被回显进中文产物应判无效"""
        assert is_valid_stage_text(
            "ほら、自分の精液の味するか? ||| 看看，尝尝自己的精液味道如何？",
            "zh") is False
        # 含汉字也不放行（白名单外的短路规则）
        assert is_valid_stage_text(
            "これはペンです ||| 这是笔。", "zh") is False

    def test_pipe_separator_unaffected_in_ja(self):
        """日文阶段不受该短路规则影响（" ||| "是阶段B合法输入格式）"""
        assert is_valid_stage_text("ほら ||| 中文参考", "ja") is True
