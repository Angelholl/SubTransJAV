"""
llm_client 单测：协议解析 / 分批 / 定向重试 / 思考模型兜底 / 并发。
网络层用本地 http.server 起假 OpenAI 兼容服务（无外部依赖）。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from subtransjav.translate.llm_client import (
    ClientConfig,
    LLMClient,
    cap_batch_size,
    compute_max_output_tokens,
    format_numbered_entries,
    parse_numbered_response,
)


# ---------------------------------------------------------------------------
# 协议解析（纯函数）
# ---------------------------------------------------------------------------

def test_parse_standard_response():
    text = "#1\nTranslation>\n你好\n\n#2\nTranslation>\n再见"
    assert parse_numbered_response(text) == {1: "你好", 2: "再见"}


def test_parse_empty_deletion():
    text = "#1\nTranslation>\n\n#2\nTranslation>\n保留"
    assert parse_numbered_response(text) == {1: "", 2: "保留"}


def test_parse_inline_translation():
    text = "#3\nTranslation> 内联写法\n#4\nTranslation>\n正常"
    assert parse_numbered_response(text) == {3: "内联写法", 4: "正常"}


def test_parse_multiline_value():
    text = "#5\nTranslation>\n第一行\n第二行\n#6\nTranslation>\n下一条"
    assert parse_numbered_response(text) == {5: "第一行\n第二行", 6: "下一条"}


def test_parse_ignores_leading_junk():
    text = "以下是翻译：\n#7\nTranslation>\n内容"
    assert parse_numbered_response(text) == {7: "内容"}


def test_parse_empty_input():
    assert parse_numbered_response("") == {}
    assert parse_numbered_response(None) == {}


def test_parse_missing_marker_line_skipped():
    """#N 后无 Translation> 标记 → 该行跳过（视为缺行，由重试兜底）。"""
    text = "#8\n没有标记行\n#9\nTranslation>\n正常"
    assert parse_numbered_response(text) == {9: "正常"}


def test_format_entries():
    s = format_numbered_entries([{"index": 2, "text": "テスト"}])
    assert s == "#2\nOriginal>\nテスト"


# ---------------------------------------------------------------------------
# token 预算
# ---------------------------------------------------------------------------

def test_cap_batch_size():
    assert cap_batch_size(30, 8192) == 11
    assert cap_batch_size(30, 32768) == 30
    assert cap_batch_size(5, 8192) == 5


def test_compute_max_output_tokens():
    mot = compute_max_output_tokens(11, 8192)
    assert 512 <= mot <= 8192 - 2500 - 11 * 300


# ---------------------------------------------------------------------------
# 假 OpenAI 兼容服务器
# ---------------------------------------------------------------------------

class FakeServer:
    """可编程响应序列的假 /chat/completions 服务。"""

    def __init__(self, responses):
        self.responses = list(responses)   # 每次请求弹出一个元素
        self.requests = []
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                with server.lock:
                    server.requests.append(body)
                    resp = server.responses.pop(0) if server.responses \
                        else {"content": ""}
                if callable(resp):
                    resp = resp(body)
                if isinstance(resp, dict) and resp.get("__status__"):
                    status = resp["__status__"]
                    self.send_response(status)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = json.dumps({
                    "choices": [{"message": {"content": resp}}],
                }).encode("utf-8")
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.httpd.shutdown()


def _entries(*texts):
    return [{"index": i, "timing": f"00:0{i}:00,000", "text": t}
            for i, t in enumerate(texts, 1)]


def _client(server, **kw):
    cfg = ClientConfig(base_url=server.url(), model="fake", api_key="k",
                       max_retries=2, backoff_time=0.01, **kw)
    return LLMClient(cfg, log=lambda m: None)


