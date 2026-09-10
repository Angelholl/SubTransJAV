# -*- coding: utf-8 -*-
"""refine.glossary_learn 单元测试（P0 #5：原子写；P1 #3：词库写锁）"""
import os

from subtransjav.refine.glossary_learn import (
    _acquire_glossary_lock,
    _release_glossary_lock,
    load_learned_glossary,
    save_learned_glossary,
)


def test_save_load_roundtrip(tmp_path):
    p = str(tmp_path / "learned.csv")
    rows = [("むらむら", "心痒"), ("IKU", "去了")]
    save_learned_glossary(p, rows)
    assert load_learned_glossary(p) == rows


def test_save_dedup(tmp_path):
    p = str(tmp_path / "learned.csv")
    save_learned_glossary(p, [("a", "b"), ("a", "b"), ("c", "d")])
    assert load_learned_glossary(p) == [("a", "b"), ("c", "d")]


def test_save_no_temp_leftover(tmp_path):
    p = str(tmp_path / "learned.csv")
    save_learned_glossary(p, [("a", "b")])
    leftovers = [n for n in os.listdir(str(tmp_path)) if n != "learned.csv"]
    assert leftovers == [], "原子写不应残留临时文件"


def test_lock_acquire_and_release(tmp_path):
    p = str(tmp_path / "learned.csv")
    lock = _acquire_glossary_lock(p)
    assert lock is not None
    assert os.path.isfile(lock), "应创建锁文件"
    _release_glossary_lock(lock)
    assert not os.path.isfile(lock), "释放后锁文件应被移除"


def test_lock_prevents_second_acquisition(tmp_path):
    p = str(tmp_path / "learned.csv")
    lock = _acquire_glossary_lock(p)
    assert lock is not None
    # 同一路径在持有锁期间再次获取应超时返回 None
    assert _acquire_glossary_lock(p, timeout=0.2) is None
    _release_glossary_lock(lock)


def test_lock_stale_cleared(tmp_path):
    p = str(tmp_path / "learned.csv")
    lock = _acquire_glossary_lock(p)
    # 人为将锁文件 mtime 设为很久以前，模拟崩溃残留的陈旧锁
    old = 0
    os.utime(lock, (old, old))
    _release_glossary_lock(lock)
    # 陈旧锁已被清除，应能再次获取
    lock2 = _acquire_glossary_lock(p, timeout=1.0)
    assert lock2 is not None
    _release_glossary_lock(lock2)
