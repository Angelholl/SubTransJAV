"""ensure_lmstudio_model 引擎对齐逻辑单测（全部 mock，不触网、不依赖 lms）。

fake 服务端维护共享可变状态：unload 清空已载集、load 把目标加回已载集——
与真实 LM Studio 的状态迁移一致，保证"加载后复核"环节可测。
"""

import json
import types

import pytest

from subtransjav.utils import lmstudio as lm


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeRequests:
    def __init__(self, v1_ids, v0):
        self.v1_ids = set(v1_ids)       # 已加载集（被 fake_run 联动增删）
        self.v0 = v0

    def get(self, url, timeout=None):
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": i} for i in sorted(self.v1_ids)]})
        return _Resp(self.v0)


class _FakeRun:
    def __init__(self, req, ps_payload='{"data": []}', fail_load=False,
                 ps_timeout=False):
        self.req = req
        self.calls = []
        self.ps_payload = ps_payload
        self.fail_load = fail_load
        self.ps_timeout = ps_timeout

    def __call__(self, args, capture_output, text, timeout):
        self.calls.append(list(args))
        sub = args[1:2]
        if sub == ["ps"]:
            if self.ps_timeout:
                raise lm.subprocess.TimeoutExpired(cmd="lms ps", timeout=timeout)
            return types.SimpleNamespace(returncode=0, stdout=self.ps_payload,
                                         stderr="")
        if sub == ["unload"]:
            self.req.v1_ids.clear()
        if sub == ["load"] and not self.fail_load:
            self.req.v1_ids.add(args[2])
        rc = 1 if (self.fail_load and sub == ["load"]) else 0
        return types.SimpleNamespace(returncode=rc, stdout="out", stderr="err")


@pytest.fixture
def env(monkeypatch):
    """装好 fake requests/subprocess/_find_lms 的沙箱，返回安装器。"""

    def _install(v1_ids=(), v0=None, **fake_run_kw):
        fr = _FakeRequests(v1_ids, v0 if v0 is not None else {"data": []})
        run = _FakeRun(fr, **fake_run_kw)
        monkeypatch.setattr(lm, "requests", fr)
        monkeypatch.setattr(lm.subprocess, "run", run)
        monkeypatch.setattr(lm, "_find_lms", lambda: "lms-fake")
        return fr, run

    return _install


EP = "http://localhost:1234/v1"
V0_M1 = {"data": [{"id": "m1", "loaded_context_length": 16384},
                  {"id": "qwen3.5-0.8b"}]}


def test_already_loaded_and_ctx_matches_passes_without_subprocess(env):
    fr, run = env(v1_ids=["m1"], v0=V0_M1)
    ok, msg = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384)
    assert ok and msg == "模型已加载"
    assert run.calls == []


def test_unloads_others_then_loads_with_full_args(env):
    fr, run = env(v1_ids=["other-model"], v0=V0_M1)
    ok, _ = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384, parallel=2)
    assert ok
    assert run.calls[0] == ["lms-fake", "unload", "--all"]
    assert run.calls[1] == ["lms-fake", "load", "m1", "-y", "--gpu", "max",
                            "-c", "16384", "--parallel", "2"]
    # 防投机解码（draft）回归锁：load 命令不得再携带 speculative 旗标
    assert not any("speculative" in str(a).lower() for a in run.calls[1])


def test_ctx_mismatch_on_loaded_model_forces_reload(env):
    v0 = {"data": [{"id": "m1", "loaded_context_length": 130048}]}
    fr, run = env(v1_ids=["m1"], v0=v0)
    ok, _ = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384)
    assert ok
    assert any(c[1:2] == ["load"] for c in run.calls)


def test_main_model_not_downloaded_fails_with_hint(env):
    fr, run = env(v1_ids=[], v0={"data": [{"id": "other"}]})
    ok, msg = lm.ensure_lmstudio_model(EP, "m1")
    assert not ok and "未在 LM Studio 中下载" in msg
    assert run.calls == []


def test_load_failure_returns_error_tail(env):
    fr, run = env(v1_ids=[], v0=V0_M1, fail_load=True)
    ok, msg = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384)
    assert not ok and "err" in msg


def test_server_unreachable_fails_fast(env, monkeypatch):
    class _Boom:
        def get(self, url, timeout=None):
            raise OSError("refused")

    monkeypatch.setattr(lm, "requests", _Boom())
    ok, msg = lm.ensure_lmstudio_model(EP, "m1")
    assert not ok and "未运行或无法连接" in msg


def test_ctx_unreadable_loaded_model_keeps_state(env):
    # v0 拿不到 ctx（旧版/字段缺失）→ 不误判重载
    v0 = {"data": [{"id": "m1"}]}       # 无 loaded_context_length
    fr, run = env(v1_ids=["m1"], v0=v0)
    ok, msg = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384)
    assert ok and msg == "模型已加载"
    assert run.calls == []


