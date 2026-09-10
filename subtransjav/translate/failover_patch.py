"""
云端故障本地接管补丁（Cloud → Local failover）
================================================
双层触发 + 粘性热切换：

1. 传输类·即时：单批请求重试耗尽（429退避/5xx/超时/连接失败）→ 立即切换，
   当批即用本地模型重试（包装 CustomClient._make_request 实现）
2. 内容类·连续：连续 3 批 No Matches / 校验不过 → 切换（由 core.py 的
   诊断事件处理器上报，本模块只提供判定与执行）
3. 限流类·窗口：滑动窗口最近 6 批中 ≥4 批遭遇限流，或首遇限流后累计
   超过 10 分钟仍在发生 → 切换（防"慢速节流爬行"永不触发前两类）

粘性：一旦切换，本阶段剩余批次全部走本地；不自动回切（避免乒乓）。
温度跟随：切换时应用 fallback_cfg 指定的 temperature（LM Studio 建议 0.1）。
"""

import logging
import threading
import time
from collections import deque

from PySubtrans.SubtitleError import TranslationImpossibleError


# 触发阈值
CONTENT_FAIL_STREAK_LIMIT = 3    # 连续内容失败批数
WINDOW_SIZE = 6                  # 滑动窗口大小
WINDOW_FAIL_LIMIT = 4            # 窗口内限流批数上限
THROTTLE_GIVEUP_SECONDS = 600    # 首遇限流后累计观察上限（10分钟）


