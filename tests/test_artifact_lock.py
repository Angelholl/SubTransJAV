"""产物级 lockfile 测试（v1.3.0 D4，D2026-0925-01 补充裁决终选三路径；
M2 三态语义区分：冲突=抛 ArtifactLockConflict，机制故障=降级返回 None）。

覆盖：
1. 同输入同输出目录：第二次获取 → ArtifactLockConflict（冲突拒绝），
   释放后可重获；
2. 不同输入：两把锁并存互不误伤；
3. 平台锁函数抛非冲突 OSError（EIO）：降级返回 None 不崩溃（降级不罢工）；
4. 平台锁函数抛 EACCES（他方持锁）：翻译为 ArtifactLockConflict；
5. 平台无 msvcrt/fcntl：降级返回 None。
"""
import errno
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine import artifact_lock as al  # noqa: E402


def _touch(p: Path) -> str:
    p.write_text("1\n00:00:00,000 --> 00:00:00,500\n测试\n", encoding="utf-8")
    return str(p)


def test_same_input_same_outdir_second_acquire_rejected(tmp_path):
    """同输入同输出：首获取成功，第二次获取 → ArtifactLockConflict。"""
    inp = _touch(tmp_path / "ep01.srt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    h1 = al.acquire_artifact_lock(inp, str(out_dir))
    assert h1 is not None
    try:
        assert (out_dir / ".subtransjav.lock").exists() or \
            any(f.name.endswith(".subtransjav.lock")
                for f in out_dir.iterdir())
        with pytest.raises(al.ArtifactLockConflict):
            al.acquire_artifact_lock(inp, str(out_dir))   # 冲突 → 拒绝
    finally:
        al.release_artifact_lock(h1)
    # 释放后可重新获取（锁文件已清理）
    h3 = al.acquire_artifact_lock(inp, str(out_dir))
    assert h3 is not None
    al.release_artifact_lock(h3)


def test_different_inputs_locks_coexist(tmp_path):
    """不同输入不误伤：两把锁并存，互不冲突。"""
    in1 = _touch(tmp_path / "ep01.srt")
    in2 = _touch(tmp_path / "ep02.srt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    h1 = al.acquire_artifact_lock(in1, str(out_dir))
    h2 = al.acquire_artifact_lock(in2, str(out_dir))
    try:
        assert h1 is not None and h2 is not None
        assert h1.path != h2.path
    finally:
        al.release_artifact_lock(h1)
        al.release_artifact_lock(h2)


def test_locking_ioerror_degrades_to_none(tmp_path, monkeypatch):
    """msvcrt.locking 抛非冲突 OSError(EIO) → 机制故障降级返回 None。"""
    def _boom(fd, mode, nbytes):
        raise OSError(errno.EIO, "simulated IO failure")

    if al.msvcrt is not None:
        monkeypatch.setattr(al.msvcrt, "locking", _boom)
    else:
        monkeypatch.setattr(al, "_lock_fd_exclusive",
                            lambda fd: (_ for _ in ()).throw(
                                OSError(errno.EIO, "x")))
    inp = _touch(tmp_path / "ep01.srt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    h = al.acquire_artifact_lock(inp, str(out_dir))
    assert h is None                            # 降级不罢工
    # 降级路径已关闭 fd：锁文件可能残留（OS 兜底），属可接受行为


def test_lock_eacces_raises_conflict(tmp_path, monkeypatch):
    """平台锁函数抛 EACCES（他方持锁）→ ArtifactLockConflict（冲突拒绝）。

    有 msvcrt 时 patch al.msvcrt.locking 抛 OSError(EACCES)，走真实
    _lock_fd_exclusive 的 errno 翻译；无 msvcrt 平台该翻译在
    _lock_fd_exclusive 内部，patch 该函数时直接抛翻译后的
    ArtifactLockConflict（即 EACCES 场景下 acquire 收到的异常）。
    两分支均不依赖真实平台，Windows 本地与 ubuntu CI 皆可跑。
    """
    def _boom(fd, mode, nbytes):
        raise OSError(errno.EACCES, "simulated lock contention")

    if al.msvcrt is not None:
        monkeypatch.setattr(al.msvcrt, "locking", _boom)
    else:
        monkeypatch.setattr(al, "_lock_fd_exclusive",
                            lambda fd: (_ for _ in ()).throw(
                                al.ArtifactLockConflict("产物锁被其他进程持有")))
    inp = _touch(tmp_path / "ep01.srt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(al.ArtifactLockConflict):
        al.acquire_artifact_lock(inp, str(out_dir))


def test_no_lock_impl_degrades_to_none(tmp_path, monkeypatch):
    """平台无 msvcrt/fcntl → 降级返回 None，不抛异常。"""
    monkeypatch.setattr(al, "msvcrt", None)
    # 非 Windows 才有 fcntl 属性：raising=False 兼容 Windows 本地与 CI
    monkeypatch.setattr(al, "fcntl", None, raising=False)
    inp = _touch(tmp_path / "ep01.srt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    h = al.acquire_artifact_lock(inp, str(out_dir))
    assert h is None                            # 降级不罢工
