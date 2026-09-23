# 更新日志

本项目的所有显著变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

### 新增

- 引擎自动化并发对齐：重载前核对 `lms ps` 的 parallel 实值，与管线并发配置失配自动重载；检测不可判定时跳过并告警，绝不误重载。

### 移除

- **投机解码 draft 功能彻底移除**：LM Studio draft 挂载全链路清理——GUI 下拉与回填/保存（app.js / index.html）、CLI `--s1/s3-draft-model` 旗标（cli.py）、`StageConfig.engine_draft_model` 字段（config.py）、manifest 阶段指纹字段（manifest.py）、api 透传（webview_gui/api.py）、`ensure_lmstudio_model` 的 draft 形参/`lms ps` 痕迹探测/进程内挂载缓存/speculative 旗标拼接（utils/lmstudio.py）及配套测试。LM Studio 引擎自动化（自动加载/卸载、ctx 对齐、GPU 拉满、并发参数）完整保留；重载命价新增断言不含任何 speculative 旗标，防 draft 回归。

## [1.2.3] - 2026-09-22

### 概述

- 残译清洗修复（[未翻译] 残译形态）、漏覆盖口径补测收口、GUI 四参数、mypy report-only、生产型号归档（详见 `models/README.md`）。

## [1.2.2] - 2026-09-18

### 新增

- **TM 清洗工具 `tools/tm_purge.py`**：以某个输入 srt 的源句为候选集定位同源条目，生成清洗计划用于清退错误/毒化条目。候选集三形态匹配（raw / premerge / normalized，多形态同时命中按优先级取 match_type）；**dry-run 为默认**（零写入，产出 utf-8-sig CSV 审核清单），实际删除须显式 `--yes` 且执行前先全量备份；`--exclude-ids` / `--exclude-file` 白名单承接人工审核裁决；`--probe` 探针快速验证单句命中；`--since` / `--until` 时间窗过滤（`created_at` 为空的行归入 unknown_time 组，指定时间窗时默认不删，未指定时正常进入候选）；`--yes` 删除前对删除清单（entry_id 升序）计算 sha256 写入运行日志供审计。
- **tm_entries `source_name` 溯源列**：TM 库自动幂等迁移补列（旧行为 NULL 不回填），自学习入库统一写入来源 srt 文件名 stem；属 provenance 簿记列，不参与 `--resume` 指纹。旧 TM 无溯源，跨片同源句仍无法区分，观察口径保守。
- **反义误译检测三规则与两条补充规则**：反义三规则（やめて / 最低 / ずるい 源文形态）+ 补充两规则（身体语境「首」、イク 系假名变体高潮语境复核），全部采用**双侧锚定**结构（源文命中指定形态 且 译文命中目标集才告警）与 `warn_only`（只进质量报告复核清单并阻断该翻译对进入 TM 学习，不改动译文）；`config/rules/translation_rules.yaml` 单一数据源，可整文件覆盖。
- **per-片语境 sidecar**：与 srt 同名、后缀 `.context.md` 的旁路文件，含【剧情摘要】与【误听怀疑】两小节（误听词条按「疑似词 => 疑似正解」逐行填写），供 A/B 两阶段注入消解指代与歧义；`context_sidecar` 开关（默认开）可全局禁用；模板见 `docs/上下文sidecar模板.md`。
- **术语冲突观察闸与【术语一致性】章节**：译文与词库译法不一致时先**观察不阻断**——逐术语一致性统计与冲突样本写入质量报告【术语一致性】/【术语冲突观察】章节并累计落盘观察记录；是否转阻断由用户显式开启 `glossary_conflict_block` 裁决（开启后冲突条目同时禁止进入 TM 学习）。
- **质量报告 TM 命中/入库摘要行**：H=阶段A 精确命中数、L=本次学习入库数（阶段A 复用或未启用 TM 时按"取不到"处理），TM 对成品的影响一眼可见。
- **glossary `target_aliases` 可选列**：第三列以 `|` 分隔多个候选译法，命中别名不再误报术语冲突，未填列行为与旧版一致。
- **learned 自学习治理开关与重置工具**：`glossary_learn_enabled=False` 跳过 learned 词库自动学习（止增）；`tools/glossary_learned_reset.py` 提供存量清退（dry-run 默认 / `--yes` 备份后执行 / `--keep-term` 白名单），只触碰 `glossary_learned.csv`，绝不写人工词库。
- **剧情自摘要（auto_synopsis，Beta）**：默认开启，可用 `--no-auto-synopsis` 关闭；闸门0+预合并后过滤纯噪声行、按时间分桶均匀抽样整片剧情行（`synopsis_max_chars` 默认 6000 字限幅），一次独立 LLM 调用（max_tokens=300）生成 3-5 行中文梗概，以【剧情背景（自动摘要·beta）】块注入 A/B 翻译提示词消解指代与歧义——**仅注入提示词，不产生任何输出内容**；手写 sidecar【剧情摘要】非空时手写优先；缓存于独立目录 `Temp/synopsis_cache/`（与 TM 完全隔离），摘要与缓存不参与 `--resume` 指纹；验收采用双轨——正式验收关闭 beta，另开 beta 做对比。

### 修复

