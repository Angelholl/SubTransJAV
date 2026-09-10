"""
运行日志（Run Log）
====================
每次 refine 任务自动生成一个全量日志文件：
  - 位置: 项目根目录/Logs/
  - 命名: 以日期命名（M-D.txt）；同日再跑则追加 24 小时制时间
    （M-D-HHMM.txt，同分钟冲突时降级到秒/序号）
  - 内容: 子进程全部 stdout/stderr 输出，逐行带 [HH:MM:SS] 时间戳
  - 结尾: 自动追加"运行摘要"（状态/耗时/输入数/错误统计）
  - 清理: 启动时自动删除 7 天前的旧日志
"""

import os
import time
from datetime import datetime

LOG_RETENTION_DAYS = 7


def _stamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


class TeeWriter:
    """同时写终端与日志文件；按行给日志内容加时间戳。"""

    _MARK_ERROR = ('ERROR', 'Traceback', '执行失败', 'Translation FAILED')
    _MARK_WARN = ('failed validation', 'NO MATCHES', '⚠️')
    _MARK_FAILOVER = ('[failover]',)

    def __init__(self, original, fileobj, counts: dict = None):
        self._original = original
        self._file = fileobj
        self._buf = ""
        self.counts = counts if counts is not None \
            else {'error': 0, 'warn': 0, 'failover': 0}

    # ------------------------------------------------------------------
    def write(self, s):
        try:
            self._original.write(s)
        except Exception:
            pass
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            self._write_line(line)
        return len(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            self._file.flush()
        except Exception:
            pass

    def close(self):
        if self._buf:
            self._write_line(self._buf)
            self._buf = ""
        self.flush()

    # ------------------------------------------------------------------
    def _write_line(self, line: str):
        try:
            if line.strip():
                self._classify(line)
                self._file.write(f"{_stamp()} {line}\n")
            else:
                self._file.write("\n")
        except Exception:
            pass

    def _classify(self, line: str):
        if any(m in line for m in self._MARK_FAILOVER):
            self.counts['failover'] += 1
        elif any(m in line for m in self._MARK_ERROR):
            self.counts['error'] += 1
        elif any(m in line for m in self._MARK_WARN):
            self.counts['warn'] += 1


def next_log_path(logs_dir: str) -> str:
    """生成日志文件路径：日期优先，同日追加 HHMM，冲突降级秒/序号。"""
    os.makedirs(logs_dir, exist_ok=True)
    now = datetime.now()
    base = f"{now.month}-{now.day}"
    candidate = os.path.join(logs_dir, f"{base}.txt")
    if not os.path.exists(candidate):
        return candidate
    hhmm = now.strftime("%H%M")
    candidate = os.path.join(logs_dir, f"{base}-{hhmm}.txt")
    if not os.path.exists(candidate):
        return candidate
    seq = 1
    while True:
        candidate = os.path.join(
            logs_dir, f"{base}-{hhmm}-{seq}.txt")
        if not os.path.exists(candidate):
            return candidate
        seq += 1


def cleanup_old_logs(logs_dir: str, retention_days: int = LOG_RETENTION_DAYS) -> int:
    """删除超过保留期的 .txt 日志；返回删除数量（静默容错）。"""
    if not os.path.isdir(logs_dir):
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    try:
        for name in os.listdir(logs_dir):
            if not name.lower().endswith('.txt'):
                continue
            path = os.path.join(logs_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def errors_dir(logs_dir: str) -> str:
    """Errors 目录：与 Logs 同级（项目根/Errors）。"""
    return os.path.join(os.path.dirname(os.path.abspath(logs_dir)), "Errors")


def archive_error_log(log_path: str, status: str, logs_dir: str) -> str:
    """失败/中断的运行日志归档到 Errors/（便于与正常日志区分排查）。

    返回归档路径；状态正常或归档失败时返回空串。
    """
    if not status.startswith(("❌", "⚠️")):
        return ""
    try:
        err_dir = errors_dir(logs_dir)
        os.makedirs(err_dir, exist_ok=True)
        target = os.path.join(err_dir, os.path.basename(log_path))
        if os.path.exists(target):
            # 同名冲突（同分钟多次失败）：加序号
            base, ext = os.path.splitext(target)
            seq = 1
            while os.path.exists(f"{base}-{seq}{ext}"):
                seq += 1
            target = f"{base}-{seq}{ext}"
        with open(log_path, 'r', encoding='utf-8') as src, \
                open(target, 'w', encoding='utf-8') as dst:
            dst.write(src.read())
        return target
    except OSError:
        return ""


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s")


def write_summary(f, status: str, elapsed: float, inputs: int,
                  counts: dict, log_path: str):
    """在日志末尾追加运行摘要块。"""
    bar = '=' * 52
    lines = [
        "", "", bar, " 运行摘要", bar,
        f" 状态   : {status}",
        f" 耗时   : {format_duration(elapsed)}",
        f" 输入   : {inputs} 个文件",
        f" 统计   : 错误 {counts.get('error', 0)} · "
        f"警告/校验 {counts.get('warn', 0)} · "
        f"故障接管 {counts.get('failover', 0)}",
        f" 日志   : {log_path}",
        f" 保留策略: {LOG_RETENTION_DAYS} 天自动清理", bar,
    ]
    text = "\n".join(lines) + "\n"
    f.write(text)
    try:
        f.flush()
    except Exception:
        pass
