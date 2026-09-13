"""
v2 两阶段流水线测试：TM 命中替代 / 重试链 / 兜底档位 / 指令组装。
LLM 客户端以假实现注入（不联网）。
"""

import io
import json

import pytest

from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine.config import RefineConfig, StageConfig
from subtransjav.refine.events import parse_event_line
from subtransjav.refine.manifest import (
    MANIFEST_VERSION,
    StageRecord,
    TaskManifest,
    compute_config_hash,
    load_manifest,
    manifest_path,
    save_manifest,
)
from subtransjav.refine.tm import TranslationMemory
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


@pytest.fixture(autouse=True)
def _reset_grammar_cache():
    """P1-6 模块级语法缓存跨测试隔离：每个用例前后清空，防负缓存
    短路后续用例 mock 的 generate_grammar_hints（不弱化任何断言）。"""
    pv._GRAMMAR_CACHE.clear()
    yield
    pv._GRAMMAR_CACHE.clear()


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
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "审1" in content and "审2" in content
    # 任务成功完成：阶段A中间产物与恢复清单已自动清理（P0 新行为）
    assert not (tmp_path / "demo_refine_A.srt").exists()
    assert not (tmp_path / "demo_manifest.json").exists()
    assert not (tmp_path / "work").exists()   # 临时工作区一并清理
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
    with open(out, encoding="utf-8") as f:
        content = f.read()
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
    def fake_single(c, p, **kw):
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


# ---------------------------------------------------------------------------
# P0：中断恢复（manifest）/ 事件流 / 风险收集
# ---------------------------------------------------------------------------

class InterruptingBClient(FakeClient):
    """阶段B 批次调用时抛 KeyboardInterrupt（模拟用户 Ctrl+C）。"""

    def __init__(self):
        super().__init__()
        self.interrupted = False

    def translate_entries(self, entries, **kw):
        if any("|||" in e["text"] for e in entries) and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt()
        return super().translate_entries(entries, **kw)


class ProgressClient(FakeClient):
    """模拟 LLM 客户端的 ⏳ 批次进度回调。"""

    def translate_entries(self, entries, *, progress=None, **kw):
        if progress:
            progress("批次 1/2")
            progress("批次 2/2")
        return super().translate_entries(entries, progress=progress, **kw)


def _setup_e2e(tmp_path, monkeypatch, fake, name="demo"):
    """端到端公共装配：输入 srt + 假客户端 + 固定 tmp_dir + 无 TM。"""
    in_srt = tmp_path / f"{name}.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
        "2\n00:00:10,000 --> 00:00:11,000\nさようなら\n",
        encoding="utf-8")

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    return in_srt


def _run_interrupted(tmp_path, monkeypatch, cfg, name="demo"):
    """第一次运行：阶段B 中断（恢复清单应保留阶段A完成状态）。"""
    in_srt = _setup_e2e(tmp_path, monkeypatch, InterruptingBClient(), name=name)
    cfg.inputs = [str(in_srt)]
    with pytest.raises(KeyboardInterrupt):
        pv.run_v2(cfg)
    return in_srt


