"""
v2 两阶段流水线测试：TM 命中替代 / 重试链 / 兜底档位 / 指令组装。
LLM 客户端以假实现注入（不联网）。
"""

import io
import json
import types
from pathlib import Path

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
    """合并后超跨度硬上限（5000ms）或 >3 条 → 停止合并。"""
    entries = [
        {"index": 1, "timing": "00:00:00,000 --> 00:00:01,000", "text": "あの出."},
        {"index": 2, "timing": "00:00:01,200 --> 00:00:02,000", "text": "そして"},
        {"index": 3, "timing": "00:00:02,200 --> 00:00:03,000", "text": "そして"},
        {"index": 4, "timing": "00:00:03,200 --> 00:00:04,000", "text": "そして"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 2          # 1+2+3 合并（3s/3000ms、3条），第4条超条数独立


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


# 真实回归 fixture：提取自 4k2.me@mfyd-074（一次测试）条目 9-12，逐字未改。
# 旧行为曾把 9+10 合并为 42,679→50,479、11+12 合并为 51,259→57,039。
_PREMERGE_FIXTURE = (Path(__file__).parent / "fixtures" / "premerge"
                     / "overmerge_9_10_11_12.srt")


def _premerge_fixture_entries():
    return pv.parse_srt(_PREMERGE_FIXTURE.read_text(encoding="utf-8"))


def _premerge_cfg(**kw):
    """轻量 cfg：仅携带预合并参数（直调 _premerge_entries 用）。"""
    base = dict(premerge_max_span_ms=5000, premerge_max_items=3,
                premerge_max_chars=80, premerge_min_fragment_chars=6)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_premerge_fixture_real_overmerge_chain_not_merged():
    """真实回归 fixture（4k2.me@mfyd-074 条目 9-12）：双省略号链
    9+10 / 11+12 均不得合并（旧产物 42,679→50,479 与 51,259→57,039）。

    三重防线：RC1 剥离行尾省略号后「です/になります」恢复句末判定；
    RC2 双省略号否决；RC3 跨度 7800/5780ms 超出 5000ms 硬上限。
    """
    entries = _premerge_fixture_entries()
    assert len(entries) == 4
    out = pv._premerge_entries(entries)
    assert len(out) == 4
    # timing 与 text 逐字保持（未被拉长、未被拼接）
    assert [e["timing"] for e in out] == [e["timing"] for e in entries]
    assert [e["text"] for e in out] == [e["text"] for e in entries]
    assert out[0]["timing"] == "00:00:42,679 --> 00:00:46,520"
    assert out[2]["timing"] == "00:00:51,259 --> 00:00:53,679"


def test_premerge_gap0_double_ellipsis_not_merged():
    """gap=0 双省略号（剥离停顿后前条为完整句尾、后条也以停顿收尾）
    → 不合并（RC2 回归：否决条款补「前省略号+后省略号」情形）。"""
    entries = [
        {"index": 1, "timing": _t(42.679, 46.52),
         "text": "もうそろそろ3年になるところです…"},
        {"index": 2, "timing": _t(46.52, 50.479),
         "text": "…撮り始めて3年、になります…"},
    ]
    assert len(pv._premerge_entries(entries)) == 2


def test_premerge_chain_within_hard_caps_still_merges():
    """正确碎片不回退：3 连链在硬上限内（跨度≤5000ms、字符≤80）仍合并为 1 条。"""
    entries = [
        {"index": 1, "timing": _t(1, 2), "text": "僕たち水泳部の部長で"},
        {"index": 2, "timing": _t(2, 3), "text": "みんなのエースで"},
        {"index": 3, "timing": _t(3, 4.5), "text": "泳いでいる"},
    ]
    out = pv._premerge_entries(entries)
    assert len(out) == 1
    assert out[0]["timing"] == "00:00:01,000 --> 00:00:04,500"
    assert out[0]["text"] == "僕たち水泳部の部長でみんなのエースで泳いでいる"


def test_premerge_span_hard_cap_exact_boundary():
    """跨度硬上限精确边界：5000ms 整过 / 5001ms 拒（round 防浮点毛刺）。"""
    ok = [
        {"index": 1, "timing": _t(10, 11), "text": "水泳部の部長で"},
        {"index": 2, "timing": _t(11.2, 15), "text": "エースだよ"},
    ]
    out = pv._premerge_entries(ok, _premerge_cfg(premerge_max_span_ms=5000))
    assert len(out) == 1                     # 合并跨度恰 5000ms → 放行
    over = [
        {"index": 1, "timing": _t(10, 11), "text": "水泳部の部長で"},
        {"index": 2, "timing": _t(11.2, 15.001), "text": "エースだよ"},
    ]
    out = pv._premerge_entries(over, _premerge_cfg(premerge_max_span_ms=5000))
    assert len(out) == 2                     # 5001ms → 拒


def test_premerge_char_cap_rejects_overlong_text():
    """字符上限：合并双方字符数之和（日文按字符数计）> premerge_max_chars → 拒。"""
    ok = [
        {"index": 1, "timing": _t(1, 2), "text": "あああああは"},   # 6 字
        {"index": 2, "timing": _t(2.3, 3.5), "text": "そこで"},     # 3 字 → 合计 9
    ]
    assert len(pv._premerge_entries(
        ok, _premerge_cfg(premerge_max_chars=10))) == 1
    over = [
        {"index": 1, "timing": _t(1, 2), "text": "あああああは"},   # 6 字
        {"index": 2, "timing": _t(2.3, 3.5), "text": "そこでした"},  # 5 字 → 合计 11
    ]
    assert len(pv._premerge_entries(
        over, _premerge_cfg(premerge_max_chars=10))) == 2


def test_premerge_min_fragment_chars_threshold_gates_merge():
    """语义断裂档「上行未完成+下行短碎片」受 premerge_min_fragment_chars 控制：
    4 字碎片 < 默认阈值 6 → 合并；阈值调小到 4 后不再视为短碎片 → 不合并。"""
    entries = [
        {"index": 1, "timing": _t(1, 2), "text": "今日の天気はとっても"},
        {"index": 2, "timing": _t(2.2, 3.2), "text": "良い感じ"},   # 4 字
    ]
    assert len(pv._premerge_entries(entries)) == 1        # 默认阈值 6：合并
    cfg = _premerge_cfg(premerge_min_fragment_chars=4)
    assert len(pv._premerge_entries(entries, cfg)) == 2   # 阈值 4：碎片不再合格


def test_run_single_v2_real_fixture_no_overmerge(tmp_path, monkeypatch):
    """端到端（真实 fixture）：终稿不得出现 42,679→50,479 / 51,259→57,039
    过度合并条；阶段A 送翻条目数保持 4（未被预合并缩减）。"""
    cfg = _make_cfg(tmp_path)
    in_srt = tmp_path / "mfyd-074.srt"
    in_srt.write_text(_PREMERGE_FIXTURE.read_text(encoding="utf-8"),
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
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "00:00:42,679 --> 00:00:50,479" not in content
    assert "00:00:51,259 --> 00:00:57,039" not in content
    assert len(fake.entry_log[0]) == 4


def test_run_single_v2_premerge_disabled_passthrough(tmp_path, monkeypatch):
    """premerge_enabled=False：本可合并的碎片原样透传（阶段A 收到原条数）。"""
    cfg = _make_cfg(tmp_path)
    cfg.premerge_enabled = False
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n僕たち水泳部の部長で\n\n"
        "2\n00:00:02,200 --> 00:00:03,500\nエースだよ\n",
        encoding="utf-8")

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    fake = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)

    pv._run_single_v2(cfg, str(in_srt))
    # 开关打开时这两条会被 ≤1.0s 助词档合并；关闭后原样透传
    assert len(fake.entry_log[0]) == 2


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
# 闸门0：送翻前源侧幻觉检测（预合并前剔除幻觉行，两档 profile 均执行）
# ---------------------------------------------------------------------------

def _write_gate0_srt(tmp_path):
    """含一条纯标点幻觉行的输入（时间轴间隔 >8s，避开预合并干扰）。"""
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
        "2\n00:00:20,000 --> 00:00:21,000\n。。。。。\n\n"
        "3\n00:00:40,000 --> 00:00:41,000\nさようなら\n",
        encoding="utf-8")
    return in_srt


