"""上游 ASR 运行信号通道（asr_meta）测试。

覆盖：
1. 容错解析：缺键 / 未知 schema / 类型漂移 / 0-1 比例换算；
2. 显式路径：不存在 / 目录形式 / JSON 损坏；
3. 自动发现 + 新鲜度检查（os.utime 伪造超龄，R6）；
4. 指纹语义：同内容不同路径 → 同 sha1；内容变 → 变；无信号 → None（R1）。
"""

import hashlib
import json
import os
import time
import types

from subtransjav.refine.asr_meta import (
    RUN_META_NAME,
    SUSPECT_STATUSES,
    fingerprint,
    load_asr_meta,
    load_asr_telemetry,
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _cfg(**kw) -> types.SimpleNamespace:
    base = {"asr_meta": "", "asr_telemetry": "",
            "v2_asr_meta_stale_max_hours": 24}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _write_meta(directory, payload, name=RUN_META_NAME):
    p = directory / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _write_srt(directory, name="demo.srt"):
    p = directory / name
    p.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. 容错解析
# ---------------------------------------------------------------------------

def test_present_with_status_and_mileage(tmp_path):
    _write_meta(tmp_path, {"status": "OK", "mileage_pct": 42})
    meta = load_asr_meta(_cfg(), str(_write_srt(tmp_path)))
    assert meta["present"] is True
    assert meta["status"] == "ok"                      # 转小写字符串
    assert meta["mileage_pct"] == 42.0
    assert meta["file"] == RUN_META_NAME
    assert meta["warnings"] == []


def test_missing_keys_yields_signal_without_semantic_fields(tmp_path):
    """可读 JSON 但缺候选键：present=True、语义字段为 None（未识别字段忽略）。"""
    _write_meta(tmp_path, {"version": 3, "engine": "faster-whisper",
                           "segments": 1200})
    meta = load_asr_meta(_cfg(), str(_write_srt(tmp_path)))
    assert meta["present"] is True
    assert meta["status"] is None
    assert meta["mileage_pct"] is None
    assert fingerprint(meta) is None                   # 无语义字段 → 无指纹


def test_candidate_keys_in_declared_order(tmp_path):
    """候选键按声明顺序取首个可识别值（run_status 先于 state）。"""
    _write_meta(tmp_path, {"state": "suspect", "run_status": "ok",
                           "junk": {"nested": True}})
    meta = load_asr_meta(_cfg(), str(_write_srt(tmp_path)))
    assert meta["status"] == "ok"
    _write_meta(tmp_path, {"state": "suspect"})
    meta2 = load_asr_meta(_cfg(), str(_write_srt(tmp_path)))
    assert meta2["status"] == "suspect"


def test_status_and_coverage_candidate_key_order(tmp_path):
    _write_meta(tmp_path, {"state": "failed", "run_status": "completed",
                           "coverage": 0.55})
    meta = load_asr_meta(_cfg(), str(_write_srt(tmp_path)))
    assert meta["status"] == "completed"               # run_status 先于 state
    assert meta["mileage_pct"] == 55.0                 # 0-1 比例 ×100


def test_type_drift_numeric_status_and_string_coverage(tmp_path):
    _write_meta(tmp_path, {"status": 0, "mileage": "37.5"})
    meta = load_asr_meta(_cfg(), str(_write_srt(tmp_path)))
    assert meta["status"] == "0"
    assert meta["mileage_pct"] == 37.5


def test_type_drift_invalid_values_skipped(tmp_path):
    """bool/负数/不可转换值跳过，继续尝试下一候选键。"""
    _write_meta(tmp_path, {"status": True, "coverage": -5,
                           "mileage_pct": "abc", "speech_ratio": "0.8"})
    meta = load_asr_meta(_cfg(), str(_write_srt(tmp_path)))
    assert meta["status"] is None                      # bool 不作状态
    assert meta["mileage_pct"] == 80.0                 # 前两个候选无效，speech_ratio 生效


def test_mileage_ratio_vs_percent_boundary(tmp_path):
    """0-1 视为比例 ×100（含 1=100%）；>1 视为百分比原样。"""
    _write_meta(tmp_path, {"mileage": 1})
    assert load_asr_meta(_cfg(), str(_write_srt(tmp_path)))["mileage_pct"] == 100.0
    _write_meta(tmp_path, {"mileage": 0})
    assert load_asr_meta(_cfg(), str(_write_srt(tmp_path)))["mileage_pct"] == 0.0
    _write_meta(tmp_path, {"mileage": 76.5})
    assert load_asr_meta(_cfg(), str(_write_srt(tmp_path)))["mileage_pct"] == 76.5


# ---------------------------------------------------------------------------
# 2. 显式路径
# ---------------------------------------------------------------------------

def test_explicit_path_missing_yields_warning_not_exception(tmp_path):
    meta = load_asr_meta(_cfg(asr_meta=str(tmp_path / "nope.json")),
                         str(_write_srt(tmp_path)))
    assert meta["present"] is False
    assert meta["warnings"] and "不存在" in meta["warnings"][0]


def test_explicit_dir_finds_run_meta_inside(tmp_path):
    meta_dir = tmp_path / "run1"
    meta_dir.mkdir()
    (meta_dir / RUN_META_NAME).write_text('{"status": "ok"}', encoding="utf-8")
    meta = load_asr_meta(_cfg(asr_meta=str(meta_dir)),
                         str(_write_srt(tmp_path)))
    assert meta["present"] is True and meta["status"] == "ok"
    assert meta["file"] == RUN_META_NAME               # 报告口径只记 basename


def test_explicit_file_used_directly(tmp_path):
    meta_file = tmp_path / "custom_run.json"
    meta_file.write_text('{"status": "suspect"}', encoding="utf-8")
    meta = load_asr_meta(_cfg(asr_meta=str(meta_file)),
                         str(tmp_path / "elsewhere.srt"))
    assert meta["present"] is True and meta["status"] == "suspect"


def test_explicit_corrupt_json_silent(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    meta = load_asr_meta(_cfg(asr_meta=str(bad)), str(_write_srt(tmp_path)))
    assert meta["present"] is False
    assert meta["warnings"] and "读取失败" in meta["warnings"][0]


def test_explicit_takes_precedence_over_auto_discovery(tmp_path):
    _write_meta(tmp_path, {"status": "ok"})
    other = tmp_path / "explicit.json"
    other.write_text('{"status": "empty"}', encoding="utf-8")
    meta = load_asr_meta(_cfg(asr_meta=str(other)), str(_write_srt(tmp_path)))
    assert meta["status"] == "empty"


# ---------------------------------------------------------------------------
# 3. 自动发现 + 新鲜度（R6）
# ---------------------------------------------------------------------------

def test_auto_discovery_without_sidecar_is_silent_no_signal(tmp_path):
    srt = _write_srt(tmp_path)
    meta = load_asr_meta(_cfg(), str(srt))
    assert meta == {"present": False, "status": None, "mileage_pct": None,
                    "stale": False, "file": None, "warnings": []}


def test_fresh_sidecar_accepted(tmp_path):
    _write_meta(tmp_path, {"status": "ok"})
    srt = _write_srt(tmp_path)
    meta = load_asr_meta(_cfg(), str(srt))
    assert meta["present"] is True and meta["stale"] is False


def test_stale_sidecar_rejected_with_warning(tmp_path):
    """超龄（比 SRT 旧超过 24h）→ 无信号 + stale=True + 警告（R6）。"""
    srt = _write_srt(tmp_path)
    meta_p = _write_meta(tmp_path, {"status": "ok"})
    now = time.time()
    os.utime(str(srt), (now, now))
    stale_stamp = now - 25 * 3600                      # 比 SRT 旧 25 小时
    os.utime(str(meta_p), (stale_stamp, stale_stamp))
    meta = load_asr_meta(_cfg(), str(srt))
    assert meta["present"] is False
    assert meta["stale"] is True
    assert meta["file"] == RUN_META_NAME
    assert meta["warnings"] and "超龄" in meta["warnings"][0]


def test_stale_threshold_configurable(tmp_path):
    """阈值可配：1 小时上限时，旧 2 小时即超龄。"""
    srt = _write_srt(tmp_path)
    meta_p = _write_meta(tmp_path, {"status": "ok"})
    now = time.time()
    os.utime(str(srt), (now, now))
    stamp = now - 2 * 3600
    os.utime(str(meta_p), (stamp, stamp))
    meta = load_asr_meta(_cfg(v2_asr_meta_stale_max_hours=1), str(srt))
    assert meta["present"] is False and meta["stale"] is True
    # 上限放宽到 3 小时 → 新鲜
    meta2 = load_asr_meta(_cfg(v2_asr_meta_stale_max_hours=3), str(srt))
    assert meta2["present"] is True and meta2["stale"] is False


def test_no_srt_context_and_no_explicit_means_no_signal():
    """指纹计算等无 SRT 上下文场景：不自动发现、直接无信号。"""
    meta = load_asr_meta(_cfg(), "")
    assert meta["present"] is False and meta["warnings"] == []


# ---------------------------------------------------------------------------
# 4. 指纹语义（R1）
# ---------------------------------------------------------------------------

def test_fingerprint_same_content_different_paths_identical(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    (d1 / RUN_META_NAME).write_text('{"status": "ok", "mileage_pct": 42}',
                                    encoding="utf-8")
    (d2 / RUN_META_NAME).write_text('{"coverage": 0.42, "state": "OK"}',
                                    encoding="utf-8")  # 候选键/键序不同、比例口径换算后同语义
    m1 = load_asr_meta(_cfg(asr_meta=str(d1)), "")
    m2 = load_asr_meta(_cfg(asr_meta=str(d2)), "")
    assert fingerprint(m1) == fingerprint(m2)
    assert fingerprint(m1) == hashlib.sha1(
        json.dumps({"mileage_pct": 42.0, "status": "ok"},
                   sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def test_fingerprint_changes_on_content_change(tmp_path):
    p = tmp_path / RUN_META_NAME
    p.write_text('{"status": "ok"}', encoding="utf-8")
    h1 = fingerprint(load_asr_meta(_cfg(asr_meta=str(p)), ""))
    p.write_text('{"status": "suspect"}', encoding="utf-8")
    h2 = fingerprint(load_asr_meta(_cfg(asr_meta=str(p)), ""))
    assert h1 != h2
    p.write_text('{"status": "ok", "mileage_pct": 10}', encoding="utf-8")
    assert fingerprint(load_asr_meta(_cfg(asr_meta=str(p)), "")) != h1


def test_fingerprint_none_without_signal():
    assert fingerprint(None) is None
    assert fingerprint({}) is None
    assert fingerprint({"present": False, "status": "ok"}) is None
    assert fingerprint({"present": True}) is None       # 可读但无语义字段


def test_suspect_statuses_constant():
    assert sorted(SUSPECT_STATUSES) == ["empty", "failed", "suspect"]


# ---------------------------------------------------------------------------
# 5. 配置接入
# ---------------------------------------------------------------------------

def test_config_defaults_for_asr_meta_fields():
    from subtransjav.refine.config import (
        DEFAULT_V2_ASR_META_MIN_COVERAGE_PCT,
        DEFAULT_V2_ASR_META_STALE_MAX_HOURS,
        RefineConfig,
    )
    cfg = RefineConfig()
    assert cfg.asr_meta == ""
    assert cfg.v2_asr_meta_min_coverage_pct == 30
    assert cfg.v2_asr_meta_stale_max_hours == 24
    assert DEFAULT_V2_ASR_META_MIN_COVERAGE_PCT == 30
    assert DEFAULT_V2_ASR_META_STALE_MAX_HOURS == 24


# ---------------------------------------------------------------------------
# 6. H4b：场景级转写遥测（load_asr_telemetry 解析/防御例）
# ---------------------------------------------------------------------------

def _scene(n, **kw) -> dict:
    """实测 1.9.3 schema 的合成场景行（字段可为 null）。"""
    base = {"media": "demo", "scene": n, "elapsed_s": 1.0,
            "audio_duration_s": 10.0, "wall_s": 1.0, "produced_output": True,
            "model_epoch": 1, "rtf": 0.1, "n_segments": 5,
            "max_temperature": 0.0, "fallback_segments": 0,
            "min_avg_logprob": -0.5, "mean_avg_logprob": -0.5,
            "max_compression_ratio": 1.2, "max_no_speech_prob": 0.1,
            "cuda_used_mb": 1000.0, "cuda_allocated_mb": 0.0,
            "cuda_reserved_mb": 0.0, "rss_mb": 900.0}
    base.update(kw)
    return base


def _write_telemetry(directory, scenes, stem="demo"):
    raw = directory / "raw_subs"
    raw.mkdir(exist_ok=True)
    p = raw / f"{stem}.asr_telemetry.jsonl"
    p.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in scenes) + "\n",
        encoding="utf-8")
    return p


def test_telemetry_parse_keeps_non_null_signal_fields(tmp_path):
    _write_telemetry(tmp_path, [_scene(1), _scene(2, max_temperature=0.5)])
    r = load_asr_telemetry(_cfg(), str(_write_srt(tmp_path)))
    assert r["present"] is True and r["stale"] is False
    assert r["file"] == "demo.asr_telemetry.jsonl"
    assert set(r["scenes"]) == {1, 2}
    assert r["scenes"][1]["audio_duration_s"] == 10.0
    assert r["scenes"][1]["produced_output"] is True
    assert r["scenes"][2]["max_temperature"] == 0.5
    assert r["skipped_lines"] == 0 and r["warnings"] == []


def test_telemetry_null_fields_dropped_not_zeroed(tmp_path):
    """字段 null → 该信号不可用（dict 缺省，不当 0、不判低信任）；
    produced_output=False 保留布尔值（硬信号）。"""
    _write_telemetry(tmp_path, [
        _scene(1, max_no_speech_prob=None, mean_avg_logprob=None,
               max_compression_ratio=None),
        _scene(2, produced_output=False, max_temperature=None,
               min_avg_logprob=None, mean_avg_logprob=None,
               max_compression_ratio=None, max_no_speech_prob=None),
    ])
    r = load_asr_telemetry(_cfg(), str(_write_srt(tmp_path)))
    s1 = r["scenes"][1]
    assert "max_no_speech_prob" not in s1
    assert "mean_avg_logprob" not in s1
    assert "max_compression_ratio" not in s1
    assert r["scenes"][2]["produced_output"] is False


def test_telemetry_bad_lines_skipped_and_counted(tmp_path):
    """坏行（坏 JSON/非 dict/缺 scene/scene 非整数/bool scene/坏时长）
    逐行跳过并计数；空行静默忽略不计数。"""
    raw = tmp_path / "raw_subs"
    raw.mkdir()
    (raw / "demo.asr_telemetry.jsonl").write_text("\n".join([
        json.dumps(_scene(1)),
        "{not json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"audio_duration_s": 5.0}),                    # 缺 scene
        json.dumps({"scene": "2", "audio_duration_s": 5.0}),      # scene 非整数
        json.dumps({"scene": True, "audio_duration_s": 5.0}),     # bool scene
        json.dumps({"scene": 2, "audio_duration_s": "x"}),        # 坏时长
        json.dumps({"scene": 3, "audio_duration_s": -1.0}),       # 负时长
        "",
        json.dumps(_scene(4)),
    ]), encoding="utf-8")
    r = load_asr_telemetry(_cfg(), str(_write_srt(tmp_path)))
    assert r["present"] is True
    assert set(r["scenes"]) == {1, 4}
    assert r["skipped_lines"] == 7


def test_telemetry_duplicate_scene_counted_as_bad_line(tmp_path):
    _write_telemetry(tmp_path, [_scene(1)])
    raw = tmp_path / "raw_subs" / "demo.asr_telemetry.jsonl"
    raw.write_text(json.dumps(_scene(1)) + "\n" + json.dumps(_scene(1)) + "\n",
                   encoding="utf-8")
    r = load_asr_telemetry(_cfg(), str(_write_srt(tmp_path)))
    assert r["present"] is True and set(r["scenes"]) == {1}
    assert r["skipped_lines"] == 1


def test_telemetry_missing_file_zero_hit_warns(tmp_path):
    srt = _write_srt(tmp_path)
    r = load_asr_telemetry(_cfg(), str(srt))
    assert r["present"] is False and r["file"] is None
    assert r["scenes"] == {} and r["skipped_lines"] == 0
    assert r["warnings"] and "未发现" in r["warnings"][0]


def test_telemetry_explicit_path_used_directly(tmp_path):
    p = tmp_path / "explicit.jsonl"
    p.write_text(json.dumps(_scene(1)), encoding="utf-8")
    r = load_asr_telemetry(_cfg(asr_telemetry=str(p)), "")
    assert r["present"] is True and r["file"] == "explicit.jsonl"
    expected_scene = {k: v for k, v in _scene(1).items()
                      if k in ("produced_output", "model_epoch", "rtf",
                               "n_segments", "max_temperature",
                               "fallback_segments", "min_avg_logprob",
                               "mean_avg_logprob", "max_compression_ratio",
                               "max_no_speech_prob", "cuda_used_mb",
                               "cuda_allocated_mb", "cuda_reserved_mb",
                               "rss_mb")}
    expected_scene["audio_duration_s"] = 10.0
    assert r["scenes"] == {1: expected_scene}
    assert r["warnings"] == []


def test_telemetry_explicit_missing_yields_warning(tmp_path):
    cfg = _cfg(asr_telemetry=str(tmp_path / "nope.jsonl"))
    r = load_asr_telemetry(cfg, str(_write_srt(tmp_path)))
    assert r["present"] is False
    assert r["warnings"] and "不存在" in r["warnings"][0]


def test_telemetry_stale_auto_discovered_rejected(tmp_path):
    """自动发现超龄（比 SRT 旧超过 24h）→ stale=True 弃用信号（R6）。"""
    srt = _write_srt(tmp_path)
    p = _write_telemetry(tmp_path, [_scene(1)])
    now = time.time()
    os.utime(str(srt), (now, now))
    stamp = now - 25 * 3600
    os.utime(str(p), (stamp, stamp))
    r = load_asr_telemetry(_cfg(), str(srt))
    assert r["present"] is False and r["stale"] is True
    assert r["file"] == "demo.asr_telemetry.jsonl"
    assert r["scenes"] == {}
    assert r["warnings"] and "超龄" in r["warnings"][0]


def test_telemetry_stale_threshold_configurable(tmp_path):
    """阈值复用 v2_asr_meta_stale_max_hours：1 小时上限时旧 2 小时即超龄。"""
    srt = _write_srt(tmp_path)
    p = _write_telemetry(tmp_path, [_scene(1)])
    now = time.time()
    os.utime(str(srt), (now, now))
    stamp = now - 2 * 3600
    os.utime(str(p), (stamp, stamp))
    r = load_asr_telemetry(_cfg(v2_asr_meta_stale_max_hours=1), str(srt))
    assert r["present"] is False and r["stale"] is True
    r2 = load_asr_telemetry(_cfg(v2_asr_meta_stale_max_hours=3), str(srt))
    assert r2["present"] is True and r2["stale"] is False


def test_telemetry_corrupt_file_never_raises(tmp_path):
    """不可解码字节 → 无信号 + 警告，绝不抛异常（全容错红线）。"""
    raw = tmp_path / "raw_subs"
    raw.mkdir()
    (raw / "demo.asr_telemetry.jsonl").write_bytes(b"\xff\xfe\x00bad")
    r = load_asr_telemetry(_cfg(), str(_write_srt(tmp_path)))
    assert r["present"] is False and r["scenes"] == {}
    assert r["warnings"] and "读取失败" in r["warnings"][0]
