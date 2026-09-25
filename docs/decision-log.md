# 项目决策日志（decision-log）

> 由主模型维护，重大架构设计、技术选型、方案拍板经 decision-critic 评议后按 "## [日期] [决策ID/标题] [状态]" 格式追加归档，供续评与二次评议检索。

## 2026-09-14 [D2026-0914-01] 1.2双幻觉专项方案评议收尾与落盘 [已拍板]

### 一、背景与版本策略

- 上游 WhisperJAV v1.9.2（2026-09）发布：新增 `whisperjav_run.json` manifest（done/empty/suspect/failed + MILEAGE 覆盖率）、`raw_subs/<name>.asr_telemetry.jsonl` 逐场景遥测（仅 Balanced 模式）、`--fail-on empty|suspect`；qwen 模式源头清理纯标点行/孤立「はい・うん」/`!`串（whisper 系模式不清理）；#394 根因仍开放。
- 本仓 v1.1.0 已于 2026-09-13/14 发布于公开仓 Angelholl/SubTransJAV（commit `325dc47`），测试基线 584 passed + 1 skipped。
- 用户拍板：1.1 定版后走 1.1.x 缺陷修复线；**双幻觉治理自 2.0 前移为 1.2 大版本主题**（推翻原《1.2版本规划草案_2026-09-13.md》行 29"明确不做①"与行 33 决策 1）；2.0 议题清空重新定位；架构边界不变——不引入 ASR 代码，对第一重幻觉只做识别与拦截。

### 二、评议代行偏差声明

- `decision-critic` 子智能体连续三次启动失败（错误：Provider authentication failed），属该智能体类型的供应商配置问题。
- 由 `general-purpose` 子智能体载入同一份只读诤友评议提示词**代行评审**：通读《双幻觉专项讨论简报_2026-09-14.md》与《1.2版本规划_双幻觉专项_2026-09-13.md》，并逐条核实代码证据（`manifest.py:278-305`、`pipeline_v2.py:701/92/890/925-928`、`language_validator.py:44-93/163/187/211-220`、`llm_client.py:357/412/448-452`、`cli.py:16-37`、`tests/test_language_validator.py`、`hallucination_patterns.yaml`、`cleaner_rules.py:155-181`），确认文档关键事实陈述与代码一致、无失实。
- 评议结论：6 个裁决点全部达成共识（4 项同意、1 项有条件同意、1 项同意无保留），2 项 [HIGH_RISK_OBJECTION]，总体判定"文本级修订后即可开工，无架构性返工"。
- 用户审批（2026-09-14）：同意落盘、列入工作，"有条件通过"——条件仅限文档完整性，不改变技术路线、不构成架构返工。
- **执行方式记录**：用户 2026-09-14 指示"当前任务由主模型负责完成，除非主模型存在问题才调用子模型辅助完成"——落盘与决策日志由主模型直接撰写，未回传评议 agent 索取日志文本（评议结论已完整固化于工作区文档）。

### 三、六个裁决点结论

1. **孤立应答词（はい/うん）default 档只计数不删**：strict 档才删，且须叠加位置+密度启发式；keep-list 优先级最高。依据：本仓 1.1 假阳性学费（`language_validator.py:44-47` `_RESPONSE_WORDS`、`:82-88` `_INDEPENDENT_WORDS` 已保护 はい/うん/ふん，真实误删语料已固化回归）；本领域孤立应答词是高频真实台词，精修工具误删代价不对称。
2. **H4 拆层**：4a 薄版 P0（manifest run 状态 + MILEAGE 覆盖率 → 管线警告/报告标注/过滤总开关提档）；4b 厚版 P1 或顺延 1.3（telemetry 场景级信任度 → 条目级阈值自适应）。**自动提档仅收紧高精确率类别阈值，永不解锁计数类**（防上游无 schema 承诺的标签远程打开孤立应答词删除）。
3. **黄金集三源**：历史 dropped_entries.log 固化语料（21 个假阳性回归）+ 用户提供的上游 1.9.2 真实输出（含 suspect/empty 案例）+ 构造样本（须防循环验证：记录生成方式与标注人，定稿冻结版本号）。**真实 1.9.2 语料是"定版阻塞项"而非"开工阻塞项"**：第一、二批可在历史语料+构造样本守卫下开发；定版前语料仍未到位则基线降级标注"构造集基线"，不得宣称真实场景精度。
4. **与上游清理重叠类别保留**：本仓输入契约是"任意来源 SRT"（whisper 系模式、旧版上游、第三方工具产物均在契约内），不能假设上游已清理；规则外置成本近零；闸门0 增量价值聚焦上游不处理的类别。
5. **1.1.x 修复线边界**：YAML 规则数据修补（既有 schema 字段内加词/白名单）＝缺陷修复，可进 1.1.x 热修（白名单是 Python 内置集合 `language_validator.py:44-93`，实为代码改动，须逐一附回归测试；每次热修后跑满 584 基线并 `sync_release.py --check` 同步公开仓）；新检测器/新输入通道＝功能，只进 1.2；1.1.x 修补不得预埋 1.2 源侧类别 schema。
6. **原草案工程项处置**：#4 GUI i18n、#8 LRU、#9 文档修正顺延 1.3；#6 pipeline_v2.py 拆分维持第三批原位；管线三个钩子（闸门0 / H3 报告 / H4 信号）一律"一行函数调用进模块"，不内联（否则第三批拆分成本随 1.2 膨胀）；#1 实测反馈窗口与双幻觉专项共用同一反馈入口。

### 四、两项 [HIGH_RISK_OBJECTION] 的采纳回应

- **R1（resume 指纹缺口）——采纳**：`manifest.py:278-305` 的 `_CONFIG_FIELDS` 不含 `--source-filter` 档位；`instruction_source_files` 不哈希闸门0 规则 YAML；`input_sha1` 不含 `--asr-meta`。落地四件套：①`_CONFIG_FIELDS` 增加 source_filter 档位；②闸门0 规则 YAML 内容哈希并入 config_hash（对齐 cleaner_config_dir_hash 做法）；③`--asr-meta` 指纹定义为**解析后语义字段规范化 JSON 的 sha1**（非原始文件字节 sha1，避免路径/字段顺序变化导致 resume 误失效）；④验收新增条款：信号/规则/档位变化时 `--resume` 必须判定失效。
- **R2（分档表缺表）——采纳**：原方案仅给 4 类定档，`!`串、片尾元信息、无意义音节连缀三类悬空；且"无意义音节连缀"若默认删将与 `language_validator.py:70-80` 白名单保护的长假名重复（あああ/ううう等，本领域真实台词形态）正面冲突。落地为七类别×三档位显式分档表（见第五节 R2 条目），并要求闸门0 keep-list 与 `_WHITELIST_JA` 的交叉守卫测试随 H1 入库。

### 五、R3–R10 处置一览

| 风险 | 处置结论 |
|---|---|
| R3 保险阀 | 闸门0 复刻 `language_validator.py:211-220` 的 >50% 保险阀并变体：拦截率超阈值 → **降级为只计数+报告红标**（非全量放行，防 4a 对 suspect 文件提档在最需要时失效）。默认阈值、配置键、红标格式在计划文档 H1 条目中可验收化 |
| R4 验收错位 | H1 第一批验收改为"假阳性交叉守卫 + 构造 fixture 全类别单测"；"黄金集全覆盖"移至 H6/定版验收 |
| R5 profile 关系 | 闸门0 与 cloud/本地 profile 无关，两档均执行（省 token 收益恰在 cloud 档最大；区别于兜底规则层在 cloud 档跳过，`pipeline_v2.py:861-862`） |
| R6 陈旧信号 | 旁车自动发现需校验 manifest 与 SRT 的配对/新鲜度（mtime 或命名配对），失败降级"无信号+警告"；显式 `--asr-meta` 用户明示即采信并记入报告 |
| R7 三处规则源分裂 | 1.2 内以交叉守卫测试钉住三处一致性（中文侧 `hallucination_patterns.yaml`、源侧新 YAML、`language_validator` 白名单）；规则条目强制携带证据样本；白名单迁共享 YAML 列为可选、不阻塞 1.2；H8 手册给出三库分工图 |
| R8 用户感知 | 运行摘要显式输出闸门0 分类计数；NDJSON 事件只增兼容；手册写明与 1.1 行为差异（同输入成品条目可能变少）及 `--source-filter off` 回退路径；changelog 明示 |
| R9 新产物兼容 | H5 隔离区定为独立 `*_隔离区.srt`（译文通顺条目只有 SRT 形态才能播放对照）；H3/H5 新产物纳入 `delete_resume_artifacts`（`manifest.py:157-169`）扩充；NDJSON 只增不改 |
| R10 条目内重复 | 闸门0 置于预合并前（相邻同文 ≥N 连检测依赖此顺序），同时支持条目内重复检测（正则），覆盖预合并拼接后的残留形态 |

### 六、七类别×三档位分档表（R2 落地）

| 检测类别 | default 档 | strict 档 | off |
|---|---|---|---|
| 纯标点行 | 删 | 删 | 不处理 |
| `!`串（模型无解占位符） | 删 | 删 | 不处理 |
| 不可发音辅音串 | 删 | 删 | 不处理 |
| 重复循环（相邻同文 ≥4 连，含最小长度条件） | 删 | 删 | 不处理 |
| 片尾元信息（时间轴位置+词表双闸） | 删 | 删 | 不处理 |
| 孤立应答词（はい/うん等单独成行） | **只计数+报告** | 删（叠加位置+密度启发式） | 不处理 |
| 无意义音节连缀 | **只计数+报告** | 删（叠加位置+密度启发式） | 不处理 |

keep-list 白名单优先级最高，高于任何档位；H6 黄金集对两个计数类按"漏报（漏删）"统计而非"误报"统计。

### 七、开工条件与门槛

- **开工条件（已满足）**：计划文档文本级修订完成（本次落盘即是）→ 即可开工。
- **第一批 H1+H2 代码实施门槛**：①文档修订完成（本次完成）；②用户确认"列入工作"；③委派 coding 前做一次"文档 diff 核对"——对照 10 项共识缺口确认已全部写入且与讨论边界无冲突。
- 讨论边界（不可发散）：不引入 ASR 代码、不做 ML 过滤器、不做批间污染专项（#347 向量已核实不存在）、1.1.x 只收缺陷修复、v2_file_parallel 与批次级断点续传维持排除。
- 验收口径沿用：pytest 只增不减（基线 584 passed + 1 skipped）、ruff 0、mypy 门禁 0 错、`tools/sync_release.py --check` 幂等、公开仓差异契约不破坏。

### 八、随批生效的附加守则

- 管线三个钩子（闸门0/H3 报告/H4 信号）一律"一行函数调用进模块"，不内联逻辑进 `pipeline_v2.py`。
- 黄金集截止期与责任人：**待用户指定**；逾期未获真实 1.9.2 语料则 1.2 基线降级标注"构造集基线"写入决策记录。
- 本决策由主模型直接撰写归档（依据已固化评议结论），未再联系代行评议 agent；如需二次评议，可检索本条目及《双幻觉专项讨论简报_2026-09-14.md》第 6 节。

### 九、开工审批（2026-09-14 追记，[已开工]）

用户审批结论：**确认"列入工作"，条件通过**（条件仅限文档一致性，不改变技术路线、不构成架构返工）。落盘核验通过（决策日志/计划修订稿 v2/简报第 6 节与 10 项共识缺口一致）。开工前已修正一处阻塞级文档矛盾并落定三项裁决：

1. **H1 闸门0 插入点统一为预合并之前**（`pipeline_v2.py:247-313` `_premerge_entries` 调用之前）：原计划文档中"总体设计图（预合并前）"与"H1.1（todo 构建前 :701 附近，即预合并后）"互斥；"相邻同文 ≥4 连"依赖预合并前的原始条目序列，若在合并后检测则失真。修正后：闸门0 对**原始 SRT 条目**做七类别检测；预合并之后仅保留 TM 精确命中剔除与已删条目跳过；条目内重复检测为补充、不替代相邻重复循环检测。
2. **H4b 顺延 1.3**（用户拍板）：telemetry 仅 Balanced 产出、schema 无承诺、H6 基线未建立前自适应无法验证净收益；4a 薄版已覆盖主要价值。
3. **H6 截止期与责任人**（用户拍板）：截止期不晚于第三批 H6 启动前；用户负责提供含 suspect/empty 的真实 1.9.2 语料，主模型负责归档、防循环验证、逾期降级标注"构造集基线"写入决策记录。
4. **实施注意项纳入 H1 契约**：保险阀阈值 `v2_source_filter_valve_pct` 纳入 resume 指纹（或至少配置变更显著警告）——R1 精神延伸，防用户改阈值后静默复用旧产物。

拆批委派口径：H2 先独立修；H1 按"独立模块 + 预合并前一行调用"实施；H1 第一批验收＝假阳性交叉守卫 + 构造 fixture 七类别×三档位 + 保险阀路径 + 规则/档位变化时 resume 判定失效。

### 十、第一、二批执行与偏差裁决（2026-09-14/15 追记，[已执行]）

**第一批（H1+H2）**：实施完成并通过主模型复核。pytest 585+1、ruff 全绿、新模块 mypy 0 错；主模型复核中直接修复 ruff 4 处（UP031/I001）与 mypy 5 处（类型注解）。

**第二批（H3+H4a）**：实施完成并通过主模型复核。pytest 635+1（+50 新测试）、ruff 全绿、asr_meta.py mypy 0 错。落地：`{stem}_幻觉处置报告.json`（schema 契约测试钉住）、`--asr-meta` 容错解析+旁车自动发现（R6 新鲜度校验，显式路径豁免）、语义指纹 `asr_meta_sha1` 进 config_hash、tighten 收紧块（仅 default 档删五类，计数类契约测试钉死不解锁）、`gate0_summary` NDJSON 只增事件（PROTOCOL_VERSION 保持 1）、R8 摘要显式输出、delete_resume_artifacts 扩充报告模式。

**偏差裁决（gate0_ran 语义，主模型裁决并已实施）**：第二批规格原假设"resume 复用阶段A 时闸门0 未重跑"，实际闸门0 在管线头部**无条件执行**且在受信 resume 下**幂等**（指纹校验保证规则/档位/信号语义与原次一致，重跑结果相同）。故报告口径定为：**如实记录真实计数，`gate0_ran` 恒为 True**，废除原"resume 时归零"规格（归零反而制造报告与 NDJSON 事件的矛盾）；回归测试重写为"含幻觉行输入 + resume 后报告保留真实删除数"。

**其他已接受偏差**：报告写入置于恢复类清理之后一环（防自删，扩充的 delete 模式用于清上一轮遗留）；覆盖率定点化 round(…,4) 防浮点噪声破坏指纹；保险阀触发计入 files_degraded（幻觉行保留送翻属真实降级，exit code 3 语义成立）。

**实弹测试裁决（2026-09-15，真实语料暴露缺陷）**：用 `E:\无字幕\新建文件夹\一次测试`（7 个上游真实 SRT，10924 条）实测发现——白名单词与计数类目标高度重合（うん。823/はい。445），keep_list 短路在计数之前，导致**计数类检出恒为 0**，裁决点 1 附加条件"计数不得静默"落空。裁决并已实施：**白名单词任何档位仍不删除（宁漏勿误不变），但计数类命中计入检出统计**（H3 报告/摘要可见性）；交叉守卫测试断言同步更新（删五类恒 0、计数类按命中计 1）。修复后完整文件实测：mdon-087（545 条）检出 100/删除 0/保险阀未触发，报告与摘要如实呈现。

**根因更正（2026-09-15 追记，推翻本节前述"阶段B 模型服从性差"结论）**：一次测试实跑中"阶段B 无译文 427/545"的真正根因是**本机缺少 `openai` Python 包**（LM Studio 走 OpenAI 兼容客户端，import 失败使阶段B 全部批次秒败；"缺行走定向重试"日志：`No module named 'openai'`），与模型/提示词无关。此前 mdon-087 仍有大部分译文，是 TM 库（17090 条，用户历史积累）与 A 译文兜底掩盖了故障。`pip install openai httpx` 后 80 条样本复跑：阶段A/B 零警告全部成功，译文质量正常。教训：测试环境与依赖声明（pyproject extras）不齐会以"模型质量差"的假象呈现；后续考虑把 openai/httpx 移入主依赖或启动前预检（留给 1.2 收尾评估）。

### 十一、第三批执行与 H6 基线归档（2026-09-15 追记，[已执行]）

**第三批（H5+H6）实施完成并通过主模型复核**：H5 隔离区（保险阀降级路径产出候选，计数类永不入选，off 档恒空；`quarantine_review` 按原始下标→编号映射回捞"源文高置信幻觉+译文流畅中文"条目至 `{stem}_隔离区.srt`，只移不删）；H6 黄金样本回归集（`tests/fixtures/hallucination_golden/golden_v1.json` 冻结 v1.0 + `tools/gate0_golden_stats.py` 统计脚本）。门禁：pytest **696 passed + 1 skipped**（+61）、ruff 全绿、新模块 mypy 0 错。

**H6 基线归档（构造集基线，D2026-0914-01 裁决的降级路径生效）**：

- `golden_v1.0`：样本 49 条（delete 18 / count 9 / keep 22），行为一致 **49/49**；七类别 precision=1.000 / recall=1.000（宏平均 1.000/1.000）。
- **真实 1.9.2 语料截止期已到（不晚于第三批 H6 启动前）而用户暂未提供**——按裁决基线降级标注为**"构造集基线"**，不得宣称真实场景精度；后续用户提供语料后以 `origin: "real"` 追加并递增小版本。
- **防循环验证记录**：首轮核对发现 3 处构造缺陷（GP-RP-001/002/003 整文件全为重复文本导致保险阀降级干扰期望），已修正为混入真实台词（占比 40-45%），留痕于 `generated_by` 与 README。
- 实弹验证补充：二次测试 6 文件全量真实跑完成，闸门0 检出 1371/删除 1（jur-676 第 133 条纯标点行 `。`），报告与预扫描逐一吻合；保险阀全程未触发（最高占比 19%），隔离候选恒 0（符合"仅降级路径产生候选"设计）。

### 十二、BUG-2026-0915-01 预合并过度合并修复执行（2026-09-15 追记，[已执行]）

闲时工单自动化受理并执行（完整设计与根因见 `internal_docs/BUG-2026-0915-01_预合并过度合并.md`，执行者未重新排查，直接按定稿第三节实施）。

**修复落地**：`_premerge_entries` 新增 `_strip_trailing_pause`（省略号归一，句末判定作用于剥离后文本，修 RC1）；省略号否决扩展至"前省略号+后省略号"（修 RC2）；语义断裂档加短碎片门槛 `premerge_min_fragment_chars`；新增三枚硬上限配置 `premerge_max_span_ms=5000` / `premerge_max_chars=80` / `premerge_min_fragment_chars=6`（进 TUNABLE_FIELD_TYPES、manifest `_CONFIG_FIELDS` 与 validate 校验，premerge_max_gap_s 解除对跨度上限的兼任，修 RC3）；`_run_single_v2` 预合并占比 >25% 输出 info 级风险提示（不新增 warning）。新配置未 commit。

**门禁**：pytest **746 passed**（基线 696+1 只增不减，其中 1 处既有测试按新口径意图保持调整）、ruff 全仓 0 错、mypy 全仓 60 错与基线完全一致（新改模块 0 新增）、`sync_release --check` EXIT=0 幂等。

**确定性复验（13 个真实输入，18046 条）**：预合并合并数 **1252 → 53**（-95.8%）、跨度>5000ms 合并产物 **963 → 0**；用户案例（mfyd-074 条目 9-12）逐字保持独立。

**全量 LLM 实弹重跑（LM Studio 双模型、并发 3、TM 同口径、`--force` 自动备份旧产物，输出目录不变）**：13 文件全部成功（7+6），闸门0 正常、保险阀未触发。final 层 >5000ms 条目 **936 → 18**（-98.1%）；用户案例 42,679→50,479（7.8s）/ 51,259→57,039（5.78s）两条过度合并产物 **0 残留**，修复后案例条独立（42,679→46,520 / 51,259→53,679）。残留 18 条经逐条定性为**阶段A/B（LLM 翻译行合并 + 时间轴对齐）既有形态**（13 条与旧版同 timing，5 条同属该类、旧版因预合并掩盖而无同 timing），文本为完整单句译文合并（跨度 5.0-5.5s 略超阈），非预合并产物——若要进一步消除需另立工单（阶段A/B 对齐层），不在本工单范围。

**状态**：修复与复验通过，未 commit/未发布，等用户验收指令。

## [2026-09-16] D2026-0916-01 两仓合并：公开仓收编为唯一开发仓（方案 B'）[已拍板·已执行（收尾见 D2026-0916-02）]

### 背景
- 用户意愿：两仓合一。内部仓 WhisperJAV-traslate（单 master、无 remote、50 commits、121 tracked，历史含敏感内容 sexual_terms.csv / 真实 glossary / 方案2试点归档；工作区在途 1.2 第三批双幻觉专项 + 预合并修复，零备份）并入公开仓 SubTransJAV（origin=GitHub 公开，main，100 tracked，HEAD=cebafda 测试修复已 commit 且 0/0 已 push）。
- 密钥面已核验干净：api_keys.bin 与 glossary_learned.csv 在全部历史零接触（`log --all` 空）。
- 公仓历史泄漏既成事实：根提交 ae4c873 含真实 _SENSITIVE_HINTS 词表，7994909 才中性化；glossary 与 translation_rules 从初始发布即中性化。历史重写为独立选项留档，默认不做（破坏已 clone 副本一致性）。

### 方案要点（B' 出树版）
1. 公开侧先闭环：cebafda（importorskip/skipif）已推送；windows-3.12 的 line-endings 步骤 `shell: bash` 修复实施中；全矩阵绿待下次 push 后人工确认（工单 02 验收口径）。
2. 内部：两处测试修复 + ci.yml 修复移植进内部仓（HRO-1 移植面三处）→ `git add` 核对 staging 面（无 internal_docs/Temp/密钥）→ commit 归档快照 → `git status` 复核。
3. 备份先行：本地 `git bundle` 全量（主备份）+ 私有仓 push（可选云备份）；push 前禁用文件名扫描双保险。
4. 最终 sync：check 模式审 diff（含 decision-log.md 公开性、项目介绍稿重复文件、使用手册覆盖、新增 1.2 文件）→ apply → 公开仓 `git status` 复核。
5. 公开仓本地全量 pytest + push → CI 矩阵验收。
6. 全绿后：internal_docs/、api_keys.bin、sexual_terms.csv、真实 glossary、敏感 Temp 脚本移至仓库树外同级目录（本地路径约定，不入库）；公开仓 .gitignore 提交"本地独有资产"注释段锁定契约；自动化提示词/计划任务路径更新并实弹验证；_internal_archive 改名排最后。
7. sync 退役；中性化契约由四件套接替：路径隔离（出树）+ guard 脚本（1.3 可选）+ 覆盖层（1.3 立项：本地覆盖文件与加载优先级）+ 契约文档化（排除名单沉淀为 docs 章节与本日志）。

### HRO 裁决（decision-critic 异议 → 主模型）
- HRO-1 CI 连续性：**采纳**（修复先入内部仓、sync 后复验）。
- HRO-2 敏感资产入公仓：**采纳出树方案（B' 首选）**；"进树+守卫脚本"三件套不采用。
- HRO-3 备份先行：**采纳**（bundle 主 + 私有仓备，先于任何 apply/rename）。

### 用户拍板（2026-09-16 AskUserQuestion）
- 合并执行：按 B' 六步序列执行（每步汇报，改名归档前再确认一次）。
- decision-log.md 公开性：**脱敏后随镜像公开**。
- 词表/术语表：接受过渡期降级；本地覆盖文件+加载优先级改造 1.3 立项。

### 遗留与风险跟踪
- [执行面增量] ci.yml 的 shell: bash 修复须与测试修复一起移植内部仓（.github 目录整体镜像会覆盖公开侧修改）——已随第 2 步移植。
- 备份落地核验：bundle 用 `git bundle verify`；私有仓 push 后检查可见性与策略告警。
- CI 全矩阵绿需 push 后 GitHub Actions 页人工确认；自动化路径迁移后实弹触发一次轮询。
- 出树的正面副作用：原"Temp/ 同名目录整覆盖风险"消除。
- 1.3 立项：覆盖层 + guard 脚本（可选）。

### 决策日志字段（decision-critic 协议）
- **原决策**：两仓合一，公开仓收编为唯一开发仓（方案 B → B' 出树版）。
- **异议**：HRO-1 / HRO-2 / HRO-3。
- **主模型最终决定**：采纳（3/3）。
- **是否 [PRESSURE-OVERRIDE]**：否。
- **条件闭环状态**：决策层面闭环；执行期验证（sync 后 pytest + CI 矩阵、备份核验、自动化实弹）待执行。

## [2026-09-16] D2026-0916-02 双仓归并收尾执行与旧仓退役准备 [已拍板·已执行]

### 执行记录（工单 MIG-20260916-01，闲时自动化 2026-09-16 16:29 受理，16:5x 完成）

- **D2026-0916-01（方案 B'）执行状态**：1.2 内容已改由公开仓直提交完成（009f398 全量 + 70b2eca 修复），sync_release --apply 未执行即退役（重跑只会回退公开侧修复）；本条记录剩余收尾的落地。
- **备份（HRO-3 落地）**：既有全量 bundle `git bundle verify` 通过（完整历史，HEAD e41090d，51 提交）；试克隆抽查 SHA 6/6 一致、pytest 冒烟 38 passed；三份 MD5 一致副本：原址、`D:\SubTransJAV-internal-archive`、`E:\SubTransJAV-internal-archive`。**修订**：bundle 历史含敏感词表，原"可选云备份"不执行，双副本均为本地第二介质，严禁上云。
- **出树归档（约 61MB）**：internal_docs、translation_memory 全链（备份链/测试库/A-B库）、api_keys.bin、sexual_terms.csv、真实 glossary（37 行）、方案2试点归档、.analysis_tmp、测试文件、项目介绍稿；内部仓 5 个未提交文件散件 + `git diff` 补丁归档（其内容已在公开仓，散件仅为保险）；归档副本 sync_release.py 头部加"已退役，禁止 --apply"警告。内部仓 git 状态零改动（Mimosa 约束下免 commit 设计）。
- **公开仓资产落位**：tm.db 换库（旧 24KB 库备份为 tm.db.bak-pre-migration-20260916；PRAGMA quick_check ok、24121 行、app 加载器 stage=1 lookup_exact HIT——全库条目 stage=1 系内部管线写入口径，schema 与加载代码两仓一致）；api_keys.bin 解密 3/3；glossary.csv `git rm --cached` + 真实 37 行转本地维护；.gitignore 契约段补 config/glossary.csv 与 create_shortcut.py（commit 4354961，不 push）。
- **卸载脚本**：新增 uninstall.bat（GBK 无 BOM、CRLF、风格对齐首次安装.bat；清理桌面 SubTransJAV.lnk + 4 个历史遗留名 + %LOCALAPPDATA% numba_cache；备份提醒 文档 output 与项目内 api_keys.bin/tm.db；不删安装文件夹本身）；test_strings_and_shortcut.py 追加 6 项断言。验收：pytest 全量 **753 passed / 1 skipped**（基线 747 + 新增 6，只增不减）、行尾守卫与 ruff 通过、GUI 启动冒烟存活至超时、TM 命中与密钥解密冒烟通过。
- **旧仓处置**：`D:\WhisperJAV-traslate` 留给用户手动删除（前置=四项验证通过 + bundle 三副本就位，均已满足）；删除后队列文件随之消失，轮询自动化按"文件不存在静默结束"设计自然失效。
- **遗留**：1.3 立项（词表覆盖层 + 加载优先级、guard 脚本可选、Mimosa 21 项甄别、1.2 定版 tag）见归档 internal_docs 交接文档；公开仓 push 由用户单独拍板。

## [2026-09-16] D2026-0916-03 v1.2.1 修复方案评议拍板（删除权收口/台账/分工/反演防护）[已拍板·已执行（待实弹验收/提交）]

### 一、背景与诉求

- 用户实弹成品反馈（源 1162 条 → 终稿 1036 条）：126 条实义条目被静默删除（含汉字台词「部長さん。」「…最高…」）；保留条目时间戳与源逐毫秒一致但出现空洞；5 条终稿日文假名残留；1 条 ASR 乱码源文被 LLM 语义反转译（原意"快停下"→"来含住吧"）；主语误判告警实为正则误报；质量报告同一条目按假名串重复展开 5 条编号；统计行"规则清洗合并 114 处"实为删除为主、命名误导；幻觉处置报告.json 仅覆盖闸门0 且普通用户看不懂。
- 用户四项诉求：①源字幕实义行必须翻译，不得静默丢弃；②质量报告去重、如实统计；③处置报告换方式或取消；④采纳分工提案（环节A只翻译不处理时间轴/删行，环节B润色审核）。
- decision-critic 评议（2026-09-16）出具 D1–D7 独立意见：全部"有条件支持"，D1/D7 标 [HIGH_RISK_OBJECTION]，D4 检出 [MATERIAL_CONFLICT]（规格文档认定"我们是"作主语=误判 vs 用户判定为误报，口径相反）。
- 主模型逐项回应并获用户批准进入执行。

