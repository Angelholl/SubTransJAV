"""
上下文预审：离线增强复核 CSV 生成工具
==================================================
对双通道字幕翻译的必看分歧行做 LLM 五分类审核，产出增强复核 CSV 辅助人工。
**离线工具**：不碰主管线（不改 pipeline_v2 / 不改终稿），管线跑完后独立执行。

设计要点见本模块 docstring 各节说明（离线增强复核）。

流程：
1. 读同目录 ``*_分歧复核.csv``（按 merged 命名约定定位同目录
   pass1/pass2/final_cn 文件，规则与 pass_disagreement 一致）；
2. 按 --group 选必看/可选行（已过滤一律排除），可 --similarity-max 减载；
3. 目标行按时间轴最大重叠定位到 pass1/pass2/final_cn 条目，
   各取前后 ±5 行上下文，每 --batch-size 行打包一次 LLM 五分类调用
   （temperature 0.1；同音/同形异义双候选硬编码降级为类别4）；
4. 证据核验（纯代码确定性）：类别1 的依据时间轴必须在指定 pass 的
   ±5 行窗口内真实存在（容差 1.0s）；R2 理由→选边矛盾（理由明确支持
   另一侧）直接判 FAIL，理由双侧均支持则置信强制 LOW 并加
   ``[需人工复核]`` 标记；核验失败类别不变、置信强制 LOW、同类内
   排序沉底、不计入高置信集合；
5. 输出增强复核 CSV（utf-8-sig）：原列全保留 + 追加 7 列（末列为
   「提示词版本」审计留痕）+ 头部 ``#`` 统计注释块（末行标注提示词
   版本）；四级排序（影片→类别→核验沉底→相似度→时间轴）；
6. 性能埋点：每 call 记录行数/token/耗时，stdout 汇总 + sidecar
   ``{output}.usage.json``。

用法：
    python tools/context_review.py 某片_分歧复核.csv --dry-run
    python tools/context_review.py 字幕目录 --sample 50 --seed 42
    python tools/context_review.py 某片_分歧复核.csv --prompt-version B
"""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine.filters import parse_srt
from subtransjav.refine.pass_disagreement import (
    _LANG_RE,
    _MARKER_RE,
    _timing_span,
)

# ======================================================================
# 常量（审核协议参数）
# ======================================================================
CSV_SUFFIX = "_分歧复核.csv"       # 分歧复核 CSV 文件名后缀
DEFAULT_ENDPOINT = "http://localhost:1234/v1"
# LM Studio 兼容端点占位密钥（本地服务不鉴权）；支持环境变量覆盖
DEFAULT_API_KEY = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")
DEFAULT_BATCH_SIZE = 10            # 每 call 打包行数（设计稿 2.2）
DEFAULT_TEMPERATURE = 0.1          # 与主管线一致（设计稿 2.2）
CONTEXT_RADIUS = 5                 # 目标行前后各 5 行（提示词窗口=核验窗口，2.3）
EVIDENCE_TOL_SEC = 1.0             # 证据时间戳与窗口行 start_sec 的容差（秒）
REASON_CLIP = 200                  # 依据说明截断长度（字符）
RETRY_MAX = 2                      # 单次 call 失败重试次数（非瞬态错误也重一次）

# 五分类定义（设计稿 2.2 表）
CATEGORIES = {
    1: "单侧可修",
    2: "双侧不可靠",
    3: "错配存疑",
    4: "无法判断",
    5: "同义详略",
}
CATEGORY_PRIORITY = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4}   # 排序用（1 最优先）

# 类别5 尾块前插入的注释行（设计稿第3节）
CATEGORY5_HEADER = "# === 类别5 同义详略（可整体跳过） ==="

# 分歧复核 CSV 原有列（须原样保留到增强 CSV）
ORIGINAL_COLUMNS = ["文件名", "分组", "时间轴", "相似度", "iou", "offset",
                    "artifact", "pass1", "pass2", "final_cn译文"]
# 追加列（末列「提示词版本」为 A/B 双版本审计留痕）
EXTRA_COLUMNS = ["建议类别", "建议选边", "依据", "证据核验", "置信", "依据说明",
                 "提示词版本"]

# 提示词（中文，代码内模板；硬编码规则：同音/同形异义双候选 → 类别4）
# A/B 双版本（D2 裁定）：
#   A = 激进（默认）：完全移除终稿列与 final_cn 上下文通道，选边依据
#       仅限上下文行语义、时间轴对齐、ASR 原文特征（碎片/乱码/重复度）；
#   B = 保守：保留终稿列与 final_cn 上下文，但强制声明「终稿仅供参考、
#       可能含幻觉、不得作为选边依据」。
# 两版均含 R3 双侧碎片/幻觉判定、R4 类别1/3 边界规则与 few-shot 示例。
SYSTEM_PROMPT = (
    "你是字幕双引擎分歧的上下文审核员。pass1 与 pass2 是两条 ASR 引擎对同一段"
    "音频的转写候选，人工需要判断哪侧更符合上下文。请严格按要求输出 JSON。"
)
DEFAULT_PROMPT_VERSION = "A"
PROMPT_VERSION_LABELS = {
    "A": "A（激进-无终稿）",
    "B": "B（保守-含终稿）",
}

# ---- 共用段落：五分类定义 ----
_CATEGORIES_SECTION = """## 五分类定义（category 取 1-5 之一）
- 1 单侧可修：一侧明显更符合上下文，需要选边（另一侧明显错）。
- 2 双侧不可靠：两侧各自尝试解读均不成立（如双侧幻觉、噪声），无修复价值。
- 3 错配存疑：两行时间轴或内容错位，可能两边各自成立（如错切分句）。输出错配点说明，**不选边**。
- 4 无法判断：证据不足无法归类。**硬编码规则**：若 pass1/pass2 是同音/同形异义的双候选（例如「会いたかった/開いたかった」型），仅凭上下文无法裁决，必须判 4，不给任何倾向。
- 5 同义详略：两侧意思一致，仅详略/长短不同。"""

