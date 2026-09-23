"""
轻量 LLM 字幕翻译客户端（v2 管线唯一 LLM 调用点）
==================================================
直接走 OpenAI 兼容接口（LM Studio / DeepSeek / Zen / SiliconFlow / custom），
替代 PySubtrans + monkey-patch 方案。

职责边界（代码层 vs LLM）：
- 本模块负责：分批、#N 编号协议、响应解析、格式校验、定向重试、
  瞬态错误退避、批间并发、思考模型 reasoning 兜底、token 预算。
- LLM 只负责：按角色卡对每批条目产出译文（留空=有意删除）。

输入协议（与 legacy 角色卡一致）：
    #N
    Original>
    <条目文本>
输出协议：
    #N
    Translation>
    <译文/净语结果>（留空 = 删除该条）
"""

import ipaddress
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# token 预算（自 legacy core.py 迁移，参数可调；适配 Qwen3 等长上下文本地模型）
# ---------------------------------------------------------------------------
DEFAULT_TOKEN_BUDGET = {
    "overhead": 2500,          # system + 指令 + 上下文 + 响应标签
    "tokens_per_line": 500,    # 单条输入+输出（长行最坏情况）
    "input_per_line_cjk": 300,
    "output_per_line": 160,    # 中文译文/条（含协议标记）
    "output_fixed_tags": 300,
}


def cap_batch_size(max_batch_size: int, n_ctx: int,
                   token_budget: dict | None = None) -> int:
    """按上下文窗口收紧批大小。n_ctx 过小（装不下固定开销）时返回 1。"""
    b = token_budget or DEFAULT_TOKEN_BUDGET
    safe_max = (n_ctx - b["overhead"]) // b["tokens_per_line"]
    if safe_max < 1:
        return 1
    return min(max_batch_size, safe_max)


def compute_max_output_tokens(batch_size: int, n_ctx: int,
                              token_budget: dict | None = None) -> int:
    """为本地模型计算 max_tokens 上限，防止上下文溢出。

    返回值永远不超过 available = n_ctx - overhead - 输入预算；
    available 耗尽时返回较小安全值 256（批大小已由 cap_batch_size 收紧）。
    """
    b = token_budget or DEFAULT_TOKEN_BUDGET
    available = n_ctx - b["overhead"] - batch_size * b["input_per_line_cjk"]
    if available <= 0:
        return 256
    expected = batch_size * b["output_per_line"] + b["output_fixed_tags"]
    return max(min(512, available), min(available, expected))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ClientConfig:
    base_url: str                  # OpenAI 兼容 base_url（如 http://localhost:1234/v1）
    api_key: str = ""
    model: str = ""
    temperature: float | None = 0.1
    # 单一批次超时（秒）。P1-5 单一来源：v2 管线经 cfg.timeout_llm 传入
    # （refine.config.DEFAULT_TIMEOUT_LLM 同值 900.0）；本默认值仅为
    # 无 cfg 上下文的直接构造保留（translate 层不反向依赖 refine 层）。
    timeout: float = 900.0
    max_retries: int = 3           # 瞬态错误（429/408/5xx/超时）退避重试次数
    backoff_time: float = 5.0
    concurrency: int = 1           # 批间并发（本地模型保持 1）
    n_ctx: int | None = None       # 本地上下文窗口（启用 max_tokens 预算与批收紧）
    max_tokens: int | None = None  # 显式覆盖输出上限
    token_budget: dict = field(default_factory=lambda: dict(DEFAULT_TOKEN_BUDGET))


@dataclass
class BatchResult:
    """translate_entries 结果。translations 值为空串表示"有意删除"。"""
    translations: dict             # index -> text
    deleted: set                   # index 集合（留空=删除）
    failed: list                   # 重试后仍无法解析的 index（协议失败行）

    @property
    def ok_count(self) -> int:
        return len(self.translations)


