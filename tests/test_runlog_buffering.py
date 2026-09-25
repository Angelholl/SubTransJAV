"""运行日志行缓冲回归测试（v1.3.1 7c，D2026-0925-02 D7c）。

背景：cli.py 的长生命周期日志句柄原为默认块缓冲，进程被硬杀
（TerminateProcess，不跑 finally/close）时已写日志整块丢失。
终选修复：open(..., buffering=1) 行缓冲，覆盖 TeeWriter._write_line
与 write_summary 两条直写路径。

结案口径（分层）：
- 缓冲丢失已消除：每行写后即落盘，第二句柄不经 close 即可读到；
- 硬杀缺运行摘要 = 设计内：TerminateProcess 不执行 finally，
  末尾 write_summary 无法补救，属可接受损耗。

测试面：不直测底层 open 语义，而是按 cli.py:334 同参数打开句柄，
经 TeeWriter 与 write_summary 真实写路径各写一行，断言第二只读句柄
（同进程显式 encoding='utf-8' 只读打开，critic 预授权备选口径）
不经 close 即读到该行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine.runlog import TeeWriter, write_summary  # noqa: E402


def _open_like_cli(tmp_path):
    """复刻 cli.py:334 的日志打开参数（含 buffering=1 修复）。"""
    log_path = tmp_path / "log.txt"
    return log_path, open(log_path, 'w', encoding='utf-8', buffering=1)


def test_tee_writer_line_visible_without_close(tmp_path):
    """TeeWriter._write_line 路径：写一行后第二句柄只读即得，不经 close。"""
    log_path, log_file = _open_like_cli(tmp_path)
    counts = {'error': 0, 'warn': 0, 'failover': 0}
    tee = TeeWriter(_StubStream(), log_file, counts)
    try:
        tee.write("第一行输出\n")
        with open(log_path, encoding='utf-8') as reader:
            content = reader.read()
        assert "第一行输出" in content
    finally:
        log_file.close()


def test_write_summary_visible_without_close(tmp_path):
    """write_summary 直写路径：摘要行写后第二句柄只读即得。"""
    log_path, log_file = _open_like_cli(tmp_path)
    try:
        write_summary(log_file, "✅ 成功", 1.0, 3,
                      {'error': 0, 'warn': 0, 'failover': 0},
                      str(log_path))
        with open(log_path, encoding='utf-8') as reader:
            content = reader.read()
        assert "运行摘要" in content
        assert "✅ 成功" in content
    finally:
        log_file.close()


def test_buffering1_line_flushed_per_line(tmp_path):
    """buffering=1 语义本身：逐行 write 后每行即时可读（对照面）。"""
    log_path, log_file = _open_like_cli(tmp_path)
    try:
        log_file.write("第一行\n")
        with open(log_path, encoding='utf-8') as reader:
            assert reader.read() == "第一行\n"
        log_file.write("第二行\n")
        with open(log_path, encoding='utf-8') as reader:
            assert reader.read() == "第一行\n第二行\n"
    finally:
        log_file.close()


class _StubStream:
    """替代真实 stdout 的哑流。"""

    def __init__(self):
        self.chunks = []

    def write(self, s):
        self.chunks.append(s)
        return len(s)

    def flush(self):
        pass
