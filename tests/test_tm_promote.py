"""tm_promote 测试库→主库受控回灌工具（P3-10）。

覆盖：拒绝自身、dry-run 零写入、apply 计数与去重、备份文件生成、
主库缺表拒绝、含 SQL 元字符文本的安全往返。
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import tm_promote  # noqa: E402

from subtransjav.refine.tm import TranslationMemory  # noqa: E402


def _make_db(path, pairs):
    """用 tm.py 的 TranslationMemory 建一个小库并写入 (source, target, stage)。"""
    tm = TranslationMemory(str(path))
    try:
        for src, tgt, stage in pairs:
            assert tm.store(src, tgt, stage) is True
    finally:
        tm.close()


def _rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT content_hash, source_text, target_text, stage, "
            "hit_count, created_at FROM tm_entries ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.fixture()
def two_dbs(tmp_path):
    """源（测试）库 3 条；主库含其中 1 条重叠 + 1 条自有。"""
    src = tmp_path / "test_tm.db"
    dst = tmp_path / "main_tm.db"
    _make_db(src, [
        ("これはペンです", "这是钢笔", 1),
        ("今日は暑いですね", "今天真热啊", 1),
        ("ありがとう", "谢谢", 0),
    ])
    _make_db(dst, [
        ("これはペンです", "这是钢笔（旧译）", 1),  # 与源库重叠（同 hash+stage）
        ("主库独有句子", "主库独有译文", 0),
    ])
    return src, dst


def test_promote_rejects_same_db(tmp_path):
    p = str(tmp_path / "tm.db")
    assert tm_promote.promote(p, p, apply_changes=False) == 2
    assert tm_promote.promote(p, p, apply_changes=True) == 2


def test_promote_dry_run_writes_nothing(two_dbs, capsys):
    src, dst = two_dbs
    before = _rows(dst)
    rc = tm_promote.promote(str(src), str(dst), apply_changes=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    # 计划：新增 2（另两条源库条目）/ 跳过 1（重叠条目）/ 源库总计 3
    assert "新增 2" in out and "跳过 1" in out and "总计 3" in out
    # 零写入：主库逐字节级内容不变
    assert _rows(dst) == before


def test_promote_apply_counts_and_dedup(two_dbs):
    src, dst = two_dbs
    dst_before_rows = _rows(dst)
    rc = tm_promote.promote(str(src), str(dst), apply_changes=True)
    assert rc == 0
    rows = _rows(dst)
    # 主库原有 2 条全部保留（含被跳过的重叠条目，译文不被覆盖）
    assert dst_before_rows[0] in rows, "重叠条目应原样保留（跳过不覆盖）"
    assert dst_before_rows[1] in rows
    # 回灌后主库共 4 条：原 2 条 + 新增 2 条（1 条重叠被跳过，未计入新增）
    assert len(rows) == 4
    promoted = {r[1]: r for r in rows}
    assert "今日は暑いですね" in promoted
    assert "ありがとう" in promoted


def test_promote_apply_creates_backup(two_dbs):
    src, dst = two_dbs
    dst_before_rows = _rows(dst)
    rc = tm_promote.promote(str(src), str(dst), apply_changes=True)
    assert rc == 0
    baks = list(dst.parent.glob(dst.name + ".bak-*"))
    assert len(baks) == 1, "apply 后应生成恰好一个备份文件"
    assert re.fullmatch(r".+\.bak-\d{8}_\d{4}", baks[0].name)
    # 备份内容 = apply 之前的主库快照
    assert _rows(baks[0]) == dst_before_rows


def test_promote_rejects_target_missing_table(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src, [("何か", "什么", 0)])
    bad_dst = tmp_path / "not_tm.db"
    conn = sqlite3.connect(str(bad_dst))
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit) as ei:
        tm_promote.promote(str(src), str(bad_dst), apply_changes=False)
    assert ei.value.code == 1


def test_promote_rejects_missing_db_files(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src, [("何か", "什么", 0)])
    missing = tmp_path / "missing.db"
    with pytest.raises(SystemExit) as ei:
        tm_promote.promote(str(src), str(missing), apply_changes=False)
    assert ei.value.code == 1
    with pytest.raises(SystemExit) as ei:
        tm_promote.promote(str(missing), str(src), apply_changes=False)
    assert ei.value.code == 1


def test_promote_sql_metachar_text_roundtrips(two_dbs):
    """含 SQL 引号/注释元字符的文本走参数化 INSERT，安全入库。"""
    src, dst = two_dbs
    evil = "x'; DROP TABLE tm_entries; --"
    tm = TranslationMemory(str(src))
    try:
        assert tm.store(evil, "无害译文", 0) is True
    finally:
        tm.close()
    rc = tm_promote.promote(str(src), str(dst), apply_changes=True)
    assert rc == 0
    conn = sqlite3.connect(str(dst))
    try:
        hit = conn.execute(
            "SELECT target_text FROM tm_entries WHERE source_text=?",
            (evil,)).fetchone()
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='tm_entries'").fetchone()[0]
    finally:
        conn.close()
    assert hit == ("无害译文",), "含元字符文本应原样入库"
    assert tables == 1, "tm_entries 表必须仍然存在（未被注入破坏）"
