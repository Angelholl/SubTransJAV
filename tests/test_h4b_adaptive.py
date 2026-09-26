"""H4b 条目级阈值自适应测试（全部合成 fixture，不依赖 .abtest 真实语料）。

覆盖：
1. telemetry 解析（正常/null 字段/坏行/缺文件/超龄/容错）；
2. 发现（前缀匹配命中/零命中/多命中/显式路径）；
3. 场景→条目映射（累计边界/中点落格/超界不映射/不可解析跳过）；
4. 信任派生（硬信号/软信号合取/null 不参与）；
5. 端到端谓词链（合成 6 场景 telemetry + 对应 timing 条目 →
   低信任场景条目走 tighten）；
6. 三可数指标与红标事件（gate0_summary upstream.telemetry 契约）；
7. 指纹/CLI/GUI 接线契约（adaptive_thresholds 在 _CONFIG_FIELDS、
   asr_telemetry 不在）。
"""

import io
import json
import os
import time
import types

import pytest

from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine.asr_meta import (
    ASR_TELEMETRY_SUFFIX,
    load_asr_telemetry,
    scene_low_trust,
)
from subtransjav.refine.config import RefineConfig, StageConfig
from subtransjav.refine.events import parse_event_line
from subtransjav.refine.manifest import _CONFIG_FIELDS, compute_config_hash
from subtransjav.refine.pipeline_v2 import map_entries_to_scenes
from subtransjav.refine.source_hallucination import apply_source_filter
from subtransjav.translate.llm_client import BatchResult

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _scene(n, dur=10.0, **kw) -> dict:
    """实测 1.9.3 schema 的合成场景行。"""
    base = {"media": "demo", "scene": n, "elapsed_s": 1.0,
            "audio_duration_s": dur, "wall_s": 1.0, "produced_output": True,
            "model_epoch": 1, "rtf": 0.1, "n_segments": 5,
            "max_temperature": 0.0, "fallback_segments": 0,
            "min_avg_logprob": -0.5, "mean_avg_logprob": -0.5,
            "max_compression_ratio": 1.2, "max_no_speech_prob": 0.1}
    base.update(kw)
    return base


def _write_raw_srt(directory, stem="demo"):
    p = directory / f"{stem}.ja.whisperjav.srt"
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n",
                 encoding="utf-8")
    return p


def _write_telemetry(directory, scenes, stem="demo"):
    raw = directory / "raw_subs"
    raw.mkdir(exist_ok=True)
    p = raw / f"{stem}{ASR_TELEMETRY_SUFFIX}"
    p.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in scenes) + "\n",
        encoding="utf-8")
    return p


def _cfg(**kw) -> types.SimpleNamespace:
    base = {"asr_meta": "", "asr_telemetry": "", "adaptive_thresholds": False,
            "v2_asr_meta_stale_max_hours": 24,
            "v2_source_filter": "default", "v2_source_filter_valve_pct": 50}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _t(sec, dur=1):
    """可控时间轴：起始 sec 秒、时长 dur 秒（无重叠、递增；全整秒防格式漂移）。"""
    m1, s1 = divmod(int(sec), 60)
    m2, s2 = divmod(int(sec) + int(dur), 60)
    return f"00:{m1:02d}:{s1:02d},000 --> 00:{m2:02d}:{s2:02d},000"


# ---------------------------------------------------------------------------
# 1. telemetry 解析
# ---------------------------------------------------------------------------

def test_telemetry_parse_normal_null_and_bad_lines(tmp_path):
    """null 字段缺省（不当 0）；坏行跳行计数；正常场景入 scenes。"""
    raw = tmp_path / "raw_subs"
    raw.mkdir()
    (raw / "demo.asr_telemetry.jsonl").write_text("\n".join([
        json.dumps(_scene(1)),
        json.dumps(_scene(2, max_no_speech_prob=None, mean_avg_logprob=None)),
        "{not json",
        json.dumps({"scene": "3", "audio_duration_s": 5.0}),
        json.dumps(_scene(4, produced_output=False, max_temperature=None)),
    ]), encoding="utf-8")
    r = load_asr_telemetry(_cfg(), str(_write_raw_srt(tmp_path)))
    assert r["present"] is True and r["stale"] is False
    assert set(r["scenes"]) == {1, 2, 4}
    assert "max_no_speech_prob" not in r["scenes"][2]
    assert r["scenes"][4]["produced_output"] is False
    assert r["skipped_lines"] == 2
    assert r["warnings"] == []