### 二、各决策裁决一览

| 决策 | critic 立场 | 主模型裁决 | 关键条件/配套 |
|---|---|---|---|
| D1 删除权收归闸门0 | 有条件支持（HRO） | **采纳（含全部四条件）** | 见第三节 |
| D7 A/B分工+行数强约束 | 有条件支持（HRO） | **采纳（按改造意见）** | 见第三节 |
| D2 删除台账+统计拆分 | 支持 | **采纳+恒等式** | 新增条数恒等式核对行 |
| D3 假名残留按条目归并 | 支持 | **采纳+片段上限** | 单条片段清单上限 5 段 |
| D4 主语误判正则 | 有条件支持（口径冲突） | **采纳方案(a)（收窄到单数）** | 见第四节裁定 |
| D5 反语义反转硬规则 | 有条件支持 | **采纳（按条件）** | 复用闸门0 现成信号，不新造启发式 |
| D6 处置报告.json 取消落盘 | 有条件支持 | **采纳（按四条件顺序）** | 排 D2 之后；外部脚本消费者风险由用户侧接受 |

### 三、HRO 回应与落地条件

**D1（采纳，四条件全含，且为执行契约）**：
1. 阶段A空输出兜底链显式化为契约：`allow_empty_deletions` 双阶段转 False；新增 e2e 用例（FakeClient 模拟模型拒不输出）断言"终稿 index 集合 == 闸门0后集合（预合并除外）"；最弱兜底=空输出保留原文+[未翻译]标记，作为契约底线写入。
2. `_keep_original` 路径标记化：回退原文的条目必须带 [未翻译] 标记并纳入质量报告"未翻译/假名残留"统计口径，不得绕过语言过滤静默通过。
3. `clean_srt` 增加源文映射入参（按时间轴对齐）：`_should_delete` 前置源侧证据检查，合并行任一源行含汉字即保留，证据建立失败一律 fail-safe keep；新增「部長さん」「…最高…」类用例回归（0 删除断言）。
4. `V2_STAGE_PROMPTS` 常量纳入 manifest `instruction_source_files` 指纹（跨批硬条件，与 D7 共享）；改提示词后 `--resume` 必须拒绝复用旧阶段产物。
- 附带：`noise_left_empty` 禁删后恒 0，同步清理其语义与相关测试；strict 档用户显式删除语义保留（档位差异写入手册）。

**D7（采纳，按改造意见）**：
1. 行数守卫降级为批后日志断言+防御线；主守卫=现有逐行解析+缺行重试；新增重试预算 N=2，预算用尽降级逐行 fallback（[未翻译]+风险清单），绝不整文件失败；FakeClient"恒定合并2行"用例断言预算与降级路径。
2. 提示词四文件（V2_STAGE_PROMPTS + 两张角色卡）同步修改，`test_rules_loader` 同步更新。
3. 指纹补齐同 D1 条件4。

### 四、D4 口径裁定（[MATERIAL_CONFLICT] 收敛）

**裁定：采纳方案 (a)——收窄到单数。** 理由：用户为最终验收人，已对该实例做出判定（僕たち水泳部の部長で… 应译"……是我们的部长"，定语/所属读法），"我们是"开头在该场景非误判；误报侵蚀复核清单公信力。
- ① `target_pattern` 加负向前瞻排除 我们/俺们，仅命中单数"我是/俺是"；
- ② 告警文案动态引用实际译文开头（废弃硬编码"以'我是'开头"）；
- ③ `translation_rules.yaml:45` 注释与角色卡第 72 行反例同步改写，消除规格自相矛盾；
- ④ TM 污染补偿：该形态依赖卡A/B 既有主语规则防护，并在该实例作为卡片示例补充；
- ⑤ learn_gate 既有缺陷过滤保持覆盖。

**D5（采纳）**：复核集复用闸门0 现成信号（`count_positions`/`gate0_noise_indexes` × `is_fluent_zh`），不新造启发式；叠加强信号过滤（单条重复连打/单元平铺）；复核清单设上限（"其余 N 条略"）；角色卡硬条款含终局选项"无法辨识→[未翻译]+原文，严禁从零编造"；「快停下→来含住吧」案例入黄金集回归；实弹复跑该文件验证。

**D6（采纳，按四条件顺序）**：排 D2 之后；契约测试迁移至质量报告处置章节 + `gate0_summary` payload 键集；`delete_resume_artifacts` 保留旧 json 清理条目；手册 §7.2 与 CHANGELOG 同步；外部脚本消费者风险由用户侧接受（用户明确要求移除）。

**D2（采纳+恒等式）**：新增条数恒等式核对行——`原文 = 闸门0删除 + 预合并合并 + cleaner合并 + cleaner删除 + 终稿`；逐条台账引用 `dropped_entries.log`，报告只放样本+计数；`clean_srt` 返回结构化统计（合并/删除拆分）。

**D3（采纳+片段上限）**：按条目归并，单条片段清单上限 5 段+"等 N 段"；结论行按条目数；假名残留/未翻译两章统一按条口径防重复计数。

### 五、批次计划与发布结构

- **发布结构**：v1.2.1 单版本，内部按顺序执行：
  - **P0（D1+D2+D3+D4）**：删除权收口（含阶段A禁删行）、删除台账+恒等式、报告去重、正则修复。**P0 全绿 + 用户 1162 案例复跑验证（终稿指数=闸门0后指数、恒等式成立）为同版发布门槛**。
  - **P1（D5+D7 剩余）**：反语义反转硬规则+复核、B 卡改写、行数断言+重试预算、指纹补齐。
  - **P2（D6）**：json 移除 + 契约测试/文档迁移（依赖 D2 处置章节就位）。
- 禁止任何颠倒顺排（D5 依赖 D1 行为前提；D6 依赖 D2）。

### 六、决策日志字段

- **原决策**：v1.2.1 修复方案七项（D1–D7），用户四项诉求全量覆盖。
- **我的异议**：D1/D7 为 [HIGH_RISK_OBJECTION]（均附改进条件）；D4 检出 [MATERIAL_CONFLICT]（"我们是"作主语=规格反例 vs 用户判误报）；D6 未知外部消费者 [UNVERIFIABLE]。
- **主模型最终决定**：采纳（7/7，含全部条件）；D4 口径选择方案(a)收窄到单数。
- **条件是否已闭环**：决策层面闭环（条件全部明文化为执行契约）；执行期验证待 P0/P1 完成时逐项勾验（e2e 指数断言、FakeClient 空输出/恒定合并用例、resume 指纹失效用例、cleaner 0 删除回归、黄金集反演案例、实弹复跑）。
- **是否 [PRESSURE-OVERRIDE]**：否。
- **后续风险跟踪**：①D4 收窄后该形态翻译对将进入 TM 学习面，凭卡片示例+learn_gate 防护，真实语料复验观察误学率；②D1 强制翻译噪声行的怪译文观感，凭 D5 复核清单兜底，P0 实弹复跑时抽检；③D6 外部脚本消费者（不可枚举）依赖用户侧接受，手册注明 json 取消与事件替代通道；④resume 指纹补齐需验证"仅改提示词常量"路径亦失效；⑤strict 档删除语义保留的档位差异文档化。

### 七、执行追记

- 2026-09-17：P0-D1①（下游只译不删：V2_STAGE_PROMPTS 新契约、allow_empty_deletions=False、_keep_original 标记化、阶段B禁删、语言过滤标注化、noise_left_empty 清零、V2_STAGE_PROMPTS 纳入 config 指纹）已实施，e2e/FakeClient/resume 指纹用例通过；P0-D1②/D2（cleaner 源侧证据门槛 + clean_srt 结构化统计 merged/deleted/deleted_by_rule/kept_by_source_evidence，merge_stats 新增 clean_deleted 等键）已实施，cleaner 源侧证据 0 删除回归通过。
- 2026-09-17：P0-D2③/D3（质量报告【处置】章节、条数恒等式、假名残留按条目归并、【未翻译】小节）已实施；P0-D4（subject_misjudge 正则收窄单数、动态告警文案、两卡反例改写）与恒等式补"隔离区移出"项已实施。
- 2026-09-17：P1（D5 反反转硬条款四处同步、strong_garble_signal × is_fluent_zh × 源文无汉字 复核检测、【乱码强译复核】小节、semantic_v1.json 语义黄金集；D7 行数守卫 + 重试预算 N=2 + 降级 fallback 风险清单）已实施。
- 2026-09-17：P2-D6（处置报告.json 落盘移除、契约测试迁移至 gate0_summary payload + 处置章节、manifest 保留旧 json 清理、手册 §7.2/§10、GUI 文案）已实施；版本落 1.2.1（pyproject.toml + __version__.py），CHANGELOG [1.2.1] - 2026-09-17 条目就绪。
- 2026-09-17 收尾：全量测试 765 passed / 1 skipped（tests/test_process_manager.py 7 项 Windows 环境性预存失败不计入，与本次改动无交集）。风险跟踪④（仅改提示词常量即 resume 失效）由 test_resume_rejects_changed_stage_prompts / test_config_hash_tracks_v2_stage_prompts 钉死；风险跟踪②③待用户实弹复跑抽检。改动未提交，待用户验收。

## [2026-09-17] D2026-0917-01 v1.2.2 质量修正与 TM 防二次污染（G1–G7 评议拍板）[已拍板·代码侧 A–D 完成，待批次 E 实弹验收]

**背景三事实**（docs/v1.2.2-计划表.md §一）：
1. 错译已固化进 TM：tm.db 存量 24,214 条，探针证实"やめて→别停"等错译在库；实弹重跑 1130/1160 条 TM 精确命中直接复用——不清洗 TM，任何提示词/规则/词表改动对约 94% 的行无效。
2. 六类错译病因确认（11/13 例核实属实）：语境级硬伤（バラまく 0/5）、高频反义（やめて→别停 7/32）、专名幻觉（オラ 音译、宮下错译）、术语不一致（クリ）、ASR 误听（ペソ/マズミ/らめ）、纯假名实义行被删（strict 档 L8×73 + L11×15 + L7×4）。
3. 二次污染路径确认：反义/术语错译可经 TM 学习与 learned 自学习词库再次入库，形成"翻译一次、错误永久"闭环。

**批次结构**（顺序即依赖，每批全量测试绿后进下一批）：
第 0 步决策归档（本条目）→ 批次 A（TM 治理：tools/tm_purge.py 默认 dry-run、无 --yes 不删、export_csv 全量备份；tm_entries 加 source_name 列，旧行迁移置 NULL；本批不发生删除）→ 批次 B（glossary.csv 硬术语表 + 反义规则三条件，warn_only 检测 + A/B 卡禁令）→ 批次 C（per-片 sidecar 一机制两用途；L8/L11/L7 源侧噪声门槛收紧）→ 批次 D（术语冲突观察闸 + 一致性统计 + learned 重置 + 拟声条款）→ 批次 E（--yes 实际清洗 → 全新重跑 → 按计划表 §二 8 项指标逐项验收）。--yes 仅批次 E 且 B/C/D 全部落地后允许。

**TM 四层防治**（对应三污染路径：TM 学习 / learned 自学习 / B 阶段旧错译覆盖入库）：
1. 事前拦截（入库闸）：反义规则命中经 flagged_indexes 阻断 TM 学习；术语冲突闸先"观察模式"统计误伤率、确认后转"阻断模式"；learned 学习同步适用冲突过滤（批次 B/D）。
2. 事中标记（可追溯）：tm_entries 新增 source_name 来源列，学习时记录出处（批次 A）。
3. 事后可撤销：本片清洗工具 tm_purge + 后续基于 provenance 的按片清理（批次 A/D）。
4. 持续观测：报告新增【术语一致性】章节 + TM 命中率/入库数摘要；"清洗后首轮命中≈0"为流程断言（批次 D）。

**用户 5 契约点（全部采纳）**：
① 指标 7 口径拆分：本片旧命中 0 / 跨片同源句 dry-run 单列用户确认 / 合法复用正常统计 / 错误条目探针 0 命中。
② A1 多形态+可审计：raw/premerge/normalized 三 match_type 候选集；CSV 七列含 created_at/hit_count；--since/--until 时间窗；NULL 归 unknown_time、默认不删除非 --include-unknown-time；删除清单排序后 sha256 写运行日志；--probe 内置。
③ B2 反义规则双侧锚定：源文 やめて 形态 且 译文命中"别停/不要停"目标集；源侧负向排除 やめないで/やめるな；"住手"不在目标集不会误阻断；仅 local/strict 档生效，手册注明；卡措辞"やめて 禁止默认译'别停/不要停'，优先'住手/停下'，按语境裁定"。
④ D1 观察闸转阻断标准：连续 3 次运行或累计 ≥300 样本、误伤率 <2%、人名/解剖词/称呼类零误伤；最小样本护栏（连续 3 次累计候选不足 100 只观察不转阻断）；glossary 支持 target_aliases 向后兼容，learned 不生成别名。
⑤ C2 审计口径：门槛保留行计入终稿，恒等式结构不变；报告新增"纯假名实义保留：N（其中 [未翻译] 标记 M）"；cleaner 新计数器 kept_by_noise_gate。

**用户实现注记（新增边界）**：created_at 为 NULL 的行归 unknown_time，指定时间窗时默认不删；normalized 为独立 match_type 逐行可辨；删除清单排序后 sha256 写运行日志。

**两条风险（手册定稿措辞）**：
1. "清洗改变 TM 指纹，旧 resume 必然失效——删旧 resume 产物，禁止复用。"
2. "旧 TM 无 provenance，清洗可能跨片误删同源句，只能靠 dry-run + CSV 备份兜底。"

**G7 模型实测**：列为批次 E 可选旁路 E3（Sakura-GalTransl-13B vs qwen2.5-14B，同卡同规则、TM 关闭），不作为本版主线。

**分工**：用户侧——B1 词条内容审核、C1 sidecar 内容提供、E1 清洗清单确认、E2 重跑执行与指标验收（E3 可选参与）；代码侧——批次 A/B/C/D 全部机制、测试、文档、版本收尾由 coding 子智能体实施。

**decision-critic 异议记录**：G5（TM 清洗）曾提 [HIGH_RISK_OBJECTION]，主模型按三条件采纳执行（dry-run 默认不直接删；删除清单 CSV 备份 + sha256 可审计；--yes 限定批次 E 且 B/C/D 全部落地后），三条件已全部落入批次契约。条件是否闭环：机制条件已写入计划，最终闭环以批次 E 实际执行（dry-run 清单人工确认 + CSV 备份留存 + 8 项指标验收）为准。无 [PRESSURE-OVERRIDE]。

**后续风险跟踪**：① 批次 E 清洗执行前须留存 CSV 备份并人工核对 dry-run 清单；② 观察闸转阻断须满足契约点④标准并记录样本量与误伤率；③ 指标 7 口径拆分后跨片同源句单列项须用户逐次确认；④ 版本收尾时本条目状态转"已执行"。

**执行追记（2026-09-18）**：
- 批次 A 完成（定时任务触发执行）：契约审计通过（上次中断执行遗留物经逐项审计仅补 1 处 docstring 缺口）；tests/test_tm_purge.py 14 用例；全量 779 passed；真实库 source_name 列幂等迁移确认、零写入；样例片 dry-run 删除候选 1,136 条（raw 997 / premerge 2 / normalized 137），删除清单 sha256 3bcfa5a5…3b879。
- 用户人工审核裁决：raw 997 + normalized 137 批准删除；premerge 2 条（entry_id 43755/43843）暂缓转白名单——**批次 A 有条件通过**。CSV 物理行数疑云已解（1,137 物理行=表头+1,136 记录、零内嵌换行，不存在未审记录）。CSV 编码缺陷（实际无 BOM UTF-8，与契约 utf-8-sig 不符）已修复并新增 BOM 契约测试。
- 白名单机制落地：tm_purge.py 新增 --exclude-ids / --exclude-file（would_keep_excluded / user_hold_whitelist，不入 sha256 删除清单）；裁决存档 Temp/translation_memory/tm_purge_hold_ids_20260918.txt；**批次 E --yes 执行时必须携带 --exclude-file 该文件**。
- 批次 B2 完成：antonym_yamete / antonym_saitei / antonym_zurui 三条 warn_only 规则（双侧锚定+源侧负向排除 やめないで/やめるな）入 translation_rules.yaml 两份副本；A/B 卡反义禁令行；antonym_v1.json 语义黄金集（8 用例）；learn-gate 阻断直接用例；全量 801 passed。
- 批次 C 完成：C1 per-片 sidecar（剧情摘要+误听怀疑，冻结措辞注入、命中即列留痕【误听疑似改写】、context_sidecar 开关入指纹、模板 docs/上下文sidecar模板.md）；C2 L7/L8/L11 源侧噪声证据收紧（is_source_counting_noise：keep_list 与含汉字不算噪声；kept_by_noise_gate 计数器；报告"纯假名实义保留"行）；3 个旧"纯假名即可删"用例按新契约更新；832 passed。
- 批次 B 审批闭环（用户裁决）：9 词条入 glossary.csv（イク 译法定为"要去了"，其余按建议），纯追加+备份 glossary.csv.bak-20260918，load_glossary_merged 验证 11/11 命中；两项可选项均只补规则不入表——body_part_kubi（首：习语负向排除）与 climax_iku_variant（イク变体：显式负向排除 行く系与精确イク，捕获变体泛化译"去"），均 warn_only/双侧锚定/仅 local/strict/TM 阻断沿用既有链路；A/B 卡补 身体语境首 与 イク系 两行；837 passed。B2 已实施内容（三 antonym 规则/卡行/黄金集/测试）经用户批准无调整。
- 批次 D 完成：术语冲突观察闸（默认观察不阻断，glossary_conflict_block 转阻断由用户裁决；跨运行 JSON 累计与三态建议行+最小样本护栏）、glossary target_aliases 可选第三列（注入只用主译法、冲突判定豁免别名、learned 不生成别名）、【术语一致性】章节与 TM 命中/入库摘要行、glossary_learn_enabled 开关 + tools/glossary_learned_reset.py（tm_purge 同款纪律）、拟声行卡条款（两卡+四源钉扎）；866 passed。
- v1.2.2 代码侧收尾完成：版本 1.2.2（pyproject+__version__）、CHANGELOG [1.2.2] - 2026-09-18、手册新增 §11「1.2.2 行为变化一览」（11.1–11.6，含两条风险定稿措辞与 strict/cloud 档位差异）；全量 866 passed / 1 skipped，ruff 全项目 All checks passed。批次 E 待用户实弹执行（--yes 清洗携 --exclude-file 白名单 → 全新重跑 → 8 项指标验收）。
- 备注：用户侧记录"磁盘 CSV 1,167 行差异仍记录在案，放行磁盘全集需另行对账"——实测口径为 1,137 物理行=表头+1,136 记录、零内嵌换行，两说并存留档。

## [2026-09-17] D2026-0917-02 生产模型替换选型（Qwen3.8-27B-Uncensored 候选路线）[已拍板·待E3实测裁决]

### 一、决策问题

是否以 Qwen3.8-27B-Uncensored（IQ3_M 为投产底线、否决 IQ2_M）替换当前本地双模型（阶段A 净语翻译 + 阶段B 审校抛光），并并入 v1.2.2 批次 E 一次性"清洗 TM + 新模型全量重跑"（避免两次全量重跑）。

### 二、已核实事实

- **换模型通道**：`--s1-model` / `--s3-model` 免码可换（`subtransjav/refine/cli.py:48,56`）；`config.py:22` temperature_local=0.1、`:29` timeout_llm=900、`:218` batch_local=30、`:253` v2_ctx_local=32768。
- **当前生产双模型型号无记录**（`models/` 目录为空、决策日志空白）——E3 前置缺口。
- **根因记录**（D2026-0917-01、v1.2.2-计划表 §一）：错译主因＝TM 固化（tm.db 存量 24,214 条；实弹 1130/1160 精确命中，约 94% 的行 TM 直接复用绕开模型）；六类病因以 TM/规则污染为主轴。
- **代码事实**：`subtransjav/translate/llm_client.py:277-283` 请求 kwargs 为白名单构造（model/messages/stream/temperature/max_tokens），**无 `extra_body` / `chat_template_kwargs` 透传口**；`:292-299` 已有思考型模型兜底（content 为空时取 reasoning_content/reasoning）。
- **质量基建就绪**：闸门0、quality_report、`tests/test_golden_set.py`（golden_v1.json + semantic_v1.json）；无跨模型翻译质量自动基准。
- **量化/情报**：IQ2_M KLD 0.0702 出处＝Artefact2 GGUF 量化横评 gist（通用基准，非 27B 专项、非翻译专项）——降级标注，不作唯一否决依据；heretic 系去审查 KLD≈0.0021 为 3.6-27B 测值；3.8-27B heretic 存在性与 MTP/FastMTP 在 LM Studio 的支持度为待补证项。

### 三、decision-critic 异议记录（D2026-0917-02）

**HRO-1（[HIGH_RISK_OBJECTION]，点1"值得替换"先验结论）**：与既有根因记录（TM 固化 94% 命中为主因）冲突，且现模型无型号、无 TM-off 基线——要求在证据具备前不得以"能力上限"为换型前提。**主模型回应：采纳（措辞降级）**。顺序固定：①记录现双模型实际型号（用户侧查 LM Studio）→ ②E3 TM-off 实测（含现模型对照组）→ ③胜出者才随批次 E 全量重跑。写入显式出口：**"若现模型在 TM-off 下指标 1/2/5 达标，则不替换，仅走原'清洗 TM+现模型重跑'主线，模型替换顺延独立立项。"**

**7 项条件——全部采纳**（后两处为主模型补充）：
1. E3 实测四探针（反义/语境/术语/结构化）+ 拒答探针 + 语义黄金集；
2. IQ2_M 保留投产否决、纳入测量档；回退链重序：27B IQ3_S（≈11.8GB）→ Qwen3.6-35B-A3B heretic（MoE 路线）→ 暂缓不换；
3. 模型对比口径＝指标 1/2/5 + 探针；8 项验收留在 E2 全量重跑阶段；
4. E3 前先记录现型号；
5. 候选上限 4（现双模型 / Sakura-GalTransl-13B / 3.8-27B IQ3_M / IQ2_M 测量档），固定样例片与种子；
6. "关闭 thinking"优先尝试 LM Studio 模型级/服务器级开关（零代码改动）；仅当无法端到端验证生效时，才新立 coding 任务为 `llm_client.py:277-283` 增加 extra_body/chat_template_kwargs 透传口，并加"content 非空且非思考链"断言——**本决策不改代码**；
7. 批次 A-D 不阻塞于模型胜负；27B 吞吐下降入批次 E 单批耗时预算。

### 四、主模型最终决定

- **结论措辞**："值得替换"降级为**待证假设，实证裁决**；胜出与出口条件按 HRO-1 执行。
- **量化**：IQ3_M 为投产底线；IQ2_M 投产否决保留（依据降级标注为通用基准，不作唯一否决依据），纳入 E3 测量档。
- **变体取舍**：unsloth 官方未去审查不可直接投产（JAV 内容拒答风险）；JonathanColetti 来源不明弃用；HauhauCS Aggressive 由"零拒绝保底"降级为**最后手段**，须过语义黄金集 + 乱码强译探针方可入选；heretic 系优先（判据＝实测四件套：指令遵循 / 结构化输出 / 反义与语境 / 拒答率，KLD 仅作平手参考）。
- **流程**：E3 TM-off 先行（含现模型对照）→ 胜出者随批次 E"清洗 TM + 新模型全量重跑"一次；E2 验收 8 项指标 + 单批耗时预算。

### 五、条件闭环状态

机制条件未闭环（决策层面已拍板）。闭环项：①E3 TM-off 实测（四探针 + 拒答 + 语义黄金集）；②thinking 关闭端到端验证生效（LM Studio 模型级/服务器级优先）；③内存/KV 预算核算——硬件已由用户确认（2026-09-17：i5-14600KF + RTX 5060 Ti 16GB + 64GB DDR4），核算口径改为：dense 27B IQ3_S/IQ3_M（11.8-13.5GB）+ KV q8_0 按 16GB VRAM 全载为基准；MoE 路线按 expert offload 至 RAM 口径核算；④现双模型型号记录。

### 六、是否 [PRESSURE-OVERRIDE]

否。

### 七、后续风险跟踪

- ① IQ2_M KLD 0.0702 出自通用基准（Artefact2 gist），降级标注，不得作为唯一否决依据；
- ② 3.8-27B heretic 存在性与 MTP/FastMTP 在 LM Studio 支持度＝**两日内补证项**，[UNVERIFIABLE] 未闭环前不静默携带；
- ③ 批次 E 若胜者 E2 全量未过 8 项验收线（届时 TM 已清洗）：回退＝CSV 备份恢复 TM + 退回次胜者重跑；
- ④ 换型为交付差异，须进 CHANGELOG 与手册（含档位差异）；若 thinking 无法模型级关闭而需代码改动，另立工单；
- ⑤ 现型号与换型后型号一并补记本文档空缺（决策日志空白项）。

### 八、分工

- **用户侧**：现双模型型号记录；E1 清洗清单确认；E2/E3 执行与 8 项指标验收。
- **代码侧（本决策不改代码）**：仅当 thinking 无法在 LM Studio 模型级/服务器级关闭且端到端验证失败时，才新立 coding 任务增补透传口 + 断言；批次 A-D 机制实施不受模型选型阻塞，可与 E3 并行。

## [2026-09-17] D2026-0917-02-R1 生产模型选型 E3 名单定稿与 enet45 取舍（续评）[已拍板·待用户下载确认]

**一、续评依据**：基于 D2026-0917-02 原条目（HRO-1 出口条件 + 7 项条件 + 风险跟踪 5 项）续评，任务起于 2026-09-17 11:11。立场整体维持；两处修订（见三）；一处升级为 [HIGH_RISK_OBJECTION]（见六，已采纳闭环）。

**二、新事实核验（5 条，均已核实）**：① 硬件 i5-14600KF + RTX 5060 Ti 16GB + 64GB DDR4："内存只容 IQ2_M"前提消失，16G 显存可全载 dense 27B IQ3_S/IQ3_M（11.8-13.5GB）+ q8_0 KV，MoE expert offload 路线成立；② thinking 可全候选模型级关闭 → 原条件⑥（llm_client.py 透传口备用工单）作废；③ Sakura 家族最新为 Sakura-14B-Qwen3-v1.5-GGUF（已上 Qwen3 底座），语域适配问号成立（训练语料为通用日文+轻小说/Galgame 书面文本 vs 本项目 JAV 口语 ASR 转写）、低成本高上限留一席实测；④ Gemma 4 系全砍（中文弱 / OS-Software QAT-heretic 变体底座未变、档位 IQ2_S 级、模型卡自曝复读循环）；⑤ 新候选 enet45/qwen3-8b-ja2zh-v1.1（lora/merged/gguf 三仓库为同一模型三格式）：底座 Qwen3-8B-Base（2025-04 代，隔 Qwen3.6/3.8 两代）、训练语料与 SFT 提示词模板均未披露、GGUF 269 下载/2 赞社区验证极少、8B Q4≈5GB 全场最快。

**三、原条件修订**：
- 条件②修订——IQ2_M 测量席取消（存在前提"内存只容 IQ2_M"已消失），**IQ2_M 投产否决显式保留**（取消的仅是测量席，回退链最低档仍禁 IQ2_M）；
- 条件⑥作废——thinking 已可模型级/服务器级关闭，"透传口备用工单"整体关停；
- 条件⑦升级——E3 协议追加逐候选吞吐指标（tok/s、有效 token/分钟，同卡同并发、LM Studio 实测），随 E3 报告产出"席2/席3/席4 三行全库重跑耗时区间估算"，批次 E 耗时预算由实测重算；全库规模（SRT 文件数/总行数）列为批次 E 计划输入，由用户侧目录盘点提供；
- 条件④增强——席1 型号未入库前，E3 不得出具"现模型达标/不达标"基线结论（无型号即不可复现、不可归因）。

**四、E3 名单定稿（≤4 席）**：
- 席1 现双模型（基线对照，型号待用户补记后入档）；
- 席2 Qwen3.8-27B Uncensored IQ3_S/IQ3_M（heretic 优先，存在性为前置确认项；无则 HauhauCS Aggressive 过语义黄金集+乱码强译探针后作最后手段；unsloth 官方原版仅作对照不投产）——质量主力；
- 席3 Sakura-14B-Qwen3-v1.5（**Q5_K_M 主测单档**，全显存；微弱落败时加测原生 glossary 提示词格式维持原口径）——领域对照；
- 席4 Qwen3.6-35B-A3B heretic IQ3（expert offload 至 64G 内存，**转正必测**）——速度/平衡轴。