# ---- 共用段落：few-shot（轮1 真实案例提炼，已去除影片名等信息）----
_FEWSHOT_SECTION = """## 示例（仅供理解分类，不得照抄内容）
1. 类别 2：p1「タイム、アリマスタマ、シマシマシャー」/ p2「だいぶありますね。だい、大分ありますからね。ちょっと。」——p1 为不可发音乱码；p2 虽为完整句但与上下文时间轴不符（相邻场景内容），双侧均不可靠。
2. 类别 2：p1「カンペンさま、イイマイ」/ p2「おい静かにしろ」——p1 乱码；p2 为相邻行内容错位（并非目标行），双侧均不可靠。
3. 类别 1（1/3 边界正例）：p1「いや、嫌いじゃないんだ」/ p2「そんなことより早く行こう」——p1 完整句且与上下文语义连贯；p2 是目标行前后 ±1-2 秒相邻行的时间轴错位内容 → 判 1（side=pass1），不是 3。
4. 类别 5 正例：p1「たぶん」/ p2「多分」——同义异写，仅写法/详略不同 → 判 5。
5. 类别 5 反例：p1「ちゅーして」/ p2「吸って」——意思相近但一侧为拟声/口语、另一侧为具体动作词，并非同义详略 → 不是 5（按上下文裁决；无法裁决则判 4）。"""

# ---- 共用段落：依据要求 + 输出格式 ----
_FIELD_SECTION = """## 每类的依据要求（reason 与附加字段）
- 类别1 必须给 side(pass1|pass2) + evidence_timing（所引上下文行的时间轴，形如 HH:MM:SS,mmm）+ evidence_pass(pass1|pass2)，reason 说明为何选这侧。
- 类别2 的 reason 必须说明两侧各自的解读尝试以及为何均不成立。
- 类别3 的 reason 必须说明错配点（可能两边各自成立）。
- 类别5 的 reason 说明详略差异。
- 类别3/4/5 的 side、evidence_timing、evidence_pass 输出 null。

## 输出格式（严格 JSON 数组，不要 markdown 代码块，不要多余文字）
[{{"row": <行序号>, "category": 1-5, "side": "pass1"|"pass2"|null, "evidence_timing": "..."|null, "evidence_pass": "pass1"|"pass2"|null, "reason": "...", "confidence": "high"|"medium"|"low"}}]
行序号 row = 该行在本任务中的编号（1 起）。每行都必须且只能输出一个元素。"""

# ---- 版本差异段落 ----
_PREAMBLE_A = ("以下每行是一个待审核的分歧行（含 pass1/pass2 两种转写候选，"
               "以及目标行在两个字幕文件中各自前后±5行的上下文）。"
               "选边依据仅限：上下文行语义、时间轴对齐、ASR 原文特征"
               "（碎片/乱码/重复度）。")
_PREAMBLE_B = ("以下每行是一个待审核的分歧行（含 pass1/pass2 两种转写候选、"
               "终稿译文参考，以及目标行在三个字幕文件中各自前后±5行的"
               "上下文）。")
# R3 双侧碎片/幻觉判定（B 版保留「与终稿匹配」分句；A 版无终稿通道）
_RULE_A_2 = ("- 双侧碎片/幻觉判定：若两侧均为碎片/乱码/无语义拟声，或某一侧虽成形"
             "但本身疑似 ASR 幻觉（不可发音的辅音串、无意义音节连缀），"
             "应判类别 2。")
_RULE_B_2 = ("- 双侧碎片/幻觉判定：若两侧均为碎片/乱码/无语义拟声，或一侧与终稿"
             "译文匹配但该侧本身疑似 ASR 幻觉（不可发音的辅音串、无意义音节"
             "连缀），应判类别 2。")
# R4 类别1/3 边界（两版同构，B 版含终稿参照）
_RULE_A_3 = ("- 类别 1/3 边界：一侧与上下文匹配、另一侧为时间轴错位（其内容出现在"
             "目标行前后 ±1-2 秒附近的相邻行）→ 判 1，不是 3。类别 3 仅用于："
             "两行内容可能各自对应不同目标（错切分句、上下文错位无明确错位方向）"
             "且无法确定哪侧代表目标行。")
_RULE_B_3 = ("- 类别 1/3 边界：一侧与上下文或终稿匹配、另一侧为时间轴错位（其内容"
             "出现在目标行前后 ±1-2 秒附近的相邻行）→ 判 1，不是 3。类别 3 仅用于："
             "两行内容可能各自对应不同目标（错切分句、上下文错位无明确错位方向）"
             "且无法确定哪侧代表目标行。")
_RULES_A = "## 硬规则（必须遵守）\n" + _RULE_A_2 + "\n" + _RULE_A_3
_RULES_B = "## 硬规则（必须遵守）\n" + _RULE_B_2 + "\n" + _RULE_B_3
_FINAL_NOTE_B = ("## 终稿译文使用声明（强制）\n"
                 "终稿仅供参考，可能含幻觉，不得作为选边依据；若你发现终稿与两侧"
                 "均不符或两侧均碎片，应判类别 2。")

PROMPT_TEMPLATES = {
    "A": "\n\n".join([_PREAMBLE_A, _CATEGORIES_SECTION, _RULES_A,
                      _FEWSHOT_SECTION, _FIELD_SECTION,
                      "## 待审核行\n{rows_text}"]),
    "B": "\n\n".join([_PREAMBLE_B, _CATEGORIES_SECTION, _FINAL_NOTE_B,
                      _RULES_B, _FEWSHOT_SECTION, _FIELD_SECTION,
                      "## 待审核行\n{rows_text}"]),
}


# ======================================================================
# 文件定位（与 pass_disagreement 同规则）
# ======================================================================

def sibling_paths_for_csv(csv_path: Path) -> dict:
    """从分歧复核 CSV 路径推同目录 pass1/pass2/final_cn 路径。

    CSV 名 = merged 名 + ``_分歧复核.csv``；merged stem 为
    ``{base}.{lang}.merged[.subtransjav]``（剥法同 pass_disagreement）。
    只拼路径不查存在性，返回 {"merged","pass1","pass2","final_cn"}。
    """
    stem = csv_path.name[:-len(CSV_SUFFIX)]      # = merged 文件 stem
    m = _MARKER_RE.search(stem)
    if m:
        stem = stem[:m.start()]
    lang = "ja"
    m2 = _LANG_RE.search(stem)
    if m2:
        lang = m2.group(1)
        stem = stem[:m2.start()]
    d = csv_path.parent
    return {
        "merged": d / (csv_path.name[:-len(CSV_SUFFIX)] + ".srt"),
        "pass1": d / f"{stem}.{lang}.pass1.srt",
        "pass2": d / f"{stem}.{lang}.pass2.srt",
        "final_cn": d / f"{csv_path.name[:-len(CSV_SUFFIX)]}_final_cn.srt",
    }


def collect_review_csvs(input_path: Path) -> list:
    """收集待审核分歧 CSV 列表（文件名升序）。

    输入是文件 → [该文件]；输入是目录 → 仅扫描直接子文件中
    ``*_分歧复核.csv``（**不递归**，避免混入子目录）。
    """
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        return []
    return sorted(p for p in input_path.glob(f"*{CSV_SUFFIX}")
                  if p.is_file())


