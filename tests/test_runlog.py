"""refine.runlog 单元测试（P0 #6：TeeWriter.close 刷盘；P3-10 补薄）"""
import os
import time
from datetime import datetime
from pathlib import Path

from subtransjav.refine import runlog
from subtransjav.refine.runlog import TeeWriter


class _NullStream:
    def write(self, s):
        return len(s)

    def flush(self):
        pass


def test_teewriter_close_flushes_underlying_file():
    class TrackingFile:
        def __init__(self):
            self.flushed = False
            self.written = []

        def write(self, s):
            self.written.append(s)

        def flush(self):
            self.flushed = True

    tf = TrackingFile()
    tw = TeeWriter(_NullStream(), tf, {'error': 0, 'warn': 0, 'failover': 0})
    tw.write("hello")
    tw.close()
    assert tf.flushed, "close() 必须 flush 底层文件，否则归档日志可能读到不完整数据"
    assert any("hello" in w for w in tf.written), "缓冲内容应写入底层文件"


# ---------------------------------------------------------------------------
# P3-10 补薄：next_log_path 同日冲突序号 / archive_error_log ⚠️ 归档 /
# write_summary 摘要块
# ---------------------------------------------------------------------------

class _FixedDatetime(datetime):
    """固定"当前时间"，消除日志命名用例的分钟翻转竞态。"""

    @classmethod
    def now(cls):  # noqa: N802 与 datetime.now 签名一致
        return datetime(2026, 9, 12, 10, 30, 15)


def test_next_log_path_same_day_conflict_uses_hhmm_seq(tmp_path, monkeypatch):
    monkeypatch.setattr(runlog, "datetime", _FixedDatetime)
    logs = str(tmp_path / "Logs")

    # 空目录：当天日期文件
    assert Path(runlog.next_log_path(logs)).name == "9-12.txt"

    # 同日第二次：追加 -HHMM
    Path(logs, "9-12.txt").write_text("x", encoding="utf-8")
    assert Path(runlog.next_log_path(logs)).name == "9-12-1030.txt"

    # 同分钟第三次起：降级为 -序号
    Path(logs, "9-12-1030.txt").write_text("x", encoding="utf-8")
    assert Path(runlog.next_log_path(logs)).name == "9-12-1030-1.txt"
    Path(logs, "9-12-1030-1.txt").write_text("x", encoding="utf-8")
    assert Path(runlog.next_log_path(logs)).name == "9-12-1030-2.txt"


def test_archive_error_log_warn_status(tmp_path):
    logs = tmp_path / "Logs"
    logs.mkdir()
    log = logs / "9-12.txt"
    log.write_text("运行内容", encoding="utf-8")

    # ⚠️ 开头状态：归档到同级 Errors/，同名同内容
    target = runlog.archive_error_log(str(log), "⚠️ 部分降级（成功1/降级1/失败0）",
                                      str(logs))
    assert target, "⚠️ 开头状态必须归档"
    t = Path(target)
    assert t.parent.name == "Errors" and t.name == "9-12.txt"
    assert t.read_text(encoding="utf-8") == "运行内容"

    # ✅ 开头状态：不归档（返回空串）
    assert runlog.archive_error_log(str(log), "✅ 成功", str(logs)) == ""

    # ❌ 开头状态：归档
    assert runlog.archive_error_log(str(log), "❌ 失败", str(logs)) != ""


def test_write_summary_appends_block(tmp_path):
    logs = tmp_path / "Logs"
    logs.mkdir()
    log_path = logs / "9-12.txt"
    log_path.write_text("[10:00:00] 正文\n", encoding="utf-8")
    with open(log_path, "a", encoding="utf-8") as f:
        runlog.write_summary(f, "✅ 成功", 65.0, 2,
                             {'error': 1, 'warn': 2, 'failover': 3},
                             str(log_path))
    text = log_path.read_text(encoding="utf-8")
    assert "[10:00:00] 正文" in text, "既有正文必须保留（追加而非覆盖）"
    assert "运行摘要" in text
    assert "状态   : ✅ 成功" in text
    assert "耗时   : 1m05s" in text
    assert "输入   : 2 个文件" in text
    assert "错误 1" in text and "警告/校验 2" in text and "故障接管 3" in text
    assert "日志   :" in text and "9-12.txt" in text
    assert "保留策略: 7 天自动清理" in text
    assert text.rstrip().endswith("=" * 52), "摘要块应以分隔条收尾"


# ---------------------------------------------------------------------------
# 1.2 H2：cleanup_old_logs 扩展 .log 清理（dropped_entries.log 及其轮转文件）
# ---------------------------------------------------------------------------

def test_cleanup_old_logs_removes_expired_txt_and_log(tmp_path):
    """超期 .txt 与 .log 都删除；保留期内的两类文件都保留"""
    logs = tmp_path / "Logs"
    logs.mkdir()
    old_txt = logs / "9-1.txt"
    old_log = logs / "dropped_entries.log"
    old_log_upper = logs / "ROTATED.LOG"       # 后缀大小写不敏感
    keep_txt = logs / "9-12.txt"
    keep_log = logs / "dropped_entries-1.log"
    other = logs / "state.json"                # 非 .txt/.log 不受影响
    for p in (old_txt, old_log, old_log_upper, keep_txt, keep_log, other):
        p.write_text("x", encoding="utf-8")
    expired = time.time() - 8 * 86400          # 保留期 7 天，伪造 8 天前
    for p in (old_txt, old_log, old_log_upper):
        os.utime(p, (expired, expired))

    removed = runlog.cleanup_old_logs(str(logs), retention_days=7)

    assert removed == 3
    assert not old_txt.exists() and not old_log.exists()
    assert not old_log_upper.exists(), ".log 大小写变体也应被清理"
    assert keep_txt.exists() and keep_log.exists() and other.exists()


def test_cleanup_old_logs_missing_dir_returns_zero(tmp_path):
    """目录不存在：静默返回 0"""
    assert runlog.cleanup_old_logs(str(tmp_path / "nope")) == 0
