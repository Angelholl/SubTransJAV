"""
翻译记忆库 (Translation Memory)
================================
基于 SQLite 的句子级翻译记忆：避免对相同/高度相似内容重复调用 API。
与词库（术语级）互补——TM 是句子级。

存储格式：
  source_text  原文（日文/中文）
  target_text  译文
  stage        来源阶段 (1/2/3)
  char_count   原文字符数（用于快速过滤）
  created_at   创建时间戳
  hit_count    命中次数（自学习排序）

匹配策略：
  精确匹配（O(1) 哈希查找）→ 返回完全一致的译文
  模糊匹配（字符重叠率 ≥ threshold）→ 返回候选项供提示
"""

import hashlib
import os
import sqlite3
import time
from typing import List, Tuple, Optional


_DEFAULT_TM_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Temp", "translation_memory"
)


def _default_tm_path() -> str:
    os.makedirs(_DEFAULT_TM_DIR, exist_ok=True)
    return os.path.join(_DEFAULT_TM_DIR, "tm.db")


def _normalize(text: str) -> str:
    """归一化：去首尾空白、合并连续空白"""
    import re
    return re.sub(r"\s+", " ", (text or "").strip())


def _simhash(text: str) -> str:
    """内容哈希（用于精确匹配的快速索引）"""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:32]


