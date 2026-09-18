"""tools/tm_purge 与 TM source_name 迁移测试（v1.2.2 批次 A）。

全部使用临时 sqlite 库与合成文本，绝不引用成人内容、绝不落真实库。
覆盖：三形态匹配（raw 精确 / premerge 合并句 / normalized 全半角+标点差异）、
unknown_time 时间窗行为、dry-run 零写入、--yes 删除+备份+sha256、
--probe 命中/未命中、ensure_source_name_column 幂等迁移、store 带
source_name 写入、_learn_to_tm 传来源 stem。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import tm_purge  # noqa: E402

from subtransjav.refine import pipeline_v2 as pv  # noqa: E402
from subtransjav.refine.tm import TranslationMemory, _normalize, _simhash, ensure_source_name_column  # noqa: E402

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------

def _timing(sec: float) -> str:
    m1, s1 = divmod(int(sec), 60)
    m2, s2 = divmod(int(sec) + 1, 60)
    return f"00:{m1:02d}:{s1:02d},000 --> 00:{m2:02d}:{s2:02d},000"


def _write_srt(path: Path, items) -> Path:
    """items = [(text, start_sec), ...]，每条时长 1 秒。"""
    lines = []
    for i, (text, start) in enumerate(items, 1):
        lines += [str(i), _timing(start), text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _insert_row(db_path, src, tgt, stage=1, created_at=1234567890.0,
                source_name=None):
    """直插一行（绕过 store，用于构造 NULL created_at / 任意时间戳）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tm_entries (content_hash, source_text, target_text, "
            "stage, char_count, hit_count, created_at, source_name) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (_simhash(_normalize(src)), _normalize(src), _normalize(tgt),
             stage, len(_normalize(src)), created_at, source_name))
        conn.commit()
    finally:
        conn.close()


def _row_count(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0]
    finally:
        conn.close()


def _schema(db_path) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA table_info(tm_entries)").fetchall()
    finally:
        conn.close()


def _make_legacy_db(tmp_path):
    """宽松 schema 旧库（created_at 允许 NULL）：用于 unknown_time 场景。

    tm_entries 正式 schema 对 created_at 有 NOT NULL 约束；unknown_time
    分支是任务契约要求的防御性口径（历史/异常行），故用同列名的宽松
    schema 构造 NULL 行来验证计划逻辑（build_plan/load_tm_rows 不依赖约束）。
    """
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE tm_entries ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "content_hash TEXT NOT NULL, "
            "source_text TEXT NOT NULL, "
            "target_text TEXT NOT NULL, "
            "stage INTEGER NOT NULL DEFAULT 0, "
            "char_count INTEGER NOT NULL DEFAULT 0, "
            "hit_count INTEGER NOT NULL DEFAULT 0, "
            "created_at REAL, "
            "source_name TEXT, "
            "UNIQUE(content_hash, stage))")
        conn.commit()
    finally:
        conn.close()
    return db


def _make_standard_db(tmp_path):
    """标准测试库：3 行合成文本（raw / premerge / normalized 三形态各一）。"""
    db = str(tmp_path / "tm.db")
    tm = TranslationMemory(db)
    try:
        tm.store("これはペンです", "这是一支钢笔", 1)        # raw 精确命中
        tm.store("太郎が走って、跳ねた", "太郎跑了跳了", 1)   # premerge 合并句命中
        tm.store("ＡＢＣ、です！", "是ABC", 1)               # normalized 命中
    finally:
        tm.close()
    return db


def _make_standard_srt(tmp_path) -> Path:
    """对应三形态的输入 srt（合成文本，行 2+3 相邻短碎片可被预合并）。"""
    return _write_srt(tmp_path / "sample.merged.whisperjav.srt", [
        ("これはペンです", 0.0),
        ("太郎が走って、", 20.0),
        ("跳ねた", 20.5),
        ("ＡＢＣです", 40.0),
    ])


# ---------------------------------------------------------------------------
# 三形态匹配
# ---------------------------------------------------------------------------

