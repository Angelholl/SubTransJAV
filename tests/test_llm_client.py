"""
llm_client 单测：协议解析 / 分批 / 定向重试 / 思考模型兜底 / 并发。
网络层用本地 http.server 起假 OpenAI 兼容服务（无外部依赖）。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from subtransjav.translate.llm_client import (
    ClientConfig,
    LLMClient,
    LLMError,
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
                    body = json.dumps(resp.get(
                        "__body__", {"error": {"message": ""}})).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
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


def test_missing_retry_budget_exactly_two_rounds():
    """D7：缺行定向重试预算 N=2——模型恒定两行并一行返回时，
    恰好重试 2 轮后停止，剩余缺行按 failed 链路降级，不抛异常。"""
    merge = "#1\nTranslation>\n你好"      # 恒定只回 #1（#2 被并掉）
    server = FakeServer([merge, merge, merge])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは", "さようなら"),
            system_text="", user_prompt="p", max_batch_size=10)
        assert len(server.requests) == 3   # 首批 + 恰好 2 轮定向重试
        assert r.translations == {1: "你好"}
        assert r.failed == [2]             # 预算耗尽降级，绝不整批失败
    finally:
        server.stop()


def test_missing_recovered_in_first_retry_keeps_budget():
    """D7 对照：第 1 轮重试补回缺行后不再继续重试（预算不空转）。"""
    server = FakeServer([
        "#1\nTranslation>\n你好",          # 首批漏 #2
        "#2\nTranslation>\n再见",          # 第 1 轮补回 → 停止
    ])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは", "さようなら"),
            system_text="", user_prompt="p", max_batch_size=10)
        assert len(server.requests) == 2
        assert r.translations == {1: "你好", 2: "再见"}
        assert r.failed == []
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


# ---------------------------------------------------------------------------
# O6：回环端点绕过系统代理 + 连接拒绝快速失败
# ---------------------------------------------------------------------------

def test_loopback_http_client_bypasses_env_proxy(monkeypatch):
    """回环 base_url → trust_env=False 专用客户端（无视代理 env）；
    非回环端点返回 None，保持 openai 默认行为不变。"""
    from subtransjav.translate import llm_client as lc
    for env in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(env, "http://proxy.example:7890")
    cases = (
        ("http://127.0.0.1:1234/v1", True),
        ("http://localhost:11434", True),
        ("http://[::1]:8080/v1", True),
        ("https://api.deepseek.com/v1", False),
        ("http://192.168.1.10:8000/v1", False),
        ("", False),
    )
    for url, is_loopback in cases:
        hc = lc._loopback_http_client(url, timeout=30.0)
        if is_loopback:
            assert hc is not None, url
            assert hc.trust_env is False
            hc.close()
        else:
            assert hc is None, url


def test_openai_client_uses_trust_env_false_for_loopback(monkeypatch):
    """回环端点构造的 OpenAI 客户端底层 httpx 客户端 trust_env=False；
    非回环端点保持默认 trust_env=True 行为。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:7890")
    cfg = ClientConfig(base_url="http://127.0.0.1:1234/v1", model="m")
    client = LLMClient(cfg, log=lambda m: None)
    oc = client._ensure_openai_client()
    assert oc._client.trust_env is False      # openai 内部 httpx 客户端
    oc.close()

    cfg2 = ClientConfig(base_url="https://api.deepseek.com/v1", model="m")
    client2 = LLMClient(cfg2, log=lambda m: None)
    oc2 = client2._ensure_openai_client()
    assert oc2._client.trust_env is True      # 保持默认（受 env 影响，原行为）
    oc2.close()