**五、enet45 取舍**：不进正式名单。理由：① 训练语料与提示词模板双重不透明（与 JonathanColetti"来源不明弃用"先例一致，独立成立，为排除支柱）；② 同赛道被席3 全面占优（底座代数/规模/社区验证/glossary 支持）——"8B 容量治不了核心病因"为假设性论据 [UNVERIFIABLE]，不构成排除支柱亦不撤销排除；③ 吞吐优势不构成破例（瓶颈是质量不是速度；真实反事实是席4 而非 8B）。名单外 10 分钟自测照录：复用"领域席预筛 scorecard"（8-12 条 JAV 口语特征样本，与 golden_v1/semantic_v1 黄金集**不相交**防判决污染）、结果记录、不突变名单、不投产、不入胜出裁决；**即便 surprise 通过，投产仍须语料披露 + 完整 E3 四探针**。席3 若领域胜出仅说明"Qwen3 系 ja2zh 领域适配在本域有效"，**不触发 enet45 复评**（范围防滑）。

**六、decision-critic 异议记录与主模型最终决定**：
- [HIGH_RISK_OBJECTION]（一条）：席4 可选化与"速度轴已覆盖"论证自依赖 + 回退链第二档无实测。判定依据：影响 ≥3 个任务（E3 执行面、批次 E 耗时预算、回退链第二档实测、最终投产裁决）；且"席4 是速度轴"本身为未验证假设——35B-A3B expert offload 至 DDR4 受内存带宽约束，端到端吞吐可能低于全载 VRAM 的 dense 27B，须实测后定性。
- **主模型回应：采纳（选项 A——席4 转正必测）**。理由：回退链第二档必须有实测数据；"速度轴"须实证后定性。
- 三项改进条件全部采纳，参数落定：① 席4 转正必测；② E3 追加逐候选吞吐指标 + 三行耗时区间估算，全库规模列为批次 E 计划输入（用户侧盘点）；③ **胜出规则预声明**：质量容忍带 **ε=5%**（指标 1/2/5 相对差 ≤5% 视为平手）；平手且席4 在耗时预算内 → 席4 凭速度胜出；超出容忍带 → 席2 质量优先。**ε 可由用户在 E3 开跑前调整，跑后不得改**（判决契约，防跑后定性）。
- 普通级建议全部采纳（6/6）：席3 Q5_K_M 单档；席1 型号未入库不出具基线结论；enet45 自测契约；席2 显存余量薄（13.5GB+KV q8_0 首跑监控，不足降 KV q6 或改 IQ3_S）；IQ2_M 投产否决显式保留；席3 胜出不触发 enet45 复评。

**七、条件闭环状态**：决策层面已拍板（HRO 采纳，参数全落定），执行面未闭环。已闭环：thinking 端到端验证（√）、硬件核算升级（√）、条件⑥作废（√）、质量容忍带与胜出规则预声明（√）。未闭环：① 现双模型型号记录（E3 前置）；② heretic 存在性补证（席2/席4 前置，无则落 HauhauCS gate / 驳回重选）；③ E3 TM-off 实测（四探针 + 拒答 + 语义黄金集 + 吞吐/耗时估算）；④ 批次 E 全库规模盘点。

**八、是否 [PRESSURE-OVERRIDE]**：否。

**九、后续风险跟踪**：① 席4 offload 吞吐可能低于全载 27B，不得预判，以 E3 实测为准；② 全库规模确认前批次 E 耗时为开放量（TM 清洗后首轮命中≈0 ⇒ 近似全量重译），估算进 E3 报告；③ 席3 若在指标上同时压过席2/席4（领域对照反超）——胜出规则仅声明于席2 vs 席4，届时由主模型在 E3 现场作一次明确从席裁决（Sakura 若投产须过 E2 8 项验收 + 语域样本复核），预先知会用户；④ 批次 E 胜出者未过 8 项验收时，回退链第二档已由席4 实测补齐（本续评主要增益）；⑤ 席2 首跑监控 nvidia-smi/LM Studio 显存，不足降 KV q6 或改 IQ3_S；⑥ 现型号与换型后型号一并补记本文档空缺（D2026-0917-02 原风险跟踪⑤续办）。

**十、决策日志字段**：原决策＝D2026-0917-02 之 E3 名单定稿续评（R1）；decision-critic 异议＝[HIGH_RISK_OBJECTION] 一条（附三条件）+ 普通级建议六项；主模型最终决定＝采纳（HRO 选项 A 席4 转正；三条件全落定含 ε=5% 预声明；普通级 6/6 采纳）；条件闭环＝决策层面闭环，执行面待 E3 实测、型号记录、heretic 补证、全库规模盘点；[PRESSURE-OVERRIDE]＝否；风险跟踪见第九节，其中③（席3 反超的现场裁决）与⑥（型号补记）为新增项。

## [2026-09-17] D2026-0917-03 上游转录配置选型·WhisperJAV v1.9.2（三档固定 + Qwen 试点）[已拍板·待执行验证]

> **注（同日）**：本条目已被 **D2026-0917-03-R1 并轨修订**（主力改判两遍 ensemble、B 档降级吞吐车道、aggressive 分派原则、qwen 段切器修正为 firered-vad），本条目保留为历史记录，现行配置以 R1 条目为准。修订缘起：原条目遗漏官方默认面（v1.9.2 README 默认两遍组合），且 #374 评论面证据抓取不全（内容过滤器拦截），经用户质询后重新取证续评。

**一、决策问题**：为 SubTransJAV 下游（闸门0/处置报告/回捞/黄金集）选定上游 WhisperJAV v1.9.2（本地 D:\whisperJAV）转录提取字幕的固定配置搭配，并裁决 Qwen 路线是否可直接定为日常主力。用户硬件已确认为 RTX 5060 Ti 16GB + 64GB DDR4（Blackwell 代，v1.9.2 已适配 int8_float16，出处 D2026-0917-02 条目）。

**二、已核实事实**（decision-critic 独立只读核验，均一致）：
- large-v3→large-v2 回退注释与 `model_id = "large-v2"`（config/components/asr/faster_whisper.py:211-223）；温度收紧 [0.0] 无重试（faster_whisper.py:339，"accuracy is a wash"、回退主因是 JAV 连续能量非语音内容的病理空输出，而非普遍准确度差——#374 用户"v3 准确度差"无独立佐证，属单例经验，不构成选型支柱，只与回退方向一致）。
- `--qwen-timestamp-mode` 运行时默认 vad_only（main.py:801-806）；vad_only 时 0.6B 对齐器不加载（qwen_pipeline.py:544-546，G1 fix）；对齐器走分相独占显存路径（modules/qwen_asr.py:1093-1113，约 1.2GB，切换代价为二遍解码+模型换载）。
- 新事实修正：qwen 管线 max_group_duration 默认实为 **3.0s / chunk 0.3s**（qwen_pipeline.py:121，v1.9.0 JAV retune），6.0 仅 cohere 分支（qwen_pipeline.py:343-346）——原材料"默认 4.0s"不成立。
- balanced 拒绝 `--speech-segmenter`（main.py:2094-2108）；`--fail-on` 合法值 `("empty","suspect")`（utils/run_outcome.py:79）；20 分钟模型刷新默认且**仅覆盖 Balanced/Fidelity**（main.py:529-536，#394=同实例退化，缓解而非根因修复）；semantic 场景检测为全管线新默认（config/segmenter_presets.py:126，MFCC+聚类无 VRAM 成本，留 auditok 回退）；fidelity 默认 FireRedVAD（segmenter_presets.py:98）；GUI 内置组合：qwen pass1 whisperseg+balanced / pass2 ten+balanced、anime pass1 semantic+whisperseg+aggressive / pass2 balanced、Whisper 系一律 aggressive（assets/app.js 约 1840-1925）；qwen_guide.html 为 v1.8.5（:995，Silero v6.2 段已陈旧；Aligner+VAD Fallback 与 ecosystem/model YAML 一致，与 CLI 运行时默认相悖）。
- 下游耦合面（INFO_GAP 闭环）：无调用上游 CLI 的自动化入口，仅消费产物 SRT + 旁车 whisperjav_run.json（subtransjav/refine/cli.py:37-38 可选传入/自动发现；pipeline_v2.py:1403 H4a 先于闸门0 加载；asr_meta.py:32-35 候选键容忍解析、读取异常降级 present=False 不崩溃）——v1.9.2 CLI 破坏性变更对下游零迁移风险；闸门0 已消费 v1.9.2 新增 MILEAGE（mileage_pct 低覆盖率告警）。
- 证据补全：issue #374 URL=https://github.com/meizhong986/WhisperJAV/issues/374（测评主体为帖子正文，作者 weifu8435，2026-06-24；5688373787 为仓库作者 meizhong986 2026-09-15 致谢评论，无新增技术数据）。黄金集 AB 指标脚本复用现有 tests/test_golden_set.py + semantic_v1 黄金集机制，四指标对照表由执行轮产出。

**三、拍板内容（四档定位 + 配套原则）**：
- **A 档 质量优先（Whisper 路线，低频/高价值片源）**：`--mode fidelity --model large-v2`（FireRedVAD 默认、semantic 场景）。接受慢；16GB 卡较 #374 用户（8GB 笔记本 1h→1-2h）应更快，速度以实跑为准。
- **B 档 均衡日常（日常主力）**：`--mode balanced --model large-v2`，vad-version 4.0、semantic 场景默认不动，加 `--fail-on empty` 兜 #394。
- **C 档 Qwen 试点车道（非主力）**：`--mode qwen`（1.7B），sensitivity=balanced 起步、漏线告警再按文件升 aggressive，framer=vad-grouped、regroup=off、postprocess=high_moan、repetition-penalty 1.1、token-budget 20、timestamp 默认 vad_only、max_group_duration 待黄金集 AB 后定（暂不上 6.0）。**晋升条件：黄金集 AB（B/C/D 三档对照）四指标（CER/漏线率/幻觉行占比/闸门0 拦截率）对 B 档全面占优或打平**；期间文档明示"C 档为试点"。
- **D 档 两遍 ensemble（覆盖优先/高价值片源）**：qwen pass1 + balanced pass2（GUI 内置组合）；anime 向内容 anime-whisper pass1 + balanced pass2。
- **配套原则**：enhancer 默认 none（噪音明显再 ffmpeg-dsp/zipenhancer）；模型统一 large-v2 系，不上 large-v3/turbo；qwen_guide.html 仅作参数语义参考、不照抄推荐值；**全档位 sensitivity=balanced 起步**，不设全局 aggressive 默认（GUI 内置"Whisper 系一律 aggressive"属纯上游单发默认，不适用于本下游搭配，沿用 GUI 组合时须按本决策覆写）；下游宁可多召回不漏线，误报成本以闸门0 拦截率/处置报告异常为回退信号。

**四、decision-critic 异议记录与主模型最终决定**：
- **[HIGH_RISK_OBJECTION-1]（C 档直接定主力缺验收闸门）→ 主模型：采纳**。影响 ≥3 个任务（闸门0 输入、翻译、回捞、黄金集验收全链路），qwen 管线社区验证度最低（#374 证据走 large-v2 路线）、v1.9.2 仍在密集修 qwen 补丁、长视频无同实例退化遏制，且黄金集 AB 条件已具备却未设为晋升前提。B 档为日常主力，C 档试点车道，晋升按第三条 AB 条件，文档明示试点状态。
- **普通级修正（3 项）全部采纳**：
  1. timestamp 取舍：默认维持 vad_only；aligner+vad_fallback 为**验收驱动选配**（黄金集 10 部实测时间戳偏移分布与耗时后再定）；代价修正为约 1.5-2x 时长（分相对齐解码+模型换载），非 +2GB 常驻。
  2. max_group_duration：基线修正为 pipeline 默认 3.0s（qwen_pipeline.py:121，v1.9.0 JAV retune）；未经黄金集 AB（3.0 vs 6.0，监控单组失败率）不直接上 6.0；"6s 更完整"方向成立但须实证，不得只引 YAML 注释。
  3. 宁多召回分工：分层成立，"宁多召回"不落全局 aggressive 默认；全档位 balanced 起步、黄金集命中率/漏线告警跌破阈值按文件升级 aggressive，升级后以闸门0 拦截率/处置报告异常为回退信号。
- **遗漏风险清单全部采纳**，其中两项因硬件事实更新调整：FireRedVAD / qwen1.7B+aligner 同驻显存风险权重**下调**（16GB 卡 + Blackwell int8_float16 适配）；下述四项照旧记录：① 20min 模型刷新仅覆盖 Balanced/Fidelity，qwen 长视频无同实例退化遏制，须 mileage 覆盖率监控兜底；② semantic 场景检测新默认回归面，留 auditok 回退 + 黄金集至少一轮新旧对比；③ manifest 候选键命中比对（升级后首轮，防 asr_meta 静默降级 present=False）；④ qwen aggressive 预设 batch=16+max_new_tokens=8192 属高载，谨慎禁用提示入文档/脚本。

**五、条件闭环状态**：决策层面已拍板（HRO 采纳、修正与参数全部落定），执行面未闭环。已闭环：硬件事实更新（√，16GB+Blackwell）、下游耦合面核实（√，零迁移风险）、#374 证据补全（√）、黄金集 AB 复用路径（√，tests/test_golden_set.py + semantic_v1）、max_group_duration 基线修正（√，3.0s）。未闭环：① B/C/D 三档四指标 AB；② timestamp aligner 10 部实测（偏移分布+耗时）；③ max_group_duration 3.0 vs 6.0 AB + 单组失败率；④ 升级后 manifest 候选键命中比对（首轮）；⑤ semantic 新旧场景检测对比（至少一轮）；⑥ qwen 长视频 mileage 监控窗口建立。

**六、是否 [PRESSURE-OVERRIDE]**：否。

**七、后续风险跟踪**：① 四指标 AB 未出前，C 档保持试点，任何文档不得标注主力；② AB 平局判定口径未定——建议沿用 D2026-0917-02-R1 的 ε=5% 容忍带先例，由执行轮**跑前预声明、跑后不得改**（判决契约，此项待执行轮敲定，本决策未落死）；③ qwen 长视频（≥1h）异常以 mileage 覆盖率 + 单组失败率兜底，异常即该文件回退 B 档重跑；④ manifest schema 键名漂移为静默通道，升级后首轮必须人工比对 candidate keys 命中与 MILEAGE 值域；⑤ aligner 选配"为开而开"风险，未过 10 部实测不启用；⑥ GUI 内置 aggressive（Whisper 系/anime pass1）与本决策 balanced 起步原则冲突，D 档沿用 GUI 组合时须按原则覆写并记录；⑦ qwen aggressive 预设高载（batch=16+8192 tokens），文档/脚本显式禁用提示。

**八、分工**：用户侧——黄金集 AB 执行与四指标验收、10 部 timestamp 实测、升级后首轮 manifest 人工比对。执行侧——四档 CLI 配置固化进脚本/文档、AB 对照表产出、semantic 对比轮、mileage 监控窗口落地。代码侧——本决策不改代码，仅配置与文档；如 AB 暴露需要新参数（如 qwen 侧刷新机制），另立工单。

**九、决策日志字段**：原决策＝D2026-0917-03 新立（上游转录配置选型，非续评，硬件事实承接 D2026-0917-02）；decision-critic 异议＝[HIGH_RISK_OBJECTION-1] 一条（C 档主力缺验收闸门）+ 普通级修正三项（timestamp / max_group_duration / 宁多召回）+ 遗漏风险清单；主模型最终决定＝全部采纳（B 档主力、C 档试点、AB 晋升条件、参数修正落定）；条件闭环＝决策层面闭环，执行面待 AB 四指标、timestamp 10 部实测、3.0 vs 6.0 AB、manifest 首轮比对、semantic 对比轮、mileage 监控窗口；[PRESSURE-OVERRIDE]＝否；风险跟踪见第七节，其中②（AB 平局口径预声明）为执行轮待决新增项。

## [2026-09-17] D2026-0917-03-R1 上游转录配置选型·WhisperJAV v1.9.2（R1 续评：主力改判两遍 ensemble）[已拍板·待 AB 执行]

**一、续评依据**：基于 D2026-0917-03 原条目（B 档主力 / C 档试点 / balanced 起步 / qwen 默认 whisperseg）续评。新证据 A（官方 v1.9.2 README，本地权威面 whisperjav-1.9.2.dist-info/METADATA + 作者 8-29 评论 #5463389888）+ 证据 B（#374 全部 55 条评论，weifu8435 9 月实测）。**原条目三档结构被本续评并轨修订，原条目保留为历史记录**。

**二、新旧事实核验**：
- 证据 A 已核验（METADATA:265/:344/:355/:370-372/:420/:428-434）——官方默认两遍 = anime-whisper·WhisperSeg·aggressive / Qwen3-ASR·TEN；aggressive 为基准调优目标；balanced 官方自评粗时间戳 + 弱 run-outcome 检查；最佳 = ensemble（anime-whisper + qwen）；内容分派表含 ASMR→fidelity/anime-whisper aggressive、重 BGM→balanced conservative；whisper-ja-1.5B 场景基准最强（不进本期）；Qwen finetune 双通道（QA-Galgame CER−27% rel / neosophie，:358-359）。
- 证据 B 复核升级：55 条评论已由主模型经 GitHub API 全文拉取并本地存档 `D:\SubTransJAV\.tmp374\comments.json`（+body.json）。decision-critic 侧抽查关键 7 条 ID 核验一致：5463389888（作者，8-29，#394 属真实 bug、1.9.0 仍复现）、5579871700（9-08，fidelity 新版快约一倍、semantic 场景、large-v2 幻觉经词表消除）、5585938271（9-08，Qwen3-ASR+TEN 亦触发短字幕时间过长，**只能配 FireRedVAD**）、5604217223（9-09，方案一 anime-whisper+FireRedVAD / large-v2 vs 方案二对比，准确度方案一稍优、覆盖方案二更优）、5610000509（9-09，暂定最终配置=方案二、弃 anime-whisper 原由：语气词过多；qwen"所有模型里字幕最全"但碎且短字幕轴拖尾）、5614392021（9-10，large-v3-turbo 后段时间轴偏移被否；whisperseg 捡漏最强但语气词偏多）、5614575021（9-10，large-v2 原生词级时间戳护城河长评）。
- **归因疑点（成立，列为 AB 第一轮前置）**：行碎机制被指为"Assembly 对齐器拆 VAD 块 2-4 行"，与 aligner=on 纠缠；1.9.x 默认 vad_only 不加载对齐器（main.py:801-806；qwen_pipeline.py:544-546 G1 fix）——"必须 FireRedVAD"对 1.9.2 默认前提的可迁移性须实测归因。
- 代码可表达性已验：qwen 段切器选项含 firered-vad（main.py:729-738）；fidelity ensemble pass 官方注记亦用 firered-vad（main.py:454-466）。

**三、拍板内容（方向性默认，即时生效）**：
1. **主力 = 两遍 ensemble（pass1_primary）**：pass1 = anime-whisper · semantic · WhisperSeg · aggressive；pass2 = Qwen3-ASR 1.7B · semantic · aggressive · **段切器默认 firered-vad（TEN 保留为 AB 对照席）**；ASMR/耳语向片源 pass1 换 fidelity + large-v2；weifu 方案（fidelity+large-v2 / qwen+FireRedVAD）列为 AB 对照席，不直接照搬。原 D 档并入主力框架，不再单列。
2. **B 档（balanced + large-v2 + --fail-on empty + MILEAGE 遥测）降级为快速吞吐车道**（批量/低值片/赶量场景）。
3. **C 档单 qwen 试点**：配置修正为 firered-vad + aggressive，带归因 AB 后再议去留。
4. **sensitivity 原则**：原"全档位 balanced 起步"作废；**aggressive 为 JAV 对话/ASMR 默认**，重 BGM 向 **conservative**，闸门0 拦截率/处置报告异常为回退信号。
5. **模型族锁定扩展为三族**：anime-whisper / qwen3 / large-v2 系，均不进 large-v3 / v3-turbo；whisper-ja-1.5B 与 qwen finetune 双通道记录为后续升级通道，不入本期名单。

**四、decision-critic 异议记录与主模型最终决定**：
- **[HIGH_RISK_OBJECTION-1-R1]**（三参数写死前置 AB 门槛：qwen 段切器弃 TEN / aggressive 全量固定 / pass1 单型定死）→ **主模型：采纳**。形态 = 方向性默认 + AB 门槛闭环：三条参数（① qwen 段切器 firered-vad vs TEN 最终归属；② aggressive/conservative 内容分派边界；③ pass1 单型归属 anime-whisper vs fidelity+large-v2）以 AB 产出为准锁定；ε=5% 容忍带按先例跑前预声明、跑后不得改。异议②已含于修订案"重 BGM 向 conservative"，视为已满足。
- **普通级修订前四项**（主力改判 / B 档降级 / aggressive 分派 / C 档段切器）→ 全部采纳，拍板如上。
- **AB 前置清单（执行轮顺序）**：第一轮——行碎归因矩阵（段切器 firered-vad/TEN/WhisperSeg × 时间戳模式 vad_only/aligner_vad_fallback × 行碎度/成品短行占比/时间轴偏移）+ ensemble 空 pass 兜底行为确认（merge pass1_primary 语义，ensemble/merge.py:338-386，+ --fail-on empty 在 ensemble 下行为）；第二轮——六指标决胜（原四指标 CER/漏线率/幻觉行占比/闸门0 拦截率 + 行碎度/短行占比 + 时间轴偏移分布），pass1 双型与段切器归属据此锁定。

**五、条件闭环状态**：决策层面已拍板（HRO 采纳、方向性默认生效、AB 前置清单定序），执行面未闭环。已闭环：新证据核验（官方默认面 METADATA √、证据 B 存档复核 √、qwen×firered-vad 可表达性 √、下游行合并吸收面 √（config.py:27 断句预合并 <6 字符碎片，闸门0 在预合并前逐行运作 config.py:255，故行碎影响有限但非零））。未闭环：① 行碎归因矩阵（AB 第一轮）；② ensemble 空 pass 兜底确认（AB 第一轮）；③ pass1 双型与段切器归属决胜（AB 第二轮六指标）；④ 六指标 AB 实跑；⑤ 升级后 manifest 键名比对（首轮）；⑥ qwen 长视频 mileage 监控窗口（qwen 升为 pass2 常规组件后为必配）。

**六、是否 [PRESSURE-OVERRIDE]**：否。

**七、后续风险跟踪**：① 行碎归因未决前，不得以"FireRedVAD 唯一可用"写死文档（与官方默认 TEN 的分叉以 AB 裁决，不选边）；② ensemble 空 pass 兜底未确认前不得以主力身份批量投产；③ qwen 为 pass2 常规组件后 20min 模型刷新（main.py:529-536）不覆盖 qwen 的隐患升级——mileage 覆盖率监控为必配（下游 asr_meta 已消费 mileage_pct，链路可用）；④ #394 对降级后 B 档仍为知情风险车道，--fail-on empty + 遥测悬挂；⑤ 误报反噬转监控项：闸门0 拦截率/处置报告异常为 aggressive 回退信号；⑥ whisper-ja-1.5B 与 qwen finetune 双通道记录不入本期；⑦ semantic 新旧场景对比轮、manifest 键名比对、qwen aggressive 预设高载（batch=16+8192 tokens）禁用提示照旧；⑧ 证据存档位于临时目录 `.tmp374`，如需长期留存建议迁移至持久目录（如 docs/evidence/）并连同 golden 集指纹一并记录。

**八、分工**：用户侧——AB 第一/二轮执行与六指标验收、空 pass 兜底验证、manifest 首轮比对、mileage 监控窗口确认。执行侧——方向性默认参数入库（文档口径"方向性默认 + 待 AB 裁决"）、AB 矩阵脚本、ε=5% 跑前预声明落地、AB 完成后条目状态转"已执行"并补记裁决结果。代码侧——不改代码；如 AB 暴露 ensemble 空 pass 无兜底或 qwen 长视频退化属实，另立工单。

**九、决策日志字段**：原决策＝D2026-0917-03（R1 续评，原条目三档结构并轨修订）；decision-critic 异议＝[HIGH_RISK_OBJECTION-1-R1] 一条（三参数写死前置 AB 门槛）+ 普通级修订四项；主模型最终决定＝采纳（方向性默认 + AB 门槛闭环：六指标 + 行碎度 + 时间轴偏移，ε=5% 跑前预声明、跑后不得改）；条件闭环＝决策层面闭环、执行面待 AB 两轮 + 六指标 + manifest 首轮比对 + mileage 监控窗口（见第五/七节）；[PRESSURE-OVERRIDE]＝否；后续风险跟踪见第七节，其中②空 pass 兜底与①行碎归因为 AB 第一轮前置项，⑧证据存档迁移为新增建议项。

## [2026-09-18] D2026-0917-03-R1-E1 执行补记·结构性轮 AB 完成 [已执行·结构性轮完成]

**一、执行概况**：D2026-0917-03-R1 拍板执行面第一/二轮前置项落地。4 部影片（mihd002/mikr082/ure125/start422，122-148 分钟，含难片 mikr082）× 6 配置格 = 24/24 成功（首跑 Q2__mihd002 对齐器模型下载网络瞬断 rc=1，已重跑成功；队列 09-17 22:25 起至 09-18 05:10，Q2 重跑 05:26 完成）。数据位置：D:\SubTransJAV\.abtest\results.md（指标表）、metrics.py/run_queue.sh/logs/progress.log/out 同目录；#374 评论证据存档 D:\SubTransJAV\.tmp374\。

**二、四片均值对照**（速度=min/部实测；覆盖%=字幕去重占时/片长；重复%=同文本≥3 次行占比，幻觉代理非判定；碎<6% 与下游 DEFAULT_PREMERGE_MIN_FRAGMENT_CHARS 同口径；全部格子 state=done、无空输出，官方 manifest span 99.7-99.8）：

| 配置 | 行数 | 覆盖% | 均字 | 碎<6% | 重复% | 连重 | min/部 |
|---|---|---|---|---|---|---|---|
| ENS合并(anime+qwen) | 1164 | 40.9 | 11.2 | 17.8 | 9.5 | **0.0** | 20.5 |
| 　pass1 anime-whisper | 896 | 32.5 | 12.4 | 9.9 | 1.4 | 0.2 | 10.0 |
| 　pass2 qwen+FireRed | 1204 | 41.6 | 10.9 | 21.6 | 14.1 | 16.0 | 17.0 |
| qwen+FireRed+vadonly(Q1) | 1204 | 41.7 | 10.9 | 21.6 | 14.1 | 16.0 | 13.5 |
| qwen+FireRed+aligner(Q2) | 1238 | 29.3 | 10.9 | 20.8 | 14.7 | 14.2 | 22.0 |
| qwen+TEN+vadonly(Q3) | 1284 | 50.2 | 10.4 | 20.6 | 14.9 | 16.2 | 14.0 |
| qwen+WhisperSeg(Q4) | 1056 | 39.7 | 11.6 | 16.6 | 5.9 | 3.2 | 13.5 |
| balanced+large-v2(BAL) | 1018 | 24.8 | 9.6 | 25.9 | 3.5 | 2.2 | 5.4 |

关键单格：难片 mikr082——ENS 覆盖 24.8 vs BAL 14.9；anime pass1 396 行 vs qwen pass2 703 行（−44% 漏线互证）；qwen+WhisperSeg 572 行 vs qwen+TEN 776 行（−26%）。

**三、结构性裁决（decision-critic 立场标注）**：
1. ENS 主力+结构性验证通过（连重 0 全场唯一、覆盖 +65%、重复率较 pass2 单跑降 1/3、行数不缩水）；BAL 4x 速度 → 吞吐车道分工成立。**限注：结构性验证非完整六指标；BAL #394 n=4 未复现≠排除，车道维持知情风险标记 + --fail-on empty/遥测**。
2. 行碎归因定案（修正 weifu 归因）：vad_only vs aligner 碎行 21.6→20.8 几乎不变 → 行碎源于帧原生分组粒度、与对齐器无关；aligner 效应为时间轴收紧（均长 2.79→1.91s、覆盖 −12pp）非拆行；碎行治理方向 = 下游预合并/分行。
3. 段切器三参数①裁决：**FireRed 保留方向性默认；TEN/WhisperSeg 升格为合法候选；归属最终由人耳时间轴抽检定夺**。"FireRed 唯一可用"与"TEN 灾难"均否证（TEN max_dur 仅 +0.3-1.5s）；TEN 高重复率 14.1-14.9 由下游闸门0 吸收，其覆盖 50.2 是补漏正面证据。
4. pass1 三参数③：缺省 anime-whisper + qwen 补漏维持（anime 重复 1.4%/碎行 9.9% 全场最优、漏线 −44% 与官方"misses faint utterances"及 weifu"qwen 最全"互证、ENS 合并取长补短连重 0）。**缺口记录：ASMR/耳语向 "pass1 换 fidelity+large-v2" 本轮未测（BAL≠fidelity），保持"未验证方向性默认"（待办 d，非阻塞）**。
5. sensitivity 三参数②：aggressive 默认原则维持，conservative 边界标注"原则性、未实测"。
6. 空 pass 兜底：非阻塞，但未自然触发≠已验证；兜底语义维持代码审读（merge pass1_primary）+ 运行时 --fail-on empty/遥测常态化监控。

