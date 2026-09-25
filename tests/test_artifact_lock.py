"""产物级 lockfile 测试（v1.3.0 D4，D2026-0925-01 补充裁决终选三路径）。

覆盖：
1. 同输入同输出目录：第二次获取 → None（拒绝，冲突保护生效）；
2. 不同输入：两把锁并存互不误伤；
3. 平台锁函数抛 IOError：降级返回 None 不崩溃（降级不罢工）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine import artifact_lock as al  # noqa: E402


def _touch(p: Path) -> str:
    p.write_text("1\n00:00:00,000 --> 00:00:00,500\n测试\n", encoding="utf-8")
    return str(p)


def test_same_input_same_outdir_second_acquire_rejected(tmp_path):
    """同输入同输出：首获取成功，第二次获取 → None。"""
    inp = _touch(tmp_path / "ep01.srt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    h1 = al.acquire_artifact_lock(inp, str(out_dir))
    assert h1 is not None
    try:
        assert (out_dir / ".subtransjav.lock").exists() or \
            any(f.name.endswith(".subtransjav.lock")
                for f in out_dir.iterdir())
        h2 = al.acquire_artifact_lock(inp, str(out_dir))
        assert h2 is None                       # 冲突 → 拒绝
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
    """msvcrt.locking 抛 IOError → 降级返回 None，不崩溃。"""
    def _boom(fd, mode, nbytes):
        raise OSError("simulated lock contention")

    if al.msvcrt is not None:
        monkeypatch.setattr(al.msvcrt, "locking", _boom)
    else:
        monkeypatch.setattr(al, "_lock_fd_exclusive",
                            lambda fd: (_ for _ in ()).throw(OSError("x")))
    inp = _touch(tmp_path / "ep01.srt")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    h = al.acquire_artifact_lock(inp, str(out_dir))
    assert h is None                            # 降级不罢工
    # 降级路径已关闭 fd：锁文件可能残留（OS 兜底），属可接受行为