class TranslationMemory:
    """SQLite 翻译记忆库"""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or _default_tm_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tm_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL,
                source_text  TEXT NOT NULL,
                target_text  TEXT NOT NULL,
                stage        INTEGER NOT NULL DEFAULT 0,
                char_count   INTEGER NOT NULL DEFAULT 0,
                hit_count    INTEGER NOT NULL DEFAULT 0,
                created_at   REAL NOT NULL,
                UNIQUE(content_hash, stage)
            );
            CREATE INDEX IF NOT EXISTS idx_tm_hash
                ON tm_entries(content_hash);
            CREATE INDEX IF NOT EXISTS idx_tm_chars
                ON tm_entries(char_count);
        """)
        conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------

    def store(self, source: str, target: str, stage: int = 0) -> bool:
        """存入翻译对。已存在（同 hash + stage）则更新译文。返回是否新增。

        无竞态实现：INSERT OR IGNORE 的 rowcount 直接区分 新插入(1)/
        已存在(0)，无需前置 SELECT（消除 SELECT 与写入之间的竞态窗口；
        实测 conn.total_changes 对 REPLACE 无论新增还是覆盖差值均为 1，
        无法区分，故不用）。已存在时走 UPDATE：保留原 hit_count 与
        created_at，与原 INSERT OR REPLACE + COALESCE(hit_count) 语义一致。
        """
        src = _normalize(source)
        tgt = _normalize(target)
        if not src or not tgt:
            return False
        h = _simhash(src)
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO tm_entries "
            "(content_hash, source_text, target_text, stage, char_count, hit_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (h, src, tgt, stage, len(src), time.time()))
        if cur.rowcount == 1:
            conn.commit()
            return True
        conn.execute(
            "UPDATE tm_entries SET source_text=?, target_text=?, char_count=? "
            "WHERE content_hash=? AND stage=?",
            (src, tgt, len(src), h, stage))
        conn.commit()
        return False

    def store_batch(self, pairs: List[Tuple[str, str, int]]) -> int:
        """批量存入。pairs = [(source, target, stage), ...]。返回新增条数。"""
        added = 0
        for src, tgt, stg in pairs:
            if self.store(src, tgt, stg):
                added += 1
        return added

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------

    def lookup_exact(self, source: str, stage: int = 0) -> Optional[str]:
        """精确查找：返回译文或 None。命中时自动更新 hit_count。"""
        h = _simhash(_normalize(source))
        conn = self._get_conn()
        row = conn.execute(
            "SELECT target_text FROM tm_entries WHERE content_hash=? AND stage=?",
            (h, stage)).fetchone()
        if row:
            conn.execute(
                "UPDATE tm_entries SET hit_count=hit_count+1 "
                "WHERE content_hash=? AND stage=?", (h, stage))
            conn.commit()
            return row[0]
        return None

    def has_exact(self, source: str, stage: int = 0) -> bool:
        """只读精确查找：返回是否存在匹配条目（不更新 hit_count）。"""
        h = _simhash(_normalize(source))
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM tm_entries WHERE content_hash=? AND stage=?",
            (h, stage)).fetchone()
        return row is not None

    def lookup_exact_all_stages(self, source: str) -> dict:
        """精确查找所有阶段：返回 {stage: target_text}"""
        h = _simhash(_normalize(source))
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT stage, target_text FROM tm_entries WHERE content_hash=?",
            (h,)).fetchall()
        if rows:
            for r in rows:
                conn.execute(
                    "UPDATE tm_entries SET hit_count=hit_count+1 "
                    "WHERE content_hash=? AND stage=?", (h, r[0]))
            conn.commit()
            return {r[0]: r[1] for r in rows}
        return {}

    def lookup_fuzzy(self, source: str, stage: int = 0,
                     threshold: float = 0.8) -> List[Tuple[str, str, float]]:
        """模糊查找：返回 [(source, target, similarity), ...] 按相似度降序。
        threshold: 字符重叠率下限 (0-1)。
        """
        src = _normalize(source)
        if not src:
            return []
        src_len = len(src)
        conn = self._get_conn()
        # 按字符数缩小范围（±50% 以覆盖不同长度的相似句子）
        lo, hi = max(1, int(src_len * 0.5)), int(src_len * 1.5)
        rows = conn.execute(
            "SELECT source_text, target_text FROM tm_entries "
            "WHERE stage=? AND char_count BETWEEN ? AND ? "
            "ORDER BY hit_count DESC LIMIT 200",
            (stage, lo, hi)).fetchall()
        results = []
        src_lower = src.lower()
        for s, t in rows:
            s_norm = _normalize(s)
            sim = _char_overlap(src_lower, s_norm.lower())
            if sim >= threshold:
                results.append((s_norm, t, sim))
        results.sort(key=lambda x: x[2], reverse=True)
        return results[:10]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0]
        by_stage = {}
        for row in conn.execute(
                "SELECT stage, COUNT(*) FROM tm_entries GROUP BY stage"):
            by_stage[row[0]] = row[1]
        total_hits = conn.execute(
            "SELECT SUM(hit_count) FROM tm_entries").fetchone()[0] or 0
        return {
            "total": total,
            "by_stage": by_stage,
            "total_hits": total_hits,
            "db_path": self.db_path,
        }

    def clear(self, stage: Optional[int] = None):
        """清空记忆库。stage=None 清空全部。"""
        conn = self._get_conn()
        if stage is not None:
            conn.execute("DELETE FROM tm_entries WHERE stage=?", (stage,))
        else:
            conn.execute("DELETE FROM tm_entries")
        conn.commit()

    def export_csv(self, path: str, stage: Optional[int] = None):
        """导出为 CSV"""
        import csv
        conn = self._get_conn()
        if stage is not None:
            rows = conn.execute(
                "SELECT source_text, target_text, stage, hit_count "
                "FROM tm_entries WHERE stage=? ORDER BY hit_count DESC",
                (stage,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_text, target_text, stage, hit_count "
                "FROM tm_entries ORDER BY stage, hit_count DESC").fetchall()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source", "target", "stage", "hit_count"])
            for r in rows:
                w.writerow(r)

    def import_csv(self, path: str) -> int:
        """从 CSV 导入。返回新增条数。"""
        import csv
        added = 0
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    # Skip header row
                    if row[0].strip().lower() in ("source", "原文", "original"):
                        continue
                    stage = int(row[2]) if len(row) > 2 and row[2].isdigit() else 0
                    if self.store(row[0].strip(), row[1].strip(), stage):
                        added += 1
        return added


def _char_overlap(a: str, b: str) -> float:
    """字符级重叠率（Jaccard-like on characters）。用于快速模糊匹配。"""
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0