def test_telemetry_missing_file_zero_hit_warns(tmp_path):
    r = load_asr_telemetry(_cfg(), str(_write_raw_srt(tmp_path)))
    assert r["present"] is False and r["stale"] is False
    assert r["file"] is None and r["scenes"] == {}
    assert r["skipped_lines"] == 0
    assert len(r["warnings"]) == 1 and "未发现" in r["warnings"][0]


def test_telemetry_stale_auto_discovered_rejected(tmp_path):
    """超龄（比 SRT 旧超过 24h）→ stale=True 弃用信号（R6 沿 _is_fresh 手法）。"""
    srt = _write_raw_srt(tmp_path)
    p = _write_telemetry(tmp_path, [_scene(1)])
    now = time.time()
    os.utime(str(srt), (now, now))
    stamp = now - 25 * 3600
    os.utime(str(p), (stamp, stamp))
    r = load_asr_telemetry(_cfg(), str(srt))
    assert r["present"] is False and r["stale"] is True
    assert r["scenes"] == {} and r["file"] == "demo.asr_telemetry.jsonl"
    assert r["warnings"] and "超龄" in r["warnings"][0]


def test_telemetry_never_raises_on_corrupt_input(tmp_path):
    raw = tmp_path / "raw_subs"
    raw.mkdir()
    (raw / "demo.asr_telemetry.jsonl").write_bytes(b"\xff\xfe\x00bad")
    r = load_asr_telemetry(_cfg(), str(_write_raw_srt(tmp_path)))
    assert r["present"] is False
    assert r["warnings"] and "读取失败" in r["warnings"][0]


# ---------------------------------------------------------------------------
# 2. 发现（前缀匹配）
# ---------------------------------------------------------------------------

def test_discovery_strips_language_and_pipeline_suffixes(tmp_path):
    """SRT 剥 .ja.whisperjav 等后缀后前缀匹配（禁止 naive stem）。"""
    srt = _write_raw_srt(tmp_path)                 # demo.ja.whisperjav.srt
    _write_telemetry(tmp_path, [_scene(1)], stem="demo")
    r = load_asr_telemetry(_cfg(), str(srt))
    assert r["present"] is True and r["file"] == "demo.asr_telemetry.jsonl"


def test_discovery_real_world_naming(tmp_path):
    """真实命名：SRT=4k2.me@mihd-002.ja.whisperjav.srt 而
    telemetry=4k2.me@mihd-002.asr_telemetry.jsonl。"""
    stem = "4k2.me@mihd-002"
    srt = tmp_path / f"{stem}.ja.whisperjav.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nあ\n", encoding="utf-8")
    _write_telemetry(tmp_path, [_scene(1)], stem=stem)
    r = load_asr_telemetry(_cfg(), str(srt))
    assert r["present"] is True


def test_discovery_zero_hit_warns(tmp_path):
    srt = _write_raw_srt(tmp_path)
    (tmp_path / "raw_subs").mkdir()
    r = load_asr_telemetry(_cfg(), str(srt))
    assert r["present"] is False and r["warnings"]
    assert "未发现" in r["warnings"][0]


def test_discovery_multi_hit_abandons_with_warning(tmp_path):
    srt = _write_raw_srt(tmp_path)
    _write_telemetry(tmp_path, [_scene(1)], stem="demo")
    _write_telemetry(tmp_path, [_scene(1)], stem="demo.alt")
    r = load_asr_telemetry(_cfg(), str(srt))
    assert r["present"] is False and r["file"] is None
    assert any("多个" in w for w in r["warnings"])


