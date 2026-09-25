"""P1-5 配置单一来源与分层测试
================================

覆盖：
- 散点收口字段默认值 = 历史硬编码现值（行为不变）；
- 分层优先级：默认 < 用户配置文件 < 环境变量 < CLI/GUI 显式赋值；
- 非法用户配置文件 / 非法类型值 → 警告并忽略（不 crash）；
- effective_summary 摘要内容；
- manifest 指纹字段联动（影响产物的收口字段必须使指纹变化；
  超时/并发上限不参与指纹）。
"""

import json
import types

import pytest

from subtransjav.refine import config as rc
from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine.config import (
    DEEPSEEK_BASE_DEFAULT,
    DEFAULT_PREMERGE_MAX_CHARS,
    DEFAULT_PREMERGE_MAX_GAP_S,
    DEFAULT_PREMERGE_MAX_ITEMS,
    DEFAULT_PREMERGE_MAX_SPAN_MS,
    DEFAULT_PREMERGE_MIN_FRAGMENT_CHARS,
    DEFAULT_TEMPERATURE_CLOUD,
    DEFAULT_TEMPERATURE_LOCAL,
    DEFAULT_TIMEOUT_HTTP,
    DEFAULT_TIMEOUT_LLM,
    DEFAULT_TIMEOUT_PROBE,
    DEFAULT_V2_CONCURRENCY_MAX,
    RefineConfig,
    StageConfig,
    load_user_settings,
    resolve_tunable,
    user_settings_path,
)
from subtransjav.refine.manifest import compute_config_hash

# ---------------------------------------------------------------------------
# 工具：把用户配置文件重定向到 tmp_path，隔离真实 repo 配置
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_user_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CONFIG_DIR", str(tmp_path))
    # 清理可能影响结果的同名环境变量
    for name in rc.TUNABLE_FIELD_TYPES:
        monkeypatch.delenv(f"SUBTRANSJAV_{name.upper()}", raising=False)
    yield


