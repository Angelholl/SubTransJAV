/**
 * SubTransJAV GUI 前端控制器
 *
 * Features:
 * - SRT 文件列表管理（按钮选择 / 文件夹扫描 / 拖拽）
 * - 两阶段流水线配置与启动（refine 子进程）
 * - 实时日志流 + 进度轮询
 * - 主题切换
 */

// ============================================================
// User-Facing Strings（用户可见文案表）
// 与后端 subtransjav/webview_gui/strings.py 的 MSG 字典对应；
// JS 侧无法 import Python，故集中镜像于此（键名尽量保持一致）。
// ============================================================
const MSG = {
    // 状态栏 / 流程
    idle: '空闲',
    starting: '正在启动...',
    running: '运行中',
    completed: '已完成',
    cancelled: '已取消',
    error: '错误',
    ready: '就绪。',

    // 文件列表
    usingSourceOutput: '未获取默认输出目录，已切换为「保存到字幕同目录」',
    addedViaDrop: n => `✓ 已通过拖放添加 ${n} 个 .srt 文件`,
    skippedDuplicates: n => `ℹ 跳过 ${n} 个重复文件`,
    skippedNonSrt: n => `ℹ 跳过 ${n} 个非 .srt 文件`,
    addedFiles: n => `已添加 ${n} 个 .srt 文件`,
    addedFilesFromFolder: n => `已从文件夹添加 ${n} 个 .srt 文件`,
    fileSelectError: '文件选择出错',
    folderSelectError: '文件夹选择出错',
    removedItems: n => `已移除 ${n} 项`,
    clearedItems: n => `已清空 ${n} 项`,

    // 输出目录
    outputDirSet: p => `输出目录：${p}`,
    browseOutputError: '浏览输出目录出错',
    noOutputDirTitle: '未设置输出目录',
    noOutputDirHint: '请先指定输出目录。',
    folderOpened: p => `已打开文件夹：${p}`,
    openFolderFailed: '打开文件夹失败',
    openFolderError: '打开文件夹出错',

    // 翻译流程
    noFilesTitle: '未添加文件',
    translationStarted: pid => `翻译已启动（pid ${pid}）`,
    startFailed: '启动翻译失败',
    translationErrorLog: m => `翻译出错：${m}`,
    translationErrorTitle: '翻译出错',
    cancelledLog: '翻译已取消',
    completedLog: '翻译完成',
    translationFailedTitle: '翻译失败',
    unknownError: '未知错误',

    // 杂项
    themeSwitched: k => `主题：${k}`,
    bridgeConnected: 'PyWebView 桥接已连接',

    // ============================================================
    // i18n 键表（W2 收编）：index.html data-i18n/data-i18n-title/
    // data-i18n-placeholder 引用的键必须全部出现在本表（tests 钉住）；
    // JS 动态文案也统一收编于此，键名 snake_case 或 camelCase 语义化。
    // ============================================================

    // ---- 顶栏 / 主题 ----
    doc_title: '净语翻译 · SubTransJAV Translate',
    app_header_title: '净语翻译 · SRT',
    feature_status_title: '功能状态',
    grammar_hint_text: '语法提示',
    theme_label: 'Theme',
    theme_default: 'Default Theme',
    theme_google: 'Google Theme',
    theme_carbon: 'IBM Carbon Theme',
    theme_primer: 'GitHub Primer Theme',

    // ---- Source 区 / 文件按钮 ----
    source_header: 'Source（.srt 字幕）',
    no_files_selected: 'No files selected',
    empty_hint: '点击「添加文件 / 添加文件夹」或直接拖入 .srt 文件',
    add_files: '添加文件',
    add_folder: '添加文件夹',
    remove_selected: '移除选中',
    clear_btn: '清空',

    // ---- 输出目录 ----
    output_header: '输出目录',
    save_to_source_dir: '保存到字幕同目录',
    output_label: '输出:',
    output_placeholder: '输出目录...',
    browse_btn: '浏览',
    open_btn: '打开',

    // ---- 净语翻译面板 ----
    refine_panel_title: '净语翻译 · 两阶段流水线（净语+翻译 → 审校+抛光）',
    stage_a_label: '阶段A 净语+翻译（日译中）',
    stage_b_label: '阶段B 审校+抛光（中文）',
    provider_zen: 'Zen 免费',
    provider_lmstudio: '本地 LM Studio',
    provider_ollama: '本地 Ollama',
    provider_siliconflow: '硅基流动',
    provider_custom: '自定义兼容接口',
    model_default_1: 'custom-model-1（默认）',
    model_default_2: 'custom-model-2（默认）',
    model_refresh_hint: '（点 ⟳ 刷新模型列表）',
    refresh_model_title: '在线拉取模型列表',
    test_stage_title: '测试该阶段连通性',
    test_stage_btn: '测试',
    key_placeholder: '留空用已保存密钥（LM Studio 免填）',
    save_key_title: '保存该阶段服务商的 API Key（DPAPI 加密存储）',
    profile_label: '兜底档位',
    profile_title: 'local=兜底规则全开(cleaner_rules+误译拦截，适合本地模型)；cloud=lenient(仅零维护通用校验，适合强模型)',
    profile_local: '本地·严格',
    profile_cloud: '云端·宽松',
    ctx_label: '上下文窗口',
    ctx_title: '本地模型上下文窗口：启动后管线自动按此值对齐引擎（覆盖 LM Studio 手工设置），并据此收紧批大小。16GB 显存建议 16384~22272（缺省 22272 为作者 16GB 单卡实测档案值，请按自身显存调整）',
    cleaner_dir_label: '净语配置目录',
    cleaner_dir_placeholder: '留空=使用内置默认',
    browse_dots: '浏览...',
    templates_dir_label: '角色卡目录',
    templates_dir_placeholder: '（未设置，使用默认）',

    // ---- 角色卡编辑 ----
    tpl_editor_summary: '角色卡模板编辑',
    tpl_stage_a: '阶段A · 角色-净语翻译.txt',
    tpl_stage_b: '阶段B · 角色-审校抛光.txt',
    tpl_reload: '重新加载',
    tpl_save: '💾 保存角色卡',
    tpl_placeholder: '选择阶段后自动加载角色卡内容，可直接编辑后保存',

    // ---- 全局词库 ----
    gl_summary: '全局词库编辑',
    gl_tab_label: '翻译术语',
    th_source: '原文词条',
    th_target: '期望译文',
    gl_add: '＋添加',
    gl_del: '删除选中',
    gl_import: '导入CSV/TXT',
    gl_export: '导出CSV',
    gl_save: '💾 保存词库',
    gl_scope_note: '生效范围用下方"词库→阶段A/阶段B"勾选控制；绑定文件：',
    gl_path_empty: '（未加载）',

    // ---- 批量 / 开关 ----
    batch_local_label: '批量·本地',
    batch_cloud_label: '批量·云端',
    concurrency_label: '并行',
    concurrency_title: '批间并发数（1-5），默认 2',
    gl1_label: '词库→阶段A',
    gl2_label: '词库→阶段B',
    resume_label: '断点恢复（复用已完成阶段）',
    resume_title: '中断后重跑时，检测到 *_manifest.json 即复用已完成阶段，仅续跑剩余阶段',
    verbose_label: '详细日志',
    verbose_title: '向子进程传递 --verbose 输出调试日志',
    source_filter_label: '源侧检测',
    sf_strict: '严格',
    sf_default: '标准',
    sf_off: '关闭',
    sf_title: '闸门0 送翻前源侧幻觉检测档位：严格=叠加启发式删除 | 标准=仅明确幻觉删除 | 关闭=关闭检测',
    synopsis_label: '剧情自摘要',
    synopsis_title: '剧情自摘要（Beta）：默认开启，摘要仅注入翻译提示词，不产生任何输出内容',
    dry_run_label: '试运行',
    dry_run_title: '试运行：仅生成执行计划，不调用模型、不产出字幕',
    fallback_local_label: '云端故障时本地接管',
    fallback_local_title: '云端阶段遭遇限流/宕机/持续解析失败时，自动切换本地模型完成剩余批次',
    fallback_model_label: '接管模型',
    refresh_local_title: '刷新本地模型列表',

    // ---- 接口地址 / 启动 ----
    endpoints_summary: '接口地址（对应阶段A/B，切换服务商自动填充）',
    s1_endpoint_label: '阶段A 地址',
    s1_endpoint_placeholder: '阶段A 服务商的接口地址',
    s3_endpoint_label: '阶段B 地址',
    s3_endpoint_placeholder: '阶段B 服务商的接口地址',
    save_endpoints_btn: '💾 保存接口配置',
    start_btn: '▶ 开始净语翻译',
    stop_btn: '⏹ 停止',
    artifact_note: '产物命名含 .subtransjav 中间件与 *_final_cn.srt 终稿；已存在产物默认跳过',
    status_idle: 'Idle',

    // ---- 质量报告导读 ----
    guide_summary: '质量报告导读',
    guide_load_btn: '加载导读',
    guide_conclusions: '结论',
    guide_sections: '章节导读',
    guide_companions: '伴生文件',

    // ---- 控制台 / 页脚 ----
    console_header: 'Console',
    clear_console: 'Clear',
    footer_brand: '净语翻译 · SubTransJAV Translate |',
    about_link: '关于',

    // ---- About 模态 ----
    about_title: '关于',
    about_intro: '简介',
    about_intro_text: 'SubTransJAV 是一款 .srt 字幕日译中桌面工具，支持本地与云端双引擎推理。' +
        '采用 v2 两阶段流水线：阶段A 净语+翻译（一次调用完成文本清洗与日译中）→ ' +
        '阶段B 审校+抛光（对照日文原文审核、补译、润色）。',
    feat_tm: 'TM 翻译记忆库（三层准入门槛，防止低质翻译入库）',
    feat_report: '双引擎分歧质量报告与伪影/幻觉过滤（删除台账见报告【处置】章节）',
    feat_glossary_learn: '词库自动学习（带准入门槛，防止污染词库）',
    feat_fallback: '云端故障本地接管（云端限流/宕机时自动切换本地模型）',
    feat_context_review: '双语字幕上下文预审（context_review 分歧复核工具）',
    shortcuts_title: '快捷键',
    sc_ctrl_o: 'Ctrl+O - 添加文件',
    sc_ctrl_r: 'Ctrl+R - 开始翻译',
    sc_escape: 'Escape - 取消/关闭对话框',
    sc_f1: 'F1 - 显示本对话框',
    project_home_link: 'SubTransJAV 项目主页',
    close_btn: '关闭',

    // ---- 动态文案收编（原表外内联中文）----
    no_files_hint: '请先在上方 Source 区添加 .srt 字幕文件。',
    resumable_found: n => `🔄 检测到可恢复 ${n} 个（将复用已完成阶段）`,
    still_running: secs => `（仍在运行，最近活动 ${secs}s 前）`,
    risk_suffix: n => `｜风险 ${n}`,
    risk_word: '风险',
    untranslated_title: '⚠️ 整段未翻译',
    untranslated_hint: '大量条目保留了日文原文，请检查风险清单',
    level_critical: '严重风险',
    level_warning: '风险',
    completed_with_risks: (level, n) => `翻译完成，但检测到${level}（${n} 条），请检查风险清单`,
    completed_title: '翻译完成',
    completed_detail: '全部文件处理完毕，产物见输出目录。',
    confirm_reload: '翻译正在进行中。仍要刷新吗？这将终止子进程。',
    ep_deepseek_placeholder: 'DeepSeek 原生通道（无需地址）',
    ep_placeholder: '接口地址（以 /v1 结尾）',
    select_provider_first: '⚠️ 请先选择该阶段的服务商',
    loading_models: '加载中...',
    fetching_models: '拉取模型列表中...',
    default_model_missing: (id, cur) => `⚠️ 默认模型 ${id} 不在您的模型列表中，已选择 ${cur}，请按需更换`,
    models_loaded: n => `✅ 模型 ${n} 个`,
    fetch_failed: '获取失败',
    testing: '测试中...',
    failed: '失败',
    alias_label: '别名: ',
    gl_saved: n => `💾 已保存 ${n} 条`,
    tpl_saved: p => `💾 已保存 ${p}`,
    gl_prompt_src: '原文词条（字幕中出现的词）：',
    gl_prompt_dst: s => `「${s}」的期望译文：`,
    gl_imported: n => `📥 导入完成：新增 ${n} 条（已自动保存）`,
    gl_exported: (n, p) => `📤 已导出 ${n} 条 -> ${p}`,
    dir_not_set: '⚠ 目录未设置，请先通过「浏览」选择目录',
    dir_opened: p => `📂 已打开目录: ${p}`,
    dir_open_failed: m => `✗ 打开目录失败: ${m}`,
    dir_open_error: e => `✗ 打开目录异常: ${e}`,
    provider_required: '⚠️ 请先选择服务商',
    lmstudio_no_key: 'LM Studio 无需密钥',
    ollama_no_key: 'Ollama 无需密钥',
    key_saved: p => `💾 密钥已加密保存（${p}）`,
    key_cleared: '🗑 已清除该服务商密钥',
    key_saved_placeholder: '已保存密钥 ✓（留空即用）',
    grammar_hint_on_title: '语法提示已启用 - 阶段A自动分析日语语法结构',
    grammar_hint_off_title: '语法提示未启用 - 安装 sudachipy 可启用',
    endpoints_saved: '💾 接口配置已保存，下次启动自动加载',
    no_conclusions: '（无结论）',
    no_sections: '（无章节导读）',
    no_companions: '（无伴生文件信息）',
    generated_at_label: '生成时间：',
    unknown: '未知',
    api_not_ready: '接口未就绪，请稍后再试',
    guide_need_inputs: '请先选择输入文件并指定输出目录',
    guide_loading: '加载中…',
    guide_loaded: p => `已加载：${p}`,
    guide_load_failed: m => `加载失败：${m}`,
    gui_initialized: '净语翻译 GUI 已初始化',
    gui_usage_hint: '在上方 Source 区添加 .srt 字幕后点击「▶ 开始净语翻译」',
};

