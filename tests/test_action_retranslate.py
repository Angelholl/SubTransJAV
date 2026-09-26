"""D11 契约④⑤ 行动层重翻执行器测试：全部真函数 + tmp_path + FakeClient
monkeypatch（_make_action_client 注入点），不起真 LLM、不触网。"""

import argparse
import json

import pytest

from subtransjav.refine import action_retranslate
from subtransjav.refine.action_retranslate import (
    parse_entries_arg,
    run_action_retranslate,
)
from subtransjav.refine.config import RefineConfig
from subtransjav.refine.filters import build_srt, parse_srt

T1 = "00:00:01,000 --> 00:00:02,000"
T2 = "00:00:03,000 --> 00:00:04,000"
T3 = "00:00:05,000 --> 00:00:06,000"
T4 = "00:00:07,000 --> 00:00:08,000"

FINAL_ENTRIES = [
    {"index": 1, "timing": T1, "text": "こんにちは"},
    {"index": 2, "timing": T2, "text": "[未翻译] テスト"},
    {"index": 3, "timing": T3, "text": "前辈真厉害"},
    {"index": 4, "timing": T4, "text": "四"},
]

LEDGER_FIELDS = {"index", "timing", "category", "old_text", "new_text",
                 "model_used", "outcome", "reason", "ts", "source_partial"}


def _item(index, timing, text, category="mistranslation", **kw):
    it = {"index": index, "timing": timing, "category": category,
          "message": "疑似误译", "current_text": text,
          "source_excerpt": "せんぱい", "status": "open", "severity": "warning"}
    it.update(kw)
    return it


def _write_final(tmp_path, entries):
    (tmp_path / "ep01_final_cn.srt").write_text(build_srt(entries),
                                                encoding="utf-8")


def _write_guide(tmp_path, items):
    guide = {"version": 2, "source": "ep01.srt", "stem": "ep01",
             "generated_at": "2026-01-01 00:00:00", "basis": "基于本次运行",
             "conclusions": ["结论甲"], "sections": ["章节乙"],
             "extras": {"对齐率": "90.0%"}, "items": items,
             "companions": {}}
    p = tmp_path / "ep01_质量报告导读.json"
    p.write_text(json.dumps(guide, ensure_ascii=False), encoding="utf-8")
    return p


def _args(tmp_path, **kw):
    defaults = dict(
        action_retranslate=str(tmp_path / "ep01_质量报告导读.json"),
        entries="", action_source="", action_model="",
        action_sample=0, apply=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _cfg():
    return RefineConfig(inputs=[])


def _read_final(tmp_path):
    return parse_srt((tmp_path / "ep01_final_cn.srt").read_text(
        encoding="utf-8"))


def _read_ledger(tmp_path):
    return json.loads((tmp_path / "ep01_重翻记录.json").read_text(
        encoding="utf-8"))


class FakeClient:
    """单条 _chat 替身：按序回放响应（序列元素为 Exception 实例则抛出）
    或整体抛错，记录全部调用。"""

    def __init__(self, responses=None, error=None):
        self.calls = []
        self.responses = list(responses or [])
        self.error = error

    def _chat(self, system_text, user_text, max_tokens=None):
        self.calls.append((system_text, user_text))
        if self.error is not None:
            raise self.error
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return "重翻后的中文台词"


@pytest.fixture
def install_client(monkeypatch):
    """安装 _make_action_client 注入点替身，返回 FakeClient 实例。"""
    def _install(responses=None, error=None):
        fake = FakeClient(responses=responses, error=error)
        monkeypatch.setattr(action_retranslate, "_make_action_client",
                            lambda cfg, model_override="": fake)
        return fake
    return _install


# ---------------------------------------------------------------------------
# --entries 解析
# ---------------------------------------------------------------------------

def test_parse_entries_arg_single_range_and_mixed():
    assert parse_entries_arg("3") == {3}
    assert parse_entries_arg("12-15") == {12, 13, 14, 15}
    assert parse_entries_arg("3,7,12-15") == {3, 7, 12, 13, 14, 15}
    assert parse_entries_arg(" 3 , 7 ") == {3, 7}      # 容忍空隙空白
    assert parse_entries_arg("") == set()              # 缺省=空集


@pytest.mark.parametrize("spec", ["3;a", "a", "1,,2", "15-12", "1-2-3", "-5"])
def test_parse_entries_arg_illegal_raises(spec):
    with pytest.raises(ValueError):
        parse_entries_arg(spec)


# ---------------------------------------------------------------------------
# dry-run：零写入 + 计划打印
# ---------------------------------------------------------------------------

def test_dry_run_zero_writes_and_prints_plan(tmp_path, capsys):
    _write_final(tmp_path, FINAL_ENTRIES)
    guide_path = _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト", category="untranslated"),
        _item(3, T3, "前辈真厉害"),
    ])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    rc = run_action_retranslate(_cfg(), _args(tmp_path))
    assert rc == 0
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert before == after                          # 零写入（含台账不产生）
    out = capsys.readouterr().out
    assert "重翻计划" in out and "零写入" in out
    assert "#2" in out and "#3" in out
    assert "テスト" in out[:out.index("#3")]        # 现译文本预览出现
    assert guide_path.is_file()


