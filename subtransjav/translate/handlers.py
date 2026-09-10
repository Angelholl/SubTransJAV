"""
Provider-specific translation handlers (Strategy Pattern).

Each handler encapsulates the configuration, setup, and execution logic
for a specific translation provider category:
- OllamaHandler: Ollama-based local models
- CloudHandler: Cloud/API-based providers

Usage:
    handler = get_handler(provider, endpoint)
    result = handler.execute(context)
"""

import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .core import (
    translate_subtitle,
    _api_base_to_custom_server,
    cap_batch_size_for_context,
    compute_max_output_tokens,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared dataclass that carries all resolved config to the handlers
# ---------------------------------------------------------------------------

@dataclass
class TranslationContext:
    """Immutable bundle of resolved translation parameters passed to handlers."""

    input_path: str
    output_path: Path
    provider: str
    provider_config: dict
    resolved_model: str
    resolved_api_key: str
    source_lang: str
    target_lang: str
    instruction_file: Optional[str]
    scene_threshold: float
    max_batch_size: int
    stream: bool
    debug: bool
    provider_options: dict
    extra_context: Optional[str]
    progress_callback: Optional[Callable[[str], None]]
    preprocess_subtitles: bool
    allow_empty_deletions: bool
    cloud_fallback: Optional[dict]

    # Provider-specific params (only used by the relevant handler)
    n_gpu_layers: int = -1
    endpoint: Optional[str] = None
    ollama_url: Optional[str] = None
    auto_confirm: bool = False
    ollama_max_tokens: Optional[int] = None
    ollama_num_ctx: Optional[int] = None
    temperature: Optional[float] = None

    # Token budget overrides (from settings.json → token_budget)
    token_budget: Optional[dict] = None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class ProviderHandler(ABC):
    """Base class for provider-specific translation execution."""

    @abstractmethod
    def execute(self, ctx: TranslationContext) -> Optional[Path]:
        """Run the translation and return the output Path (or None on failure)."""
        ...

    def _call_translate(self, ctx: TranslationContext, **overrides) -> Optional[Path]:
        """Convenience wrapper around translate_subtitle with sensible defaults.

        Any keyword in *overrides* replaces the corresponding field from *ctx*.
        """
        params = dict(
            input_path=str(ctx.input_path),
            output_path=ctx.output_path,
            provider_config=ctx.provider_config,
            model=ctx.resolved_model,
            api_key=ctx.resolved_api_key,
            source_lang=ctx.source_lang,
            target_lang=ctx.target_lang,
            instruction_file=ctx.instruction_file,
            scene_threshold=ctx.scene_threshold,
            max_batch_size=ctx.max_batch_size,
            stream=ctx.stream,
            debug=ctx.debug,
            provider_options=ctx.provider_options,
            extra_context=ctx.extra_context,
            emit_raw_output=True,
            preprocess_subtitles=ctx.preprocess_subtitles,
            allow_empty_deletions=ctx.allow_empty_deletions,
        )
        params.update(overrides)
        return translate_subtitle(**params)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class OllamaHandler(ProviderHandler):
    """Handler for Ollama-based local models.

    Responsibilities:
    - Server detection & model readiness via OllamaManager
    - Context-window-aware batch size capping
    - Dynamic num_ctx / max_tokens resolution
    """

    def execute(self, ctx: TranslationContext) -> Optional[Path]:
        from .ollama_manager import OllamaManager

        print("\n[SERVICE] Ollama Provider Mode", file=sys.stderr)
        mgr = OllamaManager(base_url=ctx.ollama_url or ctx.endpoint)

        readiness = mgr.ensure_ready(
            model=ctx.resolved_model,
            auto_start=True,
            auto_pull=ctx.auto_confirm,
            interactive=False,
        )

        resolved_model = readiness['model']
        n_ctx = readiness['num_ctx']

        # User --ollama-num-ctx override
        if ctx.ollama_num_ctx is not None:
            n_ctx = ctx.ollama_num_ctx

        resolved_max_batch = cap_batch_size_for_context(ctx.max_batch_size, n_ctx, ctx.token_budget)
        max_tokens = compute_max_output_tokens(resolved_max_batch, n_ctx, ctx.token_budget)
        if ctx.ollama_max_tokens is not None:
            max_tokens = ctx.ollama_max_tokens

        provider_options = dict(ctx.provider_options)
        if ctx.temperature is None and readiness.get('temperature'):
            provider_options['temperature'] = readiness['temperature']
        provider_options['num_ctx'] = n_ctx

        provider_config = dict(ctx.provider_config)
        provider_config['max_tokens'] = max_tokens

        # Override supports_system_messages based on actual model template
        if not readiness.get('supports_system_messages', True):
            provider_config['supports_system_messages'] = False

        if readiness.get('base_url'):
            server_addr, endpoint_path = _api_base_to_custom_server(readiness['base_url'])
            provider_config['server_address'] = server_addr
            provider_config['endpoint'] = endpoint_path

        print(f"[SERVICE]   Model: {resolved_model}", file=sys.stderr)
        print(f"[SERVICE]   num_ctx={n_ctx}, batch_size={resolved_max_batch}, max_tokens={max_tokens}",
              file=sys.stderr)

        return self._call_translate(
            ctx,
            provider_config=provider_config,
            model=resolved_model,
            api_key='',
            max_batch_size=resolved_max_batch,
            stream=True,  # Always stream for local models
            provider_options=provider_options,
        )



# ---------------------------------------------------------------------------
# Cloud (OpenAI-compatible APIs, DeepSeek, Gemini, Claude, etc.)
# ---------------------------------------------------------------------------

class CloudHandler(ProviderHandler):
    """Handler for cloud / API-based providers.

    This is the simplest handler — it delegates directly to translate_subtitle
    with the resolved configuration.
    """

    def execute(self, ctx: TranslationContext) -> Optional[Path]:
        print(f"\n[SERVICE] Cloud Provider Mode: {ctx.provider}", file=sys.stderr)
        print(
            f"[SERVICE]   Using provider config: {ctx.provider_config.get('pysubtrans_name', ctx.provider)}",
            file=sys.stderr,
        )
        if 'api_base' in ctx.provider_config:
            print(f"[SERVICE]   API base: {ctx.provider_config['api_base']}", file=sys.stderr)

        return self._call_translate(
            ctx,
            cloud_fallback=ctx.cloud_fallback,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_handler(provider: str, endpoint: Optional[str] = None) -> ProviderHandler:
    """Return the appropriate handler for the given provider.

    Args:
        provider: Lowercase provider name (e.g. 'ollama', 'deepseek', 'custom')
        endpoint: Optional custom endpoint URL

    Returns:
        A concrete ProviderHandler instance.
    """
    if provider == 'ollama':
        return OllamaHandler()
    return CloudHandler()