// i18n 注入：DOMContentLoaded 时把 MSG 写回带 data-i18n* 标记的元素
function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const v = MSG[el.dataset.i18n];
        if (typeof v === 'string') el.textContent = v;
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        const v = MSG[el.dataset.i18nTitle];
        if (typeof v === 'string') el.setAttribute('title', v);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const v = MSG[el.dataset.i18nPlaceholder];
        if (typeof v === 'string') el.setAttribute('placeholder', v);
    });
}

// ============================================================
// State Management
// ============================================================
const AppState = {
    // File list
    selectedFiles: [],
    selectedIndices: new Set(),

    // UI state
    isRunning: false,

    // Default output directory
    outputDir: '',
    _fallbackOutputDir: '',

    async init() {
        await this.loadDefaultOutputDir();
    },

    async loadDefaultOutputDir() {
        try {
            const defaultDir = await pywebview.api.get_default_output_dir();
            this._fallbackOutputDir = defaultDir;

            const sourceCheckbox = document.getElementById('outputToSource');
            if (sourceCheckbox && sourceCheckbox.checked) {
                this.outputDir = 'source';
                document.getElementById('outputDir').value = 'source';
                document.getElementById('outputDir').disabled = true;
                document.getElementById('browseOutputBtn').disabled = true;
            } else {
                this.outputDir = defaultDir;
                document.getElementById('outputDir').value = defaultDir;
            }
        } catch (error) {
            console.error('Failed to load default output directory:', error);
            this._fallbackOutputDir = '';
            this.outputDir = 'source';
            document.getElementById('outputDir').value = 'source';
            ConsoleManager.log(MSG.usingSourceOutput, 'warning');
        }
    }
};

// ============================================================
// UI Helpers - Loading States
// ============================================================
const UIHelpers = {
    showLoadingState(button, isLoading) {
        button.disabled = isLoading ? true : false;
        button.classList.toggle('loading', isLoading);
    },

    showError(title, message) {
        ErrorHandler.show(title, message);
    },

    showSuccess(title, message) {
        ErrorHandler.showSuccess(title, message);
    }
};

// ============================================================
// Error Handler
// ============================================================
const ErrorHandler = {
    show(title, message) {
        ConsoleManager.log(`✗ ${title}: ${message}`, 'error');
        alert(`${title}\n\n${message}`);
    },

    showWarning(title, message) {
        ConsoleManager.log(`⚠ ${title}: ${message}`, 'warning');
    },

    showSuccess(title, message) {
        ConsoleManager.log(`✓ ${title}: ${message}`, 'success');
    }
};