def test_a_success_cleans_resume_artifacts(tmp_path, monkeypatch):
    """场景a：任务成功完成后 manifest / _refine_A.srt / tmp_dir 全部清理。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _setup_e2e(tmp_path, monkeypatch, FakeClient())
    cfg.inputs = [str(in_srt)]
    summary = {}
    out = pv.run_v2(cfg, summary_sink=summary)
    assert out.endswith("demo_final_cn.srt")
    assert (tmp_path / "demo_final_cn.srt").is_file()
    assert not (tmp_path / "demo_manifest.json").is_file()
    assert not (tmp_path / "demo_refine_A.srt").is_file()
    assert not (tmp_path / "work").is_dir()
    assert summary["files_ok"] == 1 and summary["files_degraded"] == 0
    assert summary["files_failed"] == 0
    assert summary["untranslated_majority"] is False
    assert summary["risk_count"] == 0


def test_b_interrupt_keeps_manifest_with_stage_a_done(tmp_path, monkeypatch):
    """场景b：阶段B 中断 → manifest 保留且 stages["A"].status=="done"。"""
    cfg = _make_cfg(tmp_path)
    _run_interrupted(tmp_path, monkeypatch, cfg)
    m = load_manifest(tmp_path / "demo_manifest.json")
    assert m is not None
    assert m.stages["A"].status == "done"
    assert m.stages["B"].status == "running"
    assert (tmp_path / "demo_refine_A.srt").is_file()   # 断点产物保留
    assert not (tmp_path / "demo_final_cn.srt").is_file()


def test_c_resume_reruns_only_stage_b(tmp_path, monkeypatch, capsys):
    """场景c：--resume 重跑 → 只调用阶段B，最终产物正常，artifacts 清理。"""
    cfg = _make_cfg(tmp_path)
    _run_interrupted(tmp_path, monkeypatch, cfg)
    fake2 = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake2)
    cfg.resume = True
    out = pv.run_v2(cfg)
    assert out.endswith("demo_final_cn.srt")
    assert "阶段A产物复用" in capsys.readouterr().out
    # 仅阶段B 一次调用（B 输入带 ||| 标记）
    assert len(fake2.calls) == 1
    assert any("|||" in e["text"] for e in fake2.entry_log[0])
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "审1" in content and "审2" in content
    # 成功后恢复类产物清理
    assert not (tmp_path / "demo_manifest.json").is_file()
    assert not (tmp_path / "demo_refine_A.srt").is_file()
    assert not (tmp_path / "work").is_dir()


def test_d_resume_rejects_changed_config_unless_force(tmp_path, monkeypatch,
                                                      capsys):
    """场景d：指纹不匹配 → 默认重跑A；force_resume → 强制复用。"""
    cfg = _make_cfg(tmp_path)
    _run_interrupted(tmp_path, monkeypatch, cfg)

    # 不带 force：配置变化 → 不复用，阶段A 重跑（A+B 两次调用）
    fake2 = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake2)
    cfg.resume = True
    cfg.v2_concurrency = 3        # 参与 config_hash 但不影响行为
    pv.run_v2(cfg)
    assert "不复用" in capsys.readouterr().out
    assert len(fake2.calls) == 2

    # force_resume：配置变化仍复用阶段A（换名输入，避开终稿跳过分支；
    # 中断时先把配置还原，重跑时再改，制造指纹不匹配）
    cfg.resume = False
    cfg.force_resume = False
    cfg.v2_concurrency = 1
    _run_interrupted(tmp_path, monkeypatch, cfg, name="demo2")
    fake3 = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake3)
    cfg.resume = True
    cfg.force_resume = True
    cfg.v2_concurrency = 3
    pv.run_v2(cfg)
    out = capsys.readouterr().out
    assert "强制复用" in out
    assert "阶段A产物复用" in out
    assert len(fake3.calls) == 1
    assert any("|||" in e["text"] for e in fake3.entry_log[0])


def test_d2_force_resume_alone_implies_resume_and_reuses_a(tmp_path, monkeypatch,
                                                           capsys):
    """O4 回归：构造时仅传 force_resume（不传 resume）→ resume 隐含生效，
    指纹不匹配时阶段A 仍被复用；文案只列一次实际变化明细。"""
    cfg = _make_cfg(tmp_path, force_resume=True)
    assert cfg.resume is True         # 构造期不变式：force_resume 隐含 resume
    _run_interrupted(tmp_path, monkeypatch, cfg)

    # 制造指纹不匹配（v2_concurrency 参与 config_hash）
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    cfg.v2_concurrency = 3
    pv.run_v2(cfg)
    out = capsys.readouterr().out
    assert "强制复用（指纹校验不匹配：配置已变化）" in out   # 去重后不再出现两次"配置已变化"
    assert "阶段A产物复用" in out
    assert len(fake.calls) == 1       # 只跑阶段B（阶段A 复用）


def test_prepare_manifest_rejection_keeps_file_intact(tmp_path):
    """D3 验收（单元）：拒绝复用那一刻不得触碰清单文件——中断现场
    （A=done、旧指纹）原样保留；--force-resume 随后可采信并复用。"""
    cfg = _make_cfg(tmp_path)
    cfg.resume = True
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n",
                      encoding="utf-8")
    out_a = tmp_path / "demo_refine_A.srt"
    old = TaskManifest(
        manifest_version=MANIFEST_VERSION,
        input_path=str(in_srt),
        input_sha1="0" * 40,                    # 旧输入指纹（必失配）
        input_size=in_srt.stat().st_size,
        config_hash="1" * 40,                   # 旧配置指纹（必失配）
        out_dir=str(tmp_path), stem="demo",
        stages={"A": StageRecord(status="done", output=str(out_a), entries=1,
                                 degraded_count=0),
                "B": StageRecord(), "final": StageRecord()})
    m_path = manifest_path(tmp_path, "demo")
    save_manifest(m_path, old)
    before = m_path.read_text(encoding="utf-8")

    # 不带 force 的 --resume：被拒，但磁盘清单一字不改（A=done 不丢）
    manifest, trusted = pv._prepare_manifest(cfg, str(in_srt), str(tmp_path),
                                             "demo", None)
    assert trusted is False
    assert m_path.read_text(encoding="utf-8") == before
    on_disk = load_manifest(m_path)
    assert on_disk.stages["A"].status == "done"
    assert on_disk.config_hash == "1" * 40

    # --force-resume：强制采信旧产物、指纹刷新为当前值（复用前提成立）
    cfg.force_resume = True
    manifest2, trusted2 = pv._prepare_manifest(cfg, str(in_srt), str(tmp_path),
                                               "demo", None)
    assert trusted2 is True
    assert manifest2.stages["A"].status == "done"
    refreshed = load_manifest(m_path)
    assert refreshed.config_hash == compute_config_hash(cfg)
    assert refreshed.stages["A"].status == "done"


def test_resume_after_rejected_rerun_reuses_stage_a_without_force(
        tmp_path, monkeypatch, capsys):
    """D3 补充（场景续）：被拒的重跑在实际重跑阶段A时把新指纹落盘 →
    下次同配置 --resume 无需 force 即可复用阶段A。"""
    # 第一次运行：阶段B 中断（A=done，默认配置）
    cfg = _make_cfg(tmp_path)
    _run_interrupted(tmp_path, monkeypatch, cfg)

    # 第二次运行：改配置（concurrency=3）→ 不复用被拒，A 重跑后 B 再次中断
    cfg2 = _make_cfg(tmp_path)
    cfg2.resume = True
    cfg2.v2_concurrency = 3
    _run_interrupted(tmp_path, monkeypatch, cfg2)
    m = load_manifest(tmp_path / "demo_manifest.json")
    assert m.stages["A"].status == "done"
    assert m.config_hash == compute_config_hash(cfg2)   # 新指纹已落盘

    # 第三次运行：同配置（concurrency=3）--resume → 直接复用A，仅调阶段B
    fake3 = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake3)
    cfg3 = _make_cfg(tmp_path)
    cfg3.resume = True
    cfg3.v2_concurrency = 3
    cfg3.inputs = [str(tmp_path / "demo.srt")]
    out = pv.run_v2(cfg3)
    assert "阶段A产物复用" in capsys.readouterr().out
    assert len(fake3.calls) == 1
    assert out.endswith("demo_final_cn.srt")


# ---------------------------------------------------------------------------
# D5：TM 指纹不得被命中簿记（hit_count 自增 / WAL 回放）击穿
# ---------------------------------------------------------------------------

def test_tm_fingerprint_immune_to_hits_and_wal(tmp_path):
    """D5 验收1（单元）：任意数量精确命中（hit_count 自增、WAL 回放）
    后指纹不变。"""
    db = tmp_path / "tm.db"
    tm = TranslationMemory(str(db))
    tm.store("こんにちは", "你好", 1)
    tm.store("さようなら", "再见", 1)
    cfg = _make_cfg(tmp_path, tm_db_path=str(db))
    fp0 = pv._tm_fingerprint(cfg, tm)

    # lookup_exact / exact_map 每次命中都会自增 hit_count 并提交
    for _ in range(3):
        assert tm.lookup_exact("こんにちは", 1) == "你好"
    assert tm.exact_map(["さようなら", "こんにちは"], 1)["さようなら"] == "再见"
    assert pv._tm_fingerprint(cfg, tm) == fp0

    # 关闭连接、重新打开（WAL 回放 / 下次连接读取）后仍不变
    tm.close()
    assert pv._tm_fingerprint(cfg, None) == fp0
    tm2 = TranslationMemory(str(db))
    try:
        assert pv._tm_fingerprint(cfg, tm2) == fp0
    finally:
        tm2.close()


def test_tm_fingerprint_changes_on_content_change(tmp_path):
    """D5 验收2（单元）：新增/修改内容条目 → 指纹必变（拒绝复用）。"""
    db = tmp_path / "tm.db"
    tm = TranslationMemory(str(db))
    cfg = _make_cfg(tmp_path, tm_db_path=str(db))
    fp0 = pv._tm_fingerprint(cfg, tm)

    tm.store("こんにちは", "你好", 1)              # 新增内容条目
    fp1 = pv._tm_fingerprint(cfg, tm)
    assert fp1 != fp0

    tm.store("こんにちは", "您早", 1)              # 同 hash+stage 覆盖译文
    fp2 = pv._tm_fingerprint(cfg, tm)
    assert fp2 != fp1

    tm.store("こんにちは", "你好", 2)              # 新增 stage 维度条目
    fp3 = pv._tm_fingerprint(cfg, tm)
    assert fp3 != fp2
    tm.close()


def test_tm_fingerprint_edge_cases(tmp_path):
    """D5 验收3（单元）：未启用/文件缺失/非法文件/空库均不崩。"""
    assert pv._tm_fingerprint(_make_cfg(tmp_path), None) is None   # 无路径
    cfg_missing = _make_cfg(tmp_path, tm_db_path=str(tmp_path / "nope.db"))
    assert pv._tm_fingerprint(cfg_missing, None) is None           # 文件缺失
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"definitely not a sqlite database")
    cfg_bad = _make_cfg(tmp_path, tm_db_path=str(bad))
    assert pv._tm_fingerprint(cfg_bad, None) is None               # 非法文件
    empty_db = tmp_path / "empty.db"
    TranslationMemory(str(empty_db)).close()                       # 空库（无行）
    cfg_empty = _make_cfg(tmp_path, tm_db_path=str(empty_db))
    fp = pv._tm_fingerprint(cfg_empty, None)
    assert fp == pv._tm_fingerprint(cfg_empty, None)               # 稳定且不崩


def test_resume_reuses_stage_a_after_tm_hits(tmp_path, monkeypatch, capsys):
    """D5 验收1（端到端）：阶段A 的 TM 精确命中（hit_count 自增）后，
    同配置 --resume 必须复用阶段A，不再报「翻译记忆库已变化」。"""
    db = tmp_path / "tm.db"
    seed = TranslationMemory(str(db))
    seed.store("こんにちは", "你好", 1)
    seed.store("さようなら", "再见", 1)
    seed.close()

    conns = []

    def _fake_init_tm(cfg):
        t = TranslationMemory(str(db))   # 每次运行新连接（模拟跨进程/WAL 回放）
        conns.append(t)
        return t

    cfg = _make_cfg(tmp_path, tm_db_path=str(db))
    in_srt = _setup_e2e(tmp_path, monkeypatch, InterruptingBClient())
    monkeypatch.setattr(pv, "_init_tm", _fake_init_tm)
    cfg.inputs = [str(in_srt)]
    with pytest.raises(KeyboardInterrupt):
        pv.run_v2(cfg)                       # 阶段A 命中 TM → hit_count 自增

    fake2 = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake2)
    cfg2 = _make_cfg(tmp_path, tm_db_path=str(db))
    cfg2.resume = True
    cfg2.inputs = [str(in_srt)]
    out = pv.run_v2(cfg2)
    for t in conns:
        t.close()
    assert "阶段A产物复用" in capsys.readouterr().out   # reused=true
    assert len(fake2.calls) == 1                       # 仅阶段B 一次调用
    assert out.endswith("demo_final_cn.srt")


def test_e_fail_b_reports_degradation_and_risk_report(tmp_path, monkeypatch):
    """场景e：阶段B 部分失败 → files_degraded=1 + 风险清单 + 回退A译文事件。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _setup_e2e(tmp_path, monkeypatch, FakeClient(fail_b={1}))
    cfg.inputs = [str(in_srt)]
    summary = {}
    out = pv.run_v2(cfg, summary_sink=summary)
    assert summary["files_ok"] == 1
    assert summary["files_degraded"] == 1
    assert summary["files_failed"] == 0
    assert summary["risk_count"] >= 1
    assert not summary["untranslated_majority"]
    # B失败行回退A译文
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "译1" in content and "审2" in content
    # 风险清单文件生成，且包含 kept_a 降级语义
    report_json = tmp_path / "demo_风险清单.json"
    report_md = tmp_path / "demo_风险清单.md"
    assert report_json.is_file() and report_md.is_file()
    payload = json.loads(report_json.read_text(encoding="utf-8"))
    actions = [e["action"] for e in payload["events"]]
    assert "回退A译文" in actions
    kept = [e for e in payload["events"] if e["action"] == "回退A译文"]
    assert kept[0]["stage"] == "B"
    assert kept[0]["affected_count"] == 1
    assert len(kept[0]["samples"]) == 1