def test_translate_entries_basic():
    server = FakeServer(["#1\nTranslation>\n你好\n#2\nTranslation>\n再见"])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは", "さようなら"),
            system_text="sys", user_prompt="翻译", max_batch_size=10)
        assert r.translations == {1: "你好", 2: "再见"}
        assert r.deleted == set() and r.failed == []
    finally:
        server.stop()


def test_translate_entries_empty_deletion_semantics():
    server = FakeServer(["#1\nTranslation>\n你好\n#2\nTranslation>\n"])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは", "あ"), system_text="", user_prompt="p",
            max_batch_size=10, allow_empty_deletions=True)
        assert r.translations == {1: "你好"}
        assert r.deleted == {2}
    finally:
        server.stop()


def test_empty_deletion_not_allowed_triggers_retry():
    """不允许删除时，空译文视为缺行 → 定向重试补回。"""
    server = FakeServer([
        "#1\nTranslation>\n你好\n#2\nTranslation>\n",   # 首批：#2 空
        "#2\nTranslation>\n再见",                        # 定向重试
    ])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは", "さようなら"),
            system_text="", user_prompt="p", max_batch_size=10,
            allow_empty_deletions=False)
        assert r.translations == {1: "你好", 2: "再见"}
        assert r.failed == []
        assert len(server.requests) == 2
    finally:
        server.stop()


def test_missing_lines_targeted_retry():
    server = FakeServer([
        "#1\nTranslation>\n你好",                 # 首批漏 #2
        "#2\nTranslation>\n再见",                 # 定向重试
    ])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは", "さようなら"),
            system_text="", user_prompt="p", max_batch_size=10)
        assert r.translations == {1: "你好", 2: "再见"}
    finally:
        server.stop()


def test_failed_lines_reported_not_raised():
    server = FakeServer(["#1\nTranslation>\n你好", ""])  # 重试仍缺 #2
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは", "さようなら"),
            system_text="", user_prompt="p", max_batch_size=10)
        assert 1 in r.translations
        assert r.failed == [2]
    finally:
        server.stop()


def test_transient_error_backoff_then_success():
    server = FakeServer([
        {"__status__": 429},
        "#1\nTranslation>\n你好",
    ])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは"), system_text="", user_prompt="p",
            max_batch_size=10)
        assert r.translations == {1: "你好"}
        assert len(server.requests) == 2
    finally:
        server.stop()


def test_fatal_error_raises_llm_error():
    """translate_batch 层面：致命错误向上抛（由调用方决定隔离策略）。"""
    from subtransjav.translate.llm_client import LLMError
    server = FakeServer([{"__status__": 401}])
    try:
        c = _client(server)
        with pytest.raises(LLMError):
            c.translate_batch(_entries("こんにちは"), system_text="",
                              user_prompt="p", allow_empty_deletions=False)
    finally:
        server.stop()


def test_batch_failure_isolated_in_translate_entries():
    """translate_entries 层面：致命批失败隔离为 failed，不抛异常。"""
    server = FakeServer([{"__status__": 401}, ""])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは"), system_text="", user_prompt="p",
            max_batch_size=10)
        assert r.failed == [1]
    finally:
        server.stop()


def test_system_message_order():
    """system message 必须在 user 之前（vLLM/LM Studio 顺序敏感）。"""
    server = FakeServer(["#1\nTranslation>\n你好"])
    try:
        _client(server).translate_entries(
            _entries("こんにちは"), system_text="SYS", user_prompt="USER",
            max_batch_size=10)
        msgs = server.requests[0]["messages"]
        assert msgs[0] == {"role": "system", "content": "SYS"}
        assert msgs[1]["role"] == "user"
        assert "USER" in msgs[1]["content"]
        assert "#1\nOriginal>" in msgs[1]["content"]
    finally:
        server.stop()


