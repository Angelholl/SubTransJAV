#!/usr/bin/env python
"""tm_purge — 翻译记忆库 (TM) 清洗 dry-run / 受控执行工具（v1.2.2 批次 A1）。

用途:
  以某个输入 srt 的源句为候选集，在 tm_entries 中定位「同源条目」并生成
  清洗计划，用于清退错误/毒化条目。候选集三形态（均由输入 srt 源句派生）：
    raw        srt 原始条目文本逐条（content_hash 精确匹配）；
    premerge   raw 条目喂给 pipeline_v2._premerge_entries 复现确定性预合并
               后的文本形态（content_hash 精确匹配）；
    normalized 对 raw 与 premerge 文本再做归一化（NFKC 全半角、空白折叠、
               去标点/括号/省略号类符号、去引号）后的匹配键，与 TM 行的
               「归一化 source_text」等值匹配（独立 match_type，CSV 逐行可辨）。
  多形态同时命中取优先级 raw > premerge > normalized 为 match_type，
  reason 列出全部命中的形态。

时序约束（契约原文）:
  --yes 仅批次 E 且批次 B/C/D 全部落地后才允许执行。

风险声明:
  1. 清洗改变 TM 指纹，旧 resume 必然失效——删旧 resume 产物，禁止复用。
  2. 旧 TM 无 provenance，清洗可能跨片误删同源句，只能靠 dry-run + CSV 备份兜底。

跨片单列口径（保守）:
  旧库无 source_name（provenance），全部行视为疑似跨片；仅 source_name 与
  输入 srt stem 一致的行判定为本片产物，凡无法判定来源一律
  suspect_cross_file=true，摘要单列「跨片同源句（无法区分来源）单列计数」
  供用户人工审核。

时间窗:
  --since / --until（YYYY-MM-DD 或 YYYY-MM-DD HH:MM）按 created_at 过滤。
  created_at 为 NULL 的行归入 unknown_time 组：指定了时间窗时默认不删
  （action=would_keep_unknown_time），除非显式 --include-unknown-time；
  未指定时间窗时 unknown_time 正常进入删除候选（suspect_cross_file 仍标 true）。

排除机制（白名单暂缓）:
  --exclude-ids "1,2,3"（逗号分隔 entry_id）与 --exclude-file <txt>
  （一行一个 entry_id，# 开头行为注释）可并用取并集；被排除的 id 从删除
  候选移除，CSV 中 action=would_keep_excluded（reason 标注
  user_hold_whitelist）。排除机制用于批次 E 前承接人工审核裁决
  （dry-run CSV 审核后，暂缓条目经本参数排除）。

用法:
  python tools/tm_purge.py --srt <input.srt>                     # dry-run（默认，零写入）
  python tools/tm_purge.py --srt <input.srt> --csv 计划.csv       # 指定 CSV 路径
  python tools/tm_purge.py --srt <input.srt> --since 2026-09-01 --until "2026-09-17 23:59"
  python tools/tm_purge.py --srt <input.srt> --exclude-ids "43755,43843"  # 白名单暂缓
  python tools/tm_purge.py --srt <input.srt> --exclude-file 白名单.txt    # 同上（文件形态）
  python tools/tm_purge.py --db <tm.db> --probe "源句片段"        # 探针（退出码 0=命中/1=未命中）
  python tools/tm_purge.py --srt <input.srt> --yes               # 实际删除（见时序约束）

--yes 执行流程: 先调 tm.export_csv 全量备份到带时间戳路径 → 删除清单
（entry_id 升序）计算 sha256 写入运行日志（logger + stdout）→ 单事务删除 →
打印 tm.stats() 前后对比。--yes 仍要求提供 srt 路径，并打印时序约束警告。

退出码: 0=成功（dry-run / probe 命中）；1=probe 未命中 / 执行错误；2=用法错误。
"""