def test_f_all_fail_marks_untranslated_majority(tmp_path, monkeypatch):
    """场景f：A/B 全失败保留日文原文 → untranslated_majority 置位。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _setup_e2e(tmp_path, monkeypatch,
                        FakeClient(fail_a={1, 2}, fail_b={1, 2}))
    cfg.inputs = [str(in_srt)]
    summary = {}
    out = pv.run_v2(cfg, summary_sink=summary)
    assert summary["untranslated_majority"] is True
    assert summary["files_ok"] == 1 and summary["files_degraded"] == 1
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "こんにちは" in content and "さようなら" in content


def test_g_ndjson_events_parseable_and_progress_mapped(tmp_path, monkeypatch):
    """场景g：ndjson 模式事件流每行可解析，批次进度映射为 phase_progress。"""
    cfg = _make_cfg(tmp_path)
    cfg.event_format = "ndjson"
    in_srt = _setup_e2e(tmp_path, monkeypatch, ProgressClient())
    cfg.inputs = [str(in_srt)]
    buf = io.StringIO()
    summary = {}
    pv.run_v2(cfg, summary_sink=summary, event_stream=buf)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines
    events = [parse_event_line(ln) for ln in lines]
    assert all(e is not None for e in events)     # 每行都是合法事件行
    types = [e["type"] for e in events]
    assert types[0] == "task_started" and types[-1] == "task_finished"
    # 逐文件 A/B/final 三阶段 phase 事件
    assert [e["phase"] for e in events
            if e["type"] == "phase_started"] == ["A", "B", "final"]
    # 批次进度映射（阶段A/B 各 2 批）
    prog = [e for e in events if e["type"] == "phase_progress"]
    assert [(e["phase"], e["payload"]["done"], e["payload"]["total"])
            for e in prog] == [("A", 1, 2), ("A", 2, 2),
                               ("B", 1, 2), ("B", 2, 2)]
    # task_finished 汇总载荷
    tf = events[-1]["payload"]
    assert tf["status"] == "success"
    assert tf["files_ok"] == 1 and tf["risk_count"] == 0
    # 事件流不含人类可读文本（print 不进事件流）
    assert "阶段A" not in buf.getvalue()
    assert "refine-v2" not in buf.getvalue()


def test_g_text_mode_stdout_has_no_json_lines(tmp_path, monkeypatch, capsys):
    """场景g（对照）：text 模式 stdout 无 JSON 事件行，人类文本原样保留。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _setup_e2e(tmp_path, monkeypatch, FakeClient())
    cfg.inputs = [str(in_srt)]
    pv.run_v2(cfg)
    out = capsys.readouterr().out
    assert "阶段A 净语+翻译" in out
    assert all(parse_event_line(line) is None for line in out.splitlines())


def test_single_file_failure_counts_and_emits_error(tmp_path, monkeypatch):
    """单文件异常隔离：files_failed 计数 + critical 风险 + error 事件。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _setup_e2e(tmp_path, monkeypatch, FakeClient())
    bad = tmp_path / "empty.srt"
    bad.write_text("", encoding="utf-8")
    cfg.inputs = [str(bad), str(in_srt)]
    summary = {}
    pv.run_v2(cfg, summary_sink=summary)
    assert summary["files_ok"] == 1
    assert summary["files_failed"] == 1
    assert summary["risk_count"] >= 1
    payload = json.loads(
        (tmp_path / "demo_风险清单.json").read_text(encoding="utf-8"))
    failed = [e for e in payload["events"] if e["action"] == "该文件失败跳过"]
    assert failed and failed[0]["severity"] == "critical"
