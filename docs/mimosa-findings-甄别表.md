# Mimosa 扫描发现三态甄别表（v1.3.1 D7a-3，D2026-0925-02）

- **数据源**：`C:\Users\57850\.mimosa\security-scans\project-84b400c5f32332301acf457f\scan-2026-09-24T17-54-12.317Z-a0276cc48beb\findings.json`
- **Seal digest（历史锚：2026-09-24 首扫基线，下表甄别结论即对该次快照作出）**：`sha256:53010c48d878099dfb2c3b91443593f3654baf79619c60063efbaa44bae080da`（seal.json，artifacts 含 findings.json sha256:51623f83…）
- **Seal digest（2026-09-26 复扫后新基线）**：`sha256:79eae27d882b5b250dc2bac8574dd2ed3accd42cc29adbf0fa19f485bedecd2e`，findingCount=**24**，零新增；净减 2 = `tools/tm_promote.py` SQL 字面量化与 `create_shortcut.py` 换 `subprocess` 两处修复在复扫中兑现消除；依赖扫描 completion=completed / packagesScanned=60 / matchedAdvisories=1。
- **行号声明**：下表 line 号为扫描时点快照，会随代码演进而漂移；以 `identity.anchor`（findings.json 内 sha256 锚）为准做身份比对，行号仅作定位便利。
- **口径修正声明（双基线并列）**：旧基线（2026-09-24 首扫，即上行历史锚 seal）——主控下发口径按 :786 分类相加为 25≠26；该次以 findings.json 实际数据为准：**20 路径穿越 + 2 SSRF + 1 SQL 注入 + 1 命令注入 + 2 弱随机 = 26 条（历史口径，仅指首扫快照）**。新基线（2026-09-26 复扫，即上新基线 seal）——findingCount=**24 条**，零新增，净减 2 = tm_promote.py SQL 注入 1 条与 create_shortcut.py 命令注入 1 条兑现消除。两引擎口径差异分列有先例：**D2026-0921-03 27/26 分列**。

三态定义：
1. **已修复**：本轮改动消除（入库）。
2. **树外签注**：目标文件 UNTRACKED/.gitignore，不入仓库基线；本机一次性处置/留存声明，不构成仓库基线修复面（UNTRACKED 2 文件 3 条统一用此表述；Temp 瞬态脚本不修，留存期间深扫面不减）。
3. **留痕维持**：本地单机工具，无不可信输入面，维持现状并留痕判据。

## 三态统计（双基线并列）：旧基线（2026-09-24 首扫，26 条，历史口径）：①已修复 1 ｜ ②树外签注 3 ｜ ③留痕维持 22 ｜ 新基线（2026-09-26 复扫，24 条）：上式净减 ①中 tm_promote.py SQL 1 条与 ②中 create_shortcut.py 1 条（兑现消除，①已从扫描面消除）→ ②剩 2（Temp/build_blind_pack.py 弱随机）｜ ③ 22 不变

---

## ① 已修复（1 条）

| # | file:line | publicClass | severity | findingId | 证据行与判据 |
|---|---|---|---|---|---|
| 1 | tools/tm_promote.py:51 | sql-injection | high | finding:f3e2befa37d0938f19301e67 | 原 `f"SELECT {cols} FROM {TABLE}…"` f-string SQL 三处（:51/:64/:96 附近）已改字面量 SQL（7a-1，先例 7f1c0c1 v2_manifest_fp），插值面清零，数据一律参数化 `?`。 |

## ② 树外签注（3 条，UNTRACKED 2 文件）

| # | file:line | publicClass | severity | findingId | 证据行与判据 |
|---|---|---|---|---|---|
| 2 | create_shortcut.py:11 | command-injection | high | finding:e9281db5d22bac827e16b3c9 | 原 `os.system(f'"{sys.executable}" -m pip install pywin32')` 已改 `subprocess.call([sys.executable, "-m", "pip", "install", "pywin32"])`（7a-2）；文件 UNTRACKED/.gitignore，本机一次性处置，**不构成仓库基线（D2026-0925-02 D7a）**，故记树外签注。 |
| 3 | Temp/build_blind_pack.py:10 | insecure-randomness | low | finding:0bdd4fe29173e436783d1499 | `rng = random.Random(42)`：固定种子盲测打包器，弱随机反而是确定性重放需要；瞬态脚本不修，留存期间深扫面不减。 |
| 4 | Temp/build_blind_pack.py:57 | insecure-randomness | low | finding:0bdd4fe29173e436783d1499 | `random.Random(7).sample(rest, …)`：同上，抽样盲测集用固定种子可复现是特性不是缺陷。 |

## ③ 留痕维持（22 条）

### refine 面 10 条路径穿越（本地单机工具构造路径，无不可信输入面）

