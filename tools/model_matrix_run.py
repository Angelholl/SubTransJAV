#!/usr/bin/env python3
"""5 本地模型 × A/B 两阶段全搭配无人值守跑批器（仅标准库，不联网）。

管线事实（已对照源码核实，文件:行号）：
1) -o 路由：全部产物落 -o 目录 —— {stem}_refine_A.srt / {stem}_final_cn.srt
   (pipeline_v2.py:1600-1601)、{stem}_质量报告.txt (quality_report.py:853，经
   pipeline_v2.py:2060 以 out_dir 调用)、{stem}_分歧复核.csv (pipeline_v2.py:2064)、
   {stem}_术语冲突观察.csv (pipeline_v2.py:2019-2020)、{stem}_manifest.json
   (pipeline_v2.py:1733; manifest.py:119-121)。
2) 退出码：0=成功、3=有降级/风险警戒（都算成功）、1=异常、130=中断 (cli.py:368-381)。
3) manifest：stages.A/.B/.final 的 StageRecord 只有 status/completed_at/output/
   entries/degraded_count，无模型名 (manifest.py:34-41)；模型名在顶层
   models.A.model (pipeline_v2.py:292-302; manifest.py:55-56)；输入指纹
   input_sha1 (manifest.py:51)。
4) 阶段A复用条件 (pipeline_v2.py:1740-1743)：--resume + 清单可信 + A status∈
   (done,degraded) + {stem}_refine_A.srt 存在 + final 未 done；成功收尾时
   delete_resume_artifacts 删 manifest/refine_A (pipeline_v2.py:2085)。复用日志
   标志“阶段A产物复用” (1746)；阶段A开始标志 “[STAGE] 阶段A 净语+翻译” (937)。
5) synopsis 缓存：键=sha1("v1\\0{provider}/{model}\\0{采样文本}")
   (synopsis.py:28,149-157)，文件名 {key}.txt，目录 Temp/synopsis_cache
   (synopsis.py:31,207; config.py:111)；调用时点在复用判定之前
   (pipeline_v2.py:1717，用阶段A provider/model, 607-611)。键虽含模型，但精确
   文件名需 gate0+预合并后的采样文本（管线运行期内部状态），工具侧无法复算 →
   按预案采用“目录快照比对”兜底：种子对成功时把 synopsis_cache 文件名集合快照
   存入 .done；B≠A 对预检=该集合仍全部存在（同一文件采样文本确定性一致，种子
   摘要缓存文件即复用对将命中的键）；复用对运行前后再快照比对，有新增/丢失 →
   suspect_synopsis。

用法：--plan | --queue [--combos a>b,..] [--file 子串] |
      --validate-diff --model <key> --file 子串 | --pilot |
      --mida-plan | --mida-queue（第二轮 mida-559：TM 启用全新模拟，4×4 全序对）
"""
import argparse
import contextlib
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3  # 第二轮 mida-559 段使用（tm.db 清零）；标准库导入无副作用，安全上移
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib import request as _urlreq

for _s in (sys.stdout, sys.stderr):                     # 自身输出强制 UTF-8
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")

MODELS = {  # key -> LM Studio 模型 id（/v1/models 已确认）
    "trans8b":    "translate-ja-zh-qwen3-8b",
    "sakura14b":  "sakura-14b-qwen3-v1.5",
    "joyfox27b":  "qwen3.8-27b-uncensored-joyfox-aggressive",
    "heretic35b": "qwen3.6-35b-a3b-uncensored-heretic-apex",
    "hauhau35b":  "qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive",
}
GROUP_ORDER = ["trans8b", "sakura14b", "heretic35b", "hauhau35b", "joyfox27b"]
INPUT_DIR = r"E:\无字幕\新建文件夹\三次测试"      # 只读
MATRIX_DIR = r"E:\无字幕\新建文件夹\三次测试\AB矩阵"
REPO = r"D:\SubTransJAV"
REFINE = REPO + r"\.venv\Scripts\subtransjav-refine.exe"
ENDPOINT = "http://localhost:1234/v1"

ACACHE = Path(MATRIX_DIR) / "_Acache"                # 种子截获库
ACACHE_V = Path(MATRIX_DIR) / "_Acache_validate"     # diff 验收截获库
LOGS = Path(MATRIX_DIR) / "_logs"
SUMMARY = Path(MATRIX_DIR) / "_汇总"
STATUS_CSV = SUMMARY / "matrix_status.csv"
VALIDATE_TMP = Path(MATRIX_DIR) / "_validate_tmp"
SYNOPSIS_DIR = Path(REPO) / "Temp" / "synopsis_cache"
GLOSSARY_CSV = Path(REPO) / "config" / "glossary.csv"
GLOSSARY_LEARNED = Path(REPO) / "config" / "glossary_learned.csv"

CSV_COLS = ["ts", "combo", "stem", "a", "b", "mode", "exit", "elapsed_s",
            "untrans", "result", "note"]
SEED_TIMEOUT_H = {"trans8b": 1.5, "sakura14b": 2.5, "joyfox27b": 4.0,
                  "heretic35b": 2.5, "hauhau35b": 2.5}   # 按 b 模型，复用对取 60%
REUSE_FACTOR = 0.6
UNTRANS_PREFIX = "[未翻译]"
MIN_FREE = 10 * 1024 ** 3                            # 10GB
PING_TRIES, PING_INTERVAL = 30, 10
_BASELINE_GLOSSARY = None                            # 队列启动时的词库指纹


