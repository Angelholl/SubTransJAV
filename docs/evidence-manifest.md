# 本地测试证据 manifest（.abtest/ 与 .tmp374/）

> 生成：2026-09-21（D2026-0921-02 收尾拍板第 9 条）。原始证据目录 `.abtest/`、
> `.tmp374/` 自该日起被 .gitignore 忽略、不入库（体积 56MB+，含片名与本地路径）。
> 本文件是仓库内唯一索引：记录目录内容、复现命令、关键哈希与结论摘要，供磁盘
> 清理前的固化与续评检索。**清理原始目录前必须先确认本文件内容完整。**

## 一、.abtest/（上游转录配置 AB 测试与 F06 定版证据）

决策链：docs/decision-log.md `D2026-0917-03` → R1 → E1 → E1-R1 → **E2 已执行**
（终选 **F06 = pass1 Qwen3-ASR-1.7B+WhisperSeg × pass2 Qwen3-ASR-1.7B+TEN，
pass1_primary 合并，semantic+aggressive**；v1.9.3 增补轮裁定 F06 维持、
htdemucs/enhance-for-vad 记为按片开关）。

| 文件/目录 | 内容 | sha256（前16位） |
|---|---|---|
| `prod_rerun.sh` | F06 生产默认配置 CLI（权威复现命令） | `8675b8c7d1948776` |
| `WhisperJAV_v1.9.2_实测报告.md` | 给上游的测评报告（工程问题前置结构，含第九节 v1.9.3 增补结论） | `4a8c804280f9515b` |
| `WhisperJAV_192_实测数据.zip` | 上游附件包（results.md/复现命令/脚本，无字幕无本项目痕迹） | `d4b993a92572f775` |
| `results.md` | AB 全组合结果总表 | `1d15f3360511104d` |
| `metrics.py` / `markers.py` | 结构指标 / 标记词计数脚本（词表 v1 归档） | `5d2a712dec4c5cb8` / `158fc6fc964a9425` |
| `materialize.py` / `finals.py` | 离线物化（MergeEngine.merge pass1_primary）/ 10 finals 定义 | `922e0c828efa5c0e` / `cc0c7d4f2b03351e` |
| `gate0_probe.py` | 待办 b 实弹：闸门0 拦截率对照（F06 4/4821 vs F02 4/4785 删除） | `39c8858df8f445a3` |
| `prod/F06__{mihd002,mikr082,ure125,start422}` | F06 生产签名件（2026-09-19 01:11-06:33 真实 GPU 重跑 4/4 done，行数 1133/712/1457/1515，span 99.8/99.7/99.7/99.7%） | 目录 |
| `v193_round.sh` / `v193_round2.sh` / `v193_progress.log` | v1.9.3 增补轮（回归格零漂移；增量格 htdemucs 裁定记录） | — |
| `issue_attachments/` | 上游 issue 附件脚本副本 | — |
| `logs/` `out/` `smart_sens/` `progress.log` `prod_progress.log` | 跑批日志与中间产物 | — |

**结论摘要**：qwen 覆盖最全；串台句全部来自 large-v2/bal 行；F06 行噪声可被
下游吸收（净 +36 行真实内容 @ 零额外删除成本）；对齐器 8.95-11.4s 长尾 v1.9.3
未修（上游已知）。F06 定版后 4 部过拟合滚动观测为长期惯例项。

**发帖状态**：测评报告与附件包齐备（含第九节增补结论），发帖为用户动作。

## 二、.tmp374/（上游 issue #374 评论快照）

| 文件 | 内容 |
|---|---|
| `body.json` / `comments.json` | issue #374 正文与评论快照（E1-R1 外部 AI 点评轮的审读对象溯源） |

## 三、磁盘清理注意事项

1. `.abtest/prod/` 是 F06 生产签名件（下游滚动观测基线），删除即失去权威产物，
   只能重跑（约 5.3h GPU）再生成。
2. `.abtest/WhisperJAV_192_实测数据.zip` 是发帖附件，发出前不得删除。
3. 其余日志/中间产物可按需清理；本 manifest 记录的哈希可用于清理前核对身份。
