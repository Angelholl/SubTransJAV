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

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
    timeout: float = 900.0         # 本地慢模型单批可达数分钟
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


# 瞬态 HTTP 状态码与错误特征（自 deletion_patch 的瞬态重试逻辑收编）
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}

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


class LLMClient:
    """OpenAI 兼容字幕翻译客户端。"""

    def __init__(self, config: ClientConfig, log=None):
        self.config = config
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._openai_client = None    # 复用 HTTP 连接池（openai client 线程安全）

    # -- 单批请求 -----------------------------------------------------------

    def _chat(self, system_text: str, user_text: str,
              max_tokens: int | None = None) -> str:
        """单次 chat 调用（含瞬态退避与思考模型兜底）。

        max_tokens 由调用方按批传入（不写入共享 config，
        避免并发批间互相覆盖的竞态）。
        """
        from openai import OpenAI
        cfg = self.config
        if self._openai_client is None:
            # 双重检查加锁：translate_entries 在 concurrency>1 时由
            # ThreadPoolExecutor 并发调 _chat，避免竞态重复初始化
            with self._lock:
                if self._openai_client is None:
                    self._openai_client = OpenAI(
                        base_url=cfg.base_url,
                        api_key=cfg.api_key or "not-needed",
                        timeout=cfg.timeout,
                        max_retries=0,   # 瞬态退避由本模块统一控制（避免双层重试叠加）
                    )
        client = self._openai_client
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
                if attempt >= cfg.max_retries or not self._is_transient(e):
                    raise LLMError(f"LLM 调用失败: {e}") from e
                wait = cfg.backoff_time * (2 ** attempt)
                self._log(f"[llm] 瞬态错误（{e}），{wait:.0f}s 后重试 "
                          f"({attempt + 1}/{cfg.max_retries})")
                time.sleep(wait)
        raise LLMError(f"LLM 调用失败: {last_err}")

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
                          progress=None) -> BatchResult:
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
            n = self.config.n_ctx
            mot = (compute_max_output_tokens(len(batch), n,
                                             self.config.token_budget)
                   if n else None)
            return self.translate_batch(
                batch, system_text=system_text, user_prompt=user_prompt,
                allow_empty_deletions=allow_empty_deletions,
                max_output_tokens=mot)

        def _report(fut):
            with self._lock:
                done[0] += 1
                if progress:
                    progress(f"批次 {done[0]}/{total}")

        # 部分失败隔离：单批任何异常（LLMError 或意外错误）只丢弃该批
        # （缺行走定向重试，重试仍败标记 failed），不拖垮其他已完成批次
        if self.config.concurrency > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as ex:
                futs = {ex.submit(_run_batch, b): b for b in batches}
                for fut in futs:
                    fut.add_done_callback(_report)
                for fut in futs:
                    try:
                        results.update(fut.result())
                    except Exception as e:   # noqa: BLE001 批级隔离
                        self._log(f"[llm] 批次失败（缺行走定向重试）: {e}")
        else:
            for b in batches:
                try:
                    results.update(_run_batch(b))
                except Exception as e:   # noqa: BLE001 批级隔离
                    self._log(f"[llm] 批次失败（缺行走定向重试）: {e}")
                done[0] += 1
                if progress:
                    progress(f"批次 {done[0]}/{total}")

        # 定向重试：缺行（含不允许删除的空译文）单独组成小批再试一次
        missing = [e for e in entries if e["index"] not in results]
        if missing:
            self._log(f"[llm] {len(missing)} 条缺行，定向重试")
            retry_batch = self.build_batches(missing, min(10, max_batch_size),
                                             scene_threshold)
            for rb in retry_batch:
                try:
                    results.update(_run_batch(rb))
                except Exception as e:   # noqa: BLE001 重试仍败保留缺行
                    self._log(f"[llm] 定向重试失败（保留缺行）: {e}")

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