**四、AB 口径修订声明（主模型已追认，见十）**：原"六指标决胜 + ε=5% 判平、跑后不得改"契约中，CER/漏线率/时间轴偏移因无 ground truth 不可测。修订为：六指标 = 结构性三项（已测）+ 人耳时间轴抽检（待办 a）+ 闸门0 拦截率对照（待办 b）；CER/漏线率降级为按需；ε=5% 仅适用于可测指标。

**五、遗留待办**：a) 人耳抽检 4 部 × {Q1/Q3/Q4/ENS} 各 3 段（段切器归属 + aligner 取舍唯一决定性证据；协议随机化 + 含 mikr082 段 + 必查 Q2 max_dur 长尾 10.6-11.4s 异常）；b) ENS/Q1/BAL 三路 SRT 喂下游跑闸门0 拦截率对照；c) 黄金集标注轮降级按需（触发条件见十）；d) fidelity+large-v2 pass1 补测（按需级）；e) conservative 格补测（重 BGM 片型若存在）；f) MILEAGE/覆盖率告警口径与 aligner 覆盖收缩联动核对。

**六、运维事实**：CLI 需将 D:\whisperJAV\Library\bin 入 PATH（GUI 专属预设）；对齐器模型下载遇网络瞬断可致整格失败需重跑（progress.log 支持断点续跑）；5060 Ti 16GB 实测速度——qwen 系 13-22 min/部、ENS 20.5、BAL 5.4。

**七、条件闭环状态**：已闭环——行碎归因矩阵（定案）、段切器结构面（三候选）、pass1 结构性证据、ensemble 主力结构性验证、BAL 吞吐分工。未闭环——人耳时间轴抽检（a）、闸门0 拦截率对照（b）、fidelity+large-v2 pass1 补测（d）、conservative 边界（e）、aligner 取舍终定（随 a）、升级后 manifest 键名比对（沿用 R1 待办）。

**八、后续风险跟踪**：① Q2 aligner max_dur 长尾异常（8.95-11.4s）未解释，人耳轮必查；② aligner 覆盖收缩 × 下游覆盖率告警口径联动，可致系统性误报（待办 f）；③ BAL #394 不因 n=4 排除，持续遥测；④ 段切器归属未定前 FireRed 为默认、TEN/WhisperSeg 合法，文档维持"待抽检"措辞；⑤ 人耳抽检样本量小（12 段），结果只作择优不作全否；⑥ .tmp374 与 .abtest 证据路径临时存放，保留策略由用户定（建议迁 docs/evidence/ 或定期清理）；⑦ 维持：qwen 长视频 mileage 监控必配、qwen aggressive 预设高载禁用、semantic 新旧对比轮。

**九、决策日志字段**：原决策＝D2026-0917-03 / R1（执行面 E1 补记）；decision-critic 立场＝结构性裁决六项全部支持（含限注与缺口记录）+ 新增 AB 口径修订声明与拟补待办 d/e/f；主模型最终决定＝结构性裁决定案 + 口径修订追认（见十）；条件闭环＝部分闭环（人耳/闸门0/补测项未闭环，见七）；[PRESSURE-OVERRIDE]＝否。

**十、主模型追认（2026-09-18）**：
1. **采纳 AB 口径修订声明全文**（第四节），ε=5% 仅适用于可测指标（结构性三项与闸门0 拦截率）；本次修订系"跑前契约中的指标在实测面不可得"的客观修订，非跑后改判，特此入档。
2. **待办 c（黄金集标注轮）触发条件**（满足任一即启动）：① 用户报告某片型系统性质量差；② 人耳抽检中漏线普遍（≥30% 抽检段存在明显漏线）；③ 闸门0 对照中某配置格拦截率 >25% 且显著高于其余格。
3. **待办排期**：a 人耳抽检需用户执行（本补记归档同时交付抽检清单）；b 闸门0 拦截率对照由执行侧排期为下一步任务；d/e/f 按需。
4. 风险跟踪①②⑤⑥照单全收；.abtest/.tmp374 暂保留原位待用户定保留策略。

**【2026-09-18 审查排序注记（两遍组合前三，供用户全面审查）】** 经与 decision-critic 讨论（其评议：有条件支持，五项修正全部采纳）达成共识：前三组合 = ① **anime-whisper(WhisperSeg) + qwen(TEN)**（官方拍档）② **anime-whisper + qwen(FireRed)**（=ENS 已测集成，锚点）③ **BAL(large-v2) + qwen(TEN)**（词级时间轴测试席）；另免费物化第 4 格 anime-whisper + qwen(WhisperSeg) 备席。采纳方法修正：取消 GPU 补跑，改用上游 MergeEngine.merge(pass1_primary) 零 GPU 离线物化（.abtest/materialize.py）；**方法自检通过**（离线 anime+FireRed 与已测 ENS 集成行数 1141/670/1427/1420 vs 1141/670/1426/1420，±1，离线≡集成）。组合实测四片均值：**C1 覆盖 43.5%/重复 10.9%/碎 18.2%/连重 0**；**C3 覆盖 36.0%/碎 29.5%/重复 11.5%/连重 1.0**（结构面弱）；**C4 覆盖 37.7%/碎 15.3%/重复 2.9%/连重 0**（结构面全面优于 C3，C3 席位理由仅剩词级时间轴待耳证）。审查配对：#1 vs #2 隔离 pass2 段切器（TEN/FireRed），#1 vs #3 隔离 pass1 体系（anime/BAL）；四类必抽段 = 难片(mikr082 必抽)/安静环境音/pass 交替边界/密集对话（はい・うん高频区），同窗跨格并行播放。GPU 仅留最终胜者集成新鲜重跑出生产签名件。待办 b（闸门0 拦截率对照）扩展至前三格合并产物；Q2 max_dur 长尾（8.95-11.4s）在耳审轮保 1 段闭合或显式关闭。

## [2026-09-19] D2026-0917-03-R1-E2 终态补记·F06 定版与实弹验证 [已执行]

**一、用户终选与推理（2026-09-19）**：用户终选 **F06（wseg×ten = pass1 Qwen3-ASR-1.7B+WhisperSeg / pass2 Qwen3-ASR-1.7B+TEN，pass1_primary）为上游默认搭配**。用户推理：下游可吸收重复噪声（本项目闸门0+预合并），覆盖不可恢复 → F06 反超 F02。终选与 E1-R1 既有裁定（F06 默认首推）同向，非推翻裁决；F02/F01 降为备选，bal 四格（F04/F07/F08/F09）维持出局、F10 维持不推荐。

**二、待办 b 实弹（gate0_probe.py，2026-09-19）**：复用项目 apply_source_filter/RefineConfig/parse_srt，与 pipeline_v2.py 真实调用同构，default 档 valve=50%。输入 Σ：F06=4821 行、F02=4785 行。删除：F06 4/4821（0.08%）vs F02 4/4785（0.08%），**唯一删除=mihd002 各 4 条「うん」连打行**；8 文件零 valve/tighten 触发。计数类检出 F06 反而更低：孤立应答词 197 vs 217、无意义音节连缀 23 vs 26。**结论："F06 行噪声可被下游吸收"证实，F06 反超成立（净 +36 行真实内容 @ 零额外删除成本）**。注：探针双跑导致 dropped_entries.log 内删除条目各重复追加一遍（共 16 行），计数口径按单跑 4 条/组合。

**三、生产签名件（2026-09-19 01:11-06:33）**：F06 真实集成 GPU 重跑 4/4 done，rc=0（.abtest\prod\F06__*\）。whisperjav_run.json：行数 1133/712/1457/1515（mihd002/mikr082/ure125/start422），与离线物化各 -1；span 99.8%/99.7%/99.7%/99.7%；耗时 43.4/61.7/117.5/98.9 min，合计约 5.3h。签名件即上游默认搭配权威产物，供下游滚动观测基线。

**四、待办清单销项状态**：待办 b ✅ 已闭环。manifest 键名比对部分完成（files[0] 结构经探针/run.json 验证 ✓；asr_meta 消费面 mileage_pct/tighten 链路留口至真实下游联跑）。待办 a（段切器耳审 12 段）**转为长期惯例项**——用户实际选用 TEN 格已构成行动偏好；风险显式记录：TEN 环境音短句型标记 6/部虽温和但未逐条耳核，日常观测异常时抽检随时启用。待办 d/e/f、semantic 对比轮按需保留。

**五、生产默认配置 CLI（prod_rerun.sh）**：`--ensemble --pass1-pipeline qwen --pass1-sensitivity aggressive --pass1-scene-detector semantic --pass1-speech-segmenter whisperseg --pass2-pipeline qwen --pass2-sensitivity aggressive --pass2-scene-detector semantic --pass2-speech-segmenter ten --merge-strategy pass1_primary`

**六、后续风险跟踪**：① 4 部过拟合滚动观测（日用 mileage/质量报告，新片型异常触发待办 c 条件）；② 按片覆写路径（单部异常可独立切换 F02/F01 备选并回取离线物化件）；③ bal 系留盘（可回取词级时间轴，维持出局结论）；④ 证据目录保留策略由用户定（.abtest、.tmp374、外部点评两 txt）。

**七、decision-critic 核验声明**：转「已执行」条件成立——用户终选（①）、待办 b（③）、生产签名件（④）已闭环；待办 a（②）经用户终选构成行动偏好转为常规惯例项（风险已列）；过拟合监控（⑤）为长期观测项。无 [HIGH_RISK_OBJECTION]，[PRESSURE-OVERRIDE]＝否。

**【2026-09-19 v1.9.3 增补轮注记（E2 后追加，结论不变）】** 上游发布 v1.9.3（主题=语音增强器 bug 修复+翻译/安装体验，未触及 qwen 管线/段切器/合并策略，本决策链报告的五项工程问题均未修复）。升级方式：whisperjav-upgrade 在兼容性检查环节无超时挂死（实测 0 CPU/0 磁盘/0 网络），改用官方 release wheel `--no-deps` 安装 + 清华镜像装 demucs（4.1.0），环境零损伤（torch 2.11.0+cu128/ct2 4.8.1 保持）。双难片增补实测（MIKR-082 稀疏安静型 + URE-125 密集长片型）：① **回归格两片与 v1.9.2 签名件逐项一致（712/27.6%、1457/48.7%）——v1.9.3 零行为漂移，F06 定版结论与全部历史测试数据继续有效**；② 增量格 htdemucs+enhance-for-vad（作者 1.9.3 新首推）：环境音短句 5→1（-80%）、重复率 15.0→11.1（-3.9pp），代价覆盖 48.7→46.2（-2.5pp，-85 行）+耗时 +23%；安静片上无效果无损害。**裁定：F06 定版维持原样（覆盖优先）；htdemucs/enhance-for-vad 记录为"按片开关"（dense 型片求净可开），不改默认**；增补结论已并入上游测评报告第九节。

## [2026-09-18] D2026-0917-03-R1-E1-R1 外部 AI 点评综合裁定轮（10 final 最终推荐排序）[已拍板·待用户终选与待办 a/b]

- **原决策**：在 D2026-0917-03-R1-E1（结构性轮 AB 完成）基础上，综合外部 AI 点评 + 标记词硬数据（结构指标 + 两轮 substring 计数），裁定 10 个 final（F01-F10，pass1_primary 离线物化，锚点回归 16/16 通过）的最终推荐名单与排序，供用户全盘定夺。
- **外部点评的双族幻觉分类学（标记词计数证实）**：**BAL/Whisper 系=内容离谱型串台句**（犬/猫/母親/犯人类，实测集中于含 BAL 的格子：F04=9、F07/F08/F09 各 11-12，与 pass 角色无关=BAL 内容稳定属性）；**TEN 补漏=环境音短句型**（ねえ、何を 类，F02/F06/F08/F10 各 6，wseg 偶产 1）；anime 系"大规模崩溃"指控实测为低密度（F01-F04 各 4=每部 2 处）；纯 Qwen 格与 anime+Qwen 格的"安全句"（あなたは何を…类）**实测全 0**——安全句仅现于 bal 相关格（F07/F08/F09=4、F04=1），归因修订为"**BAL 补漏触发型安全句填充**"而非 Qwen 通用特性。标记总分（不含争议类）：F05=3、F01=4、F03=5、F06=8、F10=8、F02=10、F04=14、F07=20、F09=21、F08=26。
- **外部点评的采纳/驳回**：采纳——双族分类学（修订后）、folder1 选 F06（唯一未被硬数据否证的背书）、互证+语义审读方法论保留为人工/LLM 审读环节、F05/F06/F10 确认怪词。驳回——folder2 选 F07（其核心论据"F07 无自创句"被硬计数反驳：安全句 F07/F08/F09 各 4 而 F06=0；且 F07 在 folder1 有 11 处串台+结构面最弱，按片型分派不采纳）；"anime 大规模崩溃"（低密度）；多处版本归属张冠李戴（おきはなかしい 实在 F07/08/09 非 F03；車性依存症 仅 bal 系）。
- **decision-critic 异议（八项）与主模型决定**：**全部采纳**——①F01"最干净"标签撤回（结构最净=F03 重复 2.9/碎 15.3；标记最净=F05=3；F01 改称"ENS 锚点+零环境音污染"）；②bal 出局扩为四格（含 F04）；③安全句归因修订（见上）；④计数表述改用"最低存证密度"（substring 相关非真值，相对排序可信、绝对密度不可信，含真台词误报风险）；⑤词级时间轴为 BAL 原始 SRT 中间资产（留盘可回取，非 final 属性——合并产物均长 1.75-2.39s 仍为句级，F08 测试席层位错误故移除）；⑥INFO_GAP 闭环：外部审读对象确认为日文源文 SRT（引例均为 final 源文原文行），与我方计数同一产物面；⑦方法论六条记录（真台词误报、词表内生性、N=4、分母口径、等权重、脚本落盘 .abtest/markers.py v1）；⑧三强含 2 个 TEN 格，段切器耳审（待办 a）先于生产默认定版，选 TEN 格≠预投一票。
- **最终推荐排序**：**1. F06（wseg×ten）默认首推**——覆盖 45.0 全场最高（不可恢复维度最大化）、连重 0、标记 8 全温和类、外部唯一幸存背书同向；**2. F02（anime×ten）并列首选**——官方拍档信任面、结构较净（重复 10.9/碎 18.2），代价覆盖 -1.5pp、标记 10 含争议句；**3. F01（anime×fire）**——ENS 锚点、零环境音污染、覆盖 40.9，难片占比高则名次降；**相邻备选 F03（anime×wseg，结构最净）/ F05（wseg×fire，标记最低）**；**明确不推荐：F04/F07/F08/F09（BAL 内容污染，标记 14-26）与 F10（重复 16.4 最噪无补偿轴）**。
- **条件是否已闭环**：决策层面闭环。执行面未闭环——①用户终选（F06/F02/F01 口味取舍）；②待办 a 段切器耳审先于定版；③待办 b 闸门0 拦截率对照扩展至三强先于签名件；④胜者 GPU 新鲜重跑出生产签名件（现三强为离线物化件，仅作评审基线）；⑤4 部过拟合由日用 mileage/质量报告滚动观测+按片覆写路径兜底。
- **是否 [PRESSURE-OVERRIDE]**：否。
- **后续风险跟踪**：① F06 行噪声"可被下游吸收"待待办 b 实弹核对；② bal 串台标记解读须耳审抽 ≤10 条验证（相关非真值）；③ markers.py 复跑偏移已修订归因（C 类=TEN 主导+wseg 偶产，F03/F05/F09 各 +1，排序不变）；④ 词表 v1 归档 .abtest/markers.py，修订须升版留 diff；⑤ 无词级消费者假设失效时 BAL 原始 SRT 留盘回取；⑥ 外部点评两份审读原文（.abtest 前用户置于 abtest_final 的 F06/F07 txt）建议随证据迁移持久路径。

## [2026-09-18] D2026-0918-01 v1.2.2 Beta 特性「剧情自摘要 auto_synopsis」[已拍板·实施完成（待批次E双轨实弹）]

**一、决策问题**：将 per-片手写剧情摘要自动化（auto_synopsis）：闸门0+预合并后、阶段A 前，用阶段A 同一 provider/model 追加 1 次 LLM 调用，对源文采样（synopsis_max_chars 默认 6000）生成 3-5 行中文剧情备忘（人物关系/核心剧情线/场景构成），以批次 C 冻结措辞注入 A/B 两阶段 prompt。用户已批准"翻译提示词功能，可尝试作 beta"；纯提示词内部消耗、与刮削无关、产物不落输出。版本策略与 E 验收口径须裁决。

**二、已核实事实**：
1. sidecar 冻结措辞（pipeline_v2.py:169-171）与 A/B 双注入点（:578-581）存在，自动摘要复用同挂载机制与同措辞；手写【剧情摘要】存在时优先、自动跳过。
2. context_sidecar 入指纹已有先例（manifest.py:369-372），新增 auto_synopsis/synopsis_max_chars 两键进 _CONFIG_FIELDS 无机制障碍。
3. v1.2.2 批次 A-D 完成、866 passed、E 待执行；代码未 commit、未发布。
4. 8 项指标中 バラまく 语境正确 0/5→5/5 为语境级硬伤指标，语境词绝不入 hard 词表（v1.2.2-计划表 §四）——该指标达成的唯一语境通道是本 beta 或手写摘要。
5. 长片 83KB（正文约 2.6 万字），6000 字符采样约覆盖 22%，单窗采样对情节主线覆盖无保证。
6. 净语清洗在阶段A 批内执行（pipeline_v2.py:1089），摘要输入含未净语行；有效指令落盘 refine_v2_A/B.txt 可作审计留痕。

**三、decision-critic 评议结论**：
- **[HIGH_RISK_OBJECTION] 一条**（E 验收混入 beta 变量=循环验收）：beta 开启下指标 2 达成无法区分 v1.2.2 既有修复与 beta 成就；8 指标不覆盖"摘要编造情节"，beta 缺陷可被 E2 全绿掩盖；失败亦归因不清。判定依据：与 D2026-0917-01 批次验收共识冲突 + 影响方案结构。
- **普通级条件六项**：摘要幻觉（非连续片段显式说明+信息不足计数，不加全量复核）；采样升级为时间戳分桶；默认开但须显式关闭通道、E2-A 显式关、cloud 成本不设门槛；缓存不得放 translation_memory（TM 用户数据语义，且用户已在该目录做过人工对账）改独立目录+复合缓存键；补并发原子写/独立 max_tokens/超时/噪声行过滤/日志附 sha1。并入 v1.2.2 工程时点最优，问题在验收口径不在版本号。

**四、主模型拍板（全部采纳）**：
- **HRO → 方案甲双轨 E2**：E2-A 显式关闭 beta 做正式 8 指标验收；指标 2（バラまく）因用户已否决手写剧情摘要如实验收——预期不达标、不宣称达标，顺延由 beta 验证；E2-B 同片开启 beta，仅做 diff 对比 + 指标 2 改善评估 + 摘要质量人工抽读 + 非指标行反向污染复检，改善结果显式声明归属 beta，绝不记 v1.2.2 既有机制头上。
- **条件 1-8 全部采纳**：①双轨 E2；②时间戳分桶均匀采样（6-8 桶×约750字符≈6000 上限）+采样范围入日志+摘要输入跳过纯噪声行；③prompt 增"片段来自整片不同位置抽样、时间不连续、禁止补足跳跃段"+"信息不足"逃生口与计数；④--no-auto-synopsis 显式关闭通道，E2-A 必须显式关闭；⑤缓存独立目录 Temp/synopsis_cache/，键=sha1(采样后输入文本)+provider/model+prompt 版本号，原子写+每键锁；⑥独立 max_tokens（约300）与超时 min(timeout_llm,300s)；⑦手册补 resume/换模型指纹边界，日志行附摘要 sha1；⑧决策日志与 CHANGELOG 标注 Beta，指标 2 归属显式声明。
- **版本策略**：并入 v1.2.2（未 commit 未发布、E 未执行），CHANGELOG 标 Beta；默认开（用户明示尝试）+显式关闭开关；cloud 档跟随阶段A provider/model，成本不设门槛。
- **INFO_GAP 答复**：(a) E2 样例片无手写 sidecar（用户已否决手写剧情摘要）；(b) 现行生产模型型号为 D2026-0917-02 风险⑥悬置项，摘要模型自动跟随 --s1-model，不阻塞本特性。

**五、验收归属声明（写入 E2 验收报告）**：指标 2 的 E2-A 结果如实记录（预期不达标）；E2-B 的改善以名文"该达成由 beta 特性 auto_synopsis 贡献，非 v1.2.2 既有机制成就"标注，二者均不得混记。

**六、后续风险跟踪**：① E2-A 指标 2 不达标不得以任何形式宣称 v1.2.2 语境修复达标；② beta 反向污染复检已入 E2-B，上线后按月抽读 2-3 部片摘要并核对信息不足计数；③ 缓存键含 provider/model，换 --s1-model 自动隔离，配合手册 --force 边界说明；④ 原子写/每键锁须在多文件并行场景实测一次；⑤ 摘要质量与"信息不足"占比为 beta 转正式的判定素材，由 E2-B 与日常运行积累；⑥ D2026-0917-02 风险⑥（生产模型型号补记）仍悬置，记录在案。

**七、决策日志字段**：原决策＝v1.2.2 Beta 特性「剧情自摘要 auto_synopsis」（新立，含版本策略与 E2 验收口径裁决）；decision-critic 异议＝[HIGH_RISK_OBJECTION] 一条（循环验收，附方案甲/乙）+ 普通级条件六项；主模型最终决定＝采纳（方案甲双轨 E2、条件 1-8 全采纳、并入 v1.2.2、默认开、cloud 跟随）；条件闭环＝决策层面全闭环，执行面已全部实施（synopsis.py 新模块、分桶采样、缓存迁移、独立 max_tokens/超时、--no-auto-synopsis、指纹两键、手册 11.7、CHANGELOG Beta 标注；882 passed），待批次 E 双轨实弹；是否 [PRESSURE-OVERRIDE]＝否；风险跟踪见第六节，指标 2 归属声明（第五节）为验收强制项。

**八、执行追记（2026-09-18，批次 E 完成）**：
- E1 清洗：删除 1,134（24,214→23,080），全量备份 tm_full_backup_20260918_161031.csv，白名单 2 条保留，sha256 6a15b48b…99a0。
- 模型基线偏差（显式声明）：探针实测 translate-ja-zh-qwen3-8b 与 sakura-14b 均不遵守输出协议（编号/Translation> 前缀缺失），本机唯一协议兼容模型为 qwen3.8-27b-uncensored-joyfox-aggressive——E2 基线改用之，兼做该候选本地全量首验；D2026-0917-02 风险⑥（生产型号无记录）就此部分关闭（09-17 运行实走云端中转）。
- E2-A（关 beta）正式验收：指标 1=0/32 ✅、指标 3=5/5 ✅、指标 4=0/2 ✅、指标 5=2/2 ✅、指标 6=保留 64/删 6 ✅、指标 7=TM 命中 2/1160 ✅（恰为白名单 2 条）、指标 8=恒等式✅/100%；指标 2=2/5 明确正确+0 错误+3 条待定性，按裁决如实记录不宣称达标。[未翻译] 59 条（契约保留）。
- E2-B（开 beta，--no-tm 保 diff 纯度）：摘要 92 字/8 桶全片覆盖/信息不足 0 次（sha1 720d5e74…）；指标 1-5 与 E2-A 完全一致；diff=433 条（37.6%）译文不同；恒等式✅；[未翻译] 64。
- **用户验收裁决（2026-09-18）：选定 E2B 为成品**（基于翻译字幕对比效果更好；B 的[未翻译]略高 64:59、质量报告其余基本一致，均知悉）。按本决策第五节归属声明：**E2B 的语境改善由 beta 特性 auto_synopsis 贡献，非 v1.2.2 既有机制成就**。beta 维持默认开启；术语冲突观察 5 条留待用户判定。分歧复核.csv 两轨均为空属预期：本次为单引擎模式（无兄弟 pass），双引擎分歧通道无数据。
- 产物转正：E2B final_cn/质量报告 已复制至源目录（旧 09-17 版本改名 .bak-0917 保留）。v1.2.2 commit 待用户指示。

**九、执行追记（2026-09-18，TM 全量清空）**：用户指令全量清洗 TM 库（当前翻译不入库，为下一阶段模型测试提供干净基线）。执行：文件级备份 tm.db.bak-full-purge-20260918（清空前 24,071 行=23,080+E2 轨道新学 991）+ 既有全量 CSV 备份 tm_full_backup_20260918_161031.csv（E1 前 24,214 行）；tm_entries 清空至 0。模型测试建议每候选独立 --no-tm 或轮间清库，防首批学习污染后续对照。learned 自学习词库（glossary_learned.csv）为独立注入通道，是否同清由用户裁决（工具 glossary_learned_reset.py 就绪）。

## 2026-09-19 D2026-0919-01 [已拍板，条件待试点闭环]

**原决策**：5 本地模型 × A/B 两阶段翻译管线全搭配测试（25 组合）执行方案——`tools/model_matrix_run.py` 跑批器 + manifest 种子化复用阶段A + 组合间隙换模；全程 --no-tm、人工词库统一注入、组级 synopsis 缓存管理；试点 joyfox27b→trans8b 先行验证后放全队列。

**我的异议**（本决策我输出两项 [HIGH_RISK_OBJECTION] 及若干条件项，均被采纳）：
1. **[HIGH_RISK_OBJECTION-1] A 产物截获时机与代码事实冲突**：`subtransjav/refine/pipeline_v2.py:1762-1769` 中 "✅ 阶段A完成" 的 print 早于 manifest 落盘（A=done）且无 `flush=True`；stdout 触发存在静默劣化 A 草稿或静默全量重跑两重风险。要求改 manifest 文件轮询触发。
2. **[HIGH_RISK_OBJECTION-2] synopsis 组合级清空与复用机制自相矛盾**：缓存键已含 provider+model（`synopsis.py:162-174`），跨模型泄漏结构上不可能；组合级清空会迫使 B≠A 组合在 s1 未装载时调用 s1、失败静默降级为无摘要，与种子组合不同处理。建议改组级清空以实现全矩阵零中途换模。
3. 其余条件项：不传 --auto-glossary；glossary.csv 与 glossary_learned.csv sha1 批前快照+逐组合复核（变动即停队列）；成功判据=退出码∈{0,3} 且 final_cn.srt + 质量报告.txt 齐备才原子写 .done；退出码 3 为噪音信号（`cli.py:368-370`），[未翻译] 计数直接数终稿 srt；组合级软看门狗超时 kill+resume；stdout 环形缓冲；磁盘 ≥10GB 预检；include 存活检查用 /v1/models 就绪轮询、失败=组合失败不降级。

**主模型最终决定**：**采纳（全部，含两项 [HIGH_RISK_OBJECTION]，无复议）**
- 采纳-1：截获改 0.5s 轮询 manifest（A.status∈{done,degraded} 且 final≠done 时原子复制 srt+manifest），种子校验含 stages.A 模型字段与 input_sha1。
- 采纳-2：synopsis 清空粒度改组级；B≠A 组合零 s1 调用（缓存键命中）→ 全矩阵 25 组合零中途换模，JIT 竞态整体删除；换模仅在组合间隙（lms unload/load + 1-token ping 就绪，失败=组合失败）；附加看门狗——B 组合运行前后快照 `Temp/synopsis_cache`，出现变动即判 suspect_synopsis 无效。
- 其余条件全部采纳；glossary_learned 若已存在存量文件，作为隐藏常量在评测口径中注明。

**条件是否已闭环**：未闭环。关闭路径=试点验收：joyfox27b→trans8b / ipzz-847（739 条）全链路验证（截获内容 A=done、B 组合零 s1 调用、[未翻译] 残留与新鲜基线一致）+ 同模型新鲜重跑 A 的 diff 验收（≤2% 行差异）+ kill -9 中断恢复演练。责任方=主模型（编码实现后主模型复核试点数据）。试点未通过 → 全队列不放行（本人回退立场为反对放行）。

**是否 [PRESSURE-OVERRIDE]**：否

**后续风险跟踪**（复核节点）：
- 试点：截获 manifest 状态正确性、synopsis 缓存零变动、diff ≤2%、kill -9 续跑。
- 批中：glossary 双 sha1 逐组合复核；synopsis_cache 变动 → suspect 标记；[未翻译] 残留 vs 同组种子基线（显著高出 → 无效重跑）；组合级看门狗触发记录；.done 完整性。
- 批后：25 组合 manifest glossary_sha1/config_hash 一致性核验；glossary_learned 隐藏常量口径声明；评估阶段排除 suspect_synopsis 组合。

---

决策日志输出完毕，可归档。若后续试点出现新证据需复议（如 diff >2%、suspect_synopsis 频发、种子校验失败率异常），请携带上一轮日志与本轮材料重新发起评议，我将引用本条记录明示维持/修订/反转。

### D2026-0919-01 续记（2026-09-19 复议终结：diff 门槛重新定性）

