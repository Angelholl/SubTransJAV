"""P1-6 性能优化测试
====================

覆盖：
- TM 批量 exact_map 与逐条 lookup_exact 语义一致（含 stage 过滤、
  原始输入串为键、hit_count 自增）；
- 阶段A 优先批量查询、无 exact_map 的对象回退逐条；
- 语法提示跨阶段缓存（命中短路、tag/profile 键隔离、LRU 逐条淘汰）；
- 云端多文件并行（opt-in）：两产物都在、summary 计数正确、
  本地服务商一律串行、单文件异常隔离；
- 词库学习异步化：慢学习不阻塞主流程，超时风险被记录；快速学习无警告。
"""

import threading
import time

import pytest

from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine.config import RefineConfig, StageConfig
from subtransjav.refine.tm import TranslationMemory
from subtransjav.translate.llm_client import BatchResult

# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_grammar_cache():
    pv._GRAMMAR_CACHE.clear()
    yield
    pv._GRAMMAR_CACHE.clear()


def _entries(*texts):
    return [{"index": i, "timing": f"00:00:0{i},000 --> 00:00:0{i},500",
             "text": t} for i, t in enumerate(texts, 1)]


class _TMBase:
    """TM 公共骨架：模糊查询空、学习入库、可关闭。"""

    def __init__(self, mapping=None):
        self.mapping = dict(mapping or {})
        self.lookup_calls = 0
        self.stored = []

    def lookup_fuzzy(self, source, stage=0, threshold=0.8):
        return []

    def store_batch(self, pairs):
        self.stored.extend(pairs)
        return len(pairs)

    def close(self):
        pass


class CountingTM(_TMBase):
    """带 exact_map 计数的假 TM（模拟真实 TranslationMemory 接口）。"""

    def __init__(self, mapping=None):
        super().__init__(mapping)
        self.exact_map_calls = 0

    def exact_map(self, sources, stage=0):
        self.exact_map_calls += 1
        return {s: self.mapping[s] for s in sources if s in self.mapping}

    def lookup_exact(self, source, stage=0):
        self.lookup_calls += 1
        return self.mapping.get(source)


class LegacyTM(_TMBase):
    """无 exact_map 属性的旧接口对象（验证逐条回退）。"""

    def lookup_exact(self, source, stage=0):
        self.lookup_calls += 1
        return self.mapping.get(source)


