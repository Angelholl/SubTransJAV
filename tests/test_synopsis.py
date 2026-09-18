"""
剧情自摘要（v1.2.2 Beta）测试
====================
覆盖：采样（噪声过滤/分桶/限幅/单桶全量/时间排序）、提示词要素、缓存
（命中免 LLM/模型键隔离/原子写落盘）、失败降级、手写优先、A/B 注入与
关闭开关、指纹敏感度、终稿纯净。
LLM 以假实现注入（不联网），全部使用临时目录与中性合成文本。
"""

from pathlib import Path

import pytest

from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine import synopsis as syn
from subtransjav.refine.cli import build_parser
from subtransjav.refine.config import RefineConfig, StageConfig
from subtransjav.refine.manifest import compute_config_hash
from subtransjav.translate.llm_client import BatchResult

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path=None, **kw):
    cfg = RefineConfig(inputs=[], tm_enabled=False, v2_profile="cloud", **kw)
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "fake-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "lmstudio", "fake-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    return cfg


def _entries(*texts):
    return [{"index": i,
             "timing": f"00:00:{i:02d},000 --> 00:00:{i:02d},500",
             "text": t} for i, t in enumerate(texts, 1)]


class CountingClient:
    """假客户端：摘要调用走 _chat（可脚本化失败），翻译走 translate_entries。"""

    def __init__(self, synopsis_text="人物：A 与 B。剧情：C。场景：D。",
                 fail_synopsis=False):
        self.synopsis_text = synopsis_text
        self.fail_synopsis = fail_synopsis
        self.chat_calls = []
        self.calls = []
        self.prompts = []

    def _chat(self, system_text, user_text, max_tokens=None):
        self.chat_calls.append({"system": system_text, "user": user_text,
                                "max_tokens": max_tokens})
        if self.fail_synopsis:
            raise RuntimeError("模拟摘要调用失败")
        return self.synopsis_text

    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          progress=None):
        self.calls.append([e["index"] for e in entries])
        self.prompts.append({"system": system_text, "user": user_prompt})
        is_b = any("|||" in e["text"] for e in entries)
        translations = {e["index"]: (f"审{e['index']}" if is_b
                                     else f"译{e['index']}") for e in entries}
        return BatchResult(translations=translations, deleted=set(), failed=[])


@pytest.fixture(autouse=True)
def _isolated_synopsis_cache(tmp_path, monkeypatch):
    """缓存目录重定向到 tmp_path：跨用例隔离，不触碰真实 Temp/synopsis_cache。"""
    monkeypatch.setattr(syn, "SYNOPSIS_CACHE_DIR",
                        str(tmp_path / "synopsis_cache"))


def _pipeline_env(tmp_path, monkeypatch):
    """全管线测试公共脚手架：临时工作区 + 输入 srt（两条中性合成文本）。"""
    in_srt = tmp_path / "demo.srt"
    in_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは、今日は暑いですね\n\n"
        "2\n00:00:10,000 --> 00:00:11,000\nさようなら、また明日\n",
        encoding="utf-8")

    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)

    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    return in_srt


# ---------------------------------------------------------------------------
# build_synopsis_input：噪声过滤 / 分桶 / 限幅 / 单桶全量 / 排序
# ---------------------------------------------------------------------------

def test_build_input_filters_noise_lines():
    # "ああああああ"（同假名 6 连打）命中 is_source_counting_noise
    entries = _entries("こんにちは、今日は暑いですね", "ああああああ",
                       "さようなら、また明日")
    text, meta = syn.build_synopsis_input(entries, 6000)
    assert "ああああああ" not in text
    assert "こんにちは" in text and "さようなら" in text
    assert meta["total_chars"] == sum(
        len(e["text"]) for e in entries if e["text"] != "ああああああ")


def test_build_input_bucket_count_and_budget():
    # 24 行 × 50 字 = 1200 字；ceil(1200/750)=2 → 桶数 max(3, min(8,2))=3
    texts = [f"段落{i:02d}" + "あ" * 46 for i in range(1, 25)]
    entries = _entries(*texts)
    text, meta = syn.build_synopsis_input(entries, 600)
    assert meta["buckets"] == 3
    assert meta["chars"] == len(text) <= 600          # 限幅不超预算
    # 每桶预算 200，逐行成本 51 → 每桶取 3 行，共 9 行
    assert len(text.splitlines()) == 9
    # 桶区间：每桶 8 条，首末 index 如实记录
    assert [(r["first_index"], r["last_index"])
            for r in meta["bucket_ranges"]] == [(1, 8), (9, 16), (17, 24)]
    assert meta["bucket_ranges"][0]["first_start"] == 1.0
    assert meta["bucket_ranges"][-1]["last_start"] == 24.0


