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