def test_three_form_matching(tmp_path):
    """raw 精确 / premerge 合并句 / normalized 全半角+标点差异各命中一例。"""
    db = _make_standard_db(tmp_path)
    srt = _make_standard_srt(tmp_path)
    cands = tm_purge.build_candidates(str(srt))
    assert len(cands["premerge_texts"]) == 1        # 行2+行3 预合并成一句
    assert cands["premerge_texts"][0] == "太郎が走って、跳ねた"

    rows, _has = tm_purge.load_tm_rows(db)
    plan, summary = tm_purge.build_plan(rows, cands, srt.stem)
    by_id = {r["entry_id"]: r for r in plan}

    assert len(plan) == 3
    # TM 插入顺序 id 1/2/3 = raw / premerge / normalized
    assert by_id[1]["match_type"] == "raw"
    assert by_id[2]["match_type"] == "premerge"
    assert by_id[3]["match_type"] == "normalized"
    # 多形态命中在 reason 里列出全部形态（raw 命中行同时也是 normalized 键）
    assert "raw" in by_id[1]["reason"] and "normalized" in by_id[1]["reason"]
    assert all(r["action"] == "would_delete" for r in plan)
    # 保守口径：临时库行无 source_name → 全部疑似跨片
    assert summary["suspect_cross_file"] == 3
    assert summary["hit_raw"] == 1 and summary["hit_premerge"] == 1 \
        and summary["hit_normalized"] == 1


def test_source_name_provenance_breaks_cross_file_suspect(tmp_path):
    """source_name 与 srt stem 一致的行判为本片产物（suspect=False）。"""
    db = str(tmp_path / "tm.db")
    tm = TranslationMemory(db)
    try:
        tm.store("これはペンです", "这是一支钢笔", 1, source_name="sample.merged.whisperjav")
    finally:
        tm.close()
    srt = _write_srt(tmp_path / "sample.merged.whisperjav.srt",
                     [("これはペンです", 0.0)])
    rows, _has = tm_purge.load_tm_rows(db)
    plan, summary = tm_purge.build_plan(rows, tm_purge.build_candidates(str(srt)),
                                        srt.stem)
    assert len(plan) == 1
    assert plan[0]["suspect_cross_file"] is False
    assert summary["suspect_cross_file"] == 0


# ---------------------------------------------------------------------------
# unknown_time 行为
# ---------------------------------------------------------------------------

def test_unknown_time_kept_with_window(tmp_path):
    """NULL created_at + 指定时间窗 → action=would_keep_unknown_time。"""
    db = _make_legacy_db(tmp_path)
    in_window = tm_purge.parse_time_arg("2026-06-01 12:00", "x")
    _insert_row(db, "窗内の通常の文", "窗内普通句子", stage=1,
                created_at=in_window)
    _insert_row(db, "時間不明の文です", "时间不明的句子", stage=1, created_at=None)
    srt = _write_srt(tmp_path / "extra.srt", [
        ("窗内の通常の文", 0.0),
        ("時間不明の文です", 20.0),
    ])

    rows, _has = tm_purge.load_tm_rows(db)
    cands = tm_purge.build_candidates(str(srt))
    since = tm_purge.parse_time_arg("2026-01-01", "since")
    until = tm_purge.parse_time_arg("2026-12-31 23:59", "until")
    plan, summary = tm_purge.build_plan(rows, cands, srt.stem,
                                        since=since, until=until)
    by_action = {r["action"]: r for r in plan}
    assert set(by_action) == {"would_delete", "would_keep_unknown_time"}
    unknown = by_action["would_keep_unknown_time"]
    assert unknown["created_at"] is None
    assert unknown["source_text"] == "時間不明の文です"
    assert summary["unknown_time_total"] == 1
    assert summary["keep_unknown_time"] == 1
    assert summary["unknown_time_deleted"] == 0


def test_unknown_time_deleted_with_include_flag(tmp_path):
    """NULL created_at + --include-unknown-time → 进入删除候选。"""
    db = _make_legacy_db(tmp_path)
    in_window = tm_purge.parse_time_arg("2026-06-01 12:00", "x")
    _insert_row(db, "窗内の通常の文", "窗内普通句子", stage=1,
                created_at=in_window)
    _insert_row(db, "時間不明の文です", "时间不明的句子", stage=1, created_at=None)
    srt = _write_srt(tmp_path / "extra.srt", [
        ("窗内の通常の文", 0.0),
        ("時間不明の文です", 20.0),
    ])

    rows, _has = tm_purge.load_tm_rows(db)
    cands = tm_purge.build_candidates(str(srt))
    since = tm_purge.parse_time_arg("2026-01-01", "since")
    until = tm_purge.parse_time_arg("2026-12-31 23:59", "until")
    plan, summary = tm_purge.build_plan(rows, cands, srt.stem,
                                        since=since, until=until,
                                        include_unknown_time=True)
    assert all(r["action"] == "would_delete" for r in plan)
    assert len(plan) == 2
    assert summary["unknown_time_deleted"] == 1
    assert summary["keep_unknown_time"] == 0