def test_discovery_explicit_path_used_directly_without_freshness(tmp_path):
    """显式 cfg.asr_telemetry 直接用、不做新鲜度（沿 asr_meta 显式路径先例）。"""
    p = tmp_path / "explicit.jsonl"
    p.write_text(json.dumps(_scene(1)), encoding="utf-8")
    now = time.time()
    os.utime(str(p), (now - 100 * 3600, now - 100 * 3600))   # 超龄也不弃用
    r = load_asr_telemetry(_cfg(asr_telemetry=str(p)), "")
    assert r["present"] is True and r["stale"] is False


# ---------------------------------------------------------------------------
# 3. 场景→条目映射
# ---------------------------------------------------------------------------

def test_map_entries_cumulative_boundaries_and_midpoints():
    """场景按时长累计得边界（1=0-10s，2=10-30s，3=30-35s），条目按
    timing 中点落格（边界点归先到场景）。"""
    scenes = {1: {"audio_duration_s": 10.0},
              2: {"audio_duration_s": 20.0},
              3: {"audio_duration_s": 5.0}}
    entries = [
        {"index": 1, "timing": _t(0, 4)},      # 中点 2 → 场景1
        {"index": 2, "timing": _t(8, 4)},      # 中点 10 → 场景1（边界点）
        {"index": 3, "timing": _t(13, 4)},     # 中点 15 → 场景2
        {"index": 4, "timing": _t(31, 4)},     # 中点 33 → 场景3
    ]
    assert map_entries_to_scenes(entries, scenes) == {1: 1, 2: 1, 3: 2, 4: 3}


def test_map_entries_beyond_last_boundary_not_mapped():
    """超出末场景累计边界的条目不映射（禁外推）。"""
    scenes = {1: {"audio_duration_s": 10.0}}
    entries = [
        {"index": 1, "timing": _t(0, 4)},
        {"index": 2, "timing": _t(60, 4)},     # 中点 62 > 10
    ]
    assert map_entries_to_scenes(entries, scenes) == {1: 1}


def test_map_entries_unparseable_timing_or_missing_index_skipped():
    scenes = {1: {"audio_duration_s": 10.0}}
    entries = [{"index": 1, "timing": "garbage"},
               {"timing": _t(0, 2)},                     # 缺 index
               {"index": None, "timing": _t(1, 2)}]
    assert map_entries_to_scenes(entries, scenes) == {}
    assert map_entries_to_scenes(entries, {}) == {}      # 无场景 → 空映射


# ---------------------------------------------------------------------------
# 4. 信任派生（scene_low_trust）
# ---------------------------------------------------------------------------

def test_scene_low_trust_hard_signals():
    assert scene_low_trust({"produced_output": False}) is True
    assert scene_low_trust({"fallback_segments": 1}) is True
    assert scene_low_trust({"fallback_segments": 0}) is False
    assert scene_low_trust({"max_temperature": 0.5}) is True
    assert scene_low_trust({"max_temperature": 0.0}) is False
    assert scene_low_trust({"max_compression_ratio": 2.5}) is True
    assert scene_low_trust({"max_compression_ratio": 2.4}) is False   # 严格大于


def test_scene_low_trust_soft_signals_conjunction():
    """软信号合取：nsp>0.6 且 mean_avg_logprob<-1.0，缺一不判。"""
    assert scene_low_trust({"max_no_speech_prob": 0.7,
                            "mean_avg_logprob": -1.5}) is True
    assert scene_low_trust({"max_no_speech_prob": 0.7,
                            "mean_avg_logprob": -0.9}) is False
    assert scene_low_trust({"max_no_speech_prob": 0.5,
                            "mean_avg_logprob": -1.5}) is False
    assert scene_low_trust({"max_no_speech_prob": 0.7}) is False
    assert scene_low_trust({"mean_avg_logprob": -1.5}) is False


def test_scene_low_trust_null_or_missing_signals_never_low_trust():
    """None/缺失/非数值信号不参与（不当 0、不判低信任）；正常场景 False。"""
    assert scene_low_trust({"max_no_speech_prob": None,
                            "mean_avg_logprob": None}) is False
    assert scene_low_trust({}) is False
    assert scene_low_trust(None) is False
    assert scene_low_trust({"max_compression_ratio": "abc"}) is False
    assert scene_low_trust(_scene(1)) is False