def load_review_rows(csv_path: Path) -> tuple[list, dict]:
    """读分歧复核 CSV 为行 dict 列表（保原列）。

    返回 (rows, by_文件名统计)：rows 元素含原列 + "_sim"/"_start" 附加键
    （相似度/时间轴起点数值化，供抽样与排序）。
    """
    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for rec in csv.DictReader(f):
            if rec.get("分组") == "已过滤":      # 已过滤伪影一律不审核
                continue
            row = {k: (rec.get(k) or "") for k in ORIGINAL_COLUMNS}
            try:
                row["_sim"] = float(row["相似度"] or 0.0)
            except ValueError:
                row["_sim"] = 0.0
            s, _ = _timing_span(row["时间轴"])
            row["_start"] = s
            rows.append(row)
    return rows


# ======================================================================
# 行筛选 / 抽样（纯函数）
# ======================================================================

def filter_rows(rows: list, group: str, similarity_max: float | None) -> list:
    """按 --group / --similarity-max 过滤行（不修改输入顺序）。

    group: "must"→必看，"optional"→可选，"both"→两者；已过滤行已在
    load_review_rows 剔除。similarity_max 非 None 时仅留相似度 ≤ 该值行。
    """
    want = {"must": {"必看"}, "optional": {"可选"},
            "both": {"必看", "可选"}}[group]
    out = []
    for r in rows:
        if r["分组"] not in want:
            continue
        if similarity_max is not None and r["_sim"] > similarity_max:
            continue
        out.append(r)
    return out


def sample_rows(film_rows: list, n: int, seed: int | None = None) -> dict:
    """跨片均匀抽样（round-robin 每片轮流取 1 行，纯函数、确定性）。

    film_rows: [(片名, 行列表)]，片顺序按文件名排序（调用方保证）；
    行在片内须先按相似度升序排好（调用方保证），轮转时按该顺序消费
    （先取相似度最低=最分歧的行）。N 超过总行数时全取；N≤0 取空。
    seed 为保留参数（当前算法确定性，不依赖随机源，固定任意值均复现）。
    返回被抽中行集合（dict 键=行 id），片内保持相似度升序原序。
    """
    total = sum(len(rows) for _, rows in film_rows)
    if n is None or n <= 0 or total == 0:
        return {}
    queues = [list(rows) for _, rows in film_rows]
    if n >= total:
        return {r["id"]: r for q in queues for r in q}
    picked = {}
    turn = 0
    while len(picked) < n and any(queues):
        q = queues[turn % len(queues)]
        if q:
            r = q.pop(0)
            picked[r["id"]] = r
        turn += 1
    return picked


# ======================================================================
# 上下文构建
# ======================================================================

def _locate_target(merged_timing: str, entries: list) -> int:
    """merged 时间轴在 entries 中按 span 最大重叠定位，返回下标；无则 -1。"""
    s0, e0 = _timing_span(merged_timing)
    if s0 < 0 or e0 < 0:
        return -1
    best, best_ov = -1, 0.0
    for i, e in enumerate(entries):
        s1, e1 = _timing_span(e["timing"])
        if s1 < 0 or e1 < 0:
            continue
        if max(s0, s1) >= min(e0, e1):       # 无正重叠
            continue
        ov = min(e0, e1) - max(s0, s1)
        if ov > best_ov:
            best, best_ov = i, ov
    return best


def build_context(row: dict, films: dict) -> dict | None:
    """为一行构建三通道上下文（pass1/pass2/final_cn 各 ±5 行）。

    返回 {"row": row, "pass1": [..], "pass2": [..], "final_cn": [..]}，
    每元素 {"index","timing","text"}；目标行定位失败的通道置 None；
    pass1 与 pass2 都定位失败时整体返回 None（无法审核）。
    """
    src = films[row["_film"]]
    ctx = {"row": row}
    ok = False
    for ch in ("pass1", "pass2", "final_cn"):
        entries = src.get(ch) or []
        i = _locate_target(row["时间轴"], entries)
        if i < 0:
            ctx[ch] = None
            if ch in ("pass1", "pass2"):
                continue
        else:
            ctx[ch] = entries[max(0, i - CONTEXT_RADIUS):
                              i + CONTEXT_RADIUS + 1]
            if ch in ("pass1", "pass2"):
                ok = True
    return ctx if ok else None


# ======================================================================
# LLM 调用层（可注入 client 便于单测）
# ======================================================================