def test_build_input_short_text_single_bucket_full():
    texts = ["これはテストの行です", "さようなら、また明日"]
    text, meta = syn.build_synopsis_input(_entries(*texts), 6000)
    assert meta["buckets"] == 1
    assert text == texts[0] + "\n" + texts[1]          # 单桶全量，无删减
    assert meta["chars"] == len(text)
    assert meta["bucket_ranges"][0]["first_index"] == 1
    assert meta["bucket_ranges"][0]["last_index"] == 2


def test_build_input_sorts_by_start_time():
    late = {"index": 1, "timing": "00:00:10,000 --> 00:00:10,500",
            "text": "後から来る行"}
    early = {"index": 2, "timing": "00:00:01,000 --> 00:00:01,500",
             "text": "先に来る行"}
    text, _ = syn.build_synopsis_input([late, early], 6000)
    assert text.splitlines()[0] == "先に来る行"


def test_build_input_empty_entries():
    text, meta = syn.build_synopsis_input([], 6000)
    assert text == "" and meta["buckets"] == 0 and meta["chars"] == 0


# ---------------------------------------------------------------------------
# 提示词要素：抽样不连续声明 / 禁止补足 / 3-5 行 / 信息不足逃生口
# ---------------------------------------------------------------------------

def test_prompt_elements_complete(tmp_path):
    client = CountingClient()
    syn.request_synopsis(client, "サンプル", provider="p", model="m",
                         cache_dir=str(tmp_path / "c"))
    call = client.chat_calls[0]
    assert call["max_tokens"] == syn.SYNOPSIS_MAX_TOKENS == 300
    assert "剧情分析员" in call["system"]
    assert "时间上不连续" in call["system"]
    assert "禁止补足跳跃段内容" in call["system"]
    assert "忽略纯噪声行" in call["system"]
    assert "3-5 行" in call["user"]
    assert "主要人物及关系" in call["user"]
    assert "核心剧情线" in call["user"]
    assert "场景构成" in call["user"]
    assert "禁止编造源文没有的内容" in call["user"]
    assert "信息不足" in call["user"]


# ---------------------------------------------------------------------------
# 缓存：命中免 LLM / 模型键隔离 / 原子写落盘
# ---------------------------------------------------------------------------

def test_cache_hit_avoids_llm_and_atomic_write(tmp_path):
    cdir = str(tmp_path / "c")
    client = CountingClient("梗概甲")
    t1 = syn.request_synopsis(client, "同じ入力", provider="p", model="m1",
                              cache_dir=cdir)
    t2 = syn.request_synopsis(client, "同じ入力", provider="p", model="m1",
                              cache_dir=cdir)
    assert t1 == t2 == "梗概甲"
    assert len(client.chat_calls) == 1      # 第二次命中缓存，不再打 LLM
    key = syn.cache_key("p", "m1", "同じ入力")
    f = Path(cdir) / f"{key}.txt"
    assert f.is_file()                       # 原子写产物存在
    assert f.read_text(encoding="utf-8") == "梗概甲"   # 内容一致


def test_cache_key_differs_by_provider_model(tmp_path):
    cdir = str(tmp_path / "c")
    client = CountingClient("梗概乙")
    syn.request_synopsis(client, "同じ入力", provider="p", model="m1",
                         cache_dir=cdir)
    syn.request_synopsis(client, "同じ入力", provider="p", model="m2",
                         cache_dir=cdir)
    assert len(client.chat_calls) == 2       # 不同 model 键不同，各打一次
    assert syn.cache_key("p", "m1", "x") != syn.cache_key("p", "m2", "x")
    assert syn.cache_key("p1", "m", "x") != syn.cache_key("p2", "m", "x")


def test_request_synopsis_failure_returns_none(tmp_path):
    client = CountingClient(fail_synopsis=True)
    out = syn.request_synopsis(client, "入力", provider="p", model="m",
                               cache_dir=str(tmp_path / "c"))
    assert out is None
    out = syn.request_synopsis(CountingClient(synopsis_text="   "),
                               "入力", provider="p", model="m",
                               cache_dir=str(tmp_path / "c"))
    assert out is None                       # 空输出同样降级 None
    out = syn.request_synopsis(CountingClient(), "  ", provider="p",
                               model="m", cache_dir=str(tmp_path / "c"))
    assert out is None                       # 空采样文本直接 None


# ---------------------------------------------------------------------------
# 注入块：冻结措辞 + beta 标注
# ---------------------------------------------------------------------------

