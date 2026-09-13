"""
Tests for subtransjav.refine.grammar_hint module.
"""



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SRT_CONNECTIVE = """\
1
00:00:01,000 --> 00:00:03,000
ボクたち、水泳部の部長で…

2
00:00:03,500 --> 00:00:05,500
先輩は優しいけど、厳しいです。

3
00:00:06,000 --> 00:00:08,000
今日は暑いですね。

"""

SRT_BOKUTACHI = """\
1
00:00:01,000 --> 00:00:03,000
先輩、お疲れ様です。

2
00:00:03,500 --> 00:00:05,500
ボクたち、水泳部の部長で…

3
00:00:06,000 --> 00:00:08,000
エースでもあるんです。

"""

SRT_SIMPLE = """\
1
00:00:01,000 --> 00:00:03,000
これは猫です。

2
00:00:03,500 --> 00:00:05,500
今日はいい天気ですね。

"""


# ---------------------------------------------------------------------------
# Test: connective particle detection
# ---------------------------------------------------------------------------

class TestConnectiveParticle:
    """T3: が/けど/けれども 接续助词检测。"""

    def test_connective_particle_split_token(self):
        """接续助词「けど」应被检测并提示。"""
        from subtransjav.refine.grammar_hint import generate_grammar_hints
        hint = generate_grammar_hints(SRT_CONNECTIVE, 2)
        # 应包含转折提示
        assert "けど" in hint or "转折" in hint or "不过" in hint

    def test_subject_ga_not_misclassified(self):
        """主语が 不应被误判为接续助词（转折）。

        注意：此测试在无 SudachiPy 环境下验证 fallback 模式。
        fallback 模式使用正则检测，不会产生"转折"误报。
        """
        from subtransjav.refine.grammar_hint import generate_grammar_hints
        # 「私がします」中的 が 是格助词（主语标记），不是接続助词
        srt = "1\n00:00:01,000 --> 00:00:03,000\n私がします\n\n"
        hint = generate_grammar_hints(srt, 1)
        # fallback 模式不会检测到转折（正则不匹配）
        # SudachiPy 模式下也不会（pos[1] 区分）
        assert "转折" not in hint
        assert "不过" not in hint
        # hint 可能为空（无规则匹配），这是正确的


# ---------------------------------------------------------------------------
# Test: context ellipsis detection
# ---------------------------------------------------------------------------

class TestContextEllipsis:
    """T5: 省略主语/主题检测。"""

    def test_bokutachi_context_ellipsis(self):
        """「ボクたち、水泳部の部長で…」应检测省略主语。"""
        from subtransjav.refine.grammar_hint import generate_grammar_hints
        hint = generate_grammar_hints(SRT_BOKUTACHI, 2)
        # 应包含省略提示或修饰关系提示
        assert hint  # 至少应有提示
        # 可能是省略主语或の-修饰关系
        assert "省略" in hint or "の" in hint or "部長" in hint


# ---------------------------------------------------------------------------
# Test: fallback when SudachiPy unavailable
# ---------------------------------------------------------------------------

class TestFallback:
    """T6: SudachiPy 不可用时的回退模式。"""

    def test_fallback_when_sudachi_unavailable(self):
        """回退模式应能检测基本的转折和修饰关系。"""
        from subtransjav.refine.filters import parse_srt
        from subtransjav.refine.grammar_hint import _detect_rules_fallback

        entries = parse_srt(SRT_CONNECTIVE)
        # 使用条目2（含けど）
        text = entries[1]["text"]
        hints = _detect_rules_fallback(text, entries[:1], entries[2:])
        # 应检测到转折
        assert any("けど" in h or "转折" in h for h in hints)


# ---------------------------------------------------------------------------
# Test: clean_grammar_hint_residue
# ---------------------------------------------------------------------------