import argparse
import csv
import hashlib
import logging
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine.filters import parse_srt  # noqa: E402
from subtransjav.refine.pipeline_v2 import _premerge_entries  # noqa: E402

# 与 tm.py 同源复用：content_hash 的归一化/哈希定义必须与入库侧完全一致，
# 否则 raw/premerge 精确匹配会漏配——此处耦合即正确性，勿本地复制实现
from subtransjav.refine.tm import TranslationMemory, _default_tm_path, _simhash  # noqa: E402

logger = logging.getLogger("tm_purge")

# dry-run CSV 列（契约要求齐全；固定 encoding='utf-8-sig'（带 BOM）写出，
# Excel 直开不乱码；source/target 原样写入本地文件，脱敏只在向用户汇报
# 样例时做）
CSV_COLUMNS = ["entry_id", "source_text", "target_text", "match_type",
               "action", "reason", "created_at", "hit_count",
               "suspect_cross_file"]

# normalized 归一化键：去全部 Unicode 标点类（含括号/引号/省略号 …），
# 再补去个别归类为符号的省略号/波浪线类字符
_STRIP_CATEGORY_PREFIX = "P"
_STRIP_EXTRA = set("…⋯‥~〜～·")


def normalize_for_match(text: str) -> str:
    """归一化匹配键：NFKC 全半角 → 去标点/括号/省略号类符号/引号 → 空白折叠。

    仅用于匹配键等值比较，不回写库、不改 TM 数据。
    """
    t = unicodedata.normalize("NFKC", text or "")
    kept = []
    for ch in t:
        if unicodedata.category(ch).startswith(_STRIP_CATEGORY_PREFIX) \
                or ch in _STRIP_EXTRA:
            continue
        kept.append(ch)
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def mask(text: str, keep: int = 10) -> str:
    """向用户汇报样例时的脱敏展示：前 N 字符 + …"""
    t = text or ""
    return t[:keep] + "…" if len(t) > keep else t