# ---------------------------------------------------------------------------
# apply：恒等式（非目标块逐字节不变 + 目标块文本更新）
# ---------------------------------------------------------------------------

def test_apply_updates_targets_and_keeps_others_byte_identical(
        tmp_path, install_client):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト"),
        _item(3, T3, "前辈真厉害"),
    ])
    install_client(responses=["新翻的第二块。", "重翻的第三块。"])
    rc = run_action_retranslate(_cfg(), _args(tmp_path, apply=True))
    assert rc == 0
    entries = _read_final(tmp_path)
    assert len(entries) == len(FINAL_ENTRIES)       # 条目数不变
    assert [e["timing"] for e in entries] == \
        [e["timing"] for e in FINAL_ENTRIES]        # 逐块 timing 全等
    assert entries[0]["text"] == "こんにちは"        # 非目标块原样
    assert entries[3]["text"] == "四"
    assert entries[1]["text"] == "新翻的第二块。"     # 目标块文本更新
    assert entries[2]["text"] == "重翻的第三块。"


# ---------------------------------------------------------------------------
# timing 定位纪律：歧义/未命中/空 timing 一律 failed 不猜
# ---------------------------------------------------------------------------

def test_timing_ambiguity_miss_and_empty_recorded_failed(
        tmp_path, install_client):
    dup = [dict(e) for e in FINAL_ENTRIES]
    dup[2]["timing"] = T2                            # 人造 timing 歧义
    _write_final(tmp_path, dup)
    _write_guide(tmp_path, [
        _item(5, T2, "x"),                           # 命中两块 → 歧义
        _item(6, "00:00:99,000 --> 00:00:99,500", "y"),  # 未命中
        _item(7, "", "z"),                           # 空 timing
    ])
    fake = install_client()
    rc = run_action_retranslate(_cfg(), _args(tmp_path, apply=True))
    assert rc == 1                                   # 全败
    records = _read_ledger(tmp_path)
    assert len(records) == 3
    assert all(r["outcome"] == "failed" for r in records)
    assert "歧义" in records[0]["reason"]
    assert "未命中" in records[1]["reason"]
    assert "timing 为空" in records[2]["reason"]
    assert fake.calls == []                          # 绝不猜 → 零 LLM 调用
    # 终稿未被改动
    assert _read_final(tmp_path) == parse_srt(build_srt(dup))


# ---------------------------------------------------------------------------
# 失败最小质量门：不过→保留原文 outcome=failed
# ---------------------------------------------------------------------------

def test_quality_gate_failures_keep_original_text(tmp_path, install_client):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト"),
        _item(3, T3, "前辈真厉害"),
    ])
    # 纯 ASCII（非合格中文）与与旧文相同两种失败形态
    install_client(responses=["123456", "前辈真厉害"])
    rc = run_action_retranslate(_cfg(), _args(tmp_path, apply=True))
    assert rc == 1
    assert _read_final(tmp_path) == parse_srt(build_srt(FINAL_ENTRIES))
    records = _read_ledger(tmp_path)
    assert all(r["outcome"] == "failed" for r in records)
    assert "合格中文" in records[0]["reason"]
    assert "与现有译文相同" in records[1]["reason"]
    assert records[0]["new_text"] == "123456"        # 落选候选入台账供审计