class TestCleanResidue:
    """清理 LLM 输出中的语法提示残留。"""

    def test_clean_grammar_hint_residue(self):
        """应能清理【语法提示】和原文：标记。"""
        from subtransjav.refine.cleaner_rules import clean_grammar_hint_residue

        # 完整残留
        text1 = "【语法提示】\n- 补出\"是\"\n原文：ボクたち、水泳部の部長で…"
        cleaned1 = clean_grammar_hint_residue(text1)
        assert "【语法提示】" not in cleaned1
        assert "原文：" not in cleaned1
        assert "ボクたち" in cleaned1 or "水泳部" in cleaned1

        # 纯提示行
        text2 = "【语法提示】- 「水泳部の部長」→ 修饰关系"
        cleaned2 = clean_grammar_hint_residue(text2)
        assert "【语法提示】" not in cleaned2

        # 无残留（正常文本）
        text3 = "今日はいい天気ですね。"
        cleaned3 = clean_grammar_hint_residue(text3)
        assert cleaned3 == "今日はいい天気ですね。"

    def test_clean_residue_line_start_prefix(self):
        """孤立"原文："回显整行删除（仅限行首，注入格式总在行首）。"""
        from subtransjav.refine.cleaner_rules import clean_grammar_hint_residue

        # LLM 回显丢失【语法提示】块、仅剩行首"原文：…"行 → 整行清理
        assert clean_grammar_hint_residue("原文：ボクたち") == ""
        # 正文中的"原文："（非行首）不得被误删
        kept = "彼は原文：の意味を説明した"
        assert clean_grammar_hint_residue(kept) == kept

    def test_clean_residue_pipe_separator_echo(self):
        """阶段B"日文 ||| 中文"回显：只保留最后一个分隔符后的中文侧。"""
        from subtransjav.refine.cleaner_rules import clean_grammar_hint_residue

        # 提示块 + 原文行整体回显
        cleaned = clean_grammar_hint_residue(
            "【语法提示】\n- 定语，禁止\n"
            "原文：ほら、自分の精液の味するか? ||| 看看，尝尝自己的精液味道如何？")
        assert cleaned == "看看，尝尝自己的精液味道如何？"

        # 提示块丢失，仅剩"日文 ||| 中文"回显
        cleaned2 = clean_grammar_hint_residue(
            "ほら、自分の精液の味するか? ||| 看看，尝尝自己的精液味道如何？")
        assert cleaned2 == "看看，尝尝自己的精液味道如何？"


# ---------------------------------------------------------------------------
# Test: rules 7/8 - plural pronoun adnominal + で 中顿
# ---------------------------------------------------------------------------

SRT_DE_ADNOMINAL = """\
1
00:00:01,000 --> 00:00:03,000
ボクたち、水泳部の部長で…誰もが一目置くエース。

"""


class TestPluralAdnominalAndDeRentou:
    """规则⑦（僕たち+名词+で 定语结构）与规则⑧（で 中顿）。"""

    def test_fallback_rule7_plural_adnominal(self):
        """fallback 路径（无 Sudachi）：正则命中 → 输出定语提示。"""
        from subtransjav.refine.filters import parse_srt
        from subtransjav.refine.grammar_hint import _detect_rules_fallback

        text = parse_srt(SRT_DE_ADNOMINAL)[0]["text"]
        hints = _detect_rules_fallback(text, [], [])
        assert any("定语" in h and "禁止" in h for h in hints)

    def test_generate_rule7(self):
        """生成的提示包含"定语"与"禁止"（Sudachi 或 fallback 路径均应命中）。"""
        from subtransjav.refine.grammar_hint import generate_grammar_hints
        hint = generate_grammar_hints(SRT_DE_ADNOMINAL, 1)
        assert "定语" in hint
        assert "禁止" in hint

    def test_sudachi_rule7_when_available(self):
        """Sudachi 路径（已安装 sudachipy 时）：检测规则⑦命中。"""
        from subtransjav.refine.filters import parse_srt
        from subtransjav.refine.grammar_hint import _detect_rules, is_grammar_hint_available
        if not is_grammar_hint_available():
            return  # 未安装则由 fallback 用例覆盖
        text = parse_srt(SRT_DE_ADNOMINAL)[0]["text"]
        hints = _detect_rules(text, [], [])
        assert any("定语" in h and "禁止" in h for h in hints)

    def test_sudachi_rule8_de_rentou_when_available(self):
        """Sudachi 路径（已安装 sudachipy 时）：助動詞「だ」連用形「で」→ 中顿提示。"""
        from subtransjav.refine.grammar_hint import _detect_rules, is_grammar_hint_available
        if not is_grammar_hint_available():
            return  # 未安装则跳过（fallback 无法区分格助詞/助動詞，宁缺毋滥）
        hints = _detect_rules("僕たち水泳部の部長で", [], [])
        assert any("中顿" in h and "作为" in h for h in hints)

    def test_subject_sentence_no_false_positive(self):
        """僕たちが主语句（僕たちが公園で遊ぶ）不应触发规则⑦定语提示。"""
        from subtransjav.refine.grammar_hint import _detect_rules, is_grammar_hint_available
        if not is_grammar_hint_available():
            return
        hints = _detect_rules("僕たちが公園で遊ぶ", [], [])
        assert not any("定语" in h for h in hints)