# ---------------------------------------------------------------------------
# 5. 端到端谓词链（telemetry → 低信任场景 → 条目级 tighten）
# ---------------------------------------------------------------------------

def _six_scene_telemetry():
    """6 场景 × 10s：场景3（produced_output=False）与场景5（fallback>0）
    低信任，其余默认。"""
    return [_scene(1), _scene(2),
            _scene(3, produced_output=False),
            _scene(4),
            _scene(5, fallback_segments=2),
            _scene(6)]


def _build_predicate(entries, scenes):
    low = {no for no, sc in scenes.items() if scene_low_trust(sc)}
    scene_of = map_entries_to_scenes(entries, scenes)
    return (lambda e: scene_of.get(e.get("index")) in low), low, scene_of


def test_end_to_end_predicate_chain_routes_low_trust_entries_to_tighten(
        tmp_path):
    """合成 6 场景 telemetry + 对应 timing 条目：低信任场景（20-30s）内
    3 连同文按收紧参数删除，默认场景（30-40s）内 3 连保留。"""
    srt = _write_raw_srt(tmp_path)
    _write_telemetry(tmp_path, _six_scene_telemetry())
    telemetry = load_asr_telemetry(_cfg(), str(srt))
    assert telemetry["present"] is True
    entries = [
        {"index": 1, "timing": _t(1), "text": "こんにちは"},    # 场景1
        {"index": 2, "timing": _t(20), "text": "みんな"},       # 场景3 低信任
        {"index": 3, "timing": _t(21), "text": "みんな"},
        {"index": 4, "timing": _t(22), "text": "みんな"},
        {"index": 5, "timing": _t(25), "text": "また明日"},     # 场景3
        {"index": 6, "timing": _t(30), "text": "さようなら"},   # 场景4 默认
        {"index": 7, "timing": _t(31), "text": "さようなら"},
        {"index": 8, "timing": _t(32), "text": "さようなら"},
        {"index": 9, "timing": _t(55), "text": "ありがとう"},   # 场景6
    ]
    pred, low, scene_of = _build_predicate(entries, telemetry["scenes"])
    assert low == {3, 5}
    assert [scene_of[i] for i in (1, 2, 5, 6, 9)] == [1, 3, 3, 4, 6]
    kept, stats = apply_source_filter(entries, _cfg(),
                                      tighten_entry_predicate=pred)
    texts = [e["text"] for e in kept]
    assert texts.count("みんな") == 0           # 低信任场景 3 连被收紧删除
    assert texts.count("さようなら") == 3       # 默认场景 3 连保留
    assert stats["categories"]["重复循环"] == {"detected": 3, "deleted": 3}


def test_end_to_end_same_entries_without_predicate_keep_three_run():
    """对照：同一批条目无谓词（H4a 路径）时 3 连保留（min_run=4）。"""
    entries = [
        {"index": 1, "timing": _t(20), "text": "みんな"},
        {"index": 2, "timing": _t(21), "text": "みんな"},
        {"index": 3, "timing": _t(22), "text": "みんな"},
        {"index": 4, "timing": _t(25), "text": "また明日"},
        {"index": 5, "timing": _t(30), "text": "こんにちは"},
        {"index": 6, "timing": _t(55), "text": "ありがとう"},
    ]
    kept, stats = apply_source_filter(entries, _cfg())
    assert len(kept) == 6
    assert stats["categories"]["重复循环"] == {"detected": 0, "deleted": 0}


# ---------------------------------------------------------------------------
# 6. 三可数指标与红标事件（管线级，合成 fixture）
# ---------------------------------------------------------------------------

class _FakeClient:
    """按 index 脚本化返回译文的假客户端（同 test_pipeline_v2 口径）。"""

    def __init__(self):
        self.entry_log = []

    def translate_entries(self, entries, *, system_text, user_prompt,
                          max_batch_size=30, allow_empty_deletions=False,
                          scene_threshold=60.0, progress=None):
        self.entry_log.append([dict(e) for e in entries])
        translations, deleted, failed = {}, set(), []
        for e in entries:
            if "|||" in e["text"]:
                translations[e["index"]] = f"审{e['index']}"
            else:
                translations[e["index"]] = f"译{e['index']}"
        return BatchResult(translations=translations, deleted=deleted,
                           failed=failed)