- **原决策**：5×5 A/B 全搭配矩阵测试执行方案（含"试点新鲜重跑 A diff ≤2%"验收门槛）。
- **上轮状态**：已拍板、条件待试点闭环（diff ≤2% + kill -9 演练）。
- **本轮复议事由**：试点 diff 实测 25.85%（heretic35b），主模型以新证据申请门槛重新定性并放行。
- **复议裁决**：**采纳提案放行 + 条件**。机械验收（kill 中断/补漏截获/逐字节复用/摘要缓存同键命中）经日志抽查实证通过；25.85% 经证据链定性为模型采样方差（temp 0.1 长生成近并列转折级联；短生成近乎确定；已排除比较器空白噪声与服务端采样覆盖）。组内共享同一 A 草稿的固定刺激设计因高方差反而更强成立。
- **条件**：C1 报告口径（单次快照声明+方差指标+跨 A 组结论强制保留意见）；C2 joyfox27b 同口径方差补测（不阻塞队列）；C3 既有门槛全保（残留基线/synopsis 看门狗/glossary sha1/软看门狗）；C4 P3/P4 断言通过后放全队列；C5 可选端到端重复样本。
- **是否 [PRESSURE-OVERRIDE]**：否。异议级别：普通。
- **风险跟踪**：高方差模型种子 A 可能落于质量尾端（缓解=C1/C2/C5，[UNVERIFIABLE] 事前位点无法判定）；C1 未落地则跨 A 组比较结论无效。

## 2026-09-21 [D2026-0921-01] 生产模型搭配定版：质量优先 joyfox27b>heretic35b [已拍板·默认落位待执行]

**原决策**：从两轮矩阵证据中选定生产默认 A>B 搭配；判定权在用户。

**最终决定（用户拍板，主模型转达）**：
- 默认生产搭配 = joyfox27b>heretic35b（质量优先，约 70 分钟/部）。
- 同档替代采纳评议修正：heretic>hauhau 提为并列替代（盲评 39、R2 12.4 分、合计缺失 2、别停 1 处、A 离群 3）；hauhau>heretic 降为门槛候选（前置：用户接受"别停"4 处+"滑溜溜"1 处与盲评簇 34.6 偏硬风格）。
- 特选门控被本拍板取代：joyfox>heretic 由门控备选升为默认；原"启用前复验增益"建议转为生产期首文件三方抽检动作。

**决定性质**：用户权重选择（质量优先 + 暖库样本上限），非证据推翻指标对比；评议员稳健口径推荐（heretic>heretic）异议保留归档，执行层面服从。评议采纳情况：部分采纳（采纳同档替代纠正；未采纳默认搭配）。

**两轮证据要点**：
- R1 盲评（测试片B，21 段/42 分）：heretic>heretic 与 joyfox>heretic 均 40，heretic>hauhau 39；hauhau-A 簇 34.6（偏硬）；sakura-A 18.2；陷阱句全数通过。顶部 40/40/40 存在天花板效应，40 vs 39 无判别力。
- R2 终稿机械统计（测试片C，1162 源，TM 全新安装模拟）：健康梯队合计缺失（占位+真丢失）hauhau>heretic 0 / hauhau>hauhau 1 / heretic>hauhau 2 / heretic>heretic 4 / joyfox>hauhau 13 / joyfox>heretic 17；B=joyfox 占位 23~52、真丢失 7~20（重试耗尽降级，含管道混杂，未纯模型归因）；A=sakura 占位最多 151 最差。前四健康组合分数 12.4~13.0 差在 heretic 采样方差内，排序依据为稳健性/考点/盲评簇而非分数。
- 考点对照（51 源×16 组合）：部長で 人称读法仅 joyfox-A 语境正确（4/4）；クリ 误译"克里"源出 joyfox-A（4 中 3）、唯一被 B=heretic 纠正 → "若选 joyfox-A 则 B 必须配 heretic"前提成立。
- "别停"命中：heretic-A 各组合 1 处、joyfox>heretic 2 处、hauhau-A 系 4~9 处；B=hauhau 对 A=hauhau 有放大效应（4→9），对 A=heretic 不放大。
- 模型背景：heretic35b 采样方差大（批级 53%、全管线近义改写 25.9%）；joyfox27b 慢 5~10 倍；hauhau35b 修复力最强但 A 席位考点偏离多；sakura14b 草稿协议缺陷。

**勘误登记（两处）**：
1. 报告正文转录错：B=sakura 三组被误写为"0/669"并列首选；实测终稿 958~980 块、整行丢失 167~204 条（约 15~17%，字符量同比例下降，排除条目合并假象）。正文不得再作为后续选型引用源。
2. metrics_table 口径低估：漏覆盖记录 102/101/109 vs 实测整行丢失 167~204，低估约 50~100%。漏覆盖指标定义待审计（疑只记初始失败、漏记重试后吞行）。

**评议员异议与回应记录**：
1. [INFO_GAP] 同档替代取舍（hauhau>heretic vs heretic>hauhau）——采纳，heretic>hauhau 提级。
2. 默认稳健口径推荐 heretic>heretic——未采纳（用户权重选择），异议保留、执行服从。
3. 特选门控化——被升默认决策取代，原复验建议转生产期抽检。
4. 异议级别：无 [HIGH_RISK_OBJECTION]。

**新增机制证据（拍板后代码查证/实测）**：
- TM 精确命中=整句哈希直接替代、跳过 A/B 两段 LLM（pipeline_v2.py:940-963）；模糊命中阈值 0.98 仅注入参考不省生成；TM 入库为 B 终稿（pipeline_v2.py:2214-2358）。速度不随库增长显著改善（新内容命中率≈0），"提速论"未纳入决策理由。
- tm.db 实测 995 条（=joyfox>joyfox 产物）：含 [未翻译] 占位 40 条、克里误译 2 条；入库门槛只滤纯假名、不过滤占位 → 精确命中存在将占位/克里错误确定性回填后续文件的传播向量，与"优质缓存"前提冲突（优先级：高）。
- glossary_learned 通道默认开启但 32 单元零产出 → 新遗留核验项。

**条件闭环状态**：
- 条件①（生产期监控：合计缺失超阈值或出现整行丢失则回退并列同档 heretic>hauhau）→ 转生产期动作；【阈值校准】原 0.5% 阈值按健康梯队基线设定，而默认档自身观测基线为 17/1162≈1.46%，应以默认档基线±容忍带为界，超过即回退。
- 条件②（用户对"别停"语义明示判定）→ 未闭环：本拍板未含别停专项判定；joyfox>heretic 别停命中 2 处，收为首期抽检核验项。
- 特选复验建议 → 转生产期首文件三方抽检（含 别停/クリ/部長で 考点复核）。

**关联待决项**：
1. 决策项 #2：TM 995 条清零或保留——未决。按"优质缓存"前提，清零与本次质量优先拍板更自洽；若保留，至少剔除 40 占位与 2 克里条目。建议生产首部跑批前定。
2. glossary_learned 零产出核验。
3. 默认落位（安装/GUI 默认值写入）为待执行动作，涉及管线代码，需用户明确批准后另行委派。
4. 漏覆盖指标口径审计。

**后续风险跟踪**：TM 保留情形下占位/克里精确命中传播（含源字幕跨集重复句）；joyfox>joyfox 995 条不清零将构成"已排除搭配产物回流"；heretic 采样方差导致的占位/别停计数翻转；B=joyfox 类重试耗尽事件生产告警。

## 2026-09-21 [D2026-0921-02] 1.2.2 收尾批量执行：TM 治理、配置落位与遗留修复 [已拍板·执行中]

**决策问题**：D2026-0921-01 拍板后的收尾范围取舍——用户逐项裁决交接文件第六节遗留待决项与 1.2.2 审查遗留项，并指定执行顺序（防误提交 → TM 清零 → 配置落位 → 代码修复 → 新搭配成因分析 → 仓库收尾）。

**用户拍板（2026-09-21，闲时任务执行）**：
1. **防误提交**：.gitignore 补 config/glossary.csv.bak-*、tm.db.bak-*、tm_full_backup_*.csv、.abtest/、.tmp374/；提交前禁 git add -A 盲加。
2. **TM 995 条不保留为正式库**：备份（tm.db.bak-glossary-purge-20260921，sha1 7d1c8e64…，清空前 995 条 joyfox>joyfox 学习产物）+ CSV 快照后原子清零；新搭配 joyfox27b>heretic35b 使用独立库 Temp/translation_memory/tm_joyfox_heretic_20260921.db（--tm-db 指定），不复用旧库。关闭 D2026-0921-01 关联待决项 1。**【追记】执行中用户裁决：joyfox>joyfox 学习产物不需要备份——上述备份与 CSV 快照已删除，995 条彻底废弃**。
3. **glossary_learn_enabled 默认改 False**（config.py，D2026-0921-01 关联待决项 2 一并落位）：成因分析与清洗缺陷修复完成前学习通道保持关闭；显式 True 仍可开启。回归测试锁定。
4. **生产默认搭配落位**：RefineConfig.stages 默认 A=joyfox27b（qwen3.8-27b-uncensored-joyfox-aggressive）> B=heretic35b（qwen3.6-35b-a3b-uncensored-heretic-apex）。关闭 D2026-0921-01 关联待决项 3。生产首文件三方抽检留给用户首部正片时执行。
5. **新搭配成因分析立项**：mida-559 单组合 joyfox27b>heretic35b 跑批 + [未翻译]/缺失成因分类（证据独立目录、独立 TM、学习关闭）。
6. **8 条「[未翻译] Chicks。」残译清洗缺陷立项**：与前缀契约统一同批修复，确保该形态不进 TM 且不误删正常引用。
7. **sakura14b 不重测**，LM Studio 聊天模板修复项关闭。
8. **旧第一轮 25 组合不补做**成因分析，由新搭配成因分析替代。
9. **.abtest/、.tmp374/ 原始证据不入库**（原始目录保持 ignored），仓库只留 manifest/README（命令、日期、版本、哈希、结论、关键日志摘要）入库。
10. **1.2.2 审查遗留 4 条修复执行**：_filter_language 回填保护（P0）、[未翻译] 前缀契约统一（P1）、漏覆盖口径修正（quality_report.py，拆 missing_entry/untranslated_content，P1）、target_aliases 写侧修复（P2）；quality_report.py 动工前先补 544d8d0 diff 核查欠账。

**决策性质**：执行层收尾裁决（既有评议链 D2026-0919-01/D2026-0921-01 的延续），无新架构/选型议题，不另启动 decision-critic 评议；与 D2026-0921-01 评议结论（无 HIGH_RISK_OBJECTION）同向。

**是否 [PRESSURE-OVERRIDE]**：否。

**后续风险跟踪**：① 新搭配首部正片三方抽检（heretic>heretic / heretic>hauhau 对照）+ 别停/クリ/部長で 考点复核；② 学习通道重开前提=成因分析与清洗缺陷修复完成且抽检通过；③ .abtest/ 原始证据仅本地保留，磁盘清理前须先固化 manifest。

## [2026-09-22] [D2026-0921-03] Mimosa git-gate 阻塞裁决与本轮标准运行模式 [已执行]

**背景**：Mimosa 插件 git-gate（ZCode Bash 工具层 PreToolUse，非 .git/hooks 原生钩子）在 commit/push 前对全仓做静态快扫并强制拦截。本轮 1.2.2 收尾的 20 件变更（17 改 + 3 新，以提交时刻 git status 为准）被阻塞。核查事实：① 快扫报 27 个高危全部位于历史文件与被忽略的本地文件（create_shortcut.py、tests/test_secrets.py、tools/ 旧脚本、Temp/ 脚本、api.py 旧代码），与待提交 20 件零交集——且这些历史文件已随既有历史公开于 origin（本地与 origin/main 同步），对其拦截无保护价值；② 两次带 seal 正式扫描（normal + deep）对 26 条 findings 复核层全部裁定 verdictEffect=none、无 proofGaps（静态误报：本地单机工具硬编码 localhost、脚本内部构造路径、pytest tmp_path 测试夹具模式）；③ tests/test_secrets.py 上一轮已为过扫描改写字面量、本轮仍被同一规则命中——改写正常代码满足全仓扫描不可收敛；④ 插件 payload 为 Ed25519 签名混淆包，无扫描范围配置项；payload 文档核实 `mimosa validate` 的 allowlist 为内置窄域 Oracle，**当前版本无用户可配置的 findings baseline/allowlist**；⑤ 计数口径：hook 快扫 27 高危（实时工作树，含 Temp/ 脚本）vs 密封扫描 26 findings（快照口径 24 高 + 2 低），两套引擎范围时点不同，双口径分列、不强行核平。

**裁决（用户拍板，decision-critic 有条件支持，其 [HIGH_RISK_OBJECTION] 六条件全部采纳）**：
1. 本轮提交采用**终端直提**（(a)：在 ZCode 之外的终端执行提交脚本，gate 不生效）——否决 (b) 临时停用插件（全局无扫描窗口 + 恢复动作是典型静默失效点）；`--no-verify` 对工具层 gate 无效，不使用。
2. **不为 gate 修改正常业务代码**；历史误报不混入本轮提交，单列跟踪（见后续风险跟踪）。
3. 不修改/不卸载 Mimosa 插件。

**标准运行模式（当前插件版本下的固定提交前置流程，非一次性豁免）**：定向 secret 扫描（以提交时刻 git status 真实全集为准）→ 终端直提 → 三查（工作树干净 / 语义 commit 数与预期一致 / committed blobs 复扫）→ push 前远程状态核对 → push。任何一项失败即停止回报。

**长期建议（移交维护方，本仓无法自行配置签名保护包）**：① git-gate 改为仅扫 staged diff；② 提供 findings baseline/allowlist 机制。**重评估触发器**：插件更新（任一版本变更）即重评本模式。

**本轮前置证据（2026-09-22）**：定向 secret 扫描：20 件真实全集（17 改 + 3 新）× 10 模式（aws/github/google/slack token、私钥块、Bearer、JWT、键值对字面量、hex32/hex40 高熵串）全部零命中；decision-log 增补本条后对最终入库内容复扫仍零命中。输出摘要随执行追记留存。

**0916/0918 历史 TM 备份**：保持不动，处置权在用户。

**执行追记（2026-09-22 补录，状态转已执行）**：
- **六段提交**（b30f93f → 8223106，顺序与裁决一致）：b30f93f chore: ignore private glossary backups and evidence dirs → f94de78 chore: set glossary_learn_enabled default False → 66d259f fix: harden language filter and unify untranslated prefix → f64b79d feat: detect untranslated content in quality report + tests → 9701473 fix: preserve target_aliases on GUI save → 8223106 docs: finalize decision log and handoff/model matrix runner（含本条 D2026-0921-03 归档）
- **三查**：工作树干净（porcelain 空）；git log 恰六段语义提交；fetch 后 ahead 6、无分叉
- **blobs 复扫**：HEAD~6..HEAD 新增 1964 行 × 10 模式（aws/github/google/slack token、私钥块、Bearer、JWT、键值对字面量、hex32/hex40）全部零命中
- **push**：首次执行未达远程（远程仍 745cb04，原因未查明，疑似凭据/工作目录问题）；重推成功 `745cb04..8223106 main -> main`，ls-remote 复核 origin/main=8223106，本地与远程完全同步
- 本追记为工作区唯一未提交变更，随下一轮标准运行模式入库（其提交前流程照旧：定向扫描 → 终端直提 → 三查 → blobs 复扫 → push）

## [2026-09-22] [D2026-0922-01] 1.2.3/1.3 版本划分与 GUI 参数裁剪定版 [已拍板→已执行]

**背景**：1.2.2 已收尾发布（六段 745cb04..8223106 加 SIM105 轮 92ebcea/952f6f4，本地与 origin/main 同步）。下一版本规划经两路探索调研与 decision-critic 评议形成推荐，含三项待终选。

**裁决（用户终选，2026-09-22）**：
1. **划分方式：两段式**——1.2.3 收尾+加固小版本（A2 残译清洗修复、A5 漏覆盖口径收口、A4 glossary_learned 零产出成因核验、quality_report/pass_disagreement 补测、mypy report-only 进 CI、GUI 裁剪参数、补打 v1.2.2/v1.2.3 tag、生产型号归档、决策包三项拍板）→ 1.3.0 架构版（pipeline_v2 拆分[验收门=全量测试+黄金集+CI 全绿，行为等价不过即回退]、词表覆盖层+加载优先级改造、GUI i18n+完整参数面板、LRU、文档修正、lockfile）→ 1.3.1 行为变更版（H4b 条目级阈值自适应、legacy providers 清理执行、guard 脚本、Mimosa 21 项甄别）。
2. **GUI 参数：裁剪版进 1.2.3**——仅暴露 source_filter、auto_synopsis、dry_run、verbose 四个安全参数；敏感开关（force_resume、glossary_learn 等）留 1.3 与 i18n 一并做。
3. **1.2.3 现在开工**。

**硬依赖**：①批次 E（TM 清洗+全新重跑+8 项指标验收）必须用 A5 修正后口径执行；②1.2.3 测试补齐是 1.3 拆分安全网；③A4 结论是 1.3 词表覆盖层改造前置输入。

**边界事实**：A5 双口径拆分（missing_entry/untranslated_content）已随 1.2.2 收尾批 f64b79d 落地并带验收测试（quality_report.py:604-638，test_pipeline_v2.py:1046 起）；1.2.3 内完成独立补测与复核收口，确认无残余缺口即结项，有缺口则补改。

**决策性质**：规划层裁决（延续 D2026-0921-01/02 评审链）；规划稿已经 decision-critic 评议（无 HIGH_RISK_OBJECTION），本次为用户终选，不另加评。

**是否 [PRESSURE-OVERRIDE]**：否。

**后续风险跟踪**：①批次 E 待 A5 结项后由用户执行；②A2/A4 修复结论与决策包三件拍板结果随执行追记回填本条或另立新条目归档。

**执行追记（2026-09-22 补录，状态转已执行）**：
- **release 提交**：`0b3dbc8` chore(release): 1.2.3 收尾版（单段提交，用户裁定口径；16 件 667+/11-，含 A2 残译清洗修复、A5 补测收口、A4 注释收口、GUI 四参、mypy report-only、版本 1.2.3、生产型号归档 models/README.md，及本条与 D2026-0922-02 归档）
- **执行结论回填**：A5 复核=双口径拆分实现正确零缺陷（f64b79d 已落地，本轮独立补测 11 例收口），批次 E 前置满足；A2=泄漏路径封口（`_normalize_untranslated_marker`，+3 例回归）；A4=32 单元零产出定性为双闸门默认关闭的设计行为，仅注释收口；决策包三件见 D2026-0922-02
- **tag**：`v1.2.3` → 0b3dbc8（已推送）；`v1.2.2` → 952f6f4 仅本地保留未推（远程已有用户手建无 v 前缀 `1.2.2` tag 同点并挂 GitHub Release，避免重名冗余；本地是否删除由用户定）
- **三查**：工作树干净（porcelain 空）；恰一段语义提交、领先 origin/main=1 无分叉；tag 解引用 v1.2.2=952f6f4、v1.2.3=0b3dbc8
- **blobs 复扫**：提交前工作区全集 16 件与 HEAD~1..HEAD 新增 667 行各 × 10 模式（aws/github/google/slack token、私钥块、Bearer、JWT、键值对字面量、hex32/hex40）全部零命中
- **push**：`952f6f4..0b3dbc8 main -> main` + `* [new tag] v1.2.3 -> v1.2.3`；ls-remote 复核 origin/main=0b3dbc8、refs/tags/v1.2.3 在位，本地 ahead=0 完全同步
- **待办移交**：①批次 E 由用户执行（A5 口径已收口，须用新口径跑）；②mypy report-only 首轮输出留 CI Actions 日志，阶段二收口时去 `|| true` 改门禁；③tools/model_matrix_run.py 11 项 ruff 债务（CI lint 范围外，1.3 立项清理或明确划出范围）；④本追记为工作区唯一未提交变更，随下一轮 standing mode 入库

## [2026-09-22] [D2026-0922-02] 1.2.3 决策包三件处置定版：v2_file_parallel 保留 / legacy providers 全表清理 / 4槽结构保留与槽位语义对齐 [已拍板]

**决策问题**：1.2.3（收尾+加固小版本，只决策不改码）对三件遗留项的处置裁决：①`v2_file_parallel` 休眠开关去留；②legacy providers（glm/groq 等孤立配置条目）清理范围；③v2 槽位 1/3 占位处置与 config 默认工厂语义对齐。

**评议方式**：decision-critic 独立评议。材料清单核验通过（三件事实逐条以代码只读抽查复核，含跨 git 历史对照 v1.1.0 era 与现 1.2.2 的 TM stage 写法）；主模型裁定后回传决策日志文本归档。

**裁决一（v2_file_parallel 保留，无修订）**：维持保留，2.0 规划时重议去留。依据：`config.py:331` 默认 False + 注释明示「P1-6 云端多文件并行（opt-in，默认关）……预留 2.0，当前恒为关闭（O10）」；`pipeline_v2.py:1388-1400` 有真实分支逻辑（云端服务商才启用）；无任何生产启用通道（不在 TUNABLE_FIELD_TYPES、无 env/CLI/GUI 入口）；`test_config_layering.py:79` 锁默认 False、`test_perf_optimizations.py:256/269/284` 显式 True 覆盖分支。删除是纯减法且测试有依赖，无维护收益。论据核验：`_file_parallel_enabled` 本地档恒 False，与分支注释/测试三方自洽；1.2.3 无净新增动作。

**裁决二（legacy providers 清理，范围采纳扩大）**：采纳评议的范围一致性补充——清理标准统一为「本项目全部入口（CLI choices / config.py PROVIDER_* 常量 / GUI / 文档）零暴露且仓内零引用」，据此范围扩大为 PROVIDER_CONFIGS 全表六条孤立条目：glm/groq/openrouter/gemini/claude/gpt（translate/providers.py:18-56，全仓仅自引用、无消费点；translate/__init__.py 已声明 legacy PySubtrans 引擎移除，v2 用 LLMClient 直连）。删除时对旧配置手写 `provider=<已删名>` 的路径补友好校验报错（禁静默空回退，注意 config.py:443 PROVIDER_MODEL_DEFAULTS.get() 的静默空值兜底语义需一并处理）；不采用「保留条目名只删字段」折中。执行窗口：1.3.1 执行删除，1.2.3 只记录决策。

**裁决三（槽位 1/3 占位处置，论据修正采纳）**：结论维持——保留 4 槽结构不动、压缩为 2 槽永久不做；「config 默认槽1/3 enabled 改 False」的语义对齐排 1.3.0 执行（1.2.3 不动码）。论据修正（采纳评议代码核验）：原论据「TM 按 stage 编号索引、压缩 2 槽致历史 TM 数据索引错位」不成立——v2 管线对 TM 读写恒为字面量 stage=1（pipeline_v2.py:979 lookup / :2376 store，git 历史 v1.1.0 era 起即如此），TM stage 编号体系（legacy 1/2/3，tm.py:10）与槽位数解耦。修正后归档论据：保留 4 槽的硬约束是五条路径多点联动耦合——①V2_STAGE_SLOT={"A":0,"B":2}（pipeline_v2.py:99-100）；②STAGE_NAMES 4 槽语义注释（config.py:100-101）；③manifest._V2_STAGE_SLOTS（manifest.py:219，契约测试钉住防两处漂移）；④GUI by_stage 阶段展示重建逻辑（webview_gui/api.py:1022-1036）；⑤历史配置 stages 数组默认工厂（config.py:227-233）。压缩成本高（五处联动改写+配置兼容+清单指纹）收益低，故永久不压缩。1.3.0 对齐附带要求：①顺带核对 GUI 阶段展示语义不被误导（api.py:1022 阶段重建逻辑）；②考虑一并厘清 StageConfig.name / STAGE_NAMES 索引语义（config.py:218-220），避免只改 enabled 留下第二个隐晦点。语义不一致根因确认：config.py:227-233 默认工厂槽1/3 enabled=True 与 cli.py:172-181 硬编码 False 确实矛盾；enabled 仅影响 manifest/GUI 阶段展示，对 v2 业务流程（按 tag→槽位取数）无行为影响。

**评议结论**：三件决策均无 [HIGH_RISK_OBJECTION]（均低风险、可逆）；一处论据修正（裁决三）与一处范围扩大（裁决二）均采纳；异议级别全部为普通，无异议保留项。

**是否 [PRESSURE-OVERRIDE]**：否。

**执行排期**：1.2.3 三件只记录决策、不引入行为变更；1.3.0 槽1/3 enabled 默认值对齐 False（含 GUI 展示语义核对、STAGE_NAMES/StageConfig.name 索引语义厘清）；1.3.1 PROVIDER_CONFIGS 全表六条孤立条目按统一标准审计后删除，旧配置 `provider=<已删名>` 路径补友好校验报错。

**后续风险跟踪**：①1.3.1 删除前复核一次全仓引用面（GLM_API_KEY/GROQ_API_KEY 等 env_var 占用无残留），删除后确认 config.py 校验路径对未知 provider 给出明确报错而非静默空值；②1.3.0 槽位对齐触及 config.py 默认工厂，需回归 test_config_layering.py / test_pipeline_v2.py / test_manifest_model.py 契约测试，防默认值改动影响清单指纹；③v2_file_parallel 休眠开关保持注释与测试锁定状态，2.0 规划时凭本条目重议去留。

**执行追记（2026-09-22）**：三件均为决策记录、无代码执行项；本条随 release 提交 `0b3dbc8` 入库并推送（v1.2.3），1.3.0 槽位对齐与 1.3.1 全表清理执行窗口照旧。

## [2026-09-22] [D2026-0922-03] 1.3 方案四项拍板定版：质量报告两层处置 / H4b 挂双且门 / tools ruff 前置清理 / mypy 分批清 [已拍板]

**背景**：1.3 方案（1.3.0 架构版 + 1.3.1 行为变更版，承 D2026-0922-01 / D2026-0922-02）提交 decision-critic 独立评议，产出 3 项 [HIGH_RISK_OBJECTION]，主模型全部采纳折入方案后提交用户终选。用户对四项待决点全部拍板（2026-09-22）；decision-critic R1 确认轮对问 A（H4b 定性与门条件可验证性）、问 B（mypy 边界定义）作出确认并给出补强文本，本条为定版归档。

**决策问题**（四项待决点）：①质量报告两层处置是否纳入 1.3 及子里程碑切分；②H4b 条目级阈值自适应的落地条件与顺延口径；③tools/model_matrix_run.py 11 项 ruff 债务（CI lint 范围外）处置；④mypy 存量清理边界、分批挂点与转硬门禁时点。

**三项 HRO 采纳记录**：
1. **HRO-1（行动层与 H4b 拆离）——采纳**。依据：上游 asr_telemetry.jsonl 仅 Balanced 车道产出（D2026-0914-01），而 1.3 生产默认 F06（wseg×ten，D2026-0917-03-R1-E2）非 Balanced；仓内零 telemetry 消费链（asr_meta.py 仅解析 whisperjav_run.json）；H6 黄金集为构造集基线（golden_v1.0，49 条）仅冻结闸门0 行为，不足以验证条目级自适应净收益。落地：行动层（报告驱动条目级定向重翻）独立交付于 1.3.1；条目级质量记录 schema 一次定义；H4b 仅作该 schema 的预留消费者。
2. **HRO-2（拆分验收"行为等价"域定义）——采纳**。等价域=拆分前后产物级字节快照：3-5 个真实输入，相同输入 + 固定 FakeClient + 空 TM + 固定词表 → final_cn.srt 与 {stem}_质量报告.txt 逐字节 diff（排除时间戳/LLM 段）+ 差异白名单（非零差异必须评审留痕）+ 每个新模块 ≥1 直接单测 + 有意变更单列豁免逐项挂回归（已知有意变更：词表文件缺失静默退化→WARN、槽位 1/3 enabled 默认 False）。
3. **HRO-3（1.3.0 负载与排期）——采纳**。排序：pipeline_v2 拆分最先 → 解读层 CLI 侧并行 → GUI i18n+参数面板+查看器+槽位对齐同批 → 词表覆盖层 → LRU/文档/lockfile 收尾；批次 E（用户实弹，A5 新口径）卡点在拆分动工前而非 1.3.0 末期；mypy 转硬门禁是专项非收口（见拍板四）。

**四项拍板与执行要点**（用户终选 2026-09-22，R1 确认轮结论已并入）：

