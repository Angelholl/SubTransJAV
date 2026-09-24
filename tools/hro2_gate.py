"""HRO-2 拆分验收门：pipeline_v2 重构前后产物逐字节一致性 harness
====================================================================

决策依据：D2026-0925-01（1.3.0 pipeline_v2 拆分的验收门 = HRO-2，
承接 D2026-0922-03 HRO-2 定义）。

验收门定义：同一组 golden 输入 + 固定 FakeClient + 每次全新空 TM +
固定词表，重构前后各跑一次，``*_final_cn.srt`` 逐字节 diff +
``*_质量报告.txt`` 归一化后 diff。

HRO-1 采纳条件的落点：
  ① 归一化白名单 = 质量报告.txt 中（a）头部第 3 行的时间戳
     （quality_report.py 头部行 ``时间: %Y-%m-%d %H:%M:%S``）与
     （b）TM 摘要行（``TM: 精确命中 X 条 | 本次学习入库 Y 条`` 或
     ``TM: 无样本``）。白名单以外任何差异 = FAIL。
  ② 每次跑各用独立新建空 TM 库：cfg.tm_db_path 指向本次全新路径
     （pipeline_support._init_tm 按该路径建库，天然为空）。

用法：
    python tools/hro2_gate.py capture --inputs DIR --out DIR [--glossary PATH] [--label NAME]
    python tools/hro2_gate.py compare --a DIR --b DIR [--report PATH]

capture：对 inputs 下 sorted 的 *.srt 逐个执行 v2 单文件管线（进程内
打桩 FakeClient / refine_tmp_dir / watch 路径），产物 sha256 记入
out/run_manifest.json。每次 capture 必须用全新 out 目录（严禁
force=True——会触发 _backup_existing_outputs 产生 _bak_ 文件污染比对）。

compare：按 run_manifest.json（缺省则按文件名）对齐两目录，全等退出
码 0，有差异 1，用法错误 2。

watch advice 有状态性结论：glossary_conflict.evaluate_watch 读取
default_watch_path()（全局 Temp/translation_memory/glossary_conflict_watch.json）
的**累计历史记录**做三态评估，且 pipeline_v2 每次运行都 append 新记录
（pipeline_v2.py 观察闸小节）——是跨运行有状态的。因此 capture 时必须
把 pv.default_watch_path 重定向到本次独立的临时 JSON 路径，否则第二次
跑的 watch advice 行会因历史记录数不同而污染归一化比对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 常量

REPORT_SUFFIX = "_质量报告.txt"
FINAL_SUFFIX = "_final_cn.srt"
MANIFEST_NAME = "run_manifest.json"
WATCH_JSON_NAME = "glossary_conflict_watch.json"

# HRO-1 白名单①：报告头部时间戳（仅替换时间值，其余文字保持原样）
_TS_RE = re.compile(r"时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
# HRO-1 白名单②：TM 摘要行（整行归一化）
_TM_PREFIX = "TM: "

_MAX_DIFF_SHOWN = 10
_TRUNC = 60


# ---------------------------------------------------------------- 纯函数

def normalize_report_line(line: str) -> str:
    """归一化单行报告。白名单恰好两条：
    1. 含 ``时间: YYYY-MM-DD HH:MM:SS`` 的行 → 时间值替换为 <归一化>；
    2. 以 ``TM: `` 开头的整行 → 整行替换为 ``TM: <归一化>``。
    其余行原样返回。"""
    if line.startswith(_TM_PREFIX):
        return "TM: <归一化>"
    return _TS_RE.sub("时间: <归一化>", line)


def normalize_report_text(text: str) -> str:
    """逐行归一化整份报告文本（保持行序）。"""
    return "\n".join(normalize_report_line(ln) for ln in text.splitlines())


def compare_outputs(dir_a, dir_b) -> dict:
    """比对两目录的产物。final_cn.srt 逐字节；质量报告.txt 归一化后逐行。

    返回 {"equal": bool, "differences": [...]}，differences 最多记录
    _MAX_DIFF_SHOWN 处，每处含 file/line/两侧内容截断。"""
    dir_a, dir_b = Path(dir_a), Path(dir_b)
    finals_a = {p.name: p for p in dir_a.glob(f"*{FINAL_SUFFIX}")}
    finals_b = {p.name: p for p in dir_b.glob(f"*{FINAL_SUFFIX}")}
    diffs: list[dict] = []

    for name in sorted(set(finals_a) | set(finals_b)):
        if name not in finals_a:
            diffs.append({"kind": "missing", "file": name,
                          "side": "a", "detail": "A 侧缺失终稿"})
            continue
        if name not in finals_b:
            diffs.append({"kind": "missing", "file": name,
                          "side": "b", "detail": "B 侧缺失终稿"})
            continue
        ba = finals_a[name].read_bytes()
        bb = finals_b[name].read_bytes()
        if ba != bb:
            diffs.append(_first_byte_diff(name, ba, bb))

        report_name = name[: -len(FINAL_SUFFIX)] + REPORT_SUFFIX
        ra, rb = dir_a / report_name, dir_b / report_name
        if not ra.is_file() or not rb.is_file():
            diffs.append({"kind": "missing", "file": report_name,
                          "side": "a" if not ra.is_file() else "b",
                          "detail": "缺失质量报告"})
            continue
        diffs.extend(_report_diffs(report_name, ra, rb))

    return {"equal": not diffs, "differences": diffs[:_MAX_DIFF_SHOWN]}


def _first_byte_diff(name: str, ba: bytes, bb: bytes) -> dict:
    i = next((k for k in range(min(len(ba), len(bb))) if ba[k] != bb[k]),
             min(len(ba), len(bb)))
    return {
        "kind": "final_bytes", "file": name, "line": None,
        "a": f"offset {i} (len={len(ba)})",
        "b": f"offset {i} (len={len(bb)})",
        "detail": "final_cn.srt 逐字节不一致",
    }


def _report_diffs(name: str, ra: Path, rb: Path) -> list[dict]:
    la = normalize_report_text(ra.read_text(encoding="utf-8")).splitlines()
    lb = normalize_report_text(rb.read_text(encoding="utf-8")).splitlines()
    out = []
    for i in range(max(len(la), len(lb))):
        va = la[i] if i < len(la) else "<缺行>"
        vb = lb[i] if i < len(lb) else "<缺行>"
        if va != vb:
            out.append({
                "kind": "report_line", "file": name, "line": i + 1,
                "a": va[:_TRUNC], "b": vb[:_TRUNC],
                "detail": "质量报告归一化后仍有差异（白名单外）",
            })
            if len(out) >= _MAX_DIFF_SHOWN:
                break
    return out


# ---------------------------------------------------------------- 工具

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _sha1_file(path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            check=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- 打桩

class FakeClient:
    """按 index 脚本化返回译文的假客户端（与 tests/test_pipeline_v2.py
    FakeClient 同构：确定性输出，阶段A 译{i}、阶段B 审{i}）。"""

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
        from subtransjav.translate.llm_client import BatchResult
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


def _make_stubs(work_dir: Path, watch_json: Path) -> dict:
    """返回 {module: {attr: 原值}} 的打桩表（调用方 try/finally 恢复）。"""
    from subtransjav.refine import pipeline_v2 as pv
    work_dir.mkdir(parents=True, exist_ok=True)
    watch_json.parent.mkdir(parents=True, exist_ok=True)

    def _fake_tmp(p, s):
        return str(work_dir)

    stubs = {
        pv: {
            "_make_client": pv._make_client,
            "refine_tmp_dir": pv.refine_tmp_dir,
            "default_watch_path": pv.default_watch_path,
        }
    }
    pv._make_client = lambda cfg, tag: FakeClient()
    pv.refine_tmp_dir = _fake_tmp
    pv.default_watch_path = lambda: str(watch_json)   # watch 有状态 → 每跑独立
    return stubs


def _restore_stubs(stubs: dict) -> None:
    for module, attrs in stubs.items():
        for name, orig in attrs.items():
            setattr(module, name, orig)


# ---------------------------------------------------------------- capture

def _build_cfg(in_path: Path, run_dir: Path, glossary_path: str):
    from subtransjav.refine.config import RefineConfig, StageConfig
    cfg = RefineConfig(
        inputs=[str(in_path)],
        output_dir=str(run_dir),
        tm_enabled=True,
        tm_db_path=str(run_dir / "tm" / "tm.db"),   # HRO-1②：全新空 TM
        glossary_path=glossary_path,
        auto_synopsis=False,
        v2_concurrency=1,
        force=False,     # 严禁 force：避免 _bak_ 备份污染比对
    )
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "fake-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "lmstudio", "fake-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    return cfg


def _capture_one(in_path: Path, out_root: Path, glossary_path: str) -> dict:
    from subtransjav.refine import pipeline_v2 as pv
    stem = in_path.stem
    run_dir = out_root / stem
    if run_dir.exists():
        raise SystemExit(f"capture 输出目录非全新，已存在：{run_dir}")
    run_dir.mkdir(parents=True)

    stubs = _make_stubs(run_dir / "work", run_dir / "watch" / WATCH_JSON_NAME)
    try:
        cfg = _build_cfg(in_path, run_dir, glossary_path)
        cfg.validate()
        pv._run_single_v2(cfg, str(in_path))
    finally:
        _restore_stubs(stubs)

    final = run_dir / f"{stem}{FINAL_SUFFIX}"
    report = run_dir / f"{stem}{REPORT_SUFFIX}"
    return {
        "input": str(in_path),
        "input_sha256": _sha256_file(in_path),
        "final": final.name,
        "final_sha256": _sha256_file(final) if final.is_file() else None,
        "report": report.name,
        "report_sha256": _sha256_file(report) if report.is_file() else None,
    }


def cmd_capture(args) -> int:
    inputs_dir = Path(args.inputs)
    out_dir = Path(args.out)
    if not inputs_dir.is_dir():
        print(f"[hro2-gate] 输入目录不存在：{inputs_dir}")
        return 2
    if out_dir.exists():
        print(f"[hro2-gate] 输出目录必须为全新路径，已存在：{out_dir}")
        return 2
    out_dir.mkdir(parents=True)

    glossary = args.glossary or "config/glossary.csv"
    if not Path(glossary).is_file():
        print(f"[hro2-gate] 词表文件不存在：{glossary}")
        return 2

    files = sorted(inputs_dir.glob("*.srt"))
    if not files:
        print(f"[hro2-gate] 输入目录无 *.srt：{inputs_dir}")
        return 2

    manifest = {
        "label": args.label,
        "captured_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "git_head": _git_head(),
        "glossary": glossary,
        "glossary_sha1": _sha1_file(glossary),
        "config_fingerprint": {
            "v2_ctx_local": 22272,
            "v2_concurrency": 1,
            "tm_enabled": True,
            "auto_synopsis": False,
            "glossary_path": glossary,
        },
        "files": {},
    }
    for in_path in files:
        print(f"[hro2-gate] capture: {in_path.name}")
        manifest["files"][in_path.stem] = _capture_one(
            in_path, out_dir, glossary)

    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[hro2-gate] manifest 已写入：{out_dir / MANIFEST_NAME}")
    return 0


# ---------------------------------------------------------------- compare

def cmd_compare(args) -> int:
    dir_a, dir_b = Path(args.a), Path(args.b)
    if not dir_a.is_dir() or not dir_b.is_dir():
        print(f"[hro2-gate] 目录不存在：{dir_a} / {dir_b}")
        return 2

    result = compare_outputs(dir_a, dir_b)

    ma, mb = dir_a / MANIFEST_NAME, dir_b / MANIFEST_NAME
    manifest_aligned = ma.is_file() and mb.is_file()
    if manifest_aligned:
        sa = json.loads(ma.read_text(encoding="utf-8"))
        sb = json.loads(mb.read_text(encoding="utf-8"))
        result["label_a"] = sa.get("label")
        result["label_b"] = sb.get("label")
        result["glossary_match"] = (
            sa.get("glossary_sha1") == sb.get("glossary_sha1"))
        if not result["glossary_match"]:
            result["equal"] = False
            result["differences"].append({
                "kind": "manifest", "file": MANIFEST_NAME, "line": None,
                "a": sa.get("glossary"), "b": sb.get("glossary"),
                "detail": "两次 capture 词表 sha1 不一致",
            })

    if args.report:
        Path(args.report).write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8")

    if result["equal"]:
        print("[hro2-gate] PASS：重构前后产物全等（final 逐字节 / 报告归一化后）")
        return 0
    print(f"[hro2-gate] FAIL：共 {len(result['differences'])} 处差异：")
    for d in result["differences"]:
        print(f"  - [{d['kind']}] {d['file']}:{d.get('line')}")
        print(f"    A: {d.get('a')}")
        print(f"    B: {d.get('b')}")
    return 1


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hro2_gate",
        description="HRO-2 拆分验收门：pipeline_v2 重构前后产物一致性比对")
    sub = ap.add_subparsers(dest="command", required=True)

    ap_cap = sub.add_parser("capture", help="跑一遍 golden 输入并记录产物指纹")
    ap_cap.add_argument("--inputs", required=True, help="golden 输入目录（*.srt）")
    ap_cap.add_argument("--out", required=True, help="输出目录（必须为全新路径）")
    ap_cap.add_argument("--glossary", default=None, help="词表路径（缺省 config/glossary.csv）")
    ap_cap.add_argument("--label", default="", help="本次 capture 标签（如 pre-refactor）")
    ap_cap.set_defaults(func=cmd_capture)

    ap_cmp = sub.add_parser("compare", help="比对两次 capture 的产物目录")
    ap_cmp.add_argument("--a", required=True, help="A 侧目录（重构前）")
    ap_cmp.add_argument("--b", required=True, help="B 侧目录（重构后）")
    ap_cmp.add_argument("--report", default=None, help="结果 JSON 输出路径")
    ap_cmp.set_defaults(func=cmd_compare)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