def parse_exclude_ids(comma_text: str = "", file_path: str = None) -> set:
    """解析白名单排除清单：--exclude-ids 与 --exclude-file 取并集。

    --exclude-ids  逗号分隔 entry_id（如 "43755,43843"）；
    --exclude-file 一行一个 entry_id，# 开头为注释行，空行跳过。
    排除机制用于批次 E 前承接人工审核裁决（dry-run CSV 审核后，暂缓条目
    经本参数排除）。返回 int 集合；非法 id / 文件缺失立即报错退出。
    """
    ids: set = set()

    def _add(token: str, where: str):
        t = token.strip()
        if not t:
            return
        try:
            ids.add(int(t))
        except ValueError as exc:
            raise SystemExit(
                f"[错误] {where} 含非数字 entry_id: {token!r}") from exc

    for tok in (comma_text or "").split(","):
        _add(tok, "--exclude-ids")
    if file_path:
        p = Path(file_path)
        if not p.is_file():
            raise SystemExit(f"[错误] --exclude-file 不存在: {file_path}")
        for ln, line in enumerate(
                p.read_text(encoding="utf-8-sig").splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            _add(s, f"--exclude-file 第 {ln} 行")
    return ids


# ---------------------------------------------------------------------------
# 候选集构建（三形态）
# ---------------------------------------------------------------------------

def build_candidates(srt_path: str) -> dict:
    """由输入 srt 源句构建 raw / premerge / normalized 三形态候选集。

    premerge：按 _premerge_entries 原签名构造最小 entries 结构
    （parse_srt 已含 index/timing/text，cfg 缺省回退模块常量），只取文本
    形态，且与 raw 形态去重（未发生合并的条目文本与 raw 相同）。
    """
    p = Path(srt_path)
    if not p.is_file():
        raise SystemExit(f"[错误] 输入 srt 不存在: {srt_path}")
    try:
        content = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = p.read_text(encoding="cp932")
        except UnicodeDecodeError as e:
            raise SystemExit(
                f"[错误] srt 解码失败（utf-8/cp932 均失败）: {e}") from e
    entries = parse_srt(content)
    if not entries:
        raise SystemExit(f"[错误] srt 解析结果为空（无有效条目）: {srt_path}")

    raw_texts = []
    for e in entries:
        t = (e["text"] or "").strip()
        if t and t not in raw_texts:
            raw_texts.append(t)

    premerge_texts = []
    seen = set(raw_texts)
    for e in _premerge_entries([dict(e) for e in entries]):
        t = (e["text"] or "").strip()
        if t and t not in seen:
            premerge_texts.append(t)
            seen.add(t)

    norm_keys = {normalize_for_match(t) for t in raw_texts + premerge_texts}
    norm_keys.discard("")
    return {
        "raw_texts": raw_texts,
        "premerge_texts": premerge_texts,
        "norm_keys": norm_keys,
        "n_entries": len(entries),
    }


# ---------------------------------------------------------------------------
# TM 读取（只读）与计划构建
# ---------------------------------------------------------------------------

def _open_ro(db_path: str) -> sqlite3.Connection:
    """只读打开（URI mode=ro）：dry-run/probe 对真实库零写入。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def load_tm_rows(db_path: str) -> tuple:
    """读取全部行。返回 (rows, has_source_name)；rows 元素为 dict。"""
    conn = _open_ro(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tm_entries)")}
        has_source_name = "source_name" in cols
        sel = ("id, content_hash, source_text, target_text, stage, "
               "char_count, hit_count, created_at")
        sql = (f"SELECT {sel}{', source_name' if has_source_name else ', NULL'} "
               "FROM tm_entries ORDER BY id")
        keys = ("id", "content_hash", "source_text", "target_text", "stage",
                "char_count", "hit_count", "created_at", "source_name")
        rows = [dict(zip(keys, r, strict=True)) for r in conn.execute(sql)]
        return rows, has_source_name
    finally:
        conn.close()


_TIME_FMTS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")


def parse_time_arg(value: str, name: str) -> float:
    """--since/--until 解析为本地时间 epoch 秒（created_at 为 time.time()）。"""
    for fmt in _TIME_FMTS:
        try:
            return time.mktime(time.strptime(value.strip(), fmt))
        except ValueError:
            continue
    raise SystemExit(f"[错误] --{name} 格式应为 YYYY-MM-DD[ HH:MM]: {value}")


def build_plan(rows: list, cands: dict, srt_stem: str, since=None,
               until=None, include_unknown_time=False,
               exclude_ids=None) -> tuple:
    """逐行判定命中与 action。返回 (plan_rows, summary)。

    - action: would_delete / would_keep_unknown_time（仅 unknown_time 行在
      时间窗模式下默认保留时出现）/ would_keep_excluded（用户白名单暂缓）；
    - exclude_ids：排除机制（白名单暂缓）——被排除的 id 从删除候选移除，
      action 置 would_keep_excluded，reason 标注 user_hold_whitelist；
    - 指定时间窗时窗外的命中行不进 CSV，仅计入摘要（时间窗外命中（保留））；
    - match_type 取最高优先级命中形态，reason 列出全部命中形态；
    - suspect_cross_file 保守口径：source_name 与 srt stem 一致才判 False。
    """
    raw_hashes = {_simhash(t) for t in cands["raw_texts"]}
    premerge_hashes = {_simhash(t) for t in cands["premerge_texts"]}
    norm_keys = cands["norm_keys"]
    window = since is not None or until is not None
    excluded = set(exclude_ids or ())
    plan = []
    summary = {
        "total": len(rows),
        "n_entries": cands["n_entries"],
        "raw_forms": len(cands["raw_texts"]),
        "premerge_forms": len(cands["premerge_texts"]),
        "norm_keys": len(norm_keys),
        "hit_raw": 0,
        "hit_premerge": 0,
        "hit_normalized": 0,
        "would_delete": 0,
        "excluded_hold": 0,
        "keep_unknown_time": 0,
        "unknown_time_total": 0,
        "unknown_time_deleted": 0,
        "out_of_window": 0,
        "suspect_cross_file": 0,
    }
    for row in rows:
        forms = []
        if row["content_hash"] in raw_hashes:
            forms.append("raw")
        if row["content_hash"] in premerge_hashes:
            forms.append("premerge")
        if normalize_for_match(row["source_text"]) in norm_keys:
            forms.append("normalized")
        if not forms:
            continue
        match_type = forms[0]          # 优先级 raw > premerge > normalized
        reason = "命中形态: " + "; ".join(forms)
        created = row["created_at"]
        # 跨片保守口径：旧库无 provenance → 全部疑似跨片；source_name 与
        # 输入 srt stem 一致才判定为本片产物
        suspect = not (srt_stem and row["source_name"]
                       and row["source_name"] == srt_stem)
        if created is None:
            summary["unknown_time_total"] += 1
            if window and not include_unknown_time:
                action = "would_keep_unknown_time"
                reason += "; created_at 为 NULL，时间窗模式下默认保留"
                summary["keep_unknown_time"] += 1
            else:
                action = "would_delete"
                reason += "; created_at 为 NULL" + (
                    "（--include-unknown-time 显式纳入）" if window
                    else "（未指定时间窗）")
                if row["id"] not in excluded:
                    summary["unknown_time_deleted"] += 1
        elif window and not ((since is None or created >= since)
                             and (until is None or created <= until)):
            summary["out_of_window"] += 1
            continue                   # 时间窗外：保留，不进删除计划
        else:
            action = "would_delete"
        if row["id"] in excluded:
            # 排除机制：用户审核裁决暂缓条目转白名单，从删除候选移除，
            # 不随 --yes 自动删除（删除清单/sha256 仅含 would_delete，
            # 排除自动生效）
            action = "would_keep_excluded"
            reason += ("; user_hold_whitelist"
                       "（用户审核裁决暂缓，白名单不自动删）")
            summary["excluded_hold"] += 1
        if action == "would_delete":
            summary["would_delete"] += 1
        summary["hit_" + match_type] += 1
        if suspect:
            summary["suspect_cross_file"] += 1
        plan.append({
            "entry_id": row["id"],
            "source_text": row["source_text"],
            "target_text": row["target_text"],
            "match_type": match_type,
            "action": action,
            "reason": reason,
            "created_at": created,
            "hit_count": row["hit_count"],
            "suspect_cross_file": suspect,
        })
    plan.sort(key=lambda r: r["entry_id"])
    return plan, summary


def write_csv(plan: list, path: str):
    """dry-run 报告 CSV 落盘。

    编码契约：固定 encoding='utf-8-sig'（带 BOM），Excel 直开不乱码。
    此前一次真实 dry-run 报告曾按无 BOM 形态写出，中文 Windows 下 Excel
    默认按 GBK 解码导致乱码（被误判为"GBK 编码"）——本函数不得改回
    默认 locale 编码，也不得去掉 BOM。
    """
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in plan:
            w.writerow({k: r[k] for k in CSV_COLUMNS})


def deletion_manifest_sha256(plan: list) -> tuple:
    """删除清单 sha256：仅 action=would_delete 行，entry_id 升序，每行一个 id。"""
    ids = sorted(r["entry_id"] for r in plan if r["action"] == "would_delete")
    digest = hashlib.sha256()
    for i in ids:
        digest.update(f"{i}\n".encode())
    return digest.hexdigest(), ids


def print_summary(summary: dict, csv_path: str, sha: str, has_source_name: bool,
                  window: bool):
    print("=" * 64)
    print("== tm_purge dry-run 报告 ==")
    print(f"TM 总行数: {summary['total']}"
          f"（provenance 列 source_name: {'存在' if has_source_name else '缺失'}）")
    print(f"输入 srt 条目: {summary['n_entries']}"
          f"（候选形态 raw={summary['raw_forms']}"
          f" / premerge={summary['premerge_forms']}"
          f" / normalized 键={summary['norm_keys']}）")
    print("命中计数（按 match_type，优先级 raw > premerge > normalized）:")
    print(f"  raw={summary['hit_raw']}  premerge={summary['hit_premerge']}"
          f"  normalized={summary['hit_normalized']}")
    print(f"unknown_time 计数: {summary['unknown_time_total']}"
          f"（进入删除候选 {summary['unknown_time_deleted']}"
          f" / 保留 {summary['keep_unknown_time']}）")
    if window:
        print(f"时间窗外命中（保留，不进 CSV）: {summary['out_of_window']}")
    print(f"跨片同源句（无法区分来源）单列计数: {summary['suspect_cross_file']}")
    print(f"删除候选合计（would_delete）: {summary['would_delete']}")
    print(f"白名单暂缓计数: {summary['excluded_hold']}"
          f"（would_keep_excluded，不随 --yes 自动删除）")
    print(f"计划删除清单 sha256: {sha}（--yes 时原样写入运行日志）")
    print(f"CSV: {csv_path}")
    print("=" * 64)


# ---------------------------------------------------------------------------
# probe / 执行
# ---------------------------------------------------------------------------

def _print_probe_hit(match_type: str, row) -> None:
    entry_id, source = row[0], row[1]
    print(f"[probe] 命中 match_type={match_type} entry_id={entry_id} "
          f"source_text={mask(source)}")


def run_probe(db_path: str, probe_text: str) -> int:
    """探针：精确（content_hash）→ 归一化 → LIKE，逐级退化。0=命中/1=未命中。"""
    conn = _open_ro(db_path)
    try:
        row = conn.execute(
            "SELECT id, source_text FROM tm_entries WHERE content_hash=? "
            "ORDER BY id LIMIT 1", (_simhash(probe_text),)).fetchone()
        if row:
            _print_probe_hit("exact", row)
            return 0
        nk = normalize_for_match(probe_text)
        if nk:
            for r in conn.execute(
                    "SELECT id, source_text FROM tm_entries ORDER BY id"):
                if normalize_for_match(r[1]) == nk:
                    _print_probe_hit("normalized", r)
                    return 0
        esc = (probe_text.replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
        row = conn.execute(
            "SELECT id, source_text FROM tm_entries WHERE source_text "
            "LIKE ? ESCAPE '\\' ORDER BY id LIMIT 1", (f"%{esc}%",)).fetchone()
        if row:
            _print_probe_hit("like", row)
            return 0
        print(f"[probe] 未命中: {mask(probe_text)}")
        return 1
    finally:
        conn.close()


def execute_delete(db_path: str, plan: list, backup_path: str) -> tuple:
    """--yes 执行：备份 → 删除（单事务）→ 返回 (sha256, ids)。

    删除清单（entry_id 升序）sha256 写入运行日志（logger + stdout 双写）。
    """
    sha, ids = deletion_manifest_sha256(plan)
    logger.info("tm_purge 备份完成: %s", backup_path)
    print(f"📦 全量备份: {backup_path}")
    logger.info("tm_purge 删除清单 sha256=%s 条数=%d 库=%s",
                sha, len(ids), db_path)
    print(f"🗑️ 删除清单 sha256: {sha}（{len(ids)} 条，entry_id 升序）")
    if not ids:
        print("删除清单为空，跳过删除。")
        return sha, ids
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM tm_entries WHERE id IN ({ph})", chunk)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return sha, ids


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="TM 清洗 dry-run / 受控执行（v1.2.2 批次 A1）")
    parser.add_argument("--srt", default=None,
                        help="输入 srt（候选集来源；--yes 时必填，--probe 时可省略）")
    parser.add_argument("--db", default="",
                        help="TM 库路径（默认复用 config 定位 Temp/translation_memory/tm.db）")
    parser.add_argument("--since", default=None,
                        help="时间窗起点 YYYY-MM-DD[ HH:MM]")
    parser.add_argument("--until", default=None,
                        help="时间窗终点 YYYY-MM-DD[ HH:MM]")
    parser.add_argument("--include-unknown-time", action="store_true",
                        help="时间窗模式下把 created_at 为 NULL 的行也纳入删除候选")
    parser.add_argument("--exclude-ids", default="",
                        help="逗号分隔 entry_id：白名单暂缓，从删除候选移除"
                             "（action=would_keep_excluded），不随 --yes 自动删除")
    parser.add_argument("--exclude-file", default="",
                        help="排除清单文件：一行一个 entry_id，# 开头为注释行"
                             "（与 --exclude-ids 并用取并集）")
    parser.add_argument("--csv", default="",
                        help="dry-run 报告 CSV 路径（默认库目录下自动带时间戳）")
    parser.add_argument("--probe", default=None,
                        help="源句片段探针：输出命中与否及 match_type（0=命中/1=未命中）")
    parser.add_argument("--yes", action="store_true",
                        help="实际删除（时序约束：仅批次 E 后允许，见 docstring）")
    args = parser.parse_args(argv)

    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db_path = args.db or _default_tm_path()
    if not Path(db_path).is_file():
        print(f"[错误] TM 库不存在: {db_path}", file=sys.stderr)
        return 1

    if args.probe is not None:
        return run_probe(db_path, args.probe)

    if not args.srt:
        parser.error("--srt 必填（仅 --probe 模式可省略）")   # 退出码 2
    if args.yes:
        print("⚠️ 时序约束：--yes 仅批次 E 且批次 B/C/D 全部落地后才允许执行。")
        print("⚠️ 清洗改变 TM 指纹，旧 resume 必然失效——删旧 resume 产物，禁止复用。")

    since = parse_time_arg(args.since, "since") if args.since else None
    until = parse_time_arg(args.until, "until") if args.until else None
    if since is not None and until is not None and since > until:
        raise SystemExit("[错误] --since 晚于 --until")
    exclude_ids = parse_exclude_ids(args.exclude_ids, args.exclude_file)

    cands = build_candidates(args.srt)
    srt_stem = Path(args.srt).stem
    rows, has_source_name = load_tm_rows(db_path)
    plan, summary = build_plan(rows, cands, srt_stem, since=since, until=until,
                               include_unknown_time=args.include_unknown_time,
                               exclude_ids=exclude_ids)
    sha, _ids = deletion_manifest_sha256(plan)

    if not args.yes:
        csv_path = args.csv or str(
            Path(db_path).parent
            / f"tm_purge_report_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        write_csv(plan, csv_path)
        print_summary(summary, csv_path, sha, has_source_name,
                      window=since is not None or until is not None)
        return 0

    # ---- --yes 执行路径（本批不运行；时序约束见 docstring）----
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = str(Path(db_path).parent / f"tm_full_backup_{stamp}.csv")
    tm = TranslationMemory(db_path)     # 打开即自动补 source_name 列（幂等）
    try:
        stats_before = tm.stats()
        tm.export_csv(backup_path)      # 删除前全量备份（契约）
        sha2, ids = execute_delete(db_path, plan, backup_path)
        stats_after = tm.stats()
    finally:
        tm.close()
    print_summary(summary, backup_path, sha2, has_source_name,
                  window=since is not None or until is not None)
    print("-- tm.stats() 前后对比 --")
    print(f"  前: total={stats_before['total']} "
          f"by_stage={stats_before['by_stage']} "
          f"total_hits={stats_before['total_hits']}")
    print(f"  后: total={stats_after['total']} "
          f"by_stage={stats_after['by_stage']} "
          f"total_hits={stats_after['total_hits']}")
    print(f"  删除 {len(ids)} 条"
          f"（{stats_before['total'] - stats_after['total']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