| # | file:line | severity | findingId | 证据行与判据 |
|---|---|---|---|---|
| 5 | subtransjav/refine/cli.py:329 | high | finding:bafa083aed72363167807da2 | `from .config import LOGS_DIR`：日志目录为程序内常量构造路径，非用户穿越面。 |
| 6 | subtransjav/refine/filters.py:91 | high | finding:7cc1ebfc7715bc5ca435880d | `open(srt_path, "w", …)`：输出路径由本地 CLI 参数链（操作者本人）构造，单机无跨信任边界调用方。 |
| 7 | subtransjav/refine/glossary_conflict.py:164 | high | finding:821a7f08ca21f75982bc1e1c | `open(p, "w", encoding="utf-8-sig", …)`：冲突报告路径同上，操作者 CLI 参数链。 |
| 8 | subtransjav/refine/glossary.py:60 | high | finding:aea75b4fd802834ad18b95ad | `open(path, "w", encoding="utf-8-sig", …)`：术语表导出路径由本地 config/参数构造，无不可信输入。 |
| 9 | subtransjav/refine/instructions.py:87 | high | finding:ec34fe6407d2dfef39e7ec56 | `open(path, "w", encoding="utf-8")`：提示词文件路径为程序内派生路径。 |
| 10 | subtransjav/refine/language_validator.py:274 | high | finding:e65e93ab1230f3b6ecbff2aa | `open(srt_path, "w", encoding="utf-8")`：校验器写回路径，操作者本人指定。 |
| 11 | subtransjav/refine/manifest.py:131 | high | finding:dc990779a13a808494c43465 | `open(tmp, "w", encoding="utf-8")`：manifest 临时文件路径程序内构造（tmp→rename 模式）。 |
| 12 | subtransjav/refine/quality_report.py:404 | high | finding:98f2b0aac98b7d33e7e57033 | 质量报告产物路径枚举（`[(r, "已过滤") for r in artifacts]` 一带）：报告目录本地派生，无外部输入。 |
| 13 | subtransjav/refine/runlog.py:148 | high | finding:1f68310dd90cadd7dbf5bafa | `open(target, 'w', encoding='utf-8')`：错误日志归档目标路径由 Logs/ 常量目录派生。 |
| 14 | subtransjav/refine/tm.py:331 | high | finding:a14e3c7d40ccc72049c86bb3 | `open(path, "w", encoding="utf-8-sig", …)`：TM 导出路径，操作者 CLI 参数链。 |

### api 面 2 条路径穿越（本地 GUI 回环，路径来自本地配置/会话内自产数据）

| # | file:line | severity | findingId | 证据行与判据 |
|---|---|---|---|---|
| 15 | subtransjav/webview_gui/api.py:1057 | high | finding:ccbcb5d0b3d2c98620b1b856 | `by_stage = {s.get("stage"): s for s in data["stages"]…}`：本地 webview GUI 只回环服务本机会话，路径/阶段数据自产自用，无远程不可信客户端。 |
| 16 | subtransjav/webview_gui/api.py:1193 | high | finding:ccbcb5d0b3d2c98620b1b856 | `if not os.path.isfile(p):`：同 finding 同源，`p` 为本地会话内工作目录派生路径。 |

### tools 面 8 条路径穿越（操作者 CLI 参数链）

| # | file:line | severity | findingId | 证据行与判据 |
|---|---|---|---|---|
| 17 | tools/ab_compare_srt.py:318 | high | finding:a5484d1c0947dfeb4d24d39b | `open(OUT_TXT, "w", encoding="utf-8")`：A/B 对比输出为脚本内常量路径。 |
| 18 | tools/bench_refine.py:122 | high | finding:f78a5359eef3855f194e9521 | `open(in_path, "w", encoding="utf-8")`：基准输入构造，操作者本人指定路径。 |
| 19 | tools/context_review.py:906 | high | finding:af160b62079add92c8d6b247 | `open(out_path, "w", encoding="utf-8-sig", …)`：复核报告输出，操作者 CLI 参数链。 |
| 20 | tools/glossary_learned_reset.py:71 | high | finding:4f8c0efde120d7f5a26517c3 | `open(p, "w", encoding="utf-8-sig", …)`：学习术语表重置目标路径，config 常量派生。 |
| 21 | tools/model_matrix_run.py:121 | high | finding:9e4a8467fa88a394ea68d158 | `open(tmp, "w", encoding="utf-8")`：矩阵评测临时文件，程序内构造。 |
| 22 | tools/model_matrix_run.py:403 | high | finding:9e4a8467fa88a394ea68d158 | `logf = open(log_path, "w", …)`：评测日志路径，操作者 CLI 参数链。 |
| 23 | tools/model_matrix_run.py:1069 | high | finding:9e4a8467fa88a394ea68d158 | 同上（第二处日志句柄），同源同判据。 |
| 24 | tools/tm_purge.py:348 | high | finding:ce19f1834895030d6d709990 | `open(path, "w", encoding="utf-8-sig", …)`：清理工具导出路径，操作者本人指定。 |

### SSRF 2 条（本地回环探测，无远程攻击者可控 URL）

| # | file:line | severity | findingId | 证据行与判据 |
|---|---|---|---|---|
| 25 | subtransjav/refine/glossary_learn.py:91 | high | finding:f47d894d666709fe19ea5304 | `r = requests.get(probe_url, timeout=timeout_probe)`：probe URL 为本地词典服务健康探测（本机回环），目标由本地配置给出。 |
| 26 | tools/model_matrix_run.py:221 | high | finding:0f0d5e2b912d2f7d0cc88f80 | `with _urlreq.urlopen(req, timeout=60) as resp:`：请求本地 LM Studio/模型服务端点（127.0.0.1 回环），端点来自本地配置。 |

---

**留痕维持总判据**：本项目为本地单机工具链（CLI + 本地 webview 回环），上述路径/URL 全部来自操作者本人 CLI 参数、程序内常量或本地配置文件派生，不存在跨信任边界的不可信输入方；边界加固以守卫脚本（tools/guard_banned_paths.py，D10）+ 敏感路径名单制承接（decision-log :139 契约）。