// ============================================================
// File List Management (always .srt mode)
// ============================================================
const FileListManager = {
    init() {
        const fileList = document.getElementById('fileList');

        fileList.addEventListener('click', (e) => {
            const item = e.target.closest('.file-item');
            if (item) {
                this.handleItemClick(item, e);
            }
        });

        fileList.addEventListener('keydown', (e) => {
            this.handleKeyboard(e);
        });

        this.initializeDragDrop();

        document.getElementById('addFilesBtn').addEventListener('click', () => this.addFiles());
        document.getElementById('addFolderBtn').addEventListener('click', () => this.addFolder());
        document.getElementById('removeSelectedBtn').addEventListener('click', () => this.removeSelected());
        document.getElementById('clearBtn').addEventListener('click', () => this.clearAll());
    },

    initializeDragDrop() {
        const fileList = document.getElementById('fileList');

        fileList.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.stopPropagation();
            fileList.classList.add('drag-over');
        });

        fileList.addEventListener('dragenter', (e) => {
            e.preventDefault();
            e.stopPropagation();
            fileList.classList.add('drag-over');
        });

        fileList.addEventListener('dragleave', () => {
            fileList.classList.remove('drag-over');
        });

        fileList.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            fileList.classList.remove('drag-over');
            // Actual path extraction happens in Python (main.py) via pywebviewFullPath,
            // which calls back into FileListManager.addDroppedFiles(paths).
        });
    },

    // Method called by Python DOM event handler with full file paths (.srt only)
    addDroppedFiles(paths) {
        if (!Array.isArray(paths) || paths.length === 0) {
            return;
        }

        let addedCount = 0;
        let duplicates = 0;
        let skipped = 0;

        paths.forEach(path => {
            if (!String(path).toLowerCase().endsWith('.srt')) {
                skipped++;
                return;
            }
            if (!AppState.selectedFiles.includes(path)) {
                AppState.selectedFiles.push(path);
                addedCount++;
            } else {
                duplicates++;
            }
        });

        if (addedCount > 0) {
            this.render();
            ConsoleManager.log(MSG.addedViaDrop(addedCount), 'success');
        }
        if (duplicates > 0) {
            ConsoleManager.log(MSG.skippedDuplicates(duplicates), 'info');
        }
        if (skipped > 0) {
            ConsoleManager.log(MSG.skippedNonSrt(skipped), 'warning');
        }
    },

    render() {
        const fileList = document.getElementById('fileList');
        const emptyState = document.getElementById('emptyState');

        if (AppState.selectedFiles.length === 0) {
            emptyState.style.display = 'flex';
            fileList.querySelectorAll('.file-item').forEach(item => item.remove());
            this.updateButtons();
            return;
        }

        emptyState.style.display = 'none';

        const existingItems = Array.from(fileList.querySelectorAll('.file-item'));
        const existingPaths = existingItems.map(item => item.dataset.path);

        AppState.selectedFiles.forEach((file, index) => {
            if (!existingPaths.includes(file)) {
                const item = this.createFileItem(file, index);
                fileList.appendChild(item);
            }
        });

        existingItems.forEach(item => {
            if (!AppState.selectedFiles.includes(item.dataset.path)) {
                item.remove();
            }
        });

        this.updateButtons();
    },

    createFileItem(path, index) {
        const item = document.createElement('div');
        item.className = 'file-item';
        item.dataset.path = path;
        item.dataset.index = index;
        item.tabIndex = 0;

        const icon = document.createElement('span');
        icon.className = 'file-icon';
        icon.textContent = '📄';

        const pathSpan = document.createElement('span');
        pathSpan.className = 'file-path';
        pathSpan.textContent = path;

        item.appendChild(icon);
        item.appendChild(pathSpan);

        return item;
    },

    handleItemClick(item, event) {
        const index = parseInt(item.dataset.index);

        if (event.ctrlKey || event.metaKey) {
            this.toggleSelection(index);
        } else if (event.shiftKey && AppState.selectedIndices.size > 0) {
            const lastIndex = Math.max(...Array.from(AppState.selectedIndices));
            this.selectRange(lastIndex, index);
        } else {
            this.selectSingle(index);
        }

        this.updateSelectionUI();
        this.updateButtons();
    },

    toggleSelection(index) {
        if (AppState.selectedIndices.has(index)) {
            AppState.selectedIndices.delete(index);
        } else {
            AppState.selectedIndices.add(index);
        }
    },

    selectSingle(index) {
        AppState.selectedIndices.clear();
        AppState.selectedIndices.add(index);
    },

    selectRange(start, end) {
        const [min, max] = [Math.min(start, end), Math.max(start, end)];
        for (let i = min; i <= max; i++) {
            AppState.selectedIndices.add(i);
        }
    },

    updateSelectionUI() {
        document.querySelectorAll('.file-item').forEach(item => {
            const index = parseInt(item.dataset.index);
            item.classList.toggle('selected', AppState.selectedIndices.has(index));
        });
    },

    updateButtons() {
        const hasFiles = AppState.selectedFiles.length > 0;
        const hasSelection = AppState.selectedIndices.size > 0;

        document.getElementById('removeSelectedBtn').disabled = !hasSelection || AppState.isRunning;
        document.getElementById('clearBtn').disabled = !hasFiles || AppState.isRunning;

        TranslatorManager.updateButtons();
    },

    handleKeyboard(e) {
        const items = Array.from(document.querySelectorAll('.file-item'));
        if (items.length === 0) return;

        const currentIndex = Array.from(AppState.selectedIndices).sort((a, b) => b - a)[0] || 0;

        let newIndex = currentIndex;

        if (e.key === 'ArrowDown') {
            newIndex = Math.min(items.length - 1, currentIndex + 1);
            e.preventDefault();
        } else if (e.key === 'ArrowUp') {
            newIndex = Math.max(0, currentIndex - 1);
            e.preventDefault();
        } else if (e.key === 'Delete' || e.key === 'Backspace') {
            this.removeSelected();
            e.preventDefault();
            return;
        } else {
            return;
        }

        if (e.shiftKey) {
            this.selectRange(currentIndex, newIndex);
        } else {
            this.selectSingle(newIndex);
        }

        this.updateSelectionUI();
        this.updateButtons();

        if (items[newIndex]) {
            items[newIndex].scrollIntoView({ block: 'nearest' });
        }
    },

    async addFiles() {
        const btn = document.getElementById('addFilesBtn');
        UIHelpers.showLoadingState(btn, true);

        try {
            const result = await pywebview.api.select_srt_files();

            if (result.success && result.paths && result.paths.length > 0) {
                result.paths.forEach(file => {
                    if (!AppState.selectedFiles.includes(file)) {
                        AppState.selectedFiles.push(file);
                    }
                });

                this.render();
                ConsoleManager.log(MSG.addedFiles(result.paths.length), 'info');
            }
        } catch (error) {
            ErrorHandler.show(MSG.fileSelectError, error.toString());
        } finally {
            UIHelpers.showLoadingState(btn, false);
        }
    },

    async addFolder() {
        const btn = document.getElementById('addFolderBtn');
        UIHelpers.showLoadingState(btn, true);

        try {
            const result = await pywebview.api.select_srt_folder();
            if (result.success && result.paths && result.paths.length > 0) {
                result.paths.forEach(filePath => {
                    if (!AppState.selectedFiles.includes(filePath)) {
                        AppState.selectedFiles.push(filePath);
                    }
                });
                this.render();
                ConsoleManager.log(MSG.addedFilesFromFolder(result.paths.length), 'info');
            } else if (result.message) {
                ConsoleManager.log(result.message, 'warning');
            }
        } catch (error) {
            ErrorHandler.show(MSG.folderSelectError, error.toString());
        } finally {
            UIHelpers.showLoadingState(btn, false);
        }
    },

    removeSelected() {
        if (AppState.selectedIndices.size === 0) return;

        const indicesToRemove = Array.from(AppState.selectedIndices).sort((a, b) => b - a);
        indicesToRemove.forEach(index => {
            AppState.selectedFiles.splice(index, 1);
        });

        AppState.selectedIndices.clear();
        this.render();
        ConsoleManager.log(MSG.removedItems(indicesToRemove.length), 'info');
    },

    clearAll() {
        if (AppState.selectedFiles.length === 0) return;

        const count = AppState.selectedFiles.length;
        AppState.selectedFiles = [];
        AppState.selectedIndices.clear();
        this.render();
        ConsoleManager.log(MSG.clearedItems(count), 'info');
    }
};

// ============================================================
// Console Management
// ============================================================
const ConsoleManager = {
    init() {
        document.getElementById('clearConsoleBtn').addEventListener('click', () => this.clear());
    },

    log(message, type = 'info') {
        const output = document.getElementById('consoleOutput');
        const line = document.createElement('div');
        line.className = `console-line ${type}`;
        line.textContent = message;
        output.appendChild(line);

        requestAnimationFrame(() => {
            if (output) {
                output.scrollTop = output.scrollHeight;
            }
        });
    },

    clear() {
        const output = document.getElementById('consoleOutput');
        output.innerHTML = `<div class="console-line">${MSG.ready}</div>`;
    },

    appendRaw(text) {
        const output = document.getElementById('consoleOutput');

        const lines = text.split('\n');

        lines.forEach((line, index) => {
            if (index === lines.length - 1 && line === '') return;

            const lineEl = document.createElement('div');
            lineEl.className = 'console-line';
            lineEl.textContent = line || ' ';
            output.appendChild(lineEl);
        });

        requestAnimationFrame(() => {
            if (output) {
                output.scrollTop = output.scrollHeight;
            }
        });
    }
};

// ============================================================
// Progress Management (progress bar inside the refine panel)
// ============================================================
const ProgressManager = {
    init() {
        this.progressBar = document.getElementById('progressBar');
        this.progressFill = document.getElementById('progressFill');
        this.statusLabel = document.getElementById('statusLabel');
    },

    setIndeterminate(active) {
        if (this.progressBar) this.progressBar.classList.toggle('indeterminate', active);
    },

    setProgress(percent) {
        if (this.progressFill) this.progressFill.style.width = `${percent}%`;
    },

    setStatus(text) {
        if (this.statusLabel) this.statusLabel.textContent = text;
    },

    reset() {
        this.setIndeterminate(false);
        this.setProgress(0);
        this.setStatus(MSG.idle);
    }
};

