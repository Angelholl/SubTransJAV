"""tests for tools/e3_benchmark.py（E3 基准解析器，批次4-A2）"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from e3_benchmark import parse_log_files, summarize  # noqa: E402


def _write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _stage_a_lines():
    return [
        "[00:36:58] [STAGE] 阶段A 净语+翻译",
        "[00:36:59] [llm] 共 100 条，分 4 批（并发=1）",
        "[00:37:09]    ⏳ 批次 1/4",
        "[00:37:19]    ⏳ 批次 2/4",
        "[00:37:29]    ⏳ 批次 3/4",
        "[00:37:39]    ⏳ 批次 4/4",
    ]


def test_run_header_and_batch_durations(tmp_path):
    p = _write(tmp_path, "a.txt", _stage_a_lines())
    runs = parse_log_files([p])
    assert len(runs) == 1
    r = runs[0]
    assert r["stage"] == "A" and r["stage_source"] == "marker"
    assert (r["entries"], r["n_batches"], r["concurrency"]) == (100, 4, 1)
    assert r["seen_batches"] == 4
    assert r["batch_durs"] == [10, 10, 10, 10]
    result = summarize(runs)
    bd = result["stages"]["A"]["batch_durations"]
    assert bd["n"] == 4 and bd["min"] == 10 and bd["total"] == 40
    assert result["stages"]["A"]["duration_sec"] == 40  # 00:36:59 -> 00:37:39
    assert result["total_duration_sec"] == 40


def test_markerless_two_runs_inferred_stages(tmp_path):
    lines = _stage_a_lines()
    lines += [  # 无 [STAGE] 标记的第二段运行头
        "[00:38:00] [llm] 共 80 条，分 2 批（并发=1）",
        "[00:38:10]    ⏳ 批次 1/2",
        "[00:38:25]    ⏳ 批次 2/2",
    ]
    p = _write(tmp_path, "b.txt", lines)
    runs = parse_log_files([p])
    assert [r["stage"] for r in runs] == ["A", "B"]
    assert [r["stage_source"] for r in runs] == ["marker", "inferred"]
    result = summarize(runs)
    assert set(result["stages"]) == {"A", "B"}
    assert result["stages"]["B"]["batch_durations"] == {"n": 2, "min": 10, "median": 12.5, "mean": 12.5, "total": 25}
    assert result["total_duration_sec"] == 86  # 00:36:59 -> 00:38:25


def test_midnight_rollover(tmp_path):
    lines = [
        "[23:59:00] [STAGE] 阶段A 净语+翻译",
        "[23:59:10] [llm] 共 10 条，分 2 批（并发=1）",
        "[23:59:40]    ⏳ 批次 1/2",
        "[00:00:10]    ⏳ 批次 2/2",
    ]
    p = _write(tmp_path, "c.txt", lines)
    runs = parse_log_files([p])
    r = runs[0]
    assert r["batch_durs"] == [30, 30]  # 23:59:40 -> 00:00:10 跨零点 = 30s
    assert r["end"] - r["start"] == 60


def test_incomplete_run_crash(tmp_path):
    lines = _stage_a_lines()[:3]  # 运行头后只有 1 个心跳（崩溃）
    p = _write(tmp_path, "d.txt", lines)
    result = summarize(parse_log_files([p]))
    run = result["stages"]["A"]["runs"][0]
    assert run["complete"] is False
    assert run["seen_batches"] == 1
    assert run["n_batches"] == 4


def test_multi_file_stitch(tmp_path):
    p1 = _write(tmp_path, "9-23.txt", _stage_a_lines())
    p2 = _write(
        tmp_path,
        "9-23-1501.txt",
        [
            "[01:38:00] [STAGE] 阶段B 审校+抛光",
            "[01:38:01] [llm] 共 99 条，分 3 批（并发=1）",
            "[01:38:31]    ⏳ 批次 1/3",
            "[01:39:01]    ⏳ 批次 2/3",
            "[01:39:31]    ⏳ 批次 3/3",
        ],
    )
    runs = parse_log_files([p1, p2])
    assert len(runs) == 2
    result = summarize(runs)
    assert set(result["stages"]) == {"A", "B"}
    assert result["stages"]["B"]["batch_durations"]["total"] == 90
    assert result["stages"]["B"]["runs"][0]["files"] == ["9-23-1501.txt"]
    assert result["total_duration_sec"] == 3752  # 00:36:59 -> 01:39:31


def test_file_without_runs_skipped(tmp_path):
    p = _write(tmp_path, "gui.txt", ["[00:00:01] gui 启动", "[00:00:02] 无批次行"])
    runs = parse_log_files([p])
    assert runs == []
    p2 = _write(tmp_path, "ok.txt", _stage_a_lines())
    runs = parse_log_files([p, p2])
    assert len(runs) == 1


def test_400_failure_line_ignored(tmp_path):
    lines = _stage_a_lines()
    lines.insert(4, "[00:37:12] [llm] 批次失败（缺行走定向重试）: LLM 调用失败: Error code: 400 - {'error': 'x'}")
    p = _write(tmp_path, "e.txt", lines)
    runs = parse_log_files([p])
    assert runs[0]["batch_durs"] == [10, 10, 10, 10]
