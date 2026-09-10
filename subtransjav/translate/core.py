"""
Core translation logic - PySubtrans wrapper.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Backward-compatible logging setup ──────────────────────────────────
# If no handler has been configured for this package yet, install a
# StreamHandler to stderr so existing print-to-stderr behaviour is
# preserved.  Callers that set up structured logging (e.g. CLI entry
# points) should call logging.basicConfig() or configure the
# 'subtransjav' logger *before* importing this module.
if not logging.getLogger('subtransjav').handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter('%(message)s'))
    logging.getLogger('subtransjav').addHandler(_handler)
    logging.getLogger('subtransjav').setLevel(logging.INFO)


def cap_batch_size_for_context(max_batch_size: int, n_ctx: int, token_budget: Optional[dict] = None) -> int:
    """Cap translation batch size to fit within the LLM context window.

    Local LLMs (e.g., gemma-9b via llama-cpp) have limited context windows.
    PySubtrans formats each subtitle line with numbered headers, original text,
    and translation placeholders. Both the prompt (input) and the expected
    response must fit within n_ctx tokens.

    Japanese text is tokenized at ~2-3 tokens per character by byte-level
    tokenizers (LLaMA, Gemma). A typical subtitle line of 30-50 Japanese
    characters becomes 75-150 tokens. Combined with the response (translated
    text), PySubtrans formatting headers, and <summary>/<scene> tags, we
    budget for worst-case (long) lines:

    Per subtitle line (both directions):
    - Input: ~150 tokens (``#N\\nOriginal>\\n`` + Japanese text, long lines)
    - Output: ~100 tokens (``#N\\nTranslation>\\n`` + translated text)
    - Subtotal: ~250 tokens typical, 500 budget for worst-case long lines

    Fixed overhead:
    - System message: ~500 tokens
    - Translation instructions (standard.txt): ~700 tokens
    - Context from previous batches (<scene>/<summary>): ~500 tokens
    - Response summary/scene tags: ~200 tokens
    - Formatting, preamble, retry margin: ~600 tokens
    - Total: ~2500 tokens

    Revision history:
    - v1.8.6: overhead=2000, per_line=350 → 17 for 8K. Proved unsafe (#196).
    - v1.8.7: overhead=2500, per_line=500 → 11 for 8K. Accounts for long
      Japanese lines, PySubtrans context, and response summary tags.

    Results by context size:
    - 8K (8192):  11 lines per batch
    - 16K (16384): 27 lines per batch
    - 32K+: 30 (capped by max_batch_size default)

    Args:
        max_batch_size: Current batch size setting
        n_ctx: LLM context window in tokens

    Returns:
        Capped batch size (may be unchanged if already within limits)
    """
    budget = token_budget or {}
    overhead = budget.get('overhead', 2500)  # system message + instructions + context + response tags
    tokens_per_line = budget.get('tokens_per_line', 500)  # input + output per subtitle line (worst-case long lines)
    safe_max = max(5, (n_ctx - overhead) // tokens_per_line)
    return min(max_batch_size, safe_max)


def compute_max_output_tokens(batch_size: int, n_ctx: int, token_budget: Optional[dict] = None) -> int:
    """Compute max_tokens for local LLM output to prevent context overflow (#196).

    Japanese/CJK tokenization in LLaMA/Gemma BPE tokenizers:
    - Each CJK character encodes as 3 UTF-8 bytes → typically 2-3 BPE tokens/char
    - A long JAV narration line (80-100 Japanese chars) = ~240-300 input tokens
    - We budget 300 tokens/line input as the worst-case (very long narration)

    English output per line:
    - PySubtrans markers (#N\\nTranslation>\\n): ~8 tokens
    - Translated English text for a typical subtitle: ~50-100 tokens
    - Budget: 120 tokens/line output (generous for long translated sentences)

    Fixed per-batch output overhead:
    - PySubtrans appends <summary>, <scene>, <synopsis> tags after translations
    - These can consume 200-400 tokens depending on model verbosity
    - Budget: 500 tokens fixed overhead

    Strategy: clamp to the smaller of (available context after worst-case input)
    and expected output. Previous versions used 2× expected, but this gave models
    too much room to produce verbose/garbled output (e.g., 2392 tokens of garbage
    instead of ~1200 tokens of translation). Tightening to 1× forces concise output
    and reduces "No matches found" failures caused by off-format model verbosity.

    Results:
    - 8K  (batch=11): ~1820 tokens max output (matches expected)
    - 16K (batch=27): ~3740 tokens max output (matches expected)
    - 32K (batch=30): ~4100 tokens max output (matches expected)

    Args:
        batch_size: Number of subtitle lines in the batch
        n_ctx: LLM context window in tokens

    Returns:
        max_tokens value to pass to the local LLM server
    """
    budget = token_budget or {}
    overhead = budget.get('overhead', 2500)          # matches cap_batch_size_for_context() — same fixed overhead
    input_per_line_cjk = budget.get('input_per_line_cjk', 300)  # JAV worst-case: 80-100 JP chars × ~3 BPE tokens/char
    output_per_line_en = budget.get('output_per_line_en', 120)  # English translation per line incl. PySubtrans markers
    output_fixed_tags = budget.get('output_fixed_tags', 500)    # <summary>/<scene>/<synopsis> tags emitted per batch

    available = n_ctx - overhead - (batch_size * input_per_line_cjk)
    expected = (batch_size * output_per_line_en) + output_fixed_tags
    return max(512, min(available, expected))


def _normalize_api_base(url: str) -> str:
    """Strip API path suffixes — the OpenAI SDK appends them automatically.

    Users sometimes paste full endpoint URLs like
    ``https://api.example.com/v1/chat/completions``.  The ``openai`` SDK
    already appends ``/chat/completions`` to ``base_url``, so passing the
    full path results in a doubled suffix and a 404.
    """
    if not url:
        return url
    for suffix in ('/chat/completions', '/responses', '/completions'):
        if url.rstrip('/').endswith(suffix):
            url = url.rstrip('/')[:-len(suffix)]
    return url.rstrip('/')


def _api_base_to_custom_server(api_base: str) -> tuple:
    """Convert an api_base URL to Custom Server's (server_address, endpoint) pair.

    PySubtrans's CustomClient uses httpx.Client(base_url=server_address) and
    then client.post(endpoint, ...).  An absolute endpoint path (starting with /)
    replaces the base_url path, so server_address must be scheme+host only,
    and endpoint must be the full path including /chat/completions.
    """
    from urllib.parse import urlparse
    normalized = _normalize_api_base(api_base)
    parsed = urlparse(normalized)
    server_address = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip('/')
    endpoint = f"{path}/chat/completions" if path else "/v1/chat/completions"
    return server_address, endpoint


def translate_subtitle(
    input_path: str,
    output_path: Path,
    provider_config: dict,
    model: str,
    api_key: str,
    source_lang: str = "japanese",
    target_lang: str = "english",
    instruction_file: str = None,
    scene_threshold: float = 120.0,
    max_batch_size: int = 30,
    stream: bool = False,
    debug: bool = False,
    provider_options: dict = None,
    extra_context: str = None,
    emit_raw_output: bool = True,
    preprocess_subtitles: bool = True,
    allow_empty_deletions: bool = False,
    cloud_fallback: dict = None
):
    """
    Translate subtitle file using PySubtrans.

    Returns:
        Path to translated file
    """
    # When debug=True, promote the logger so DEBUG-level messages are emitted.
    if debug:
        logger.setLevel(logging.DEBUG)

    try:
        from PySubtrans import (
            init_options,
            init_translator,
            init_translation_provider,
            init_project
        )

        # =========================================================================
        # DIAGNOSTIC: Translation Configuration
        # =========================================================================
        logger.info("\n[TRANSLATE] PySubtrans Configuration:")
        logger.info("[TRANSLATE]   Input: %s", input_path)
        logger.info("[TRANSLATE]   Output: %s", output_path)
        logger.info("[TRANSLATE]   Provider: %s", provider_config.get('pysubtrans_name', 'unknown'))
        logger.info("[TRANSLATE]   Model: %s", model)
        logger.info("[TRANSLATE]   Source lang: %s -> Target lang: %s", source_lang, target_lang)
        logger.info("[TRANSLATE]   Max batch size: %d", max_batch_size)
        logger.info("[TRANSLATE]   Scene threshold: %ss", scene_threshold)
        logger.info("[TRANSLATE]   Stream requested: %s (actual depends on provider)", stream)

        # Log provider-specific settings
        if 'server_address' in provider_config:
            logger.info("[TRANSLATE]   Server address: %s", provider_config['server_address'])
        if 'endpoint' in provider_config:
            logger.info("[TRANSLATE]   Endpoint: %s", provider_config['endpoint'])
        if 'api_base' in provider_config:
            logger.info("[TRANSLATE]   API base: %s", provider_config['api_base'])

        # Build prompt
        prompt = f"Translate these subtitles from {source_lang} into {target_lang}."
        if extra_context:
            prompt += "\n" + extra_context
            logger.info("[TRANSLATE]   Extra context: %s", extra_context[:200])

        # Qwen3-family thinking model flag: consumed later by the response
        # parsing patch (after provider init). Filtered out of provider_options
        # below (before opt_kwargs.update) so it doesn't get passed to
        # PySubtrans as an unknown option.
        _is_thinking_model = (provider_options or {}).get('_thinking_model', False)
        if _is_thinking_model:
            logger.info("[TRANSLATE]   Thinking model: YES (will patch response parsing)")

        # Build provider options
        opt_kwargs = {
            'provider': provider_config['pysubtrans_name'],
            'model': model,
            'api_key': api_key,
            'target_language': target_lang.capitalize(),
            'prompt': prompt,
            'preprocess_subtitles': preprocess_subtitles,
            'scene_threshold': scene_threshold,
            'max_batch_size': max_batch_size,
            'postprocess_translation': True
        }

        # Pass instruction file to PySubtrans so it can parse the
        # ### prompt / ### instructions / ### retry_instructions sections.
        # Previously this was handled via project.SetInstructions() which
        # does not exist in current PySubtrans — instructions were silently
        # dropped for ALL providers.
        if instruction_file:
            opt_kwargs['instruction_file'] = str(instruction_file)

        if 'api_base' in provider_config:
            opt_kwargs['api_base'] = _normalize_api_base(provider_config['api_base'])
        if stream:
            opt_kwargs['stream_responses'] = True

        # Custom Server provider settings (for local LLM)
        if 'server_address' in provider_config:
            opt_kwargs['server_address'] = provider_config['server_address']
            # 本地/自定义通道：慢模型（如 9B 本地推理单批 prompt 处理即需
            # 3 分钟）在默认 300s 超时 + 1 次重试下必然失败 —— 客户端超时
            # 断开后服务端还在继续生成，两边空转烧时间。放宽到 900s/重试3次。
            opt_kwargs['timeout'] = int(provider_config.get('timeout', 900))
            opt_kwargs['max_retries'] = 3
            opt_kwargs['backoff_time'] = 5.0
        if 'endpoint' in provider_config:
            opt_kwargs['endpoint'] = provider_config['endpoint']
        if 'supports_conversation' in provider_config:
            opt_kwargs['supports_conversation'] = provider_config['supports_conversation']
        if 'supports_system_messages' in provider_config:
            opt_kwargs['supports_system_messages'] = provider_config['supports_system_messages']
            _sys_msg = provider_config['supports_system_messages']
            logger.info("[TRANSLATE]   System messages: %s",
                        'enabled' if _sys_msg else 'DISABLED (embedded in user msg)')
        if 'max_tokens' in provider_config:
            opt_kwargs['max_tokens'] = provider_config['max_tokens']
        if 'max_completion_tokens' in provider_config:
            opt_kwargs['max_completion_tokens'] = provider_config['max_completion_tokens']
        if 'supports_streaming' in provider_config:
            opt_kwargs['supports_streaming'] = provider_config['supports_streaming']

        # Merge provider-specific options
        if provider_options:
            # Filter out internal-only keys (e.g. _thinking_model) that should
            # not be forwarded to PySubtrans as unknown options.
            provider_options = {k: v for k, v in provider_options.items() if not k.startswith('_')}
            opt_kwargs.update(provider_options)
            _po_display = {k: v for k, v in provider_options.items() if k != 'api_key'}
            logger.info("[TRANSLATE]   Provider options: %s", _po_display)

        # =========================================================================
        # DIAGNOSTIC: Final opt_kwargs (what PySubtrans actually receives)
        # =========================================================================
        if debug:
            logger.debug("[TRANSLATE] Full opt_kwargs for PySubtrans:")
            for k, v in opt_kwargs.items():
                # Don't log API key
                if k == 'api_key':
                    logger.debug("[TRANSLATE]   %s: %s", k, '*' * 8 if v else '(empty)')
                elif k == 'prompt':
                    logger.debug("[TRANSLATE]   %s: %s...", k, v[:50])
                else:
                    logger.debug("[TRANSLATE]   %s: %s", k, v)

        # Initialize options and provider
        logger.info("[TRANSLATE] Initializing PySubtrans options...")
        options = init_options(**opt_kwargs)
        logger.debug("[TRANSLATE] Final batch_size: %s", opt_kwargs.get('max_batch_size'))

        # =========================================================================
        # DIAGNOSTIC: Instruction Loading Verification (always-on)
        # =========================================================================
        # Verify that PySubtrans actually loaded and parsed the instructions.
        # The instruction_file dead-path bug (commit 56315f2) went undetected
        # for months because nothing inspected what init_options() produced.
        _loaded_inst_file = options.get('instruction_file')
        _has_instructions = bool(options.get('instructions'))
        _has_retry = bool(options.get('retry_instructions'))
        if instruction_file:
            # We asked for a specific instruction file — verify it was loaded
            if _loaded_inst_file:
                logger.info("[TRANSLATE]   Instructions loaded: %s", _loaded_inst_file)
                logger.info("[TRANSLATE]   Sections: instructions=%s, retry=%s",
                            'YES' if _has_instructions else 'MISSING',
                            'YES' if _has_retry else 'MISSING')
            else:
                logger.warning("[TRANSLATE]   WARNING: instruction_file='%s' was passed "
                               "but PySubtrans did not load it!", instruction_file)
        else:
            logger.info("[TRANSLATE]   Instructions: (default — no instruction file specified)")

        # Debug-only: instruction content preview
        if debug:
            _inst_text = options.get('instructions', '')
            if _inst_text:
                logger.debug("[TRANSLATE]   Instructions preview: %s...", _inst_text[:150])
            _prompt_text = options.get('prompt', '')
            if _prompt_text:
                logger.debug("[TRANSLATE]   Prompt: %s", _prompt_text[:150])

        # Initialize provider
        logger.info("[TRANSLATE] Initializing provider: %s...", provider_config['pysubtrans_name'])
        # 瞬时限流重试补丁：429/408 转入退避重试而非整任务失败（全通道生效）
        try:
            from .deletion_patch import install_transient_retry_patch
            if install_transient_retry_patch():
                logger.info("[TRANSLATE]   Transient-retry patch: ACTIVE "
                            "(HTTP 408/429 now retried with backoff)")
        except Exception as e:
            print(f"[patch] 安装失败（忽略）: {e}")
        try:
            provider = init_translation_provider(provider_config['pysubtrans_name'], options)
        except Exception as init_err:
            if "Unknown translation provider" in str(init_err):
                # PySubtrans silently skips providers whose SDK isn't installed.
                # Surface the likely missing package so users know what to install.
                _PROVIDER_DEPS = {
                    'Gemini': 'google-genai',
                    'Claude': 'anthropic',
                    'OpenAI': 'openai',
                    'DeepSeek': 'openai',
                    'OpenRouter': 'openai',
                    'Custom Server': None,
                }
                pkg = _PROVIDER_DEPS.get(provider_config['pysubtrans_name'])
                hint = f"  Hint: install the provider SDK:  pip install {pkg}" if pkg else ""
                raise RuntimeError(
                    f"PySubtrans does not have the '{provider_config['pysubtrans_name']}' provider registered.\n"
                    f"  This usually means its SDK package is not installed.\n"
                    f"{hint}"
                ) from init_err
            raise

        # Validate provider settings
        if hasattr(provider, 'ValidateSettings') and not provider.ValidateSettings():
            msg = getattr(provider, 'validation_message', 'Invalid provider settings')
            logger.error("[TRANSLATE] ERROR: Provider validation failed: %s", msg)
            return None
        logger.info("[TRANSLATE]   Provider initialized and validated")

        # Log provider internals for debugging (if available)
        if debug:
            if hasattr(provider, 'client') and provider.client:
                client = provider.client
                if hasattr(client, 'settings'):
                    settings = client.settings
                    logger.debug("[TRANSLATE]   Client timeout: %s", getattr(settings, 'timeout', 'unknown'))
                    logger.debug("[TRANSLATE]   Client server_address: %s", getattr(settings, 'server_address', 'unknown'))
                    logger.debug("[TRANSLATE]   Client endpoint: %s", getattr(settings, 'endpoint', 'unknown'))

        # Initialize project (PySubtrans 1.5.x expects subtitle path in 'filepath' kwarg)
        logger.info("[TRANSLATE] Loading subtitle project...")
        project = init_project(options, filepath=str(input_path), persistent=False)

        # Set output path immediately so intermediate saves go to the user's
        # desired location. Without this, PySubtrans defaults to writing in the
        # input directory as *.translated.srt — which can overwrite existing
        # translations.  (#259)
        if hasattr(project, 'subtitles') and project.subtitles:
            project.subtitles.outputpath = str(output_path)

        logger.info("[TRANSLATE]   New project created")

        # Log subtitle count
        if hasattr(project, 'subtitles') and project.subtitles:
            subtitle_count = getattr(project.subtitles, 'linecount', None)
            if subtitle_count is None:
                subtitle_count = 'unknown'
            logger.info("[TRANSLATE]   Subtitle lines: %s", subtitle_count)

        # Instructions are verified in the post-init_options() block above.
        # The old project.SetInstructions() path was dead code — that method
        # does not exist in current PySubtrans versions.

        # Initialize translator and translate
        logger.info("[TRANSLATE] Initializing translator...")
        translator = init_translator(options, translation_provider=provider)

        # =====================================================================
        # Deletion-aware parsing patch (refine cleaning stages)
        # =====================================================================
        # 清洗协议（角色卡）用"Translation> 留空"表示删除该条。PySubtrans
        # 原生把空译文判为 EmptyLinesError 并在重试指令中要求补全每一行，
        # 导致删除被系统性撤销（实测 1866 条仅删掉 7 条）。启用本补丁后，
        # 空译文 = 有意删除：不进校验、不触发重试，保存时自然丢弃。
        # 注意：补丁为全局粘性安装，每阶段必须显式置位空译文语义——
        # 阶段1/3 开启删除语义，阶段2（翻译）关闭（空译文走原生补译）。
        from .deletion_patch import set_allow_deletions
        set_allow_deletions(allow_empty_deletions)
        if allow_empty_deletions:
            from .deletion_patch import install_deletion_patch
            if install_deletion_patch():
                logger.info("[TRANSLATE]   Deletion-aware patch: ACTIVE "
                            "(empty Translation> = intentional delete, no re-fill)")
            else:
                logger.info("[TRANSLATE]   Deletion-aware patch: already installed")

        # =====================================================================
        # Cloud → Local failover（云端故障本地接管）
        # =====================================================================
        _failover = None
        if cloud_fallback:
            from .failover_patch import FailoverController, install_cloud_failover_patch
            _failover = FailoverController(
                cloud_fallback,
                log_fn=lambda msg: logger.info("[TRANSLATE]   %s", msg))
            if hasattr(translator, 'client') and translator.client:
                _failover.bind_client(translator.client)
            install_cloud_failover_patch(_failover)
            logger.info("[TRANSLATE]   Cloud-failover patch: ARMED → "
                        "%s%s model=%s",
                        cloud_fallback.get('server_address'),
                        cloud_fallback.get('endpoint'),
                        cloud_fallback.get('model'))

        # =====================================================================
        # Thinking-model workaround: patch response parsing (ALWAYS active)
        # =====================================================================
        # 思考型模型（Qwen3/3.5 系等）即使关闭 THINKING 开关，也可能把全部
        # 输出写进 'reasoning'（Ollama）或 'reasoning_content'（OpenAI/LM
        # Studio 约定）而 'content' 为空 —— PySubtrans 只读 content，翻译
        # 静默丢失、token 白烧。此补丁在 content 为空时把思考字段回填为
        # content 兜底；content 非空时无任何影响，因此对所有模型常开。
        _client = getattr(translator, 'client', None)
        if _client and hasattr(_client, '_process_api_response'):
            _original_process = _client._process_api_response

            def _patched_process_api_response(content, result, _orig=_original_process):
                """Patched to extract thinking output from reasoning fields."""
                import copy
                patched_content = copy.deepcopy(content)
                choices = patched_content.get('choices', [])
                for choice in choices:
                    msg = choice.get('message', {})
                    if not msg.get('content'):
                        for _key in ('reasoning_content', 'reasoning'):
                            if msg.get(_key):
                                msg['content'] = msg[_key]
                                logger.debug("[TRANSLATE]   [thinking-patch] Moved "
                                             "'%s' -> 'content' (%d chars)",
                                             _key, len(msg['content']))
                                break
                return _orig(patched_content, result)

            _client._process_api_response = _patched_process_api_response
            logger.info("[TRANSLATE]   Thinking-model fallback patch: ACTIVE "
                        "(reasoning_content/reasoning -> content when empty)")
        else:
            logger.warning("[TRANSLATE]   WARNING: Could not install thinking-model "
                           "fallback — translator.client not found")

        if emit_raw_output:
            def _make_raw_wrapper(level):
                """Create a log-forwarding wrapper for a PySubtrans event signal."""
                def _wrapper(sender, message=None, **kwargs):
                    msg = message
                    if msg is None and kwargs:
                        msg = kwargs.get('message')
                        if msg is None and kwargs:
                            msg = " ".join(str(v) for v in kwargs.values())
                    if msg is not None:
                        logger.log(level, "%s", msg)
                return _wrapper

            translator.events._default_error_wrapper = _make_raw_wrapper(logging.ERROR)
            translator.events._default_warning_wrapper = _make_raw_wrapper(logging.WARNING)
            translator.events._default_info_wrapper = _make_raw_wrapper(logging.INFO)

            # Connect the wrappers to the Blinker signals - this was missing!
            # Without this, the wrappers are replaced but never actually receive events
            translator.events.connect_default_loggers()

        # =========================================================================
        # A1: Diagnostic token logging — track batch results and failures
        # =========================================================================
        _batch_stats = {'total': 0, 'no_matches': 0, 'errors': 0}

        def _diagnostic_batch_handler(sender, **kwargs):
            """Track batch translation results for diagnostic summary.

            batch_translated fires for every batch attempt, regardless of whether
            translations were extracted. We count it as 'total' only — success is
            determined by subtracting known failures.
            """
            _batch_stats['total'] += 1

        def _diagnostic_warning_handler(sender, message=None, **kwargs):
            """Capture translation warnings with context."""
            msg = message or kwargs.get('message', '')
            if msg is None:
                return
            msg_str = str(msg)
            if 'No matches' in msg_str or 'no matches' in msg_str:
                _batch_stats['no_matches'] += 1
                # Warning fires before batch_translated increments total, so +1
                logger.warning("\n[TRANSLATE] *** NO MATCHES DETECTED (batch #%d) ***",
                               _batch_stats['total'] + 1)
                logger.warning("[TRANSLATE]   The model's response didn't match PySubtrans's expected format.")
                logger.warning("[TRANSLATE]   Raw warning: %s", msg_str[:500])
                if provider_config.get('max_tokens'):
                    logger.warning("[TRANSLATE]   max_tokens was set to: %s",
                                   provider_config['max_tokens'])
                logger.warning("[TRANSLATE]   Consider: smaller --max-batch-size, different model, or cloud provider")
                # 内容类失败上报（连续阈值判定见 failover_patch，按批去重）
                if _failover:
                    _failover.record_content_failure(
                        _failover.normalize_label(msg_str))

        def _diagnostic_error_handler(sender, message=None, **kwargs):
            """Track all translation errors — not just HTTP errors."""
            _batch_stats['errors'] += 1
            msg = message or kwargs.get('message', '')
            if msg:
                msg_str = str(msg)
                # 限流类上报（滑动窗口/累计时长判定见 failover_patch）
                if _failover and any(code in msg_str for code in ('429', '408', 'Rate limit', 'rate limit', 'FreeUsageLimit')):
                    _failover.record_throttle()
                # Error count serves as attempt number (includes retries on same batch)
                _err_num = _batch_stats['errors']
                # Categorize known failure modes and give relevant advice
                if any(code in msg_str for code in ('502', '500', '503', 'Server Error', 'timed out')):
                    logger.error("\n[TRANSLATE] *** SERVER ERROR (attempt #%d) ***", _err_num)
                    logger.error("[TRANSLATE]   %s", msg_str[:500])
                    # Parse the error to give relevant advice
                    msg_lower = msg_str.lower()
                    if 'unable to load model' in msg_lower:
                        logger.error("[TRANSLATE]   The model blob failed to load. Possible causes:")
                        logger.error("[TRANSLATE]     - Corrupt download — try: ollama rm <model> then re-pull")
                        logger.error("[TRANSLATE]     - Ollama version too old for this GGUF format — try: ollama update")
                        logger.error("[TRANSLATE]     - Insufficient VRAM — try a smaller quantization (Q4 instead of Q6/Q8)")
                    elif 'context length' in msg_lower or 'too long' in msg_lower:
                        logger.error("[TRANSLATE]   Context overflow — reduce --max-batch-size or use a model with larger context.")
                    elif 'timed out' in msg_lower or 'timeout' in msg_lower:
                        logger.error("[TRANSLATE]   Request timed out — the model may be too slow. Try a smaller model or smaller batch size.")
                    else:
                        logger.error("[TRANSLATE]   Check Ollama server logs for details: ollama logs")
                    # Ollama's internal debug logs (GGUF parsing, tensor loading,
                    # VRAM allocation) are only available server-side — they don't
                    # flow through the HTTP API. Guide the user when debug is on.
                    if debug and _err_num == 1:
                        logger.error("[TRANSLATE]   NOTE: SubTransJAV debug shows client-side HTTP traffic only.")
                        logger.error("[TRANSLATE]   For Ollama's own server-side logs (model loading, GGUF errors):")
                        logger.error("[TRANSLATE]     Windows: check the Ollama app log or run 'ollama logs'")
                        logger.error("[TRANSLATE]     Linux/macOS: journalctl -u ollama or OLLAMA_DEBUG=1 ollama serve")
                elif 'No text returned' in msg_str or 'no text' in msg_str.lower():
                    logger.error("\n[TRANSLATE] *** EMPTY RESPONSE (attempt #%d) ***", _err_num)
                    logger.error("[TRANSLATE]   The model returned no text. Possible causes:")
                    logger.error("[TRANSLATE]     - Model failed to generate (out of memory, crashed)")
                    logger.error("[TRANSLATE]     - Streaming response contained no content chunks")
                    logger.error("[TRANSLATE]     - Model is too large for available VRAM")
                    logger.error("[TRANSLATE]   Try: smaller model, reduce --max-batch-size, or check Ollama logs")
                else:
                    logger.error("\n[TRANSLATE] *** ERROR (attempt #%d): %s ***", _err_num, msg_str[:300])

        def _diagnostic_info_handler(sender, message=None, **kwargs):
            """批次结果上报：解析 'N lines and M untranslated' 判定批次成败。"""
            if not _failover:
                return
            msg = message or kwargs.get('message', '')
            if not msg:
                return
            import re as _re
            text = str(msg)
            m = _re.search(r'(\d+) lines and (\d+) untranslated', text)
            if m:
                try:
                    _failover.record_batch_ok(
                        untranslated=int(m.group(2)),
                        batch_label=_failover.normalize_label(text))
                except (ValueError, TypeError):
                    pass

        # Connect diagnostic handlers to translator events
        if hasattr(translator, 'events'):
            if hasattr(translator.events, 'batch_translated'):
                translator.events.batch_translated.connect(_diagnostic_batch_handler)
            # Also hook warning/error signals for "No matches" detection
            if hasattr(translator.events, 'warning'):
                translator.events.warning.connect(_diagnostic_warning_handler)
            if hasattr(translator.events, 'error'):
                translator.events.error.connect(_diagnostic_error_handler)
            if hasattr(translator.events, 'info'):
                translator.events.info.connect(_diagnostic_info_handler)

        # =========================================================================
        # DIAGNOSTIC: Translation Start
        # =========================================================================
        import time as _time
        _translation_start = _time.time()
        _translation_success = False
        logger.info("\n[TRANSLATE] Starting translation...")
        logger.info("[TRANSLATE]   This may take several minutes depending on batch size and model speed.")
        logger.info("[TRANSLATE]   If using local LLM, watch for timeout errors (consider smaller batch size).")
        logger.info("")

        # Translate subtitles (with timing regardless of success/failure)
        try:
            project.TranslateSubtitles(translator)
            _translation_success = True
        finally:
            _translation_elapsed = _time.time() - _translation_start

            # =====================================================================
            # P1/P2: Ground-truth success detection via PySubtrans project state
            # =====================================================================
            # The event-signal approach (counting batch_translated / error signals)
            # is unreliable: PySubtrans catches TranslationResponseError internally
            # and fires batch_translated anyway — errors counter stays at 0 even
            # when ALL batches fail. Instead, check what PySubtrans actually stored.
            _any_translated = False
            _all_translated = False
            if hasattr(project, 'subtitles') and project.subtitles:
                _any_translated = getattr(project.subtitles, 'any_translated', False)
                _all_translated = getattr(project.subtitles, 'all_translated', False)

            if _translation_success:
                logger.info("\n[TRANSLATE] Translation completed in %.1fs", _translation_elapsed)
            else:
                logger.error("\n[TRANSLATE] Translation FAILED after %.1fs", _translation_elapsed)

            # Batch event stats (advisory — may undercount failures)
            logger.info("[TRANSLATE] Batch statistics:")
            logger.info("[TRANSLATE]   Batches processed: %d", _batch_stats['total'])
            if _batch_stats['errors'] > 0:
                logger.info("[TRANSLATE]   Errors (signal-based): %d", _batch_stats['errors'])
            if _batch_stats['no_matches'] > 0:
                logger.warning("[TRANSLATE]   'No matches' failures: %d", _batch_stats['no_matches'])
                logger.warning("[TRANSLATE]   The LLM produced output PySubtrans couldn't parse.")
                logger.warning("[TRANSLATE]   Try: smaller --max-batch-size, different model, or cloud provider")
            # Successful batch count — cross-check signal-based counter with
            # ground truth. Signal counters undercount failures (PySubtrans
            # fires batch_translated even on server errors), so if ground
            # truth says nothing was translated, don't claim any succeeded.
            _successful = _batch_stats['total'] - _batch_stats['errors'] - _batch_stats['no_matches']
            if _successful > 0 and _batch_stats['total'] > 0:
                if _any_translated:
                    logger.info("[TRANSLATE]   Successful batches: %d/%d", _successful, _batch_stats['total'])
                else:
                    # Signal counters say N succeeded but ground truth disagrees
                    logger.warning("[TRANSLATE]   Successful batches: 0/%d "
                                   "(signal counters reported %d, but no subtitles were translated)",
                                   _batch_stats['total'], _successful)

            # Ground-truth: what PySubtrans actually stored in the project
            logger.info("[TRANSLATE] Translation result (ground truth):")
            logger.info("[TRANSLATE]   Any subtitles translated: %s",
                        'YES' if _any_translated else 'NO')
            logger.info("[TRANSLATE]   All subtitles translated: %s",
                        'YES' if _all_translated else 'NO')

            # Override success based on ground truth — if TranslateSubtitles()
            # didn't raise but nothing was actually translated, it's a failure.
            if _translation_success and not _any_translated and _batch_stats['total'] > 0:
                _translation_success = False
                logger.error("[TRANSLATE] *** ALL %d BATCHES FAILED — "
                             "no subtitles were translated ***", _batch_stats['total'])
                logger.error("[TRANSLATE]   Common causes: model returned empty content (thinking mode),")
                logger.error("[TRANSLATE]   output format not parseable, or server errors.")

        # Save translation
        logger.info("[TRANSLATE] Saving translated subtitles...")
        saved_path = None
        try:
            saved_path = project.SaveTranslation(str(output_path))
        except TypeError:
            saved_path = project.SaveTranslation()
        except Exception as e:
            logger.warning("[TRANSLATE]   Warning: SaveTranslation error: %s", e)
            saved_path = None

        # Convert to Path if needed
        if isinstance(saved_path, (str, Path)):
            output_path = Path(saved_path)

        # Verify the save actually produced a file.
        # PySubtrans' SubtitleProject.SaveTranslation() catches exceptions
        # internally (logging.error) and returns None — we never see the error.
        # Check the filesystem to know if the save actually worked.
        _save_succeeded = output_path.is_file()
        if not _save_succeeded:
            logger.warning("[TRANSLATE]   WARNING: SaveTranslation did not produce output at: %s",
                           output_path)

        # =====================================================================
        # Issue 2: Clean up PySubtrans' default .translated.srt artifact
        # =====================================================================
        # PySubtrans creates a .translated.srt file next to the input during
        # SaveProject() (line 303-304 of SubtitleProject.py) using the default
        # self.outputpath. When we then call SaveTranslation(output_path) with
        # a different path (e.g., .english.srt or a user-specified directory),
        # we end up with TWO output files. Clean up the redundant artifact.
        # IMPORTANT: Only clean up if the intended save succeeded — otherwise
        # the artifact is the ONLY copy of the translation.
        if hasattr(project, 'subtitles') and project.subtitles:
            _default_outpath = getattr(project.subtitles, 'outputpath', None)
            if _default_outpath:
                _default_outpath = Path(_default_outpath)
                if _default_outpath.exists() and _default_outpath.resolve() != output_path.resolve():
                    if _save_succeeded:
                        try:
                            _default_outpath.unlink()
                            logger.info("[TRANSLATE]   Cleaned up intermediate artifact: %s",
                                        _default_outpath.name)
                        except OSError as _e:
                            logger.warning("[TRANSLATE]   Warning: Could not remove artifact %s: %s",
                                           _default_outpath.name, _e)
                    else:
                        # The intended save failed but the artifact exists —
                        # this IS the translation. Report it as the output.
                        output_path = _default_outpath
                        logger.info("[TRANSLATE]   Using fallback output: %s",
                                    _default_outpath.name)

        # =========================================================================
        # DIAGNOSTIC: Final Summary — truthful status
        # =========================================================================
        logger.info("")
        logger.info("[TRANSLATE] " + "=" * 50)
        if _translation_success:
            logger.info("[TRANSLATE]   TRANSLATION COMPLETE")
            logger.info("[TRANSLATE]   Output: %s", output_path)
        else:
            logger.error("[TRANSLATE]   TRANSLATION FAILED")
            logger.error("[TRANSLATE]   All batches returned errors — no subtitles translated.")
            logger.error("[TRANSLATE]   Output file may be empty or contain only originals.")
        logger.info("[TRANSLATE] " + "=" * 50)
        logger.info("")

        # Return None on total failure so cli.py reports it as failed
        return output_path if _translation_success else None

    except Exception:
        logger.debug("Translation error traceback:", exc_info=True)
        raise