// ============================================================
// Directory Controls (Destination section)
// ============================================================
const DirectoryControls = {
    _savedOutputDir: '',

    init() {
        document.getElementById('browseOutputBtn').addEventListener('click', () => this.browseOutput());
        document.getElementById('openOutputBtn').addEventListener('click', () => this.openOutput());

        const sourceCheckbox = document.getElementById('outputToSource');
        sourceCheckbox.addEventListener('change', () => this.toggleSourceOutput(sourceCheckbox.checked));
    },

    toggleSourceOutput(enabled) {
        const outputDirInput = document.getElementById('outputDir');
        const browseBtn = document.getElementById('browseOutputBtn');

        if (enabled) {
            this._savedOutputDir = outputDirInput.value;
            outputDirInput.value = 'source';
            AppState.outputDir = 'source';
            outputDirInput.disabled = true;
            browseBtn.disabled = true;
        } else {
            const restoreDir = this._savedOutputDir && this._savedOutputDir !== 'source'
                ? this._savedOutputDir
                : (AppState._fallbackOutputDir || AppState.outputDir);
            outputDirInput.value = restoreDir === 'source' ? '' : restoreDir;
            AppState.outputDir = restoreDir === 'source' ? '' : restoreDir;
            outputDirInput.disabled = false;
            browseBtn.disabled = false;
        }
    },

    async browseOutput() {
        try {
            const result = await pywebview.api.select_output_directory();

            if (result.success && result.path) {
                document.getElementById('outputDir').value = result.path;
                AppState.outputDir = result.path;
                ConsoleManager.log(MSG.outputDirSet(result.path), 'info');
            }
        } catch (error) {
            ErrorHandler.show(MSG.browseOutputError, error.toString());
        }
    },

    async openOutput() {
        let path = document.getElementById('outputDir').value;

        if (!path) {
            ErrorHandler.showWarning(MSG.noOutputDirTitle, MSG.noOutputDirHint);
            return;
        }

        // "source" is a sentinel meaning "save next to source file" -
        // resolve to the parent directory of the first selected file
        if (path === 'source' && AppState.selectedFiles.length > 0) {
            const firstFile = AppState.selectedFiles[0];
            const lastSlash = Math.max(firstFile.lastIndexOf('/'), firstFile.lastIndexOf('\\'));
            if (lastSlash > 0) {
                path = firstFile.substring(0, lastSlash);
            }
        }

        try {
            const result = await pywebview.api.open_output_folder(path, true);

            if (result.success) {
                ConsoleManager.log(MSG.folderOpened(path), 'info');
            } else {
                ErrorHandler.show(MSG.openFolderFailed, result.message);
            }
        } catch (error) {
            ErrorHandler.show(MSG.openFolderError, error.toString());
        }
    }
};

// ============================================================
// Translator Manager (净语翻译两阶段执行)
// ============================================================
const TranslatorManager = {
    state: {
        isRunning: false,
        progress: 0
    },

    init() {
        console.log('TranslatorManager initialized');
    },

    /**
     * Update Start/Cancel button states.
     * Called whenever the shared file list or run state changes.
     */
    updateButtons() {
        const hasFiles = AppState.selectedFiles.length > 0;
        const isRunning = this.state.isRunning;

        const startBtn = document.getElementById('refineStartBtn');
        const cancelBtn = document.getElementById('refineCancelBtn');
        if (startBtn) startBtn.disabled = !hasFiles || isRunning;
        if (cancelBtn) cancelBtn.disabled = !isRunning;
    },

    async startTranslation() {
        if (AppState.isRunning) return;

        if (AppState.selectedFiles.length === 0) {
            ErrorHandler.show(MSG.noFilesTitle, MSG.noFilesHint);
            return;
        }

        try {
            // collectOptions 抛错时按钮状态才能正常复位并提示
            const options = this.collectOptions();

            AppState.isRunning = true;
            this.state.isRunning = true;
            FileListManager.updateButtons();
            this.setProgress(0);
            this.setStatus(MSG.starting);
            ProgressManager.setIndeterminate(true);

            // 断点恢复探测：把可恢复文件打到控制台（纯提示，不阻塞启动）
            if (AppState.selectedFiles.length > 0) {
                try {
                    const states = await pywebview.api.scan_resume_states(
                        AppState.selectedFiles.slice());
                    const resumable = (states || [])
                        .filter(s => s && s.state === 'resumable');
                    if (resumable.length > 0) {
                        ConsoleManager.log(
                            MSG.resumableFound(resumable.length), 'info');
                        resumable.forEach(s =>
                            ConsoleManager.log(`   ↺ ${s.stem}`, 'info'));
                    }
                } catch (e) {
                    console.warn('scan_resume_states failed:', e);
                }
            }

            const result = await pywebview.api.start_translation(options);

            if (result.success) {
                ConsoleManager.log(MSG.translationStarted(result.pid), 'info');
                this.startStatusPolling();
            } else {
                throw new Error(result.error || MSG.startFailed);
            }
        } catch (error) {
            ConsoleManager.log(MSG.translationErrorLog(error.message), 'error');
            ErrorHandler.show(MSG.translationErrorTitle, error.message);
            this._finish(MSG.error);
        }
    },

    async cancelTranslation() {
        try {
            await pywebview.api.cancel_translation();
            ConsoleManager.log(MSG.cancelledLog, 'warning');
            // 状态轮询会检测到进程退出并收尾；此处立即恢复按钮避免竞态窗口
            this._finish(MSG.cancelled);
        } catch (error) {
            console.error('Error cancelling translation:', error);
        }
    },

    _finish(statusText) {
        this.stopStatusPolling();
        this._reportedRiskCount = 0;
        this.state.isRunning = false;
        AppState.isRunning = false;
        FileListManager.updateButtons();
        ProgressManager.setIndeterminate(false);
        this.setStatus(statusText);
    },

    // ---- Status polling ----

    startStatusPolling() {
        this._reportedRiskCount = 0;
        this.statusInterval = setInterval(async () => {
            try {
                const status = await pywebview.api.get_translation_status();

                // 状态栏：阶段名（current_stage）优先，叠加当前文件（current_file）
                let text = '';
                if (status.current_stage) text = status.current_stage;
                if (status.current_file &&
                    (!text || !status.current_file.includes(text))) {
                    text = text ? `${text} · ${status.current_file}` : status.current_file;
                }
                // 心跳超时且仍在运行：追加最近活动提示
                // （阈值来自后端配置 heartbeat_stale_s 分层解析结果，缺省 45s ≈ 2.25×心跳间隔 20s）
                const staleS = (typeof status.heartbeat_stale_s === 'number')
                    ? status.heartbeat_stale_s : 45;
                if (status.status === 'running' &&
                    status.heartbeat_age != null && status.heartbeat_age > staleS) {
                    const secs = Math.round(status.heartbeat_age);
                    text = `${text || MSG.running}${MSG.stillRunning(secs)}`;
                }
                // 风险计数
                if (status.risk_count > 0) {
                    text = `${text || MSG.running}${MSG.riskSuffix(status.risk_count)}`;
                }
                if (text) this.setStatus(text);

                // 风险明细：仅在数量增长时把新增条目追加到控制台
                const reported = this._reportedRiskCount || 0;
                if (status.risk_count > reported) {
                    const risks = status.risks || [];
                    const fresh = risks.slice(
                        Math.max(0, risks.length - (status.risk_count - reported)));
                    fresh.forEach(r => {
                        const where = r.phase ? `（${r.phase}）` : '';
                        ConsoleManager.log(
                            `⚠️ ${where}${r.message || r.type || MSG.riskWord}`, 'warning');
                    });
                    this._reportedRiskCount = status.risk_count;
                }

                if (status.progress !== undefined && status.progress > 0) {
                    ProgressManager.setIndeterminate(false);
                    this.setProgress(status.progress);
                }

                this.fetchLogs();

                if (status.status === 'completed') {
                    this.setProgress(100);
                    if (status.untranslated_majority) {
                        ErrorHandler.show(MSG.untranslatedTitle,
                            MSG.untranslatedHint);
                    } else if (status.warning_level) {
                        const level = status.warning_level === 'critical'
                            ? MSG.levelCritical : MSG.levelWarning;
                        ConsoleManager.log(
                            MSG.completedWithRisks(level, status.risk_count || 0),
                            'warning');
                    } else {
                        ConsoleManager.log(MSG.completedLog, 'success');
                        ErrorHandler.showSuccess(MSG.completedTitle, MSG.completedDetail);
                    }
                    if (typeof this.guideAutoDetect === 'function') this.guideAutoDetect();
                    this._finish(MSG.completed);
                } else if (status.status === 'error') {
                    ConsoleManager.log(MSG.translationErrorLog(status.error), 'error');
                    ErrorHandler.show(MSG.translationFailedTitle,
                        status.error || MSG.unknownError);
                    this._finish(MSG.error);
                } else if (status.status === 'cancelled') {
                    this._finish(MSG.cancelled);
                }
            } catch (error) {
                console.error('Status poll error:', error);
            }
        }, 1000);
    },

    stopStatusPolling() {
        if (this.statusInterval) {
            clearInterval(this.statusInterval);
            this.statusInterval = null;
        }
    },

    async fetchLogs() {
        try {
            const logs = await pywebview.api.get_translation_logs();
            if (logs && logs.length > 0) {
                logs.forEach(line => {
                    const cleanLine = line.replace(/\n$/, '');
                    if (cleanLine) {
                        ConsoleManager.appendRaw(cleanLine);
                    }
                });
            }
        } catch (error) {
            console.error('Error fetching logs:', error);
        }
    },

    setProgress(percent) {
        this.state.progress = percent;
        ProgressManager.setProgress(percent);
    },

    setStatus(text) {
        ProgressManager.setStatus(text);
    }
};

