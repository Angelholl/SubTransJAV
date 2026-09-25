"""tests for tools/hro2_gate.py（HRO-2 拆分验收门 harness，批次 HRO-2）

契约（不跑管线，只测纯函数与 manifest 往返）：
  1. 归一化：时间戳行 / TM 行被替换为占位、普通行原样、归一化幂等。
  2. compare_outputs：final 逐字节 + 报告归一化后比对——
     仅白名单差异（时间戳/TM 行）→ PASS；final 差一字节 → FAIL；
     报告白名单外差异 → FAIL。
  3. run_manifest.json 写读往返无损。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import hro2_gate  # noqa: E402

# ---------------------------------------------------------------- 归一化

def test_normalize_timestamp_line():
    line = "来源: a.srt | 时间: 2026-09-25 12:34:56 | "
    assert hro2_gate.normalize_report_line(line) == \
        "来源: a.srt | 时间: <归一化> | "


def test_normalize_tm_lines():
    assert hro2_gate.normalize_report_line(
        "TM: 精确命中 3 条 | 本次学习入库 5 条") == "TM: <归一化>"
    assert hro2_gate.normalize_report_line("TM: 无样本") == "TM: <归一化>"


def test_normalize_plain_line_untouched():
    plain = "条数核对: 原文 10 = 闸门0删除 0 + 终稿 10"
    assert hro2_gate.normalize_report_line(plain) == plain


def test_normalize_idempotent():
    text = "时间: 2026-09-25 12:34:56\nTM: 无样本\n普通行"
    once = hro2_gate.normalize_report_text(text)
    assert hro2_gate.normalize_report_text(once) == once


# ---------------------------------------------------------------- compare

def _write_outputs(base: Path, final: str, report: str) -> Path:
    d = base / "demo"
    d.mkdir(parents=True)
    (d / "demo_final_cn.srt").write_text(final, encoding="utf-8")
    (d / "demo_质量报告.txt").write_text(report, encoding="utf-8")
    return d


def test_compare_pass_with_whitelist_only_diffs(tmp_path):
    report_a = "质量报告\n时间: 2026-09-25 10:00:00\nTM: 精确命中 1 条 | 本次学习入库 2 条\n普通行"
    report_b = "质量报告\n时间: 2026-09-25 11:00:00\nTM: 无样本\n普通行"
    da = _write_outputs(tmp_path / "a", "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
                        report_a)
    db = _write_outputs(tmp_path / "b", "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
                        report_b)
    res = hro2_gate.compare_outputs(da, db)
    assert res["equal"], res


def test_compare_fail_final_one_byte_diff(tmp_path):
    report = "质量报告\n时间: 2026-09-25 10:00:00\nTM: 无样本\n"
    da = _write_outputs(tmp_path / "a", "你好\n", report)
    db = _write_outputs(tmp_path / "b", "你坏\n", report)
    res = hro2_gate.compare_outputs(da, db)
    assert not res["equal"]
    assert any(d["kind"] == "final_bytes" for d in res["differences"])


def test_compare_fail_report_non_whitelist_diff(tmp_path):
    da = _write_outputs(tmp_path / "a", "你好\n", "结论行A\n时间: 2026-09-25 10:00:00\n")
    db = _write_outputs(tmp_path / "b", "你好\n", "结论行B\n时间: 2026-09-25 11:00:00\n")
    res = hro2_gate.compare_outputs(da, db)
    assert not res["equal"]
    assert any(d["kind"] == "report_line" and d["line"] == 1
               for d in res["differences"])


# ---------------------------------------------------------------- manifest

def test_run_manifest_roundtrip(tmp_path):
    manifest = {
        "label": "pre",
        "captured_at_utc": "2026-09-25T00:00:00Z",
        "git_head": "unknown",
        "glossary": "config/glossary.csv",
        "glossary_sha1": "a" * 40,
        "config_fingerprint": {"v2_ctx_local": 22272, "v2_concurrency": 1,
                               "tm_enabled": True, "auto_synopsis": False},
        "files": {"demo": {"input": "x.srt", "input_sha256": "b" * 64,
                           "final": "demo_final_cn.srt",
                           "final_sha256": "c" * 64,
                           "report": "demo_质量报告.txt",
                           "report_sha256": "d" * 64}},
    }
    p = tmp_path / hro2_gate.MANIFEST_NAME
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded == manifest


# ---------------------------------------------------------------- 真实布局回归钉
# （A3 缺陷修复：compare 原顶层 glob 对真实 capture 布局 run/{stem}/ 命中为空，
#   循环 0 次 → 假 PASS；2026-09-25 拆分期间四次 compare 空转后修复并重做真裁决）

def test_compare_nested_capture_layout_pass(tmp_path):
    """真实 capture 嵌套布局（产物在 run/{stem}/ 下）必须真比对。"""
    report = "质量报告\n时间: 2026-09-25 10:00:00\nTM: 无样本\n普通行"
    final = "1\n00:00:01,000 --> 00:00:02,000\n你好\n"
    da = _write_outputs(tmp_path / "a", final, report)
    db = _write_outputs(tmp_path / "b", final, report)
    res = hro2_gate.compare_outputs(da.parent, db.parent)   # 文件在 a/demo/、b/demo/
    assert res["equal"], res


def test_compare_nested_capture_layout_report_diff_fails(tmp_path):
    """嵌套布局下报告白名单外差异必须 FAIL（旧实现此场景假 PASS）。"""
    da = _write_outputs(tmp_path / "a", "你好\n",
                        "结论行A\n时间: 2026-09-25 10:00:00\n")
    db = _write_outputs(tmp_path / "b", "你好\n",
                        "结论行B\n时间: 2026-09-25 10:00:00\n")
    res = hro2_gate.compare_outputs(da.parent, db.parent)
    assert not res["equal"]
    assert any(d["kind"] == "report_line" for d in res["differences"])


def test_compare_nested_missing_final_reports_missing(tmp_path):
    """嵌套布局下一侧缺失终稿必须报 missing（不得静默相等）。"""
    report = "质量报告\n时间: 2026-09-25 10:00:00\n"
    da = _write_outputs(tmp_path / "a", "你好\n", report)
    db = tmp_path / "b"
    db.mkdir()
    res = hro2_gate.compare_outputs(da.parent, db)
    assert not res["equal"]
    assert any(d["kind"] == "missing" for d in res["differences"])
