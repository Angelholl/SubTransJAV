"""
Refine - v2 两阶段字幕净语翻译流水线（SubTransJAV 整合版）
=========================================================
阶段A 净语+翻译(ja→zh) → 阶段B 审校+抛光(zh→zh)

每阶段可独立选择服务商与模型：
  deepseek  : DeepSeek 官方 API（原生通道）
  zen       : OpenCode Zen（OpenAI 兼容，含免费模型）
  lmstudio  : 本地 LM Studio（OpenAI 兼容端点，默认 http://localhost:1234/v1）
  custom    : 任意 OpenAI 兼容接口（须提供 endpoint）
"""

from .batch import find_srt_files, scan_summary
from .config import (
    DEFAULT_BATCH_CLOUD,
    DEFAULT_BATCH_LOCAL,
    PROVIDER_TEXT,
    STAGE_NAMES,
    RefineConfig,
    StageConfig,
    ensure_language_support,
)
from .glossary import (
    format_glossary_block,
    load_glossary,
    match_glossary,
    save_glossary,
)
from .tm import TranslationMemory

__all__ = [
    "RefineConfig",
    "StageConfig",
    "STAGE_NAMES",
    "DEFAULT_BATCH_LOCAL",
    "DEFAULT_BATCH_CLOUD",
    "PROVIDER_TEXT",
    "ensure_language_support",
    "load_glossary",
    "save_glossary",
    "match_glossary",
    "format_glossary_block",
    "TranslationMemory",
    "find_srt_files",
    "scan_summary",
]
