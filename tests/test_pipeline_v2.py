"""
v2 两阶段流水线测试：TM 命中替代 / 重试链 / 兜底档位 / 指令组装。
LLM 客户端以假实现注入（不联网）。
"""


from subtransjav.refine.config import RefineConfig, StageConfig
from subtransjav.refine import pipeline_v2 as pv
from subtransjav.translate.llm_client import BatchResult


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path, profile="cloud", **kw):
    cfg = RefineConfig(
        inputs=[],
        templates_dir=str(None) if False else "",
        tm_enabled=False,
        v2_profile=profile,
        **kw)
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "fake-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "lmstudio", "fake-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    return cfg


def _entries(*texts):
    return [{"index": i, "timing": f"00:00:0{i},000 --> 00:00:0{i},500",
             "text": t} for i, t in enumerate(texts, 1)]


class FakeClient:
    """按 index 脚本化返回译文的假客户端。"""

    def __init__(self, fail_a=(), fail_b=(), delete_a=(), delete_b=()):
        self.fail_a = set(fail_a)
        self.fail_b = set(fail_b)
        self.delete_a = set(delete_a)
        self.delete_b = set(delete_b)
        self.calls = []
        self.entry_log = []          # 每次调用收到的完整条目

    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          scene_threshold=60.0, progress=None):
        self.calls.append([e["index"] for e in entries])
        self.entry_log.append([dict(e) for e in entries])
        is_stage_b = any("|||" in e["text"] for e in entries)
        translations, deleted, failed = {}, set(), []
        for e in entries:
            i = e["index"]
            if is_stage_b:
                if i in self.delete_b:
                    deleted.add(i)
                elif i in self.fail_b:
                    failed.append(i)
                else:
                    translations[i] = f"审{i}"
            else:
                if i in self.delete_a:
                    deleted.add(i)
                elif i in self.fail_a:
                    failed.append(i)
                else:
                    translations[i] = f"译{i}"
        return BatchResult(translations=translations, deleted=deleted,
                           failed=failed)


class FakeTM:
    def __init__(self, hits=None):
        self.hits = hits or {}
        self.stored = []

    def lookup_exact(self, source, stage=0):
        return self.hits.get(source.strip())

    def store_batch(self, pairs):
        self.stored.extend(pairs)
        return len(pairs)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 指令组装
# ---------------------------------------------------------------------------

def test_load_v2_instruction(tmp_path):
    cfg = _make_cfg(tmp_path)
    system_text, user_prompt = pv._load_v2_instruction(
        cfg, "A", "", str(tmp_path))
    assert "净语翻译专家" in system_text
    assert "净语清洗并翻译成中文" in user_prompt


def test_load_v2_instruction_b_includes_hardened(tmp_path):
    cfg = _make_cfg(tmp_path)
    system_text, user_prompt = pv._load_v2_instruction(
        cfg, "B", "", str(tmp_path))
    assert "审校抛光专家" in system_text
    assert "硬性豁免规则" in system_text        # 代码自动追加
    assert "|||" in system_text


# ---------------------------------------------------------------------------
# 阶段A / 阶段B
# ---------------------------------------------------------------------------

def test_stage_a_tm_exact_hit_skips_llm(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは", "さようなら", "ありがとう")
    tm = FakeTM(hits={"こんにちは": "你好"})
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    result = pv._run_stage_a(cfg, entries, tm, str(tmp_path), [])
    # TM 命中行不进 LLM
    assert all(1 not in c for c in fake.calls)
    assert result.exact_hits == {1: "你好"}
    texts = {e["index"]: e["text"] for e in result.entries}
    assert texts[1] == "你好"
    assert texts[2] == "译2"


def test_retry_chain_a_fail_b_rescue(tmp_path, monkeypatch):
    """阶段A失败行 → 阶段B补译（重试链第2环）。"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは", "さようなら", "ありがとう")
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    a = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    # 手动注入：阶段A对 #2 失败
    a.entries = [
        e if e["index"] != 2 else
        {**e, "text": pv.UNTRANSLATED_PREFIX + e["text"]}
        for e in a.entries
    ]
    a.failed = {2}
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    final = pv._run_stage_b(cfg, a, entries, str(tmp_path), [])
    texts = {e["index"]: e["text"] for e in final}
    assert texts[2] == "审2"       # 阶段B补译成功
    assert "|||" not in texts[2]


def test_retry_chain_b_fail_falls_back_to_a(tmp_path, monkeypatch):
    """阶段B失败行 → 回退阶段A译文（重试链第3环）。"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは", "さようなら")
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    a = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    fake = FakeClient(fail_b={1})
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    final = pv._run_stage_b(cfg, a, entries, str(tmp_path), [])
    texts = {e["index"]: e["text"] for e in final}
    assert texts[1] == "译1"       # B失败回退A
    assert texts[2] == "审2"