- **纯假名实义行误删**：L7/L8/L11 三条 L 规则追加源侧噪声证据门槛——源文不含汉字时还须命中闸门0 计数类噪声特征才允许删除，实义纯假名源文（如应答、口语短句）不再被误删，免删条目计入质量报告"纯假名实义保留"统计。
- **质量报告 CSV 编码**：风险/分歧等 CSV 输出改用 `utf-8-sig`（带 BOM），Excel 直接打开不乱码。

### 变更

- **删除权契约下的 `[未翻译]` 保留策略持续**：翻译失败条目仍以 `[未翻译]` 前缀保留在终稿原文位置，本版继续沿用该契约且范围不变。
- **新配置键纳入 `--resume` 指纹**：`glossary_conflict_block` / `glossary_learn_enabled` / `context_sidecar` 等治理开关变更会使旧断点失效（保护产物正确性）；另请注意 sidecar 文件内容不参与指纹，修改 sidecar 后请勿 `--resume` 复用旧阶段产物，必要时用 `--force` 重跑。

## [1.2.1] - 2026-09-17

### 修复

- **断句预合并过度合并**：行尾省略号使「です/ます」句末判定失效导致完整句被误合并、双省略号否决条款缺失、合并跨度复用 `premerge_max_gap_s`（8.0s）无独立硬上限——三处根因一并修复：句末判定前剥离尾部停顿标记、否决条款覆盖「前省略号+后省略号」、新增跨度（毫秒）与字符硬上限；新增 `premerge_max_span_ms`（默认 5000）/ `premerge_max_chars`（默认 80）/ `premerge_min_fragment_chars`（默认 6）三个配置并纳入 `--resume` 指纹。
- **实义条目静默删除（用户实弹 126 条）根因修复**：模型个别批次漏译、翻译失败、语言校验误杀、兜底清洗误删四处下游删除路径全部关闭——删除权收归闸门0，翻译失败条目改以 `[未翻译]` 前缀保留在终稿，成品条目数不再无故减少。
- **主语误判误报**：风险清单对「我们是」等中文正常主语的告警为正则误报，已修正判定口径。
- **质量报告同条目重复展开**：假名残留检查曾把同一条目的多个假名片段重复展开为多条编号，现按条目归并展示。

### 新增

- **双幻觉防护·闸门0**：送翻前源侧幻觉检测（`--source-filter strict|default|off`，默认 default）。七类别分档：纯标点行/`!`串/不可发音辅音串/重复循环/片尾元信息在 default 档即删（白名单词任何档位不删除），孤立应答词/无意义音节连缀仅计数可见。规则库 `config/rules/source_hallucination.yaml` 可整文件覆盖。
- **质量报告处置台账**：新增【处置】章节（闸门0 删除总数/类别/样本，全量明细归档 `Errors/dropped_entries.log`，新增 `dropped_log_rotate_mb` 轮转，默认 5MB）与条数核对恒等式（原文 = 闸门0删除 + 预合并合并 + 规则清洗合并/删除 + 隔离区移出 + 终稿，失衡标 ⚠️），删除去向一目了然。
- **乱码强译复核**：源文疑似转写乱码但译文通顺中文的条目在质量报告单列小节，提示人工复核语义是否被反转（拒绝↔邀请、停止↔继续等），不自动改稿。
- **语义黄金集**：新增幻觉/语义反转判定的黄金样本回归基线，防止判定规则在后续迭代中悄悄退化。
- **行数重试预算**：批后行数守卫——输出行数与输入不符时自动对缺失行做定向重试（最多 2 轮），漏译行在批内即被补齐而非静默丢失。
- **保险阀**：单文件拦截率超过 `v2_source_filter_valve_pct`（默认 50%）时降级为只计数，防 ASR 整体崩坏场景误删。
- **上游信号通道**：`--asr-meta` 读取上游 WhisperJAV v1.9.2 运行清单（`whisperjav_run.json`，支持旁车自动发现与新鲜度校验）；suspect/empty/failed 信号触发显著警告、风险清单告警与闸门0 收紧（仅收紧高精确率类别，永不删除应答词类）；语义指纹参与 `--resume` 校验。
- **隔离区**：保险阀降级/关闭闸门0 时，源文高置信幻觉但译文"通顺"的条目移入 `{字幕名}_隔离区.srt` 供人工复核，移出条数计入条数恒等式并在运行摘要标注。
- **NDJSON 事件只增**：新增 `gate0_summary` 事件（每文件闸门0 执行摘要；protocol_version 保持 1，原九类不变）。

### 变更

- **`{字幕名}_幻觉处置报告.json` 退场**：该 json 仅覆盖闸门0 且机器格式对普通用户不友好，1.2.1 起不再生成；信息由质量报告【处置】章节承接（人读），机器可读通道为 `gate0_summary` NDJSON 事件。旧版运行残留文件会在成功运行后自动清理。
- **`[未翻译]` 标记保留策略**：翻译失败条目不再被删除，以 `[未翻译]` 前缀保留在终稿（可在质量报告【未翻译】小节逐条核对），由人工决定补译或删行。
- **提示词纳入 `--resume` 指纹**：内置阶段提示词参与配置指纹校验，升级后旧提示词产出的中间稿不会被 `--resume` 误复用。
- **行为差异**：default 档下同一输入的成品条目数可能少于 1.1（源侧幻觉条目在送翻前被删除）；需要完整复现 1.1 行为请使用 `--source-filter off`。详见手册第 7、10 节。
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
