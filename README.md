# SubTransJAV

面向中文用户的日文影片字幕双引擎翻译与精修工具链：LLM 提示词工程 + 术语自学习 + 幻觉检测 + 质量审校，配套 Whisper 转写工具链使用。

A dual-engine subtitle translation & refinement pipeline built for Chinese-speaking users, translating Japanese video subtitles into Chinese: LLM prompt engineering, self-learning glossary/TM, hallucination detection and quality review. Works with Whisper-based transcription toolchains.

## 它解决什么问题

直译工具翻此类字幕的三大痛点：

1. 直白表述生硬尴尬 → refine 角色卡二次精修（净语翻译 / 审校抛光）
2. 术语/人名前后不一致 → TM 术语自学习（带准入门槛防污染）+ 强制术语表
3. 长句幻觉编造 → 幻觉模式检测 + 短语加固规则

## 核心特性

- 双引擎翻译 + 风格指令（tone）机制
- refine 精修管线：净语翻译 / 审校抛光两段角色卡（模板可编辑）
- TM 术语自学习（准入门槛：低质翻译不进词库）
- 幻觉检测 + 加固短语规则（YAML，可自行维护）
- 双语字幕上下文预审（跨行上下文，防误翻）
- 质量报告 + 双引擎分歧分析
- Webview GUI + CLI 双入口

## 架构一览

```
SRT 输入
   │  预合并（短句按时间间隙合并，减少批次数）
   ▼
阶段A：净语+翻译（角色卡①，词库/TM 上下文注入）
   │  产出中间稿 *.subtransjav.srt
   ▼
阶段B：审校+抛光（角色卡②，幻觉检测/加固短语/质量审校）
   │  兜底规则（解析失败/超时降级：保留原文并记录风险事件）
   ▼
产物：*_final_cn.srt 终稿 + 质量报告 / 分歧复核 / 风险清单
   └── 学习沉淀：TM 句子级翻译记忆（带准入门槛）+ 术语词库
```

两阶段各自独立配置服务商与模型（`--s1-*` / `--s3-*`）；云端阶段故障可选本地接管（`--fallback-local`）。

## 受众定位

本项目面向中文用户：默认翻译方向为 日文 → 中文，角色卡模板、质量审校规则与 GUI 均为中文语境设计。

英文等其他目标语言在翻译引擎层受支持（`target_language` 设置），但精修管线（refine）与模板未做英文适配，请自行调整：

- 修改 `config/templates/` 下的角色卡模板（当前硬性要求"只输出中文译文"）
- 调整 refine 规则（`config/rules/translation_rules.yaml` 等）中的语言相关条目

## 快速开始

要求 Python 3.10–3.13。

```bat
:: 安装（核心翻译引擎 + CLI）
pip install -e .

:: 安装（含桌面 GUI）
pip install -e ".[gui]"
```

配置 LLM 端点（DeepSeek / 兼容 OpenAI 协议的自定义端点，或 LM Studio 等本地服务）：

- 全本地（LM Studio / Ollama）不需要任何密钥；
- 云端服务商密钥经环境变量提供：`DEEPSEEK_API_KEY` / `SILICONFLOW_API_KEY` / `CUSTOM_API_KEY`，也可在 GUI 中保存。

命令行示例：

```bat
:: GUI
subtransjav-gui

:: CLI 全流程（单文件）
subtransjav-refine -i 字幕.srt --profile local --s1-provider lmstudio --s1-model <模型名> --s3-provider lmstudio --s3-model <模型名>

:: CLI 批量（目录递归）
subtransjav-refine --input-dir "字幕目录" -r --filter-pattern "*.srt" --exclude "*_final_cn.srt" "*_refine_*" --profile local --lmstudio-endpoint http://localhost:1234/v1
```

常用参数速查：`-i` / `--input-dir -r`（输入）、`--filter-pattern` / `--exclude`（文件过滤）、`-o`（输出目录）、`--glossary`（词库 CSV）、`--tm-db`（指定 TM 库）、`--force`（强制重跑）、`--dry-run`（执行计划预览，不实际调用）。