def test_unknown_time_deleted_without_window(tmp_path):
    """未指定时间窗时 unknown_time 正常进入删除候选（suspect 仍 true）。"""
    db = _make_legacy_db(tmp_path)
    _insert_row(db, "時間不明の文です", "时间不明的句子", stage=1, created_at=None)
    srt = _write_srt(tmp_path / "extra.srt", [("時間不明の文です", 0.0)])
    rows, _has = tm_purge.load_tm_rows(db)
    plan, summary = tm_purge.build_plan(rows, tm_purge.build_candidates(str(srt)),
                                        srt.stem)
    assert len(plan) == 1 and plan[0]["action"] == "would_delete"
    assert summary["unknown_time_deleted"] == 1
    assert plan[0]["suspect_cross_file"] is True


def test_out_of_window_rows_excluded(tmp_path):
    """时间窗外的命中行不进删除计划（计入摘要 out_of_window）。"""
    db = _make_standard_db(tmp_path)
    srt = _make_standard_srt(tmp_path)
    rows, _has = tm_purge.load_tm_rows(db)
    cands = tm_purge.build_candidates(str(srt))
    # 窗口停在 2020 年：所有行（created_at=现在）都在窗外
    since = tm_purge.parse_time_arg("2020-01-01", "since")
    until = tm_purge.parse_time_arg("2020-12-31", "until")
    plan, summary = tm_purge.build_plan(rows, cands, srt.stem,
                                        since=since, until=until)
    assert plan == []
    assert summary["out_of_window"] == 3


# ---------------------------------------------------------------------------
# dry-run 零写入 / --yes 执行
# ---------------------------------------------------------------------------

def test_dry_run_no_deletion(tmp_path, capsys):
    """无 --yes：库行数不变，CSV 列齐全，摘要含 sha256 与计数。"""
    db = _make_standard_db(tmp_path)
    srt = _make_standard_srt(tmp_path)
    before = _row_count(db)
    csv_path = tmp_path / "plan.csv"

    rc = tm_purge.main(["--srt", str(srt), "--db", db, "--csv", str(csv_path)])

    assert rc == 0
    assert _row_count(db) == before
    header = ",".join(tm_purge.CSV_COLUMNS)
    text = csv_path.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == header
    assert "would_delete" in text
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "sha256" in out
    assert "跨片同源句（无法区分来源）单列计数: 3" in out


def test_yes_deletes_with_backup_and_sha256(tmp_path, capsys):
    """--yes（临时库）：命中行被删、未命中行保留、备份文件存在、sha256 出现。"""
    db = _make_standard_db(tmp_path)
    tm = TranslationMemory(db)
    try:
        tm.store("関係ない別の文です", "无关的另一句", 1)   # id 4：未命中，须保留
    finally:
        tm.close()
    srt = _make_standard_srt(tmp_path)
    before = _row_count(db)
    assert before == 4

    rc = tm_purge.main(["--srt", str(srt), "--db", db, "--yes"])

    assert rc == 0
    assert _row_count(db) == 1                          # 只删命中的 3 行
    conn = sqlite3.connect(db)
    try:
        left = conn.execute("SELECT source_text FROM tm_entries").fetchall()
    finally:
        conn.close()
    assert left == [(_normalize("関係ない別の文です"),)]
    backups = list(tmp_path.glob("tm_full_backup_*.csv"))
    assert len(backups) == 1 and backups[0].stat().st_size > 0
    out = capsys.readouterr().out
    assert "sha256" in out
    assert "tm.stats() 前后对比" in out               # stats 前后对比块


# ---------------------------------------------------------------------------
# --probe
# ---------------------------------------------------------------------------

def test_probe_hit_and_miss(tmp_path, capsys):
    """--probe 两态：命中退出码 0，未命中退出码 1。"""
    db = _make_standard_db(tmp_path)
    assert tm_purge.main(["--db", db, "--probe", "これはペンです"]) == 0
    out = capsys.readouterr().out
    assert "命中" in out and "match_type=exact" in out
    # normalized 退化：同字形全角、无标点差异文本也能探到
    # （归一化口径按契约不含大小写折叠，故探针用同大小写字形）
    assert tm_purge.main(["--db", db, "--probe", "ＡＢＣです"]) == 0
    out = capsys.readouterr().out
    assert "match_type=normalized" in out
    # 未命中（合成库中不存在该文本）
    miss = "この文はどこにも存在しないユニークな文です"
    assert tm_purge.main(["--db", db, "--probe", miss]) == 1
    out = capsys.readouterr().out
    assert "未命中" in out


def test_probe_like_fallback(tmp_path, capsys):
    """probe 退化 LIKE：探针为某条目的子串（非精确/非归一化等值）仍命中。"""
    db = _make_standard_db(tmp_path)
    # 「これはペンです」的子串，与任何整行既不精确也不归一化等值
    assert tm_purge.main(["--db", db, "--probe", "ペンです"]) == 0
    out = capsys.readouterr().out
    assert "match_type=like" in out