def test_retry_chain_both_fail_keeps_original(tmp_path, monkeypatch):
    """A、B 都失败 → 保留日文原文（宁多勿缺，全自动）。"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは", "さようなら")
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    a = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    a.entries = [
        e if e["index"] != 1 else
        {**e, "text": pv.UNTRANSLATED_PREFIX + e["text"]}
        for e in a.entries
    ]
    a.failed = {1}
    fake = FakeClient(fail_b={1})
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    final = pv._run_stage_b(cfg, a, entries, str(tmp_path), [])
    texts = {e["index"]: e["text"] for e in final}
    assert texts[1] == "こんにちは"    # 回退日文原文


def test_stage_b_deletion(tmp_path, monkeypatch):
    """阶段B删除的条目不进终稿。"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは", "あ")
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    a = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    fake = FakeClient(delete_b={2})
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    final = pv._run_stage_b(cfg, a, entries, str(tmp_path), [])
    assert [e["index"] for e in final] == [1]


# ---------------------------------------------------------------------------
# 兜底档位
# ---------------------------------------------------------------------------

def test_strict_profile_applies_post_validate(tmp_path, monkeypatch):
    """local(strict) 档：で误译被代码层修正，warnings 一并返回。"""
    cfg = _make_cfg(tmp_path, profile="local")
    entries = _entries("部長で、エースで。")
    # 模拟阶段A产出了误译
    a_entries = [{"index": 1, "timing": entries[0]["timing"],
                  "text": "作为部长，作为王牌。"}]
    out, warns, clean_merged, flagged = pv._apply_fallback_rules(
        cfg, a_entries, entries)
    assert out[0]["text"] == "是部长，是王牌。"
    assert warns and "で误译修正" in warns[0]


def test_lenient_profile_skips_fallback(tmp_path):
    """cloud(lenient) 档：不做语义拦截，译文原样保留。"""
    cfg = _make_cfg(tmp_path, profile="cloud")
    entries = _entries("部長で、エースで。")
    a_entries = [{"index": 1, "timing": entries[0]["timing"],
                  "text": "作为部长，作为王牌。"}]
    out, warns, clean_merged, flagged = pv._apply_fallback_rules(
        cfg, a_entries, entries)
    assert out[0]["text"] == "作为部长，作为王牌。"
    assert warns == [] and clean_merged is None


# ---------------------------------------------------------------------------
# TM 学习
# ---------------------------------------------------------------------------

def test_learn_to_tm_stores_final_pairs():
    tm = FakeTM()
    orig = _entries("こんにちは", "さようなら")
    final = [{"index": 1, "timing": orig[0]["timing"], "text": "你好"},
             {"index": 2, "timing": orig[1]["timing"],
              "text": pv.UNTRANSLATED_PREFIX + "さようなら"}]
    pv._learn_to_tm(tm, orig, final)
    assert tm.stored == [("こんにちは", "你好", 1)]   # 未翻译行不入库


# ---------------------------------------------------------------------------
# 端到端（假客户端 + 假 TM + 临时目录）
# ---------------------------------------------------------------------------

def test_run_single_v2_end_to_end(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
        "2\n00:00:10,000 --> 00:00:11,000\nさようなら\n",
        encoding="utf-8")

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)

    out = pv._run_single_v2(cfg, str(in_srt))
    assert out.endswith("demo_final_cn.srt")
    content = open(out, encoding="utf-8").read()
    assert "审1" in content and "审2" in content
    # 阶段A中间产物存在
    assert (tmp_path / "demo_refine_A.srt").exists()
    # 两次 LLM 调用（阶段A + 阶段B）
    assert len(fake.calls) == 2


