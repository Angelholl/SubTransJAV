"""
SubTransJAV Translation Module

Provides AI-powered subtitle translation via PySubtrans.

For programmatic usage (e.g., from the refine pipeline):
    from subtransjav.translate import translate_with_config

    result = translate_with_config(
        input_path="subtitles.srt",
        provider="deepseek",
        target_lang="english"
    )
"""

from . import core, providers, service

# Export high-level service API for direct usage
from .service import (
    ConfigurationError,
    TranslationError,
    translate_with_config,
)

__all__ = [
    # Submodules
    'core',
    'providers',
    'service',
    # Service layer exports
    'translate_with_config',
    'TranslationError',
    'ConfigurationError',
]