def _wire_gate0_e2e(tmp_path, monkeypatch, fake):
    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    # 归档目录隔离到临时目录（不污染仓库 Errors/）
    import subtransjav.refine.source_hallucination as gate0
    monkeypatch.setattr(gate0, "_default_errors_dir",
                        lambda: str(tmp_path / "Errors"))


def test_gate0_filters_hallucination_before_llm(tmp_path, monkeypatch):
    """default 档：幻觉行在送翻前剔除（不进 LLM、归档、不进终稿）。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _write_gate0_srt(tmp_path)
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    # 阶段A 收到的条目不含幻觉行（闸门0 先于 LLM 与预合并执行）
    assert all("。。。。。" not in e["text"] for e in fake.entry_log[0])
    assert len(fake.entry_log[0]) == 2          # 3 条输入只送翻 2 条
    assert len(fake.calls) == 2
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "。。。。。" not in content
    assert "审1" in content and "审3" in content
    # 删除条目按「闸门0-类别名」归档
    log = (tmp_path / "Errors" / "dropped_entries.log").read_text(
        encoding="utf-8")
    assert "原因=闸门0-纯标点行" in log


def test_gate0_off_sends_hallucination_to_llm(tmp_path, monkeypatch):
    """off 档（对照）：幻觉行照常送翻（走完整 A→B 链路被译出）。"""
    cfg = _make_cfg(tmp_path)
    cfg.v2_source_filter = "off"
    in_srt = _write_gate0_srt(tmp_path)
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    assert any(e["text"] == "。。。。。" for e in fake.entry_log[0])
    assert len(fake.entry_log[0]) == 3          # 幻觉行照常送翻
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "审2" in content                     # 幻觉行被翻译（未在源头剔除）
    assert not (tmp_path / "Errors" / "dropped_entries.log").exists()


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


# ---------------------------------------------------------------------------
# H3：幻觉处置报告 / R8 摘要行 / gate0_summary 事件 / H4a 上游信号接线
# ---------------------------------------------------------------------------

def _read_gate0_report(tmp_path, name="demo"):
    return json.loads(
        (tmp_path / f"{name}_幻觉处置报告.json").read_text(encoding="utf-8"))


def test_gate0_report_generated_with_schema_contract(tmp_path, monkeypatch):
    """final 输出阶段原子写 {stem}_幻觉处置报告.json，schema 契约钉死。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _write_gate0_srt(tmp_path)
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    assert out.endswith("demo_final_cn.srt")
    report = _read_gate0_report(tmp_path)
    # 顶层键集合（契约：缺一不可、不可增减；H5 只增 quarantine/noise_left_empty）
    assert set(report) == {"report_version", "source", "gate0_ran", "mode",
                           "total", "deleted", "detected_total", "valve",
                           "categories", "samples", "upstream",
                           "quarantine", "noise_left_empty"}
    assert report["report_version"] == 1
    assert report["source"] == "demo.srt"
    assert report["gate0_ran"] is True
    assert report["mode"] == "default"
    assert report["total"] == 3
    assert report["deleted"] == 1 and report["detected_total"] == 1
    # valve 区块
    assert set(report["valve"]) == {"tripped", "pct", "message"}
    assert report["valve"] == {"tripped": False, "pct": 50, "message": None}
    # categories：七类别中文 label 齐全
    assert set(report["categories"]) == {
        "!串", "纯标点行", "不可发音辅音串", "重复循环", "片尾元信息",
        "孤立应答词", "无意义音节连缀"}
    assert report["categories"]["纯标点行"] == {"detected": 1, "deleted": 1}
    # samples：已删条目（编号/类别/原文）
    assert report["samples"] == [
        {"number": 2, "category": "纯标点行", "text": "。。。。。"}]
    # upstream：无旁车文件 → 无信号
    assert report["upstream"] == {
        "present": False, "status": None, "mileage_pct": None,
        "stale": False, "file": None, "warnings": []}
    # H5：default 正常删除 → 无候选、无隔离区产物、无留空删除
    assert report["quarantine"] == {"candidates": 0, "quarantined": 0,
                                    "file": None}
    assert report["noise_left_empty"] == 0
    assert not (tmp_path / "demo_隔离区.srt").exists()
    # 原子写不留 .tmp 残留
    assert not list(tmp_path.glob("*.tmp"))


