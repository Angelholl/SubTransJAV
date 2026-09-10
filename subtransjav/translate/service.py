"""
Translation Service Layer.

Provides a high-level API for subtitle translation that can be called
directly from the main pipeline without subprocess invocation.

This module encapsulates:
- Configuration resolution (settings + parameters)
- Provider setup and validation
- Instruction file fetching/caching
- Translation execution via core.py

Usage from main.py:
    from subtransjav.translate.service import translate_with_config

    result_path = translate_with_config(
        input_path=output_path,
        provider=args.translate_provider,
        target_lang=args.translate_target,
        tone=args.translate_tone,
        api_key=args.translate_api_key
    )
"""

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from .providers import PROVIDER_CONFIGS, SUPPORTED_TARGETS
from .settings import load_settings
from .instructions import get_instruction_content
from .core import _normalize_api_base, _api_base_to_custom_server

logger = logging.getLogger(__name__)


class TranslationError(Exception):
    """Raised when translation fails."""
    pass


class ConfigurationError(Exception):
    """Raised when configuration is invalid."""
    pass


def _resolve_api_key(provider_config: dict, api_key: Optional[str] = None) -> str:
    """
    Resolve API key from parameter or environment variable.

    Args:
        provider_config: Provider configuration dict with 'env_var' key
        api_key: Explicit API key (highest priority)

    Returns:
        Resolved API key

    Raises:
        ConfigurationError: If no API key found
    """
    if api_key:
        return api_key

    env_var = provider_config.get('env_var')
    if env_var:
        key = os.getenv(env_var)
        if key:
            return key

    raise ConfigurationError(
        f"API key not found. Set {env_var} environment variable or provide api_key parameter."
    )


def _resolve_model(provider_config: dict, model: Optional[str] = None, settings: Optional[dict] = None) -> str:
    """
    Resolve model name with precedence: parameter > settings > provider default.

    Args:
        provider_config: Provider configuration dict with 'model' key
        model: Explicit model override
        settings: User settings dict

    Returns:
        Resolved model name
    """
    if model:
        return model

    if settings and settings.get('model'):
        return settings['model']

    return provider_config['model']


def _resolve_instruction_file(tone: str = "standard", refresh: bool = False) -> Optional[str]:
    """
    Resolve instruction file path by fetching content and caching to temp file.

    Args:
        tone: Translation tone ('standard' or 'pornify')
        refresh: Force refresh of cached content

    Returns:
        Path to instruction file, or None if unavailable
    """
    instruction_content = get_instruction_content(tone=tone, refresh=refresh)

    if not instruction_content:
        logger.debug(f"No instruction content available for tone: {tone}")
        return None

    # Save to temp file
    from subtransjav.utils.model_cache import project_models_root
    temp_dir = project_models_root() / "translate_instructions"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f'instructions_{tone}.txt'

    with open(temp_file, 'w', encoding='utf-8') as f:
        f.write(instruction_content)

    return str(temp_file)


def _build_provider_options(
    tone: str = "standard",
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    settings_model_params: Optional[dict] = None
) -> dict:
    """
    Build provider options with tone-aware defaults.

    Precedence: explicit params > settings > tone-aware defaults

    Args:
        tone: Translation tone for default selection
        temperature: Explicit temperature override
        top_p: Explicit top_p override
        settings_model_params: Model params from settings file

    Returns:
        Dict of provider options
    """
    # Start from tone-aware defaults
    if tone == 'pornify':
        default_temperature = 1.2
        default_top_p = 0.9
    else:
        default_temperature = 0.5
        default_top_p = 0.9

    result_temp = default_temperature
    result_top_p = default_top_p

    # Apply settings overrides
    if settings_model_params:
        if settings_model_params.get('temperature') is not None:
            try:
                result_temp = float(settings_model_params['temperature'])
            except (ValueError, TypeError):
                pass
        if settings_model_params.get('top_p') is not None:
            try:
                result_top_p = float(settings_model_params['top_p'])
            except (ValueError, TypeError):
                pass

    # Apply explicit parameter overrides (highest priority)
    if temperature is not None:
        result_temp = temperature
    if top_p is not None:
        result_top_p = top_p

    # Clamp to valid ranges
    result_temp = max(0.0, min(2.0, result_temp))
    result_top_p = max(0.0, min(1.0, result_top_p))

    return {
        'temperature': result_temp,
        'top_p': result_top_p
    }