class FailoverController:
    """持有切换状态与判定逻辑；绑定运行中的 client 执行热切换。"""

    def __init__(self, fallback_cfg: dict, log_fn=None):
        self.fallback_cfg = dict(fallback_cfg or {})
        self.switched = False
        self.switch_reason = ""
        self.switch_batch_label = ""

        self._client = None
        self._log = log_fn or (lambda msg: logging.warning(msg))
        self._lock = threading.Lock()

        # 判定状态
        self._content_streak = 0          # 连续内容失败计数（按批次去重）
        self._last_fail_label = ""        # 最近一次内容失败的批次标签
        self._throttle_window = deque(maxlen=WINDOW_SIZE)   # True=该批遭遇限流
        self._first_throttle_ts = None    # 首次限流时间戳
        self._throttle_events = 0         # 限流事件总数（诊断用）

    # ------------------------------------------------------------------
    def bind_client(self, client):
        """绑定运行中的 TranslationClient（CustomClient 实例）。"""
        self._client = client

    # ------------------------------------------------------------------
    # 事件上报接口（core.py 诊断处理器调用）
    # ------------------------------------------------------------------
    def record_throttle(self, batch_label: str = ""):
        """该批遭遇 429/408 限流。"""
        if self.switched:
            return
        self._throttle_events += 1
        self._throttle_window.append(True)
        now = time.monotonic()
        if self._first_throttle_ts is None:
            self._first_throttle_ts = now
        self._evaluate(batch_label)

    @staticmethod
    def normalize_label(text: str) -> str:
        """从事件消息中提取 'scene N batch M' 标签，用于按批去重。"""
        import re
        m = re.search(r'[Ss]cene\s+(\d+)\s+batch\s+(\d+)', text or "")
        return f"s{m.group(1)}b{m.group(2)}" if m else ""

    def record_content_failure(self, batch_label: str = ""):
        """该批以内容失败告终（No Matches / 重试后仍有未翻译行）。

        同一批次的多次尝试（首次失败+重试）只计一次：标签相同则跳过。
        """
        if self.switched:
            return
        if batch_label and batch_label == self._last_fail_label:
            return
        if batch_label:
            self._last_fail_label = batch_label
        self._content_streak += 1
        self._throttle_window.append(False)
        self._evaluate(batch_label)

    def record_batch_ok(self, untranslated: int = 0, batch_label: str = ""):
        """批次最终成功（0 未翻译行）。untranslated>0 视为内容失败。"""
        if self.switched:
            return
        if untranslated and untranslated > 0:
            self.record_content_failure(batch_label)
            return
        self._content_streak = 0
        self._last_fail_label = ""
        self._throttle_window.append(False)
        self._evaluate()

    def record_transport_failure(self, batch_label: str = ""):
        """传输类失败且客户端内重试已耗尽（即时触发）。"""
        if self.switched:
            return
        self._switch("transport", batch_label,
                     "云端请求重试耗尽(5xx/超时/连接失败/限流)")

    # ------------------------------------------------------------------
    def _evaluate(self, batch_label: str = ""):
        # 内容类：连续 N 批内容失败
        if self._content_streak >= CONTENT_FAIL_STREAK_LIMIT:
            self._switch("content", batch_label,
                         f"连续 {self._content_streak} 批内容解析/校验失败")
            return
        # 限流类：窗口占比
        if len(self._throttle_window) >= WINDOW_SIZE and \
                sum(self._throttle_window) >= WINDOW_FAIL_LIMIT:
            self._switch("throttle-window", batch_label,
                         f"最近 {WINDOW_SIZE} 批中 {sum(self._throttle_window)} "
                         f"批遭遇限流")
            return
        # 限流类：累计时长兜底（防均匀慢速节流永远凑不满窗口占比）
        if (self._first_throttle_ts is not None
                and time.monotonic() - self._first_throttle_ts
                > THROTTLE_GIVEUP_SECONDS
                and self._throttle_events > 0):
            self._switch("throttle-time", batch_label,
                         f"限流持续超过 {THROTTLE_GIVEUP_SECONDS // 60} 分钟"
                         f"（共 {self._throttle_events} 次）")

    # ------------------------------------------------------------------
    def _switch(self, kind: str, batch_label: str, reason: str):
        with self._lock:
            if self.switched or self._client is None:
                return
            try:
                fb = self.fallback_cfg

                # 本地接管前置检查：模型未加载时自动加载；失败则不切换
                #（本地接不住时保持原通道错误，避免切换后立刻死得更糊涂）
                if fb.get('auto_load'):
                    from ..utils.lmstudio import ensure_lmstudio_model
                    ok, msg = ensure_lmstudio_model(
                        fb.get('server_address', '').rstrip('/') + '/v1',
                        fb.get('model', ''), log=self._log)
                    if not ok:
                        self._log(f"[failover] 本地模型就绪检查失败，放弃接管: {msg}")
                        return

                settings = self._client.settings

                # 原子更新：先在局部 dict 构造完整新值，再一次性写回
                new_settings = dict(settings)
                for key in ('server_address', 'endpoint', 'model'):
                    if key in fb:
                        new_settings[key] = fb[key]
                api_key = fb.get('api_key') or ''
                new_settings['api_key'] = api_key
                if 'temperature' in fb:
                    new_settings['temperature'] = fb['temperature']

                # 一次性写回
                settings.update(new_settings)

                # 重置连接与认证头
                auth = f"Bearer {api_key}" if api_key else None
                if auth:
                    self._client.headers['Authorization'] = auth
                elif 'Authorization' in self._client.headers:
                    del self._client.headers['Authorization']

                self.switched = True
                self.switch_reason = f"{kind}: {reason}"
                self.switch_batch_label = batch_label
                self._log(f"⚠️ [failover] 云端异常，本地模型已接管"
                          f"（{batch_label or '下一批'}起）｜原因: {reason}｜"
                          f"目标: {fb.get('server_address')}{fb.get('endpoint')} "
                          f"model={fb.get('model')}")
            except Exception as e:   # noqa: BLE001 切换本身不允许炸翻译流程
                self._log(f"[failover] 切换失败（继续用原通道）: {e}")
                self.switched = False


def install_cloud_failover_patch(controller: FailoverController):
    """包装 CustomClient._make_request：传输类失败的即时接管点。

    客户端内部重试耗尽后抛 TranslationImpossibleError —— 在其冲出重试循环
    处拦截，切换设置后对同一批重试一次（此时 settings 已指向本地）。
    """
    import PySubtrans.Providers.Clients.CustomClient as CC

    if getattr(CC.CustomClient, "_failover_patch_installed", False):
        return False

    _orig_make_request = CC.CustomClient._make_request

    def _patched_make_request(self, request, temperature):
        try:
            return _orig_make_request(self, request, temperature)
        except TranslationImpossibleError:
            if controller.switched or controller._client is None:
                raise
            # 绑定并切换，然后就地用新通道重试同一批
            controller.bind_client(self)
            controller.record_transport_failure("")
            if not controller.switched:
                raise
            return _orig_make_request(self, request, temperature)

    CC.CustomClient._make_request = _patched_make_request
    CC.CustomClient._failover_patch_installed = True
    return True