# ---------------------------------------------------------------------------
# source_name 迁移（A2）
# ---------------------------------------------------------------------------

def _old_schema_sql() -> str:
    return ("CREATE TABLE tm_entries ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "content_hash TEXT NOT NULL, "
            "source_text TEXT NOT NULL, "
            "target_text TEXT NOT NULL, "
            "stage INTEGER NOT NULL DEFAULT 0, "
            "char_count INTEGER NOT NULL DEFAULT 0, "
            "hit_count INTEGER NOT NULL DEFAULT 0, "
            "created_at REAL NOT NULL, "
            "UNIQUE(content_hash, stage))")


def test_ensure_source_name_column_idempotent(tmp_path):
    """旧库补列：首遍加列，第二遍幂等，schema 两次完全一致。"""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(_old_schema_sql())
        conn.commit()
    finally:
        conn.close()

    assert ensure_source_name_column(db) is True
    schema1 = _schema(db)
    names1 = [c[1] for c in schema1]
    assert names1.count("source_name") == 1

    assert ensure_source_name_column(db) is False       # 幂等：不再加列
    assert _schema(db) == schema1
    # 再走一次 TranslationMemory 初始化（自动迁移路径）也不变
    tm = TranslationMemory(db)
    try:
        assert _schema(db) == schema1
    finally:
        tm.close()


def test_new_db_has_source_name_column(tmp_path):
    """新建库 schema 自带 source_name 列（NULL 允许）。"""
    db = str(tmp_path / "fresh.db")
    tm = TranslationMemory(db)
    try:
        names = [c[1] for c in _schema(db)]
        col = next(c for c in _schema(db) if c[1] == "source_name")
        assert "source_name" in names
        # pragma 列序: (cid, name, type, notnull, dflt_value, pk)
        assert col[2] == "TEXT" and col[3] == 0         # TEXT 类型、可空
    finally:
        tm.close()


def test_store_with_source_name(tmp_path):
    """store/store_batch 带 source_name 写入；覆盖写 COALESCE 语义。"""
    db = str(tmp_path / "tm.db")
    tm = TranslationMemory(db)
    try:
        assert tm.store("書き込みテスト", "写入测试", 1, source_name="ep01") is True
        assert tm.store("由来不明の文", "来源不明", 1) is True
        assert tm.store_batch([("一つ目の文", "第一个", 1),
                               ("二つ目の文", "第二个", 1)],
                              source_name="ep02") == 2

        def _src_name(text):
            conn = sqlite3.connect(db)
            try:
                return conn.execute(
                    "SELECT source_name FROM tm_entries WHERE source_text=?",
                    (_normalize(text),)).fetchone()[0]
            finally:
                conn.close()

        assert _src_name("書き込みテスト") == "ep01"
        assert _src_name("由来不明の文") is None
        assert _src_name("一つ目の文") == "ep02"
        assert _src_name("二つ目の文") == "ep02"
        # 覆盖写（同 hash+stage）：带 source_name → 更新 provenance
        assert tm.store("由来不明の文", "来源不明v2", 1, source_name="ep03") is False
        assert _src_name("由来不明の文") == "ep03"
        # 覆盖写不带 source_name → 保留旧 provenance（COALESCE）
        assert tm.store("書き込みテスト", "写入测试v2", 1) is False
        assert _src_name("書き込みテスト") == "ep01"
    finally:
        tm.close()


class _RecordingTM:
    """最小 fake：只记录 store_batch 调用参数。"""

    def __init__(self):
        self.calls = []

    def store_batch(self, pairs, source_name=None):
        self.calls.append((list(pairs), source_name))
        return len(pairs)


def test_learn_to_tm_passes_source_stem():
    """_learn_to_tm 把来源 srt 文件名 stem 传给 store_batch。"""
    fake = _RecordingTM()
    orig = [{"index": 1, "timing": _timing(0), "text": "うん。"},
            {"index": 2, "timing": _timing(10), "text": "今日はいい天気だ。"}]
    final = [{"index": 1, "timing": _timing(0), "text": "嗯。"},
             {"index": 2, "timing": _timing(10), "text": "今天天气真好。"}]

    pv._learn_to_tm(fake, orig, final, gate=False,
                    file_name="4k2.me@mida-559.ja.merged.whisperjav.srt")

    assert len(fake.calls) == 1
    pairs, source_name = fake.calls[0]
    assert source_name == "4k2.me@mida-559.ja.merged.whisperjav"
    assert len(pairs) == 2
    # file_name 缺省时不传来源（None，不回填）
    fake2 = _RecordingTM()
    pv._learn_to_tm(fake2, orig, final, gate=False)
    assert fake2.calls[0][1] is None