def test_llm_call_exception_marks_item_failed_not_batch(
        tmp_path, install_client):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト"),
        _item(3, T3, "前辈真厉害"),
    ])
    install_client(responses=[RuntimeError("boom"), "合格的新译文。"])
    rc = run_action_retranslate(_cfg(), _args(tmp_path, apply=True))
    assert rc == 3                                   # 一败一成 → 部分降级
    records = _read_ledger(tmp_path)
    assert records[0]["outcome"] == "failed"
    assert "LLM 调用失败" in records[0]["reason"]
    assert records[1]["outcome"] == "applied"
    assert _read_final(tmp_path)[2]["text"] == "合格的新译文。"


def test_client_construction_failure_marks_all_failed(tmp_path, monkeypatch):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [_item(2, T2, "[未翻译] テスト")])

    def _boom(cfg, model_override=""):
        raise RuntimeError("no endpoint")
    monkeypatch.setattr(action_retranslate, "_make_action_client", _boom)
    rc = run_action_retranslate(_cfg(), _args(tmp_path, apply=True))
    assert rc == 1
    records = _read_ledger(tmp_path)
    assert records[0]["outcome"] == "failed"
    assert "客户端构造失败" in records[0]["reason"]


# ---------------------------------------------------------------------------
# 台账：累积追加与字段齐全
# ---------------------------------------------------------------------------

def test_ledger_appends_across_runs_with_full_fields(tmp_path, install_client):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト"),
        _item(3, T3, "前辈真厉害"),
    ])
    install_client(responses=["第一轮改写文本。"])
    assert run_action_retranslate(
        _cfg(), _args(tmp_path, apply=True, entries="2")) == 0
    install_client(responses=["第二轮改写文本。"])
    assert run_action_retranslate(
        _cfg(), _args(tmp_path, apply=True, entries="3")) == 0
    records = _read_ledger(tmp_path)
    assert len(records) == 2                         # 累积追加不覆盖
    assert records[0]["index"] == 2 and records[1]["index"] == 3
    for r in records:
        assert set(r) >= LEDGER_FIELDS
        assert r["outcome"] == "applied"
        assert r["ts"] and r["model_used"]
        assert r["old_text"] and r["new_text"]


# ---------------------------------------------------------------------------
# 导读快照刷新
# ---------------------------------------------------------------------------

def test_guide_snapshot_refreshed_but_conclusions_kept(tmp_path,
                                                       install_client):
    _write_final(tmp_path, FINAL_ENTRIES)
    (tmp_path / "ep01_质量报告.txt").write_text("旧报告", encoding="utf-8")
    _write_guide(tmp_path, [_item(2, T2, "[未翻译] テスト")])
    install_client(responses=["全新的第二块。"])
    run_action_retranslate(_cfg(), _args(tmp_path, apply=True))
    guide = json.loads((tmp_path / "ep01_质量报告导读.json").read_text(
        encoding="utf-8"))
    assert guide["generated_at"] != "2026-01-01 00:00:00"
    assert guide["retranslated_at"] == guide["generated_at"]
    assert guide["conclusions"] == ["结论甲"]         # 结论区保真
    assert guide["sections"] == ["章节乙"]
    assert guide["extras"] == {"对齐率": "90.0%"}
    assert guide["items"][0]["current_text"] == "全新的第二块。"
    assert len(guide["companions"]) == 6             # 六件存在性重算
    assert guide["companions"]["ep01_质量报告.txt"] is True
    assert guide["companions"]["ep01_风险清单.md"] is False


# ---------------------------------------------------------------------------
# 标陈旧打印
# ---------------------------------------------------------------------------

def test_stale_print_contains_three_names(tmp_path, install_client, capsys):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [_item(2, T2, "[未翻译] テスト")])
    install_client(responses=["重翻后的第二块。"])
    run_action_retranslate(_cfg(), _args(tmp_path, apply=True))
    out = capsys.readouterr().out
    for name in ("ep01_质量报告.txt", "ep01_分歧复核.csv",
                 "ep01_术语冲突观察.csv"):
        assert name in out