// ============================================================
// Theme Manager (runtime stylesheet switching with persistence)
// ============================================================
const ThemeManager = {
    storageKey: 'subtransjav_theme',
    themes: {
        'default': 'style.css',
        'google': 'style.google.css',
        'carbon': 'style.carbon.css',
        'primer': 'style.primer.css'
    },

    init() {
        this.linkEl = document.getElementById('themeStylesheet');
        if (!this.linkEl) {
            console.warn('ThemeManager: #themeStylesheet not found; falling back to style.css');
            return;
        }

        const saved = this.getSavedTheme();
        this.applyTheme(saved);

        const btn = document.getElementById('themeBtn');
        const menu = document.getElementById('themeMenu');
        if (btn && menu) {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                menu.classList.toggle('active');
                if (menu.classList.contains('active')) {
                    const first = menu.querySelector('.theme-option');
                    if (first) first.focus();
                }
            });

            document.addEventListener('click', (e) => {
                if (menu.classList.contains('active')) {
                    menu.classList.remove('active');
                }
            });

            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    menu.classList.remove('active');
                }
            });

            menu.querySelectorAll('.theme-option').forEach(opt => {
                opt.addEventListener('click', (e) => {
                    const theme = opt.dataset.theme;
                    this.applyTheme(theme);
                    menu.classList.remove('active');
                });
            });
        }
    },

    getSavedTheme() {
        try {
            const key = localStorage.getItem(this.storageKey) || 'default';
            return this.themes[key] ? key : 'default';
        } catch (e) {
            return 'default';
        }
    },

    saveTheme(key) {
        try {
            localStorage.setItem(this.storageKey, key);
        } catch (e) {
            // ignore
        }
    },

    applyTheme(key) {
        if (!this.linkEl) return;
        const href = this.themes[key] || this.themes['default'];
        this.linkEl.setAttribute('href', href);
        this.saveTheme(key in this.themes ? key : 'default');
        ConsoleManager.log(MSG.themeSwitched(key), 'info');
    }
};

// ============================================================
// Keyboard Shortcuts
// ============================================================
const KeyboardShortcuts = {
    init() {
        document.addEventListener('keydown', (e) => {
            // Ctrl+O: Add files
            if (e.ctrlKey && e.key === 'o') {
                e.preventDefault();
                FileListManager.addFiles();
            }

            // Ctrl+R: Start translation
            if (e.ctrlKey && e.key === 'r') {
                e.preventDefault();
                if (!AppState.isRunning && AppState.selectedFiles.length > 0) {
                    TranslatorManager.startTranslation();
                }
            }

            // Escape: Close modal
            if (e.key === 'Escape') {
                const modal = document.getElementById('aboutModal');
                if (modal && modal.classList.contains('active')) {
                    closeAbout();
                }
            }

            // F1: Show About dialog
            if (e.key === 'F1') {
                e.preventDefault();
                showAbout();
            }

            // F5: Refresh with warning
            if (e.key === 'F5') {
                if (AppState.isRunning) {
                    e.preventDefault();
                    if (confirm(MSG.confirmReload)) {
                        location.reload();
                    }
                }
            }
        });
    }
};

// ============================================================
// About Dialog
// ============================================================
async function showAbout() {
    const modal = document.getElementById('aboutModal');
    const versionEl = document.getElementById('aboutVersion');

    try {
        const result = await pywebview.api.get_version();
        if (result.success) {
            versionEl.textContent = `Version ${result.version}`;
        }
    } catch (e) {
        console.warn('Could not fetch version:', e);
    }

    modal.classList.add('active');
}

function closeAbout() {
    const modal = document.getElementById('aboutModal');
    modal.classList.remove('active');
}

