"""
Instructions management - load from local defaults with optional cache.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def get_default_instruction_path(tone: str) -> Path:
    """Get the path of the bundled default instruction file for a tone."""
    from .settings import get_settings_path
    return get_settings_path().parent / 'defaults' / f'{tone}.txt'


def load_bundled_default(tone: str) -> Optional[str]:
    """Load bundled default instruction file."""
    try:
        from importlib import resources
        default_file = resources.files('subtransjav.translate.defaults').joinpath(f'{tone}.txt')
        if default_file.is_file():
            return default_file.read_text(encoding='utf-8')
    except Exception as e:
        logger.debug(f"No bundled default for tone '{tone}': {e}")
    return None


def get_instruction_content(tone: str = 'standard', refresh: bool = False) -> Optional[str]:
    """
    Get instruction content (local only, no network access).

    Strategy:
    1. Load bundled default instruction file shipped with the package.

    Args:
        tone: Instruction tone (standard, pornify, etc.)
        refresh: Kept for signature compatibility; no-op (content is local)

    Returns:
        Instruction content or None
    """
    bundled = load_bundled_default(tone)
    if bundled:
        logger.info("Using bundled default instructions")
        return bundled

    logger.error(f"Failed to load instructions for tone: {tone}")
    return None
