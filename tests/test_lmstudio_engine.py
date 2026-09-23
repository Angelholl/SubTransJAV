"""ensure_lmstudio_model 引擎对齐逻辑单测（全部 mock，不触网、不依赖 lms）。

fake 服务端维护共享可变状态：unload 清空已载集、load 把目标加回已载集——
与真实 LM Studio 的状态迁移一致，保证"加载后复核"环节可测。
"""

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
    def __init__(self, req, ps_payload='{"data": []}', fail_load=False):
        self.req = req
        self.calls = []
        self.ps_payload = ps_payload
        self.fail_load = fail_load

    def __call__(self, args, capture_output, text, timeout):
        self.calls.append(list(args))
        sub = args[1:2]
        if sub == ["ps"]:
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