// ===== Refine UI：模型刷新/测试 + 词库表格编辑器 + 角色卡编辑器 =====
(function () {
  if (window.__refineUI) return;
  window.__refineUI = true;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/"/g, '&quot;')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;');
  }

  // 服务商默认接口地址（切换服务商时自动填充到该阶段的地址栏）
  const REFINE_PROVIDER_URLS = {
    lmstudio: 'http://localhost:1234/v1',
    ollama: 'http://localhost:11434/v1',
    zen: 'https://opencode.ai/zen/v1',
    deepseek: 'https://api.deepseek.com/v1',
    siliconflow: 'https://api.siliconflow.cn/v1',
    custom: ''
  };

  // 每阶段独立的接口地址输入框
  function stageEndpoint(n) {
    const v = ($('refineS' + n + 'Endpoint') || {}).value;
    return v && v.trim() ? v.trim() : '';
  }

  // 指定服务商当前使用的接口地址：优先取使用该服务商的阶段地址栏，否则用默认
  function stageEndpointFor(prov) {
    for (const n of [1, 3]) {
      if ((($('refineS' + n + 'Provider') || {}).value) === prov) {
        const ep = stageEndpoint(n);
        if (ep) return ep;
      }
    }
    return REFINE_PROVIDER_URLS[prov] || '';
  }

  // 服务商切换 → 该阶段地址栏自动填充对应默认地址
  function applyProviderEndpoint(n) {
    const prov = ($('refineS' + n + 'Provider') || {}).value;
    const ep = $('refineS' + n + 'Endpoint');
    if (!ep) return;
    ep.value = REFINE_PROVIDER_URLS[prov] || '';
    ep.disabled = (prov === 'deepseek');
    if (prov === 'deepseek') ep.value = '';
    ep.placeholder = prov === 'deepseek'
      ? MSG.ep_deepseek_placeholder : MSG.ep_placeholder;
    // ollama 地址同样可编辑（与 lmstudio 一致，placeholder 提示默认端口）
  }

  // 每阶段独立 API Key（LM Studio 无需；留空则后端回退到已保存密钥）
  function stageKeyOrNull(n) {
    const v = ($('refineS' + n + 'Key') || {}).value;
    return v && v.trim() ? v.trim() : null;
  }

  // 取指定服务商在阶段行里填写的第一个非空密钥（随启动参数传给子进程）
  function stageKeyFor(prov) {
    for (const n of [1, 3]) {
      if ((($('refineS' + n + 'Provider') || {}).value) === prov) {
        const k = stageKeyOrNull(n);
        if (k) return k;
      }
    }
    return '';
  }

  // 行内状态提示（每阶段自己的 span）
  function stageStatus(n, text, kind) {
    const st = $('refineTestS' + n + 'Status');
    if (!st) return;
    st.style.color = kind === 'err' ? 'crimson' : (kind === 'ok' ? 'green' : '#888');
    st.textContent = text;
  }

  // ---- 并行度读取（1-5，非法/缺省回退2）----
  function readRefineConcurrency() {
    let n = parseInt(($('refineConcurrency') || {}).value, 10);
    if (!Number.isFinite(n)) n = 2;
    return Math.max(1, Math.min(5, n));
  }

  // ---- 上下文窗口读取（≥4096，非法/缺省回退22272；管线会按此值对齐引擎）----
  function readRefineCtx() {
    let n = parseInt(($('refineV2Ctx') || {}).value, 10);
    if (!Number.isFinite(n) || n < 4096) n = 22272;
    return n;
  }

  // ---- 启动选项收集器（v2 两阶段：阶段A→s1 槽位，阶段B→s3 槽位）----
  function buildRefineOptions() {
    return {
      inputs: AppState.selectedFiles,
      output_dir: $('outputDir') ? $('outputDir').value : '',
      profile: ($('refineProfile') || {}).value || 'local',
      v2_concurrency: readRefineConcurrency(),
      v2_ctx: readRefineCtx(),
      s1_provider: ($('refineS1Provider') || {}).value,
      s1_model: ($('refineS1Model') || {}).value || '',
      s3_provider: ($('refineS3Provider') || {}).value,
      s3_model: ($('refineS3Model') || {}).value || '',
      templates_dir: ($('refineTemplatesDir') || {}).value || '',
      glossary: ($('refineGlossary') || {}).value || '',
      custom_key: stageKeyFor('custom'),
      zen_key: stageKeyFor('zen'),
      siliconflow_key: stageKeyFor('siliconflow'),
      deepseek_key: stageKeyFor('deepseek'),
      apply_glossary_stage1: !($('refineGl1') && !$('refineGl1').checked),
      apply_glossary_stage2: !($('refineGl2') && !$('refineGl2').checked),
      batch_local: parseInt(($('refineBatchLocal') || {}).value) || 30,
      batch_cloud: parseInt(($('refineBatchCloud') || {}).value) || 30,
      lmstudio_endpoint: stageEndpointFor('lmstudio'),
      ollama_endpoint: stageEndpointFor('ollama'),
      zen_endpoint: stageEndpointFor('zen'),
      siliconflow_endpoint: stageEndpointFor('siliconflow'),
      custom_endpoint: stageEndpointFor('custom'),
      fallback_local: !!($('refineFallbackLocal') || {}).checked,
      fallback_model: (($('refineFallbackModel') || {}).value || '').trim(),
      cleaner_config_dir: (($('refineCleanerConfig') || {}).value || '').trim(),
      resume: !!($('resumeToggle') && $('resumeToggle').checked),
      verbose: !!($('debugLogging') || {}).checked,
      source_filter: (($('refineSourceFilter') || {}).value || 'default'),
      auto_synopsis: !($('refineAutoSynopsis') && !$('refineAutoSynopsis').checked),
      dry_run: !!($('refineDryRun') || {}).checked
    };
  }

  TranslatorManager.collectOptions = buildRefineOptions;
  TranslatorManager.guideAutoDetect = guideAutoDetect;

  // ---- 本地模型默认值（槽位1=阶段A，槽位3=阶段B；与 index.html 中 selected 项一致）----
  const LOCAL_MODEL_DEFAULTS = {
    1: { id: 'custom-model-1', keys: ['custom-model-1'] },
    3: { id: 'custom-model-2', keys: ['custom-model-2'] }
  };

  // 在已填充的模型下拉中优先选中该槽位的默认模型，三级匹配：
  // ①大小写不敏感精确匹配；②默认名去掉 "-gguf" 后缀后包含匹配；
  // ③关键词片段匹配（matchKeys，任一 option 小写 value 包含任一 key 即命中，
  //   注意嵌入模型如 text-embedding-* 不会被这些 key 命中）。
  // 都找不到返回 false（由调用方回退 selectedIndex = 0）
  function selectDefaultModel(sel, n) {
    const def = LOCAL_MODEL_DEFAULTS[n];
    if (!def || !def.id) return false;
    const opts = Array.from(sel.options || []);
    const idLower = def.id.toLowerCase();
    // ① 精确匹配
    let opt = opts.find(o => (o.value || '').toLowerCase() === idLower);
    // ② 去掉 -gguf 后缀后包含匹配
    if (!opt) {
      const idNoGguf = idLower.replace(/-gguf$/, '');
      opt = opts.find(o => (o.value || '').toLowerCase().indexOf(idNoGguf) !== -1);
    }
    // ③ 关键词片段匹配
    if (!opt && Array.isArray(def.keys) && def.keys.length) {
      opt = opts.find(o => {
        const v = (o.value || '').toLowerCase();
        return !v.startsWith('text-embedding') && def.keys.some(k => v.indexOf(k) !== -1);
      });
    }
    if (opt) { sel.value = opt.value; return true; }
    return false;
  }

  // ---- 模型列表刷新 ----
  async function refreshModels(n) {
    const prov = ($('refineS' + n + 'Provider') || {}).value;
    const btn = $('refineRefreshS' + n);
    const sel = $('refineS' + n + 'Model');
    if (!btn || !sel) return;
    if (!prov) { stageStatus(n, MSG.select_provider_first, 'err'); return; }
    btn.disabled = true;
    const old = btn.textContent; btn.textContent = '…';
    // 保留 HTML 初始默认选中项，失败/异常时恢复，避免下拉被清空
    const originalHTML = sel.innerHTML;
    sel.innerHTML = '<option value="">' + MSG.loading_models + '</option>';
    stageStatus(n, MSG.fetching_models, '');
    try {
      const r = await pywebview.api.refine_list_models(
        prov, stageEndpoint(n), stageKeyOrNull(n));
      if (r.success && r.models.length) {
        sel.innerHTML = r.models.map(m =>
          '<option value="' + esc(m) + '">' + esc(m) + '</option>').join('');
        if (prov === 'zen') {
          const free = r.models.filter(x => x.endsWith('-free'));
          if (free.length) sel.value = free[0]; else sel.selectedIndex = 0;
        } else if (!selectDefaultModel(sel, n)) {
          // 默认模型未命中：优先选第一个非嵌入模型，避免误选 text-embedding-*
          const opts = Array.from(sel.options || []);
          const nonEmbed = opts.find(o =>
            !(o.value || '').toLowerCase().startsWith('text-embedding'));
          sel.value = nonEmbed ? nonEmbed.value : sel.options[0].value;
          const def = LOCAL_MODEL_DEFAULTS[n];
          if (def && def.id) {
            stageStatus(n, MSG.default_model_missing(def.id, sel.value), '');
          } else {
            stageStatus(n, MSG.models_loaded(r.models.length), 'ok');
          }
        } else {
          stageStatus(n, MSG.models_loaded(r.models.length), 'ok');
        }
      } else {
        sel.innerHTML = originalHTML;
        stageStatus(n, '❌ ' + (r.error || MSG.fetch_failed) + (r.tip ? ' · ' + r.tip : ''), 'err');
      }
    } catch (e) {
      sel.innerHTML = originalHTML;
      stageStatus(n, '❌ ' + e, 'err');
    } finally {
      btn.disabled = false; btn.textContent = old;
    }
  }

  // ---- 接管模型列表刷新 ----
  async function refreshFallbackModels() {
    const sel = $('refineFallbackModel');
    const btn = $('refreshFallbackModels');
    if (!sel || !btn) return;

    // 获取 LM Studio endpoint（从阶段A的 lmstudio 配置中读取）
      const endpoint = stageEndpointFor('lmstudio') || 'http://localhost:1234/v1';

    btn.disabled = true;
    const old = btn.textContent; btn.textContent = '…';
    try {
      const r = await pywebview.api.list_local_models(endpoint);
      if (r.success && r.models.length) {
        const cur = sel.value;
        sel.innerHTML = r.models.map(m =>
          '<option value="' + esc(m) + '">' + esc(m) + (r.loaded.includes(m) ? ' ✓' : '') + '</option>').join('');
        // 尝试保持之前选中的值
        if (cur && r.models.includes(cur)) sel.value = cur;
      } else {
        // 保留默认选项
      }
    } catch (e) {
      // 静默失败，保留现有选项
    } finally {
      btn.disabled = false; btn.textContent = old;
    }
  }

  // 页面加载时自动刷新接管模型列表
  document.addEventListener('DOMContentLoaded', function() {
    setTimeout(refreshFallbackModels, 1000);
  });

  // ---- 单阶段测试 ----
  async function testStage(n) {
    const prov = ($('refineS' + n + 'Provider') || {}).value;
    const model = ($('refineS' + n + 'Model') || {}).value;
    const st = $('refineTestS' + n + 'Status');
    const btn = $('refineTestS' + n);
    if (!btn) return;
    btn.disabled = true;
    if (st) { st.style.color = '#888'; st.textContent = MSG.testing; }
    try {
      const r = await pywebview.api.refine_test_stage(
        prov, model, stageEndpoint(n), stageKeyOrNull(n));
      if (st) {
        st.style.color = r.success ? 'green' : 'crimson';
        st.textContent = (r.success ? '✅ ' : '❌ ') +
          (r.success ? r.message : (r.tip || r.error || MSG.failed));
      }
      if (!r.success && r.tip) console.warn('[refine]', r.tip);
    } catch (e) {
      if (st) { st.style.color = 'crimson'; st.textContent = '❌ ' + e; }
    } finally {
      btn.disabled = false;
    }
  }

  function glStatus(t) {
    const el = $('refineGlStatus');
    if (el) { el.style.color = '#888'; el.textContent = t; }
  }

  // ---- 词库表格 ----
  function glRows() {
    const rows = [];
    document.querySelectorAll('#refineGlossTable tbody tr').forEach(tr => {
      const i = tr.querySelector('input.gl-src');
      const o = tr.querySelector('input.gl-dst');
      if (i && o && i.value.trim() && o.value.trim())
        rows.push([i.value.trim(), o.value.trim()]);
    });
    return rows;
  }

  function glRender(rows) {
    const tb = document.querySelector('#refineGlossTable tbody');
    tb.innerHTML = rows.map(r =>
      '<tr style="border-bottom:1px solid #ddd;">' +
      '<td style="padding:2px 4px;"><input class="form-input compact gl-src" ' +
      'style="width:100%;" value="' + esc(r[0]) + '"></td>' +
      '<td style="padding:2px 4px;"><input class="form-input compact gl-dst" ' +
      'style="width:100%;" value="' + esc(r[1]) + '">' +
      // 别名第三列只读展示（不由前端编辑；无别名不渲染，保存时后端保留）
      (r[2] ? '<div style="font-size:11px; color:#888; margin-top:1px;">' + MSG.alias_label +
        esc(r[2]) + '</div>' : '') + '</td>' +
      '<td style="text-align:center;"><input type="checkbox" class="gl-sel"></td>' +
      '</tr>').join('');
    const cnt = $('refineGlCount');
    if (cnt) cnt.textContent = rows.length;
  }

  async function glLoad() {
    try {
      const d = await pywebview.api.refine_default_paths();
      if (d.success) {
        if ($('refineGlossary'))
          $('refineGlossary').value = d.glossary_path;
        if ($('refineTemplatesDir')) {
          $('refineTemplatesDir').value = d.templates_dir;
          const show = $('refineTemplatesDirShow');
          if (show) show.value = d.templates_dir;
        }
        const p = $('refineGlossary') ? $('refineGlossary').value : '';
        const gp = $('refineGlPath');
        if (gp) gp.textContent = p;
      }
      const g = $('refineGlossary') ? $('refineGlossary').value : '';
      if (!g) return;
      const r = await pywebview.api.refine_get_glossary(g);
      if (r.success) glRender(r.rows);
    } catch (e) { console.warn('[refine] 词库加载失败', e); }
  }

  async function glSave() {
    const r = await pywebview.api.refine_save_glossary(
      glRows(), $('refineGlossary') ? $('refineGlossary').value : null);
    glStatus(r.success ? MSG.gl_saved(r.count) : '❌ ' + r.error);
  }

  function glAdd() {
    const src = prompt(MSG.gl_prompt_src);
    if (!src || !src.trim()) return;
    const dst = prompt(MSG.gl_prompt_dst(src.trim()));
    if (!dst || !dst.trim()) return;
    const rows = glRows().filter(r => r[0] !== src.trim());
    rows.push([src.trim(), dst.trim()]);
    glRender(rows);
  }

  async function glDel() {
    document.querySelectorAll('#refineGlossTable tbody tr').forEach(tr => {
      const cb = tr.querySelector('.gl-sel');
      if (cb && cb.checked) tr.remove();
    });
    const cnt = $('refineGlCount');
    if (cnt) cnt.textContent = glRows().length;
    await glSave();
  }

  async function glImport() {
    const pick = await pywebview.api.refine_pick_csv_open();
    if (!pick.success) return;
    const r = await pywebview.api.refine_get_glossary(pick.path);
    if (!r.success) { glStatus('❌ ' + r.error); return; }
    // 合并基准 = 文件中的既有词条（权威来源），而非 DOM 表格
    const target = $('refineGlossary') ? $('refineGlossary').value : null;
    let merged = [];
    try {
      const baseR = await pywebview.api.refine_get_glossary(target || null);
      if (baseR && baseR.success) merged = baseR.rows;
    } catch (e) { console.warn('[refine] 读取既有词库失败，将仅导入所选文件', e); }
    const have = new Set(merged.map(x => x[0]));
    let added = 0;
    r.rows.forEach(row => {
      if (!have.has(row[0])) { merged.push(row); have.add(row[0]); added++; }
    });
    glRender(merged);
    glStatus(MSG.gl_imported(added));
    await pywebview.api.refine_save_glossary(merged, target || null);
  }

  async function glExport() {
    const pick = await pywebview.api.refine_pick_csv_save();
    if (!pick.success) return;
    const r = await pywebview.api.refine_save_glossary(glRows(), pick.path);
    glStatus(r.success ? MSG.gl_exported(r.count, r.path) : '❌ ' + r.error);
  }

  // ---- 角色卡编辑器 ----
  async function tplLoad() {
    const idx = ($('refineTemplateStage') || {}).value;
    const dir = $('refineTemplatesDir') ? $('refineTemplatesDir').value : '';
    const st = $('refineTemplateStatus');
    try {
      const r = await pywebview.api.refine_get_template(idx, dir);
      if (r.success) {
        $('refineTemplateText').value = r.text;
        if (st) {
          st.style.color = '#888';
          st.textContent = (r.note ? '📌 ' + r.note + '  ' : '') + r.path;
        }
      } else if (st) {
        st.style.color = 'crimson'; st.textContent = r.error;
      }
    } catch (e) { if (st) st.textContent = '❌ ' + e; }
  }

  async function tplSave() {
    const idx = ($('refineTemplateStage') || {}).value;
    const st = $('refineTemplateStatus');
    try {
      const r = await pywebview.api.refine_save_template(
        idx, $('refineTemplateText').value,
        $('refineTemplatesDir') ? $('refineTemplatesDir').value : null);
      if (st) {
        st.style.color = r.success ? 'green' : 'crimson';
        st.textContent = r.success ? MSG.tpl_saved(r.path) : '❌ ' + r.error;
      }
    } catch (e) { if (st) st.textContent = '❌ ' + e; }
  }

  async function pickDir() {
    const r = await pywebview.api.refine_pick_folder();
    if (r.success && r.path) {
      $('refineTemplatesDir').value = r.path;
      const show = $('refineTemplatesDirShow');
      if (show) show.value = r.path;
      tplLoad();
    }
  }

  async function pickCleanerDir() {
    const r = await pywebview.api.refine_pick_folder();
    if (r.success && r.path) {
      $('refineCleanerConfig').value = r.path;
      const show = $('refineCleanerConfigShow');
      if (show) show.value = r.path;
    }
  }

  async function openDir(path) {
    if (!path) {
      ConsoleManager.log(MSG.dir_not_set, 'warning');
      return;
    }
    try {
      const r = await pywebview.api.open_output_folder(path, false);
      if (r.success) {
        ConsoleManager.log(MSG.dir_opened(path), 'info');
      } else {
        ConsoleManager.log(MSG.dir_open_failed(r.message), 'error');
      }
    } catch (e) {
      ConsoleManager.log(MSG.dir_open_error(e), 'error');
    }
  }

  // ---- 保存/读取 每阶段服务商+接口地址+密钥 ----
  async function saveStageKey(n) {
    const prov = ($('refineS' + n + 'Provider') || {}).value;
    const key = stageKeyOrNull(n) || '';
    if (!prov) { stageStatus(n, MSG.provider_required, 'err'); return; }
    if (prov === 'lmstudio') { stageStatus(n, MSG.lmstudio_no_key, ''); return; }
    if (prov === 'ollama') { stageStatus(n, MSG.ollama_no_key, ''); return; }
    try {
      const r = await pywebview.api.refine_save_stage_settings(null,
        [{ stage: n, provider: prov, key: key }]);
      if (r.success) {
        stageStatus(n, key ? MSG.key_saved(prov) : MSG.key_cleared, 'ok');
        const inp = $('refineS' + n + 'Key');
        if (inp) { inp.value = ''; inp.placeholder = key ? MSG.key_saved_placeholder : inp.placeholder; }
      } else {
        stageStatus(n, '❌ ' + r.error, 'err');
      }
    } catch (e) { stageStatus(n, '❌ ' + e, 'err'); }
  }

  async function saveStageEndpoints() {
    const st = $('refineEndpointStatus');
    const stages = [];
    for (const n of [1, 3]) {
      stages.push({
        stage: n,
        provider: ($('refineS' + n + 'Provider') || {}).value || '',
        endpoint: stageEndpoint(n)
      });
    }
    try {
      const r = await pywebview.api.refine_save_stage_settings(stages, null,
        { v2_concurrency: readRefineConcurrency(),
          v2_ctx: readRefineCtx() });
      if (st) {
        st.style.color = r.success ? 'green' : 'crimson';
        st.textContent = r.success ? MSG.endpoints_saved : '❌ ' + r.error;
      }
    } catch (e) {
      if (st) { st.style.color = 'crimson'; st.textContent = '❌ ' + e; }
    }
  }

  async function applySavedStageSettings() {
    try {
      const r = await pywebview.api.refine_get_stage_settings();
      if (!r.success) return;
      // 回填并行度（1-5，越界忽略；缺省时保持控件默认值2）
      if (r.settings && r.settings.v2_concurrency != null) {
        const n = parseInt(r.settings.v2_concurrency, 10);
        const sel = $('refineConcurrency');
        if (sel && n >= 1 && n <= 5) sel.value = String(n);
      }
      // 回填上下文窗口（非法/缺省保持控件默认值）
      if (r.settings && r.settings.v2_ctx != null) {
        const c = parseInt(r.settings.v2_ctx, 10);
        const ctxEl = $('refineV2Ctx');
        if (ctxEl && Number.isFinite(c) && c >= 4096) ctxEl.value = String(c);
      }
      for (const s of (r.stages || [])) {
        const n = parseInt(s.stage);
        if (!n || n < 1 || n > 4) continue;
        const pv = $('refineS' + n + 'Provider');
        const ep = $('refineS' + n + 'Endpoint');
        if (pv && s.provider) pv.value = s.provider;
        if (ep && s.endpoint) ep.value = s.endpoint;
        else if (ep && !ep.value) applyProviderEndpoint(n);
      }
      for (const n of [1, 3]) {
        const ep = $('refineS' + n + 'Endpoint');
        if (ep && !ep.value) applyProviderEndpoint(n);
        const prov = ($('refineS' + n + 'Provider') || {}).value;
        if (prov && r.key_status && r.key_status[prov]) {
          const inp = $('refineS' + n + 'Key');
          if (inp && prov !== 'lmstudio' && prov !== 'ollama')
            inp.placeholder = MSG.key_saved_placeholder;
        }
      }
    } catch (e) { console.warn('[refine] 读取已保存接口配置失败', e); }
  }

  // ---- 兜底档位（v2：local=strict / cloud=lenient，无 UI 联动需求）----

  // ---- 质量报告导读查看器（W1b：仅读 *_质量报告导读.json，不读 txt/全量 json）----
  const GUIDE_SUFFIX = '_质量报告导读.json';

  function guideStatus(text) {
    const st = $('guideStatus');
    if (st) st.textContent = text || '';
  }

  function guidePath() {
    const opts = buildRefineOptions();
    const outDir = (opts.output_dir || '').trim();
    const first = (opts.inputs || [])[0] || '';
    if (!outDir || !first) return '';
    const stem = String(first).replace(/\.[^./\\]+$/, '');
    return outDir.replace(/[\\/]+$/, '') + '/' + stem + GUIDE_SUFFIX;
  }

  function guideRender(data) {
    const ulC = $('guideConclusions');
    const dlS = $('guideSections');
    const ulP = $('guideCompanions');
    if (ulC) {
      ulC.innerHTML = (data.conclusions || [])
        .map(c => '<li>' + esc(c) + '</li>').join('')
        || '<li>' + MSG.no_conclusions + '</li>';
    }
    if (dlS) {
      dlS.innerHTML = (data.sections || [])
        .map(s => '<dt>' + esc(s.title) + '</dt><dd>' + esc(s.note) + '</dd>')
        .join('') || '<dt>' + MSG.no_sections + '</dt>';
    }
    if (ulP) {
      const comp = data.companions || {};
      const keys = Object.keys(comp);
      ulP.innerHTML = keys.length
        ? keys.map(k => '<li>' + esc(k) + ' ' + (comp[k] ? '✓' : '✗') + '</li>').join('')
        : '<li>' + MSG.no_companions + '</li>';
    }
    const meta = $('guideMeta');
    if (meta) {
      meta.textContent = MSG.generated_at_label + (data.generated_at || MSG.unknown)
        + ' · ' + (data.basis || '');
    }
  }

  async function guideLoad(silent) {
    if (!window.pywebview || !window.pywebview.api) {
      if (!silent) guideStatus(MSG.api_not_ready);
      return;
    }
    const p = guidePath();
    if (!p) {
      if (!silent) guideStatus(MSG.guide_need_inputs);
      return;
    }
    if (!silent) guideStatus(MSG.guide_loading);
    try {
      const r = await window.pywebview.api.read_output_artifact(p);
      if (r && r.success) {
        const dv = $('refineGuideViewer');
        if (dv) dv.open = true;
        guideRender(r.data || {});
        guideStatus(MSG.guide_loaded(r.path || p));
      } else {
        guideStatus(MSG.guide_load_failed((r && r.error) || MSG.unknownError));
      }
    } catch (e) {
      guideStatus(MSG.guide_load_failed(e && e.message ? e.message : String(e)));
    }
  }

  // 完成翻译后的静默自动探测：成功才展开面板，失败不打扰用户
  function guideAutoDetect() {
    guideLoad(true);
  }

  // ---- 性暗示词替换（legacy 功能，已随 legacy 管线删除）----

  // ---- DOM 绑定（不依赖 pywebview 就绪）----
  function bindDom() {
    if (!$('refineStartBtn') || window.__refineBound) {
      if (!window.__refineBound) setTimeout(bindDom, 300);
      return;
    }
    window.__refineBound = true;

    $('refineStartBtn').addEventListener('click', () => TranslatorManager.startTranslation());
    $('refineCancelBtn').addEventListener('click', () => TranslatorManager.cancelTranslation());

    for (const n of [1, 3]) {
      const rb = $('refineRefreshS' + n);
      if (rb) rb.addEventListener('click', () => refreshModels(n));
      const tb = $('refineTestS' + n);
      if (tb) tb.addEventListener('click', () => testStage(n));
      const pv = $('refineS' + n + 'Provider');
      if (pv) pv.addEventListener('change', () => {
        const sel = $('refineS' + n + 'Model');
        if (sel) sel.innerHTML = '<option value="">' + MSG.model_refresh_hint + '</option>';
        applyProviderEndpoint(n);
        if (window.__pywebviewReady) refreshModels(n);
      });
      const kb = $('refineSaveS' + n + 'KeyBtn');
      if (kb) kb.addEventListener('click', () => saveStageKey(n));
    }

    $('refreshFallbackModels').addEventListener('click', refreshFallbackModels);

    const epSaveBtn = $('refineSaveEndpointsBtn');
    if (epSaveBtn) epSaveBtn.addEventListener('click', saveStageEndpoints);

    const glAddBtn = $('refineGlAdd');
    if (glAddBtn) glAddBtn.addEventListener('click', glAdd);
    const glDelBtn = $('refineGlDel');
    if (glDelBtn) glDelBtn.addEventListener('click', glDel);
    const glImpBtn = $('refineGlImport');
    if (glImpBtn) glImpBtn.addEventListener('click', glImport);
    const glExpBtn = $('refineGlExport');
    if (glExpBtn) glExpBtn.addEventListener('click', glExport);
    const glSaveBtn = $('refineGlSave');
    if (glSaveBtn) glSaveBtn.addEventListener('click', glSave);

    const tStage = $('refineTemplateStage');
    if (tStage) tStage.addEventListener('change', tplLoad);
    const tReload = $('refineTemplateReload');
    if (tReload) tReload.addEventListener('click', tplLoad);
    const tSave = $('refineTemplateSave');
    if (tSave) tSave.addEventListener('click', tplSave);
    const pickBtn = $('refinePickDirBtn');
    if (pickBtn) pickBtn.addEventListener('click', pickDir);

    // 角色卡目录打开按钮
    const openDirBtn = $('refineOpenDirBtn');
    if (openDirBtn) openDirBtn.addEventListener('click', () => {
      openDir($('refineTemplatesDir') ? $('refineTemplatesDir').value : '');
    });

    // 净语配置目录浏览按钮
    const pickCleanerBtn = $('refinePickCleanerDir');
    if (pickCleanerBtn) pickCleanerBtn.addEventListener('click', pickCleanerDir);

    // 净语配置目录打开按钮
    const openCleanerBtn = $('refineOpenCleanerDir');
    if (openCleanerBtn) openCleanerBtn.addEventListener('click', () => {
      openDir($('refineCleanerConfig') ? $('refineCleanerConfig').value : '');
    });

    // S3 引擎联动已随 v2 移除（原 updateS3EngineUI）
    // 性暗示词替换按钮已随 legacy 管线删除

    // 词库编辑器标签页切换
    document.querySelectorAll('.gl-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.gl-tab').forEach(b => {
          b.classList.remove('active');
          b.style.borderBottom = '2px solid transparent';
        });
        btn.classList.add('active');
        btn.style.borderBottom = '2px solid #1a73e8';
        const tab = btn.dataset.tab;
        const glTab = $('glTabGlossary');
        if (glTab) glTab.style.display = tab === 'glossary' ? '' : 'none';
      });
    });

    // 质量报告导读查看器（W1b）
    const guideBtn = $('refineGuideLoadBtn');
    if (guideBtn) guideBtn.addEventListener('click', () => guideLoad(false));
  }

  // ---- 远程数据加载（pywebview 就绪后调用一次）----
  async function loadRemote() {
    applySavedStageSettings();
    // 净语配置目录默认值
    const defCleanerDir = 'config/templates';
    if ($('refineCleanerConfig') && !$('refineCleanerConfig').value) {
      $('refineCleanerConfig').value = defCleanerDir;
    }
    if ($('refineCleanerConfigShow') && !$('refineCleanerConfigShow').value) {
      $('refineCleanerConfigShow').value = defCleanerDir;
    }
    glLoad();
    for (const n of [1, 3]) {
      const pv = $('refineS' + n + 'Provider');
      if (pv && pv.value) refreshModels(n);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindDom);
  } else {
    bindDom();
  }

  window.__refineLoadRemote = loadRemote;
})();