def test_gate0_summary_line_in_summary_lines(tmp_path, monkeypatch):
    """R8：每文件 summary_lines 增加一行闸门0 计数。"""
    cfg = _make_cfg(tmp_path)
    in_srt = _write_gate0_srt(tmp_path)
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)
    cfg.inputs = [str(in_srt)]
    summary = {}
    pv.run_v2(cfg, summary_sink=summary)
    gate_lines = [ln for ln in summary["summary_lines"] if "闸门0" in ln]
    assert gate_lines == [
        "🚪 闸门0：删除 1/原始 3，检出计数 1（default 档，保险阀未触发）"]
    # 纯信息行不计入风险（risk_count 不因该行虚增）
    assert summary["risk_count"] == 0


def test_resume_rerun_reports_real_gate0_counts(tmp_path, monkeypatch):
    """resume 复用阶段A：闸门0 在管线头部无条件执行（受信 resume 下幂等），
    报告如实记录真实计数（gate0_ran=true、删除数保留），不归零。"""
    cfg = _make_cfg(tmp_path)
    # 自建含幻觉行的输入（_setup_e2e 的固定输入无可删条目，区分度不足）
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n。。。\n\n"
        "2\n00:00:10,000 --> 00:00:11,000\nさようなら\n",
        encoding="utf-8")

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    monkeypatch.setattr(pv, "_make_client",
                        lambda cfg, tag: InterruptingBClient())
    cfg.inputs = [str(in_srt)]
    with pytest.raises(KeyboardInterrupt):
        pv.run_v2(cfg)
    assert not (tmp_path / "demo_幻觉处置报告.json").exists()

    fake2 = FakeClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake2)
    cfg.resume = True
    pv.run_v2(cfg)
    report = _read_gate0_report(tmp_path)
    assert report["gate0_ran"] is True
    # 真实计数保留：纯标点行被闸门0 删除并归档，不因 resume 归零
    assert report["total"] == 2 and report["deleted"] == 1
    assert report["samples"] and report["samples"][0]["category"] == "纯标点行"


