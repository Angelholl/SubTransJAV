#!/usr/bin/env python
"""tm_promote — 测试库 TM 条目受控回灌主库（P3-10）。

用法::

    python tools/tm_promote.py --from 测试.db --to 主库.db           # dry-run（默认）
    python tools/tm_promote.py --from 测试.db --to 主库.db --apply   # 实际写入

行为约定:
  1. ``--from`` 与 ``--to`` 相同直接拒绝（防止自我污染）。
  2. ``--apply`` 前先把主库复制为 ``主库.db.bak-YYYYMMDD_HHMM``（隔离防污染）。
  3. 按主库唯一约束 (content_hash, stage) 去重：同源文本已存在则跳过并计数。
  4. 全部逐条参数化 INSERT（防 SQL 注入），单事务提交（全成或全不成）。
  5. 主库缺 ``tm_entries`` 表时拒绝执行。
  6. 结束输出 新增/跳过/总计 报告。

退出码: 0=成功（含 dry-run）；1=执行错误（库/表缺失等）；2=用法错误。
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time

TABLE = "tm_entries"
_COLUMNS = ("content_hash", "source_text", "target_text", "stage",
            "char_count", "hit_count", "created_at")


def open_existing_db(path: str, role: str) -> sqlite3.Connection:
    """打开已存在的 TM 库；缺文件或缺表时打印错误并退出（退出码 1）。"""
    if not os.path.isfile(path):
        print(f"[错误] {role}数据库不存在: {path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(path, timeout=10)
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE,)).fetchone()
    if not has_table:
        conn.close()
        print(f"[错误] {role}数据库缺少 {TABLE} 表，不是有效的翻译记忆库: {path}",
              file=sys.stderr)
        sys.exit(1)
    return conn


def read_source_entries(conn: sqlite3.Connection) -> list:
    cols = ", ".join(_COLUMNS)
    return conn.execute(f"SELECT {cols} FROM {TABLE} ORDER BY id").fetchall()


def plan_promotion(target_conn: sqlite3.Connection, entries: list):
    """计算回灌计划：返回 (待插入行列表, 跳过数)。

    去重键与主库唯一约束一致：UNIQUE(content_hash, stage)。
    """
    to_insert = []
    skipped = 0
    for row in entries:
        data = dict(zip(_COLUMNS, row, strict=False))
        exists = target_conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE content_hash=? AND stage=?",
            (data["content_hash"], data["stage"])).fetchone()
        if exists:
            skipped += 1
        else:
            to_insert.append(data)
    return to_insert, skipped


def backup_target(to_db: str) -> str:
    """--apply 前的隔离备份；返回备份文件路径。

    主库可能由 TranslationMemory 以 WAL 模式写入过，先做 checkpoint
    确保 .db 单文件内容完整，再整文件复制。
    """
    conn = sqlite3.connect(to_db, timeout=10)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    stamp = time.strftime("%Y%m%d_%H%M")
    bak = f"{to_db}.bak-{stamp}"
    shutil.copy2(to_db, bak)
    return bak


def insert_rows(target_conn: sqlite3.Connection, rows: list) -> None:
    """逐条参数化 INSERT（防注入），单事务提交。"""
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join("?" for _ in _COLUMNS)
    for data in rows:
        target_conn.execute(
            f"INSERT INTO {TABLE} ({cols}) VALUES ({placeholders})",
            tuple(data[c] for c in _COLUMNS))
    target_conn.commit()


def promote(from_db: str, to_db: str, apply_changes: bool) -> int:
    """执行回灌流程。返回退出码（0=成功）。"""
    if os.path.abspath(from_db) == os.path.abspath(to_db):
        print(f"[错误] --from 与 --to 指向同一数据库，拒绝回灌: {to_db}",
              file=sys.stderr)
        return 2

    src = open_existing_db(from_db, "源(测试)")
    try:
        entries = read_source_entries(src)
    finally:
        src.close()

    dst = open_existing_db(to_db, "目标(主)")
    try:
        to_insert, skipped = plan_promotion(dst, entries)
        if not apply_changes:
            print(f"[dry-run] 计划回灌: 新增 {len(to_insert)} / "
                  f"跳过 {skipped} / 源库总计 {len(entries)}")
            print("[dry-run] 未写入任何数据（加 --apply 执行实际写入）")
            return 0
        bak = backup_target(to_db)
        insert_rows(dst, to_insert)
        print(f"已备份主库: {bak}")
        print(f"回灌完成: 新增 {len(to_insert)} / 跳过 {skipped} / "
              f"源库总计 {len(entries)}")
        return 0
    finally:
        dst.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tm_promote",
        description="把测试库 TM 条目受控回灌主库（默认 dry-run，--apply 才写入）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="只统计不写入（默认行为）")
    g.add_argument("--apply", action="store_true",
                   help="实际执行写入（写前自动备份主库）")
    p.add_argument("--from", dest="from_db", required=True,
                   metavar="测试库.db", help="源（测试）TM 数据库")
    p.add_argument("--to", dest="to_db", required=True,
                   metavar="主库.db", help="目标（主）TM 数据库")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply:
        args.dry_run = False
    return promote(args.from_db, args.to_db, apply_changes=not args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
