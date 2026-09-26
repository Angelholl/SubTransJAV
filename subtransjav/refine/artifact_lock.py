"""产物级 lockfile（v1.3.0 D4，D2026-0925-01 补充裁决终选）
====================================================

同一 (输入文件 sha1, output_dir) 组合同一时刻只允许一个进程写产物：
锁文件落 output_dir（``.{input_sha1[:16]}.subtransjav.lock``），
锁住首字节即代表占用。

- 三态语义（D4/M2）：冲突（他方持锁）→ 抛 ArtifactLockConflict，由
  调用方转 RefineError 走单文件失败隔离；锁机制不可用（锁文件创建
  失败/加锁 IO 故障/平台无实现）→ 打印警告返回 None，调用方降级
  无锁继续，不得拒绝处理该文件；
- 进程意外退出时 OS 自动释放锁（msvcrt.locking 语义：锁随句柄/进程
  终止而失效），不遗留死锁；
- 跨平台：Windows 用 msvcrt.locking；非 Windows 回退 fcntl.flock
  （try import，两者皆缺则模块退化为"永不锁"，仅告警一次）。
  测试环境可 monkeypatch 平台锁函数模拟冲突/降级路径。
"""

import contextlib
import errno
import hashlib
import os
from pathlib import Path

try:                                    # Windows 首选
    import msvcrt
except ImportError:                     # 非 Windows 回退 fcntl
    msvcrt = None
    try:
        import fcntl
    except ImportError:
        fcntl = None

_warned_no_lock_impl = False


class ArtifactLockConflict(RuntimeError):
    """同键锁被其他进程持有（冲突拒绝，区别于机制故障降级）。"""


# 冲突 errno 集：Windows msvcrt.locking 对已锁区域抛 EACCES/EDEADLOCK；
# POSIX flock 非阻塞抛 EAGAIN(EWOULDBLOCK)。锁文件能 open 成功，则
# EACCES 只能来自他方持锁，不可能是自身权限问题。
_CONFLICT_ERRNOS = {getattr(errno, n, None)
                    for n in ("EACCES", "EAGAIN", "EWOULDBLOCK",
                              "EDEADLOCK", "EDEADLK")} - {None}


class ArtifactLockHandle:
    """锁句柄：持有打开的锁文件描述符，close() 释放锁并关闭、
    尽力删除锁文件（被他人接管时删除失败忽略）。"""

    def __init__(self, path: str, fd):
        self.path = path
        self._fd = fd
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if msvcrt is not None:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                # Windows 存根无 fcntl 符号，仅 POSIX 运行时走到（下行忽略 attr-defined）
                fcntl.flock(self._fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except OSError:
            pass                        # 释放失败不阻断：OS 兜底随进程回收
        finally:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            with contextlib.suppress(OSError):
                os.remove(self.path)

    # 上下文管理器糖衣
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _lock_fd_exclusive(fd) -> None:
    """对 fd 首字节加独占锁（阻塞语义由调用方 nonblocking 标志决定）。

    Windows: msvcrt.locking LK_NBLCK（非阻塞独占，冲突抛 OSError）；
    POSIX:   fcntl.flock LOCK_EX | LOCK_NB。
    errno 命中冲突集（他方持锁）→ 翻译为 ArtifactLockConflict；
    其余 IO 故障原样上抛，由调用方按机制故障降级。
    """
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in _CONFLICT_ERRNOS:
                raise ArtifactLockConflict(
                    "产物锁被其他进程持有（首字节已锁定）") from e
            raise
    elif fcntl is not None:
        # Windows 存根无 fcntl 符号，仅 POSIX 运行时走到（下行忽略 attr-defined）
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError as e:
            if e.errno in _CONFLICT_ERRNOS:
                raise ArtifactLockConflict(
                    "产物锁被其他进程持有（非阻塞 flock 被拒）") from e
            raise
    else:
        raise RuntimeError("no lock implementation")


def acquire_artifact_lock(input_path: str, output_dir: str):
    """获取产物锁（三态契约）。

    - 返回 ArtifactLockHandle：持锁成功；
    - 返回 None：锁机制不可用（锁文件创建失败/加锁 IO 故障/平台无
      实现），已打印降级警告——调用方应无锁继续，不得拒绝处理该文件；
    - 同键冲突（他方持锁）：抛 ArtifactLockConflict，调用方转
      RefineError 走单文件失败隔离（本模块不打印冲突文案，消息由
      调用方统一输出）。

    锁键 = 输入文件 sha1 前 16 位 + output_dir（锁文件物理落在
    output_dir 内，天然按输出目录分域）。
    """
    global _warned_no_lock_impl
    in_sha1 = hashlib.sha1(
        Path(input_path).resolve().as_posix().lower().encode("utf-8")
    ).hexdigest()
    lock_path = str(Path(output_dir) / f".{in_sha1[:16]}.subtransjav.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError as e:
        print(f"⚠️ [lock] 产物锁文件创建失败（降级不锁，继续处理）: "
              f"{lock_path} ({e})")
        return None
    try:
        _lock_fd_exclusive(fd)
    except ArtifactLockConflict:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    except OSError as e:
        # 机制故障（非冲突 IO 错误）→ 降级不罢工
        with contextlib.suppress(OSError):
            os.close(fd)
        print(f"⚠️ [lock] 产物锁加锁 IO 故障（降级不锁，继续处理）: "
              f"{lock_path} ({e})")
        return None
    except RuntimeError:
        with contextlib.suppress(OSError):
            os.close(fd)
        if not _warned_no_lock_impl:
            _warned_no_lock_impl = True
            print("⚠️ [lock] 当前平台无 msvcrt/fcntl，产物锁降级为不锁")
        return None
    return ArtifactLockHandle(lock_path, fd)


def release_artifact_lock(handle) -> None:
    """释放锁句柄（幂等；None 安全）。"""
    if handle is not None:
        handle.release()