class LLMError(Exception):
    """整批调用最终失败（瞬态重试耗尽/连接失败）。调用方可据此触发 fallback。"""


class ModelUnloadedError(LLMError):
    """本地引擎被卸载（HTTP 400 + "model unloaded by user" 特征串）。

    不属于瞬态错误（重载引擎需 60-120s，5s 退避重试无意义），
    由批循环通过 unloaded_recovery 回调做一次引擎重对齐后整批重试。
    """


# D2026-0924-02：LM Studio 用户/API 卸载模型的 400 响应特征串。
# 用长特征串防误伤：云端 400 的 "model unloaded due to inactivity"
# 或模型名恰好含 "unloaded" 均不命中。
_MODEL_UNLOADED_MARKER = "model unloaded by user"

# 瞬态 HTTP 状态码与错误特征（自 deletion_patch 的瞬态重试逻辑收编）
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}

# D7 缺行定向重试预算（轮数）：同一批缺行/畸形最多重试 2 轮，预算耗尽后
# 停止重试——剩余缺行按既有 failed 链路交调用方降级 [未翻译]，绝不整批失败
MISSING_RETRY_BUDGET = 2

_MARK_SPLIT = re.compile(r"^#(\d+)\s*$", re.MULTILINE)
_EXPECT_LINE = re.compile(r"^\s*Translation>\s*$")
_EXPECT_INLINE = re.compile(r"^\s*Translation>\s?(.*)$")


# ---------------------------------------------------------------------------
# 协议解析（纯函数，便于单测）
# ---------------------------------------------------------------------------


def parse_numbered_response(text: str, expected: set | None = None) -> dict:
    """解析 #N / Translation> / 正文 协议响应。

    expected: 本批期望的编号集合。提供时，正文中独立成行的 #N 若不在
    expected 内（模型复述编号/台词引用/批间串扰）按正文处理，不再切分——
    防止译文被截断、内容被错误归属到不存在的编号。

    返回 {index: text}；正文允许 1 个内部换行。容错：
    - "Translation> 译文" 同行写法
    - 响应中混入的说明文字（非编号开头行）忽略
    - 编号重复时后者覆盖前者
    - 缺 Translation> 标记的块：取块内全部非空文本（漏标记容错）
    """
    if not text or not text.strip():
        return {}
    matches = list(_MARK_SPLIT.finditer(text))
    out = {}
    for k, m in enumerate(matches):
        idx = int(m.group(1))
        if expected is not None and idx not in expected:
            continue         # 非期望编号：留给所属块的正文（跳过切分）
        body_start = m.end()
        # 正文终止于下一个"期望编号"的标记行
        body_end = len(text)
        for m2 in matches[k + 1:]:
            if expected is None or int(m2.group(1)) in expected:
                body_end = m2.start()
                break
        body = text[body_start:body_end]
        lines = body.split("\n")
        # 找 Translation> 标记行（允许同行带正文）
        content_lines = []
        found = False
        for ln in lines:
            if not found:
                em = _EXPECT_INLINE.match(ln)
                if em and (em.group(1) or _EXPECT_LINE.match(ln)):
                    if em.group(1):
                        content_lines.append(em.group(1))
                    found = True
                    continue
                # 标记前的空行/杂散文字跳过
                continue
            content_lines.append(ln)
        if found:
            out[idx] = "\n".join(content_lines).strip("\n").strip()
        elif not expected:
            continue
        else:
            # 漏标记容错：模型只写了 #N 没写 Translation> 时，
            # 取块内全部文本作为译文（重试同协议大概率复现该失误）
            fallback = "\n".join(ln for ln in lines if ln.strip()).strip()
            if fallback:
                out[idx] = fallback
    return out


