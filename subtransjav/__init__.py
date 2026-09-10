"""
SubTransJAV —— 从 SubTransJAV TAB4（AI SRT Translate / 净语翻译）剥离的
独立字幕翻译项目。

子包：
    refine    v2 两阶段净语翻译流水线（阶段A 净语+翻译 → 阶段B 审校+抛光）
    translate PySubtrans 翻译引擎封装（service.translate_with_config）
    utils     控制台/模型缓存/进程树管理
    webview_gui pywebview 桌面界面
"""

from .__version__ import __version__ as __version__
from .__version__ import __version_display__ as __version_display__