def translate_with_config(
    input_path: str,
    provider: str = "deepseek",
    target_lang: str = "english",
    tone: str = "standard",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    source_lang: str = "japanese",
    output_path: Optional[str] = None,
    instruction_file: Optional[str] = None,
    scene_threshold: Optional[float] = None,
    max_batch_size: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stream: bool = False,
    debug: bool = False,
    extra_context: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    n_gpu_layers: int = -1,
    endpoint: Optional[str] = None,
    ollama_url: Optional[str] = None,
    auto_confirm: bool = False,
    ollama_max_tokens: Optional[int] = None,
    ollama_num_ctx: Optional[int] = None,
    preprocess_subtitles: bool = True,
    allow_empty_deletions: bool = False,
    cloud_fallback: Optional[dict] = None,
) -> Optional[Path]:
    """
    Translate subtitle file with full configuration resolution.

    This is the primary entry point for programmatic translation.
    Can be called directly from main.py without subprocess overhead.

    Configuration is resolved with the following precedence:
    1. Explicit parameters (highest priority)
    2. User settings file (~/.config/SubTransJAV/translate/settings.json)
    3. Built-in defaults (lowest priority)

    Args:
        input_path: Path to input SRT file
        provider: AI provider name ('deepseek', 'openrouter', 'gemini', 'claude', 'gpt')
        target_lang: Target language ('english', 'chinese', 'indonesian', 'spanish')
        tone: Translation tone ('standard' or 'pornify')
        api_key: API key (or set via environment variable)
        model: Model override (uses provider default if not specified)
        source_lang: Source language ('japanese', 'korean', 'chinese')
        output_path: Output file path (auto-generated if not specified)
        instruction_file: Custom instruction file path
        scene_threshold: Scene threshold in seconds
        max_batch_size: Maximum batch size for translation
        temperature: Model temperature (0.0-2.0)
        top_p: Model top_p (0.0-1.0)
        stream: Stream translation progress
        debug: Enable debug output
        extra_context: Additional context for translation (movie title, etc.)
        progress_callback: Optional callback for progress updates
        endpoint: Custom API endpoint URL (for OpenAI-compatible APIs)

    Returns:
        Path to translated file, or None on failure

    Raises:
        ConfigurationError: Invalid provider or missing API key
        TranslationError: Translation execution failed
        FileNotFoundError: Input file not found
    """
    # Validate input file exists
    input_file = Path(input_path)
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    # Validate provider
    provider = provider.lower()
    if provider == 'local':
        raise ConfigurationError(
            "provider='local' has been removed. "
            "Use provider='lmstudio' (local models) or provider='custom' (any OpenAI-compatible endpoint) instead."
        )
    provider_config = PROVIDER_CONFIGS.get(provider)
    if not provider_config:
        valid_providers = list(PROVIDER_CONFIGS.keys())
        raise ConfigurationError(f"Unknown provider: {provider}. Valid providers: {valid_providers}")

    # Validate target language
    if target_lang not in SUPPORTED_TARGETS:
        raise ConfigurationError(f"Unsupported target language: {target_lang}. Valid: {SUPPORTED_TARGETS}")

    # Custom provider requires an endpoint
    if provider == 'custom' and not endpoint:
        raise ConfigurationError(
            "Custom provider requires --translate-endpoint. Example:\n"
            "  --translate-provider custom --translate-endpoint http://localhost:11434/v1"
        )

    # Override endpoint if custom endpoint provided (same logic as cli.py)
    if endpoint:
        provider_config = dict(provider_config)  # Copy to avoid mutating original
        psn = provider_config.get('pysubtrans_name')
        if psn in ('OpenAI', 'DeepSeek'):
            # These providers handle api_base natively via their SDKs
            provider_config['api_base'] = _normalize_api_base(endpoint)
        else:
            # Route through Custom Server for reliable /chat/completions
            # access without reasoning model misclassification (#178)
            server_addr, endpoint_path = _api_base_to_custom_server(endpoint)
            provider_config['pysubtrans_name'] = 'Custom Server'
            provider_config['server_address'] = server_addr
            provider_config['endpoint'] = endpoint_path
            provider_config.pop('api_base', None)

    # Load user settings
    settings = load_settings()

    # Resolve API key (not needed for local/custom/ollama providers)
    if provider in ('custom', 'ollama'):
        resolved_api_key = api_key or ''
    else:
        resolved_api_key = _resolve_api_key(provider_config, api_key)

    # Resolve model
    resolved_model = _resolve_model(provider_config, model, settings)

    # Resolve instruction file
    if instruction_file:
        if not Path(instruction_file).exists():
            raise FileNotFoundError(f"Instruction file not found: {instruction_file}")
        resolved_instruction_file = instruction_file
    else:
        resolved_instruction_file = _resolve_instruction_file(tone)

    # Resolve processing options
    resolved_scene_threshold = scene_threshold if scene_threshold is not None else settings.get('scene_threshold', 60.0)
    resolved_max_batch_size = max_batch_size if max_batch_size is not None else settings.get('max_batch_size', 30)

    # Build provider options
    provider_options = _build_provider_options(
        tone=tone,
        temperature=temperature,
        top_p=top_p,
        settings_model_params=settings.get('model_params')
    )

    # Generate output path if not specified
    if output_path:
        resolved_output_path = Path(output_path)
    else:
        stem = input_file.stem
        # Remove existing language suffix if present
        parts = stem.split('.')
        if len(parts) > 1 and parts[-1] in ['japanese', 'english', 'ja', 'en', 'jp', 'chinese', 'indonesian', 'spanish']:
            stem = '.'.join(parts[:-1])
        resolved_output_path = input_file.parent / f"{stem}.{target_lang}.srt"

    # Log configuration
    logger.info(f"Translation: {input_file.name} -> {target_lang}")
    logger.debug(f"Provider: {provider} ({resolved_model})")
    logger.debug(f"Tone: {tone}")

    # =========================================================================
    # DIAGNOSTIC: Configuration Summary
    # =========================================================================
    logger.info("Translation Configuration:")
    logger.info("  Input file: %s", input_file)
    logger.info("  Output file: %s", resolved_output_path)
    logger.info("  Provider: %s", provider)
    logger.info("  Model: %s", resolved_model)
    logger.info("  Source: %s -> Target: %s", source_lang, target_lang)
    logger.info("  Tone: %s", tone)
    logger.info("  Max batch size: %s", resolved_max_batch_size)
    logger.info("  Scene threshold: %ss", resolved_scene_threshold)
    logger.info("  Stream: %s", stream)
    logger.info("  Provider options: %s", provider_options)
    if resolved_instruction_file:
        logger.info("  Instructions: %s", resolved_instruction_file)
    else:
        logger.info("  Instructions: (none)")

    # Report progress if callback provided
    if progress_callback:
        progress_callback(f"Translating {input_file.name} from {source_lang} to {target_lang}...")
        progress_callback(f"Provider: {provider} ({resolved_model})")

    # Execute translation via strategy pattern
    try:
        from .handlers import get_handler, TranslationContext

        # Extract token_budget from settings (None if not present → uses hardcoded defaults)
        _token_budget = settings.get('token_budget') or None

        ctx = TranslationContext(
            input_path=str(input_file),
            output_path=resolved_output_path,
            provider=provider,
            provider_config=provider_config,
            resolved_model=resolved_model,
            resolved_api_key=resolved_api_key,
            source_lang=source_lang,
            target_lang=target_lang,
            instruction_file=resolved_instruction_file,
            scene_threshold=resolved_scene_threshold,
            max_batch_size=resolved_max_batch_size,
            stream=stream,
            debug=debug,
            provider_options=provider_options,
            extra_context=extra_context,
            progress_callback=progress_callback,
            preprocess_subtitles=preprocess_subtitles,
            allow_empty_deletions=allow_empty_deletions,
            cloud_fallback=cloud_fallback,
            # Provider-specific params
            n_gpu_layers=n_gpu_layers,
            endpoint=endpoint,
            ollama_url=ollama_url,
            auto_confirm=auto_confirm,
            ollama_max_tokens=ollama_max_tokens,
            ollama_num_ctx=ollama_num_ctx,
            temperature=temperature,
            # Token budget overrides from settings
            token_budget=_token_budget,
        )

        handler = get_handler(provider, endpoint)
        result_path = handler.execute(ctx)

        if result_path:
            logger.info(f"Translation complete: {result_path}")
            if progress_callback:
                progress_callback(f"Translation complete: {Path(result_path).name}")
            return Path(result_path) if isinstance(result_path, str) else result_path
        else:
            raise TranslationError("Translation returned no result")

    except (ConfigurationError, FileNotFoundError, TranslationError):
        # Re-raise known exception types without wrapping — callers
        # distinguish them for targeted error messages.
        raise
    except Exception as e:
        logger.error(f"Translation failed: {e}")
        if debug:
            import traceback
            traceback.print_exc()
        raise TranslationError(f"Translation failed: {e}") from e