def test_synopsis_prompt_block_uses_frozen_notice():
    block = pv._synopsis_prompt_block("自动摘要梗概")
    assert pv._SYNOPSIS_BLOCK_TAG in block
    assert pv._SYNOPSIS_BLOCK_TAG == "【剧情背景（自动摘要·beta）】"
    assert pv.SIDECAR_SUMMARY_NOTICE in block
    assert "自动摘要梗概" in block
    assert pv._synopsis_prompt_block(None) == ""
    assert pv._synopsis_prompt_block("   ") == ""


# ---------------------------------------------------------------------------
# 管线接线：失败降级 / 手写优先 / 注入 / 关闭
# ---------------------------------------------------------------------------

def test_pipeline_synopsis_failure_degrades_gracefully(tmp_path, monkeypatch):
    """摘要调用抛错 → 返回 None → 管线继续，阶段A 正常翻译。"""
    cfg = _make_cfg()
    in_srt = _pipeline_env(tmp_path, monkeypatch)
    fake = CountingClient(fail_synopsis=True)
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    content = Path(out).read_text(encoding="utf-8")
    assert "审1" in content and "审2" in content
    assert len(fake.calls) == 2              # 阶段A + 阶段B 照常执行
    assert len(fake.chat_calls) == 1         # 摘要仅一次调用且已失败
    assert pv._SYNOPSIS_BLOCK_TAG not in content   # 失败无注入


def test_handwritten_summary_skips_auto_synopsis(tmp_path, monkeypatch):
    """手写 sidecar【剧情摘要】非空 → 不发生摘要调用（手写优先）。"""
    cfg = _make_cfg()
    in_srt = _pipeline_env(tmp_path, monkeypatch)
    (tmp_path / "demo.context.md").write_text(
        "# 注释行\n【剧情摘要】\n手写梗概内容，忽略自动摘要。\n",
        encoding="utf-8")
    fake = CountingClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    assert fake.chat_calls == []             # 零摘要调用
    assert len(fake.prompts) == 2
    for p in fake.prompts:
        assert "手写梗概内容" in p["system"]           # 手写摘要照常注入
        assert pv._SYNOPSIS_BLOCK_TAG not in p["system"]  # 无自动摘要块
    assert pv._SYNOPSIS_BLOCK_TAG not in Path(out).read_text(encoding="utf-8")


def test_synopsis_injected_into_a_and_b_prompts(tmp_path, monkeypatch,
                                                capsys):
    """A/B 两阶段 prompt 均含自动摘要块与冻结措辞；摘要不出现在任何产物。"""
    cfg = _make_cfg()
    in_srt = _pipeline_env(tmp_path, monkeypatch)
    marker = "自动摘要梗概标记甲乙丙"
    fake = CountingClient(synopsis_text=marker)
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    assert len(fake.chat_calls) == 1
    assert len(fake.prompts) == 2
    for p in fake.prompts:
        assert pv._SYNOPSIS_BLOCK_TAG in p["system"]
        assert pv.SIDECAR_SUMMARY_NOTICE in p["system"]
        assert marker in p["system"]
    # 日志一行：已生成 N 字 sha1=… 采样=K桶[…] 信息不足出现 k 次
    captured = capsys.readouterr()
    assert "剧情自摘要(beta): 已生成" in captured.out
    assert "sha1=" in captured.out
    assert "采样=" in captured.out
    assert "信息不足出现 0 次" in captured.out
    # 终稿纯净：输出目录所有落盘文件均不含摘要片段
    for f in Path(out).parent.iterdir():
        if f.is_file():
            assert marker not in f.read_text(encoding="utf-8")


def test_auto_synopsis_disabled_no_call_no_injection(tmp_path, monkeypatch):
    cfg = _make_cfg(auto_synopsis=False)
    in_srt = _pipeline_env(tmp_path, monkeypatch)
    fake = CountingClient()
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)

    out = pv._run_single_v2(cfg, str(in_srt))
    assert fake.chat_calls == []             # 无摘要调用
    for p in fake.prompts:
        assert pv._SYNOPSIS_BLOCK_TAG not in p["system"]   # 无注入
    assert pv._SYNOPSIS_BLOCK_TAG not in Path(out).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI 旗标与指纹敏感度
# ---------------------------------------------------------------------------

def test_cli_flag_no_auto_synopsis():
    args = build_parser().parse_args(["--no-auto-synopsis"])
    assert args.no_auto_synopsis is True
    args = build_parser().parse_args([])
    assert args.no_auto_synopsis is False


def test_config_hash_sensitive_to_synopsis_fields():
    base = compute_config_hash(_make_cfg())
    assert compute_config_hash(_make_cfg(auto_synopsis=False)) != base
    assert compute_config_hash(_make_cfg(synopsis_max_chars=5120)) != base