def test_suspect_upstream_tightens_gate0_and_annotates_report(
        tmp_path, monkeypatch, capsys):
    """H4a 接线：上游 status=suspect → 显著警告 + RiskCollector warning +
    tighten 传递给闸门0（3 连同文被收紧删除）+ 报告 upstream 标注。"""
    cfg = _make_cfg(tmp_path)
    in_srt = tmp_path / "demo.srt"
    rows = []
    for i, text in enumerate(["みんな", "みんな", "みんな", "こんにちは",
                              "さようなら", "また明日", "寒いね", "そうだね"]):
        s = 20 * i
        rows.append(f"{i + 1}\n00:{s // 60:02d}:{s % 60:02d},000 --> "
                    f"00:{(s + 1) // 60:02d}:{(s + 1) % 60:02d},000\n{text}\n")
    in_srt.write_text("\n".join(rows), encoding="utf-8")
    (tmp_path / "whisperjav_run.json").write_text(
        json.dumps({"status": "suspect"}), encoding="utf-8")
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    pv._run_single_v2(cfg, str(in_srt))
    # 显著警告（文本模式 print 到 stdout）
    assert "⚠️ 上游 ASR 信号：run 状态=suspect，转写可信度低" in capsys.readouterr().out
    # tighten 传递：3 连同文在送翻前删除（无信号时 default 档不删 3 连）
    assert len(fake.entry_log[0]) == 5
    assert all("みんな" not in e["text"] for e in fake.entry_log[0])
    # 报告 upstream 标注
    report = _read_gate0_report(tmp_path)
    assert report["upstream"]["present"] is True
    assert report["upstream"]["status"] == "suspect"
    assert any("闸门0 已收紧" in w for w in report["upstream"]["warnings"])
    # RiskCollector warning（风险清单落盘，stage=gate0）
    risk = json.loads(
        (tmp_path / "demo_风险清单.json").read_text(encoding="utf-8"))
    gate0_risks = [e for e in risk["events"]
                   if e["stage"] == "gate0" and "上游 ASR 信号" in e["reason"]]
    assert gate0_risks and gate0_risks[0]["severity"] == "warning"