每部影片产出：`*.subtransjav.srt`（中间稿）、`*_final_cn.srt`（终稿）、`*_质量报告.txt`、`*_分歧复核.csv`（若存在 pass1/pass2 双引擎字幕则含「双引擎分歧」章节）。

## 配置分层速查

优先级从低到高：内置默认 < `config/user_settings.json` < `SUBTRANSJAV_*` 环境变量 < CLI/GUI 显式赋值。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `temperature_cloud` | 0.5 | 云端采样温度 |
| `temperature_local` | 0.1 | 本地采样温度 |
| `premerge_max_gap_s` | 8.0 | 预合并时长上限（秒） |
| `premerge_max_items` | 3 | 预合并条数上限 |
| `v2_concurrency_max` | 5 | 批间并发钳制上限 |
| `timeout_llm` | 900 | LLM 单批超时（秒） |
| `timeout_http` | 60 | HTTP 客户端超时（秒） |
| `timeout_probe` | 5 | 本地服务探测超时（秒） |

环境变量命名：`SUBTRANSJAV_` + 大写字段名（如 `SUBTRANSJAV_TIMEOUT_LLM=600`）。完整语义见 `docs/使用与维护手册.md` 第 2 节。

## 断点恢复

长任务中断后可续跑，复用已完成的阶段A 产物，不重复计算：

```bat
:: 指纹（输入/配置/词库/TM）校验通过才复用：
subtransjav-refine -i 字幕.srt ... --resume

:: 指纹不匹配仍强制复用旧产物（自行承担错位风险）：
subtransjav-refine -i 字幕.srt ... --resume --force-resume
```

任务成功后断点产物（`*_manifest.json`、`*_refine_A.srt`）自动清理；`--force` 为忽略产物整任务重跑（覆盖前自动备份）。

## 事件协议与退出码（供集成/二次开发）

`--event-format ndjson` 后管线向 stdout 输出结构化事件（每行一个 JSON 对象），人类可读文本转往 stderr。事件类型 9 种：`task_started` / `phase_started` / `phase_progress` / `phase_finished` / `warning` / `degraded` / `error` / `heartbeat` / `task_finished`。

退出码：`0` 成功、`1` 执行失败、`2` dry-run 配置错误、`3` 部分降级（需复核风险清单）、`130` 用户中断。完整字段与心跳机制见手册第 4 节。

## FAQ（精选）

- **GBK 控制台乱码？** 程序内部已强制 UTF-8 输出；必要时 CMD 先执行 `chcp 65001`。
- **LM Studio 未启动/探测失败？** 确认服务已启动并加载模型，端口与端点一致（默认 `http://localhost:1234/v1`）。
- **提示模型未指定？** `lmstudio` / `ollama` / `siliconflow` / `custom` 需经 `--s1-model` / `--s3-model` 指定模型名。
- **云端密钥放哪？** 三级解析：CLI/GUI 传参 > 环境变量 > GUI 保存（DPAPI 加密存储）；本地服务无需密钥。
- **只想看执行计划、不实际调用？** 加 `--dry-run`。

更多问题见 [docs/使用与维护手册.md](docs/使用与维护手册.md)。

## 词库与模板（自配）

`config/glossary.csv`、`config/templates/`、`subtransjav/*/defaults/` 均为空模板或通用默认。

本项目只提供翻译工程框架，不分发任何语料/词库数据，按需自行配置。TM 翻译记忆库的自动学习产物（`glossary_learned.csv`、`tm.db`）由你自己的翻译流程生成，管理命令见 `--tm-stats` / `--tm-export` / `--tm-import` / `--tm-clear`。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 声明

- 本项目仅供成年人学习研究字幕翻译技术使用，请遵守所在地区法律法规。
- 上游转写工具：[WhisperJAV](https://github.com/meizhong986/WhisperJAV)——分工：转写归上游，翻译+精修归本仓库（项目名由此而来）。上游 v1.9.2+ 的运行清单（`whisperjav_run.json`）可通过 `--asr-meta` 接入本仓库双幻觉防护（转写可信度信号驱动精修侧自适应过滤）；旧版上游产物同样支持。

## 许可证

MIT