// ============================================================
// Initialization
// ============================================================
document.addEventListener('DOMContentLoaded', async () => {
    console.log('SubTransJAV GUI initialized');

    // i18n：先把 MSG 文案注入 data-i18n* 标记的元素
    applyI18n();

    // Initialize components (pure DOM parts)
    ConsoleManager.init();
    ProgressManager.init();
    ProgressManager.reset();
    FileListManager.init();
    DirectoryControls.init();
    RunControlsInit();
    KeyboardShortcuts.init();
    ThemeManager.init();
    TranslatorManager.init();

    // Update Start button state once at startup
    TranslatorManager.updateButtons();

    ConsoleManager.log(MSG.gui_initialized, 'success');
    ConsoleManager.log(MSG.gui_usage_hint, 'info');
});

function RunControlsInit() {
    // Placeholder hook for future global controls; refine panel owns its own buttons.
}

// PyWebView ready event — backend bridge is now available.
window.addEventListener('pywebviewready', async () => {
    console.log('PyWebView API ready!');
    ConsoleManager.log(MSG.bridgeConnected, 'success');
    window.__pywebviewReady = true;

    await AppState.loadDefaultOutputDir();

    // Load saved stage settings, glossary, templates and model lists
    if (window.__refineLoadRemote) {
        await window.__refineLoadRemote();
    }

    // Initialize feature status indicators
    await FeatureStatus.init();
});

// Feature status management
const FeatureStatus = {
    async init() {
        try {
            const status = await pywebview.api.get_system_status();
            if (status.success && status.features) {
                this.updateGrammarHintBadge(status.features.grammar_hints);
            }
        } catch (e) {
            console.warn('Failed to load system status:', e);
        }
    },

    updateGrammarHintBadge(info) {
        const badge = document.getElementById('grammarHintBadge');
        if (!badge) return;

        if (info.available) {
            badge.className = 'feature-badge active';
                badge.title = MSG.grammar_hint_on_title;
        } else {
            badge.className = 'feature-badge inactive';
            badge.title = MSG.grammar_hint_off_title;
        }
    }
};
