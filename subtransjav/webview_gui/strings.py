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

    # ---- CLI 帮助 / 自举（main.py）----
    "app_title": "净语翻译 · SubTransJAV Translate",
    "cli_description": "净语翻译 · SubTransJAV 桌面 GUI"
                       "（两阶段字幕流水线：阶段A 净语+翻译 → 阶段B 审校+抛光）",
    "cli_help_debug": "以调试模式启动 WebView（可打开开发者工具）",
    "cli_help_version": "打印程序版本号后退出",
    "setup_creating_venv": "[SETUP] 首次运行，正在创建虚拟环境...",
    "setup_env_ready": "[SETUP] 环境就绪，正在启动程序...",
    "setup_installing": "[SETUP] 正在安装依赖，请稍候...",
    "setup_install_done": "[SETUP] 安装完成，正在重启程序...",
    "setup_init_failed": "[SETUP] 环境初始化失败: {e}",
    "setup_unknown_error": "[SETUP] 发生未知错误: {e}",
    "setup_manual_hint": "请尝试手动运行: pip install subtransjav[gui]",
    "setup_press_enter": "按回车键退出...",

    # ---- 角色卡目录守卫（api.py）----
    "template_dir_not_allowed": "模板目录仅允许服务端默认目录或本会话选择的目录: {path}",

    # ---- refine 失败诊断提示（api.py）----
    "tip_region_blocked": "该模型对中国大陆区域封锁(403)，请换其他模型",
    "tip_rate_limited": "免费额度限速(429)，稍等几分钟再试或换模型",
    "tip_upstream_down": "上游服务临时宕机，稍后重试或换模型",
    "tip_invalid_key": "密钥无效或未配置",
    "tip_check_key_network": "请检查密钥/网络",

    # ---- 系统状态（api.py）----
    "grammar_hints_available": "日语形态素分析提示（阶段A 自动启用）",
    "grammar_hints_unavailable": "日语形态素分析提示（未安装 sudachipy）",

    # ---- open_url / 目录（api.py）----
    "url_scheme_unsupported": "仅支持 http/https 链接",
    "dir_not_exist": "目录不存在: {path}",

    # ---- 模型列表 / 连通性测试（api.py）----
    "endpoint_missing": "缺少接口地址(endpoint)",
    "endpoint_scheme_unsupported": "接口地址仅支持 http/https",
    "api_key_missing": "缺少 API Key（请先在密钥区保存）",
    "model_name_missing": "未填写模型名",
    "stage_test_ping": "回复：OK",
    "stage_test_reasoning": "(推理模型)...{tail}",
    "stage_test_empty": "(空响应)",

    # ---- 角色卡模板（api.py）----
    "invalid_stage_tag": "无效阶段标识：{tag}（应为 A 或 B）",
    "template_file_missing": "模板文件不存在：{path}",
    "template_b_note": "阶段B(审校抛光)的硬性豁免段由引擎运行时自动追加，无需写在本卡内",

    # ---- 质量报告导读（api.py）----
    "guide_path_empty": "路径为空，请先指定导读文件",
    "guide_path_denied": "路径不允许访问：{e}",
    "guide_file_missing": "文件不存在：{path}",
    "guide_suffix_only": "仅支持质量报告导读文件（*{suffix}），拒绝读取其他文件：{name}",
    "guide_bad_format": "导读文件格式异常：顶层应为 JSON 对象",
    "guide_corrupted": "导读文件损坏：不是有效的 JSON，请重新生成质量报告",

    # ---- 文件对话框类型 / 取消（api.py）----
    "file_type_glossary": "词库文件 (*.csv;*.txt)",
    "file_type_csv": "CSV 文件 (*.csv)",
    "file_type_sqlite": "SQLite 数据库 (*.db)",
    "file_type_all": "所有文件 (*.*)",
    "dialog_cancelled": "已取消",

    # ---- 翻译记忆库（api.py）----
    "tm_cleared": "翻译记忆库已清空",
    "tm_export_path_missing": "未指定导出路径",
    "tm_import_path_missing": "未指定导入文件",
    "tm_imported": "已导入 {n} 条新记录",

    # ---- 完成态 [WARN] 行后缀（api.py；前缀 [WARN] 保留英文）----
    "warn_exit3": "翻译完成，但存在严重质量风险（exit 3），请检查风险清单。",
    "warn_majority": "翻译完成，但检测到整段未翻译风险，请检查风险清单。",
    "warn_risks": "翻译完成，但检测到 {n} 条风险，请检查风险清单。",

    # ---- 事件流格式化（event_stream.py；字节级文案与原实现一致，测试钉住）----
    "stage_a": "阶段A 净语+翻译",
    "stage_b": "阶段B 审校+抛光",
    "ev_tag": "[事件]",
    "ev_task_started": "任务开始",
    "ev_task_finished": "任务结束",
    "ev_phase_started": "开始",
    "ev_phase_started_generic": "阶段开始",
    "ev_phase_finished": "完成",
    "ev_phase_finished_generic": "阶段完成",
    "ev_in_progress": "进行中",
    "ev_batch": "批次 {done}/{total}",
    "ev_lines": "{done}/{total} 行",
    "ev_warning": "⚠ 警告：{e}",
    "ev_degraded": "⚠ 降级：{e}",
    "ev_error": "✗ 错误：{e}",
    "processing": "处理中",
    "progress_text": "已翻译约 {done}/{total} 行（{label}）",
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