def _v2_cfg(tmp_path, **kw):
    cfg = RefineConfig(quality_report=False, tm_enabled=False,
                       v2_profile="cloud", **kw)
    cfg.stages = [
        StageConfig(0, True, "custom", "fake-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "custom", "fake-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    cfg.endpoints = {"custom": "http://localhost:9/v1"}
    return cfg


# ---------------------------------------------------------------------------
# TM 批量化：exact_map
# ---------------------------------------------------------------------------

def test_exact_map_matches_per_entry_lookup(tmp_path):
    tm = TranslationMemory(str(tmp_path / "tm.db"))
    try:
        for i in range(30):
            tm.store(f"原文その{i}", f"译文{i}", 1)
        tm.store("仅阶段2", "译二", 2)

        sources = [f"原文その{i}" for i in range(30)] + ["未入库原文",
                                                         "原文その0"]
        batch = tm.exact_map(sources, 1)
        for src in sources:
            assert batch.get(src) == tm.lookup_exact(src, 1)
        assert batch["原文その1"] == "译文1"
        assert "仅阶段2" not in batch            # stage 过滤
        assert "未入库原文" not in batch         # 未命中不以键出现
    finally:
        tm.close()


def test_exact_map_increment_hit_count(tmp_path):
    tm = TranslationMemory(str(tmp_path / "tm.db"))
    try:
        tm.store("こんにちは", "你好", 1)
        before = tm.stats()["total_hits"]
        tm.exact_map(["こんにちは", "こんにちは"], 1)
        assert tm.stats()["total_hits"] - before == 1   # 同条目只自增一次
    finally:
        tm.close()


def test_exact_map_empty_and_whitespace_inputs(tmp_path):
    tm = TranslationMemory(str(tmp_path / "tm.db"))
    try:
        assert tm.exact_map([], 1) == {}
        assert tm.exact_map(["", "   "], 1) == {}
    finally:
        tm.close()


# ---------------------------------------------------------------------------
# 阶段A 批量命中 + 逐条回退
# ---------------------------------------------------------------------------

class FakeClient:
    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          progress=None):
        is_b = any("|||" in e["text"] for e in entries)
        return BatchResult(
            translations={e["index"]: ("审" if is_b else "译") + str(e["index"])
                          for e in entries},
            deleted=set(), failed=[])


def test_stage_a_uses_batch_exact_map(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    tm = CountingTM({"こんにちは": "你好"})
    cfg = _v2_cfg(tmp_path)
    result = pv._run_stage_a(cfg, _entries("こんにちは", "さようなら"),
                             tm, str(tmp_path), [])
    assert tm.exact_map_calls == 1             # 单次批量查询
    assert tm.lookup_calls == 0                # 不再逐条
    assert result.exact_hits == {1: "你好"}


def test_stage_a_falls_back_to_per_entry_without_exact_map(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    tm = LegacyTM({"こんにちは": "你好"})
    cfg = _v2_cfg(tmp_path)
    result = pv._run_stage_a(cfg, _entries("こんにちは"),
                             tm, str(tmp_path), [])
    assert not hasattr(tm, "exact_map")        # 旧接口对象
    assert tm.lookup_calls == 1                # 回退逐条
    assert result.exact_hits == {1: "你好"}


# ---------------------------------------------------------------------------
# 语法提示跨阶段缓存
# ---------------------------------------------------------------------------

def _patch_fake_generator(monkeypatch, counter):
    import subtransjav.refine.grammar_hint as gh
    monkeypatch.setattr(gh, "is_grammar_hint_available", lambda: True)

    def fake_generate(srt, idx, entries=None):
        counter["n"] += 1
        return "【语法提示】\n- 测试"

    monkeypatch.setattr(gh, "generate_grammar_hints", fake_generate)


def test_grammar_cache_short_circuits_second_call(monkeypatch):
    counter = {"n": 0}
    _patch_fake_generator(monkeypatch, counter)

    entries = _entries("こんにちは", "さようなら", "ありがとう")
    h1 = pv._collect_grammar_hints(entries, entries, verbose=False,
                                   tag="A", profile="cloud")
    h2 = pv._collect_grammar_hints(entries, entries, verbose=False,
                                   tag="A", profile="cloud")
    assert counter["n"] == 3                   # 冷：仅每条一次
    assert h1 == h2 and len(h2) == 3           # 热：全部命中缓存


def test_grammar_cache_key_isolated_by_tag_and_profile(monkeypatch):
    counter = {"n": 0}
    _patch_fake_generator(monkeypatch, counter)

    entries = _entries("こんにちは")
    pv._collect_grammar_hints(entries, entries, verbose=False,
                              tag="A", profile="cloud")
    pv._collect_grammar_hints(entries, entries, verbose=False,
                              tag="B", profile="cloud")     # tag 变 → 重算
    pv._collect_grammar_hints(entries, entries, verbose=False,
                              tag="A", profile="local")     # profile 变 → 重算
    assert counter["n"] == 3


def test_grammar_cache_evicts_oldest_when_full(monkeypatch):
    """v1.3.0 D3 终选：容量触顶按 LRU 逐条淘汰最旧键，不整表清空。"""
    counter = {"n": 0}
    _patch_fake_generator(monkeypatch, counter)
    monkeypatch.setattr(pv, "_GRAMMAR_CACHE_MAX", 4)
    for i in range(5):                         # 灌入 5 键触发一次淘汰
        pv._collect_grammar_hints(_entries(f"文本{i}"), _entries(f"文本{i}"),
                                  verbose=False, tag="A", profile="p")
    assert len(pv._GRAMMAR_CACHE) <= 4         # 淘汰后不超上限
    # 最旧的 文本0 已被淘汰：再次访问须重新生成
    before = counter["n"]
    pv._collect_grammar_hints(_entries("文本0"), _entries("文本0"),
                              verbose=False, tag="A", profile="p")
    assert counter["n"] == before + 1          # 最旧键被淘汰，须重新生成
    assert len(pv._GRAMMAR_CACHE) <= 4         # 新键可入，仍不超上限


def test_grammar_cache_hot_keys_survive_eviction(monkeypatch):
    """满时淘汰最旧、热键保留：频繁命中的键不因触顶被逐出。"""
    counter = {"n": 0}
    _patch_fake_generator(monkeypatch, counter)
    monkeypatch.setattr(pv, "_GRAMMAR_CACHE_MAX", 3)
    pv._collect_grammar_hints(_entries("热键"), _entries("热键"),
                              verbose=False, tag="A", profile="p")
    # 每轮都重新访问 热键，再灌入新键把容量顶满
    for i in range(6):
        pv._collect_grammar_hints(_entries("热键"), _entries("热键"),
                                  verbose=False, tag="A", profile="p")
        pv._collect_grammar_hints(_entries(f"填充{i}"), _entries(f"填充{i}"),
                                  verbose=False, tag="A", profile="p")
    assert len(pv._GRAMMAR_CACHE) <= 3
    # 热键仍在缓存：再次访问不再触发生成
    pv._collect_grammar_hints(_entries("热键"), _entries("热键"),
                              verbose=False, tag="A", profile="p")
    assert counter["n"] == 1 + 6               # 仅 热键 冷启动一次 + 6 个填充键


# ---------------------------------------------------------------------------
# 云端多文件并行（opt-in）
# ---------------------------------------------------------------------------

def _setup_files(tmp_path, monkeypatch, names):
    def _fake_tmp(p, s):
        d = tmp_path / "work" / s
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: FakeClient())
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    inputs = []
    for name in names:
        p = tmp_path / name
        p.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
            "2\n00:00:10,000 --> 00:00:11,000\nさようなら\n",
            encoding="utf-8")
        inputs.append(str(p))
    return inputs


def test_file_parallel_cloud_processes_all_files(tmp_path, monkeypatch):
    inputs = _setup_files(tmp_path, monkeypatch, ["a.japanese.srt",
                                                  "b.japanese.srt"])
    cfg = _v2_cfg(tmp_path, v2_file_parallel=True)
    cfg.inputs = inputs
    summary = {}
    pv.run_v2(cfg, summary_sink=summary)
    assert summary["files_ok"] == 2
    assert summary["files_failed"] == 0
    for name in ("a", "b"):
        assert (tmp_path / f"{name}_final_cn.srt").is_file()


def test_file_parallel_disabled_for_local_providers(tmp_path, monkeypatch):
    inputs = _setup_files(tmp_path, monkeypatch, ["a.japanese.srt",
                                                  "b.japanese.srt"])
    cfg = _v2_cfg(tmp_path, v2_file_parallel=True)
    cfg.stages[0].provider = "lmstudio"        # 任一阶段本地 → 串行
    cfg.inputs = inputs
    summary = {}
    assert pv._file_parallel_enabled(cfg) is False
    pv.run_v2(cfg, summary_sink=summary)
    assert summary["files_ok"] == 2


def test_file_parallel_isolated_failure(tmp_path, monkeypatch):
    _setup_files(tmp_path, monkeypatch, ["ok.japanese.srt",
                                         "bad.japanese.srt"])
    # bad 文件内容为空 → 阶段入口抛"输入 SRT 无有效条目" → 单文件隔离
    (tmp_path / "bad.japanese.srt").write_text("", encoding="utf-8")

    cfg = _v2_cfg(tmp_path, v2_file_parallel=True)
    cfg.inputs = [str(tmp_path / "ok.japanese.srt"),
                  str(tmp_path / "bad.japanese.srt")]
    summary = {}
    pv.run_v2(cfg, summary_sink=summary)
    assert summary["files_ok"] == 1
    assert summary["files_failed"] == 1
    assert (tmp_path / "ok_final_cn.srt").is_file()


# ---------------------------------------------------------------------------
# 词库学习异步化
# ---------------------------------------------------------------------------

def test_slow_glossary_learn_does_not_block_and_warns(tmp_path, monkeypatch):
    import subtransjav.refine.glossary_learn as gmod

    started = threading.Event()
    release = threading.Event()

    def slow_learn(*args, **kwargs):
        started.set()
        release.wait(timeout=10)               # 模拟慢学习
        return 0

    monkeypatch.setattr(gmod, "learn_from_s2_output", slow_learn)
    monkeypatch.setattr(pv, "_LEARN_JOIN_TIMEOUT", 0.3)

    inputs = _setup_files(tmp_path, monkeypatch, ["demo.japanese.srt"])
    # 学习路径默认关闭（D2026-0921-02），本测试专测异步学习须显式开启
    cfg = _v2_cfg(tmp_path, auto_glossary=True, glossary_learn_enabled=True)
    cfg.inputs = inputs
    summary = {}
    t0 = time.perf_counter()
    pv.run_v2(cfg, summary_sink=summary)
    elapsed = time.perf_counter() - t0
    release.set()                              # 放行后台线程
    assert started.is_set()                    # 学习已异步启动
    assert elapsed < 5.0                       # 主流程未被慢学习阻塞
    assert summary["files_ok"] == 1
    assert any("词库学习未完成" in line           # 超时风险已记录
               for line in summary["summary_lines"])


def test_fast_glossary_learn_no_timeout_warning(tmp_path, monkeypatch):
    import subtransjav.refine.glossary_learn as gmod

    monkeypatch.setattr(gmod, "learn_from_s2_output", lambda *a, **k: 0)
    inputs = _setup_files(tmp_path, monkeypatch, ["demo.japanese.srt"])
    # 学习路径默认关闭（D2026-0921-02），本测试专测学习完成路径须显式开启
    cfg = _v2_cfg(tmp_path, auto_glossary=True, glossary_learn_enabled=True)
    cfg.inputs = inputs
    summary = {}
    pv.run_v2(cfg, summary_sink=summary)
    assert summary["files_ok"] == 1
    assert not any("词库学习未完成" in line
                   for line in summary["summary_lines"])
