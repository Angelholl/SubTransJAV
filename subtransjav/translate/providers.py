"""
Provider configurations for translation services.
"""

PROVIDER_CONFIGS = {
    'deepseek': {
        'pysubtrans_name': 'DeepSeek',
        # v1.8.14 (#325): DeepSeek announced new model names 2026-05-06.
        # 'deepseek-chat' / 'deepseek-reasoner' deprecate 2026-07-24, replaced
        # by 'deepseek-v4-flash' (non-thinking, was deepseek-chat) and
        # 'deepseek-v4-pro' (thinking, was deepseek-reasoner).
        # Source: https://api-docs.deepseek.com/zh-cn/
        # Users wanting the thinking model can override via: --model deepseek-v4-pro
        'model': 'deepseek-v4-flash',
        'env_var': 'DEEPSEEK_API_KEY',
        'api_base': 'https://api.deepseek.com'
    },
    'openrouter': {
        'pysubtrans_name': 'OpenRouter',
        # v1.8.14 (#325): OpenRouter routes to DeepSeek; OpenRouter typically lags
        # the upstream model catalog. Keep deepseek-chat as the routed default until
        # OpenRouter publishes deepseek-v4-flash; users can override via
        # --model deepseek/deepseek-v4-flash once available.
        'model': 'deepseek/deepseek-chat',
        'env_var': 'OPENROUTER_API_KEY',
        'api_base': 'https://openrouter.ai/api/v1'
    },
    'gemini': {
        'pysubtrans_name': 'Gemini',
        'model': 'gemini-2.0-flash',
        'env_var': 'GEMINI_API_KEY'
    },
    'claude': {
        'pysubtrans_name': 'Claude',
        'model': 'claude-3-5-haiku-20241022',
        'env_var': 'ANTHROPIC_API_KEY'
    },
    'gpt': {
        'pysubtrans_name': 'OpenAI',
        'model': 'gpt-4o-mini',
        'env_var': 'OPENAI_API_KEY'
    },
    'glm': {
        'pysubtrans_name': 'Custom Server',  # Custom Server avoids Responses API misrouting (#178)
        'model': 'glm-4-flash',
        'env_var': 'GLM_API_KEY',
        'server_address': 'https://open.bigmodel.cn',
        'endpoint': '/api/paas/v4/chat/completions',
    },
    'groq': {
        'pysubtrans_name': 'Custom Server',  # Custom Server avoids Responses API misrouting (#178)
        'model': 'llama-3.3-70b-versatile',
        'env_var': 'GROQ_API_KEY',
        'server_address': 'https://api.groq.com',
        'endpoint': '/openai/v1/chat/completions',
    },
    'ollama': {
        'pysubtrans_name': 'Custom Server',  # Uses OpenAI-compatible /v1/chat/completions
        'model': 'gemma3:12b',         # Default; OllamaManager.recommend_model() overrides at runtime
        'env_var': None,               # No API key needed
        'server_address': 'http://localhost:11434',
        'endpoint': '/v1/chat/completions',
        'supports_conversation': True,
        'supports_system_messages': True,
        'supports_streaming': True,
    },
    'custom': {
        'pysubtrans_name': 'Custom Server',  # Custom Server avoids Responses API misrouting (#178)
        'model': '',                   # User provides via --translate-model
        'env_var': None,               # API key optional, provided via --translate-api-key
        'supports_streaming': True,    # Enable live progress for long cloud batches
        # vLLM/LM Studio 要求 system message 必须在 messages 数组首位，
        # 但 PySubtrans 重试时会将 retry system message 追加到末尾，
        # 导致 "System message must be at the beginning" 500 错误。
        # 关闭 system message 支持，retry 指令将嵌入 user 消息中。
        'supports_system_messages': False,
    }
}

SUPPORTED_SOURCES = {'japanese', 'korean', 'chinese', 'english'}
SUPPORTED_TARGETS = {'english', 'chinese', 'indonesian', 'portuguese', 'spanish', 'french'}