def log(msg):
    print(f"[MATRIX] {datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha1_file(path):
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def atomic_write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".matrix.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def atomic_copy(src, dst):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".matrix.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def append_csv(row):
    SUMMARY.mkdir(parents=True, exist_ok=True)
    new = not STATUS_CSV.exists()
    with open(STATUS_CSV, "a", encoding="utf-8", newline="") as f:
        if new:
            f.write("\ufeff")                        # BOM（Excel 友好）
        w = csv.writer(f)
        if new:
            w.writerow(CSV_COLS)
        w.writerow([row.get(c, "") for c in CSV_COLS])


def discover_files(substr=""):
    files = sorted(Path(INPUT_DIR).glob("*.ja.merged.whisperjav.srt"))
    return [f for f in files if substr in f.name] if substr else files


def stem_of(fpath):
    return fpath.name[:-len(".srt")]                 # 与 strip_lang_suffix 一致


def combo_dir(a, b):
    return Path(MATRIX_DIR) / f"{a}__{b}"


def unit_done(cdir, stem):
    return ((cdir / f"{stem}_final_cn.srt").is_file()
            and (cdir / f"{stem}_质量报告.txt").is_file())


# --- lms CLI 与模型管理 ------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _run_lms(args, timeout=180):
    exe = shutil.which("lms") or "lms"
    try:
        p = subprocess.run([exe, *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"lms {' '.join(args)} error: {e}"
    return p.returncode, ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")


def lms_ls_keys():
    """lms ls 输出解析（ANSI 色码容错）：每行首个 token 为模型 key。"""
    _, out = _run_lms(["ls"])
    keys = set()
    for line in out.splitlines():
        line = _ANSI_RE.sub("", line).strip()
        if not line or line.startswith(("LLM", "EMBEDDING", "PARAMS", "You have")):
            continue
        tok = line.split()[0]
        if re.fullmatch(r"[\w.\-/]+", tok):
            keys.add(tok)
    return keys


def loaded_model_ids():
    """lms ps：当前已载的（已知）模型 id 集合。"""
    _, out = _run_lms(["ps"])
    if "No models are currently loaded" in out:
        return set()
    return {mid for mid in MODELS.values() if mid in _ANSI_RE.sub("", out)}


def model_key_for(mid):
    """model_id -> lms load 用的 key：先精确，再发布者/模型名变体。"""
    keys = lms_ls_keys()
    if mid in keys:
        return mid
    for k in keys:                                   # publisher/model 变体
        if k.endswith("/" + mid) or k.split("/")[-1] == mid:
            return k
    return next((k for k in keys if mid in k), None)


def ping_model(mid):
    body = json.dumps({"model": mid,
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 1}).encode("utf-8")
    for _ in range(PING_TRIES):
        try:
            req = _urlreq.Request(ENDPOINT + "/chat/completions", data=body,
                                  headers={"Content-Type": "application/json"})
            with _urlreq.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(PING_INTERVAL)
    return False


def ensure_only_model(b_id):
    """运行前必须只有 b 模型在载：unload --all + lms load <key> -y + ping。"""
    if loaded_model_ids() == {b_id}:
        return True, "already-loaded"
    _run_lms(["unload", "--all"], timeout=600)
    key = model_key_for(b_id)
    cands, seen = [], set()
    for c in (key, b_id):                            # 去重保序
        if c and c not in seen:
            seen.add(c)
            cands.append(c)
    note = ""
    for cand in cands:
        rc, out = _run_lms(["load", cand, "-y"], timeout=1800)
        if rc == 0:
            return (True, f"loaded:{cand}") if ping_model(b_id) else \
                   (False, f"ping-failed:{b_id}")
        note = _ANSI_RE.sub("", out).strip()[-200:]
    return False, f"load-failed:{note}"


# --- manifest / 种子 / synopsis / 看门狗 -------------------------------------

def manifest_stage_a_ok(d, a_id, input_sha1):
    """种子清单校验：A status∈{done,degraded}、final≠done、（models 字段存在时）
    models.A.model==a_id，否则回退校验 input_sha1（schema 见文件头 3)。"""
    st = d.get("stages") or {}
    a_st = (st.get("A") or {}).get("status")
    if a_st not in ("done", "degraded"):
        return False, f"A-status={a_st}"
    if (st.get("final") or {}).get("status") == "done":
        return False, "final-already-done"
    a_models = (d.get("models") or {}).get("A") or {}
    if "model" in a_models:
        return (True, "ok") if a_models.get("model") == a_id else \
               (False, f"model-mismatch:{a_models.get('model')}")
    if d.get("input_sha1") != input_sha1:
        return False, "input-sha1-mismatch"
    return True, "ok"


def intercept_once(cdir, cache, stem, a_id, input_sha1, copy=True):
    """组合目录里若已出现“A 完成且 final 未完”的清单，原子复制种子两件套。
    copy=False 只探测不落盘（多进程并发轮询时避免同名 .tmp 写竞态）。"""
    man = Path(cdir) / f"{stem}_manifest.json"
    srt = Path(cdir) / f"{stem}_refine_A.srt"
    d = read_json(man) if (man.is_file() and srt.is_file()) else None
    if not d:
        return False
    ok, _ = manifest_stage_a_ok(d, a_id, input_sha1)
    if ok and copy:
        atomic_copy(srt, Path(cache) / f"{stem}_refine_A.srt")
        atomic_copy(man, Path(cache) / f"{stem}_manifest.json")
    return ok


def seed_ready(akey, a_id, stem, input_sha1):
    """B≠A 对的“种子齐备”检查（核心不变式 2）。"""
    cache = ACACHE / akey
    if not ((cache / f"{stem}_refine_A.srt").is_file()
            and (cache / f"{stem}_manifest.json").is_file()):
        return False, "seed-cache-missing"
    d = read_json(cache / f"{stem}_manifest.json")
    return manifest_stage_a_ok(d, a_id, input_sha1) if d else (False, "seed-manifest-unreadable")


def synopsis_snapshot():
    if not SYNOPSIS_DIR.is_dir():
        return []
    return sorted(p.name for p in SYNOPSIS_DIR.iterdir() if p.is_file())


def synopsis_precheck(akey, stem):
    """B≠A 对运行前的 synopsis 预检（目录快照兜底，见文件头 5）：
    种子 .done 快照集合仍全部存在→pass；无快照（rebased）→unknown（放行，
    靠运行期看门狗）；有丢失→fail（转 deferred）。"""
    d = read_json(combo_dir(akey, akey) / f"{stem}.done.json") or {}
    files = d.get("synopsis_files")
    if files is None:
        return "unknown", "no-seed-synopsis-snapshot"
    missing = [x for x in files if x not in set(synopsis_snapshot())]
    return ("fail", f"synopsis-cache-lost:{len(missing)}") if missing else \
           ("pass", f"snapshot-ok:{len(files)}")


def glossary_hashes():
    return (sha1_file(GLOSSARY_CSV), sha1_file(GLOSSARY_LEARNED))


def check_glossary():
    """词库看门狗：与队列启动基线不一致 → 立即中止整个跑批器。"""
    if _BASELINE_GLOSSARY is not None:
        cur = glossary_hashes()
        if cur != _BASELINE_GLOSSARY:
            raise SystemExit(f"[MATRIX][FATAL] glossary 在跑批期间变化 {cur} != "
                             f"{_BASELINE_GLOSSARY}，立即中止（人工确认后重跑）。")


def disk_ok():
    bad = [(d, shutil.disk_usage(d).free) for d in ("D:\\", "E:\\")
           if shutil.disk_usage(d).free < MIN_FREE]
    if bad:
        raise SystemExit(f"[MATRIX][FATAL] 磁盘剩余空间不足 10GB: {bad}")


def count_untrans(final_path):
    with open(final_path, encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f if line.startswith(UNTRANS_PREFIX))


def clean_stem_artifacts(cdir, stem):
    """重试前只清该 stem 产物（final_cn/质量报告/manifest/refine_A/两个csv/.done）。"""
    for suf in ("_final_cn.srt", "_质量报告.txt", "_manifest.json",
                "_refine_A.srt", "_分歧复核.csv", "_术语冲突观察.csv", ".done.json"):
        with contextlib.suppress(OSError):
            (Path(cdir) / f"{stem}{suf}").unlink()


def kill_tree(pid):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


# --- 核心：单次管线子进程（tee 日志 + 环形缓冲 + 看门狗 + 种子截获） ----------

def build_cmd(a_id, b_id, fpath, out_dir):
    return [REFINE, "-i", str(fpath), "-o", str(out_dir), "--profile", "local",
            "--s1-provider", "lmstudio", "--s1-model", a_id,
            "--s3-provider", "lmstudio", "--s3-model", b_id,
            "--no-tm", "--lmstudio-endpoint", ENDPOINT,
            "--resume", "--force-resume"]


def _pump(proc, logf, buf, marks):
    """逐行读子进程 stdout（utf-8, errors=replace）：tee 日志 + deque(2000)。"""
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        logf.write(line + "\n")
        logf.flush()
        buf.append(line)
        if "阶段A产物复用" in line:
            marks["reuse"] = True
        elif line.startswith("[STAGE]"):
            marks["stage_a"] = marks["stage_a"] or ("阶段A" in line)
            marks["stage_b"] = marks["stage_b"] or ("阶段B" in line)
        elif "阶段A完成" in line:
            marks["a_done_log"] = True


def run_pipeline(a_id, b_id, fpath, out_dir, log_path, timeout_s,
                 watch=None, kill_when_a_done=False):
    """跑一次单文件管线子进程。watch=(cdir,cache,stem,a_id,sha1) 时启用种子截获
    （后台线程 0.5s 轮询；进程结束后再补漏一次）。返回状态 dict。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    out_dir, log_path = Path(out_dir), Path(log_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    marks = {"reuse": False, "stage_a": False, "stage_b": False}
    buf, stop, capt = deque(maxlen=2000), threading.Event(), {"n": 0}
    if watch:
        cdir, cache, stem, wa_id, sha1 = watch

        def _watch():
            while not stop.is_set():
                if intercept_once(cdir, cache, stem, wa_id, sha1):
                    capt["n"] += 1
                    return
                stop.wait(0.5)
        wt = threading.Thread(target=_watch, daemon=True)
        wt.start()
    # noqa: SIM115 —— 日志句柄需跨进程生命周期长驻：传给 _pump 线程写 tee 日志，
    # 只能在子进程结束后（下方 finally）统一 close，无法用 with 块包裹。
    logf = open(log_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    t0 = time.time()
    proc = subprocess.Popen(build_cmd(a_id, b_id, fpath, out_dir), cwd=REPO,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    pt = threading.Thread(target=_pump, args=(proc, logf, buf, marks), daemon=True)
    pt.start()
    deadline, timed_out, killed = t0 + timeout_s, False, False
    while proc.poll() is None:
        if kill_when_a_done and not killed and marks["stage_a"] and capt["n"]:
            killed = True                            # 阶段A两件套已落盘 → 可中断
            kill_tree(proc.pid)
            break
        if time.time() > deadline:
            timed_out = True
            log(f"看门狗超时({timeout_s}s) kill pid={proc.pid}")
            kill_tree(proc.pid)
            break
        time.sleep(2)
    rc = proc.wait()
    stop.set()
    if watch:
        wt.join(3)
        if intercept_once(*watch):                   # 结束后补漏一次
            capt["n"] += 1
    pt.join(10)
    try:
        proc.stdout.close()
    finally:
        logf.close()
    return {"rc": rc, "timed_out": timed_out, "elapsed": time.time() - t0,
            "marks": marks, "captured": capt["n"], "buf": buf}


def assess_run(res, cdir, stem, mode, syn0=None):
    """成功判据（核心不变式 5/6）：rc∈{0,3} 且 final+报告齐备；复用对另做
    synopsis 快照比对（新增/丢失 → suspect_synopsis 并回滚新增缓存文件）。"""
    if res["timed_out"]:
        return "failed", f"soft-timeout:{int(res['elapsed'])}s"
    final_p = Path(cdir) / f"{stem}_final_cn.srt"
    rep_p = Path(cdir) / f"{stem}_质量报告.txt"
    if res["rc"] not in (0, 3) or not (final_p.is_file() and rep_p.is_file()):
        return "failed", f"rc={res['rc']} final={final_p.is_file()} report={rep_p.is_file()}"
    if mode == "reuse":
        s1 = synopsis_snapshot()
        new, lost = set(s1) - set(syn0 or []), set(syn0 or []) - set(s1)
        if new or lost:
            for name in new:                         # 回滚本次新增，保持零污染
                with contextlib.suppress(OSError):
                    (SYNOPSIS_DIR / name).unlink()
            return "suspect_synopsis", f"synopsis-delta:new={len(new)},lost={len(lost)}"
    return "done", f"rc={res['rc']},reuse={res['marks']['reuse']}"


# --- 工作单元（combo × 文件）：模型管理 + 重试 1 次 + .done + CSV -------------

def process_unit(a, b, fpath, total=None, idx=None):
    a_id, b_id = MODELS[a], MODELS[b]
    stem, mode = stem_of(fpath), ("seed" if a == b else "reuse")
    cdir = combo_dir(a, b)
    cdir.mkdir(parents=True, exist_ok=True)
    input_sha1 = sha1_file(fpath)
    timeout_s = int(SEED_TIMEOUT_H[b] * 3600 * (1.0 if mode == "seed" else REUSE_FACTOR))
    if mode == "reuse":                              # 种子齐备才允许运行
        ok, why = seed_ready(a, a_id, stem, input_sha1)
        if not ok:
            return {"result": "deferred", "note": why}
        syn_ok, syn_note = synopsis_precheck(a, stem)
        if syn_ok == "fail":
            return {"result": "deferred", "note": syn_note}
        log(f"({idx}/{total}) {a}>{b} {stem} synopsis预检={syn_ok}({syn_note})")
    check_glossary()

    result, note, info = "failed", "", {}
    for attempt in (1, 2):
        if attempt == 2:
            clean_stem_artifacts(cdir, stem)
        if mode == "reuse":
            # 种子两件套拷回组合目录：管线复用条件按 out_dir 找 refine_A+manifest
            # （pipeline_v2.py:1740-1743），不拷回则阶段A会以 a 模型 id 从零跑
            # 而载入的是 b 模型（零换模破约）。
            _c = ACACHE / a
            atomic_copy(_c / f"{stem}_refine_A.srt", cdir / f"{stem}_refine_A.srt")
            atomic_copy(_c / f"{stem}_manifest.json", cdir / f"{stem}_manifest.json")
        mok, mnote = ensure_only_model(b_id)
        if not mok:
            result, note = "failed", f"model-mgmt:{mnote}"
            log(f"({idx}/{total}) {a}>{b} {stem} 尝试{attempt} 模型管理失败: {mnote}")
            continue
        syn0 = synopsis_snapshot() if mode == "reuse" else None
        watch = (cdir, ACACHE / a, stem, a_id, input_sha1) if mode == "seed" else None
        log(f"({idx}/{total}) {a}>{b} {stem} 尝试{attempt} 启动 mode={mode} timeout={timeout_s}s")
        res = run_pipeline(a_id, b_id, fpath, cdir, LOGS / f"{a}__{b}__{stem}.log",
                           timeout_s, watch=watch)
        result, note = assess_run(res, cdir, stem, mode, syn0)
        info = {"rc": res["rc"], "elapsed": res["elapsed"],
                "reuse": res["marks"]["reuse"], "untrans": None, "captured": res["captured"]}
        if result == "done":
            info["untrans"] = count_untrans(cdir / f"{stem}_final_cn.srt")
            atomic_write_json(cdir / f"{stem}.done.json", {
                "combo": f"{a}>{b}", "stem": stem, "a": a, "b": b, "exit": res["rc"],
                "elapsed": round(res["elapsed"], 1), "untrans": info["untrans"],
                "mode": mode, "ts": now_ts(),
                "synopsis_files": synopsis_snapshot() if mode == "seed" else None})
            break
        log(f"({idx}/{total}) {a}>{b} {stem} 尝试{attempt} {result}: {note}")
    prog = sum(1 for x in GROUP_ORDER for y in GROUP_ORDER for fs in discover_files()
               if unit_done(combo_dir(x, y), stem_of(fs)))
    log(f"({idx}/{total}) {a}>{b} {stem} => {result} (elapsed={info.get('elapsed', 0):.0f}s "
        f"untrans={info.get('untrans')} 完成进度≈{prog}/{total})")
    return dict(info, result=result, note=note)


def csv_row(a, b, fpath, r):
    return {"ts": now_ts(), "combo": f"{a}>{b}", "stem": stem_of(fpath), "a": a,
            "b": b, "mode": r.get("mode") or ("seed" if a == b else "reuse"),
            "exit": r.get("rc", ""), "elapsed_s": round(r.get("elapsed", 0) or 0, 1),
            "untrans": r.get("untrans") if r.get("untrans") is not None else "",
            "result": r.get("result", ""), "note": r.get("note", "")}


def run_unit(a, b, fpath, total=None, idx=None):
    """process_unit + CSV 落行（deferred 不落行，交给队尾轮/另记 blocked）。"""
    r = process_unit(a, b, fpath, total=total, idx=idx)
    if r["result"] != "deferred":
        append_csv(csv_row(a, b, fpath, r))
    return r


# --- --plan / --queue --------------------------------------------------------

def parse_combos(text):
    out = []
    for part in filter(None, (x.strip() for x in (text or "").split(","))):
        a, _, b = part.partition(">")
        if a not in MODELS or b not in MODELS:
            raise SystemExit(f"[MATRIX][FATAL] --combos 非法: {part}（key 需在 MODELS）")
        out.append((a, b))
    return out


def cmd_plan(combos_filter, file_filter):
    files = discover_files(file_filter)
    if not files:
        log(f"输入目录无匹配文件: {INPUT_DIR} (filter={file_filter!r})")
        return 2
    combos = combos_filter or [(a, b) for a in GROUP_ORDER for b in GROUP_ORDER]
    log(f"计划：{len(combos)} 组合 × {len(files)} 文件 = {len(combos) * len(files)} 单元")
    log(f"输入：{INPUT_DIR}")
    for f in files:
        log(f"  输入文件: {f.name}")
    done = 0
    for a, b in combos:
        for f in files:
            stem, cdir = stem_of(f), combo_dir(a, b)
            if unit_done(cdir, stem):
                st, done = "done", done + 1
            elif a == b:
                st = "pending"
            else:                                    # 附带种子库状态（只读检查）
                ok, why = seed_ready(a, MODELS[a], stem, sha1_file(f))
                st = f"pending(种子:{'ok' if ok else why})"
            print(f"[MATRIX]   {a}>{b}  {stem}: {st}")
    log(f"汇总：done={done} pending={len(combos) * len(files) - done}")
    return 0


def recovery_scan(units):
    """日志中断恢复：final+报告齐备即视为 done（补写 .done 与 CSV，mode=rebased）。"""
    n = 0
    for a, b, f in units:
        stem, cdir = stem_of(f), combo_dir(a, b)
        if not (unit_done(cdir, stem) and not (cdir / f"{stem}.done.json").is_file()):
            continue
        un = count_untrans(cdir / f"{stem}_final_cn.srt")
        atomic_write_json(cdir / f"{stem}.done.json", {
            "combo": f"{a}>{b}", "stem": stem, "a": a, "b": b, "exit": "recovered",
            "elapsed": "", "untrans": un, "mode": "rebased", "ts": now_ts(),
            "synopsis_files": None})
        append_csv({"ts": now_ts(), "combo": f"{a}>{b}", "stem": stem, "a": a, "b": b,
                    "mode": "rebased", "exit": "recovered", "elapsed_s": "",
                    "untrans": un, "result": "done", "note": "recovery-scan"})
        n += 1
    if n:
        log(f"中断恢复扫描：补记 {n} 个已存在产物的工作单元")
    return n


def cmd_queue(combos_filter, file_filter):
    global _BASELINE_GLOSSARY
    if not Path(REFINE).is_file():
        raise SystemExit(f"[MATRIX][FATAL] 管线 CLI 缺失: {REFINE}")
    disk_ok()
    _BASELINE_GLOSSARY = glossary_hashes()
    log(f"词库基线: {_BASELINE_GLOSSARY}")
    files = discover_files(file_filter)
    if not files:
        raise SystemExit(f"[MATRIX][FATAL] 输入目录无匹配文件: {INPUT_DIR}")
    combo_set = set(combos_filter or [(a, b) for a in GROUP_ORDER for b in GROUP_ORDER])
    units = []                                       # 组序按 GROUP_ORDER
    for a in GROUP_ORDER:
        if (a, a) in combo_set:
            units += [(a, a, f) for f in files]      # 组内先种子对
        units += [(a, b, f) for b in GROUP_ORDER if b != a and (a, b) in combo_set
                  for f in files]                    # 再该组 B≠A 组合
    total = len(units)
    log(f"队列：{total} 工作单元（combos={combos_filter or '全部'} file={file_filter or '全部'}）")
    recovery_scan(units)
    todo = []
    for i, (a, b, f) in enumerate(units, 1):
        if unit_done(combo_dir(a, b), stem_of(f)):
            log(f"({i}/{total}) {a}>{b} {stem_of(f)} 已完成，跳过")
        else:
            todo.append((a, b, f, i))
    deferred = []
    for a, b, f, i in todo:
        r = run_unit(a, b, f, total=total, idx=i)
        if r["result"] == "deferred":
            deferred.append((a, b, f, i))
    if deferred:                                     # 队尾重试一轮
        log(f"deferred 重试轮：{len(deferred)} 个")
        for a, b, f, i in deferred:
            r = run_unit(a, b, f, total=total, idx=i)
            if r["result"] == "deferred":
                append_csv(csv_row(a, b, f, dict(r, result="blocked",
                                                 note="seed-still-not-ready:" + r["note"])))
                log(f"({i}/{total}) {a}>{b} {stem_of(f)} => blocked（种子仍不齐）")
    remain = [(a, b, stem_of(f)) for a, b, f in units
              if not unit_done(combo_dir(a, b), stem_of(f))]
    log(f"队列结束。未完成 {len(remain)}：{remain[:10]}")
    return 0 if not remain else 1


# --- --validate-diff：新鲜重跑 diff 验收 --------------------------------------

def parse_srt_entries(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [x for x in block.splitlines() if x.strip()]
        if not lines:
            continue
        idx = timing = None
        texts = []
        for ln in lines:
            s = ln.strip()
            if idx is None and s.isdigit():
                idx = s
            elif timing is None and "-->" in ln:
                timing = s
            else:
                texts.append(ln.rstrip())
        out.append((idx, timing, "\n".join(texts).strip()))
    return out


def diff_refine(base, val):
    e1, e2 = parse_srt_entries(base), parse_srt_entries(val)
    n = min(len(e1), len(e2))
    idx_same = sum(1 for i in range(n) if e1[i][0] == e2[i][0])
    txt_same = sum(1 for i in range(n) if e1[i][2] == e2[i][2])
    total = max(len(e1), len(e2), 1)
    return {"n_base": len(e1), "n_val": len(e2), "idx_align": idx_same / total,
            "identical": txt_same / total, "diff_lines": n - txt_same,
            "diff_rate": (n - txt_same) / total}


def validate_diff_run(key, fpath, vdir, fresh=True):
    """在 _validate_tmp/<key>__<key> 跑 (key,key)（不带种子，带 --resume
    --force-resume 以便中断续跑），截获到 _Acache_validate/<key>/。"""
    if fresh and vdir.exists():
        shutil.rmtree(vdir, ignore_errors=True)
    a_id, stem, input_sha1 = MODELS[key], stem_of(fpath), sha1_file(fpath)
    mok, mnote = ensure_only_model(a_id)
    if not mok:
        raise SystemExit(f"[MATRIX][FATAL] validate 模型管理失败: {mnote}")
    res = run_pipeline(a_id, a_id, fpath, vdir,
                       LOGS / f"validate__{key}__{stem}.log",
                       int(SEED_TIMEOUT_H[key] * 3600),
                       watch=(vdir, ACACHE_V / key, stem, a_id, input_sha1))
    ok, note = assess_run(res, vdir, stem, "seed")   # 无 syn0，等价成功判据
    return res, ok, note, ACACHE_V / key, stem


def cmd_validate_diff(key, file_filter):
    files = discover_files(file_filter)
    if len(files) != 1:
        raise SystemExit(f"[MATRIX][FATAL] --file 需唯一定位一个文件，当前匹配 {len(files)}")
    if not Path(REFINE).is_file():
        raise SystemExit(f"[MATRIX][FATAL] 管线 CLI 缺失: {REFINE}")
    disk_ok()
    fpath = files[0]
    stem = stem_of(fpath)
    res, ok, note, cache, stem = validate_diff_run(key, fpath, VALIDATE_TMP / f"{key}__{key}")
    if ok != "done":
        log(f"validate 重跑失败: {note}")
        return 2
    base, val = ACACHE / key / f"{stem}_refine_A.srt", cache / f"{stem}_refine_A.srt"
    md = LOGS / f"validate_diff_{key}_{stem}.md"
    LOGS.mkdir(parents=True, exist_ok=True)
    if not (base.is_file() and val.is_file()):
        md.write_text(f"# validate_diff {key} {stem}\n\nFAIL：缺少截获产物 "
                      f"(base={base.is_file()} val={val.is_file()})\n", encoding="utf-8")
        log(f"FAIL：截获产物缺失 base={base.is_file()} val={val.is_file()} -> {md}")
        return 1
    m = diff_refine(base, val)
    verdict = "PASS" if m["diff_rate"] <= 0.02 else "FAIL"
    md.write_text(f"# validate_diff {key} / {stem}\n\n- ts: {now_ts()}\n"
                  f"- 基线: `{base}`（{m['n_base']} 条）\n- 新跑: `{val}`（{m['n_val']} 条）\n"
                  f"- 编号对齐率: {m['idx_align']:.4f}\n- 文本完全一致占比: {m['identical']:.4f}\n"
                  f"- 差异行数: {m['diff_lines']}\n- 行差异率: {m['diff_rate']:.4f}（阈值 0.02）\n\n"
                  f"## {verdict}\n", encoding="utf-8")
    log(f"validate_diff {key} {stem}: {verdict} 差异率={m['diff_rate']:.4f} -> {md}")
    return 0 if verdict == "PASS" else 1


# --- --pilot：P1 种子 → P2 中断恢复+diff → P3 第二种子 → P4 复用链路演练 ------

def _file_by(substr):
    files = discover_files(substr)
    if len(files) != 1:
        raise SystemExit(f"[MATRIX][FATAL] --pilot 文件定位失败: {substr}")
    return files[0]


def _log_has_reuse(a, b, stem):
    """组合日志是否出现“阶段A产物复用”（管线复用标志，pipeline_v2.py:1746）。"""
    lp = LOGS / f"{a}__{b}__{stem}.log"
    if not lp.is_file():
        return False
    with open(lp, encoding="utf-8", errors="replace") as f:
        return any("阶段A产物复用" in ln for ln in f)


def pilot_p2(key):
    """中断恢复验收：run1 子进程在阶段A两件套（manifest+refine_A）落盘后 kill，
    run2 完整重跑断言“阶段A产物复用”，最后与 P1 基线做 diff 判定（阈值 2%）。

    缺陷1修复：kill 触发只依赖 intercept_once(copy=False) 探测（两件套落盘即
    kill）——--validate-diff 子进程只把管线输出写日志文件、不回显 stdout，
    “[STAGE] 阶段A”永不出现，旧“stdout 标志 or 180s”触发器必然失效；7200s
    兜底仅作超大超时保护。stdout 监视与日志 tail 线程仅用于进度展示，不参与控制。
    """
    fpath = _file_by("ipzz-847")
    stem, a_id, input_sha1 = stem_of(fpath), MODELS[key], sha1_file(fpath)
    vdir, cache = VALIDATE_TMP / f"{key}__{key}", ACACHE_V / key
    for d in (vdir, cache):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--validate-diff",
                             "--model", key, "--file", "ipzz-847"], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    killed, tl_stop = [False], threading.Event()

    def _monitor():                                  # 仅展示：子进程不回显管线输出
        for raw in iter(proc.stdout.readline, b""):
            print(f"[P2-run1] {raw.decode('utf-8', 'replace').rstrip()}", flush=True)

    def _tail_log():                                 # 仅展示：管线 stdout 实际只落这里
        lp = LOGS / f"validate__{key}__{stem}.log"
        pos = 0
        while not tl_stop.is_set():
            try:
                if lp.is_file():
                    size = lp.stat().st_size
                    if size < pos:                   # 日志被截断重写
                        pos = 0
                    if size > pos:
                        with open(lp, encoding="utf-8", errors="replace") as f:
                            f.seek(pos)
                            for ln in f:
                                print(f"[P2-run1-log] {ln.rstrip()}", flush=True)
                            pos = f.tell()
            except OSError:
                pass
            tl_stop.wait(2)
    mt = threading.Thread(target=_monitor, daemon=True)
    tt = threading.Thread(target=_tail_log, daemon=True)
    mt.start()
    tt.start()
    t0 = time.time()
    while proc.poll() is None:
        # copy=False 只探测：子进程自己的截获线程在写同一缓存目录，双进程并发写
        # 同名 .tmp 会竞态；kill 后由主进程补漏截获。
        if intercept_once(vdir, cache, stem, a_id, input_sha1, copy=False):
            killed[0] = True
            log("P2：阶段A两件套已落盘，kill 验证子进程（模拟中断）")
            kill_tree(proc.pid)
            break
        if time.time() > t0 + 7200:                  # 仅超大超时兜底保护
            log("P2：run1 超过 7200s 兜底上限，kill")
            kill_tree(proc.pid)
            break
        time.sleep(1)
    proc.wait()
    tl_stop.set()
    mt.join(5)
    tt.join(5)
    with contextlib.suppress(OSError):
        proc.stdout.close()
    if not killed[0]:
        log("P2 FAIL：未能在阶段A两件套落盘后中断")
        return False
    time.sleep(1)
    if not intercept_once(vdir, cache, stem, a_id, input_sha1):  # 补漏截获
        log("P2 FAIL：中断后 _Acache_validate 截获产物缺失")
        return False
    log("P2：中断现场确认（manifest+refine_A 已截获），完整重跑验证 --resume 复用")
    res, ok, note, cache2, _ = validate_diff_run(key, fpath, vdir, fresh=False)
    if ok != "done":
        log(f"P2 FAIL：重跑失败 {note}")
        return False
    if not res["marks"]["reuse"]:
        log("P2 FAIL：重跑日志未出现“阶段A产物复用”")
        return False
    base = ACACHE / key / f"{stem}_refine_A.srt"
    if not base.is_file():
        log("P2 FAIL：基线 _Acache 缺失（P1 未完成？）")
        return False
    m = diff_refine(base, cache2 / f"{stem}_refine_A.srt")
    verdict = "PASS" if m["diff_rate"] <= 0.02 else "FAIL"
    md = LOGS / f"validate_diff_{key}_{stem}.md"
    md.write_text(f"# validate_diff {key} / {stem}（P2 中断恢复验收）\n\n- ts: {now_ts()}\n"
                  f"- 编号对齐率: {m['idx_align']:.4f}\n- 文本一致占比: {m['identical']:.4f}\n"
                  f"- 差异行数: {m['diff_lines']}\n- 行差异率: {m['diff_rate']:.4f}（阈值 0.02）\n\n"
                  f"## {verdict}\n", encoding="utf-8")
    log(f"P2 diff 判定: {verdict} 差异率={m['diff_rate']:.4f} -> {md}")
    return verdict == "PASS"


def cmd_pilot():
    global _BASELINE_GLOSSARY
    if not Path(REFINE).is_file():
        raise SystemExit(f"[MATRIX][FATAL] 管线 CLI 缺失: {REFINE}")
    disk_ok()
    _BASELINE_GLOSSARY = glossary_hashes()
    f847 = _file_by("ipzz-847")
    stem847 = stem_of(f847)

    def _seed_step(key, idx):
        """种子对（矩阵组合），已 done 自动跳过。"""
        if unit_done(combo_dir(key, key), stem847):
            log(f"种子 {key}>{key} ipzz-847 已 done，跳过")
            return True
        r = run_unit(key, key, f847, total=4, idx=idx)
        if r["result"] != "done":
            log(f"FAIL: {key}>{key} ipzz-847 {r['note']}")
            return False
        return True

    log("P1 种子演练：heretic35b>heretic35b ipzz-847")
    if not _seed_step("heretic35b", 1):
        return 1
    log("P2 中断恢复+diff 验收（车辆=heretic35b）")
    if not pilot_p2("heretic35b"):
        return 1
    log("P3 第二种子：joyfox27b>joyfox27b ipzz-847")
    if not _seed_step("joyfox27b", 3):
        return 1
    log("P4 复用链路演练：joyfox27b>heretic35b ipzz-847（复用 joyfox 的 A 草稿）")
    a, b = "joyfox27b", "heretic35b"
    cdir = combo_dir(a, b)
    if unit_done(cdir, stem847):
        # 已完成：不重跑，按日志与 .done 复核断言（复用标志以日志文件为准，
        # .done 的 mode 字段仅由 a!=b 推得、不构成复用证据）。
        reuse_ok = _log_has_reuse(a, b, stem847)
        done = read_json(cdir / f"{stem847}.done.json") or {}
        r4 = {"result": "done", "reuse": reuse_ok, "untrans": done.get("untrans"),
              "note": "already-done"}
        log(f"P4：{a}>{b} 已 done，按日志/.done 复核（reuse标志={reuse_ok} "
            f"untrans={done.get('untrans')}）")
    else:
        r4 = process_unit(a, b, f847, total=4, idx=4)
        append_csv(csv_row(a, b, f847, r4))
    if r4["result"] != "done" or not r4.get("reuse"):
        log(f"P4 FAIL（复用对）: {r4['result']} {r4['note']} reuse={r4.get('reuse')}")
        return 1
    syn_ok, syn_note = synopsis_precheck(a, stem847)
    if syn_ok != "pass":
        log(f"P4 FAIL：synopsis 预检未通过: {syn_ok} {syn_note}")
        return 1
    # 缺陷2修复：断言基线取 A 草稿口径（种子截获 refine_A 的 [未翻译] 行数），
    # 而非种子对终稿计数——健康 B 模型会在阶段B 补译掉 A 草稿部分 [未翻译]，
    # 种子终稿计数偏低，换不合格 B 时必然假失败。
    adraft = ACACHE / a / f"{stem847}_refine_A.srt"
    if not adraft.is_file():
        log(f"P4 FAIL：A 草稿缺失 {adraft}（P3 种子截获库不完整）")
        return 1
    if not isinstance(r4.get("untrans"), int):
        log(f"P4 FAIL：复用对 [未翻译] 计数缺失: {r4.get('untrans')!r}")
        return 1
    un_base = count_untrans(adraft)
    if r4["untrans"] > un_base + 2:
        log(f"P4 FAIL：复用对 [未翻译]={r4['untrans']} > A草稿口径 {un_base}+2")
        return 1
    log(f"P4 PASS：复用日志=OK synopsis预检={syn_note} untrans 复用={r4['untrans']} "
        f"≤ A草稿口径={un_base}+2")
    log("pilot 全部通过")
    return 0


# ============================================================================
# 第二轮 mida-559 全新模拟（独立函数段，不改动第一轮任何逻辑）
# 与第一轮差异：TM 启用（不传 --no-tm）；每组合运行前 TM 清零 + synopsis 缓存
# 清空（glossary.csv/glossary_learned.csv 保留照常注入）；模型仅 4 个（trans8b
# 已被用户从 LM Studio 删除，绝不再引用）；4×4 全有序对（含 4 个 A=B），组内 B
# 按 MIDA_B_ORDER 序；无种子/无阶段A复用——每个组合都是完整管线（阶段A→阶段B），
# --resume --force-resume 仅用于同组合崩溃重试续跑。
# ============================================================================

MIDA_SRC = r"E:\新建文件夹\未刮削\4k2.me@mida-559.ja.merged.whisperjav.srt"  # 只读
MIDA_ROOT = r"E:\新建文件夹\未刮削\mida559矩阵"   # 输出根目录（<A>__<B>）
MIDA_LOGS = Path(MIDA_ROOT) / "_logs"
MIDA_SUMMARY = Path(MIDA_ROOT) / "_汇总"
MIDA_STATUS_CSV = MIDA_SUMMARY / "mida559_status.csv"   # 列同第一轮 CSV_COLS
MIDA_TM_DB = Path(REPO) / "Temp" / "translation_memory" / "tm.db"
# tm.db 默认路径依据：refine/config.py:251 `tm_db_path: str = ""`（空=默认）→
# refine/tm.py:85 `db_path or _default_tm_path()` → tm.py:26-37
# `_DEFAULT_TM_DIR = <项目根>/Temp/translation_memory`；清零等价 tm.py:309 clear()。
MODELS_R2 = {
    "heretic35b": "qwen3.6-35b-a3b-uncensored-heretic-apex",
    "hauhau35b":  "qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive",
    "sakura14b":  "sakura-14b-qwen3-v1.5",
    "joyfox27b":  "qwen3.8-27b-uncensored-joyfox-aggressive",
}
MIDA_B_ORDER = list(MODELS_R2)                       # [heretic,hauhau,sakura,joyfox]
MIDA_COMBOS = [(a, b) for a in MIDA_B_ORDER for b in MIDA_B_ORDER]  # 16 全序对
MIDA_STEM = Path(MIDA_SRC).name[:-len(".srt")]
MIDA_TIMEOUT_H = {"heretic35b": 2.0, "hauhau35b": 2.0, "sakura14b": 3.0,
                  "joyfox27b": 4.5}                  # 按 B 模型，1162 条全管线估


def mlog(msg):
    print(f"[MIDA] {datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def mida_combo_dir(a, b):
    return Path(MIDA_ROOT) / f"{a}__{b}"


def mida_clear_tm():
    """TM 清零（等价 refine/tm.py:309 TranslationMemory.clear()：
    DELETE FROM tm_entries + commit）。直接 sqlite 标准库操作、不导入项目包。
    返回 (清零前, 清零后) 条数；表尚不存在（全新库）按 0 计。"""
    if not MIDA_TM_DB.is_file():
        return 0, 0
    conn = sqlite3.connect(str(MIDA_TM_DB), timeout=10)
    try:
        def _count():
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM tm_entries").fetchone()[0]
            except sqlite3.OperationalError:
                return 0
        n0 = _count()
        conn.execute("DELETE FROM tm_entries")
        conn.commit()
        return n0, _count()
    finally:
        conn.close()


def mida_clear_synopsis():
    """清空 Temp/synopsis_cache 全部文件（SYNOPSIS_DIR 与第一轮同一目录）。"""
    if not SYNOPSIS_DIR.is_dir():
        return 0
    n = 0
    for p in SYNOPSIS_DIR.iterdir():
        try:
            if p.is_file():
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def mida_reset_state():
    """每组合前置清理：模拟软件全新安装——TM 清零 + synopsis 缓存清空
    （人工词库 glossary.csv 保留，管线照常注入）。"""
    n0, n1 = mida_clear_tm()
    ns = mida_clear_synopsis()
    mlog(f"前置清理：TM {n0} -> {n1} 条（要求 0），synopsis 缓存清除 {ns} 个文件")
    if n1 != 0:
        raise SystemExit(f"[MIDA][FATAL] TM 清零失败 count={n1}")


def mida_build_cmd(a_id, b_id, fpath, out_dir):
    """与第一轮 build_cmd 唯一差异：TM 启用（无 --no-tm）。"""
    return [REFINE, "-i", str(fpath), "-o", str(out_dir), "--profile", "local",
            "--s1-provider", "lmstudio", "--s1-model", a_id,
            "--s3-provider", "lmstudio", "--s3-model", b_id,
            "--lmstudio-endpoint", ENDPOINT,
            "--resume", "--force-resume"]


def _mida_loaded_ids():
    """lms ps：R2 已知模型中当前已载的 id 集合。"""
    _, out = _run_lms(["ps"])
    if "No models are currently loaded" in out:
        return set()
    return {mid for mid in MODELS_R2.values() if mid in _ANSI_RE.sub("", out)}


def mida_unload_one(mid):
    """定向卸载单个模型（绝不 --all，防止误杀已 JIT 加载的 B）：
    lms key 与模型 id 双候选，任一成功即可。"""
    key = model_key_for(mid)
    for cand in dict.fromkeys(x for x in (key, mid) if x):
        rc, _ = _run_lms(["unload", cand], timeout=600)
        if rc == 0:
            return True
    return False


def mida_swap_to_b(a_id, b_id):
    """组合内中途换模（仅 A≠B）：先定向卸 A，再查 lms ps——B 已 JIT 加载则
    跳过 load，否则 lms load <key> -y 并 1-token ping 确认可服务。"""
    mida_unload_one(a_id)
    if b_id in _mida_loaded_ids():
        return True, "b-already-loaded"
    key = model_key_for(b_id)
    note = ""
    for cand in dict.fromkeys(x for x in (key, b_id) if x):
        rc, out = _run_lms(["load", cand, "-y"], timeout=1800)
        if rc == 0:
            return (True, f"swapped:{cand}") if ping_model(b_id) else \
                   (False, f"ping-failed:{b_id}")
        note = _ANSI_RE.sub("", out).strip()[-200:]
    return False, f"load-failed:{note}"


def mida_run_pipeline(a_id, b_id, fpath, out_dir, log_path, timeout_s):
    """第二轮单次完整管线（阶段A→阶段B）：tee 日志 + 环形缓冲 + 软超时看门狗；
    A≠B 时后台线程 0.5s 轮询组合目录 <stem>_manifest.json，A status∈
    {done,degraded} 且 final≠done 触发一次定向换模（复用第一轮轮询模式）。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    out_dir, log_path = Path(out_dir), Path(log_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stem = stem_of(fpath)
    marks = {"reuse": False, "stage_a": False, "stage_b": False}
    buf, stop = deque(maxlen=2000), threading.Event()
    swap = {"done": False, "ok": None, "note": ""}

    def _watch():                                    # 换模只触发一次
        man = out_dir / f"{stem}_manifest.json"
        while not stop.is_set():
            d = read_json(man) if man.is_file() else None
            st = (d or {}).get("stages") or {}
            if ((st.get("A") or {}).get("status") in ("done", "degraded")
                    and (st.get("final") or {}).get("status") != "done"):
                swap["done"] = True
                swap["ok"], swap["note"] = mida_swap_to_b(a_id, b_id)
                mlog(f"换模触发 {a_id} -> {b_id}: ok={swap['ok']} {swap['note']}")
                return
            stop.wait(0.5)

    wt = threading.Thread(target=_watch, daemon=True) if a_id != b_id else None
    if wt:
        wt.start()
    # noqa: SIM115 —— 同 run_pipeline：句柄传 _pump 线程，进程结束后 finally 统一 close。
    logf = open(log_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    t0 = time.time()
    proc = subprocess.Popen(mida_build_cmd(a_id, b_id, fpath, out_dir), cwd=REPO,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    pt = threading.Thread(target=_pump, args=(proc, logf, buf, marks), daemon=True)
    pt.start()
    deadline, timed_out = t0 + timeout_s, False
    while proc.poll() is None:
        if time.time() > deadline:
            timed_out = True
            mlog(f"看门狗超时({timeout_s}s) kill pid={proc.pid}")
            kill_tree(proc.pid)
            break
        time.sleep(2)
    rc = proc.wait()
    stop.set()
    if wt:
        wt.join(3)
    pt.join(10)
    try:
        proc.stdout.close()
    finally:
        logf.close()
    return {"rc": rc, "timed_out": timed_out, "elapsed": time.time() - t0,
            "marks": marks, "buf": buf, "swap": swap}


def mida_assess(res, cdir, stem):
    """第二轮成功判据：rc∈{0,3} 且 final_cn + 质量报告齐备（无种子/复用分支）。"""
    if res["timed_out"]:
        return "failed", f"soft-timeout:{int(res['elapsed'])}s"
    final_p = Path(cdir) / f"{stem}_final_cn.srt"
    rep_p = Path(cdir) / f"{stem}_质量报告.txt"
    if res["rc"] not in (0, 3) or not (final_p.is_file() and rep_p.is_file()):
        return "failed", f"rc={res['rc']} final={final_p.is_file()} report={rep_p.is_file()}"
    return "done", f"rc={res['rc']}"


def mida_process_unit(a, b, fpath, total=None, idx=None):
    """第二轮工作单元（combo × mida-559）：前置清理 + 模型管理 + 完整管线 +
    失败重试 1 次（重试前重清 TM/synopsis、只清该 stem 产物）+ .done 原子写。"""
    a_id, b_id = MODELS_R2[a], MODELS_R2[b]
    stem = stem_of(fpath)
    cdir = mida_combo_dir(a, b)
    cdir.mkdir(parents=True, exist_ok=True)
    timeout_s = int(MIDA_TIMEOUT_H[b] * 3600)
    check_glossary()                                 # 词库看门狗：变化即中止
    result, note, info = "failed", "", {}
    mida_reset_state()                               # 首次尝试前置清理
    for attempt in (1, 2):
        if attempt == 2:                             # 重试前重清 + 只清该 stem 产物
            mida_reset_state()
            clean_stem_artifacts(cdir, stem)
        mok, mnote = ensure_only_model(a_id)         # 照旧：unload --all+载 A+ping
        if not mok:
            result, note = "failed", f"model-mgmt:{mnote}"
            mlog(f"({idx}/{total}) {a}>{b} 尝试{attempt} 模型管理失败: {mnote}")
            continue
        mlog(f"({idx}/{total}) {a}>{b} 尝试{attempt} 启动 full-pipeline "
             f"timeout={timeout_s}s swap={'on' if a != b else 'off'}")
        res = mida_run_pipeline(a_id, b_id, fpath, cdir,
                                MIDA_LOGS / f"{a}__{b}__{stem}.log", timeout_s)
        result, note = mida_assess(res, cdir, stem)
        sw = res["swap"]
        if a != b and sw["done"]:
            note = f"{note};swap={'ok' if sw['ok'] else 'fail'}:{sw['note']}"
        info = {"rc": res["rc"], "elapsed": res["elapsed"], "untrans": None,
                "swap": sw["note"] if sw["done"] else ""}
        if result == "done":
            info["untrans"] = count_untrans(cdir / f"{stem}_final_cn.srt")
            atomic_write_json(cdir / f"{stem}.done.json", {   # .done 原子写
                "combo": f"{a}>{b}", "stem": stem, "a": a, "b": b,
                "exit": res["rc"], "elapsed": round(res["elapsed"], 1),
                "untrans": info["untrans"], "mode": "full", "ts": now_ts(),
                "swap": info["swap"]})
            break
        mlog(f"({idx}/{total}) {a}>{b} 尝试{attempt} {result}: {note}")
    return dict(info, result=result, note=note)


def mida_csv_row(a, b, r):
    return {"ts": now_ts(), "combo": f"{a}>{b}", "stem": MIDA_STEM, "a": a,
            "b": b, "mode": "full", "exit": r.get("rc", ""),
            "elapsed_s": round(r.get("elapsed", 0) or 0, 1),
            "untrans": r.get("untrans") if r.get("untrans") is not None else "",
            "result": r.get("result", ""), "note": r.get("note", "")}


def mida_append_csv(row):
    MIDA_SUMMARY.mkdir(parents=True, exist_ok=True)
    new = not MIDA_STATUS_CSV.exists()
    with open(MIDA_STATUS_CSV, "a", encoding="utf-8", newline="") as f:
        if new:
            f.write("\ufeff")                        # BOM（Excel 友好）
        w = csv.writer(f)
        if new:
            w.writerow(CSV_COLS)
        w.writerow([row.get(c, "") for c in CSV_COLS])


def mida_recovery_scan():
    """中断恢复：final_cn + 质量报告齐备即补记 .done 与 CSV（容忍此前中断）。"""
    n = 0
    for a, b in MIDA_COMBOS:
        cdir = mida_combo_dir(a, b)
        if not (unit_done(cdir, MIDA_STEM)
                and not (cdir / f"{MIDA_STEM}.done.json").is_file()):
            continue
        un = count_untrans(cdir / f"{MIDA_STEM}_final_cn.srt")
        atomic_write_json(cdir / f"{MIDA_STEM}.done.json", {
            "combo": f"{a}>{b}", "stem": MIDA_STEM, "a": a, "b": b,
            "exit": "recovered", "elapsed": "", "untrans": un,
            "mode": "full", "ts": now_ts(), "swap": ""})
        mida_append_csv(mida_csv_row(a, b, {"rc": "recovered", "elapsed": "",
                                            "untrans": un, "result": "done",
                                            "note": "recovery-scan"}))
        n += 1
    if n:
        mlog(f"中断恢复扫描：补记 {n} 个已存在产物的组合")
    return n


def cmd_mida_plan():
    """--mida-plan：打印 16 组合计划与完成状态（纯文件检查，不加载模型）。"""
    if not Path(MIDA_SRC).is_file():
        mlog(f"测试源文件缺失: {MIDA_SRC}")
        return 2
    mlog(f"第二轮 mida-559 全新模拟：{len(MIDA_COMBOS)} 组合（4×4 全有序对，含 4 个 "
         f"A=B），TM 启用、每组合 TM+synopsis 清零、完整管线（阶段A→阶段B）")
    mlog(f"源文件: {MIDA_SRC}")
    mlog(f"输出根: {MIDA_ROOT}  状态CSV: {MIDA_STATUS_CSV}")
    done = 0
    for i, (a, b) in enumerate(MIDA_COMBOS, 1):
        st = "done" if unit_done(mida_combo_dir(a, b), MIDA_STEM) else "pending"
        done += st == "done"
        print(f"[MIDA]   ({i:02d}/16) {a}>{b}  {MIDA_STEM}: {st}")
    mlog(f"汇总：done={done} pending={len(MIDA_COMBOS) - done}")
    return 0


def cmd_mida_queue():
    """--mida-queue：第二轮主流程。.done 跳过；启动时补记中断恢复；逐组合：
    前置清理 → unload --all 载 A + ping → 完整管线（A 完成后定向换模 B）→ 判定。"""
    global _BASELINE_GLOSSARY
    if not Path(REFINE).is_file():
        raise SystemExit(f"[MIDA][FATAL] 管线 CLI 缺失: {REFINE}")
    if not Path(MIDA_SRC).is_file():
        raise SystemExit(f"[MIDA][FATAL] 测试源文件缺失: {MIDA_SRC}")
    disk_ok()                                        # 磁盘预检
    _BASELINE_GLOSSARY = glossary_hashes()           # 词库 sha1 快照基线
    mlog(f"词库基线: {_BASELINE_GLOSSARY}  TM库: {MIDA_TM_DB}")
    MIDA_LOGS.mkdir(parents=True, exist_ok=True)
    mida_recovery_scan()
    total = len(MIDA_COMBOS)
    mlog(f"队列：{total} 组合 × 1 文件（完整管线，TM 启用，每组合 TM+synopsis 清零）")
    fpath = Path(MIDA_SRC)
    for i, (a, b) in enumerate(MIDA_COMBOS, 1):
        if (mida_combo_dir(a, b) / f"{MIDA_STEM}.done.json").is_file():
            mlog(f"({i}/{total}) {a}>{b} .done 已存在，跳过")
            continue
        r = mida_process_unit(a, b, fpath, total=total, idx=i)
        mida_append_csv(mida_csv_row(a, b, r))
    remain = [(a, b) for a, b in MIDA_COMBOS
              if not (mida_combo_dir(a, b) / f"{MIDA_STEM}.done.json").is_file()]
    mlog(f"队列结束。未完成 {len(remain)}：{remain[:10]}")
    return 0 if not remain else 1


# --- 入口 --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="model_matrix_run",
                                 description="5 模型 × A/B 全搭配无人值守跑批器")
    ap.add_argument("--plan", action="store_true", help="打印计划与当前完成状态后退出")
    ap.add_argument("--queue", action="store_true", help="主跑批流程")
    ap.add_argument("--combos", default="", help='限定子集，如 "trans8b>trans8b,joyfox27b>trans8b"')
    ap.add_argument("--file", default="", help="文件名子串过滤")
    ap.add_argument("--validate-diff", dest="validate_diff", action="store_true",
                    help="新鲜重跑 diff 验收（需 --model/--file）")
    ap.add_argument("--model", default="", help="validate-diff 使用的模型 key")
    ap.add_argument("--pilot", action="store_true", help="P1→P4 演练，任一步失败即停")
    ap.add_argument("--mida-plan", dest="mida_plan", action="store_true",
                    help="第二轮 mida-559：打印 16 组合计划与完成状态后退出")
    ap.add_argument("--mida-queue", dest="mida_queue", action="store_true",
                    help="第二轮 mida-559 主跑批（TM 启用全新模拟，16 组合完整管线）")
    args = ap.parse_args()
    if args.plan:
        return cmd_plan(parse_combos(args.combos), args.file)
    if args.queue:
        return cmd_queue(parse_combos(args.combos), args.file)
    if args.validate_diff:
        if args.model not in MODELS or not args.file:
            raise SystemExit("[MATRIX][FATAL] --validate-diff 需 --model <key> --file <子串>")
        return cmd_validate_diff(args.model, args.file)
    if args.pilot:
        return cmd_pilot()
    if args.mida_plan:
        return cmd_mida_plan()
    if args.mida_queue:
        return cmd_mida_queue()
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("用户中断")
        sys.exit(130)