def _write_user_settings(tmp_path, data, raw=None):
    p = tmp_path / "user_settings.json"
    p.write_text(raw if raw is not None else json.dumps(data),
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 默认值 = 历史现值（行为不变）
# ---------------------------------------------------------------------------

def test_defaults_equal_legacy_hardcoded_values():
    cfg = RefineConfig()
    assert cfg.temperature_cloud == 0.5 == DEFAULT_TEMPERATURE_CLOUD
    assert cfg.temperature_local == 0.1 == DEFAULT_TEMPERATURE_LOCAL
    assert cfg.premerge_max_gap_s == 8.0 == DEFAULT_PREMERGE_MAX_GAP_S
    assert cfg.premerge_max_items == 3 == DEFAULT_PREMERGE_MAX_ITEMS
    assert cfg.premerge_max_span_ms == 5000 == DEFAULT_PREMERGE_MAX_SPAN_MS
    assert cfg.premerge_max_chars == 80 == DEFAULT_PREMERGE_MAX_CHARS
    assert cfg.premerge_min_fragment_chars == 6 \
        == DEFAULT_PREMERGE_MIN_FRAGMENT_CHARS
    assert cfg.v2_concurrency_max == 5 == DEFAULT_V2_CONCURRENCY_MAX
    assert cfg.timeout_llm == 900.0 == DEFAULT_TIMEOUT_LLM
    assert cfg.timeout_http == 60.0 == DEFAULT_TIMEOUT_HTTP
    assert cfg.timeout_probe == 5.0 == DEFAULT_TIMEOUT_PROBE
    assert cfg.v2_file_parallel is False      # opt-in 默认关
    assert DEEPSEEK_BASE_DEFAULT == "https://api.deepseek.com/v1"
    # pipeline_v2 的历史别名与 config 单源一致
    assert pv.DEEPSEEK_BASE_URL == DEEPSEEK_BASE_DEFAULT


def test_concurrency_clamp_uses_configured_max():
    assert RefineConfig(v2_concurrency=99).v2_concurrency == 5   # 默认上限
    assert RefineConfig(v2_concurrency=99,
                        v2_concurrency_max=2).v2_concurrency == 2
    assert RefineConfig(v2_concurrency=0).v2_concurrency == 1


# ---------------------------------------------------------------------------
# load_user_settings：文件读取与容错
# ---------------------------------------------------------------------------

def test_load_user_settings_missing_file_returns_empty(tmp_path):
    assert load_user_settings() == {}


def test_load_user_settings_valid_file(tmp_path):
    _write_user_settings(tmp_path, {"temperature_cloud": 0.3,
                                    "timeout_llm": 600,
                                    "unknown_field": "x"})
    assert load_user_settings() == {"temperature_cloud": 0.3,
                                    "timeout_llm": 600}


def test_load_user_settings_invalid_json_warn_and_ignore(tmp_path, capsys):
    _write_user_settings(tmp_path, None, raw="{not valid json")
    assert load_user_settings() == {}
    assert "已忽略" in capsys.readouterr().out


def test_load_user_settings_non_object_warn_and_ignore(tmp_path, capsys):
    _write_user_settings(tmp_path, None, raw="[1, 2, 3]")
    assert load_user_settings() == {}
    assert "已忽略" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 分层优先级：默认 < 用户文件 < 环境变量 < CLI/GUI 显式赋值
# ---------------------------------------------------------------------------

def test_layering_user_file_overrides_default(tmp_path):
    _write_user_settings(tmp_path, {"temperature_cloud": 0.3})
    cfg = RefineConfig()
    assert cfg.temperature_cloud == 0.3
    assert cfg.temperature_local == 0.1        # 未配置字段保持默认


def test_layering_env_overrides_user_file(tmp_path, monkeypatch):
    _write_user_settings(tmp_path, {"temperature_cloud": 0.3})
    monkeypatch.setenv("SUBTRANSJAV_TEMPERATURE_CLOUD", "0.7")
    assert RefineConfig().temperature_cloud == 0.7


def test_layering_explicit_assignment_beats_user_file_and_env(
        tmp_path, monkeypatch):
    _write_user_settings(tmp_path, {"temperature_cloud": 0.3})
    monkeypatch.setenv("SUBTRANSJAV_TEMPERATURE_CLOUD", "0.7")
    cfg = RefineConfig(temperature_cloud=0.9)   # 模拟 CLI/GUI 显式赋值
    assert cfg.temperature_cloud == 0.9


def test_layering_invalid_typed_values_ignored(tmp_path, monkeypatch,
                                               capsys):
    _write_user_settings(tmp_path, {"premerge_max_items": "abc",
                                    "timeout_http": 30})
    monkeypatch.setenv("SUBTRANSJAV_V2_CONCURRENCY_MAX", "not-an-int")
    cfg = RefineConfig()
    assert cfg.premerge_max_items == 3          # 非法 -> 默认
    assert cfg.timeout_http == 30.0             # 合法 -> 生效
    assert cfg.v2_concurrency_max == 5          # 非法 env -> 默认
    out = capsys.readouterr().out
    assert "已忽略" in out


def test_resolve_tunable_follows_layering(tmp_path, monkeypatch):
    assert resolve_tunable("timeout_probe") == 5.0
    _write_user_settings(tmp_path, {"timeout_probe": 8})
    assert resolve_tunable("timeout_probe") == 8.0
    monkeypatch.setenv("SUBTRANSJAV_TIMEOUT_PROBE", "2")
    assert resolve_tunable("timeout_probe") == 2.0
    with pytest.raises(KeyError):
        resolve_tunable("no_such_field")


# ---------------------------------------------------------------------------
# 生效配置摘要
# ---------------------------------------------------------------------------

def test_effective_summary_lists_tunable_fields_and_profile():
    cfg = RefineConfig(stages=[
        StageConfig(0, True, "lmstudio", "m1"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "deepseek", "m2"),
        StageConfig(3, False, "lmstudio", ""),
    ])
    text = cfg.effective_summary()
    assert "\n" in text
    for token in ("profile=local", "lmstudio", "m1", "deepseek", "m2",
                  "temperature_cloud=0.5", "temperature_local=0.1",
                  "max_gap_s=8.0", "max_items=3", "v2_concurrency_max=5",
                  "max_span_ms=5000", "max_chars=80",
                  "min_fragment_chars=6",
                  "llm=900.0s", "http=60.0s", "probe=5.0s"):
        assert token in text


# ---------------------------------------------------------------------------
# manifest 指纹联动：收口字段中影响产物者参与、超时/并发上限不参与
# ---------------------------------------------------------------------------

def _hash_cfg(**kw):
    base = dict(
        profile=None,
        stages=[StageConfig(0, True, "lmstudio", "m")],
        endpoints={},
        batch_local=30, batch_cloud=30, batch_size_stable=True,
        v2_profile="local", v2_concurrency=1, v2_ctx_local=32768,
        v2_keep_untranslated="original", premerge_enabled=True,
        premerge_max_span_ms=5000, premerge_max_chars=80,
        premerge_min_fragment_chars=6,
        tm_enabled=True, tm_threshold=0.85, tm_fuzzy_inject=True,
        tm_fuzzy_threshold=0.98, tm_learn_gate=True,
        apply_glossary_stage1=True, apply_glossary_stage2=True,
        fallback_local=False, fallback_model="",
        templates_dir="", cleaner_config_dir="",
    )
    base.update(kw)
    return compute_config_hash(types.SimpleNamespace(**base))


def test_config_hash_sensitive_to_new_product_affecting_fields():
    base = _hash_cfg()
    assert _hash_cfg(temperature_cloud=0.3) != base
    assert _hash_cfg(temperature_local=0.2) != base
    assert _hash_cfg(premerge_max_gap_s=6.0) != base
    assert _hash_cfg(premerge_max_items=5) != base
    # RC3 预合并硬上限：直接影响合并结果，必须参与 resume 指纹
    assert _hash_cfg(premerge_max_span_ms=6000) != base
    assert _hash_cfg(premerge_max_chars=100) != base
    assert _hash_cfg(premerge_min_fragment_chars=8) != base


def test_config_hash_insensitive_to_timeout_and_concurrency_max():
    base = _hash_cfg()
    assert _hash_cfg(timeout_llm=300.0) == base
    assert _hash_cfg(timeout_http=30.0) == base
    assert _hash_cfg(timeout_probe=1.0) == base
    assert _hash_cfg(v2_concurrency_max=2) == base


# ---------------------------------------------------------------------------
# 散点真正引用 config：客户端构造链穿入
# ---------------------------------------------------------------------------

def test_make_client_threads_temperature_and_timeout_from_config(monkeypatch):
    # lmstudio 建客户端前的引擎对齐需要本机 LM Studio，CI 无服务 → 打桩
    monkeypatch.setattr(pv, "_ensure_lmstudio_engine", lambda *a, **k: None)
    cfg = RefineConfig(temperature_local=0.2, timeout_llm=321.0)
    cfg.stages = [StageConfig(0, True, "lmstudio", "m1")]
    client = pv._make_client(cfg, "A")
    assert client.config.temperature == 0.2
    assert client.config.timeout == 321.0


def test_make_client_cloud_uses_cloud_temperature():
    cfg = RefineConfig(temperature_cloud=0.4, timeout_llm=123.0)
    cfg.stages = [StageConfig(0, True, "custom", "m1")]
    cfg.endpoints = {"custom": "http://localhost:9/v1"}
    cfg.api_key_custom = "k"           # 非 custom 必填校验仅 validate 用
    client = pv._make_client(cfg, "A")
    assert client.config.temperature == 0.4
    assert client.config.timeout == 123.0


def test_premerge_entries_uses_cfg_thresholds():
    entries = [
        {"index": 1, "timing": "00:00:00,000 --> 00:00:02,000", "text": "あ"},
        {"index": 2, "timing": "00:00:02,000 --> 00:00:04,000", "text": "い"},
        {"index": 3, "timing": "00:00:04,000 --> 00:00:06,000", "text": "う"},
    ]
    # 上限 1 条：永不合并
    cfg = RefineConfig(premerge_max_items=1)
    assert len(pv._premerge_entries([dict(e) for e in entries], cfg)) == 3
    # 默认（≤3 条且时间相邻）：碎片合并
    cfg2 = RefineConfig()
    merged = pv._premerge_entries([dict(e) for e in entries], cfg2)
    assert len(merged) < 3
    # 无 cfg 直调：回退历史默认值（兼容旧签名）
    assert len(pv._premerge_entries([dict(e) for e in entries])) == \
        len(merged)


def test_user_settings_path_under_config_dir(tmp_path):
    assert user_settings_path() == str(tmp_path / "user_settings.json")


def test_d20260921_defaults_glossary_learn_off_and_prod_pair():
    """D2026-0921-01/02 收尾落位：学习开关默认关闭（显式 True 仍可开）、
    生产默认 A>B = joyfox27b>heretic35b（本地 lmstudio）。"""
    cfg = RefineConfig()
    assert cfg.glossary_learn_enabled is False
    cfg.glossary_learn_enabled = True          # 显式开启路径保留
    assert cfg.glossary_learn_enabled is True
    a, b = cfg.stages[0], cfg.stages[2]
    assert (a.provider, a.model) == (
        "lmstudio", "qwen3.8-27b-uncensored-joyfox-aggressive")
    assert (b.provider, b.model) == (
        "lmstudio", "qwen3.6-35b-a3b-uncensored-heretic-apex")


def test_d20260922_02_default_factory_slot_enabled_alignment():
    """D2026-0922-02 裁决三：4 槽结构永久保留，默认工厂仅槽0/2（阶段A/B）
    启用，槽1/3 为 v2 未用槽默认停用（enabled 仅影响 manifest/GUI 展示）。"""
    cfg = RefineConfig()
    assert cfg.stages[0].enabled is True
    assert cfg.stages[1].enabled is False
    assert cfg.stages[2].enabled is True
    assert cfg.stages[3].enabled is False


def test_d20260922_02_legacy_providers_removed_pin():
    """D2026-0922-02 裁决二：PROVIDER_CONFIGS 六条孤立 legacy 条目
    （glm/groq/openrouter/gemini/claude/gpt）已删除，仅保留现役集合，
    正向钉防止回添。"""
    from subtransjav.translate.providers import PROVIDER_CONFIGS
    assert set(PROVIDER_CONFIGS) == {"deepseek", "ollama", "custom"}