def _make_cfg(tmp_path, **kw) -> RefineConfig:
    cfg = RefineConfig(inputs=[], templates_dir="", tm_enabled=False,
                       v2_profile="cloud", **kw)
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "fake-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "lmstudio", "fake-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    return cfg


def _wire_e2e(tmp_path, monkeypatch, fake):
    def _fake_tmp(p, s):
        d = tmp_path / "work"
        d.mkdir(exist_ok=True)
        return str(d)
    monkeypatch.setattr(pv, "refine_tmp_dir", _fake_tmp)
    monkeypatch.setattr(pv, "_make_client", lambda cfg, tag: fake)
    monkeypatch.setattr(pv, "_init_tm", lambda c: None)
    import subtransjav.refine.source_hallucination as gate0
    monkeypatch.setattr(gate0, "_default_errors_dir",
                        lambda: str(tmp_path / "Errors"))


def _gate0_srt(path):
    """9 条输入：场景3 内 3 连（低信任→收紧删除）+ 场景4 内 3 连（默认→保留）。"""
    lines = [
        (1, 1, "こんにちは"), (2, 20, "みんな"), (3, 21, "みんな"),
        (4, 22, "みんな"), (5, 25, "また明日"), (6, 30, "さようなら"),
        (7, 31, "さようなら"), (8, 32, "さようなら"), (9, 55, "ありがとう"),
    ]
    blocks = [f"{i}\n{_t(sec)}\n{text}\n" for i, sec, text in lines]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _run_capture_gate0(tmp_path, monkeypatch, cfg, in_srt):
    fake = _FakeClient()
    _wire_e2e(tmp_path, monkeypatch, fake)
    buf = io.StringIO()
    emitter = pv.EventEmitter(stream=buf, enabled=True)
    out = pv._run_single_v2(cfg, str(in_srt), emitter=emitter)
    events = [parse_event_line(ln) for ln in buf.getvalue().splitlines()
              if ln.strip()]
    summaries = [e for e in events if e["type"] == "gate0_summary"]
    assert len(summaries) == 1
    return out, summaries[0]["payload"], fake


def test_pipeline_metrics_with_telemetry_optin(tmp_path, monkeypatch, capsys):
    """opt-in + 遥测可用：三可数指标入 upstream.telemetry，print 一行，
    低信任场景条目实际走收紧删除。"""
    in_srt = _gate0_srt(tmp_path / "demo.ja.whisperjav.srt")
    _write_telemetry(tmp_path, _six_scene_telemetry(), stem="demo")
    cfg = _make_cfg(tmp_path, adaptive_thresholds=True)
    out, payload, _fake = _run_capture_gate0(tmp_path, monkeypatch, cfg,
                                             in_srt)
    assert out.endswith("demo.ja.whisperjav_final_cn.srt")
    tel = payload["upstream"]["telemetry"]
    assert tel == {"present": True, "stale": False,
                   "file": "demo.asr_telemetry.jsonl", "scenes": 6,
                   "low_trust": 2, "adaptive_entries": 4, "skipped_lines": 0}
    # 三可数指标 print 一行
    out_text = capsys.readouterr().out
    assert "📡 上游场景遥测：6 场景 / 低信任 2 场景 / 自适应收紧条目 4 条" \
        in out_text
    # 场景3（20-30s，低信任）内 3 连被收紧删除；场景4 内 3 连保留
    assert payload["categories"]["重复循环"] == {"detected": 3, "deleted": 3}