def test_run_single_v2_fallback_original_survives_language_filter(
        tmp_path, monkeypatch):
    """A、B 双失败的行回退日文原文后，不被 zh 语言白名单误删。

    keep_untranslated 回退的纯假名原文若参与 zh 白名单过滤会被判
    无效删除（与回退语义矛盾）；带 _keep_original 标记的条目应跳过过滤。
    """
    cfg = _make_cfg(tmp_path)
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nあ\n\n"
        "2\n00:00:10,000 --> 00:00:11,000\nこんにちは\n",
        encoding="utf-8")

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    fake = FakeClient(fail_a={1}, fail_b={1})
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)

    out = pv._run_single_v2(cfg, str(in_srt))
    content = open(out, encoding="utf-8").read()
    assert "あ" in content        # 回退日文原文保留（不被语言过滤删除）
    assert "审2" in content


# ---------------------------------------------------------------------------
# 云端故障本地接管（GUI「云端故障时本地接管」开关）
# ---------------------------------------------------------------------------

def test_cloud_fallback_rescues_failed_lines(tmp_path, monkeypatch):
    """云端阶段失败行 → 本地接管模型重跑修复。"""
    cfg = _make_cfg(tmp_path)
    cfg.fallback_local = True
    cfg.fallback_model = "local-fb"
    cfg.stages[0].provider = "deepseek"     # 阶段A 为云端
    entries = _entries("こんにちは", "さようなら", "ありがとう")

    class CloudFail(FakeClient):
        def translate_entries(self, entries, **kw):
            r = super().translate_entries(entries, **kw)
            r.failed = [e["index"] for e in entries]   # 云端全失败
            for i in r.failed:
                r.translations.pop(i, None)
            return r

    fb = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: CloudFail())
    monkeypatch.setattr(pv, "_make_fallback_client", lambda cfg: fb)
    result = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    # 接管模型吃到了失败行
    assert any(1 in c for c in fb.calls)
    texts = {e["index"]: e["text"] for e in result.entries}
    assert texts[1] == "译1" and texts[2] == "译2" and texts[3] == "译3"
    assert result.failed == set()


def test_cloud_fallback_disabled_for_local(tmp_path, monkeypatch):
    """本地阶段不触发接管（无云端可降级）。"""
    cfg = _make_cfg(tmp_path)
    cfg.fallback_local = True
    cfg.fallback_model = "local-fb"          # stages[0] 保持 lmstudio
    entries = _entries("こんにちは")

    class CloudFail(FakeClient):
        def translate_entries(self, entries, **kw):
            r = super().translate_entries(entries, **kw)
            r.failed = [e["index"] for e in entries]
            r.translations.clear()
            return r

    fb_calls = []
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: CloudFail())
    monkeypatch.setattr(pv, "_make_fallback_client",
                        lambda cfg: fb_calls.append(1) or FakeClient())
    result = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    assert fb_calls == []                    # 未触发接管
    assert result.failed == {1}


def test_cleaner_config_dir_wired(tmp_path, monkeypatch):
    """GUI「净语配置目录」→ strict 档 clean_srt(config_dir=...)。"""
    calls = {}
    import subtransjav.refine.cleaner_rules as cr
    def fake_clean_srt(srt, config_dir=None):
        calls["config_dir"] = config_dir
        return srt
    monkeypatch.setattr(cr, "clean_srt", fake_clean_srt)
    cfg = _make_cfg(tmp_path, profile="local")
    cfg.cleaner_config_dir = r"D:\custom\rules"
    entries = [{"index": 1, "timing": "t", "text": "你好"}]
    pv._apply_fallback_rules(cfg, entries, entries)
    assert calls["config_dir"] == r"D:\custom\rules"


# ---------------------------------------------------------------------------
# TM 污染防御
# ---------------------------------------------------------------------------