def format_numbered_entries(entries: list) -> str:
    """把条目格式化为输入协议文本。"""
    blocks = []
    for e in entries:
        blocks.append(f"#{e['index']}\nOriginal>\n{e['text']}")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def _loopback_http_client(base_url: str, timeout: float):
    """回环端点构造 trust_env=False 的 httpx.Client；非回环返回 None（O6）。

    背景：系统代理（Windows 系统设置或 HTTP_PROXY/HTTPS_PROXY 环境变量）
    会把发往 127.0.0.1/localhost 的请求转给代理，代理通常拒绝回环目标或
    本身不可达，导致"本地服务明明在跑却连不上"。trust_env=False 使该
    客户端完全无视代理 env/系统设置，直连本地端口；非回环服务商保持
    openai 默认行为不变。
    """
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        return None
    if not host:
        return None
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:                      # 主机名形式（localhost 等）
        loopback = host.lower() == "localhost"
    if not loopback:
        return None
    from httpx import Client
    return Client(trust_env=False, timeout=timeout)


class LLMClient:
    """OpenAI 兼容字幕翻译客户端。"""

    def __init__(self, config: ClientConfig, log=None,
                 unloaded_recovery: Callable[[], None] | None = None):
        self.config = config
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._openai_client = None    # 复用 HTTP 连接池（openai client 线程安全）
        # D2026-0924-02：引擎卸载恢复回调（translate_entries 未显式传入时
        # 作为默认值），由管线侧注入（仅本地 provider）
        self._unloaded_recovery_default = unloaded_recovery
        # 恢复信用：每客户端实例至多做一次引擎重对齐；双批并发 400 时
        # 由 _recovery_lock 串行化，先到者消耗信用，后到者走现状路径
        self._recovery_lock = threading.Lock()
        self._recovery_credit = 1

    # -- 单批请求 -----------------------------------------------------------

    def _ensure_openai_client(self):
        """懒构造并复用 OpenAI 客户端（双重检查加锁防并发重复初始化）。

        回环端点（LM Studio / Ollama 等本地服务）改用 trust_env=False 的
        专用 httpx.Client（O6），该连接池与客户端同生命周期，随
        _openai_client 复用；非回环端点传 http_client=None 保持 openai
        默认行为不变。
        """
        from openai import OpenAI
        cfg = self.config
        if self._openai_client is None:
            # 双重检查加锁：translate_entries 在 concurrency>1 时由
            # ThreadPoolExecutor 并发调 _chat，避免竞态重复初始化
            with self._lock:
                if self._openai_client is None:
                    http_client = _loopback_http_client(cfg.base_url,
                                                        cfg.timeout)
                    try:
                        self._openai_client = OpenAI(
                            base_url=cfg.base_url,
                            api_key=cfg.api_key or "not-needed",
                            timeout=cfg.timeout,
                            max_retries=0,   # 瞬态退避由本模块统一控制（避免双层重试叠加）
                            http_client=http_client,   # None=openai 默认行为
                        )
                    except BaseException:
                        if http_client is not None:
                            http_client.close()   # 构造失败不泄漏连接池
                        raise
        return self._openai_client

    def _chat(self, system_text: str, user_text: str,
              max_tokens: int | None = None) -> str:
        """单次 chat 调用（含瞬态退避与思考模型兜底）。

        max_tokens 由调用方按批传入（不写入共享 config，
        避免并发批间互相覆盖的竞态）。
        """
        cfg = self.config
        client = self._ensure_openai_client()
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": user_text})

        kwargs = {"model": cfg.model, "messages": messages, "stream": False}
        if cfg.temperature is not None:
            kwargs["temperature"] = cfg.temperature
        effective_max_tokens = max_tokens if max_tokens is not None \
            else cfg.max_tokens
        if effective_max_tokens is not None:
            kwargs["max_tokens"] = effective_max_tokens

        last_err = None
        for attempt in range(cfg.max_retries + 1):
            try:
                resp = client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                content = (msg.content or "").strip()
                if not content:
                    # 思考型模型兜底（Qwen3 系等）：输出落在 reasoning 字段
                    for key in ("reasoning_content", "reasoning"):
                        alt = getattr(msg, key, None)
                        if alt:
                            self._log(f"[llm] content 为空，使用 {key} 兜底 "
                                      f"({len(alt)} 字符)")
                            content = alt.strip()
                            break
                return content
            except Exception as e:   # noqa: BLE001 统一退避判定
                last_err = e
                # D2026-0924-02：引擎被卸载（400 + 特征串）→ 专用异常类型，
                # 不进瞬态退避（重载引擎 60-120s，5s 重试无意义），由批循环
                # 的 unloaded_recovery 回调负责重对齐后整批重试
                if self._is_model_unloaded(e):
                    raise ModelUnloadedError(f"LLM 调用失败: {e}") from e
                if attempt >= cfg.max_retries or not self._is_transient(e):
                    raise LLMError(f"LLM 调用失败: {e}") from e
                wait = cfg.backoff_time * (2 ** attempt)
                self._log(f"[llm] 瞬态错误（{e}），{wait:.0f}s 后重试 "
                          f"({attempt + 1}/{cfg.max_retries})")
                time.sleep(wait)
        raise LLMError(f"LLM 调用失败: {last_err}")

    @staticmethod
    def _is_connection_refused(err: Exception) -> bool:
        """连接拒绝/代理不可达类判定（O6）。

        openai 会把 httpx.ConnectError/ProxyError 包装为 APIConnectionError
        （str() 往往只剩 "Connection error."），故沿 __cause__ 链查原始
        异常类型，再用常见错误文本兜底（Windows WinError 10061「积极拒绝」、
        errno 111、代理连接失败等）。此类故障（端口没开/服务未启动/代理
        挂了）秒级重试结果几乎必然相同，判为不可重试以快速失败。
        """
        cur, depth = err, 0
        while cur is not None and depth < 6:
            if type(cur).__name__.lower() in ("connecterror", "proxyerror"):
                return True
            cur = getattr(cur, "__cause__", None)
            depth += 1
        text = str(err).lower()
        return any(m in text for m in (
            "connection refused", "econnrefused", "10061",
            "unable to connect to proxy", "cannot connect to proxy",
            "积极拒绝", "拒绝连接"))

    @staticmethod
    def _is_model_unloaded(err: Exception) -> bool:
        """识别「HTTP 400 + model unloaded by user」引擎卸载错误（大小写
        不敏感）。status_code 缺失时仅凭特征串判定（openai 包装层可能
        丢失结构化字段）。非 400 状态不命中，401/403 短路不受影响。"""
        status = getattr(err, "status_code", None)
        if status is not None:
            try:
                if int(status) != 400:
                    return False
            except (TypeError, ValueError):
                pass    # 状态码非数字 → 交由特征串兜底判定
        return _MODEL_UNLOADED_MARKER in str(err).lower()

    @staticmethod
    def _is_transient(err: Exception) -> bool:
        status = getattr(err, "status_code", None)
        if status is not None:
            # status_code 可能是非数字字符串，安全转换（转换失败按不重试处理）
            try:
                s = int(status)
            except (TypeError, ValueError):
                return False
            # 认证/权限类永久错误不重试（避免每批白等退避）
            if s in (401, 403):
                return False
            return s in _TRANSIENT_STATUS
        # 连接拒绝/代理不可达：不可重试，快速失败（O6）；
        # 429/5xx 等服务端瞬态与超时保持原有退避策略不变。
        if LLMClient._is_connection_refused(err):
            return False
        code = getattr(err, "code", None)
        if isinstance(code, int) and code in _TRANSIENT_STATUS:
            return True
        name = type(err).__name__.lower()
        if "timeout" in name or "connection" in name or "apiconnection" in name:
            return True
        text = str(err).lower()
        if "api key" in text or "unauthorized" in text or "forbidden" in text:
            return False
        return any(s in text for s in ("rate limit", "timed out", "timeout",
                                       "connection", "overloaded"))

    # -- 批处理 -------------------------------------------------------------

    def build_batches(self, entries: list, max_batch_size: int,
                      scene_threshold: float = 60.0) -> list:
        """按场景间隙 + 大小上限切批。

        场景切分：相邻条目起始时间间隔 > scene_threshold 秒时断批
        （与 legacy PySubtrans 的场景思想一致，保证批内上下文连贯）。
        """
        if self.config.n_ctx:
            max_batch_size = cap_batch_size(
                max_batch_size, self.config.n_ctx, self.config.token_budget)
        max_batch_size = max(1, int(max_batch_size))

        def _start_sec(e):
            m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", e.get("timing") or "")
            if not m:
                return None
            h, mi, s, ms = map(int, m.groups())
            return h * 3600 + mi * 60 + s + ms / 1000.0

        batches = []
        cur = []
        prev_start = None
        for e in entries:
            start = _start_sec(e)
            new_scene = (prev_start is not None and start is not None
                         and start - prev_start > scene_threshold)
            if cur and (len(cur) >= max_batch_size or new_scene):
                batches.append(cur)
                cur = []
            cur.append(e)
            if start is not None:
                prev_start = start
        if cur:
            batches.append(cur)
        return batches

    def translate_batch(self, entries: list, *, system_text: str,
                        user_prompt: str, allow_empty_deletions: bool,
                        max_output_tokens: int | None = None) -> dict:
        """翻译单批，返回 {index: text}（空串=删除）。缺行由调用方重试。"""
        user_text = f"{user_prompt}\n\n{format_numbered_entries(entries)}"
        content = self._chat(system_text, user_text,
                             max_tokens=max_output_tokens)

        expected = {e["index"] for e in entries}
        parsed = parse_numbered_response(content, expected=expected)
        result = {}
        for idx in expected:
            if idx in parsed:
                text = parsed[idx]
                if text == "" and not allow_empty_deletions:
                    continue     # 不允许删除时，空译文视为缺行
                result[idx] = text
        return result

    def translate_entries(self, entries: list, *, system_text: str,
                          user_prompt: str, max_batch_size: int = 30,
                          scene_threshold: float = 60.0,
                          allow_empty_deletions: bool = False,
                          progress=None,
                          unloaded_recovery: Callable[[], None] | None = None,
                          ) -> BatchResult:
        """全量翻译：分批 → 并发/串行执行 → 缺行定向重试一次。

        返回 BatchResult；failed 为重试后仍缺失的 index（调用方按
        [未翻译] 语义处理），不会因部分行失败而抛异常。
        """
        batches = self.build_batches(entries, max_batch_size, scene_threshold)
        total = len(batches)
        self._log(f"[llm] 共 {len(entries)} 条，分 {total} 批"
                  f"（并发={self.config.concurrency}）")

        results = {}
        done = [0]

        def _run_batch(batch):
            def _attempt():
                n = self.config.n_ctx
                mot = (compute_max_output_tokens(len(batch), n,
                                                 self.config.token_budget)
                       if n else None)
                return self.translate_batch(
                    batch, system_text=system_text, user_prompt=user_prompt,
                    allow_empty_deletions=allow_empty_deletions,
                    max_output_tokens=mot)

            try:
                return _attempt()
            except ModelUnloadedError:
                # D2026-0924-02：引擎被卸载 → 加锁 + 恢复信用（每实例一次），
                # 同步重对齐引擎后整批重试一次；信用耗尽/回调失败则按现状
                # 路径走（批失败 → 缺行定向重试 → [未翻译]）。异常仍在本层
                # 消化，不冒泡。
                recovery = (unloaded_recovery
                            if unloaded_recovery is not None
                            else self._unloaded_recovery_default)
                if recovery is None:
                    raise
                with self._recovery_lock:
                    if self._recovery_credit <= 0:
                        self._log("[llm] Model unloaded 恢复信用已耗尽，"
                                  "按现状路径处理")
                        raise
                    self._recovery_credit -= 1
                    self._log("[llm] 检测到 Model unloaded，"
                              "尝试重新对齐引擎后重试")
                    try:
                        recovery()
                    except Exception as e:   # noqa: BLE001 回调失败走现状路径
                        self._log(f"[llm] 引擎恢复回调失败"
                                  f"（按现状路径处理）: {e}")
                        raise
                    return _attempt()

        def _report(fut):
            with self._lock:
                done[0] += 1
                if progress:
                    progress(f"批次 {done[0]}/{total}")

        def _warn_line_deficit(batch_no: int, batch: list, res: dict) -> None:
            """D7 批后行数守卫：输出行数 ≠ 输入行数时告警（缺行定向重试
            仍是主守卫，此处只保证失配不静默）。"""
            if len(res) == len(batch):
                return
            logger.warning(
                "[llm] 批次 %d 行数不守恒：输入 %d 行 / 输出 %d 行"
                "（缺行进入定向重试，预算 %d 轮）",
                batch_no, len(batch), len(res), MISSING_RETRY_BUDGET)

        # 部分失败隔离：单批任何异常（LLMError 或意外错误）只丢弃该批
        # （缺行走定向重试，重试仍败标记 failed），不拖垮其他已完成批次
        if self.config.concurrency > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as ex:
                futs = {ex.submit(_run_batch, b): (bno, b)
                        for bno, b in enumerate(batches, 1)}
                for fut in futs:
                    fut.add_done_callback(_report)
                for fut, (bno, b) in futs.items():
                    try:
                        res = fut.result()
                        _warn_line_deficit(bno, b, res)
                        results.update(res)
                    except Exception as e:   # noqa: BLE001 批级隔离
                        self._log(f"[llm] 批次失败（缺行走定向重试）: {e}")
        else:
            for bno, b in enumerate(batches, 1):
                try:
                    res = _run_batch(b)
                    _warn_line_deficit(bno, b, res)
                    results.update(res)
                except Exception as e:   # noqa: BLE001 批级隔离
                    self._log(f"[llm] 批次失败（缺行走定向重试）: {e}")
                done[0] += 1
                if progress:
                    progress(f"批次 {done[0]}/{total}")

        # 定向重试（D7 重试预算 N=MISSING_RETRY_BUDGET）：缺行（含不允许
        # 删除的空译文）单独组成小批重试，最多预算轮数；预算耗尽后停止
        # 重试，剩余缺行逐行走既有 failed 链路（调用方置 [未翻译]），
        # 绝不整批/整文件失败
        for round_no in range(1, MISSING_RETRY_BUDGET + 1):
            missing = [e for e in entries if e["index"] not in results]
            if not missing:
                break
            self._log(f"[llm] {len(missing)} 条缺行，定向重试"
                      f"（第 {round_no}/{MISSING_RETRY_BUDGET} 轮）")
            retry_batch = self.build_batches(missing, min(10, max_batch_size),
                                             scene_threshold)
            for rb in retry_batch:
                try:
                    results.update(_run_batch(rb))
                except Exception as e:   # noqa: BLE001 重试仍败保留缺行
                    self._log(f"[llm] 定向重试失败（保留缺行）: {e}")
        still_missing = [e for e in entries if e["index"] not in results]
        if still_missing:
            logger.warning(
                "[llm] 缺行定向重试预算（%d 轮）耗尽，仍缺 %d 行，"
                "逐行降级为 [未翻译] 链路（不整批失败）",
                MISSING_RETRY_BUDGET, len(still_missing))

        translations, deleted, failed = {}, set(), []
        for e in entries:
            idx = e["index"]
            if idx in results:
                text = results[idx]
                if text == "":
                    deleted.add(idx)
                else:
                    translations[idx] = text
            else:
                failed.append(idx)
        return BatchResult(translations=translations, deleted=deleted,
                           failed=failed)