def test_low_coverage_upstream_warns_without_tighten(tmp_path, monkeypatch):
    """覆盖率低于阈值 → RiskCollector warning + 报告标注；不触发 tighten。"""
    cfg = _make_cfg(tmp_path)
    in_srt = tmp_path / "demo.srt"
    rows = []
    for i, text in enumerate(["みんな", "みんな", "みんな", "こんにちは",
                              "さようなら", "また明日", "寒いね", "そうだね"]):
        s = 20 * i
        rows.append(f"{i + 1}\n00:{s // 60:02d}:{s % 60:02d},000 --> "
                    f"00:{(s + 1) // 60:02d}:{(s + 1) % 60:02d},000\n{text}\n")
    in_srt.write_text("\n".join(rows), encoding="utf-8")
    (tmp_path / "whisperjav_run.json").write_text(
        json.dumps({"status": "ok", "mileage_pct": 10}), encoding="utf-8")
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    pv._run_single_v2(cfg, str(in_srt))
    # 无收紧：3 连同文照常送翻（default 档 min_run=4）
    assert len(fake.entry_log[0]) == 8
    # 报告标注覆盖率 + 警告
    report = _read_gate0_report(tmp_path)
    assert report["upstream"]["mileage_pct"] == 10.0
    assert any("覆盖率" in w and "低于阈值" in w
               for w in report["upstream"]["warnings"])
    # 风险清单 warning（stage=gate0）
    risk = json.loads(
        (tmp_path / "demo_风险清单.json").read_text(encoding="utf-8"))
    cov_risks = [e for e in risk["events"]
                 if e["stage"] == "gate0" and "覆盖率" in e["reason"]]
    assert cov_risks and cov_risks[0]["severity"] == "warning"


def test_gate0_valve_trips_risk_and_report_message(tmp_path, monkeypatch):
    """保险阀触发 → RiskCollector warning + 报告 valve.message。"""
    cfg = _make_cfg(tmp_path)
    in_srt = tmp_path / "demo.srt"
    rows = []
    for i, text in enumerate(["。。。", "！！", "kkkk", "。。。。。",
                              "？？", "…", "こんにちは", "ありがとう",
                              "さようなら", "また明日"]):   # 6/10 = 60% > 50%
        s = 20 * i
        rows.append(f"{i + 1}\n00:{s // 60:02d}:{s % 60:02d},000 --> "
                    f"00:{(s + 1) // 60:02d}:{(s + 1) % 60:02d},000\n{text}\n")
    in_srt.write_text("\n".join(rows), encoding="utf-8")
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    pv._run_single_v2(cfg, str(in_srt))
    report = _read_gate0_report(tmp_path)
    assert report["valve"]["tripped"] is True
    assert report["valve"]["message"] == "拦截率超阈值，本文件降级为只计数模式"
    assert report["deleted"] == 0 and report["detected_total"] >= 6
    risk = json.loads(
        (tmp_path / "demo_风险清单.json").read_text(encoding="utf-8"))
    valve_risks = [e for e in risk["events"] if "保险阀触发" in e["reason"]]
    assert valve_risks and valve_risks[0]["severity"] == "warning"


