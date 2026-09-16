# 更新日志

本项目的所有显著变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布 - 1.2.0]

### 修复

- **断句预合并过度合并**：行尾省略号使「です/ます」句末判定失效导致完整句被误合并、双省略号否决条款缺失、合并跨度复用 `premerge_max_gap_s`（8.0s）无独立硬上限——三处根因一并修复：句末判定前剥离尾部停顿标记、否决条款覆盖「前省略号+后省略号」、新增跨度（毫秒）与字符硬上限；新增 `premerge_max_span_ms`（默认 5000）/ `premerge_max_chars`（默认 80）/ `premerge_min_fragment_chars`（默认 6）三个配置并纳入 `--resume` 指纹。

### 新增

- **双幻觉防护·闸门0**：送翻前源侧幻觉检测（`--source-filter strict|default|off`，默认 default）。七类别分档：纯标点行/`!`串/不可发音辅音串/重复循环/片尾元信息在 default 档即删（白名单词任何档位不删除），孤立应答词/无意义音节连缀仅计数可见。规则库 `config/rules/source_hallucination.yaml` 可整文件覆盖。
- **幻觉处置报告**：每文件生成 `{字幕名}_幻觉处置报告.json`（类别计数/样本/保险阀状态/上游信号/隔离区），运行摘要显式输出闸门0 计数；删除条目归档 `Errors/dropped_entries.log`（新增 `dropped_log_rotate_mb` 轮转，默认 5MB）。
- **保险阀**：单文件拦截率超过 `v2_source_filter_valve_pct`（默认 50%）时降级为只计数，防 ASR 整体崩坏场景误删。
- **上游信号通道**：`--asr-meta` 读取上游 WhisperJAV v1.9.2 运行清单（`whisperjav_run.json`，支持旁车自动发现与新鲜度校验）；suspect/empty/failed 信号触发显著警告、风险清单告警与闸门0 收紧（仅收紧高精确率类别，永不删除应答词类）；语义指纹参与 `--resume` 校验。
- **隔离区**：保险阀降级/关闭闸门0 时，源文高置信幻觉但译文"通顺"的条目移入 `{字幕名}_隔离区.srt` 供人工复核，处置报告同步标注。
- **NDJSON 事件只增**：新增 `gate0_summary` 事件（protocol_version 保持 1，原九类不变）。

### 变更

- **行为差异**：default 档下同一输入的成品条目数可能少于 1.1（源侧幻觉条目在送翻前被删除）；需要完整复现 1.1 行为请使用 `--source-filter off`。详见手册第 7 节"双幻觉防护"。
- 上游信号通道要求上游 WhisperJAV v1.9.2+ 产出 manifest；旧版上游/whisper 系模式产物仍可正常精修（闸门0 同样生效）。

## [1.1.0] - 2026-09-12

### 新增

- **断点恢复**：`--resume` 复用上次中断任务已完成的阶段A 产物（输入/配置/词库/TM 指纹校验通过才复用），`--force-resume` 强制复用；任务清单 `*_manifest.json` 随成功自动清理。
- **NDJSON 事件协议**：`--event-format ndjson` 输出结构化事件（9 种事件类型 + 20 秒心跳），供 GUI 与脚本集成；人类可读文本转往 stderr。
- **风险清单文件**：有风险时自动生成 `*_风险清单.md` / `.json`（info / warning / critical 三级），静默降级显式化。
- **配置分层**：新增 `config/user_settings.json` 与 `SUBTRANSJAV_*` 环境变量两级覆盖，优先级：内置默认 < 用户配置文件 < 环境变量 < CLI/GUI。
- **退出码约定**：`0` 成功 / `1` 执行失败 / `2` dry-run 配置错误 / `3` 部分降级 / `130` 用户中断。

### 变更

- legacy translate 旧栈下线，v2 两阶段净语翻译管线（阶段A 净语+翻译 → 阶段B 审校+抛光）成为唯一主干。
- 批间并发与预合并参数开放可调（`v2_concurrency_max`、`premerge_max_gap_s`、`premerge_max_items` 等 8 个字段），性能可按机器与模型自行权衡。
- 桌面 GUI 体验优化（阶段进度/心跳感知等）。
- 换行治理：仓库统一 LF（`.gitattributes`），CI 增加行尾检查步骤。

### 修复

- Beta 测试（两轮：功能测试 T1~T8 + 并行压力测试）发现缺陷 5 个（D1~D5，含 2 个 P0），全部最小修复并复测通过：
  - **D1（协议）**：ndjson 模式 stdout 泄漏页脚 print（cli.py finally 块绕过事件发射器）；改经 `_echo` 输出（ndjson→stderr，text 模式不变），复测 0 坏行。
  - **D2（P0）**：同配置 `--resume` 必拒绝复用阶段A——compute_config_hash 对 templates_dir="."（仓库根）整树哈希，运行自身产物即改指纹；manifest.py 新增 `instruction_source_files()` 只哈希实际角色卡 + rules.yaml（4s→0.010s）。
  - **D3（P0 连带）**：拒绝复用时 `_prepare_manifest` 立即重写 manifest，中断现场（阶段A=done）被销毁；改为拒绝复用不触碰 manifest。
  - **D4（潜伏）**：SudachiPy Tokenizer 惰性单例非线程安全，并发冷路径抛 `Already borrowed`（12.7%）；双重检查锁 + tokenize 锁，热路径不经锁。
  - **D5（P0，D2 残留）**：TM 指纹取库整文件 sha1，阶段A 命中即 UPDATE hit_count + WAL 滞留导致指纹必漂移；改内容列流式哈希（content_hash/stage/source/target，排除簿记列）。
- 9 项决议全部落地：heartbeat_stale_s 15→45s（阈值倒挂）；`--force-resume` 隐含 `--resume` 且告警文案去重；回环端点绕过系统代理 + 连接拒绝快速失败；v2_file_parallel 标注休眠开关（等 2.0）；降级验收口径依赖风险清单；dry-run 用法错误退出码 2 口径注明手册；GUI 关键控件补 6 个 data-testid；手册补 heartbeat_stale_s 字段与计划文档笔误；D1~D5 修复合批提交（4d0a155）。
- 回归测试净增 25 个（559→584），门禁最终状态：pytest 584 passed + 1 skipped、ruff 0 警告、coverage 71%。
- 提交对应关系：4d0a155（17 文件 +764/−59）。

### 文档

- 新增《使用与维护手册》（`docs/使用与维护手册.md`）：安装、配置分层、断点恢复、事件协议、TM 库运维、风险清单与 FAQ。
- README 增补架构一览、配置分层速查、断点恢复用法、事件协议/退出码简介与 FAQ 精选。
- About 对话框的项目主页链接已于 634138e 回填完成（https://github.com/Angelholl/SubTransJAV）。

## [1.0.0] - 2026-09-11

- v2 两阶段净语翻译管线成为默认主干：净语翻译 / 审校抛光两段角色卡（模板可编辑）。
- TM 翻译记忆库句子级自学习，带准入门槛（低质翻译不进词库）。
- 幻觉检测 + 加固短语规则 + 双语字幕上下文预审（跨行上下文防误翻）。
- Webview GUI + CLI 双入口；质量报告与双引擎分歧分析输出。