# ---------------------------------------------------------------------------
# 源文恢复：--action-source timing 对齐 / 退化摘录
# ---------------------------------------------------------------------------

def test_source_recovery_full_hit_and_partial_fallback(tmp_path,
                                                       install_client):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト"),
        _item(3, T3, "前辈真厉害"),
    ])
    (tmp_path / "ep01.ja.srt").write_text(
        build_srt([{"index": 1, "timing": T2, "text": "お疲れ様です。"}]),
        encoding="utf-8")
    fake = install_client(responses=["辛苦了呀。", "前辈真棒呀。"])
    rc = run_action_retranslate(_cfg(), _args(
        tmp_path, apply=True, action_source=str(tmp_path / "ep01.ja.srt")))
    assert rc == 0
    records = _read_ledger(tmp_path)
    assert records[0]["source_partial"] is False
    assert "お疲れ様です。" in fake.calls[0][1]       # 完整源文进提示词
    assert records[1]["source_partial"] is True      # 未命中 → 摘录退化
    assert "せんぱい" in fake.calls[1][1]             # 摘录进提示词


def test_no_action_source_hint_without_path(tmp_path, capsys):
    """未提供 --action-source：提示含"未提供 --action-source"与退化条数，
    不含"不存在"、不打任何路径。"""
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト"),
        _item(3, T3, "前辈真厉害"),
    ])
    rc = run_action_retranslate(_cfg(), _args(tmp_path))   # 默认 action_source=""
    assert rc == 0
    out = capsys.readouterr().out
    assert "未提供 --action-source" in out
    assert "2 条退化为导读摘录" in out
    assert "不存在" not in out
    assert "仅导读摘录" in out                       # dry-run 计划行形态不变


def test_nonexistent_action_source_prints_given_path(tmp_path, capsys):
    """提供了 --action-source 但文件不存在：保留"不存在"提示并回显传入路径。"""
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [_item(2, T2, "[未翻译] テスト")])
    rc = run_action_retranslate(_cfg(), _args(
        tmp_path, action_source=str(tmp_path / "missing.ja.srt")))
    assert rc == 0
    out = capsys.readouterr().out
    assert "指定的原始日文 SRT 不存在" in out
    assert "missing.ja.srt" in out
    assert "1 条退化为导读摘录" in out


# ---------------------------------------------------------------------------
# --action-sample：只取前 N 条
# ---------------------------------------------------------------------------

def test_action_sample_limits_selection(tmp_path, capsys):
    _write_final(tmp_path, FINAL_ENTRIES)
    _write_guide(tmp_path, [
        _item(2, T2, "[未翻译] テスト"),
        _item(3, T3, "前辈真厉害"),
        _item(4, T4, "四"),
    ])
    rc = run_action_retranslate(_cfg(), _args(tmp_path, action_sample=2))
    assert rc == 0
    out = capsys.readouterr().out
    assert "#2" in out and "#3" in out
    assert "#4" not in out
    assert not (tmp_path / "ep01_重翻记录.json").exists()   # dry-run 不写台账


# ---------------------------------------------------------------------------
# 负向钉：行动层参数不进 manifest 指纹
# ---------------------------------------------------------------------------

def test_action_params_not_in_config_fingerprint():
    from subtransjav.refine.manifest import _CONFIG_FIELDS, compute_config_hash
    assert "action_retranslate" not in _CONFIG_FIELDS
    # 含/不含行动层参数的 cfg 哈希必须相等（参数走 CLI 直连，不进指纹；
    # 手法同 test_manifest_model 的哈希敏感度钉）
    base = RefineConfig(inputs=["a.srt"])
    with_action = RefineConfig(inputs=["a.srt"])
    with_action.action_retranslate = "guide.json"    # 模拟误挂字段
    assert compute_config_hash(with_action) == compute_config_hash(base)
    assert compute_config_hash(base) == compute_config_hash(RefineConfig(
        inputs=["a.srt"]))