# ---------------------------------------------------------------------------
# Test: entries parameter optimization
# ---------------------------------------------------------------------------

class TestEntriesParameter:
    """性能优化：传入预解析 entries 避免重复解析。"""

    def test_entries_parameter_optimization(self):
        """传入预解析 entries 应返回相同结果。"""
        from subtransjav.refine.filters import parse_srt
        from subtransjav.refine.grammar_hint import generate_grammar_hints

        entries = parse_srt(SRT_CONNECTIVE)

        # 不传 entries（内部解析）
        hint_no_entries = generate_grammar_hints(SRT_CONNECTIVE, 2)

        # 传入预解析 entries
        hint_with_entries = generate_grammar_hints(SRT_CONNECTIVE, 2, entries=entries)

        assert hint_no_entries == hint_with_entries

    def test_nonexistent_index_returns_empty(self):
        """不存在的条目序号应返回空字符串。"""
        from subtransjav.refine.grammar_hint import generate_grammar_hints
        hint = generate_grammar_hints(SRT_SIMPLE, 999)
        assert hint == ""

    def test_empty_srt_returns_empty(self):
        """空 SRT 应返回空字符串。"""
        from subtransjav.refine.grammar_hint import generate_grammar_hints
        hint = generate_grammar_hints("", 1)
        assert hint == ""


# ---------------------------------------------------------------------------
# Test: thread safety (D4)
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """D4 回归：多线程并发冷缓存 miss 时不得抛 RuntimeError: Already borrowed
    （单例惰性初始化与 tokenize 调用均已加锁）。"""

    def test_concurrent_cold_cache_tokenize(self):
        import threading

        from subtransjav.refine import grammar_hint as gh
        if not gh.is_grammar_hint_available():
            return  # 未安装 sudachipy 时无从触发，由既有 fallback 用例覆盖
        gh._tokenize_cached.cache_clear()
        gh._tokenizer_instance = None       # 强制多线程并发走单例冷初始化
        gh._sudachi_available = None
        texts = [f"ボクたち、水泳部の部長で…その{i}は良い天気ですね。"
                 for i in range(80)]        # 各不相同 → 全部缓存 miss
        errors = []

        def _worker():
            try:
                for t in texts:
                    gh._tokenize_cached(t)
            except Exception as e:          # noqa: BLE001  收集后统一断言
                errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == []
        # 并发后单例仍可用，且同一文本复算的分词面一致（Morpheme 无值相等，
        # 按 surface 序列比较）
        assert gh._get_tokenizer() is not None
        expected = [t.surface() for t in gh._tokenize_cached(texts[0])]
        gh._tokenize_cached.cache_clear()
        assert [t.surface() for t in gh._tokenize_cached(texts[0])] == expected
