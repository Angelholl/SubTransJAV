"""
SubTransJAV —— 字幕翻译与精修工具包。

子包：
    refine    v2 两阶段净语翻译流水线（阶段A 净语+翻译 → 阶段B 审校+抛光）
    translate v2 LLM 翻译客户端（llm_client）与 provider 配置
    utils     控制台/模型缓存/进程树管理
    webview_gui pywebview 桌面界面
"""

from .__version__ import __version__ as __version__
from .__version__ import __version_display__ as __version_display__
