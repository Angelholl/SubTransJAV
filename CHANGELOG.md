# 更新日志

本项目的所有显著变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

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