# ---------------------------------------------------------------------------
# 排除机制（--exclude-ids / --exclude-file，白名单暂缓）
# ---------------------------------------------------------------------------

def test_exclude_ids_moves_to_keep_excluded(tmp_path):
    """--exclude-ids 生效：被排除行 action=would_keep_excluded（reason 标注
    user_hold_whitelist）、不在删除清单；其余行照常 would_delete。"""
    db = _make_standard_db(tmp_path)
    srt = _make_standard_srt(tmp_path)
    rows, _has = tm_purge.load_tm_rows(db)
    plan, summary = tm_purge.build_plan(rows, tm_purge.build_candidates(str(srt)),
                                        srt.stem, exclude_ids={1})
    by_id = {r["entry_id"]: r for r in plan}
    assert by_id[1]["action"] == "would_keep_excluded"
    assert "user_hold_whitelist" in by_id[1]["reason"]
    assert by_id[2]["action"] == "would_delete"
    assert by_id[3]["action"] == "would_delete"
    assert summary["excluded_hold"] == 1
    assert summary["would_delete"] == 2
    # 删除清单 sha256 只含 would_delete：被排除 id 不在其中
    _sha, ids = tm_purge.deletion_manifest_sha256(plan)
    assert 1 not in ids and set(ids) == {2, 3}


def test_exclude_file_and_union(tmp_path):
    """--exclude-file 生效（# 注释行/空行跳过）；与 --exclude-ids 并用取并集。"""
    db = _make_standard_db(tmp_path)
    srt = _make_standard_srt(tmp_path)
    hold = tmp_path / "hold.txt"
    hold.write_text("# 注释行\n\n1\n2\n", encoding="utf-8")
    ids = tm_purge.parse_exclude_ids("2, 999", str(hold))
    assert ids == {1, 2, 999}                       # 并集（去重）
    rows, _has = tm_purge.load_tm_rows(db)
    plan, summary = tm_purge.build_plan(rows, tm_purge.build_candidates(str(srt)),
                                        srt.stem, exclude_ids=ids)
    by_action = {}
    for r in plan:
        by_action.setdefault(r["action"], set()).add(r["entry_id"])
    assert by_action.get("would_keep_excluded") == {1, 2}
    assert by_action.get("would_delete") == {3}
    assert summary["excluded_hold"] == 2
    assert summary["would_delete"] == 1


def test_exclude_file_missing_raises(tmp_path):
    """--exclude-file 指向不存在的文件 → SystemExit（用法错误）。"""
    with pytest.raises(SystemExit):
        tm_purge.parse_exclude_ids("", str(tmp_path / "nope.txt"))


def test_dry_run_exclude_ids_end_to_end(tmp_path, capsys):
    """main() 级：--exclude-ids 后 CSV 含 would_keep_excluded 行，摘要含
    白名单暂缓计数，库行数不变（dry-run 零写入）。"""
    db = _make_standard_db(tmp_path)
    srt = _make_standard_srt(tmp_path)
    csv_path = tmp_path / "plan.csv"
    rc = tm_purge.main(["--srt", str(srt), "--db", db, "--csv", str(csv_path),
                        "--exclude-ids", "1"])
    assert rc == 0
    assert _row_count(db) == 3
    text = csv_path.read_text(encoding="utf-8-sig")
    assert "would_keep_excluded" in text and "user_hold_whitelist" in text
    out = capsys.readouterr().out
    assert "白名单暂缓计数: 1" in out


# ---------------------------------------------------------------------------
# CSV 编码契约（utf-8-sig 带 BOM）
# ---------------------------------------------------------------------------

def test_dry_run_csv_is_utf8_sig_with_bom(tmp_path):
    """CSV 输出编码契约：utf-8-sig 写出、首行带 BOM，中文可正确解码。"""
    db = _make_standard_db(tmp_path)
    srt = _make_standard_srt(tmp_path)
    csv_path = tmp_path / "plan.csv"
    rc = tm_purge.main(["--srt", str(srt), "--db", db, "--csv", str(csv_path)])
    assert rc == 0
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV 必须带 UTF-8 BOM"
    header = ",".join(tm_purge.CSV_COLUMNS)
    text = raw.decode("utf-8-sig")
    assert text.splitlines()[0] == header
    assert "这是一支钢笔" in text           # 中文原样可解码（非 GBK 乱码）


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