def test_tm_identical_pair_not_substituted(tmp_path, monkeypatch):
    """译文==原文的 ja→ja 残留对不替代（防日文漏进中文产物）。"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは", "さようなら")
    tm = FakeTM(hits={"こんにちは": "こんにちは"})   # 毒化对：原文==译文
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    result = pv._run_stage_a(cfg, entries, tm, str(tmp_path), [])
    # 未被替代 → 走 LLM
    assert all(1 in c for c in fake.calls)
    assert result.exact_hits == {}
    texts = {e["index"]: e["text"] for e in result.entries}
    assert texts[1] == "译1"


# ---------------------------------------------------------------------------
# 条目身份防错位（cleaner 重编号 / TM 学习对齐）
# ---------------------------------------------------------------------------

def test_timing_span():
    assert pv._timing_span("00:00:01,000 --> 00:00:02,000") == (1.0, 2.0)
    assert pv._timing_span("bad") == (-1.0, -1.0)


def test_cleaner_renumbering_index_restored(tmp_path, monkeypatch):
    """cleaner 重新编号后，必须按时间轴恢复原始 index（防错位）。"""
    import subtransjav.refine.cleaner_rules as cr

    def fake_clean_srt(srt, config_dir=None):
        # 模拟 cleaner：删掉第2条并重新编号（原文 index 1,3 → 输出 1,2）
        assert srt.count("-->") == 3
        return ("1\n00:00:01,000 --> 00:00:01,500\n译1\n\n"
                "2\n00:00:03,000 --> 00:00:03,500\n译3\n")
    monkeypatch.setattr(cr, "clean_srt", fake_clean_srt)

    cfg = _make_cfg(tmp_path, profile="local")
    orig = _entries("あ", "い", "う")
    entries = [{"index": e["index"], "timing": e["timing"], "text": f"译{e['index']}"}
               for e in orig]
    out, warns, clean_merged, _flagged = pv._apply_fallback_rules(cfg, entries, orig)
    assert clean_merged == 1
    by_start = {e["text"]: e["index"] for e in out}
    # 译1→index1，译3→index3（原始身份恢复，而非重编号后的 2）
    assert by_start["译1"] == 1
    assert by_start["译3"] == 3


def test_learn_skips_merged_and_mismatched_lines():
    """时间轴不一致（被合并/删除）的行不入 TM 库；同文残留对不入库。"""
    tm = FakeTM()
    orig = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "あ"},
        {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000", "text": "い"},
        {"index": 3, "timing": "00:00:05,000 --> 00:00:06,000", "text": "う"},
    ]
    final = [
        # index 1 正常保留
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "译甲"},
        # index 2 被合并：时间轴终点延长 → 不入库
        {"index": 2, "timing": "00:00:03,000 --> 00:00:06,000", "text": "译乙"},
        # index 3 同文残留（译文==原文）→ 不入库
        {"index": 3, "timing": "00:00:05,000 --> 00:00:06,000", "text": "う"},
    ]
    pv._learn_to_tm(tm, orig, final)
    assert tm.stored == [("あ", "译甲", 1)]


# ---------------------------------------------------------------------------
# 阶段A 辅助注入：语法提示 + TM 模糊参考
# ---------------------------------------------------------------------------

def test_grammar_hints_injected(tmp_path, monkeypatch):
    """SudachiPy 可用时，语法提示注入 LLM 输入（不污染原文与产物）。"""
    import subtransjav.refine.grammar_hint as gh
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは", "さようなら")
    monkeypatch.setattr(gh, "is_grammar_hint_available", lambda: True)
    monkeypatch.setattr(gh, "generate_grammar_hints",
                        lambda srt, idx, entries=None:
                        "【语法提示】\n- 补出\"是\"")
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    sent = fake.entry_log[0][0]["text"]
    assert "【语法提示】" in sent and "原文：こんにちは" in sent
    # 原始 entries 不被污染
    assert entries[0]["text"] == "こんにちは"


def test_grammar_hints_unavailable_skipped(tmp_path, monkeypatch):
    import subtransjav.refine.grammar_hint as gh
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは")
    monkeypatch.setattr(gh, "is_grammar_hint_available", lambda: False)
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    assert fake.entry_log[0][0]["text"] == "こんにちは"


def test_grammar_hints_injected_in_stage_b(tmp_path, monkeypatch):
    """阶段B 输入同样注入语法提示（`原文：日文 ||| 译文` 格式），
    且 LLM 回显的提示残留被 clean_grammar_hint_residue 兜底清理。"""
    import subtransjav.refine.grammar_hint as gh
    cfg = _make_cfg(tmp_path)
    entries = _entries("ボクたち、水泳部の部長で")
    monkeypatch.setattr(gh, "is_grammar_hint_available", lambda: True)
    monkeypatch.setattr(gh, "generate_grammar_hints",
                        lambda srt, idx, entries=None:
                        "【语法提示】\n- 定语，禁止译成\"我是…\"开头")

    class EchoClient(FakeClient):
        def translate_entries(self, entries, **kw):
            r = super().translate_entries(entries, **kw)
            # 模拟 LLM 回显提示残留
            for i in r.translations:
                r.translations[i] = \
                    f"【语法提示】\n- 定语，禁止\n原文：{r.translations[i]}"
            return r

    client = EchoClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: client)
    a = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    final = pv._run_stage_b(cfg, a, entries, str(tmp_path), [])
    # 阶段B收到的输入含提示段与 `原文：日文 ||| 译文` 格式
    b_input = client.entry_log[-1][0]["text"]
    assert "【语法提示】" in b_input
    assert "原文：ボクたち、水泳部の部長で ||| " in b_input
    # 阶段B产物：提示残留被清理
    assert "【语法提示】" not in final[0]["text"]
    assert "原文：" not in final[0]["text"]
    assert final[0]["text"] == "审1"


def test_fuzzy_reference_injected(tmp_path, monkeypatch):
    """TM 模糊命中（高阈值）注入参考译文，标注仅供参考。"""
    cfg = _make_cfg(tmp_path)
    entries = _entries("こんにちは、今日はいい天気ですね")

    class FuzzyTM(FakeTM):
        def lookup_fuzzy(self, source, stage=0, threshold=0.8):
            return [("こんにちは", "你好", 0.99)]
    tm = FuzzyTM()
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    pv._run_stage_a(cfg, entries, tm, str(tmp_path), [])
    sent = fake.entry_log[0][0]["text"]
    assert "【参考译文】你好" in sent
    assert "严禁照抄" in sent
    assert "原文：こんにちは、今日はいい天気ですね" in sent


def test_fuzzy_inject_disabled(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    cfg.tm_fuzzy_inject = False
    entries = _entries("こんにちは")

    class FuzzyTM(FakeTM):
        def __init__(self):
            super().__init__()
            self.fuzzy_calls = 0
        def lookup_fuzzy(self, source, stage=0, threshold=0.8):
            self.fuzzy_calls += 1
            return []
    tm = FuzzyTM()
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    pv._run_stage_a(cfg, entries, tm, str(tmp_path), [])
    assert tm.fuzzy_calls == 0


# ---------------------------------------------------------------------------
# 词库自动学习防污染 + 质量报告
# ---------------------------------------------------------------------------

def test_glossary_learn_filters_hallucinated_terms(tmp_path, monkeypatch):
    """提取出的术语必须真实存在于源文/译文中（防幻觉词入库）。"""
    import subtransjav.refine.glossary_learn as gl

    src = tmp_path / "src.srt"
    tgt = tmp_path / "tgt.srt"
    src.write_text("こんにちは", encoding="utf-8")
    tgt.write_text("你好", encoding="utf-8")
    learned = tmp_path / "learned.csv"

    monkeypatch.setattr(gl, "extract_glossary_from_pair", lambda *a, **k: [
        ("こんにちは", "你好"),      # 真实存在 → 入库
        ("幻覚用語", "幻覚訳"),      # 两边都不存在 → 过滤
        ("こんにちは", "乱译"),      # 译文不存在于译文文本 → 过滤
    ])

    n = gl.learn_from_s2_output(str(src), str(tgt), str(learned),
                                endpoint="http://127.0.0.1:1/v1")
    assert n == 1
    rows = learned.read_text(encoding="utf-8-sig").strip().splitlines()
    assert rows == ["こんにちは,你好"]


def test_quality_report_metrics():
    from subtransjav.refine.quality_report import build_quality_report

    orig = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "挿入する"},
        {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000", "text": "部長で、エースで。"},
    ]
    # 终稿漏掉第2条（实义漏覆盖）
    final = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
              "text": "插进去"}]
    report = build_quality_report(orig, final, "demo")
    assert "实义内容漏覆盖: 1/2 (50.0%)" in report
    assert "⚠️ 需人工复核" in report
    assert "[实义漏覆盖] #2" in report


def test_quality_report_pass():
    from subtransjav.refine.quality_report import build_quality_report
    orig = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
             "text": "挿入する"}]
    final = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
              "text": "插进去"}]
    report = build_quality_report(orig, final, "demo")
    assert "时间轴对齐率（对预合并后期望时间轴）: 100.0%" in report
    assert "✅ 通过，无待复核项" in report


def test_quality_report_flags_kana_and_untranslated():
    from subtransjav.refine.quality_report import build_quality_report
    orig = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
             "text": "あ"}]
    final = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
              "text": "こんにちは残留"}]
    report = build_quality_report(orig, final)
    # 中性描述：仅陈述"源文对应条目中未出现该串"，不断言幻觉/未译
    assert "[假名残留·需人工确认]" in report
    assert "源文对应条目中未出现该串" in report
    assert "假名残留 1" in report
    assert "疑似" not in report
    assert "⚠️ 需人工复核" in report


def test_quality_report_review_checklist():
    """复核工单：假名残留分类 / 未对齐 / 校验告警 逐条列出编号与时间轴。"""
    from subtransjav.refine.quality_report import build_quality_report

    expected = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
         "text": "ありがとう"},
        {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000",
         "text": "部長で、エースで。"},
        {"index": 3, "timing": "00:00:05,000 --> 00:00:06,000",
         "text": "カチューシャをかける"},
        {"index": 4, "timing": "00:00:07,000 --> 00:00:08,000",
         "text": "挿入する"},
        {"index": 5, "timing": "00:00:09,000 --> 00:00:10,000",
         "text": "ボクたち、水泳部の部長で"},
    ]
    final = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "谢谢"},
        # 假名残留：源文不含「あいしゃぶんねて」
        {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000",
         "text": "あいしゃぶんねて是部长"},
        # 假名残留：源文含相同假名串「カチューシャ」
        {"index": 3, "timing": "00:00:05,000 --> 00:00:06,000",
         "text": "戴着カチューシャ"},
        # 时间轴未对齐：期望 07,000-08,000，实际整体偏移
        {"index": 4, "timing": "00:00:07,500 --> 00:00:08,500", "text": "插进去"},
        # index 5 缺失（含汉字 → 实义漏覆盖）
    ]
    warnings = [
        "[硬性] ⚠️ #5 主语误判待复核: "
        "源含'僕たち/我们'但目标以'我是'开头 | 源: ボクたち、水泳部の部長で",
    ]
    report = build_quality_report(
        expected, final, "demo",
        expected_entries=expected,
        merge_stats={"premerge_merged": 1, "clean_merged": 2},
        validator_warnings=warnings)
    assert "⚠️ 需人工复核 6 处" in report
    # 假名残留统一中性标签，仅陈述事实
    assert "[假名残留·需人工确认]" in report
    assert "源文对应条目中未出现该串" in report
    assert "源文对应条目含相同串" in report
    assert "假名残留 2" in report
    assert "疑似" not in report
    assert "时间轴未对齐" in report
    assert "主语误判" in report
    assert "[实义漏覆盖] #5" in report
    # 逐条列出编号与时间轴
    assert "00:00:03,000 --> 00:00:04,000" in report
    assert "00:00:07,500 --> 00:00:08,500" in report
    assert "あいしゃぶんねて" in report
    assert "カチューシャ" in report
    # 统计行：条目链路与合并统计
    assert "条目: 原文 6 → 预合并后 5 → 终稿 4" in report
    assert "预合并合并 1 处 | 规则清洗合并 2 处" in report
    assert "时间轴对齐率（对预合并后期望时间轴）: 75.0%" in report
    # #4 未对齐 → 期望 #4 无对应终稿条目，同计漏覆盖
    assert "实义内容漏覆盖: 2/3 (66.7%)" in report


def test_quality_report_no_miss_threshold_lists_all():
    """漏覆盖不再设 2% 门槛：1 条也逐条列出。"""
    from subtransjav.refine.quality_report import build_quality_report
    orig = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
             "text": "挿入する"}]
    final = []
    report = build_quality_report(orig, final, "demo")
    assert "[实义漏覆盖] #1" in report


def test_v2_config_new_fields():
    from subtransjav.refine.config import RefineConfig
    cfg = RefineConfig()
    assert cfg.tm_fuzzy_inject is True
    assert cfg.tm_fuzzy_threshold == 0.98
    assert cfg.quality_report is True
    assert cfg.auto_glossary is False


# ---------------------------------------------------------------------------
# 审查补充：run_v2 多文件隔离 / empty 分支 / v2 校验 / GUI 进度契约
# ---------------------------------------------------------------------------

def test_run_v2_multi_file_isolation(tmp_path, monkeypatch):
    """多文件处理：单文件失败不中断其余文件。"""
    cfg = _make_cfg(tmp_path)
    f1 = tmp_path / "a.srt"
    f2 = tmp_path / "b.srt"
    for f in (f1, f2):
        f.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n",
                     encoding="utf-8")
    calls = {"n": 0}
    def fake_single(c, p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟失败")
        return str(tmp_path / "b_final_cn.srt")
    monkeypatch.setattr(pv, "_run_single_v2", fake_single)
    cfg.inputs = [str(f1), str(f2)]
    out = pv.run_v2(cfg)
    assert out.endswith("b_final_cn.srt")


def test_run_v2_all_fail_raises(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(pv, "_run_single_v2",
                        lambda c, p: (_ for _ in ()).throw(RuntimeError("x")))
    cfg.inputs = [str(tmp_path / "a.srt")]
    try:
        pv.run_v2(cfg)
        raised = False
    except pv.RefineError:
        raised = True
    assert raised


def test_stage_b_keep_untranslated_empty(tmp_path, monkeypatch):
    """v2_keep_untranslated='empty'：A、B 双失败的行整条移除。"""
    cfg = _make_cfg(tmp_path)
    cfg.v2_keep_untranslated = "empty"
    entries = _entries("こんにちは", "さようなら")
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    a = pv._run_stage_a(cfg, entries, None, str(tmp_path), [])
    a.entries = [
        e if e["index"] != 1 else
        {**e, "text": pv.UNTRANSLATED_PREFIX + e["text"]}
        for e in a.entries
    ]
    a.failed = {1}
    fake = FakeClient(fail_b={1})
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    final = pv._run_stage_b(cfg, a, entries, str(tmp_path), [])
    assert [e["index"] for e in final] == [2]


def test_validate_v2_requires_stage_b_model():
    """v2 阶段A/阶段B（槽位0/2）必须配置模型（legacy 规则引擎跳过分支已删除）。"""
    from subtransjav.refine.config import RefineConfig, StageConfig
    cfg = RefineConfig(inputs=[__import__("os").path.abspath(__file__)])
    cfg.stages = [StageConfig(0, True, "lmstudio", ""),   # 阶段A 未配模型
                  StageConfig(1, False, "deepseek", ""),
                  StageConfig(2, True, "lmstudio", ""),    # 阶段B 未配模型
                  StageConfig(3, False, "lmstudio", "")]
    errs = cfg.validate()
    assert any("阶段A" in e for e in errs)
    assert any("阶段B" in e for e in errs)   # v2 下槽位2 必须校验模型


def test_gui_progress_contract():
    """GUI 进度条依赖的输出字符串契约（api.py 正则 ↔ 管线实际输出）。

    锁定两侧的格式字面量：任一侧改动措辞，此测试即失败，
    防止进度条静默退化为 indeterminate。
    """
    import re
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent

    llm_src = (root / "subtransjav/translate/llm_client.py")         .read_text(encoding="utf-8")
    pv_src = (root / "subtransjav/refine/pipeline_v2.py")         .read_text(encoding="utf-8")

    # api.py 解析用的正则对实际输出样例命中
    rx_stage = re.compile(r'\[STAGE\]\s*(.+)')
    rx_total = re.compile(r'共 (\d+) 条，分 (\d+) 批')
    rx_batch = re.compile(r'批次 (\d+)/(\d+)')
    assert rx_stage.search("[STAGE] 阶段A 净语+翻译")
    assert rx_total.search("[llm] 共 1200 条，分 63 批（并发=1）")
    assert rx_batch.search("   ⏳ 批次 3/63")

    # 源码中的格式字面量未被改动
    assert '共 {len(entries)} 条，分 {total} 批' in llm_src
    assert 'f"批次 {done[0]}/{total}"' in llm_src
    assert "f\"[STAGE] {V2_STAGE_NAMES['A']}\"" in pv_src
    assert "f\"[STAGE] {V2_STAGE_NAMES['B']}\"" in pv_src


# ---------------------------------------------------------------------------
# 断句预合并（修复 ASR 错误切割）
# ---------------------------------------------------------------------------

def _t(i, j):
    def fmt(sec):
        m = int(sec)
        ms = round((sec - m) * 1000)
        return f"00:00:{m:02d},{ms:03d}"
    return f"{fmt(i)} --> {fmt(j)}"


def test_premerge_aux_end_forces_merge():
    """助词/省略号结尾 + ≤1.0s 间隔 → 强制合并，时间轴取并集。"""
    entries = [
        {"index": 1, "timing": _t(1, 2), "text": "僕たち水泳部の部長で"},
        {"index": 2, "timing": _t(2.5, 4), "text": "エースで"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 1
    assert out[0]["text"] == "僕たち水泳部の部長でエースで"
    assert out[0]["timing"] == "00:00:01,000 --> 00:00:04,000"


def test_premerge_incomplete_start_merges():
    """≤0.5s 间隔 + 后条以接续词开头 → 合并。"""
    entries = [
        {"index": 1, "timing": _t(10, 12), "text": "それは"},
        {"index": 2, "timing": _t(12.3, 14), "text": "そういうことで"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 1
    assert out[0]["text"] == "それはそういうことで"


def test_premerge_complete_sentences_not_merged():
    """两条都是完整陈述句 → 不合并（哪怕间隔很小）。"""
    entries = [
        {"index": 1, "timing": _t(10, 12), "text": "ありがとうございます。"},
        {"index": 2, "timing": _t(12.2, 14), "text": "はい。"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 2


def test_premerge_respects_span_and_count_limits():
    """合并后 >8s 或 >3 条 → 停止合并。"""
    entries = [
        {"index": 1, "timing": "00:00:00,000 --> 00:00:03,000", "text": "あの出."},
        {"index": 2, "timing": "00:00:03,500 --> 00:00:06,000", "text": "そして"},
        {"index": 3, "timing": "00:00:06,500 --> 00:00:09,000", "text": "そして"},
        {"index": 4, "timing": "00:00:09,500 --> 00:00:12,000", "text": "そして"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 2          # 1+2+3 合并（6s, 3条），第4条超限独立


def test_premerge_large_gap_not_merged():
    entries = [
        {"index": 1, "timing": _t(1, 2), "text": "部長で"},
        {"index": 2, "timing": _t(10, 12), "text": "エースで"},
    ]
    assert len(pv._premerge_entries(entries)) == 2


def test_premerge_trailing_ellipsis_veto():
    """省略号收尾（刻意戏剧停顿）+ 后条自身完整句 → 一律不合并。

    回归用例：源 #2「ボクたち、水泳部の部長で…」+ 源 #3
    「誰もが一目置くエース。」曾被 ≤1.0s 强制档错误合并。
    """
    entries = [
        {"index": 1, "timing": _t(31, 31.71), "text": "ボクたち、水泳部の部長で…"},
        {"index": 2, "timing": _t(32.11, 35.149), "text": "誰もが一目置くエース。"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 2
    assert out[0]["text"] == "ボクたち、水泳部の部長で…"
    assert out[0]["timing"] == _t(31, 31.71)   # 结束时间不被拉长


def test_premerge_bare_particle_still_merges():
    """裸助词结尾（「で」）→ ≤1.0s 强制合并档保留（防回归）。"""
    entries = [
        {"index": 1, "timing": _t(1, 2), "text": "水泳部の部長で"},
        {"index": 2, "timing": _t(2.3, 3.5), "text": "エースだよ"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 1
    assert out[0]["text"] == "水泳部の部長でエースだよ"


def test_premerge_ellipsis_then_incomplete_start_merges():
    """省略号收尾 + 后条以接续词开头（后条非完整句）→ tier2 仍合并。"""
    entries = [
        {"index": 1, "timing": _t(1, 2), "text": "なんか…"},
        {"index": 2, "timing": _t(2.3, 3.5), "text": "えっと、その"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 1
    assert out[0]["text"] == "なんか…えっと、その"
