# 生产模型归档

本目录留作生产模型相关归档位。原始评测证据在仓库外（E:\ 本地目录）与被忽略的 `.abtest/` 等处，仓库只留本 README 摘要。

## 生产默认搭配（D2026-0921-01 用户拍板，质量优先）

- 阶段A（净语翻译）：joyfox27b
- 阶段B（审校抛光）：heretic35b
- 服务商：lmstudio（本地 LM Studio）

代码落点：`subtransjav/refine/config.py` `RefineConfig.stages` 默认工厂——

- 槽0（阶段A）：`qwen3.8-27b-uncensored-joyfox-aggressive`
- 槽2（阶段B）：`qwen3.6-35b-a3b-uncensored-heretic-apex`

## 依据摘要

mida-559 两轮 25+16 组合矩阵盲评与成因分层评测后定版：joyfox-A 为克里考点唯一纠正席位，heretic-B 对脏草稿修复力最强（真丢失≈0）。

排除集（不作为生产搭配）：

- B=joyfox 全系（批次缺行降级）
- B=sakura 全系（整行丢失 15~17%）
- A=sakura 全系
- sakura>sakura
- trans8b（已删除）

## 运维要点

- LM Studio 请求必须传完整模型 ID：传 key 会被在载模型模糊顶替，造成静默错配。
- 中途换模策略与看门狗详见 `docs/模型测试两轮交接.md`。
- 引擎加载/卸载已由管线自动化（`utils/lmstudio.py`，D2026-0923-01）：目标模型未载、
  已载 ctx 与 `--v2-ctx` 不符、并发与 `--v2-concurrency` 不符时，自动 `lms unload --all` →
  `lms load -y --gpu max -c <v2-ctx> --parallel <v2-concurrency>`；GUI 手工加载值会被管线对齐覆盖，
  引擎参数以管线配置为唯一事实来源（引擎/管线两侧同步由构造保证）。

---
2026-09-22，1.2.3 收尾归档。
