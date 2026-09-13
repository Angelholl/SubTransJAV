"""GUI 用户可见文案字符串表（唯一中文来源，webview-free 可测模块）。

- Python 侧（api.py / main.py）通过 ``msg(key, **kw)`` 取文案；
- JS 侧无法 import Python，app.js 顶部维护一个 ``const MSG = {...}``
  镜像对象（注释注明与本表对应，键名保持一致）。

约定：
- 值为中文文案；占位符用 str.format 风格（``{n}``/``{e}``/``{code}``），
  错误消息中夹带异常详情时占位符名固定为 ``e``；
- ``msg()`` 对未知键回退返回键名本身、格式化失败回退原文，绝不抛异常；
- 日志哨兵前缀（``[SUCCESS]``/``[ERROR]``/``[CANCELLED]``/``[WARN]``）
  保留英文标记（前端/测试依赖），仅后缀文案走本表。
"""

# 语义键 -> 中文文案
MSG = {
    # ---- 窗口 / 文件对话框（api.py）----
    "no_active_window": "无活动窗口",
    "no_folder_selected": "未选择文件夹",
    "no_files_selected": "未选择文件",
    "no_srt_in_folder": "所选文件夹中未找到 .srt 字幕",
    "folder_opened": "文件夹已打开",
    "cannot_open_folder": "无法打开文件夹：{e}",

    # ---- 版本信息（api.py）----
    "version_load_failed": "无法加载版本信息",

    # ---- 翻译进程管理（api.py）----
    "translation_in_progress": "翻译已在进行中",
    "translation_started": "翻译已启动，共 {n} 个文件",
    "translation_cancelled": "翻译已取消",
    "no_translation_in_progress": "当前没有进行中的翻译",
    "translation_still_starting": "翻译仍在启动中，请稍后重试",
    "process_exit_code": "翻译进程已退出，退出码 {code}",

    # ---- 日志哨兵行后缀（api.py；前缀 [SUCCESS]/[ERROR]/[CANCELLED] 保留英文）----
    "log_success": "翻译完成。",
    "log_cancelled": "翻译已取消。",
    "log_exit_code": "退出码：{code}",

    # ---- GUI 启动 / 控制台日志（main.py）----
    "window_created": "窗口创建成功",
    "starting_webview": "正在启动 PyWebView...（debug={debug}）",
    "dom_events_bound": "DOM 拖放事件绑定成功",
    "dom_events_bind_failed": "警告：DOM 事件绑定失败：{e}",
    "dom_events_fallback": "拖放功能可能不可用，请改用「添加文件」按钮。",
    "gui_banner": "净语翻译 GUI v{version}",
    "appusermodelid_failed": "警告：设置 AppUserModelID 失败：{e}",
    "asset_not_found": "错误：资源文件未找到！",
    "gui_start_failed": "错误：GUI 启动失败！",
    "drop_event_error": "处理拖放事件出错：{e}",
    "webview2_check_failed": "警告：无法检查 WebView2 运行时状态：{e}",
}


def msg(key: str, **kw) -> str:
    """按语义键取中文文案；支持 ``{name}`` 占位符格式化。

    - 未知键：回退返回键名本身（便于发现缺失，不抛异常）；
    - 格式化失败（缺参/占位符非法）：回退返回未格式化原文。
    """
    text = MSG.get(key, key)
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text