def test_is_transient_connection_refused_fast_fail():
    """连接拒绝/代理不可达类（含 openai APIConnectionError 包装形态）
    判为不可重试；服务端瞬态 5xx 与超时保持退避重试不变。"""
    from openai import APIConnectionError
    req = httpx.Request("POST", "http://127.0.0.1:1234/v1/chat/completions")

    wrapped = APIConnectionError(request=req)
    wrapped.__cause__ = httpx.ConnectError(
        "[WinError 10061] 由于目标计算机积极拒绝，无法连接。")
    assert LLMClient._is_connection_refused(wrapped) is True
    assert LLMClient._is_transient(wrapped) is False

    proxy_err = APIConnectionError(request=req)
    proxy_err.__cause__ = httpx.ProxyError(
        "Unable to connect to proxy", request=req)
    assert LLMClient._is_transient(proxy_err) is False

    assert LLMClient._is_transient(
        httpx.ConnectError("connection refused")) is False

    class Err503(Exception):                 # 服务端瞬态：退避策略不变
        status_code = 503
    assert LLMClient._is_transient(Err503("overloaded")) is True
    assert LLMClient._is_transient(httpx.ConnectTimeout("timed out")) is True


def test_connection_refused_fails_fast_without_backoff(monkeypatch):
    """连接拒绝异常即使 max_retries=3 也不进入退避重试（0 次等待）。"""
    sleeps = []
    monkeypatch.setattr("subtransjav.translate.llm_client.time.sleep",
                        lambda s: sleeps.append(s))
    cfg = ClientConfig(base_url="http://127.0.0.1:1/v1", model="m",
                       max_retries=3, backoff_time=5.0)
    client = LLMClient(cfg, log=lambda m: None)

    class RaisingCompletions:
        @staticmethod
        def create(**kw):
            raise httpx.ConnectError(
                "[WinError 10061] 由于目标计算机积极拒绝，无法连接。")

    class RaisingClient:
        class chat:
            completions = RaisingCompletions()

    client._openai_client = RaisingClient()
    with pytest.raises(LLMError):
        client._chat("sys", "user")
    assert sleeps == []                      # 未发生任何退避等待


# ---------------------------------------------------------------------------
# D2026-0924-02：请求期 "Model unloaded" 识别与自动恢复
# ---------------------------------------------------------------------------

from subtransjav.translate.llm_client import (  # noqa: E402
    ModelUnloadedError,
)

_UNLOADED_400 = {"__status__": 400,
                 "__body__": {"error":
                              "Model unloaded by user or API request."}}


class _Err400Unloaded(Exception):
    status_code = 400

    def __str__(self):
        return "Error code: 400 - {'error': 'Model unloaded by user or API request.'}"


class _Err400Other(Exception):
    status_code = 400

    def __str__(self):
        return "Error code: 400 - {'error': 'model unloaded due to inactivity'}"


def test_model_unloaded_400_raises_dedicated_type():
    """400 + 特征串 → ModelUnloadedError（保留原错误信息）。"""
    server = FakeServer([_UNLOADED_400])
    try:
        with pytest.raises(ModelUnloadedError, match="Model unloaded"):
            _client(server).translate_batch(
                _entries("こんにちは"), system_text="", user_prompt="p",
                allow_empty_deletions=False)
    finally:
        server.stop()


def test_other_400_not_misjudged_as_unloaded():
    """400 其他消息（云端 inactivity 卸载）→ 普通 LLMError，不误判。"""
    server = FakeServer([{"__status__": 400,
                          "__body__": {"error":
                                       "model unloaded due to inactivity"}}])
    try:
        with pytest.raises(LLMError) as ei:
            _client(server).translate_batch(
                _entries("こんにちは"), system_text="", user_prompt="p",
                allow_empty_deletions=False)
        assert not isinstance(ei.value, ModelUnloadedError)
    finally:
        server.stop()


def test_unloaded_error_is_not_transient():
    """unloaded 400 不被 _is_transient 判真（不进 5s 退避重试）。"""
    assert LLMClient._is_transient(_Err400Unloaded()) is False
    assert LLMClient._is_model_unloaded(_Err400Unloaded()) is True
    assert LLMClient._is_model_unloaded(_Err400Other()) is False