def test_gate0_summary_ndjson_event_emitted(tmp_path, monkeypatch):
    """ndjson 模式：每文件闸门0 执行后发一次 gate0_summary
    （payload=报告去 samples 的摘要，含 valve）。"""
    cfg = _make_cfg(tmp_path)
    cfg.event_format = "ndjson"
    in_srt = _write_gate0_srt(tmp_path)
    fake = FakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)
    cfg.inputs = [str(in_srt)]
    buf = io.StringIO()
    pv.run_v2(cfg, event_stream=buf)
    events = [parse_event_line(ln) for ln in buf.getvalue().splitlines()
              if ln.strip()]
    summaries = [e for e in events if e["type"] == "gate0_summary"]
    assert len(summaries) == 1                       # 每文件恰好一次
    payload = summaries[0]["payload"]
    assert set(payload) == {"report_version", "source", "gate0_ran", "mode",
                            "total", "deleted", "detected_total", "valve",
                            "categories", "upstream",
                            "quarantine", "noise_left_empty"}   # 去 samples
    assert payload["source"] == "demo.srt"
    assert payload["gate0_ran"] is True
    assert payload["valve"]["tripped"] is False
    # H5：事件时点隔离区尚未回捞判定——quarantined/file 如实记 null
    assert payload["quarantine"] == {"candidates": 0, "quarantined": None,
                                     "file": None}
    assert "samples" not in payload


# ---------------------------------------------------------------------------
# H5：翻译后回捞（隔离区）——保险阀降级 × 流畅中文 → 移出主稿落隔离区
# ---------------------------------------------------------------------------

class ZhFakeClient(FakeClient):
    """译文固定为多字流畅中文的假客户端（触发 H5 流畅中文判定）。"""

    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          scene_threshold=60.0, progress=None):
        self.calls.append([e["index"] for e in entries])
        self.entry_log.append([dict(e) for e in entries])
        is_stage_b = any("|||" in e["text"] for e in entries)
        translations = {}
        for e in entries:
            i = e["index"]
            translations[i] = (f"审校好的中文{i}" if is_stage_b
                               else f"初译的中文{i}")
        return BatchResult(translations=translations, deleted=set(),
                           failed=[])


def _write_valve_srt(tmp_path):
    """3 条删五类幻觉行 + 7 条真实台词（3/10=30% > 20% 阈值触发降级）。"""
    in_srt = tmp_path / "demo.srt"
    texts = ["。。。", "！！", "kkkk", "こんにちは", "さようなら",
             "また明日", "寒いね", "そうだね", "ほんとに", "部長でエースで"]
    rows = []
    for i, text in enumerate(texts):
        s = 20 * i
        rows.append(f"{i + 1}\n00:{s // 60:02d}:{s % 60:02d},000 --> "
                    f"00:{(s + 1) // 60:02d}:{(s + 1) % 60:02d},000\n{text}\n")
    in_srt.write_text("\n".join(rows), encoding="utf-8")
    return in_srt