class _LMStudioClient:
    """OpenAI 兼容端点最小封装（延迟 import openai，只在本类出现）。"""

    def __init__(self, endpoint: str, model: str):
        self._endpoint = endpoint
        self._model = model

    def chat(self, system_text: str, user_text: str) -> tuple[str, dict]:
        """单次 chat 调用，返回 (content, usage_dict)。

        usage: {"prompt_tokens": int, "completion_tokens": int}（缺省 0）。
        """
        from openai import OpenAI
        client = OpenAI(base_url=self._endpoint, api_key=DEFAULT_API_KEY,
                        timeout=900.0, max_retries=0)
        resp = client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system_text},
                      {"role": "user", "content": user_text}],
            temperature=DEFAULT_TEMPERATURE,
            stream=False,
        )
        u = getattr(resp, "usage", None)
        usage = {
            "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        } if u else {"prompt_tokens": 0, "completion_tokens": 0}
        return (resp.choices[0].message.content or "").strip(), usage

    def list_models(self) -> list:
        """列出端点已加载模型 id（连通性探测用）。"""
        from openai import OpenAI
        client = OpenAI(base_url=self._endpoint, api_key=DEFAULT_API_KEY,
                        timeout=30.0, max_retries=0)
        return [m.id for m in client.models.list().data]


def probe_endpoint(client) -> tuple[bool, str]:
    """连通性探测（列 models）。返回 (ok, 消息)。"""
    try:
        models = client.list_models()
        return True, f"端点可达，已加载 {len(models)} 个模型：" + \
            ", ".join(models[:5])
    except Exception as e:   # noqa: BLE001
        return False, f"端点探测失败: {e}"


def build_prompt(contexts: list, version: str = DEFAULT_PROMPT_VERSION) -> str:
    """把一批行上下文渲染为提示词（纯函数）。contexts 至少含 1 行。

    version="A"（激进-默认）：不渲染「终稿译文」行，也不渲染 final_cn
    上下文通道；version="B"（保守）：两者都渲染。
    """
    show_final = (version == "B")
    blocks = []
    for i, ctx in enumerate(contexts, 1):
        row = ctx["row"]
        lines = [
            f"### 第 {i} 行",
            f"时间轴: {row['时间轴']} | 相似度: {row['相似度']}",
            f"pass1 候选: {row['pass1']}",
            f"pass2 候选: {row['pass2']}",
        ]
        if show_final:
            lines.append(f"终稿译文: {row['final_cn译文']}")
        channels = [("pass1", "pass1 上下文"), ("pass2", "pass2 上下文")]
        if show_final:
            channels.append(("final_cn", "final_cn 终稿上下文"))
        for ch, label in channels:
            if ctx.get(ch):
                lines.append(f"{label}（含目标行，每行=序号|时间轴|文本）:")
                for e in ctx[ch]:
                    lines.append(f"  {e['index']}|{e['timing']}|{e['text']}")
            else:
                lines.append(f"{label}: （定位失败，无）")
        blocks.append("\n".join(lines))
    return PROMPT_TEMPLATES[version].format(rows_text="\n\n".join(blocks))


def extract_json_array(text: str) -> list:
    """剥 markdown code fence，截取首个 JSON 数组并 loads（纯函数）。

    无合法数组返回 []。
    """
    t = (text or "").strip()
    t = re.sub(r"```(?:json)?", "", t).strip("` \n")
    m = re.search(r"\[.*\]", t, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _valid_verdict(item: dict) -> bool:
    """单条审核结果字段合法性（结构层面）。"""
    return (isinstance(item, dict)
            and isinstance(item.get("row"), int)
            and item.get("category") in CATEGORIES
            and item.get("side") in ("pass1", "pass2", None)
            and item.get("evidence_pass") in ("pass1", "pass2", None)
            and item.get("confidence") in ("high", "medium", "low")
            and isinstance(item.get("reason"), str))


def parse_verdicts(text: str, n_rows: int) -> tuple[dict, int]:
    """解析一次 call 的输出为 {row_no: verdict}，返回 (结果, parse_fail 数)。

    行结果缺失或字段非法 → 以类别4/LOW 补位并计 parse_fail。
    """
    raw = extract_json_array(text)
    out = {}
    for item in raw:
        if not _valid_verdict(item):
            continue
        no = item["row"]
        if isinstance(no, int) and 1 <= no <= n_rows and no not in out:
            out[no] = {
                "category": int(item["category"]),
                "side": item.get("side"),
                "evidence_timing": item.get("evidence_timing") or "",
                "evidence_pass": item.get("evidence_pass"),
                "reason": (item.get("reason") or "").strip(),
                "confidence": item.get("confidence") or "low",
            }
    fails = 0
    for no in range(1, n_rows + 1):
        if no not in out:
            out[no] = {"category": 4, "side": None, "evidence_timing": "",
                       "evidence_pass": None, "reason": "",
                       "confidence": "low"}
            fails += 1
    return out, fails


def review_batch_with_client(client, contexts: list,
                             retries: int = RETRY_MAX,
                             version: str = DEFAULT_PROMPT_VERSION
                             ) -> tuple[dict, dict]:
    """一次 LLM 审核调用（含重试与解析）。

    返回 ({row_no: verdict}, usage{"prompt_tokens","completion_tokens",
    "elapsed_sec","rows","parse_fail"})。重试耗尽抛最后异常。
    """
    prompt = build_prompt(contexts, version)
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            content, usage = client.chat(SYSTEM_PROMPT, prompt)
            verdicts, fails = parse_verdicts(content, len(contexts))
            usage["elapsed_sec"] = round(time.time() - t0, 3)
            usage["rows"] = len(contexts)
            usage["parse_fail"] = fails
            return verdicts, usage
        except Exception as e:   # noqa: BLE001
            last_err = e
            if attempt >= retries:
                raise
            print(f"  ⚠️ 调用失败（{e}），重试 {attempt + 1}/{retries}")
    raise last_err   # 不可达，防御


# ======================================================================
# 证据核验（纯代码，确定性；设计稿 2.3）
# ======================================================================

_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?")


def parse_evidence_timestamps(s: str) -> list:
    """解析证据时间戳串中的所有时间点（秒），兼容 ,mmm/.mmm/无毫秒。

    无合法时间戳返回 []。
    """
    secs = []
    for m in _TS_RE.finditer(s or ""):
        h, mi, sec, ms = m.groups()
        try:
            v = int(h) * 3600 + int(mi) * 60 + int(sec)
            if ms:
                v += int(ms.ljust(3, "0")) / 1000.0
            secs.append(float(v))
        except ValueError:
            continue
    return secs


# ----------------------------------------------------------------------
# R2 理由→选边一致性启发（确定性、保守的精确短语级规则）
# ----------------------------------------------------------------------

_MANUAL_FLAG = "[需人工复核] "
_SIDE_TOKEN_RES = {
    "pass1": re.compile(r"pass\s*1", re.IGNORECASE),
    "pass2": re.compile(r"pass\s*2", re.IGNORECASE),
}
_CLAUSE_SPLIT_RE = re.compile(r"[。；;！!？?\n\r]")
# 「支持性表述」精确短语集（保守：只认明确肯定，不认中性描述）
_SUPPORT_KWS = (
    "与上下文一致", "与上下文相符", "与上下文连贯", "语义连贯", "衔接自然",
    "与终稿一致", "与终稿相符", "更符合", "更贴切", "更连贯", "更通顺",
    "成立", "正确", "完整句", "通顺", "吻合",
)
_NEG_TAIL = ("不", "非", "未", "无")      # 关键词紧邻前缀否定 → 不算支持


def _kw_supported(clause: str, kw: str) -> bool:
    """关键词在子句中是否构成支持性表述（排除紧邻否定前缀的误伤）。"""
    start = 0
    while True:
        i = clause.find(kw, start)
        if i < 0:
            return False
        j = i - 1
        while j >= 0 and clause[j] in " \u3000":
            j -= 1
        if j < 0 or clause[j] not in _NEG_TAIL:
            return True
        start = i + len(kw)


def _side_support(reason: str) -> dict:
    """扫描 reason：每侧是否出现「该侧 + 支持性关键词」的精确短语级支持。

    按子句（。；！？换行分隔）扫描：子句中提及某侧且含非否定的支持性
    关键词 → 该侧计为被支持；两侧同现于同一支持子句（无法区分强度）
    → 双侧均计被支持。未提及侧的子句不参与（保守）。
    """
    support = {"pass1": False, "pass2": False}
    for clause in _CLAUSE_SPLIT_RE.split(reason or ""):
        mentioned = [s for s in ("pass1", "pass2")
                     if _SIDE_TOKEN_RES[s].search(clause)]
        if not mentioned:
            continue
        for kw in _SUPPORT_KWS:
            if _kw_supported(clause, kw):
                for s in mentioned:
                    support[s] = True
    return support


def _reason_contradiction(verdict: dict) -> str:
    """R2 理由→选边一致性检查（纯函数，保守确定性规则）。

    返回：
    - "FAIL"：选边与理由明确矛盾（轮1 #46 模式：理由明确支持另一侧、
      未支持所选侧）；
    - "LOW_FLAG"：理由对双侧均给出支持性描述、无法区分强度；
    - ""：未触发（含非类别1、无 side、理由无支持性短语）。
    """
    if verdict.get("category") != 1 or not verdict.get("side"):
        return ""
    support = _side_support(verdict.get("reason") or "")
    if not (support["pass1"] or support["pass2"]):
        return ""
    other = "pass2" if verdict["side"] == "pass1" else "pass1"
    if support[other] and not support[verdict["side"]]:
        return "FAIL"
    if support[other] and support[verdict["side"]]:
        return "LOW_FLAG"
    return ""


# ----------------------------------------------------------------------
# R3 引用文本真实性核验（确定性；v1 收尾项）
# ----------------------------------------------------------------------

# 配对引号（含日式/弯引号/直角引号/ASCII 直引号）；未配对不提取
_CITE_QUOTE_RE = re.compile(
    r"「([^「」]*)」|『([^『』]*)』|“([^“”]*)”|‘([^‘’]*)’"
    r"|'([^'\n]*)'|\"([^\"\n]*)\"")
# 「日文特征」判定：含至少一个假名（平/片）
_KANA_RE = re.compile(r"[\u3041-\u309A\u30A1-\u30FA]")
_CITE_MIN_LEN = 4      # <4 字符片段不核验（如「を」）
_CITE_MAX_LEN = 40     # >40 字符视为概括转述，跳过


def _norm_cite_text(s: str) -> str:
    """引用匹配归一化：去全部空白 + 剥首尾「…」省略号与引号残留空白。"""
    t = re.sub(r"\s+", "", s or "")
    return t.strip("…")


def extract_cited_jp_fragments(reason: str) -> list:
    """从 verdict reason 提取「形似候选引用的日语片段」（纯函数，可直测）。

    设计约束（保守，避免误伤，v1 收尾定稿）：
    - 只取配对引号（「」『』“”‘’ 与 ASCII ' "）内层文本；未配对不取；
    - 长度 <4 字符不取（过短无法定位，如「を」）；
    - 长度 >40 字符跳过（模型概括转述不算编造载体）；
    - 纯中文/数字等不含假名的片段不取——窗口是日语原文，模型的中文
      译文引用天然不在窗口内，核验必误伤；只有含假名
      （ぁ-ん / ァ-ヶ）的片段才视为日语引文参与核验。
    """
    frags = []
    for m in _CITE_QUOTE_RE.finditer(reason or ""):
        inner = next(g for g in m.groups() if g is not None)
        inner = inner.strip().strip("…").strip()
        if len(inner) < _CITE_MIN_LEN or len(inner) > _CITE_MAX_LEN:
            continue
        if not _KANA_RE.search(inner):
            continue
        frags.append(inner)
    return frags


def _cited_fragments_missing(reason: str, win: list) -> bool:
    """引文真实性判定：存在日文引文片段但无一命中窗口文本 → True。

    只查否定性信号：无片段 / 全部命中 → False（不触发，不加分）。
    归一化子串匹配：窗口行文本与片段均去空白、剥首尾省略号
    （「…お前…」式前缀后缀省略是模型常见省略形态）。
    """
    frags = extract_cited_jp_fragments(reason)
    if not frags:
        return False
    norm_lines = [_norm_cite_text(e.get("text") or "") for e in win]
    for frag in frags:
        f = _norm_cite_text(frag)
        if not any(f in line for line in norm_lines):
            return True
    return False


def verify_evidence(verdict: dict, context: dict) -> str:
    """核验一条类别1 verdict 的证据，返回 PASS/FAIL（类别2/3/4/5 → N/A）。

    规则（定稿）：evidence_timing 的时间戳须在 evidence_pass 指定通道
    上下文窗口（目标行 ±5 行）内，与某行 start_sec 差 ≤1.0s 即存在
    （两时间戳都给时按第一个 start 匹配）。类别1 缺
    side/evidence_timing/evidence_pass 任一 → 视同 FAIL。
    R2 追加：理由与选边明确矛盾（_reason_contradiction → FAIL，如
    #46 模式「理由通篇支持 pass1 却输出 side=pass2」）→ 直接 FAIL，
    即使证据时间轴真实存在；理由双侧均支持（LOW_FLAG）不改核验结果。
    R3 追加（v1 收尾）：引用文本真实性核验——微调轮出现模型编造
    「带真实时间轴的引用文本」（如 reason 引「お前の運動神経の良さを
    見て」并给真实时间轴，但该 pass 该行实为「…旦那様かっこ。」），
    骗过时间轴核验。故时间轴命中后追加：reason 中形似候选引用的
    日文片段（≥4 字符且含假名）须归一化子串命中窗口行文本，全部
    不命中 → FAIL；无片段/全部命中 → 不影响结果（只查否定性信号，
    不要求模型必须引用）。见 extract_cited_jp_fragments 的误伤约束。
    """
    if verdict["category"] != 1:
        return "N/A"
    if _reason_contradiction(verdict) == "FAIL":
        return "FAIL"
    if not verdict.get("side") or not verdict.get("evidence_pass") \
            or not verdict.get("evidence_timing"):
        return "FAIL"
    secs = parse_evidence_timestamps(verdict.get("evidence_timing", ""))
    if not secs:
        return "FAIL"
    win = context.get(verdict["evidence_pass"])
    if not win:
        return "FAIL"
    target = secs[0]       # 两时间戳都给时按 start 匹配
    for e in win:
        s, _ = _timing_span(e["timing"])
        if abs(s - target) <= EVIDENCE_TOL_SEC:
            if _cited_fragments_missing(verdict.get("reason") or "", win):
                return "FAIL"
            return "PASS"
    return "FAIL"


def apply_review(verdict: dict, verify: str) -> dict:
    """核验结果落进行结果（核验失败降级语义，定稿；R2 追加复核标记）：

    类别不变；核验 FAIL → 置信强制 LOW；（证据核验列=FAIL，排序沉底
    与高置信排除由调用方按 verify 判定，本函数只做字段级降级）。
    R2 追加：理由双侧均支持（LOW_FLAG）→ 置信强制 LOW 并在依据说明
    前追加 ``[需人工复核]`` 标记（不改变核验结果本身）。
    """
    out = dict(verdict)
    out["verify"] = verify
    if verify == "FAIL":
        out["confidence"] = "low"
    if _reason_contradiction(verdict) == "LOW_FLAG":
        out["confidence"] = "low"
        reason = out.get("reason") or ""
        if not reason.startswith(_MANUAL_FLAG):
            out["reason"] = f"{_MANUAL_FLAG}{reason}"
    return out


# ======================================================================
# 排序（设计稿第 3 节，四级键 + 影片两级规则）
# ======================================================================

def film_sort_key(film_stat: dict) -> tuple:
    """影片排序键：审核全覆盖 → 估计真问题率降序；否则按必看行总量降序。

    film_stat: {"name","must_total","must_done","cat_counts","true_problem_rate"}。
    返回 (-主键数值, name)。
    """
    if film_stat["must_done"] >= film_stat["must_total"] > 0:
        return (-film_stat["true_problem_rate"], film_stat["name"])
    return (-film_stat["must_total"], film_stat["name"])


def row_sort_key(row: dict) -> tuple:
    """行四级排序键：类别优先级 → 核验沉底 → 相似度升序 → 时间轴。"""
    return (
        CATEGORY_PRIORITY[row["_category"]],
        1 if row["_verify"] != "PASS" else 0,     # 类内核验FAIL/N/A沉底
        row["_sim"],
        row["_start"] if row["_start"] >= 0 else 0.0,
    )


def sort_output_rows(rows: list, film_stats: list) -> list:
    """总排序：片顺序（film_stats 已按 film_sort_key 排好）→ 行内 row_sort_key。

    类别 5 整体排最后且其前插入注释行 CATEGORY5_HEADER（以 dict
    {"__comment__": 文本} 嵌入）。
    """
    out = []
    for st in film_stats:
        film_rows = [r for r in rows if r["_film"] == st["name"]]
        film_rows.sort(key=row_sort_key)
        non5 = [r for r in film_rows if r["_category"] != 5]
        cat5 = [r for r in film_rows if r["_category"] == 5]
        out.extend(non5)
        if cat5:
            out.append({"__comment__": CATEGORY5_HEADER})
            out.extend(cat5)
    # 防御：film_stats 未覆盖的片按名称尾随（正常不会发生）
    known = {st["name"] for st in film_stats}
    rest = sorted((r for r in rows if r["_film"] not in known),
                  key=lambda r: (r["_film"],) + row_sort_key(r))
    out.extend(rest)
    return out


# ======================================================================
# 统计块（头部 # 注释行）
# ======================================================================

def compute_film_stat(name: str, group_rows: list, reviewed_ids: set) -> dict:
    """单片统计。group_rows = 该片 CSV 全部分组行（必看/可选，未过滤）；

    reviewed_ids = 该片本次实际审核的行 id 集合。返回统计 dict：
    {"name","must_total","must_done","cat_counts","true_problem_rate"}。
    估计真问题率 = (类别1+类别2)/该片必看行数（必看行数为 0 时记 None）。
    """
    must_total = sum(1 for r in group_rows if r["分组"] == "必看")
    reviewed = [r for r in group_rows if r["id"] in reviewed_ids]
    cat_counts = {i: 0 for i in CATEGORIES}
    for r in reviewed:
        cat_counts[r["_category"]] = cat_counts.get(r["_category"], 0) + 1
    rate = (cat_counts[1] + cat_counts[2]) / must_total if must_total else None
    return {
        "name": name,
        "must_total": must_total,
        "must_done": sum(1 for r in reviewed if r["分组"] == "必看"),
        "cat_counts": cat_counts,
        "true_problem_rate": rate,
    }


def high_confidence_count(rows_with_verify: list | None) -> int:
    """高置信行数 = 类别1/2 且核验 PASS 的行数（核验FAIL/N/A 不计入）。

    rows_with_verify 为 None 时返回 0（供空输入兜底）。
    """
    if not rows_with_verify:
        return 0
    return sum(1 for r in rows_with_verify
               if r.get("_category") in (1, 2) and r.get("_verify") == "PASS")


def build_stat_block(film_stats: list, all_rows: list,
                     version: str = DEFAULT_PROMPT_VERSION) -> list:
    """统计块完整行（含真实高置信行数；末行标注提示词版本，审计留痕）。"""
    lines = [f"# 上下文预审复核增强（生成 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）"]
    for st in film_stats:
        must = st["must_total"]
        rate = "N/A" if st["true_problem_rate"] is None \
            else f"{st['true_problem_rate'] * 100:.1f}%"
        cats = " ".join(f"类别{i}={st['cat_counts'][i]}"
                        for i in sorted(CATEGORIES))
        lines.append(
            f"# 影片 {st['name']}：必看行 {must}，本次已审核 "
            f"{sum(st['cat_counts'].values())} 行（{cats}），"
            f"估计真问题率 {rate}")
    total_cat = {i: sum(st["cat_counts"][i] for st in film_stats)
                 for i in CATEGORIES}
    total_reviewed = sum(total_cat.values())
    high = high_confidence_count(all_rows)
    lines.append(
        f"# 总计：已审核 {total_reviewed} 行（" +
        " ".join(f"类别{i}={total_cat[i]}" for i in sorted(CATEGORIES)) +
        f"），高置信行数 {high}")
    lines.append(f"# 提示词版本: {PROMPT_VERSION_LABELS[version]}")
    return lines


# ======================================================================
# usage 埋点
# ======================================================================

def usage_summary(call_records: list) -> dict:
    """usage 汇总（纯函数）。call_records: [{"rows","prompt_tokens",
    "completion_tokens","elapsed_sec","parse_fail"}]。"""
    n = len(call_records)
    rows = sum(c.get("rows", 0) for c in call_records)
    pt = sum(c.get("prompt_tokens", 0) for c in call_records)
    ct = sum(c.get("completion_tokens", 0) for c in call_records)
    el = sum(c.get("elapsed_sec", 0.0) for c in call_records)
    return {
        "total_rows": rows,
        "total_calls": n,
        "total_prompt_tokens": pt,
        "total_completion_tokens": ct,
        "tokens_per_row": round((pt + ct) / rows, 1) if rows else 0.0,
        "total_elapsed_sec": round(el, 3),
        "avg_sec_per_row": round(el / rows, 3) if rows else 0.0,
    }


def print_usage_summary(call_records: list) -> None:
    """stdout 打印 usage 汇总（中文）。"""
    s = usage_summary(call_records)
    print(f"📊 性能汇总：总行数 {s['total_rows']}，总 call 数 {s['total_calls']}，"
          f"总 token {s['total_prompt_tokens'] + s['total_completion_tokens']}"
          f"（每行均摊 {s['tokens_per_row']}），"
          f"总耗时 {s['total_elapsed_sec']:.1f}s"
          f"（平均每行 {s['avg_sec_per_row']:.2f}s）")


def write_usage_sidecar(out_path: Path, call_records: list) -> None:
    """写 sidecar {output路径}.usage.json（per-call 明细+汇总）。"""
    payload = {"calls": call_records,
               "summary": usage_summary(call_records)}
    Path(str(out_path) + ".usage.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ======================================================================
# 输出 CSV
# ======================================================================

def _format_category(cat: int) -> str:
    return f"{cat} {CATEGORIES[cat]}"


def _format_evidence(verdict: dict) -> str:
    """依据列：`pass1@00:01:23,456` 格式（类别1 且证据齐全时）。"""
    if verdict["category"] != 1:
        return ""
    if not verdict.get("evidence_pass") or not verdict.get("evidence_timing"):
        return ""
    return f"{verdict['evidence_pass']}@{verdict['evidence_timing']}"


def write_enhanced_csv(out_path: Path, rows: list, film_stats: list,
                       version: str = DEFAULT_PROMPT_VERSION) -> None:
    """写增强复核 CSV（utf-8-sig，头部 # 统计注释块）。

    rows: 已排序（sort_output_rows 产物，含类别5注释行 dict）。
    末列「提示词版本」写入 version（A/B 审计留痕）。
    out_path 由 _default_output/_guard_output 产出（守卫已过），
    此处二次校验防御（安全复查项：输出汇点不得裸 open）。
    """
    if _guard_output(out_path) is None:
        raise SystemExit(f"❌ 输出路径越界（须位于项目目录内）: {out_path}")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for line in build_stat_block(film_stats, rows, version):
            w.writerow([line])
        w.writerow(ORIGINAL_COLUMNS + EXTRA_COLUMNS)
        for r in rows:
            if "__comment__" in r:
                w.writerow([r["__comment__"]])
                continue
            v = r["_verdict"]
            w.writerow([
                r["文件名"], r["分组"], r["时间轴"], r["相似度"], r["iou"],
                r["offset"], r["artifact"], r["pass1"], r["pass2"],
                r["final_cn译文"],
                _format_category(r["_category"]),
                v.get("side") or "",
                _format_evidence(v),
                r["_verify"],
                r["_confidence"],
                (v.get("reason") or "")[:REASON_CLIP],
                version,
            ])


# ======================================================================
# 主流程
# ======================================================================

def _assign_ids(rows_by_film: "OrderedDict[str, list]") -> None:
    """给每行赋唯一 id：`{片名}#{CSV内行号}`，并回写 _film。"""
    for name, rows in rows_by_film.items():
        for i, r in enumerate(rows):
            r["id"] = f"{name}#{i}"
            r["_film"] = name


def _dry_run_verdict() -> dict:
    """dry-run 占位 verdict（结构与 LLM 输出一致）。"""
    return {"category": 4, "side": None, "evidence_timing": "",
            "evidence_pass": None, "reason": "dry-run", "confidence": "low"}


def _guard_output(path: Path) -> Path:
    """输出路径安全闸：规范化解析，拒绝越界写入目标。

    与路径守卫 _guard_path 同型（安全复查项）：
    --output 是 CLI 输入面，直接 Path() 写文件无拦截会把任意路径
    当输出目标。放行两类位置：项目目录内，以及系统临时目录
    （tempfile.gettempdir()——离线单测端到端用例在仓库外的 pytest
    tmp_path 下运行，属工具的合法使用场景）；其余一律拒绝
    （打印错误并返回 None，由调用方以非零码退出）。
    """
    raw = str(path).replace("\\", "/")
    # 跨平台越界判定：Windows 盘符绝对路径（如 D:/x.csv）在 POSIX 下会被
    # resolve() 折进 cwd/项目内，先按越界拒绝；本机原生绝对路径仍交由
    # 下方白名单（项目内 / 系统临时目录）裁决，Windows 行为不变。
    if PureWindowsPath(raw).is_absolute() and not Path(raw).is_absolute():
        print(f"❌ 输出路径越界（须位于项目目录内）: {raw}")
        return None
    p = Path(raw).resolve()
    root = Path(__file__).resolve().parents[1]
    tmp = Path(tempfile.gettempdir()).resolve()
    inside_root = str(p).startswith(str(root) + os.sep) or p == root
    inside_tmp = str(p).startswith(str(tmp) + os.sep) or p == tmp
    if not (inside_root or inside_tmp):
        print(f"❌ 输出路径越界（须位于项目目录内）: {p}")
        return None
    return p


def _default_output(args, csv_paths: list) -> Path:
    """默认输出路径：单 CSV 输入 → 同目录 {stem}_复核_增强.csv；

    目录/多片 → {dir}/上下文预审_复核_增强_{时间戳}.csv。
    --output 指定时经 _guard_output 守卫（拒绝仓库外路径——
    本工具为离线审计工具，输出应始终落在项目目录内）。
    """
    if args.output:
        guarded = _guard_output(Path(args.output))
        if guarded is None:
            return None
        return guarded
    if len(csv_paths) == 1 and csv_paths[0].is_file():
        stem = csv_paths[0].name[:-len(CSV_SUFFIX)]
        return csv_paths[0].parent / f"{stem}_复核_增强.csv"
    base = args.input if hasattr(args, "input") else "."
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(base) / f"上下文预审_复核_增强_{ts}.csv"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="上下文预审：对双通道必看分歧行做 LLM 五分类审核，"
                    "产出增强复核 CSV（离线工具，不碰主管线）")
    ap.add_argument("input", help="分歧复核 CSV 路径或其所在目录（目录仅扫描"
                    "直接子文件，不递归；可同时处理多片）")
    ap.add_argument("--group", choices=["must", "optional", "both"],
                    default="must",
                    help="审核哪些分组（必看/可选/两者，默认 must；已过滤一律排除）")
    ap.add_argument("--sample", type=int, default=None,
                    help="跨片均匀抽样 N 行（round-robin 每片轮流取 1 行），"
                    "不给则全量")
    ap.add_argument("--seed", type=int, default=20260910,
                    help="抽样随机种子（默认 20260910）")
    ap.add_argument("--similarity-max", type=float, default=None,
                    help="只审相似度 ≤X 的行（减载开关①），默认不限")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help=f"每 call 打包行数（默认 {DEFAULT_BATCH_SIZE}）")
    ap.add_argument("--model", default=None,
                    help="LM Studio 模型名（默认自动取端点首个已加载模型）")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help=f"OpenAI 兼容端点（默认 {DEFAULT_ENDPOINT}）")
    ap.add_argument("--output", default=None,
                    help="输出增强 CSV 路径（默认按输入自动命名）")
    ap.add_argument("--prompt-version", choices=["A", "B"],
                    default=DEFAULT_PROMPT_VERSION,
                    help="提示词版本（D2 裁定 A/B 对照）：A=激进-无终稿"
                         "（默认，不渲染终稿列与 final_cn 上下文），"
                         "B=保守-含终稿（终稿仅供参考、不得作为选边依据）")
    ap.add_argument("--dry-run", action="store_true",
                    help="不调 LLM，所有行建议类别=4/置信 LOW/核验 N/A/"
                    "依据说明=dry-run（结构与流程验收用）")
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    csv_paths = collect_review_csvs(input_path)
    if not csv_paths:
        print(f"❌ 未找到任何 *{CSV_SUFFIX} 输入（input={args.input}）")
        return 1

    # ---- 加载各片 CSV + 兄弟 SRT ----
    rows_by_film = OrderedDict()    # 片名（CSV 文件名）→ 行列表
    films = {}                      # 片名 → {"pass1","pass2","final_cn"} 条目
    for c in csv_paths:
        name = c.name
        rows = load_review_rows(c)
        rows_by_film[name] = rows
        sibs = sibling_paths_for_csv(c)
        film_src = {}
        for ch, p in (("pass1", sibs["pass1"]), ("pass2", sibs["pass2"]),
                      ("final_cn", sibs["final_cn"])):
            film_src[ch] = parse_srt(p.read_text(encoding="utf-8")) \
                if p.is_file() else []
        films[name] = film_src
        print(f"📄 {name}：分组行 {len(rows)}（必看 "
              f"{sum(1 for r in rows if r['分组'] == '必看')} / 可选 "
              f"{sum(1 for r in rows if r['分组'] == '可选')}），"
              f"兄弟文件 pass1={'有' if film_src['pass1'] else '无'} "
              f"pass2={'有' if film_src['pass2'] else '无'} "
              f"final_cn={'有' if film_src['final_cn'] else '无'}")

    _assign_ids(rows_by_film)

    # ---- 筛选 + 抽样（顺序：分组→similarity-max→抽样）----
    picked_by_film = OrderedDict()
    for name, rows in rows_by_film.items():
        picked = filter_rows(rows, args.group, args.similarity_max)
        # 片内按相似度升序排好（抽样与审核顺序基准）
        picked.sort(key=lambda r: r["_sim"])
        picked_by_film[name] = picked
    if args.sample is not None:
        film_rows = [(name, picked_by_film[name]) for name in picked_by_film]
        picked_ids = sample_rows(film_rows, args.sample, args.seed)
        for name in picked_by_film:
            picked_by_film[name] = [r for r in picked_by_film[name]
                                    if r["id"] in picked_ids]
    total_picked = sum(len(v) for v in picked_by_film.values())
    print(f"🎯 待审核：{total_picked} 行（group={args.group}，"
          f"sample={'全部' if args.sample is None else args.sample}），"
          f"提示词版本 {PROMPT_VERSION_LABELS[args.prompt_version]}")
    if total_picked == 0:
        print("（无待审核行，直接产出仅含统计块的空 CSV）")

    # ---- client 准备 / 连通性探测 ----
    client = None
    if not args.dry_run:
        client = _LMStudioClient(args.endpoint, args.model or "")
        ok, msg = probe_endpoint(client)
        if not ok:
            print(f"❌ {msg}")
            print("（离线工具不云端补跑：请确认 LM Studio 已启动且模型已加载）")
            return 2
        if not args.model:
            models = client.list_models()
            if models:
                client._model = models[0]
                print(f"🤖 自动选择模型: {models[0]}")
        print(f"🔌 {msg}")

    # ---- 构建 + 审核上下文 ----
    contexts, skipped_locate = [], 0
    for _name, rows in picked_by_film.items():
        for r in rows:
            ctx = build_context(r, films)
            if ctx is None:
                skipped_locate += 1
                continue
            contexts.append(ctx)
    if skipped_locate:
        print(f"⚠️ {skipped_locate} 行 pass1/pass2 定位失败（跳过审核，"
              "不计入已审核数）")

    call_records = []
    reviewed_rows = []       # 已审核行（含结果字段）
    parse_fail_total = 0
    if args.dry_run:
        for ctx in contexts:
            v = apply_review(_dry_run_verdict(), "N/A")
            r = ctx["row"]
            r["_verdict"] = v
            r["_category"] = v["category"]
            r["_verify"] = "N/A"
            r["_confidence"] = "low"
            reviewed_rows.append(r)
        call_records.append({"rows": len(contexts), "prompt_tokens": 0,
                             "completion_tokens": 0, "elapsed_sec": 0.0,
                             "parse_fail": 0, "note": "dry-run"})
    else:
        bs = max(1, args.batch_size)
        for i in range(0, len(contexts), bs):
            chunk = contexts[i:i + bs]
            verdicts, usage = review_batch_with_client(
                client, chunk, version=args.prompt_version)
            call_records.append(usage)
            parse_fail_total += usage.get("parse_fail", 0)
            for no, ctx in enumerate(chunk, 1):
                v = verify_evidence(verdicts[no], ctx)
                final = apply_review(verdicts[no], v)
                r = ctx["row"]
                r["_verdict"] = final
                r["_category"] = final["category"]
                r["_verify"] = v
                r["_confidence"] = final["confidence"]
                reviewed_rows.append(r)
            print(f"  ✅ call {len(call_records)}：{usage['rows']} 行，"
                  f"{usage['elapsed_sec']:.1f}s，"
                  f"parse_fail {usage['parse_fail']}")
        if parse_fail_total:
            print(f"⚠️ 解析补位（缺行/字段非法→类别4）共 {parse_fail_total} 行")

    # ---- 统计 / 排序 / 输出 ----
    reviewed_ids = {r["id"] for r in reviewed_rows}
    film_stats = [compute_film_stat(name, rows_by_film[name], reviewed_ids)
                  for name in rows_by_film]
    film_stats.sort(key=film_sort_key)
    ordered = sort_output_rows(reviewed_rows, film_stats)

    out_path = _default_output(args, csv_paths)
    if out_path is None:          # --output 越界被安全闸拒绝
        return 1
    if str(out_path.parent):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    write_enhanced_csv(out_path, ordered, film_stats,
                       version=args.prompt_version)
    write_usage_sidecar(out_path, call_records)
    print_usage_summary(call_records)
    print(f"✅ 增强 CSV 已写入: {out_path}")
    print(f"   usage 明细: {out_path}.usage.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