def test_pipeline_red_mark_on_optin_without_telemetry(tmp_path, monkeypatch,
                                                      capsys):
    """红标（要素 i）：opt-in 但遥测缺失 → WARNING 风险事件 + 默认阈值执行 +
    telemetry 子块如实记无信号。"""
    in_srt = _gate0_srt(tmp_path / "demo.ja.whisperjav.srt")
    cfg = _make_cfg(tmp_path, adaptive_thresholds=True)
    collector = pv.RiskCollector()
    fake = _FakeClient()
    _wire_e2e(tmp_path, monkeypatch, fake)
    buf = io.StringIO()
    emitter = pv.EventEmitter(stream=buf, enabled=True)
    pv._run_single_v2(cfg, str(in_srt), collector=collector, emitter=emitter)
    events = [parse_event_line(ln) for ln in buf.getvalue().splitlines()
              if ln.strip()]
    payload = [e for e in events if e["type"] == "gate0_summary"][0]["payload"]
    tel = payload["upstream"]["telemetry"]
    assert tel == {"present": False, "stale": False, "file": None,
                   "scenes": 0, "low_trust": 0, "adaptive_entries": 0,
                   "skipped_lines": 0}
    red = [e for e in collector.events
           if "自适应阈值已启用但未发现可用 asr_telemetry" in (e.reason or "")]
    assert len(red) == 1
    assert red[0].severity == "warning"
    assert red[0].action == "按默认档位执行"
    assert red[0].stage == "gate0"
    # 无遥测 → 无自适应 print
    assert "📡 上游场景遥测" not in capsys.readouterr().out


def test_pipeline_no_telemetry_block_without_optin(tmp_path, monkeypatch):
    """缺省路径零变化：未 opt-in 且无遥测 → upstream 不挂 telemetry 子块
    （既有 gate0_summary 契约测试钉死无信号 upstream 键集）。"""
    in_srt = _gate0_srt(tmp_path / "demo.ja.whisperjav.srt")
    cfg = _make_cfg(tmp_path)
    out, payload, _fake = _run_capture_gate0(tmp_path, monkeypatch, cfg,
                                             in_srt)
    assert "telemetry" not in payload["upstream"]
    assert payload["categories"]["重复循环"] == {"detected": 0, "deleted": 0}


# ---------------------------------------------------------------------------
# 7. 指纹 / CLI / GUI 接线契约
# ---------------------------------------------------------------------------

def test_adaptive_thresholds_in_config_fields_and_telemetry_not():
    """开关参与指纹（影响送翻/删除产物内容）；路径沿 asr_meta 先例不进指纹。"""
    assert "adaptive_thresholds" in _CONFIG_FIELDS
    assert "asr_telemetry" not in _CONFIG_FIELDS
    assert "asr_meta" not in _CONFIG_FIELDS


def test_config_defaults_for_h4b_fields():
    cfg = RefineConfig()
    assert cfg.adaptive_thresholds is False
    assert cfg.asr_telemetry == ""


def test_config_hash_changes_with_adaptive_thresholds():
    c1 = compute_config_hash(types.SimpleNamespace(
        stages=[], adaptive_thresholds=False))
    c2 = compute_config_hash(types.SimpleNamespace(
        stages=[], adaptive_thresholds=True))
    assert c1 != c2


def test_cli_flags_wired_to_config():
    from subtransjav.refine.cli import build_parser, config_from_args
    args = build_parser().parse_args(
        ["-i", "x.srt", "--adaptive-thresholds",
         "--asr-telemetry", "t.jsonl"])
    cfg = config_from_args(args)
    assert cfg.adaptive_thresholds is True
    assert cfg.asr_telemetry == "t.jsonl"
    args2 = build_parser().parse_args(["-i", "x.srt"])
    cfg2 = config_from_args(args2)
    assert cfg2.adaptive_thresholds is False
    assert cfg2.asr_telemetry == ""


def test_gui_args_adaptive_thresholds_flag():
    _webview = pytest.importorskip("webview", exc_type=ImportError)
    assert _webview is not None
    from subtransjav.webview_gui.api import _build_refine_args
    args = _build_refine_args({"inputs": ["a.srt"],
                               "adaptive_thresholds": True})
    assert "--adaptive-thresholds" in args
    args2 = _build_refine_args({"inputs": ["a.srt"]})
    assert "--adaptive-thresholds" not in args2