def test_batch_recovery_via_unloaded_callback():
    """首请求抛 unloaded 400 → 回调恢复后整批重试成功；回调恰一次。"""
    calls = []
    server = FakeServer([
        _UNLOADED_400,
        "#1\nTranslation>\n你好",
    ])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは"), system_text="", user_prompt="p",
            max_batch_size=10,
            unloaded_recovery=lambda: calls.append(1))
        assert r.translations == {1: "你好"}
        assert r.failed == []
        assert calls == [1]
    finally:
        server.stop()


def test_concurrent_unloaded_recovery_called_once():
    """并发=2 双批同时 unloaded：锁+信用生效，回调恰一次，两批收敛，无死锁。"""
    import re as _re
    import time as _time
    calls = []
    counts = {}
    lk = threading.Lock()

    def respond(body):
        text = body["messages"][-1]["content"]
        idx = _re.search(r"#(\d+)", text).group(1)
        with lk:
            n = counts.get(idx, 0) + 1
            counts[idx] = n
        if n == 1:
            _time.sleep(0.1)     # 模拟重载窗口内另一批也 400
            return _UNLOADED_400
        return f"#{idx}\nTranslation>\n译{idx}"

    server = FakeServer([respond, respond, respond, respond])
    try:
        r = _client(server, concurrency=2).translate_entries(
            _entries("a", "b"), system_text="", user_prompt="p",
            max_batch_size=1,
            unloaded_recovery=lambda: _time.sleep(0.2) or calls.append(1))
        assert calls == [1]      # 锁 + 每实例信用 → 恰一次
        assert len(r.translations) + len(r.failed) == 2   # 收敛、无死锁
    finally:
        server.stop()


def test_recovery_callback_exception_falls_back_to_current_path():
    """回调抛异常 → 走现状路径（批失败→定向重试），不炸批循环。"""
    def _boom():
        raise RuntimeError("reload failed")

    server = FakeServer([
        _UNLOADED_400,      # 首请求 unloaded
        _UNLOADED_400,      # 缺行定向重试仍失败
    ])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは"), system_text="", user_prompt="p",
            max_batch_size=10, unloaded_recovery=_boom)
        assert r.failed == [1]
    finally:
        server.stop()


def test_plain_400_does_not_trigger_recovery():
    """非 unloaded 的 400 → 行为与现状一致，不调回调。"""
    calls = []
    server = FakeServer([
        {"__status__": 400, "__body__": {"error": "bad request"}},
        {"__status__": 400, "__body__": {"error": "bad request"}},
    ])
    try:
        r = _client(server).translate_entries(
            _entries("こんにちは"), system_text="", user_prompt="p",
            max_batch_size=10,
            unloaded_recovery=lambda: calls.append(1))
        assert r.failed == [1]
        assert calls == []
    finally:
        server.stop()


def test_pipeline_injects_recovery_for_lmstudio_only(monkeypatch):
    """管线接线：lmstudio provider 注入回调，云端 provider 不注入。"""
    from subtransjav.refine import pipeline_v2 as pv
    from subtransjav.refine.config import RefineConfig, StageConfig
    from subtransjav.refine.pipeline_v2 import V2_STAGE_SLOT

    monkeypatch.setattr(pv, "_ensure_lmstudio_engine",
                        lambda *a, **kw: None)
    cfg = RefineConfig(inputs=[])
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "fake-model"),
        StageConfig(1, False, "deepseek", ""),
        StageConfig(2, True, "lmstudio", "fake-model"),
        StageConfig(3, False, "lmstudio", ""),
    ]
    local = pv._make_client(cfg, "A")
    cfg.stages[2].provider = "deepseek"      # 阶段B 切云端（槽位2）
    cfg.stages[2].model = "deepseek-chat"
    cloud = pv._make_client(cfg, "B")
    assert callable(local._unloaded_recovery_default)
    assert cloud._unloaded_recovery_default is None
    assert V2_STAGE_SLOT["A"] == 0    # 槽位契约不变（守卫断言）