1. **质量报告两层处置**。同意纳入 1.3 拆两层：1.3.0 解读层（CLI 侧，只动 quality_report.py）为可独立交付子里程碑，1.3.0 若延期其价值不捆绑沉没；1.3.1 行动层。执行要点：①新增 {stem}_质量报告导读.json（白话结论 3-5 条 + 章节清单 + 引用伴生文件名），与 txt 同一入参快照一次采集双渲染，txt 唯一全文权威；否决"GUI 直读 txt"与全量 json；导读 json 必须进 _backup_existing_outputs 与 delete_resume_artifacts，并顺带补齐既有缺口——{stem}_风险清单.md/.json 现既不被备份（pipeline_v2.py _backup_existing_outputs 仅覆盖 final_cn.srt/质量报告.txt/分歧复核.csv/术语冲突观察.csv 四类）也不被 resume 清理（manifest.py delete_resume_artifacts 仅覆盖 manifest/refine_A/幻觉处置报告.json/隔离区.srt 四类），存在陈旧/新鲜报告并存的公信力风险；②报告白话结论区必须带产物时间戳 + "基于本次运行"声明，"需处理 N 处"的 N 与章节明细同源计算（D2026-0916-03 两处数字打架教训）；③行动层 dry-run 预览先行，复用 manifest/resume 指纹，时间轴编号不漂移、恒等式不断裂；④词表优先级链（CLI 参数 > 用户词表 > 学习词表 > 内置）与既有配置分层在文档显式分域，新 CLI 参数进 manifest._CONFIG_FIELDS 指纹。
2. **H4b 挂双且门**。由"1.3.1 落地"改为条件落地：门①F06 默认拍照下 asr_telemetry 存在或明确降级口径；门②H6 真实语料基线建立。两门皆备（AND）方落地 1.3.1，否则顺延 1.4/2.0，届时复用行动层定义的 schema 不留债；1.3.1 只作 schema 预留消费者。**定性：对 D2026-0914-01 顺延承诺的细化而非推翻（R1 确认，本条即为显式标注）**，理由：原顺延三前提（telemetry 仅 Balanced / schema 无承诺 / H6 基线未建立）与双且门一一对应且全部仍然成立，F06 定版（D2026-0917-03-R1-E2）反而强化前提①；原条目源文即"4b 厚版 P1 或顺延 1.3"，1.3 从非无条件承诺；4a 已落地价值与其余 1.2 共识零触碰。门条件补强文本（R1）：门①分支 a 判定=F06 生产默认实跑一次（可复用 .abtest/prod_rerun.sh 权威复现命令）后 raw_subs/<stem>.asr_telemetry.jsonl 存在且非空；分支 b"明确降级口径"须落盘决策日志且至少含五要素——(i) telemetry 缺失时自适应默认不启用并在报告红标；(ii) Balanced 车道 opt-in 的 CLI/GUI 暴露点与档位说明；(iii) 对 resume 指纹的影响评估；(iv) telemetry 解析容错与上游 schema 无承诺的防御（复用 asr_meta.py R6 新鲜度/降级模式，此项吸收双且门原文本未覆盖的"schema 无承诺"前提）；(v) 降级口径生效的观测方式；分支 b 通过时 H4b 有效口径为 Balanced opt-in 子集而非全量自适应，验收按子集口径执行并在决策日志记录分支结果（a=全量 / b=子集）。门②判定=golden 集新增 origin:"real" 真实语料子集 ≥30 条且含 suspect/empty 案例，防循环验证记录（generated_by/标注人）齐全，tools/gate0_golden_stats.py 输出真实子集分项 precision/recall；最低样本量补强防"1 条真实样本即过门"。判定时点：两门均为 1.3.1 行动层动工评审时一次性判定，结果回写决策日志；若顺延，在 1.4/2.0 规划时凭新事实重判一次。
3. **tools ruff 11 项前置清理**。1.3.0 开工前独立小 PR 清掉 tools/model_matrix_run.py 11 项（R1 实测复核 11 项在案、其中 4 项可 --fix；CI lint 范围仅 subtransjav tests，.github/workflows/ci.yml:27）；不混入任何 1.3.0 功能改动；若排不下则显式划 1.4 并在决策日志留痕"已评估、因排期显式顺延"，不得悄悄消失。
4. **mypy 分批清（R1 确认：需要边界定义，以下即定版边界）**。存量随 1.3 周期分批清、独立专项、不挤进 1.3.0 核心路径；存量清零后转硬门禁显式单列。①核心路径清单=生产 refine 全链路：subtransjav/refine/ 全部模块 + subtransjav/translate/（llm_client.py、providers.py）；核心路径约束双轨——存量随工作流清零、当前已 0 错模块（config.py、manifest.py 等）"清零保持"不回添；utils/process_manager.py（R1 实测 28 错，占总量 46%）与 webview_gui/*（实测 9 错）列为非核心专项批。②分批与挂点（按 R1 实测分布 61 错/11 文件，开工时以 CI ubuntu/py3.12 同口径重测冻结）：M1 管线批 21 错（pipeline_v2 10 / quality_report 4 / glossary_conflict 2 / cli 2 / post_validate 1 / pipeline_support 1 / language_validator 1）随 1.3.0 对应工作流验收门附带"触及模块存量清零"；M2 GUI 批 9 错（api.py 7 / event_stream.py 2）随 1.3.0 GUI i18n+参数面板工作流附带；M3 独立专项小 PR 31 错（process_manager.py 28 / llm_client.py 3，纯注解无行为变更）排 1.3.0 周期内、tools ruff 小 PR 之后；硬门禁转挂 **1.3.1 收口验收门**（显式单列"mypy 全仓 0 错 + CI 去 || true"），M1-M3 提前清零可提前转，不强制等 1.3.1。③"新代码不新增错误"机制（基线文件法）：提交 mypy 基线文件（file:line:error-code 三元组），CI mypy 步骤（ci.yml:29-31，仅 ubuntu/py3.12 腿）改为基线过滤判定——不在基线的错误即 fail、基线内存量放行；配 tools/mypy_baseline.py 显式 --update（决策日志留痕）；存量清零后"删基线文件 + 去 || true"同一动作完成转硬门禁；本地预检同脚本可跑，须带 --python-version 3.12 与 CI 同口径（R1 实测本地解释器 <3.12 直跑会因 numpy stub 语法差异误报检查中止，该注记写入脚本注释）；备选 per-module 错误上限表因"同模块他处减少掩盖新增"不采纳为主机制。1.3 期间新代码按此机制保证 0 新增、最好随批次逐步收紧。

**评议结论（R1）**：四项拍板全部支持；问 A 定性细化非推翻，双且门补强文本已并入拍板二；问 B 需要边界定义，三项建议已并入拍板四；无新增 [HIGH_RISK_OBJECTION]。两处基线数字实测修正随本条归档：mypy 存量实测 61 错/11 文件（此前沿用口径 60）；pytest 口传基线"951 passed + 1 skipped"与 v1.2.3 tag（0b3dbc8，工作树干净）收集数 932 存在 20 例缺口（收集零错误，成因待核）。

**是否 [PRESSURE-OVERRIDE]**：否。

**后续风险跟踪**：①pytest 基线以 1.3.0 开工时全量实跑输出冻结为准（"只增不减"参照点须钉在可复现 commit 上），主模型先核实"951+1"与"932 收集"缺口成因（条件参数化/环境差异/口传失真）再冻结；②mypy 基线文件以 CI（ubuntu/py3.12）实测重冻，本地数字仅作参考；③门①门②判定结果须回写决策日志（新条目或本条追记），H4b 若顺延须注明"复用 1.3.1 行动层 schema 不留债"；④tools ruff 小 PR 若划 1.4 须在本日志显式留痕；⑤导读 json 与风险清单 md/json 的备份/清理补齐须附契约测试，防未来新增伴生文件再次漏挂；⑥批次 E（A5 新口径，用户实弹）为拆分动工前卡点，承 D2026-0922-01 硬依赖条款。

**补充指示追记（2026-09-22，用户确认轮）**：用户逐项确认问 A（细化非推翻、门①门②补强到位）、问 B（mypy 边界三项建议）、执行要点自查与归档结果，无异议。补充指示：①实测修正采纳，mypy 存量口径以 61 错/11 文件为准；②932 缺口成因核实**优先于**基线重冻——若系收集配置问题（conftest 标记/路径过滤/插件差异）则重冻会掩盖问题，若系合理用例增减（v1.2.3 后用例合并/删除）则重冻即可；③下一轮开工顺序定版：tools ruff 独立小 PR（开工首件，不混功能改动）→ 932 缺口成因分类 → pytest/mypy 基线重冻 → 分类结论回写本条风险跟踪 → 拆分动工前等批次 E（用户实弹）。

**开工首件追记（2026-09-22 交互轮，用户指令"满足条件前提下并行开工"）**：
- **A·tools ruff 11 项清零**：UP009/E401/I001/UP015 机械修；E402 将 sqlite3 import 真移顶部（无 sys.path 前置、无副作用，非 noqa）；SIM105×4 改 contextlib.suppress；SIM115×2 因日志句柄须跨子进程生命周期长驻（finally 统一 close）不可改 with，行尾 noqa 并留原因注释。验证全绿：`ruff check tools/model_matrix_run.py` 0 错、`ruff check subtransjav tests` 全绿、pytest 实测 **951 passed+1 skipped（collected 952）零失败**、`--help` 冒烟通过；行为等价（纯语法级替换+import 重排）。提交 `9074832`（单段语义提交，仅触 tools/model_matrix_run.py，23+/18-，UTF-8 字节核验无误）；三查过（porcelain 无该文件 / 恰一段提交无分叉 / show --stat 仅该文件）；定向扫描与提交后复扫同结果（26 findings 全为既有静态告警、提交零新增、依赖风险 0）。**push 待补**：10808 代理（v2rayN）未启动致 push 失败（代理重试+直连重置各一次），commit 滞留本地 ahead=1 无分叉，用户启动代理后 `git push origin main` + ls-remote 复核补齐。
- **B·932 缺口成因分类**：结论=**口径失真（环境差异）**，非收集配置问题、非用例增减。成因：tests/test_gui_api.py:16 模块级 `pytest.importorskip("webview")`（pywebview 为可选 gui extra）在无 pywebview 环境将 20 例转为 1 条模块级 skip 不入 collected；屏蔽 webview 的注入实验精确复现 932、正常 venv 复现 952。配置面排查：仓内零 conftest.py、无 addopts/deselect/markers 过滤（pyproject 仅 testpaths）。+31 增量经 745cb04..0b3dbc8 逐文件核对属实（test 函数 815→846），老基线 collected 推导 921=920+1 闭合。**基线冻结（主口径）**：@0b3dbc8、venv 含 gui extra、Windows：collected=952 / passed=951 / skipped=1（唯一 skip 为 test_process_manager POSIX-only 场景，仍计入 collected）；**副口径（CI/无 gui extra，`pip install -e ".[dev]"`）**：collected=932 / passed=931 / skipped=2。基线引用必须携带环境条款，两口径不得混用。
- **C·mypy 存量重冻（本地参考值，CI 为权威）**：venv 装 mypy 2.3.1、Python 3.12.10（=CI py3.12 腿），`mypy subtransjav` 实测 **70 错/12 文件（checked 44）**；较 R1 的 61/11 多出 webview_gui/main.py 9 错，成因=本 venv 含 pywebview 真实类型使 create_window arg-type 显形，而 CI 只装 [dev] 不含 gui → **CI 口径仍以 61/11 为参照**。分批映射不受影响：M1 管线批 21 错两口径完全一致（pipeline_v2 10 / quality_report 4 / glossary_conflict 2 / cli 2 / post_validate 1 / pipeline_support 1 / language_validator 1）；M2 GUI 批=api 7+event_stream 2（无 gui 口径，含 gui 口径另加 main.py 9）；M3=process_manager 28+llm_client 3。基线文件机制（mypy-baseline.txt/tools/mypy_baseline.py/ci.yml 改造）未实施，留待 M 批次另立。
- **附带披露（既有债务，非本件引入）**：Mimosa 深扫全仓共 26 处既有静态告警（high 24 / low 2，verdictEffect 均为 none）：路径穿越为最大类（含 refine 核心 10 处：cli/filters/glossary_conflict/glossary/instructions/language_validator/manifest/quality_report/runlog/tm，另 api.py 2 处、tools 7 处），另有 SSRF 2、命令注入 1、SQL 注入 1、不安全随机数 2；model_matrix_run.py 内 4 处经 `git show HEAD` 对照核为既有代码（行号随 import 增行推移），非本次引入。处置不在本件范围，留待用户拍板（可并入 M 批或另立专项）。**L2 diff 锚定复查核实（Stop hook 触发）**：403/1069 两处锚点=两条 `open(log_path, ...)` 语句，与 HEAD（原 394/1061 行）逐字节一致、仅行尾新增 `# noqa: SIM115` 注释及上方两行原因注释（这正是 L2 把锚点落进本轮 hunk 的原因）；`log_path` 来源为操作者自身 CLI 参数链（run_pipeline/mida_run_pipeline 形参→Path()→mkdir），本地离线实验工具、无不可信输入面。判定：非本轮引入的真实新风险，按既有债务随上述 26 处专项统一处置，不在本件 spot-fix（避免与全仓 24 处同类告警双标）。

## [2026-09-23] [D2026-0923-01] 翻译性能优化列为下轮任务：引擎盘点先行+并发/投机分层推进（批次E并窗管理）[已拍板·已执行]（2026-09-25 状态字段更正，见 D2026-0925-01）

### 一、背景

- 当前生产搭配（D2026-0921-01 用户拍板）：A=joyfox27b（qwen3.8-27b-uncensored-joyfox-aggressive，27B dense）> B=heretic35b（qwen3.6-35b-a3b-uncensored-heretic-apex，35B-A3B MoE），引擎 LM Studio@localhost:1234（OpenAI 兼容）。管线只控制 `n_ctx=32768`（config.py:280）、批30条/请求、温度0.1、批间并发=1（config.py:279，上限5）。
- 端到端约 70 分钟/70分钟片：A 阶段 61~63 分钟（约88%、约92秒/批）+ B 阶段 6~7 分钟 + 秒级本地步骤；瓶颈=27B dense 解码。带宽算术：RTX 5060 Ti 16GB（448GB/s）÷ IQ3_M 约13.5GB 权重 → 单流解码上限约33 tok/s。
- **时效事实（2026-09-23 日志实录，本次评议新核验）**：`Logs/9-23.txt` 显示凌晨批次E 首片 ftkd-030 整片失败——01:57 起 LM Studio 侧模型被卸载（`400 Model unloaded by user or API request.`），后续批量报 `400 No engine protocol runtime is registered for 'adOfeX…'`，02:13 定向重试预算耗尽仍缺 1245 行逐行降级，最终 tmp 文件丢失（`tmpi4dqzn7i.srt.tmp` Errno 2）致"成功 0 / 失败 1"文件级失败；`Logs/9-23-1501.txt` 显示 15:01 起同片重跑（转录产物复用，1671 条）、15:04 进入阶段A，即批次 E 实跑中且引擎刚发生 runtime 事故。实测 A 阶段 56 批约 98 秒/批，折算有效解码约 5~8 tok/s，远低于带宽墙 33 tok/s，存在约 3~5 倍头寸——"当前严重低效（疑量化档部分 CPU offload 或调度损失）"为实测支持假设，非纯推测。
- 缺口存量：引擎级参数（实载 GGUF 量化档、GPU offload 层数、ctx、KV cache 量化、Flash Attention、并行请求数）全部只在 LM Studio GUI 内，仓内零记录（交接文档量化档标"Q?"，models/README.md 只记模型 ID）；D2026-0917-02-R1 条件⑦承诺的 E3 逐候选 tok/s 吞吐实测一直未闭环。
- 已拍板事项（本决策不重开）：joyfox>heretic 质量优先搭配（D2026-0921-01）；模型矩阵两轮已测完（docs/模型测试两轮交接.md）；IQ2 档投产否决线（D2026-0917-02 及 R1 修订：取消测量席但保留否决）。用户既定验收口径：每步单独变更 + 全片实测 + 质量抽检后才采纳。
- 用户排期修正（2026-09-23 拍板前已确认）：**不设"批次E结项后才可动 LM Studio"的硬门槛**——对模型的参数调整本身即测试行为，其对批次E 的相互影响（显存争用、对照污染、环境变更）作为本批任务内的调整项统一管理（错峰执行、每次变更记录在案、批次E 结果解读合并考量环境变更史），而非等结项。

### 二、决策问题

"翻译性能优化"排期与分层的成立性评议与拍板：①"参数调整即测试、不设批次E结项门槛"修正是否可行、风险能否靠错峰+记录兜住；②技术分层（并发/投机解码/量化盘点）有无事实性错误或更高性价比替代；③验证口径有无漏洞；④有无必须先解决的高风险前置。

### 三、decision-critic 评议记录（2026-09-23）

**材料核验**：决策日志全文、README、models/README.md、模型测试两轮交接.md、config.py、lmstudio.py、pipeline_v2.py、llm_client.py、manifest.py、Logs/9-23*.txt 全部核验通过。无 [MATERIAL_CONFLICT]；一处时效事实刷新（上文 ftkd-030 整片失败与重跑在途）。关键代码事实：v2_concurrency 在 manifest 指纹内（manifest.py:338，改并发即废 resume）；"每批约11k 上下文"源自 DEFAULT_TOKEN_BUDGET 预算上限（llm_client.py:36-42：overhead 2500 + 30×300 输入=11500），非实测值；cap_batch_size 随 n_ctx 收紧批大小（llm_client.py:45-52，n_ctx=16k 时批自动收紧至 27）——引擎与管线 ctx 必须同步改。

**立场**：有条件支持（5 条件）；异议级别=**[HIGH_RISK_OBJECTION-1] 一条 + 替代方案 A1-A4 + 普通级条件五项**。

**[HIGH_RISK_OBJECTION-1]（窗口与对照面保护）**：批次E 已产出的成片结果在引擎变更后无法回炉重验当初环境，且引擎刚发生 runtime 事故、原"阶段A 两次冻结同一位置"未定案，此时无边界约束地放开行为变更试验，会使批次E 结论解读与事故归因同时失真。要求：行为变更试验必须在批次E 片间边界/心跳确认空闲窗口内进行，禁止片中途切换；否则需声明替代归因纪律，不接受则该异议升级为"反对无条件放开试验窗口"。

**替代方案**：A1 量化落位提前到第0.5步（若证实 offload 先修量化+重测E3，以修复后基线评估并发，杜绝把 offload 修复收益错记到并发头上）；A2 E3 基准脚本化（解析 Logs 批耗时换算 tok/s 一键脚本）作为执行辅助件；A3 引擎级试验（小样+基准）与生产采纳（全片实测+抽检）分离，不同步、不等结项；A4 并发优先、投机殿后，投机与并行槽同开须单独验证，受限则只取并发。

**普通级条件五项**：①"质量无损"须有可测口径——固定一把非批次E样片，并发1/2 两档各跑一次，比对未翻译计数/恒等式/考点锚定，容差跑前预声明；②第0步 E3 口径对齐 D2026-0917-02-R1 条件⑦（逐候选 tok/s、有效 token/分钟、同卡同并发、LM Studio 实测）；③引擎与管线 ctx 两侧同步改并记录（llm_client cap_batch_size 联动，防静默截断）；④第0步产出三档配置显存数值账（层数×KV头×dim×ctx×槽×量化），取舍链 KV q8→q4 / ctx 16k→12k / draft 4B→1.7B→弃用；⑤每次引擎变更后必跑 E3 重测再判增益，"61→35"为预期非承诺。另记录执行注意项：并发变更废批次E 已有产物 resume。

**[INFO_GAP] 三项**：当前引擎六项实际值与版本（第0步盘点对象，不另索要）；"GUI 并行请求默认=2"官方文档未能证实（第0步 GUI 直接读取闭环）；投机解码在接受率观测上无 LM Studio 版本行为承诺（不可观测则以 tok/s 提升+同文件输出一致性为代理并标注）。

### 四、主模型最终决定（HRO 回应：采纳；拍板定版）

- **HRO-1 回应：采纳（完整采纳，非替代）**。行为变更（第1/2/3招生效性试验）只允许在批次E 片间边界/心跳确认空闲窗口内进行，禁止片中途切换引擎配置；违反即当次文件结果标"受污染"归档。批次E 重跑（含 9-23-1501 起的 ftkd-030 重跑）定性为引擎健康度重试观测，其结果与每次变更快照一并写入环境史，供事故归因与批次E 解读合并使用。
- **A1-A4 全部采纳**：A1 新增第0.5步量化落位；A2 E3 基准脚本化列为执行辅助件，"每次引擎变更前后必跑"；A3/A4 引擎级试验与生产采纳分离、并发优先投机殿后、draft 默认 1.7B（非4B）、投机与并行槽同开须单独验证。
- **普通级条件五项全部采纳**（含容差跑前预声明、E3 口径对齐条件⑦、ctx 两侧同步、显存数值账+取舍链、每步 E3 重测）；v2_concurrency 指纹/resume 影响写入执行注意项；最终量化档位回写 models/README.md。
- **拍板内容定版**：
  1. 性能优化列为下轮任务；**三层节奏**：引擎级小样试验/E3 基准随时可做（不设结项门槛）；行为变更生效性试验守片间窗口；生产采纳仍以全片实测+质量抽检为入口。
  2. 分层结构（顺序硬约束）：**第0步** 引擎盘点（六项实际值+引擎版本健康度+并行默认值+E3 基线+三档显存账+ctx 同步确认）→ **第0.5步** 量化落位（仅当盘点证实部分 offload/量化放不进 16GB；修复后重测 E3 作并发评估新基线）→ **第1招** 批间并发 1→2（v2_concurrency 现成开关，上限5，LM Studio 侧并行数≥2 同步开，重算 KV/ctx 预算，必要时 ctx 32k→16k；预期 A 阶段 61→约35分钟，以实测为准）→ **第2招** 投机解码（LM Studio 内置，同词表 Qwen3 小模型 draft，draft 默认 1.7B，tok/s 提升 <1.1x 视为无效，接受率不可观测则以提升+输出一致性为代理；备选同批 GGUF 用 llama.cpp CLI 直跑为引擎级动作先不启动）→ **第3层** 量化档复核（Q4→IQ3_M 全载则提速；已 IQ3_S/M 全载则无降档空间；IQ2 否决线不碰，档位回写 models/README.md）。
  3. 明确不做：换模型（已拍板搭配）、现阶段换推理引擎、砍 B 阶段。管线级备选（A 阶段噪音标记挪规则侧减输出 token）优先级最后、需改代码，并发/投机均落空时启用。
  4. 预期叠加效果：端到端 70 分钟 → 约 25~40 分钟（估算区间，非承诺）。
  5. 批次E 协同：试验与批次E 抢同一 GPU，守片间窗口错峰（避开实跑时段/心跳间隔内确认空闲）；每次 LM Studio 配置变更记录时间戳与内容（记录粒度=每批运行时段的引擎配置快照，与批次E 运行日志对齐）；批次E 结果解读合并考虑环境变更史。

### 五、条件闭环状态

决策层面已闭环（HRO-1 采纳、A1-A4 采纳、五项条件全落定、三层节奏与分层结构定版）。执行面未闭环，闭环路径=第0步盘点产出（六项实际值+引擎版本健康度+并行默认值+E3 基线+三档显存账+ctx 同步确认）→ 第0.5步量化落位（若证实 offload）→ 第1/2/3招逐个"E3 重测 + 片间窗口全片实测 + 质量抽检"验证，每步独立闭环；批次E 并窗管理贯穿全程（变更快照+受污染标记纪律）。

### 六、是否 [PRESSURE-OVERRIDE]

否。

### 七、后续风险跟踪

1. 引擎健康度未定案：9-23 ftkd-030 runtime 注册事故 + 原"阶段A 两点冻结"未结案，批次E 重跑即健康度重试观测，其结果与每次变更快照一并写入环境史；
2. 并发增益与 offload 修复收益分账：并发增益判定以第0.5步修复后 E3 基线为准，禁止重复记账；
3. 引擎/管线 ctx 不同步静默截断：两侧同步改并记录，cap_batch_size 联动（n_ctx=16k 时批自动收紧至27，无害）；恢复 32k 时同样两侧同步；
4. 显存超预算：第0步数值账 + nvidia-smi 实测兜底，取舍链 KV q8→q4 / ctx 16k→12k / draft 4B→1.7B→弃投；
5. draft 词表不兼容/spec 静默不生效：LM Studio 加载校验 + tok/s 提升 <1.1x 判无效；投机与并行槽同开行为单独验证；
6. "质量无损"口径：固定样片并发1/2 对照，容差跑前预声明，跑后不得改；
7. 并发变更废 resume：v2_concurrency 在 manifest 指纹内（manifest.py:338），中途改并发使批次E 已有 refine_A 产物指纹失效，执行注意项入变更记录；
8. E3 基准脚本（A2）落位为下一轮执行辅助件；第0步"GUI 并行请求默认=2"以实测值为准回写本日志；
9. 管线级备选（减输出 token）维持优先级最后，仅并发/投机均落空时立项（需代码，另立工单）。

### 八、决策日志字段

- **原决策**：翻译性能优化排期与分层（引擎盘点先行 + 并发/投机/量化分层推进，批次E 并窗管理；不设结项门槛为用户 2026-09-23 修正）。
- **我的异议**：[HIGH_RISK_OBJECTION-1] 一条（试验窗口与对照面保护，判定依据=影响≥3任务+批次E成片不可回炉重验）+ 替代方案 A1-A4 + 普通级条件五项 + 执行注意项一项。
- **主模型最终决定**：采纳（HRO-1 完整采纳；A1-A4 全采纳；五项条件全采纳；拍板内容定版如上）。
- **条件是否已闭环**：决策层面闭环；执行面未闭环（闭环路径见第五节，自第0步盘点起逐步勾验）。
- **是否 [PRESSURE-OVERRIDE]**：否。
- **后续风险跟踪**：见第七节，其中①引擎健康度观测与③ctx 两侧同步为本轮新增强调项。

**第0步盘点追记（2026-09-23 交互轮，用户提供 GUI 截图 + config.json/GGUF 核实）**：
- 引擎实值：量化=Q3_K_M（主文件 13.30GB+mmproj 0.93GB=GUI 口径 14.23GB，文件名 -no-mtp=MTP 头已剥离）；上下文=130048；GPU 卸载=50/64（**部分卸载实锤**，总层数 64 经 config.json 核实）；Max Concurrent=4；KV 量化未设（fp16）；FA=开；KV 卸 GPU=开；投机解码=Off（功能确认在）；物理批 512/评估批 2048；采样面板 top_k40/top_p0.95/min_p0.05/重复惩罚1.1 为 API 未指定参数的实际生效值（GUI 温度 0.2 被管线显式 0.1 覆盖）——记录在案不动，属质量面另走验证。
- 架构事实：**qwen3_5 混合架构**，64 层=48 linear_attention+16 full_attention（interval 4），GQA kv_heads=4/head_dim=256/vocab 248320/rope 1e7/eos 248044；KV 代价=64KB/token(fp16)；线性层常数态 ~151MB 与上下文无关。
- 显存账与诊断闭环：130k ctx 理论 KV≈8.5GB、总占 ~22.7GB≫16GB→部分卸载必然（50/64 即后果），解码被 DDR4 拖到 ~10-13 tok/s（修正 critic 口径 5~8 tok/s：其假设全部批耗时为解码，实际含 prefill）。
- 用户实测佐证：调整前载入专用显存 15.2/16.0 饱和、共享仅 0.2GB；按变更单调整后未回落至预测 14.4-14.6——三因素对账=①任务管理器含桌面/驱动基线 ~0.9GB（预测为模型净占用口径）②实际参数高于假设（ctx 22272/物理批 1024/双槽 KV 池）③mmproj 0.87GB 仍载。**关键目标已达成：64 层全载+KV 双 Q8_0+FA 开**；占用非目标，吞吐待跑批实测（基线 98s/批）。
- 变更单六项（用户已执行）：①ctx→22272（用户自选值，管线侧跑批带 --v2-ctx 22272；自动化落地后引擎 ctx 由 --v2-ctx 强制对齐）②K/V=Q8_0 ③卸载=64 ④MaxConcurrent=2 ⑤物理批=1024 ⑥采样面板不动。
- 附带现场：15:01 重跑驱动进程已消失（GPU 空载/LM Studio 无在载模型/日志 15:04 后零行零报错），BatchE_Run 计划任务已不存在（schtasks 找不到指定文件）——静默死因与凌晨 exit 137 宿主击杀同模式嫌疑，重跑启动方式待用户确认；另检出双 GUI 实例并行（.venv 与 G:\python 各一，与测试用 venv 常设规则冲突）。

**第2招配套工程执行追记（2026-09-23 交互轮，用户指令"路线一 PASS，要项目运行时自动化加载卸载模型"）**：
- 调研修正：`ensure_lmstudio_model` 原为**死代码**（仓内零调用点）；真实接线点=pipeline_v2 `_make_client`（剧情摘要/阶段A/阶段B 三处）与 `_make_fallback_client`（本地接管）。
- 实现（**commit 92805cd**，10 文件 +378/-27）：①`utils/lmstudio.py` ensure_lmstudio_model 升级——对齐判定=未在载 / 已载 ctx 与 v2_ctx_local 不符（/api/v0/models loaded_context_length，字段缺失容错不误判重载）/配置 draft 但 lms ps 无 spec/draft 痕迹（best-effort，解析不可得视为已生效防重载循环）；对齐动作=`unload --all` 清场→`lms load -y --gpu max -c <v2_ctx_local> --parallel <v2_concurrency> [--speculative-draft-simple --speculative-draft-model <id>]`；draft 未下载自动降级不挂（质量无损）。引擎参数以管线配置为唯一事实来源，ctx/parallel 两侧同步由构造保证（落地条件③，根治引擎参数 GUI 零记录）。②pipeline_v2 新增 `_ensure_lmstudio_engine`（独立函数可测试打桩），三处接线、引擎未就绪 raise RefineError 快速失败。③StageConfig.engine_draft_model（默认空=不挂）+ CLI `--s1/s3-draft-model`。④manifest `_STAGE_FIELDS` 收录 engine_draft_model（换 draft 即失效旧产物须重跑）。
- 验证：**963 passed+1 skipped**（基线 951+1+新增 12，含 test_lmstudio_engine.py 10 例 fake 服务端状态迁移用例）/ ruff 全绿 / --help 冒烟通过；test_config_layering 两例直调 _make_client 测试已打桩引擎对齐（CI 无 LM Studio 不触网）。
- 提交按 D2026-0921-03 标准运行模式执行：暂存 diff 10 类 secret 模式零命中 → 终端直提（ZCode Bash 内 commit/push 被 L3 拦截=预期行为，27 高危全为 D2026-0922-03 已归档历史误报、与本次提交零交集）→ 三查过（porcelain 仅剩往轮 decision-log 追记与 .zcodeignore / 单段提交 / blobs 复扫仅提交哈希自身命中）→ 提交前后 Mimosa 双深扫均 26 findings=基线零新增（离线 advisory 命中 1 未升格 finding，本提交无依赖变更）→ push `9074832..92805cd` → ls-remote 复核 origin/main=92805cd 完全同步。
- 使用方式与遗留：下载 Qwen3.5-0.8B 后 `--s1-draft-model <完整模型ID>` 即全自动挂 draft；生产默认 draft ID 待 E3 实测通过后再入库（沿 D2026-0921-01 模式）；llm_client 请求期 400 "Model unloaded" 不在瞬态重试集合（ftkd-030 事故暴露面），是否纳入自动重载另行议。

**GUI 生产化追记（2026-09-23 交互轮，用户要求"GUI 选择 draft、开始运行后全自动，设计必须考虑普通用户场景"，CLI 参数不能成为用户必经之路）**：
- 实现（**commit 0c5adcc**，4 文件 +102/-1）：①index.html：阶段A/B 各增"投机解码 draft"下拉（默认"不挂 draft"，tooltip 说明同词表要求与质量无损语义）；灰参数行增"上下文窗口"数字输入（默认 22272、min 4096，tooltip 说明管线自动对齐引擎与 16GB 建议区间 16384~22272）。②app.js：buildRefineOptions 透传 v2_ctx/s1|s3_draft_model；readRefineCtx 兜底读取；refreshDraftModels 页面加载自动经 list_local_models 填充已下载全集（保留已选值）；设置保存/回填含三新字段（重启后选择不丢）。③api.py `_build_refine_args` 透传 `--s1/s3-draft-model` 与 `--v2-ctx`（空值不传旗标）。
- 普通用户路径闭环：**GUI 选 draft（可留空）→ 点开始 → 引擎加载/卸载/draft 挂载/槽位切换全自动**，全程无需 CLI 与 LM Studio GUI 操作。
- 验证：965 passed+1 skipped（新增 _build_refine_args draft/ctx 透传与缺省不传 2 例）/ ruff 全绿 / node --check app.js 语法通过；提交按 D2026-0921-03 标准模式（预扫描 26=基线 → secret 零命中 → 终端直提 → 三查 → 复扫 26=基线 → push `92805cd..0c5adcc` → ls-remote 复核同步）。

**draft 挂载缺陷修复追记（2026-09-23 实测轮，用户实测发现"GUI 已配置 draft 但 LM Studio 仅 27B 在载"而定性代码问题——定性正确）**：
- 定位证据：运行中子进程命令行含 `--s1-draft-model qwen3.5-0.8b-heretic@q6_k`（GUI→CLI 参数桥正常）；运行日志仅"模型已加载"无重载；`lms ps --json` 实测条目字段为 `modelKey/identifier`（**无 id 字段**），而 `_spec_draft_active` 按 `id` 匹配→永远找不到条目→按"无法判定视为已生效"放行→不重载、不挂 draft。根因=schema 无承诺下的探测字段错配；教训：**"无法判定"的缺省方向必须指向可观测验证（重载），而非静默放行**。
- 修复（**commit 44ab09e**，2 文件 +88/-24）：①改为 `_spec_draft_trace` 三态判定（True=确认已挂/False=确认未挂/None=无法判定），条目按 modelKey/identifier/id/path/displayName 任一包含匹配，未找到或 ps 不可用返回 None（不据此放行）；②新增 `_DRAFT_LOADED` 进程内缓存——本进程以 draft 旗标加载成功即记忆，重载至多每进程一次，杜绝 ps 探测能力未知导致的重复重载循环；配置改回不挂 draft 时清缓存防陈旧。③测试 +3（真实 schema 回归/缓存命中/缓存清除），fixture 隔离进程内缓存。
- 验证：968 passed+1 skipped / ruff 全绿；提交按标准模式（预扫描 26=基线 → secret 零命中 → 终端直提 → 三查 → blobs 真实机密模式零命中 → 复扫 26=基线 → push `0c5adcc..44ab09e` → ls-remote 复核同步）。
- 附带运维事件：实测中发现**两个相同 refine.cli 进程并发跑同一文件同一输出目录**（.venv 与 G:\python 双 GUI 实例各启动一次；tmp 互相踩踏风险与凌晨"tmp 丢失"文件级失败同款）——已终止 G:\python 重复进程（PID 27432），保留 .venv 进程（PID 29340）继续无 draft 基线跑（该进程内存中为旧代码，恰作无 draft 基线有效）；双 GUI 实例必须只留一个（再次提醒未消除）。
- 生效条件：正在跑的基线批不受影响、也无法中途获得 draft；**下一批起（新进程）自动生效**——joyfox 已在载时亦会因"配置 draft 但确认未挂"而自动重载挂载，日志出现"引擎对齐: ... draft=..."行即成功；若 LM Studio ps 始终不暴露 draft 痕迹，进程内缓存保证同批次后续文件不重复重载。

**draft 实测裁决追记（2026-09-23 深夜，受控 A/B 基准跑通并裁定）**：
- 修复生效实证：基准 B 启动后日志出现 `⏳ 引擎对齐: 加载 joyfox (ctx=22272, parallel=2, gpu=max, draft=qwen3.5-0.8b-heretic)`——已载无 draft 状态下自动重载挂载，44ab09e 修复逻辑实战验证通过。
- 基准口径：ftkd-030 同片（1671 条/56 批）、ctx=22272、并发=2、--no-tm --force、独立输出目录（_AB基准/），唯一变量=draft（qwen3.5-0.8b-heretic Q6_K）。
- **结果：不挂 draft 阶段A = 13.3 分**（22:40:52→22:54:10，~13.5s/批，零错误）；**挂载 draft 阶段A = 55.2 分**（17:20 完整挂载运行 17:20:48→18:15:58，另经 23:0x 复测确认远慢于基准 A）——draft 致 **4.15 倍减速**：0.8B heretic 与 joyfox 输出分布几乎不重叠，接受率近零，draft 前向纯亏。
- **裁决（依本决策 ≥1.1x 判据）：投机解码不采用**；GUI draft 下拉应切回"（不挂 draft）"。draft 基建（自动挂载/切换/降级/@quant 解析）保留，未来候选零成本可测。
- **性能定版：阶段A 13.3 分 + 阶段B ~8 分 ≈ 21.5 分/部**（对比 9-21 基线 68.2 分 → **3.2 倍**），达成并超出本决策 30±10 分钟目标。收益构成=64 层全载（根治 130k ctx 过配致部分卸载，大头）+ 批间并发 2；非 draft 贡献。
- 运维误诊纠正：`G:\python\python.exe` 子进程=venv launcher（.venv python.exe，CPU≈0）拉起的真实工作进程，**并非重复运行**；17:15 对 27432 的击杀实为误杀 17:04 draft 运行本体（用户"上一批中止"的实际原因）。教训：进程级诊断必须核对父子链与 CPU 时间，可执行路径≠真实身份来源。
- 事故源处置：BatchE_WD 计划任务（内嵌阶段B=gemma 非生产配置）为反复重 spawn 源头，已禁用；**恢复与否待用户裁决**（其队列与基准产物已重叠，建议废弃另立）。@quant 后缀容错已修并推送（**b8a2526**）。

## [2026-09-24] [D2026-0924-01] 批次1 引擎自动化回归锁——并发/GPU 对齐缺口的处置 [已拍板·已执行]

### 一、背景与决策问题

- 已定版排班（用户 2026-09-24 拍板）：批次 1 P0「引擎自动化回归锁」。**验收原文**：「测试覆盖：未载 / ctx 与 v2_ctx_local 不符 / 并发不符 / GPU 不符 → unload --all → lms load -y --gpu max -c <ctx> --parallel <并发>；断言重载命令不含任何 speculative 旗标」。
- **代码现实**（检索确认 + decision-critic 只读复核）：`subtransjav/utils/lmstudio.py` 的 ensure_lmstudio_model 对齐判定仅三项——①模型未载（need_load）②ctx 不符（_loaded_ctx 走 /api/v0/models，读不到返回 0 则放过不误判）③draft 无法确认（随 draft 移除删除）。**并发（--parallel）与 GPU（--gpu max）只在重载发生时作为加载参数拼进命令，不存在失配检测**——仅改并发设置不触发重载。
- **实时探测**（2026-09-24，`lms ps --json`，引擎 idle）：`"contextLength":22272, "parallel":2, "status":"idle", "deviceIdentifier":null`——ctx 已可检测；并发可检测（需新建 `_loaded_parallel()`，主包现无 ps 调用，属从零重建）；**GPU offload 无任何检测字段**（另经 `/api/v0/models` 已载模型完整 JSON 只读核验：仅 loaded_context_length/max_context_length/state 等键，无 device/offload/gpu 字段，两通道均无数据源）。
- **排班约束**：生产配置锁定 ctx 22272/并发 2/GPU max/KV Q8_0/物理批 1024/无 draft；HRO-1 行为变更须在两部影片之间的窗口部署；3.2 倍收益本体 = 64 层 GPU 全载 + 并发 2，防止回退是本批次动机。
- **候选**：A 补齐并发检测 + GPU 构造性保证；B 仅锁现状、缺口交用户排班外裁决；C 逆向/实测强行 GPU 检测。

### 二、decision-critic 评议结论与异议清单

评议立场：**有条件支持 A（修正案 A′）**；B 仅作用户显式缩小验收口径后的回退档；C 反对。异议清单（按严重度）：

1. **[HIGH_RISK_OBJECTION] 验收口径披露义务**：无论 A/B，未向用户显式披露「验收原文 vs 代码现实差异」即交付，即与既有共识（用户拍板验收原文）冲突；静默交付会让用户误以为 GPU/并发漂移有护栏而防回退动机落空。
2. **普通（高严重度）反对 C**：两处已验证无失配数据源；逆向未知字段或对 idle 引擎做加载实测属高不确定破坏性探索（反复 unload/load、lms 版本升级即碎、延误窗口）；收益仅把 GPU 从命令断言升级为运行态检测，而运行态 GPU 漂移已由重载命令构造性闭环。若坚持采纳 C 则升级为 [HIGH_RISK_OBJECTION]，前置条件是先做 `/api/v0/models` 全字段盘点。
3. **普通：A 的引擎 clamp 暴露面**——parallel 请求值被引擎静默钳制（报告值≠请求值）可致每次启动多一次重载（跨启动、非循环）；概率低（生产锁定值 2 远离钳制上限，探测基准实值已一致），缓解见裁定 A′ 第 5 条。
4. **[INFO_GAP]（已闭环）**：a) 并发是否存在片级/任务级合法覆盖路径；b) ensure 调用时点是否严格在两部影片间隙。
5. **普通：fail-open 判定静默退化风险**——ctx 现有 0=放过路径无日志，检测退化不可见；A 实现时补齐可见性。

容错边界六分支（防误重载/漏重载，全部须单测覆盖）：ps 输出非 200/JSON 解析失败、字段缺失、值为 0、非整数（null/字符串/布尔）、ps 超时 → **一律跳过判定 + warning 日志，绝不触发重载**（"查不到"≠"失配"，防每启必重载自我炸弹）；合法整数 >0 且 ≠ 目标值 → 真失配，走统一重载路径；重载后复核仍失配 → 本进程不再二次重载 + 警告。

### 三、主模型回应（全部采纳）

- 异议 1 [HIGH_RISK_OBJECTION]：**采纳**。披露落点 = 批次 1 提交信息 verify 段 + 本决策日志条目 + 完成通知向用户显式对照（并发=真实检测；GPU=无失配检测数据源，以构造性保证+命令断言近似）；验收按调整后口径请用户确认，**不标注"已满足验收原文"**。
- 异议 2：**采纳，C 驳回**——不逆向、不做破坏性加载实测。
- 异议 3：**采纳缓解**——容错全 fail-open、重载后复核仍失配仅警告不二次重载（ensure 每启至多一次，天然无环）。
- 异议 4a：**查证闭环**——无片级/任务级并发覆盖路径，v2_concurrency 为全局单源（GUI 输入框 / CLI 参数 / config 默认），配置值即期望值；引擎侧手动改值属漂移、应被重载纠正（与 ctx 同语义），**无需豁免通道**。
- 异议 4b：**查证闭环**——调用点为 `pipeline_v2._make_client` 与 `_make_fallback_client`（管线启动/回退建客户端时），与 ctx_mismatch 同一生命周期点，**非新增行为类别**；每启至多一次重载，无循环风险。
- 异议 5：**采纳**——parallel 未知跳过路径与既有 ctx 读到 0 的跳过路径均补 warning 日志。
- 窗口裁决（Q3-b）：**采纳**——见第五节。

### 四、裁定 A′（全文）

1. **新增并发对齐判定**：重建干净 `_loaded_parallel()`，重载前从 `lms ps --json` 读顶层 `parallel` 实值并比对 v2_concurrency。
2. **容错边界（六分支，按第二节落地）**：字段缺失 / 值为 0 / 非整数 / 超时 / 解析失败 → 跳过判定（fail-open，偏漏检不误重载）+ warning 日志；合法整数 >0 且 ≠ 目标 → 统一重载路径（`unload --all` → `lms load -y --gpu max -c <ctx> --parallel <并发>`）；重载后复核仍失配（引擎 clamp/不兑现）→ 警告，不二次重载。目标值与拼装 load 命令同一 `parallel` 参数单源。
3. **GPU 维持构造性保证 + 命令断言**：每次重载命令必带 `--gpu max` + 测试断言锁死；**不新增 GPU 失配检测**（两通道实测无数据源）。
4. **实施顺序**：先完成 draft 移除重构，在干净基线上叠加 `_loaded_parallel()`，避免重叠区双改动互相打架。
5. **可观测性**：parallel 与 ctx 的所有跳过路径落 warning 日志，防 fail-open 判定静默退化。

### 五、窗口裁决（Q3-b）

**parallel 判定不构成需要独立守窗口的行为变更，部署随批次 1 整体在两部影片之间的窗口内进行，不做 detection-only 观察窗。** 理由：①不改变加载参数（命令串与现状同构）；②触发时点与 ctx_mismatch 完全同构（管线启动/回退建客户端时的自愈判定，同一生命周期点、同一动作序列）；③检测的是既存漂移态，不向运行中的影片处理管线注入变更；④不侵入处理循环（每启至多一次，非每批循环）。

### 六、验收口径披露段（验收原文 vs 调整后口径）

- **未载 / ctx 不符**：真实检测，验收原文字面满足。
- **并发不符**：按 A′ 真实检测（合法整数 >0 且 ≠ v2_concurrency 即重载）；0/缺失/非法/超时容错跳过为已接受限制（漏检由任何重载命令必带 `--parallel` 兜底）。
- **GPU 不符**：**无失配检测数据源**（`lms ps --json` 与 `/api/v0/models` 两通道均无 offload/GPU 字段），以"构造性保证 + 命令断言"近似——重载命令必带 `--gpu max` 且测试断言不含 speculative 旗标；验收原文中"GPU 不符 → 重载"的运行态检测场景**不成立**，验收按此调整后口径确认。
- 以上差异已在批次 1 提交信息 verify 段、本条目、完成通知三处显式对照披露；未经用户按调整后口径确认前，不标注"已满足验收原文"。

### 七、执行留痕（2026-09-24 批次 1/2 完成回填）

- **draft 彻底移除（commit af93ce8，14 文件 +34/−298）**：GUI 下拉与存取回填/refreshDraftModels、CLI `--s1/s3-draft-model`、StageConfig.engine_draft_model、manifest 指纹字段、api 透传、ensure 的 draft 降级/@quant 解析/ps 探测（_spec_draft_trace/_has_draft_trace/_DRAFT_LOADED）与 speculative 旗标全链路清除；引擎自动化核心（未载/ctx 判定、unload --all、-y --gpu/-c/--parallel 构造、复核）完整保留。验证：ruff 绿、定向 77 passed、全量 **960 passed + 1 skipped**（969+1 移除 9 例 draft 测试）、冒烟 CLI --help/GUI 启动过、双深扫 26=基线（pre scan-…18-07-22、post scan-…18-12-12）、重载命令含"无 speculative 旗标"回归锁。GUI 验证=静态渲染层（draft 控件消失/ctx 输入 22272 在位/JS-ID 交叉零悬空）+ 启动冒烟；pywebview 桥接交互未做浏览器黑盒（原生窗口专属）。
- **并发对齐判定（commit be83c25，4 文件 +223/−13）**：`_loaded_parallel()` + 三项对齐判定 + 六分支 fail-open + 重载后复核不循环 + ctx 跳过告警；新增 7 例测试含真实 ps schema fixture。验证：ruff 绿、定向 14 passed、全量 **967 passed + 1 skipped**（只增不减 +7）、冒烟过、预扫描 26=基线（scan-…18-18-45）、提交后复扫 26=基线（scan-…18-21-07）。
- **批次 2 运维裁决**：BatchE_WD 计划任务**在本机已不存在**（C:\Windows\System32\Tasks 与 schtasks 全量 verbose 均无命中；判断为已被删除，非仅禁用——与 batch-e-test-20260922 记忆"BatchE_Run 计划任务已不存在"一致），无需 enable/Disable 处置，废弃另立生产任务的建议保持；恢复命令备查 `schtasks /change /tn BatchE_WD /enable`（现状不可用，任务本体已无）。双 GUI 实例：核查时点零 python/GUI 进程，无双实例并发；G:\python 侧 GUI 未在运行，收敛达成（.venv 为唯一入口）。【2026-09-25 更正（D2026-0925-01 MC-1）：本条"已不存在"系 2026-09-24 上午时点记录——当日午后任务经用户重建后禁用；2026-09-25 实测任务存在且 Disabled（上次运行 2026-09-23 22:26，结果 0xC0000005 访问违规，与 9-23 深夜 draft 事故夜吻合，属已归档事故现场留痕）；删除前须先 `schtasks /query /xml` 导出留痕（D2026-0925-01 组A·A6）。】
- **决策日志字段（decision-critic 协议）**：异议 1 为 [HIGH_RISK_OBJECTION]，主模型已明确采纳回应，异议保留、执行层面服从，无二次复议；无 [PRESSURE-OVERRIDE]。后续风险跟踪：①引擎对 --parallel 静默钳制（重载后复核告警观测）；②fail-open 判定随 lms 升级静默退化（跳过告警 + 真实 schema fixture 兜底）；③实施后首跑若意外重载，回退=撤 _loaded_parallel 判定、保留命令断言；④测试基线 967 passed + 1 skipped 只增不减。

## [2026-09-24] [D2026-0924-02] llm_client 请求期 400 "Model unloaded" 自动恢复 [已拍板·已执行]

### 一、背景
ftkd-030 事故（2026-09-23 02:13）：跑批中 LM Studio 引擎被卸载，后续请求全部 `400 - {'error': 'Model unloaded by user or API request.'}`；llm_client `_TRANSIENT_STATUS={408,429,500,502,503,504}` 不含 400 → 不重试 → 批级兜底退化为整文件 [未翻译] 降级。定版排班批次 5 验收目标：「不再因 400 中断」。

### 二、decision-critic 评议与主模型回应（全部采纳）
- **[HIGH_RISK_OBJECTION] 1（实现点失效）**：原设计「pipeline_v2 捕获 LLMError 后调 ensure」不可达——llm_client 批循环 `except Exception` 吞异常只记日志，LLMError 永不冒泡至管线。**采纳修正**：恢复点改为 llm_client 批循环回调注入（`unloaded_recovery` 参数，pipeline 构建客户端时传 `_ensure_lmstudio_engine` 同参闭包），llm_client 保持引擎无关，异常冒泡契约不变。
- **[HIGH_RISK_OBJECTION] 2（并发竞态）**：v2_concurrency=2 时双批在途（ThreadPoolExecutor），双 400 → 双 ensure 竞态；`unload --all` 会打断另一在途批（其行由缺行定向重试吸收，可恢复）。**采纳修正**：recovery_lock + 每实例恢复信用至多 1 次；等待线程拿锁后信用=0 直接走现状路径；上限 1/客户端实例、本地文件并行已被 `_file_parallel_enabled` 排除。
- 异议 3（误伤面）：匹配稳定长串 `model unloaded by user`（大小写不敏感），云端 "model unloaded due to inactivity" 等不命中。采纳。
- 异议 4（指纹一致性）：ensure 与批处理 client 同源 cfg.v2_ctx_local/v2_concurrency，无增量。查证闭环。

### 三、裁定与执行留痕
- llm_client：`ModelUnloadedError(LLMError)` + 特征串判定 `_is_model_unloaded`（在瞬态判定之前抛出，不进 5s 退避）；批循环捕获 → 持锁 → 信用检查 → 同步回调（ensure 幂等对齐，未载自动重载 60-120s）→ 整批重试一次；信用耗尽/回调异常 → 现状路径（批失败→缺行定向重试→降级），异常契约不变。
- pipeline_v2：仅 lmstudio provider 注入（_make_client 与 _make_fallback_client 两处，与首载完全同参）；云端不受影响。
- 测试 +8：类型判定/长串不误判/不进瞬态/批级恢复/并发双 400 单次恢复/回调异常回退/普通 400 不触发/仅 lmstudio 注入。
- **verify：ruff 绿；全量 987 passed + 1 skipped**（合并基线：969+1 → +7 E3 → +3 v2-ctx → +8 本决策，只增不减；本地 .venv 口径，与 CI 同解释器）；Mimosa 预扫描+提交后复扫 26 findings=基线零新增。提交 1f83c8c（含 e3-benchmark-script b8d42b5、v2-ctx-default-fix db0fa1c 同窗）。
- 口径补记：af93ce8/be83c25 提交信息中的基线数（960/967）均为本地 .venv 口径（与 CI 同解释器），本条为 D2026-0922-01 环境条款要求的补记。
- 后续风险跟踪：①引擎反复被外部卸载时每客户端实例仅自动恢复 1 次（设计上限，防循环），第二次依赖现状路径降级——若生产出现高频卸载场景再议上调；②unload 打断在途批的缺行重试有额外请求成本（低频路径，可接受）。

### 九、验收确认追记（2026-09-24 用户批复，状态转验收通过）

- **状态变更**：[已拍板·已执行] → [已执行·验收通过（调整后口径，2026-09-24 用户确认）]。
- **用户批复要点**：并发对齐检测确认不回退（fail-open 六分支为正确工程取向：宁可漏重载绝不误重载，误重载中断生产跑批、漏重载仅参数短暂不符且有 ctx 对齐兜底）；GPU 构造性保证确认不回退（无运行态数据源是事实，不为凑排班字面伪造检测；--gpu max 为唯一生产路径 + 断言锁死 ≈ 永远全载，且部分卸载会先被 ctx 对齐与专用显存回落验收拦截）；**回退方案（撤 _loaded_parallel 判定）正式作废**。
- **验收条文正式改写（以此为准）**：
  1. 未载 / ctx 与 v2_ctx_local 不符 → unload --all + lms load -y --gpu max -c <ctx> --parallel <并发>；
  2. 并发不符 → 真实检测（lms ps --json parallel 字段），fail-open 容错，绝不误重载；
  3. GPU 失配 → 无运行态数据源，以「每次重载必带 --gpu max + 测试断言」构造性保证；
  4. 重载命令断言不含任何 speculative 旗标。
- 批次 5 确认：v2-ctx 缺省 22272 正确；400 自动恢复为本批最有价值健壮性提升（ftkd-030 暴露面闭合）。

## [2026-09-24] [D2026-0924-03] E3 基准脚本化口径警示与干净校准待办 [已拍板·校准已完成]（2026-09-25 状态字段更正，见 D2026-0925-01）

- **背景**：tools/e3_benchmark.py（b8d42b5）对 Logs/9-23.txt 全天聚合输出「阶段A 0.31 条/秒、总 01:34:53」，该日志含当日 draft 实验样本（4.15 倍减速运行、多次重载），**0.31 条/秒非生产口径，禁止作为基线引用**。
- **裁定（用户 2026-09-24 批复）**：生产定版基准仍为无 draft 配置实测 **阶段A ~13.3 分 + 阶段B ~8 分 ≈ 21.5 分/部**；干净 E3 校准（无 draft 生产配置实跑一次，覆盖旧口径读数）列为下轮首项，已纳入闲时任务。
- **执行口径**：校准优先复用 9-23 深夜干净运行的同源输入（Logs/9-23 22:40 窗口对应片源，保证可比性）；跑前核对引擎空闲（不得打断任何在途跑批）；跑后 e3_benchmark 解析新日志与 21.5 分/部比对，结果回填本条目并同步记忆；使用 .venv 解释器与生产锁定参数（ctx 22272/并发 2/GPU max/KV 双 Q8_0/批 1024/无 draft）。
- **遗留可议项（非阻塞）**：v2-ctx GUI/CLI 强联动（当前缺省修复已闭环 GUI 透传，CLI 靠缺省 22272；强联动收益待议）。
- 本条目及 D2026-0924-01 验收追记随下轮提交入库（用户指定留工作区）。

### 校准执行回填（2026-09-24 12:08–12:32 闲时改即时执行，用户指示"取消闲时任务，现在开始"）

- **执行方式**：原闲时任务 offpeak-f5a9503a（队列 274 位）经用户指示取消改为即时执行；输入原 `Temp/bench_input.srt`（118,926B/1678 条）已被 Temp 清理策略删除且全盘无副本，按本条目预留的降级口径改用**两份真实 ASR 电影转写拼接**（deaf2cbb… 1181 条 + 6646782b… 1036 条，时间轴偏移拼接 = `Temp/bench_input_e3cal.srt`，2217 条/139,660B/sha1 00c54e79…），样本密度略低于原基准（58.7 vs 70.9 B/条），故以**条/秒吞吐与每批秒数归一化**为可比口径。
- **实跑数字（Logs/9-24.txt，e3_benchmark 口径）**：阶段 A = 83 批、均值 12.1s/批、total 16:45、**2.06 条/秒**；阶段 B（heretic-apex 换载后）= 83 批、均值 4.5s/批、total 06:11、**5.58 条/秒**；总时长 23:03（脚本口径）/24m12s（运行摘要含前后处理）；错误 0、故障接管 0。
- **与基线对照（Logs/9-23-2240.txt）**：阶段 A 吞吐 2.06 vs 2.12 条/秒（**−2.8%，持平**）；阶段 B 5.58 vs 3.49 条/秒（拼接样本中文条均更短，不慢于基线）。**结论：无 draft 生产配置下 21.5 分/部复现成立（吞吐口径 ±3%），0.31 条/秒正式作废**。
- **附带验证**：引擎自动化从"已卸载"状态自动拉起 27B 并对齐（ctx 22272/并发 2 实测无误），阶段 B 自动换载 35B heretic-apex——批次 1 交付的对齐自动化在生产路径自证；400 自动恢复未被触发（无卸载事故，符合预期）。跑毕引擎驻留 35B（下次跑批阶段 A 会自动换回 27B，无需人工干预）。
- 校准输入与产物保留于 Temp/（7 天清理策略内），过期后仅存本回填记录。

## [2026-09-24] [D2026-0924-04] TM 默认路径迁出随 1.3.0 + 批次 E 实弹测试终止 [已拍板·归档]

**评议方式**：decision-critic 评议发起后由用户取消，本条为用户直接终选归档（不另加评）。

**拍板一（TM 默认路径迁出 Temp，随 1.3.0 架构版执行）**：
- 背景：生产 TM 库位于 `Temp/translation_memory/tm.db`（tm.py:26 `_DEFAULT_TM_DIR` 硬编码），目录名 Temp 诱导误清、固化学习无机制保障（仅靠无人清扫+手工 .bak 封存惯例，现存 9-16/18/21 三代备份）；当前库 1872 条（2026-09-23/24 生产跑与校准轮写入）。
- 裁定：默认路径迁出 Temp（落位仓库根专用目录或用户目录，动工时定），改 tm.py 一处默认值 + 现库一次性迁移 + 路径断言测试跟随；纳入 1.3.0 架构版（config 分层工作流），否决独立先行小修。
- **附带口径更正**：本日志 992/996 行"Temp 7 天清理策略"系**误归因**——项目唯一自动清理为 `runlog.cleanup_old_logs`，仅扫 Logs/（.txt/.log、7 天、非递归，见使用与维护手册.md:243）；Temp/ 无任何清理机制（仓库代码零实现、计划任务无清扫条目、Storage Sense 只扫系统 %TEMP%），`Temp/bench_input.srt` 消失的真实原因未查明。实证：TM 目录内 9-16/17/18/21 多代文件至 9-24 全部健在。此后涉 Temp/ 产物存亡的判断以此为准。

**拍板二（批次 E 实弹测试终止，不再重跑）**：
- 理由（用户 2026-09-24）：全流程上下游默认选择已经挂载测试定版——上游转录 F06（D2026-0917-03）、下游 refine 两槽 joyfox27b/heretic35b、ctx 22272/并发 2/引擎自动化（Logs/9-24.txt 从卸载态自动拉起+换载实测自证）、干净 E3 校准 21.5 分/部零错误复现；再跑一轮无增量意义。
- 效力：**推翻 D2026-0922-01 硬依赖①与 D2026-0922-03 HRO-3"批次 E 卡拆分动工"两处条款，1.3.0 pipeline_v2 拆分卡点解除**；8 项指标不再在 v1.2.4 参数（ctx 22272/无 draft/400 恢复）上补采，上轮"四次测试"完成记录（用户 2026-09-24 确认，验收材料曾存 E:\无字幕\新建文件夹\四次测试\_批次E验收\）为最终结项口径。
- 产物现状（用户同日告知）：批次 E 目录中 final_cn.srt 与质量报告.txt 已由用户清理。
- 对拆分验收门（HRO-2）影响评估：无实质影响——行为等价快照在拆分时点现拍（同真实输入+固定 FakeClient+**空 TM**+固定词表，重构前后各跑一次逐字节 diff），不依赖批次 E 历史输出产物；所需 3-5 个真实输入为片源转写，**拆分动工前须对 E 盘材料现状做一次留存确认**（用户掌握，缺失则届时以现有真实 ASR 转写补位）。
- 批次 E 条目就此关闭，全链闭环：5 部转录完成 → ftkd-030 refine 400 失败（LM Studio 卸载）→ 重跑环境死亡 → 用户四次测试完成 → 本条终止重跑。（该失败暴露面已由 D2026-0924-02 的 400 自动恢复闭合。）

### GUI 全量实测回填（2026-09-24 16:31–17:52，用户人工 GUI 发起，Logs/9-24-1631.txt）

- **结果**：5 部全量 AB 两阶段**全部成功**，运行摘要耗时 **1h20m58s**，错误 0 / 故障接管 0 / 警告 22（20 条=并发/ctx 检测 fail-open 跳过，设计内；2 条=词表误译待复核 #499 climax_iku_variant，已进冲突观察）。
- **逐部耗时（纯阶段口径）**：ftkd-030 18:14 / hsoda-106 12:47 / hsoda-114 17:18 / jur-531 14:04 / jur-550 14:47，均部 **~16.2 分**，较 21.5 分/部基线快 ~25%（阶段B 均值 4:22 vs 基线 ~8 分为提速大头；阶段A 含 22 次缺行定向重试+5 次响应异常重试，全部首轮恢复、无预算耗尽）。
- **健壮性自证**：TM 从零积累 **6382 条**（写穿式学习正常）；引擎自动化全程 12+ 次"引擎就绪"换载（27B↔35B 交替）零失败，lms ps 实证 ctx 22272/并发 2 全程对齐；**400 自动恢复未触发**（全程无外部卸载，符合预期）。
- **产物**：5× final_cn.srt + 5× 质量报告.txt 落位 `E:\无字幕\新建文件夹\四次测试\_批次E验收\`；glossary_conflict_watch.json 重建。
- **定性**：本回填为执行事实记录；测试验收裁定权在用户。

### 留存回填（2026-09-25，承 D2026-0925-01 组A·A2）

- **留存确认完成**：拆分验收 golden 输入=四次测试全部 5 部真实 ASR 转写（ftkd-030/hsoda-106/hsoda-114/jur-531/jur-550，`4k2.me@<片名>.ja.merged.whisperjav.srt`），已复制至项目树外 `D:\SubTransJAV-internal-archive\hro2-golden-inputs-20260925\`，sha256 双侧（源=副本）逐文件一致；清单与校验值见该目录 `_留存清单.txt`（源=E:\无字幕\新建文件夹\四次测试\_批次E验收\，2026-09-25 实测 36 文件在位）。另 7 部真实转写存于 E:\无字幕\新建文件夹\一次测试\ 备黄金集扩充。**:1011"留存确认"前置件就此闭合**（基准快照三件套中"词表 sha1 冻结"与"golden 快照实拍"两件仍开放，见 D2026-0925-01 执行契约）。

## [2026-09-25] [D2026-0925-01] 项目收口与 1.3.0 开工方案·用户处置意见逐项裁定与分批拍板 [已拍板·执行中]

> 评议链路：本条目为决策日志续评类（引用 D2026-0923-01/:788、D2026-0924-03/:982、D2026-0924-04/:998-1020 及既有拍板链 D2026-0921-01/02、D2026-0922-01/02/03）。decision-critic 独立评议本轮共 9 项用户处置意见 + 主模型 8 项补充（S1-S8），产出 [HIGH_RISK_OBJECTION] 1 条（HRO-1）与 [MATERIAL_CONFLICT] 2 条（MC-1/MC-2）；主模型逐项回应并获用户于 2026-09-25 确认。

### 原决策

"1.3.0 pipeline_v2 拆分为唯一主线立刻推进；可先行小件并行；记录侧欠账马上修；待拍板分批收口，不开新战线；删除类动作先可恢复、先留痕"——用户逐条处置意见（9 项）+ 主模型补充（S1-S8）的整体裁定与分批拍板。

### 评议方式

- decision-critic 独立只读核验：decision-log.md 全部 1020 行、使用与维护手册.md、config.py:40-61（TUNABLE 白名单）、cli.py:105-112（gemma-4-12b 兜底/--v2-ctx 缺省）、index.html:188-191、manifest.py:157-190/330-345（delete_resume_artifacts/_CONFIG_FIELDS）、pipeline_v2.py:2204-2222（_backup_existing_outputs）、quality_report.py:762-800（TM 摘要行/时间戳头）、risk.py:170-176、tm.py:26-37，及 schtasks 实查 BatchE_WD、E 盘目录实况、Temp/translation_memory 备份清单（合计约 12.5MB）。
- 检出：简报事实与代码基本一致；2 处材料冲突（见裁定段）。
- 用户直接终选拍板，本条目为终选归档。

### 我的异议与 HRO 回应

- **[HIGH_RISK_OBJECTION-1]（HRO-2 逐字节 diff 验收门现行规格下必假失败）——主模型回应：采纳（附条件完整采纳），异议解除。**
  - 判定依据：①与已验证事实直接冲突——`quality_report.py:800` 头部含现生成时间戳、`:762-770` TM 摘要行随库状态变化，同输入两次运行 txt 字节必不同；②影响 ≥3 任务（HRO-2 门/拆分工作流/质量报告契约测试，并牵动 D2026-0922-03 新增导读 json 双渲染）；③验收失真——按字面执行必红，触发事后改判，违背"跑前预声明、跑后不得改"纪律。
  - 采纳条件（两条，均写入 HRO-2 可执行测试用例，用例列为拆分动工前置件）：
    1. 归一化白名单——时间戳/TM 摘要行等非确定字段显式排除；
    2. 两次跑各用独立新建空 TM 库（防第二次跑 H>0/L=0 致 txt 必不同）。
  - 其他各项（TM 迁移、G:\python、BatchE_WD 删除、词表判定）均经判定不构成 HRO，维持普通级。
- **[INFO_GAP] 已闭环**：E 盘真实转写存量（四次测试目录 5 部 + 一次测试目录 7 部，2026-09-25 实测 36 文件在位）、BatchE_WD 本机现状（存在+Disabled）、库内交接文档位置（docs/模型测试两轮交接.md§六；树外 SubTransJAV_项目介绍稿.md 经实测 0 处 sakura/待决/重测 表述，无需动作）均经独立核验确认，无需再索材料。

### 拍板条款（用户 2026-09-25 确认）

**组 A——授权按序开工（A1+A2 先行）**：
- A1 记录侧 docs-only 一次提交：:788/:982 状态字段转"已执行"；批次 3 补最小留痕；15:01 死因归因放弃但"异常退出日志持久性核查"转观测项挂批次6；观测项 3 条（lms 升级 fail-open 静默退化 / --parallel 静默钳制 / 400 恢复每实例 1 次信用）写入已知问题保留监控；:949 BatchE_WD 存在性失实修正；库内 docs/模型测试两轮交接.md§六 sakura 加"已按 D2026-0921-02 关闭"标注；树外项目介绍稿仅核实并报告、本轮不改（已核实：无涉，见 INFO_GAP 段）。
- A2 E 盘留存 + HRO-2 输入钉死：选 3-5 部真实 ASR 转写（实取四次测试全部 5 部），sha256 清单，副本落 `D:\SubTransJAV-internal-archive\`（项目树外），回写 :1011（已执行，见上"留存回填"）。
- A3 HRO-2 可执行测试用例（含 HRO-1 两条件）；A4 M3 mypy 小 PR（process_manager 28 + llm_client 3，凡触 pipeline_v2.py/quality_report.py 的 mypy 修复一律顺延拆分后，M1 整批顺延）；A5 质量报告备份/resume 契约测试补齐（先行，不触 txt 内容）；A6 BatchE_WD 先 `schtasks /query /xml` 导 XML 留痕后删除；A7 词表误译 2 条（#499 climax_iku_variant 等）基准快照前定案、若改 glossary.csv 同步冻结 sha1。

**组 B——本轮拍板（用户全部通过，照评议建议）**：
- B1 模型缺省文档项②③：ctx 22272 三处口径统一标注"作者 16GB 单卡实测档案值，请按显存调整"；文档写明引擎自动化仅覆盖 LM Studio 后端（ollama/custom 无拉起换载）。
- B2 H4b 黄金集门②：时点=1.3.1 动工评审；责任人随该评审指定；逾期沿用 D2026-0914-01 降级为构造集基线标注，不阻塞。
- B3 E2-A 术语冲突 5 条（:572）：硬限=1.3.0 词表覆盖层开工前；按"误译/可接受/需术语表"三类判定，结果作覆盖层输入。
- B4 首文件抽检转"后版本质量专项"：一名目两触发点（首文件抽检=1.3.0 发布后首片；黄金集=1.3.1 动工评审）；验证项不合并（首文件=别停/クリ/部長で 考点域，黄金集=闸门0 行为域）；决策日志写明已知风险接受、不阻塞当前发布；考点清单与首文件样本归档备复盘。
- B5 GUI 黑盒降级协议：拆分主线期=回归抽检；GUI i18n 工作流收口时恢复一次关键路径黑盒，写进决策防静默带过。

**组 C——显式延后并记录（不拍）**：
- C1 模型缺省值持久化档位①（1.3.1 实施；评估内联 1.3.0 GUI 参数面板期）；C2 显存推荐④另立低优先版本；C3 v2-ctx 强联动（GUI i18n/参数面板稳定后）；C4 H4b 条目级阈值自适应落地（双且门，1.3.1 动工评审一次性判定）；C5 生产常驻 BatchE_WD 类任务（真实挂机需求出现时按看门狗+告警新立，不复活旧任务）。

**盲点采纳入执行说明（拆分工作流执行契约）**：
1. 拆分第 0 动作=重构前基准快照三件套（E 盘留存确认 + 词表 sha1 冻结 + golden 快照实拍），任何 pipeline_v2 改动之前完成。
2. 解读层（导读 json 双渲染）排在基准快照拍摄之后；仅备份/resume 覆盖补齐的契约测试可先行。
3. mypy 基线冻结时点=拆前快照；按模块拆完即清零该模块并同步缩减基线；M3 独立推进不与拆分文件交集。
4. TM 隔离归档放项目树外（不入 git）；删除条件=1.3.0 发布 + 新库连续运行 ≥10 部片或 ≥2 周 + tm_purge 探针抽样正常 + 用户书面确认，缺一不删。
5. TM 迁出工作流附 6382 条质量构成统计（只统计不清洗；清洗与否归用户裁决）。

### 材料冲突裁定

- **MC-1（BatchE_WD 存在性）**：decision-log.md:949（2026-09-24 上午"本机已不存在"）vs 2026-09-25 实测（存在+Disabled+上次运行 9-23 22:26、结果 0xC0000005）。裁定=:949 系 2026-09-24 上午时点记录，午后任务重建后禁用，记录失实；失实修正入 A1 docs-only 提交；删除动作照议（先导 XML 留痕）。
- **MC-2（E 盘"产物已清理"）**：:1010 指原批次 E 阶段性产物；现存 36 文件系 9-24 GUI 实测回填产物 + 5 部转写。裁定=留存确认基于现值清单+哈希，不默认"已清理"，无需补位。

### 风险跟踪

1. HRO-1 采纳后须在 A3 用例提交时逐条勾验两条条件（归一化白名单覆盖时间戳/TM 行、双独立空库），未入用例不得宣称拆分动工前置件已闭环。
2. 首文件抽检触发随 1.3.0 发布后首片，未触发前考点复核（别停/クリ/部長で）属"已知风险接受"挂账，不得静默消失；样本与清单随 B4 归档备复盘。
3. H4b 黄金集责任人随 1.3.1 动工评审指定，期间持续挂账；逾期降级路径沿用构造集基线标注。
4. TM 6382 条迁移取舍仍为开放项（质量构成统计先行，清洗与否用户裁决），随 TM 迁出工作流闭合。
5. 批次6（Mimosa 26 处静态告警 + pytest 双口径分账 + 异常退出日志持久性核查）排 1.3.0 后 1.3.1 前，本条目不新增优先级。
6. BatchE_WD 2026-09-24 上午时点与午后任务重建的时点变更已入 A1 修正记录；其上次运行 0xC0000005 系 9-23 深夜 draft 事故现场留痕，无新增风险面。

### 执行留痕

- **批次排班最小补记（A1）**：定版排班（2026-09-24）批次 1/2/3 已于当日执行完毕——批次 1=draft 移除（af93ce8）、批次 2=并发对齐（be83c25）及运维裁决（见 D2026-0924-01§七）；批次 3 排班本体未入库、无从反查，以本条为存照；批次 4 或未单列、无从考证；批次 5=400 恢复+同窗 E3 脚本化/v2-ctx 缺省（1f83c8c/b8d42b5/db0fa1c）；批次 6=债务专项待独立排期。此后排班以决策日志入库为准。
- **已知问题（观测项，保留监控不阻塞）**：① lms 升级致并发 fail-open 判定静默退化（D2026-0924-01 风险跟踪②）；② 引擎对 --parallel 静默钳制（D2026-0924-01 风险跟踪①）；③ 400 恢复每实例 1 次信用高频场景再议（D2026-0924-02）；④ 异常退出时日志缓冲丢失（批次E 15:01 观测假说——死因归因放弃，日志持久性核查转本项挂批次6）。
- **A2 已执行**（2026-09-25）：见 D2026-0924-04"留存回填"小节；A1 主体（状态字段×2、:949 修正、sakura 补注、批次补记、已知问题清单）随本条目同批入库；树外项目介绍稿核实无涉、未改动。
- 本条目后续执行追记（A3-A7 逐项 verify、B1-B5 回填、组 C 显式延后记录）随各轮提交按 D2026-0921-03 标准运行模式入库（定向 secret 扫描 → 终端直提 → 三查 → blobs 复扫 → push → ls-remote 复核）。

### 执行追记（2026-09-25 组A 全件+B1 收口，拆分前置三件套闭合）

**执行模式**：用户指示"除方案讨论/拍板外全部由主模型直接负责；可并行任务分发智能体"。本轮实现经 4 路 coding 并行（文件集互不相交）+ 主模型集成验证收口。

- **A3 ✅（f1858b2）**：tools/hro2_gate.py（410 行，capture/compare 双子命令）+ tests/test_hro2_gate.py（8 例）。HRO-1 两条件入 harness：①归一化白名单=报告时间戳头与 TM 摘要行两规则；②capture 每跑全新空 TM（cfg.tm_db_path）。watch advice 有状态实锤（evaluate_watch 依赖全局观察文件累计历史）→ capture 打桩 pv.default_watch_path 每跑重定向；打桩点三处（_make_client/refine_tmp_dir/default_watch_path）均 try/finally 恢复。
- **A4 ✅（bc8f32b）**：M3 mypy 31 错清零（process_manager 28+llm_client 3；含 ：588 循环变量 e→entry 两行零行为改名）。全仓 mypy 70→39 错/10 文件（M1 管线批按拍板顺延拆分后）。
- **A5 ✅（97aa4fb）**：_backup_existing_outputs 备份表 4→6 项（+风险清单 md/json）；新增 _remove_stale_risk_reports 写前清陈旧（残留路径=上轮失败有清单无终稿）；设计裁定=风险清单属最终产物非恢复现场，不进 delete_resume_artifacts（成功路径调用点在 write_reports 之后，收编会误删新报告）；+3 契约测试（备份覆盖/空目录 no-op/清理与 write_reports 文件名双钉防漂移）。
- **A5 验收回填（2026-09-25 用户验收轮，裁定获认可+精度建议采纳）**：①主理由锚定职责语义——delete_resume_artifacts 契约=清理可重建的恢复现场（中间态），风险清单与 final_cn.srt/质量报告.txt 同属最终交付物，收编即把成品当恢复现场处理，属职责边界问题而非实现取舍；"调用点在 write_reports 之后"降为辅证（调用点随重构漂移，不作裁定依据，pipeline_v2 docstring 已同步改锚）。②三类清理职责互斥自洽：backup 保成品（备份不删）/ _remove_stale_risk_reports 清"有清单无终稿"失败残留（写前清）/ delete_resume_artifacts 清恢复现场（不碰成品）。③补防回退断言 test_delete_resume_artifacts_never_touches_final_deliverables（钉"清理清单恰=恢复现场四件、成品六件一件不碰"）。④**边界条款：本语义在 1.3.0 拆分中属行为等价验收的一部分，不进"有意变更"豁免清单**（已入拆分实施规格）。
- **B1 ✅（1074b95）**：ctx 22272 三处（config 注释/cli help/index.html tooltip）+api docstring 统一标注"作者 16GB 单卡实测档案值，请按自身显存调整"；手册新增 §2.3 引擎自动化边界与缺省口径（仅覆盖 LM Studio 后端；管线配置为唯一事实来源；缺省数值零改动）。
- **A6 ✅**：BatchE_WD 导出 XML 留痕（D:\SubTransJAV-internal-archive\BatchE_WD_export_20260925.xml，注册 2026-09-23T00:36:57/每 5 分钟触发/wscript 挂 %LOCALAPPDATA%\Temp\batche_wd_hidden.vbs）→ schtasks /delete 成功 → 复核不存在。
- **A7 判定更正（2026-09-25 用户验收轮，验收裁定权在用户，上条定性撤回）**：用户复核裁定 **#499 复核通过、不构成误译**——高潮/快感语境下源文 いっちゃう（イク 委婉连用形）译「要去了」符合语境。事实纠正三点：①被复核译文实为终稿 #498「要去了要去了……啊……嗯……啊——。」（イク 语义在译），本条目原记"译文'好厉害好厉害。啊。'イク 语义整体丢失→真实质量缺口"系**抽块错位**——post_validate 警告产生于"按时间轴恢复 1439 条原始编号"（Logs/9-24-1631.txt:396）之前，其 #499 为管线内部序号，该区域终稿块号=管线序号−1（上游兜底清洗合并/删除所致），终稿 #499 实为下一句源文 すごいすごい。あ。的译文；②climax_iku_variant 规则判词方向=「源文命中高潮形态但译文出现词表通用变体（要去了/快去了/要高潮了/快高潮了），提请人工复核是否够地道」，**非**"译文缺失变体"；③本查例属严格匹配提醒性质的复核提示，人工复核通过即结案，规则本体不动。处置不变项（与定性无关，继续有效）：不改 glossary.csv、维持 watch 观察生效、glossary sha1 冻结 4b90fa9e…。**教训入档：复核 post_validate/风险台账的 #N 编号必须按时间轴对齐源文与终稿，不得直接按终稿块号抽取**（编号在恢复步骤后才与源文对齐）。
- **集成验证**：ruff 全仓零告警；mypy 39 错/10 文件（M3 清零后基线）；全量 pytest **998 passed + 1 skipped**（987+1 → +8 harness +3 契约，只增不减）；CLI/harness --help 冒烟过；Mimosa 深扫提交前 seal 53010c48…26 findings=基线零新增（四段提交后复扫见下）。
- **golden 基准快照（拆分第 0 动作第三件）✅**：位置 `D:\SubTransJAV-internal-archive\hro2-golden-baseline-20260925\pre-refactor-run1\`，git_head=1074b955227bfa656597304de1d77de991210285（与 B1 提交点一致），输入=树外 5 部 golden（sha256 与 ：1011 留存清单一致），词表 sha1=4b90fa9e…（冻结），配置指纹 ctx 22272/并发 1/TM on/synopsis off。**确定性自检 PASS**：同提交点连拍 run2 与 run1 互比=final 逐字节全等+报告归一化后全等（hro2_gate compare EXIT=0）；5 部 final+5 部报告+manifest 的 sha256 清单入同目录 _baseline_manifest.txt。**拆分动工前置三件套全部闭合**（留存确认 :1011 ✅ + 词表冻结 ✅ + golden 快照 ✅）——1.3.0 pipeline_v2 拆分正式解锁。
- **风险跟踪勾验**：跟踪①（HRO-1 两条件入用例）✅ 已由 A3 harness 兑现；跟踪 2/3/4/5/6 状态不变。
- **基线引用口径更新**：测试基线 **998 passed + 1 skipped**（@1074b95）；全仓 mypy 参考 39 错/10 文件；随拆分开工按执行契约"拆前快照冻结 mypy 基线、按模块清零缩减"。

### 拆分执行追记（2026-09-25 拆分主线完成：六模块迁出+M1 清零，golden 门四次 PASS）

- **拆分落地（75b9bca）**：批次 1 四模块并行逐字迁移（v2_premerge 213 行/v2_context_blocks 155 行/v2_manifest_fp 169 行/v2_outputs 244 行）+ 批次 2（v2_learn 239 行/v2_rules 166 行）+ facade 接线——pipeline_v2.py 2485→1528 行（-957），纯编排主干；**11 个含 patch 调用点的函数（_ensure_auto_synopsis/_load_v2_instruction/_make_client/_make_fallback_client/_run_with_fallback/_collect_grammar_hints/_run_stage_a/_run_stage_b/_finish_learn_threads/run_v2/_run_single_v2）与全部被 patch 名字（含 _ensure_lmstudio_engine/_read_v2_card/_GRAMMAR_CACHE_MAX/_LEARN_JOIN_TIMEOUT 的调用点）留驻 facade，patch 语义零变更**；迁出符号 from-import re-export 保持 pv.<name> 可解析；新模块零反向依赖（R1）、logger 字节级同名（R4）、tm_purge 等 import 期触面全部保持（R6）。
- **模块单测（HRO-2 条款"每个新模块 ≥1 直接单测"）**：tests/test_v2_split_modules.py 13 例——六模块各 ≥1 + V2_STAGE_TAGS/SLOT 叶子副本值对齐钉 + re-export `is` 绑定钉 + **职责边界回归钉**（风险清单/成品与恢复现场清理清单互斥，A5 验收条款随迁验证）。
- **golden 门四次 PASS（终审证据）**：①拆前确定性自检（run1↔run2）；②批次 1 接线后中途门；③批次 2 后终门；④M1 后复跑——均 post vs pre-refactor-run1 产物全等（final 逐字节/报告归一化，EXIT=0）。行为等价以产物级字节证据收口。
- **M1 mypy 管线批清零（e7594d9）**：refine 面 21 错归零（9 文件；纯注解+except 变量零行为改名，golden 等价域语句零触碰，顺延 0）；全仓 mypy 39→**18 错**（余 M2 面 main 9/api 7/event_stream 2，另行排期）；mypy 基线文件机制按契约③待 M2 收口落地。
- **基线再次更新**：全量测试 **1012 passed + 1 skipped**（999+1+13，只增不减）；全仓 mypy 参考 18 错/3 文件。
- **拆分后队列**：解读层 CLI 侧（基准快照已拍，可开工）→ GUI i18n+参数面板+查看器+槽位对齐同批 → B3 E2-A 判定+词表覆盖层 → LRU/文档/lockfile 收尾；批次 6 债务（Mimosa 26 处+M2+pytest 双口径）排 1.3.0 后。

### 解读层实施与验收门缺陷披露（2026-09-25 深夜）

- **解读层 CLI 侧落地**：quality_report.py 新增【白话导读】区（【结论】行后恒有——基于本次运行声明+时间戳同源复用头部串（hro2_gate 白名单覆盖）+①总体（N=len(items) 同源硬条款兑现，:769②）/②条目链路（rhs_total==n_src 同源）/③漏覆盖/④未翻译（条件行）/⑤阅读顺序建议）+ 9 章节白话注解（组装层统一后处理插入标题行后，render_* 函数体零改动，离线报告与 render 直调不受影响）+ 手册新增 §12 质量报告怎么读（三步走）+§6/§7.2/FAQ-9 三处指引行；文案全量避开既有负向断言黑名单（"疑似"两分支不含、条件章节字面/隔离区移出/条目在但未译等条件化规避）。**分批口径补记**：导读 json 归 i18n 批次——依据拍板一"解读层（CLI 侧，只动 quality_report.py）"硬界定（json 落地必改 v2_outputs/manifest 契约，超出本里程碑）；:1069"解读层（导读 json 双渲染）"的字面缝以此口径为准。
- **验收门缺陷披露（诚实记录）**：hro2_gate compare_outputs 文件发现用顶层 glob，而 capture 产物在 run/{stem}/ 子目录 → **拆分期间四次 compare 空转假 PASS**（循环 0 次）。发现路径=解读层变更范围自检（post-guidebar 明确含导读却对 post-refactor PASS，取报告文件头实证后定位）。修复=rglob 递归发现（兼容平铺/嵌套双布局）+报告路径由已发现 final 同目录推导+tests/test_hro2_gate.py 增 3 例真实布局回归钉（含嵌套布局报告差异"必须 FAIL"反例钉与缺失终稿必须报 missing 钉）。**教训入档：单测 fixture 布局与被测物真实布局脱节时，验收门形同虚设——门的用例必须按真实产物布局构造。**
- **真裁决重做（修复后四连比）**：①拆分等价 pre-refactor-run1 vs post-refactor **PASS**（四次空转结论补实锤：六模块拆分+facade 接线行为等价成立）；②M1 等价 post-refactor vs post-m1-check **PASS**（注解零行为）；③解读层 post-m1-check vs post-guidebar **FAIL=预期有意变更**——差异逐处核对为纯导读/注解增行+行平移、无内容改写、final 逐字节全等（有意变更留痕归档，承 :764 豁免条款）；④SQL 修复等价 post-m1-check vs post-sqlfix **PASS**。
- **基线更新**：全量测试 **1019 passed + 1 skipped**（1016+1+3 门回归钉）；mypy 18 错/3 文件不变。解读层后等价比对参照更新：pre-refactor/post-refactor/post-m1-check 为拆分与 M1 的历史裁决存档，此后变更的等价比对新侧=post-guidebar/post-sqlfix（含导读形态）。

### 决策日志字段

- **原决策**：项目收口与 1.3.0 开工方案（用户 9 项处置意见 + 主模型 S1-S8）分批拍板。
- **我的异议**：[HIGH_RISK_OBJECTION-1] 一条（HRO-2 验收门归一化白名单缺失 + TM 双库策略未定义，附两条件）；[MATERIAL_CONFLICT] 两条（MC-1/MC-2）；普通级建议 9 项（用户 1-9 有条件支持 + S1-S8 全部支持/有条件支持、含排期顺序修正与盲点 6 条）。
- **主模型最终决定**：采纳（HRO-1 附条件完整采纳、异议解除；两冲突裁定如上；组 A 七件授权、组 B 五件用户拍板、组 C 五件显式延后；盲点全部采纳入拆分执行契约）。
- **条件是否已闭环**：决策层面闭环；执行面未闭环（A3-A7 待执行 + HRO-1 两条件入用例勾验 + 拆分动工前置三件套为闭环路径；A1/A2 已随本批执行）。
- **是否 [PRESSURE-OVERRIDE]**：否。
- **后续风险跟踪**：见上文风险跟踪 1-6。
