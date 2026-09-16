# 闸门0 黄金样本回归集（golden_v1）

冻结版本 **1.0**（`golden_v1.json` 的 `version` 字段）。用途：把
`apply_source_filter`（default 档）的七类别判定行为钉死，防检测器
阈值/语义回归。硬守卫测试：`tests/test_golden_set.py`；基线统计工具：
`tools/gate0_golden_stats.py`。

## 1. 语料来源三分（裁决基线：构造集基线）

| 来源 | 状态 | 说明 |
| --- | --- | --- |
| 历史 dropped 语料 | 已固化 | 固化于 `tests/test_language_validator.py` 的既有断言，不重复收录 |
| 构造样本 | 本集（49 条） | 按规则库 `subtransjav/refine/defaults/source_hallucination.yaml` 文档化阈值构造，覆盖七类别正样本、阈值边界与近邻负样本 |
| 真实 1.9.2 语料 | **待用户提供**（截止期已到，未提供） | 按裁决基线降级标注为"构造集基线"；用户提供后以 `origin: "real"` 追加条目并递增小版本号，重跑基线 |

## 2. 防循环验证

`expected_category` / `expected_action` 字段**先于实现核对写就**：
标注时只依据规则库 YAML 的文档化阈值（`min_run`、`min_len`、
`window_ratio`、`strict_min_run`、`unit_repeat_min` 等）与各类别的
`example` 证据样本逐条手工判定，再跑
`python tools/gate0_golden_stats.py` 机械核对。每条样本的
`generated_by` 写明构造依据（引用的具体阈值/豁免条款），供复核。

首轮核对发现并修正的 3 处构造缺陷（期望未改，修正的是样本构造）：
`GP-RP-001/002/003` 原先整条文件仅含重复文本，待删占比 100% 触发
保险阀降级（只计数模式），与"重复循环应删除"的期望无关——混入真实
台词使占比降至 40%~45% 后聚焦重复循环本身。

## 3. 判定口径

- `delete`：focus 条目被删除，且期望类别的删除计数 ≥1；
- `count`：focus 保留在主稿，且期望类别的检出计数 ≥1（default 档
  计数类只计数不删除；白名单保护命中照常计数）；
- `keep`：focus 保留在主稿，且全文件零检出（负样本硬口径）。

## 4. 版本冻结

- 本集冻结为 1.0：修改任何条目的 `text` / `expected_*` 都必须走
  版本解冻流程（人工确认 + 新版本号 + 决策日志记录）；
- 新增条目（含真实语料）允许原地追加，但不得改动既有条目；
- 若检测器行为变更导致 `tests/test_golden_set.py` 失败：先区分是
  "检测器回归"（修复检测器）还是"规则有意演进"（解冻黄金集版本），
  禁止直接改期望值让测试通过。

## 5. 样本 schema

见 `golden_v1.json` 顶层 `schema` 字段。`context` / `context_after`
为跨条目类别（重复循环、片尾窗口、孤立应答词连数）提供支撑条目，
harness 按每条 20s 间隔生成时间轴；正样本均已保证待删占比低于
保险阀阈值（50%），除专门标注降级路径的场景外不受降级干扰。
