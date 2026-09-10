# -*- coding: utf-8 -*-
"""refine.runlog 单元测试（P0 #6：TeeWriter.close 刷盘）"""
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