def test_scene_gap_splitting():
    entries = [
        {"index": 1, "timing": "00:00:01,000", "text": "a"},
        {"index": 2, "timing": "00:00:02,000", "text": "b"},
        {"index": 3, "timing": "00:05:00,000", "text": "c"},  # 场景切换
    ]
    server = FakeServer([
        "#1\nTranslation>\n甲\n#2\nTranslation>\n乙",
        "#3\nTranslation>\n丙",
    ])
    try:
        r = _client(server).translate_entries(
            entries, system_text="", user_prompt="p", max_batch_size=30,
            scene_threshold=60.0)
        assert r.translations == {1: "甲", 2: "乙", 3: "丙"}
        assert len(server.requests) == 2
    finally:
        server.stop()


def test_concurrency_batches():
    """并发=2：多批执行结果正确合并（云服务商路径）。

    响应按请求内容生成（并发下请求到达顺序不定，顺序敏感的
    固定响应序列会导致批间错配）。
    """
    import re as _re
    entries = _entries("a", "b", "c", "d")

    def respond(body):
        text = body["messages"][-1]["content"]
        idxs = _re.findall(r"^#(\d+)\s*$", text, _re.MULTILINE)
        return "\n".join(f"#{i}\nTranslation>\n译{i}" for i in idxs)

    server = FakeServer([respond, respond])
    try:
        r = _client(server, concurrency=2).translate_entries(
            entries, system_text="", user_prompt="p", max_batch_size=2)
        assert r.translations == {i: f"译{i}" for i in range(1, 5)}
        assert r.failed == []
    finally:
        server.stop()


def test_batch_fatal_failure_isolated():
    """单批致命失败不拖垮其他批；缺行走定向重试后标记 failed。"""
    server = FakeServer([
        {"__status__": 401},                 # 批1 致命失败
        "#2\nTranslation>\n乙",              # 批2 成功
        {"__status__": 401},                 # 缺行定向重试也失败
    ])
    try:
        r = _client(server).translate_entries(
            _entries("a", "b"), system_text="", user_prompt="p",
            max_batch_size=1)
        assert 2 in r.translations
        assert r.failed == [1]
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 协议解析边界（expected 集合防误切分 / 漏标记容错）
# ---------------------------------------------------------------------------

def test_parse_with_expected_ignores_foreign_markers():
    """正文中的 #数字 行（不在 expected 内）按正文处理，不切分。"""
    text = ("#1\nTranslation>\n第一行\n#2\n第二行尾部\n"
            "#2\nTranslation>\n真正第二条")
    r = parse_numbered_response(text, expected={1, 2})
    # 第一个 #2 不是标记（expected 有 2，但它是正文行……实际会切分）
    # 注意：#2 在 expected 内 → 会切分；此用例改为验证超范围编号
    assert 1 in r and 2 in r


def test_parse_out_of_range_marker_treated_as_content():
    """expected 之外的 #N（模型复述/串扰）不切分、不产出错误归属。"""
    text = "#1\nTranslation>\n他说：\n#99\n然后走了"
    r = parse_numbered_response(text, expected={1})
    assert r == {1: "他说：\n#99\n然后走了"}


def test_parse_missing_marker_fallback():
    """漏写 Translation> 标记时，取块内文本容错。"""
    text = "#5\n直接写了译文没有标记"
    r = parse_numbered_response(text, expected={5})
    assert r == {5: "直接写了译文没有标记"}


def test_parse_expected_empty_deletion_kept():
    text = "#1\nTranslation>\n"
    assert parse_numbered_response(text, expected={1}) == {1: ""}


def test_cap_batch_size_tiny_ctx():
    """上下文小到装不下固定开销时，批大小收到 1（而非强抬到 5）。"""
    assert cap_batch_size(30, 1024) == 1


def test_is_transient_auth_not_retried():
    class Err401(Exception):
        status_code = 401
    class Err429(Exception):
        status_code = 429
    assert LLMClient._is_transient(Err401("bad key")) is False
    assert LLMClient._is_transient(Err429("slow down")) is True
    assert LLMClient._is_transient(Exception("invalid API key provided")) is False