# ---- 并发（--parallel）对齐判定 ----
# 2026-09-24 实测 lms ps --json 单条目 schema（截断自真实输出）
PS_ENTRY = {"type": "llm",
            "modelKey": "qwen3.8-27b-uncensored-joyfox-aggressive",
            "format": "gguf",
            "displayName": "Qwen3.8 27B Uncensored JoyFox Aggressive No Mtp",
            "publisher": "joyfox",
            "path": "joyfox/Qwen3.8-27B-Uncensored-JoyFox-Aggressive/"
                    "Qwen3.8-27B-Uncensored-JoyFox-Aggressive-Q3_K_M-no-mtp.gguf",
            "sizeBytes": 14229055768,
            "indexedModelIdentifier":
                "joyfox/Qwen3.8-27B-Uncensored-JoyFox-Aggressive/"
                "Qwen3.8-27B-Uncensored-JoyFox-Aggressive-Q3_K_M-no-mtp.gguf",
            "deviceIdentifier": None,
            "paramsString": "27B",
            "architecture": "qwen35",
            "quantization": {"name": "Q3_K_M", "bits": 3},
            "identifier": "qwen3.8-27b-uncensored-joyfox-aggressive",
            "ttlMs": None,
            "lastUsedTime": 1790177163766,
            "vision": True,
            "trainedForToolUse": True,
            "maxContextLength": 262144,
            "contextLength": 22272,
            "status": "idle",
            "queued": 0,
            "parallel": 2}


def _ps(entries):
    return json.dumps(entries)


def _m1_entry(parallel):
    """fixture 换成测试模型 m1 的同构条目。"""
    e = dict(PS_ENTRY, identifier="m1", modelKey="m1",
             indexedModelIdentifier="m1")
    if parallel is _MISSING:
        e.pop("parallel", None)
    else:
        e["parallel"] = parallel
    return e


def _with_parallel(entry, value):
    """复制 fixture 并改写/删除 parallel 字段（value=KeyError 哨兵则删除）。"""
    e = dict(entry)
    if value is _MISSING:
        e.pop("parallel", None)
    else:
        e["parallel"] = value
    return e


_MISSING = object()


def _no_reload(run):
    return not any(c[1:2] in (["unload"], ["load"]) for c in run.calls)


def test_parallel_mismatch_forces_reload_with_flag(env):
    ps = _ps([_m1_entry(4)])
    fr, run = env(v1_ids=["m1"], v0=V0_M1, ps_payload=ps)
    ok, _ = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384, parallel=2)
    assert ok
    assert any(c[1:2] == ["unload"] for c in run.calls)
    loads = [c for c in run.calls if c[1:2] == ["load"]]
    assert loads == [["lms-fake", "load", "m1", "-y", "--gpu", "max",
                      "-c", "16384", "--parallel", "2"]]
    # 回归锁：load 命令不得携带 speculative 旗标
    assert not any("speculative" in str(a).lower() for a in loads[0])


def test_parallel_match_passes_early(env):
    ps = _ps([_m1_entry(2)])
    fr, run = env(v1_ids=["m1"], v0=V0_M1, ps_payload=ps)
    ok, msg = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384, parallel=2)
    assert ok and msg == "模型已加载"
    assert _no_reload(run)


def test_parallel_undetectable_fails_open(env):
    # 字段缺失 / 0 / 字符串 / null → 一律不重载（fail-open）
    for value in (_MISSING, 0, "2", None):
        ps = _ps([_m1_entry(value)])
        fr, run = env(v1_ids=["m1"], v0=V0_M1, ps_payload=ps)
        ok, msg = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384,
                                           parallel=2)
        assert ok and msg == "模型已加载", f"parallel={value!r} 不应触发重载"
        assert _no_reload(run), f"parallel={value!r} 不应触发重载"


def test_parallel_bad_json_or_timeout_fails_open(env):
    for kw in ({"ps_payload": "not-json"}, {"ps_timeout": True}):
        fr, run = env(v1_ids=["m1"], v0=V0_M1, **kw)
        ok, msg = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384,
                                           parallel=2)
        assert ok and msg == "模型已加载"
        assert _no_reload(run)


def test_real_ps_schema_parses_parallel(monkeypatch):
    monkeypatch.setattr(lm, "_run_lms",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=0, stdout=_ps([PS_ENTRY]), stderr=""))
    logs = []
    got = lm._loaded_parallel("lms-fake",
                              "qwen3.8-27b-uncensored-joyfox-aggressive",
                              5, log=logs.append)
    assert got == 2
    assert logs == []


def test_post_reload_parallel_unfulfilled_warns_once_no_loop(env):
    ps = _ps([_m1_entry(4)])     # 重载后 ps 仍报 4
    fr, run = env(v1_ids=["m1"], v0=V0_M1, ps_payload=ps)
    logs = []
    ok, _ = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384, parallel=2,
                                     log=logs.append)
    assert ok
    assert any("未兑现并发配置" in m for m in logs)
    assert sum(1 for c in run.calls if c[1:2] == ["unload"]) == 1


def test_ctx_skip_path_logs_warning(env):
    v0 = {"data": [{"id": "m1"}]}               # 无 ctx 字段
    fr, run = env(v1_ids=["m1"], v0=v0)
    logs = []
    ok, _ = lm.ensure_lmstudio_model(EP, "m1", ctx_tokens=16384, log=logs.append)
    assert ok
    assert any("ctx 检测跳过" in m for m in logs)
    assert _no_reload(run)