def test_quarantine_moves_fluent_zh_on_valve_trip(tmp_path, monkeypatch):
    """H5 e2e：保险阀降级 → 幻觉行进 LLM 被译出流畅中文 → final 移入
    隔离区（主稿移除、隔离区 SRT 原子写、报告/摘要行接线）。"""
    cfg = _make_cfg(tmp_path)
    cfg.v2_source_filter_valve_pct = 20
    in_srt = _write_valve_srt(tmp_path)
    fake = ZhFakeClient()
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)
    cfg.inputs = [str(in_srt)]
    summary = {}
    out = pv.run_v2(cfg, summary_sink=summary)
    # 隔离区产物：3 条候选被译成流畅中文 → 移出主稿落盘
    q_path = tmp_path / "demo_隔离区.srt"
    assert q_path.is_file()
    q_content = q_path.read_text(encoding="utf-8")
    assert "审校好的中文1" in q_content and "审校好的中文2" in q_content \
        and "审校好的中文3" in q_content
    assert "审校好的中文4" not in q_content       # 真实台词不进隔离区
    with open(out, encoding="utf-8") as f:
        content = f.read()
    # "中文1\n" 带 SRT 行尾比对，避免与条目 10 的 "中文10" 子串误判
    assert "审校好的中文1\n" not in content        # 已移出主稿
    assert "审校好的中文4" in content              # 真实台词照常在主稿
    # 报告接线：候选 3、隔离 3、产物文件名
    report = _read_gate0_report(tmp_path)
    assert report["quarantine"] == {"candidates": 3, "quarantined": 3,
                                    "file": "demo_隔离区.srt"}
    # R8 摘要行：隔离区信息行（不计入风险），闸门0 计数行仍恰一条
    q_lines = [ln for ln in summary["summary_lines"] if "隔离区" in ln]
    assert q_lines == ["📪 隔离区：3 条存疑译文已移出主稿，见 demo_隔离区.srt"]
    # R8 闸门0 计数行仍恰一条（降级风险行以 ⚠️ 前缀另计，不属信息行）
    assert sum(ln.startswith("🚪 闸门0") for ln in summary["summary_lines"]) == 1


def test_quarantine_skipped_when_translation_not_fluent(tmp_path, monkeypatch):
    """H5 守卫：候选条目译文非流畅中文（单字译文）→ 留在主稿、不落隔离区
    文件；上一轮残留隔离区随恢复类清理删除（空则不落文件）。"""
    cfg = _make_cfg(tmp_path)
    cfg.v2_source_filter_valve_pct = 20
    in_srt = _write_valve_srt(tmp_path)
    # 上一轮残留的隔离区文件：本轮无存疑译文 → 随清理删除且不重建
    (tmp_path / "demo_隔离区.srt").write_text("stale", encoding="utf-8")
    fake = FakeClient()              # 译N/审N：单汉字，不判流畅
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    assert not (tmp_path / "demo_隔离区.srt").exists()
    with open(out, encoding="utf-8") as f:
        assert "审1" in f.read()     # 候选条目留在主稿
    report = _read_gate0_report(tmp_path)
    assert report["quarantine"] == {"candidates": 3, "quarantined": 0,
                                    "file": None}


def test_noise_left_empty_counts_stage_a_deletions(tmp_path, monkeypatch):
    """H5-7：计数类/候选类条目中被阶段A 留空删除的数量进报告；被留空删除
    的候选无译文可回捞 → 隔离区为空不落文件。"""
    cfg = _make_cfg(tmp_path)
    cfg.v2_source_filter_valve_pct = 20
    in_srt = _write_valve_srt(tmp_path)
    fake = FakeClient(delete_a=(1, 2, 3))    # 阶段A 留空删除 3 条候选
    _wire_gate0_e2e(tmp_path, monkeypatch, fake)

    pv._run_single_v2(cfg, str(in_srt))
    report = _read_gate0_report(tmp_path)
    assert report["noise_left_empty"] == 3
    assert report["quarantine"]["candidates"] == 3
    assert report["quarantine"]["quarantined"] == 0
    assert not (tmp_path / "demo_隔离区.srt").exists()
