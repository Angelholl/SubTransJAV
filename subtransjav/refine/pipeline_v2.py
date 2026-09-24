"""
v2 两阶段流水线编排
====================
阶段A 净语+翻译（ja→zh，LLM×1）：一次调用完成噪音标记与翻译；
阶段B 审校+抛光（zh→zh，LLM×1）：对照日文原文（`日文 ||| 中文` 格式）
审核误译/补译/润色。D1 决策（v1.2.1）：删除权收归闸门0，下游一律
不物理删条，无法给出译文的条目加 [未翻译] 标记保留。

效率设计：
- 每批 LLM 调用 4+ 次（legacy 4 阶段）→ 2 次；
- TM 精确命中行直接替代译文，零 LLM 调用；
- 失败行由客户端定向重试 → 仍失败交阶段B补译 → 仍失败保留原文并加
  [未翻译] 标记（宁多勿缺）。

兜底规则层（profile 驱动，无需人工维护）：
- local  → strict：cleaner_rules 清洗 + post_validate 误译拦截（默认，本地弱模型）
- cloud  → lenient：仅格式/语言白名单（强模型产出可靠，避免规则误伤）

角色卡：config/templates/角色-净语翻译.txt（阶段A）、角色-审校抛光.txt（阶段B）。
"""

import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .asr_meta import SUSPECT_STATUSES, load_asr_meta
from .config import (
    DEEPSEEK_BASE_DEFAULT,
    DEFAULT_PREMERGE_MAX_ITEMS,
    RefineConfig,
    ensure_language_support,
)
from .events import EventEmitter
from .filters import build_srt, parse_srt
from .glossary import format_glossary_block, match_glossary
from .glossary_conflict import (
    append_watch_record,
    default_watch_path,
    evaluate_watch,
    load_watch_records,
    scan_glossary_conflicts,
    write_conflict_csv,
)
from .instructions import hardened_suffix, write_effective_instructions
from .manifest import (
    MANIFEST_VERSION,
    TaskManifest,
    compute_config_hash,
    compute_file_sha1,
    compute_glossary_sha1,
    delete_resume_artifacts,
    load_manifest,
    manifest_path,
    save_manifest,
    validate_manifest,
)
from .pipeline_support import (
    CREATED_TMP_DIRS,
    RefineError,
    _init_tm,
    _resolve_stage_paths,
    _tmp_dirs_lock,
    learned_glossary_path,
    load_glossary_merged,
    refine_tmp_dir,
)
from .risk import SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_WARNING, RiskCollector
from .source_hallucination import (
    apply_source_filter,
    is_fluent_zh,
    quarantine_review,
    strong_garble_signal,
)
from .synopsis import (
    SYNOPSIS_TIMEOUT_CAP_S,
    SYNOPSIS_TIMEOUT_DEFAULT_S,
    build_synopsis_input,
    request_synopsis,
)
from .tm import TranslationMemory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# v2 阶段定义
# ---------------------------------------------------------------------------

V2_STAGE_TAGS = ("A", "B")
V2_STAGE_NAMES = {"A": "阶段A 净语+翻译", "B": "阶段B 审校+抛光"}
# v2 阶段复用 legacy 各阶段槽位的服务商/模型配置：A→stages[0]，B→stages[2]
V2_STAGE_SLOT = {"A": 0, "B": 2}

V2_TEMPLATE_FILES = {
    "A": "角色-净语翻译.txt",
    "B": "角色-审校抛光.txt",
}

V2_STAGE_PROMPTS = {
    "A": (
        "请对以下日文字幕进行净语清洗并翻译成中文。"
        "回复必须严格按编号协议逐条回填：\n"
        "#<编号>\nTranslation>\n<该条目的中文译文>\n"
        "要求：\n"
        "- #N 与输入条目编号一一对应，不得跳号、不得重排编号\n"
        "- 每一行都必须给出译文，不得留空；"
        "若源文确属无法辨识的乱码/纯噪声，输出 `[未翻译]` 占位\n"
        "- 源文为转录乱码/残缺时：只直译可辨认的部分，禁止臆测情节、"
        "禁止补充原文不存在的动作或语义；严禁反转语义方向"
        "（拒绝↔邀请、停止↔继续、否定↔肯定等）；"
        "确实无法辨识时输出 `[未翻译]` 并保留原文，严禁从零编造\n"
        "- 其余条目按角色卡规范输出中文译文\n"
        "- 仅当源文确定为无实义的拟声/呻吟（纯假名噪声）时，"
        "译为中文拟声（唔…/嗯…/啊…）或省略号，"
        "禁止音译成假名词或生造汉字词（如把转写噪声音译成名词）；"
        "疑似误听词（见误听怀疑清单）不适用本条，按误听语义翻译\n"
        "- 禁止输出 .srt 时间码块、序号块、说明、总结或注释"
    ),
    "B": (
        "请对照日文原文审查并抛光以下中文字幕。"
        "输入每条 Original> 下为『日文原文 ||| 中文译文』；"
        "中文部分为空或带 [未翻译] 标记时，请直接根据日文原文补译。"
        "回复必须严格按编号协议逐条回填：\n"
        "#<编号>\nTranslation>\n<该条目的修正中文译文>\n"
        "要求：\n"
        "- #N 与输入条目编号一一对应，不得跳号\n"
        "- 无需修正的条目原样输出\n"
        "- 每一行都必须给出译文，不得留空；"
        "若源文确属无法辨识的乱码/纯噪声，输出 `[未翻译]` 占位\n"
        "- 源文为转录乱码/残缺时：只直译可辨认的部分，禁止臆测情节、"
        "禁止补充原文不存在的动作或语义；严禁反转语义方向"
        "（拒绝↔邀请、停止↔继续、否定↔肯定等）；"
        "确实无法辨识时输出 `[未翻译]` 并保留原文，严禁从零编造\n"
        "- 仅当源文确定为无实义的拟声/呻吟（纯假名噪声）时，"
        "译为中文拟声（唔…/嗯…/啊…）或省略号，"
        "禁止音译成假名词或生造汉字词（如把转写噪声音译成名词）；"
        "疑似误听词（见误听怀疑清单）不适用本条，按误听语义翻译\n"
        "- 禁止输出 .srt 时间码块、序号块、说明、总结或注释"
    ),
}

# P1-5 配置单一来源：DeepSeek 地址收口到 config.DEEPSEEK_BASE_DEFAULT
# （保留模块别名，历史引用不变）
DEEPSEEK_BASE_URL = DEEPSEEK_BASE_DEFAULT
UNTRANSLATED_PREFIX = "[未翻译] "

# [未翻译] 标记裸形态（无尾空格，与 post_validate._UNTRANSLATED_MARK 同值；
# 因循环导入风险本地再声明，判定口径统一走 is_untranslated_text）。
_UNTRANSLATED_MARK_LOCAL = "[未翻译]"


def _normalize_untranslated_marker(text: str, orig_text: str) -> str:
    """A2 残译清洗：[未翻译] 前缀后跟非空残译文（如 "[未翻译] Chicks。"）
    的污染形态规范化。

    - 原文可得 → UNTRANSLATED_PREFIX + 日文原文（与阶段A 失败回退一致）；
    - 原文不可得 → 剥掉残译文只留纯前缀 "[未翻译]"；
    - 纯占位形态（前缀后无内容）原样返回，防二次加标；
    - 只做 strip 后开头匹配，正文中部合法出现 "[未翻译]" 的译文不碰。
    """
    s = (text or "").strip()
    if not s.startswith(_UNTRANSLATED_MARK_LOCAL):
        return text
    residue = s[len(_UNTRANSLATED_MARK_LOCAL):].strip()
    if not residue:
        return text                    # 纯占位：保持现状
    orig = (orig_text or "").strip()
    return UNTRANSLATED_PREFIX + orig if orig else UNTRANSLATED_PREFIX.rstrip()


# 闸门0 删除样本上限（供质量报告【处置】章节与归档日志，防大文件撑爆）
_GATE0_REPORT_SAMPLE_CAP = 50

# LLM 客户端 ⏳ 进度文本（"批次 3/63"）→ phase_progress 事件载荷的解析规则
_BATCH_PROGRESS_RE = re.compile(r"批次\s*(\d+)\s*/\s*(\d+)")

# D5 乱码强译复核：源文含汉字判定（含汉字即视为实义行，不入复核候选）
_KANJI_SRC_RE = re.compile(r"[\u4e00-\u9fff]")

# ---------------------------------------------------------------------------
# v1.2.2 C1 per-片语境 sidecar（{stem}.context.md，与输入 srt 同目录同名）：
# 一机制两用途——剧情摘要块（A/B 提示词均注入）+ 误听怀疑表（按当前批次
# 源文命中注入词条，复用 glossary 的命中风格）。挂载方式仿 glossary 块。
# ---------------------------------------------------------------------------
_SIDECAR_SUMMARY_TAG = "【剧情摘要】"
_SIDECAR_MISHEAR_TAG = "【误听怀疑】"
_SIDECAR_SUMMARY_HEADER = "【剧情摘要 - 语境参考】"
_SIDECAR_MISHEAR_HEADER = "【误听怀疑对照】"
# 冻结措辞（验收口径逐字比对，勿改动任何字）：
# 剧情摘要块引导语
SIDECAR_SUMMARY_NOTICE = (
    "以下为剧情背景参考，仅用于消解歧义；与单句字面义和术语表冲突时，"
    "以句子本身和术语表为准；不得改写原文中没有的信息。")
# 误听怀疑词条措辞（{疑似词}/{疑似正解} 为占位符）
SIDECAR_MISHEAR_NOTICE = (
    "该词在 ASR 转写中曾出现误听（{疑似词}→疑为{疑似正解}）。"
    "仅当上下文无法按字面义解读、且存在语义更合理的解读时方可按疑似义翻译；"
    "可两可时一律照字面译。")

# ---------------------------------------------------------------------------
# v1.2.2 Beta 剧情自摘要：闸门0+预合并后自动抽样整片剧情行，一次独立
# LLM 调用生成梗概，注入 A/B 提示词。手写 sidecar【剧情摘要】非空时
# 手写优先；任何失败静默跳过。摘要文本只进提示词，绝不写入输出目录/
# 终稿/质量报告；摘要缓存独立于 TM（Temp/synopsis_cache/）。摘要内容
# 与缓存不参与 manifest 指纹（与 sidecar 内容同款已知边界：换
# --s1-model 或改采样行为后须 --force 重跑方生效，见手册 §11）。
# ---------------------------------------------------------------------------
_SYNOPSIS_BLOCK_TAG = "【剧情背景（自动摘要·beta）】"


def _synopsis_prompt_block(synopsis_text: str | None) -> str:
    """组装自动摘要注入块（A/B 同措辞；引导语沿用 sidecar 冻结措辞）。"""
    if not synopsis_text or not synopsis_text.strip():
        return ""
    return "\n".join([_SYNOPSIS_BLOCK_TAG,
                      SIDECAR_SUMMARY_NOTICE,
                      synopsis_text.strip()])

# ---------------------------------------------------------------------------
# P1-6 语法提示跨阶段缓存：键 (sha1(条目文本), 阶段tag, profile)。
# A/B 两阶段与多文件批次共享；同一文本（ASR 重复行极常见）只分析一次。
# 容量满 50000 直接清空（防无限膨胀；负缓存 None 同样入缓存）。
# ---------------------------------------------------------------------------
_GRAMMAR_CACHE: dict = {}
_GRAMMAR_CACHE_MAX = 50000
_GRAMMAR_CACHE_LOCK = threading.Lock()


def _grammar_cache_key(text: str, tag: str, profile: str) -> tuple:
    return (hashlib.sha1((text or "").encode("utf-8")).hexdigest(),
            tag, profile)


def _emit_batch_progress(emitter, phase, message) -> None:
    """把 LLM 客户端的 ⏳ 进度文本映射为 phase_progress 事件（ndjson 模式）。

    无法解析的进度文本静默跳过（人类可读通道照常 print）。
    """
    if emitter is None:
        return
    m = _BATCH_PROGRESS_RE.search(message or "")
    if not m:
        return
    done, total = int(m.group(1)), int(m.group(2))
    emitter.emit("phase_progress", phase=phase,
                 payload={"batch": done, "done": done, "total": total})


def _glossary_fingerprint(cfg) -> str | None:
    """词库指纹：人工词库与自动学习词库（glossary_learned.csv）联合 sha1。

    两者都缺失 -> None（校验时跳过）；任一存在则参与联合指纹，
    保证自动学习追加的词库变化也会让旧产物失效。
    """
    parts = [
        compute_glossary_sha1(getattr(cfg, "glossary_path", "") or None),
        compute_glossary_sha1(learned_glossary_path()),
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# TM 指纹参与的内容列（按 content_hash, stage 稳定排序，跨进程确定性）。
# 刻意排除 hit_count / created_at 等簿记列（D5）：阶段A 每次精确命中都会
# 自增 hit_count 并提交（tm.exact_map / lookup_exact），变更还可能滞留
# WAL、被下一次连接打开时回放——若对整文件做 sha1，manifest 创建后 TM
# 文件字节几乎必然变化，同配置 --resume 必拒绝复用阶段A。
_TM_FINGERPRINT_COLUMNS = ("content_hash", "stage", "source_text", "target_text")


def _tm_fingerprint(cfg, tm) -> str | None:
    """翻译记忆库指纹：tm_entries 内容列按稳定排序的流式 sha1（D5）。

    - 只取内容列（_TM_FINGERPRINT_COLUMNS），排除 hit_count/created_at
      等簿记列：命中只改簿记、不改翻译结果，不应使已有阶段产物失效；
      内容条目新增/修改则指纹必变。
    - 未启用/文件缺失/无表/文件损坏 -> None（校验侧按"跳过"处理）。
    """
    db = getattr(tm, "db_path", None) if tm is not None else None
    if not db:
        db = getattr(cfg, "tm_db_path", "") or None
    if not db or not Path(db).is_file():
        return None
    digest = hashlib.sha1()
    try:
        conn = sqlite3.connect(db)
        try:
            sql = (f"SELECT {', '.join(_TM_FINGERPRINT_COLUMNS)} "
                   "FROM tm_entries ORDER BY content_hash, stage")
            cursor = conn.execute(sql)      # 游标只建一次，fetchmany 顺序推进
            while True:
                rows = cursor.fetchmany(4096)
                if not rows:
                    break
                for row in rows:
                    digest.update(
                        "\x1f".join(str(v) for v in row).encode("utf-8"))
                    digest.update(b"\n")
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return digest.hexdigest()


def _v2_models_payload(cfg: RefineConfig) -> dict:
    """清单用模型信息：{"A": {provider,model,endpoint}, "B": {...}}。"""
    models = {}
    for tag in V2_STAGE_TAGS:
        s = cfg.stages[V2_STAGE_SLOT[tag]]
        models[tag] = {
            "provider": s.provider,
            "model": cfg.resolve_model(s) or "",
            "endpoint": cfg.resolve_endpoint(s.provider) or "",
        }
    return models


def _remove_tmp_dir(tmp_dir: str) -> None:
    """删除本文件的流水线临时工作区（忽略错误，不抛异常）。"""
    import shutil
    if tmp_dir and Path(tmp_dir).is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# 代码层预合并（断句修复）：ASR 常把一句话切成多条碎片。
# 规则来自「日文净语者」角色卡的预处理标准（时间相邻 ≤0.5s 且语义断裂、
# 或助词/省略号结尾 ≤1.0s 强制合并；完整句/一问一答/超长不合并）。
# 合并后条目时间轴 = 首条起点 ~ 末条终点，文本直接拼接。
# ---------------------------------------------------------------------------
_PREMERGE_AUX_END = re.compile(
    r"(?:は|が|を|に|で|て|し|から|ので|って|けど|けども|ー)\s*$")
_PREMERGE_INCOMPLETE_START = re.compile(
    r"^(?:あの|その|えっと|なんか|でも|だから|それで|そして|つまり|"
    r"けど|が|で|ちょっと|でもね|それでね)")
_PREMERGE_COMPLETE_END = re.compile(
    r"(?:です|ます|んだ|のだ|よね|ない|た|だ|か[。？?]?|[。！？?！])\s*$")
# 省略号/波浪线收尾：刻意的戏剧停顿，不参与 ≤1.0s 强制合并档
_PREMERGE_TRAILING = re.compile(r"(?:…|⋯|〜|~)\s*$")
# 连续的尾部省略号/波浪线（可重叠多个）：剥离后使句末判定作用于真实句尾
_PREMERGE_PAUSE_TAIL = re.compile(r"(?:…|⋯|〜|~)+\s*$")
# P1-5：阈值收口到 config（模块常量仅为无 cfg 直调时保留历史默认值）
_PREMERGE_MAX_SPAN = 5.0              # 无 cfg 直调时的回退跨度上限（秒），与 premerge_max_span_ms 默认 5000ms 对齐；premerge_max_gap_s 不再兼任此职
_PREMERGE_MAX_SPAN_MS = 5000          # 无 cfg 直调时的回退跨度硬上限（毫秒，= _PREMERGE_MAX_SPAN×1000）
_PREMERGE_MAX_COUNT = DEFAULT_PREMERGE_MAX_ITEMS  # 合并条数上限
_PREMERGE_MAX_CHARS = 80              # 无 cfg 直调时的回退合并文本字符上限
_PREMERGE_MIN_FRAGMENT_CHARS = 6      # 无 cfg 直调时的回退语义断裂档短碎片阈值（字符）


def _strip_trailing_pause(text):
    """剥离尾部连续省略号/波浪线（戏剧停顿标记），使句末判定作用于真实句尾。"""
    return _PREMERGE_PAUSE_TAIL.sub("", text)


def _premerge_entries(entries: list, cfg: RefineConfig = None) -> list:
    """断句修复：按角色卡预处理标准合并被 ASR 错误切割的相邻碎片。

    cfg 提供时使用 cfg.premerge_max_span_ms / cfg.premerge_max_items /
    cfg.premerge_max_chars / cfg.premerge_min_fragment_chars（用户可调）；
    缺省回退模块常量（兼容无 cfg 的直调/单测）。premerge_max_gap_s
    不再参与合并判定（跨度上限职责已移交 premerge_max_span_ms）。
    """
    max_span_ms = _PREMERGE_MAX_SPAN_MS
    max_chars = _PREMERGE_MAX_CHARS
    min_fragment = _PREMERGE_MIN_FRAGMENT_CHARS
    max_count = _PREMERGE_MAX_COUNT
    if cfg is not None:
        max_span_ms = int(getattr(cfg, "premerge_max_span_ms",
                                  _PREMERGE_MAX_SPAN_MS))
        max_chars = int(getattr(cfg, "premerge_max_chars", _PREMERGE_MAX_CHARS))
        min_fragment = int(getattr(cfg, "premerge_min_fragment_chars",
                                   _PREMERGE_MIN_FRAGMENT_CHARS))
        max_count = int(getattr(cfg, "premerge_max_items", _PREMERGE_MAX_COUNT))
    if not entries:
        return entries
    merged = []
    for e in entries:
        prev = merged[-1] if merged else None
        if prev is None:
            merged.append(dict(e))
            continue
        ps = _timing_span(prev["timing"])
        cs = _timing_span(e["timing"])
        if ps[0] < 0 or cs[0] < 0:
            merged.append(dict(e))
            continue
        gap = cs[0] - ps[1]
        merged_dur = cs[1] - ps[0]
        prev_text = (prev["text"] or "").strip()
        cur_text = (e["text"] or "").strip()
        if not prev_text or not cur_text:
            merged.append(dict(e))
            continue
        # 剥离尾部戏剧停顿标记后再做句末/助词判定（RC1：行尾省略号曾使
        # 「です/ます」句末判定失效，导致完整句被误判为断裂而合并）
        prev_core = _strip_trailing_pause(prev_text)

        # 合并判定（角色卡标准）
        allow = False
        if gap <= 1.0 and _PREMERGE_AUX_END.search(prev_core):
            allow = True                       # 助词结尾（剥离停顿后）→ ≤1.0s 强制
        elif gap <= 0.5 and (
                _PREMERGE_INCOMPLETE_START.match(cur_text)
                or (not _PREMERGE_COMPLETE_END.search(prev_core)
                    and len(cur_text) < min_fragment)):
            allow = True                       # 语义断裂 → ≤0.5s 合并（接续词档
                                               # 不限长度；「上行未完成 + 下行短
                                               # 碎片」档要求下行是短碎片）
        # 否决：前条以省略号/波浪线收尾（刻意的戏剧停顿），后条自身是
        # 完整句、或同样以停顿收尾 → 属独立字幕，不是 ASR 碎片（无论
        # gap 多小、走哪个档都不合并；如「ボクたち、水泳部の部長で…」+
        # 「誰もが一目置くエース。」）
        if _PREMERGE_TRAILING.search(prev_text) \
                and (_PREMERGE_COMPLETE_END.search(cur_text)
                     or _PREMERGE_TRAILING.search(cur_text)):
            allow = False
        # 禁止：两条都是完整陈述句（各自语义完整）
        if allow and _PREMERGE_COMPLETE_END.search(prev_text) \
                and _PREMERGE_COMPLETE_END.search(cur_text) and gap > 0.2:
            allow = False
        # 禁止：超长/超条数（RC3：独立硬跨度上限 ms；round() 防浮点毛刺，
        # 精确边界"5000ms 过 / 5001ms 拒"；文本按合并双方字符数之和设上限）
        n_prev = prev.get("_merge_count", 1)
        if round(merged_dur * 1000) > max_span_ms \
                or len(prev_text) + len(cur_text) > max_chars \
                or n_prev >= max_count:
            allow = False

        if allow:
            # 防护：正常情况下走到这里 timing 已通过 _timing_span 校验
            # （必含 "-->"）；万一格式异常则与解析失败同口径——放弃合并
            prev_parts = prev["timing"].split("-->")
            cur_parts = e["timing"].split("-->")
            if len(prev_parts) < 2 or len(cur_parts) < 2:
                merged.append(dict(e))
                continue
            prev["timing"] = prev_parts[0].strip() \
                + " --> " + cur_parts[1].strip()
            prev["text"] = prev_text + cur_text      # 日文碎片直接拼接
            prev["_merge_count"] = n_prev + 1
        else:
            merged.append(dict(e))
    out = []
    for e in merged:
        e.pop("_merge_count", None)
        out.append(e)
    return out


def _timing_span(timing: str) -> tuple:
    """解析时间轴为 (start_sec, end_sec)；解析失败返回 (-1, -1)。

    时间轴是字幕条目的真实身份标识：cleaner 等环节会重新编号，
    index 不可作为跨环节对齐依据，时间轴可以。
    """
    m = re.match(
        r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
        (timing or "").strip())
    if not m:
        return (-1.0, -1.0)
    g = list(map(int, m.groups()))
    start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
    end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
    return (round(start, 3), round(end, 3))


def _align_orig_by_timing(entries: list, orig_entries: list) -> list:
    """按时间轴把 orig 条目顺序对齐到 entries（双指针，容忍同起点多条）。

    两个列表都必须按时间轴升序（cleaner/流水线产物天然如此）。
    返回与 entries 等长的列表：元素为对应 orig 条目，无法对齐为 None。
    - 起点相同的多条：按顺序逐条消费，不会互相覆盖；
    - 合并行（span 被延长）：对齐到其首行 orig，且不消费指针，
      使被合并的后续 orig 行自然落到 None（不参与对齐/学习）。
    """
    aligned = []
    p = 0
    n = len(orig_entries)
    eps = 1e-6
    for e in entries:
        start = _timing_span(e["timing"])[0]
        span = _timing_span(e["timing"])
        while p < n and _timing_span(orig_entries[p]["timing"])[0] < start - eps:
            p += 1
        if p < n and abs(_timing_span(orig_entries[p]["timing"])[0] - start) <= eps:
            aligned.append(orig_entries[p])
            if _timing_span(orig_entries[p]["timing"]) == span:
                p += 1              # 精确 1:1 匹配才消费；合并行不消费
        else:
            aligned.append(None)
    return aligned


# ---------------------------------------------------------------------------
# 指令/客户端构建
# ---------------------------------------------------------------------------


def _v2_glossary_block(cfg: RefineConfig, tag: str, src_text: str,
                       glossary: list) -> str:
    """按阶段复用 legacy 词库生效开关：A→stage1 开关，B→stage2 开关。"""
    if not glossary:
        return ""
    enabled = (tag == "A" and cfg.apply_glossary_stage1) or \
              (tag == "B" and cfg.apply_glossary_stage2)
    if not enabled:
        return ""
    hits = match_glossary(src_text, glossary)
    if hits:
        print(f"   📚 词库命中 {len(hits)} 条，注入提示词")
        return format_glossary_block(hits)
    return ""


def load_context_sidecar(in_path: str) -> dict | None:
    """加载 per-片语境 sidecar（v1.2.2 C1）。

    文件约定：与输入 srt 同目录同名，后缀 ``.context.md``（如
    ``movie.srt`` -> ``movie.context.md``）。两小节：
      【剧情摘要】自由文本若干行；
      【误听怀疑】每行 ``疑似词 => 疑似正解``（如 ``ペソ => おへそ``）。

    文件不存在返回 None（=无注入）；解析按可得内容降级（缺小节=该块
    不注入；# 开头行视为模板注释跳过；误听行缺 "=>" 或侧为空则跳过）。
    返回 {"summary": [str, ...], "mishear": [(疑似词, 疑似正解), ...]}。
    """
    p = Path(in_path).with_suffix(".context.md")
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        print(f"   ⚠️ 语境 sidecar 读取失败，忽略: {p.name} ({e})")
        return None
    summary: list = []
    mishear: list = []
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(_SIDECAR_SUMMARY_TAG):
            section = "summary"
            rest = s[len(_SIDECAR_SUMMARY_TAG):].strip()
            if rest:
                summary.append(rest)
            continue
        if s.startswith(_SIDECAR_MISHEAR_TAG):
            section = "mishear"
            continue
        if not s or s.startswith("#"):
            continue
        if section == "summary":
            summary.append(s)
        elif section == "mishear" and "=>" in s:
            a, _, b = s.partition("=>")
            a, b = a.strip(), b.strip()
            if a and b:
                mishear.append((a, b))
    return {"summary": summary, "mishear": mishear}


def _load_context_sidecar(cfg: RefineConfig, in_path: str) -> dict | None:
    """cfg 开关接线的 sidecar 加载：context_sidecar=False 禁用（返回 None）；
    文件不存在=无注入。加载成功打印摘要行（可见性）。"""
    if not getattr(cfg, "context_sidecar", True):
        return None
    sidecar = load_context_sidecar(in_path)
    if sidecar and (sidecar["summary"] or sidecar["mishear"]):
        print(f"   📄 语境 sidecar: 剧情摘要 {len(sidecar['summary'])} 行 / "
              f"误听怀疑 {len(sidecar['mishear'])} 条"
              f"（{Path(in_path).with_suffix('.context.md').name}）")
    return sidecar


def _v2_sidecar_block(src_text: str, sidecar: dict | None) -> str:
    """组装语境 sidecar 注入块（A/B 同措辞；仿 glossary 块的命中风格）：

    - 剧情摘要块：sidecar 存在且小节非空即注入（冻结措辞引导语在前）；
    - 误听怀疑块：仅当当前批次源文含疑似词才注入该词条（未命中条目
      不注入，防无关词条噪音）。
    两块均无内容时返回空串（不挂载）。
    """
    if not sidecar:
        return ""
    parts = []
    summary = [ln for ln in (sidecar.get("summary") or []) if ln.strip()]
    if summary:
        parts.append("\n".join([_SIDECAR_SUMMARY_HEADER,
                                SIDECAR_SUMMARY_NOTICE] + summary))
    hits = [(a, b) for a, b in (sidecar.get("mishear") or [])
            if a and a in (src_text or "")]
    if hits:
        lines = [_SIDECAR_MISHEAR_HEADER]
        lines.extend(SIDECAR_MISHEAR_NOTICE.format(疑似词=a, 疑似正解=b)
                     for a, b in hits)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _ensure_auto_synopsis(cfg: RefineConfig, entries: list,
                          sidecar: dict | None,
                          collector=None, file_name: str = None) -> str | None:
    """剧情自摘要（Beta）入口：返回摘要文本；关闭/失败一律返回 None。

    前置条件：cfg.auto_synopsis 为 True 且手写 sidecar【剧情摘要】小节
    为空（手写优先，存在则记日志跳过自动摘要）。抽样/调用/缓存任一环节
    失败均静默降级（管线照常，不影响翻译结果）。
    """
    if not getattr(cfg, "auto_synopsis", False):
        return None
    # 手写优先：sidecar【剧情摘要】小节非空时不做自动摘要
    if sidecar and [ln for ln in (sidecar.get("summary") or [])
                    if ln.strip()]:
        print("   📄 手写剧情摘要存在，跳过自动摘要")
        return None
    try:
        sampled, meta = build_synopsis_input(
            entries, int(getattr(cfg, "synopsis_max_chars", 6000)))
    except Exception as e:
        print(f"   ℹ️ 剧情自摘要(beta): 失败跳过（采样失败: {e}）")
        return None
    if not sampled:
        print("   ℹ️ 剧情自摘要(beta): 失败跳过（无有效采样文本）")
        return None
    try:
        stage_cfg = cfg.stages[V2_STAGE_SLOT["A"]]
        provider = stage_cfg.provider
        model = cfg.resolve_model(stage_cfg) or _provider_default_model(
            cfg, provider)
        client = _make_client(cfg, "A")
        # 摘要调用独立配置：串行 + 独立超时（min(timeout_llm, 300s)）；
        # 输出预算由 request_synopsis 独立传 max_tokens=300，不走阶段A
        # 的批级 max_tokens 预算（compute_max_output_tokens）。
        with contextlib.suppress(Exception):
            client.config.timeout = min(
                float(getattr(cfg, "timeout_llm", SYNOPSIS_TIMEOUT_DEFAULT_S)),
                SYNOPSIS_TIMEOUT_CAP_S)
            client.config.concurrency = 1
        text = request_synopsis(client, sampled, provider=provider,
                                model=model)
    except Exception as e:
        print(f"   ℹ️ 剧情自摘要(beta): 失败跳过（{e}）")
        return None
    if not text:
        print("   ℹ️ 剧情自摘要(beta): 失败跳过（空输出）")
        return None
    spans = meta.get("bucket_ranges") or []
    first = min((r.get("first_index") for r in spans
                 if r.get("first_index") is not None), default="?")
    last = max((r.get("last_index") for r in spans
                if r.get("last_index") is not None), default="?")
    print(f"   📝 剧情自摘要(beta): 已生成 {len(text)} 字 "
          f"sha1={hashlib.sha1(text.encode('utf-8')).hexdigest()} "
          f"采样={meta.get('buckets', 0)}桶[#{first}~#{last}] "
          f"信息不足出现 {text.count('信息不足')} 次")
    return text


def _load_v2_instruction(cfg: RefineConfig, tag: str, gl_block: str,
                         tmp_dir: str, sidecar_block: str = "",
                         synopsis_block: str = "") -> tuple:
    """读取 v2 角色卡并组装指令。返回 (system_text, user_prompt)。"""
    from .config import default_templates_dir

    stage_cfg = cfg.stages[V2_STAGE_SLOT[tag]]
    td = cfg.templates_dir
    if not td or td in (".", "./", ".."):
        td = default_templates_dir()
    base_text = _read_v2_card(tag, stage_cfg.instructions, td)

    if "### prompt" in base_text:
        effective = base_text
    else:
        effective = (f"### prompt\n{V2_STAGE_PROMPTS[tag]}\n\n"
                     f"### instructions\n{base_text}\n")
    if tag == "B" and "硬性豁免规则" not in effective:
        effective += hardened_suffix()
    if gl_block:
        effective = effective.rstrip() + "\n\n" + gl_block + "\n"
    if sidecar_block:
        effective = effective.rstrip() + "\n\n" + sidecar_block + "\n"
    if synopsis_block:
        effective = effective.rstrip() + "\n\n" + synopsis_block + "\n"

    path = write_effective_instructions(
        effective, "", work_dir=tmp_dir, tag=f"v2_{tag}",
        fixed_name=f"refine_v2_{tag}.txt")
    return _split_instruction_file(path)


def _read_v2_card(tag: str, explicit_path: str, templates_dir: str) -> str:
    if explicit_path and os.path.isfile(explicit_path):
        return Path(explicit_path).read_text(encoding="utf-8")
    p = os.path.join(templates_dir, V2_TEMPLATE_FILES[tag])
    if not os.path.isfile(p):
        raise RefineError(f"v2 角色卡缺失：{p}")
    return Path(p).read_text(encoding="utf-8")


def _split_instruction_file(path: str) -> tuple:
    """把 '### prompt / ### instructions' 文件拆成 (system, user_prompt)。"""
    text = Path(path).read_text(encoding="utf-8")
    system_text = ""
    user_prompt = ""
    if "### instructions" in text:
        head, _, rest = text.partition("### instructions")
        user_prompt = head.replace("### prompt", "").strip()
        system_text = rest.strip()
    else:
        user_prompt = text.strip()
    return system_text, user_prompt


def _ensure_lmstudio_engine(cfg: RefineConfig, model: str, base_url: str,
                            label: str = ""):
    """lmstudio 阶段建客户端前对齐引擎状态。

    未加载 / 已载但 ctx 与 v2_ctx_local 不符时：卸载在载模型 → `lms load`
    带参加载（ctx/parallel/gpu 全部由管线配置派生，构造性保证引擎与管线
    两侧同步，D2026-0923-01 条件③）。
    独立成函数便于测试 monkeypatch（CI 无 LM Studio，不触网）。
    """
    from subtransjav.utils.lmstudio import ensure_lmstudio_model
    ok, msg = ensure_lmstudio_model(
        base_url, model, log=print,
        ctx_tokens=cfg.v2_ctx_local,
        parallel=max(1, cfg.v2_concurrency),
    )
    if not ok:
        raise RefineError(f"{label or 'LM Studio'}: {msg}")
    print(f"   ⚙️ LM Studio 引擎就绪: {model}（{msg}）")


def _make_client(cfg: RefineConfig, tag: str):
    """按阶段槽位的服务商配置构建 LLMClient。"""
    from subtransjav.translate.llm_client import ClientConfig, LLMClient

    stage_cfg = cfg.stages[V2_STAGE_SLOT[tag]]
    provider = stage_cfg.provider
    model = stage_cfg.model or _provider_default_model(cfg, provider)

    if provider == "deepseek":
        base_url = DEEPSEEK_BASE_URL
        api_key = cfg.resolve_api_key("deepseek")
        n_ctx = None
        temperature = cfg.temperature_cloud
        concurrency = max(1, cfg.v2_concurrency)
    elif provider == "lmstudio":
        base_url = cfg.resolve_endpoint("lmstudio") or "http://localhost:1234/v1"
        api_key = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")  # 本地端点占位
        n_ctx = cfg.v2_ctx_local
        temperature = cfg.temperature_local   # 本地实测最优低温（config 收口）
        concurrency = max(1, cfg.v2_concurrency)
    elif provider == "ollama":
        base_url = cfg.resolve_endpoint("ollama") or "http://localhost:11434/v1"
        api_key = "ollama"   # 本地服务免密钥，占位即可
        n_ctx = cfg.v2_ctx_local
        temperature = cfg.temperature_local
        concurrency = max(1, cfg.v2_concurrency)
    else:
        base_url = cfg.resolve_endpoint(provider)
        api_key = cfg.resolve_api_key(provider)
        n_ctx = None
        temperature = cfg.temperature_cloud
        concurrency = max(1, cfg.v2_concurrency)

    if not base_url:
        raise RefineError(f"{V2_STAGE_NAMES[tag]}: 服务商 [{provider}] 缺少接口地址")
    if not model:
        raise RefineError(f"{V2_STAGE_NAMES[tag]}: 未指定模型名")

    recovery = None
    if provider == "lmstudio":
        _ensure_lmstudio_engine(
            cfg, model, base_url,
            label=V2_STAGE_NAMES[tag])

        def recovery():
            # D2026-0924-02：运行期 "Model unloaded" 自动恢复——用与首载
            # 完全相同的参数重对齐引擎，保证指纹一致；回调异常由
            # llm_client 批循环捕获并转化为「现状路径」，不会炸批循环
            _ensure_lmstudio_engine(
                cfg, model, base_url,
                label=V2_STAGE_NAMES[tag])

    cc = ClientConfig(
        base_url=base_url, api_key=api_key or "", model=model,
        temperature=temperature, concurrency=concurrency, n_ctx=n_ctx,
        timeout=cfg.timeout_llm,
    )
    return LLMClient(cc, log=print, unloaded_recovery=recovery)


def _provider_default_model(cfg: RefineConfig, provider: str) -> str:
    from .config import PROVIDER_MODEL_DEFAULTS
    return PROVIDER_MODEL_DEFAULTS.get(provider, "")


# ---------------------------------------------------------------------------
# 阶段执行
# ---------------------------------------------------------------------------


@dataclass
class StageAResult:
    entries: list           # 阶段A产物条目 [{index,timing,text}]
    deleted: set            # 删除标记（D1 后恒空集：删除权收归闸门0，字段兼容保留）
    failed: set             # 重试后仍失败的 index（交阶段B补译）
    exact_hits: dict        # TM 精确命中 {index: zh}


def _make_fallback_client(cfg: RefineConfig):
    """本地接管客户端（LM Studio + fallback_model，低温串行）。"""
    from subtransjav.translate.llm_client import ClientConfig, LLMClient
    base_url = cfg.resolve_endpoint("lmstudio") or "http://localhost:1234/v1"
    _ensure_lmstudio_engine(cfg, cfg.fallback_model.strip(), base_url,
                            label="本地接管")

    def recovery():
        # D2026-0924-02：同 _make_client，运行期卸载自动恢复（同参重对齐）
        _ensure_lmstudio_engine(cfg, cfg.fallback_model.strip(), base_url,
                                label="本地接管")

    cc = ClientConfig(
        base_url=base_url,
        api_key=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"),  # 本地端点占位
        model=cfg.fallback_model.strip(),
        temperature=cfg.temperature_local,
        concurrency=1,
        n_ctx=cfg.v2_ctx_local,
        timeout=cfg.timeout_llm,
    )
    return LLMClient(cc, log=print, unloaded_recovery=recovery)


def _fallback_enabled(cfg: RefineConfig, tag: str) -> bool:
    """云端故障本地接管条件：仅云端阶段且已启用并配置接管模型。
    本地服务商（lmstudio/ollama）不启用云端故障后的本地接管。"""
    return (cfg.fallback_local and bool(cfg.fallback_model.strip())
            and cfg.stages[V2_STAGE_SLOT[tag]].provider not in ("lmstudio", "ollama"))


def _run_with_fallback(cfg: RefineConfig, tag: str, client,
                       todo: list, *, system_text: str, user_prompt: str,
                       max_batch_size: int, allow_empty_deletions: bool,
                       emitter=None):
    """云端阶段执行 + 故障接管：失败行（批失败/缺行）切换本地模型重跑一次。

    result.failed 中被本地接管修复的行移回 translations。
    emitter 非 None 时，⏳ 批次进度同步映射为 phase_progress 事件。
    """

    def _progress(m):
        print(f"   ⏳ {m}")
        _emit_batch_progress(emitter, tag, m)

    result = client.translate_entries(
        todo, system_text=system_text, user_prompt=user_prompt,
        max_batch_size=max_batch_size,
        allow_empty_deletions=allow_empty_deletions,
        progress=_progress)

    failed_entries = [e for e in todo if e["index"] in result.failed]
    if failed_entries and _fallback_enabled(cfg, tag):
        fb_model = cfg.fallback_model.strip()
        print(f"   ☁️→💻 云端接管: {len(failed_entries)} 条失败行"
              f"切换本地模型 {fb_model} 重跑")
        fb = _make_fallback_client(cfg)
        fb_result = fb.translate_entries(
            failed_entries, system_text=system_text, user_prompt=user_prompt,
            max_batch_size=min(30, max_batch_size),
            allow_empty_deletions=allow_empty_deletions)
        for idx, text in fb_result.translations.items():
            result.translations[idx] = text
            if idx in result.failed:
                result.failed.remove(idx)
        result.deleted |= fb_result.deleted
        result.failed = [i for i in result.failed if i not in fb_result.deleted]
    return result


def _collect_grammar_hints(context_entries: list, targets: list,
                           verbose: bool = True,
                           collector=None, file_name: str = None,
                           tag: str = "A", profile: str = "") -> dict:
    """对 targets 逐条生成语法提示（SudachiPy 句法分析，legacy 同款）。

    context_entries 提供条目上下文（前后条参与分析），targets 为需要
    提示的条目。未安装 sudachipy 或分析失败时返回空 dict（静默降级）。

    P1-6：结果按 (sha1(条目文本), tag, profile) 缓存（模块级，A/B 两阶段
    与多文件共享）；全部命中时跳过 build_srt 与逐条分析。
    """
    hints = {}
    misses = []
    with _GRAMMAR_CACHE_LOCK:
        if len(_GRAMMAR_CACHE) >= _GRAMMAR_CACHE_MAX:
            _GRAMMAR_CACHE.clear()
        for e in targets:
            key = _grammar_cache_key(e.get("text"), tag, profile)
            if key in _GRAMMAR_CACHE:
                hint = _GRAMMAR_CACHE[key]
                if hint:
                    hints[e["index"]] = hint
            else:
                misses.append((e, key))
    if not misses:
        return hints
    try:
        from .grammar_hint import generate_grammar_hints, is_grammar_hint_available
        if not is_grammar_hint_available():
            if verbose:
                print("   ℹ️ 语法提示: 未安装 sudachipy，跳过句法分析")
            return hints
        srt_content = build_srt(context_entries)
        for e, key in misses:
            hint = generate_grammar_hints(
                srt_content, e["index"], entries=context_entries)
            with _GRAMMAR_CACHE_LOCK:
                if len(_GRAMMAR_CACHE) >= _GRAMMAR_CACHE_MAX:
                    _GRAMMAR_CACHE.clear()
                _GRAMMAR_CACHE[key] = hint or None
            if hint:
                hints[e["index"]] = hint
    except Exception as e:
        if verbose:
            print(f"   ⚠️ 语法提示注入失败（忽略）: {e}")
        if collector is not None:
            collector.add(stage="A", file=file_name,
                          reason=f"语法提示生成失败: {e}",
                          action="跳过语法提示",
                          affected_count=len(targets),
                          severity=SEVERITY_INFO)
        return {}
    return hints


def _inject_stage_a_assists(cfg: RefineConfig, entries: list, todo: list,
                            tm, collector=None, file_name: str = None) -> list:
    """阶段A 输入辅助注入（只改发给 LLM 的文本，不影响 TM 键与产物）：
      1) 语法提示（SudachiPy 句法分析，legacy 同款；未安装则自动跳过）；
      2) TM 模糊命中参考（高阈值旧译文，标注仅供参考防照抄；不写库）。
    返回新的 todo 列表（原 entries 不变）。
    """
    todo = [dict(e) for e in todo]

    # 1) 语法提示
    hints = _collect_grammar_hints(entries, todo, collector=collector,
                                   file_name=file_name,
                                   tag="A", profile=cfg.v2_profile)
    if hints:
        print(f"   📝 语法提示已注入: {len(hints)}/{len(todo)} 条")

    # 2) TM 模糊命中参考（仅注入提示词，不替代译文、不写库）
    refs = {}
    if tm and cfg.tm_fuzzy_inject:
        try:
            for e in todo:
                hits = tm.lookup_fuzzy(
                    (e["text"] or "").strip(), 1,
                    threshold=cfg.tm_fuzzy_threshold)
                if hits:
                    refs[e["index"]] = hits[0][1]
            if refs:
                print(f"   💬 TM 模糊参考注入: {len(refs)}/{len(todo)} 条"
                      f"（阈值 {cfg.tm_fuzzy_threshold}，仅供参考）")
        except Exception as e:
            print(f"   ⚠️ TM 模糊参考注入失败（忽略）: {e}")
            if collector is not None:
                collector.add(stage="A", file=file_name,
                              reason=f"TM 模糊参考注入失败: {e}",
                              action="跳过TM模糊参考",
                              severity=SEVERITY_INFO)

    if not hints and not refs:
        return todo

    for e in todo:
        parts = []
        if e["index"] in hints:
            parts.append(hints[e["index"]])
        if e["index"] in refs:
            parts.append(f"【参考译文】{refs[e['index']]}"
                         f"（仅供参考，须结合当前上下文重新翻译，严禁照抄）")
        if parts:
            e["text"] = "\n".join(parts) + f"\n原文：{e['text']}"
    return todo


def _run_stage_a(cfg: RefineConfig, entries: list, tm, tmp_dir: str,
                 glossary: list, collector=None, file_name: str = None,
                 emitter=None, sidecar: dict | None = None,
                 synopsis: str | None = None) -> StageAResult:
    # 协议标记：供 webview_gui 检测阶段切换，更新进度显示（勿删）
    print(f"[STAGE] {V2_STAGE_NAMES['A']}", flush=True)
    print("\n🔹 [阶段A 净语+翻译] 一次调用完成清洗与日译中")

    # TM 精确命中替代：命中行零 LLM 调用（查阶段1 日→中 翻译对）。
    # P1-6：优先批量 exact_map（一次参数化 SQL）；无该接口的对象
    # （如测试 FakeTM）回退逐条 lookup_exact，语义不变。
    exact = {}
    if tm:
        batch_fn = getattr(tm, "exact_map", None)
        raw = None
        if batch_fn is not None:
            try:
                raw = batch_fn([(e["text"] or "").strip() for e in entries], 1)
            except Exception as e:
                print(f"   ⚠️ TM 批量查询失败，回退逐条（忽略）: {e}")
                raw = None
        for e in entries:
            src = (e["text"] or "").strip()
            hit = raw.get(src) if raw is not None else tm.lookup_exact(src, 1)
            # 防御：译文与原文相同（ja→ja 残留对）不替代，避免日文漏进中文产物
            if hit and hit.strip() != src:
                exact[e["index"]] = hit
        if exact:
            print(f"   💾 TM 精确命中 {len(exact)}/{len(entries)} 条，"
                  f"直接替代（跳过 LLM）")

    todo = [e for e in entries if e["index"] not in exact]

    # 辅助注入：语法提示（SudachiPy）+ TM 模糊命中参考
    todo = _inject_stage_a_assists(cfg, entries, todo, tm,
                                   collector=collector, file_name=file_name)

    src_text = "\n".join(e["text"] for e in entries)
    gl_block = _v2_glossary_block(cfg, "A", src_text, glossary)
    sidecar_block = _v2_sidecar_block(src_text, sidecar)
    system_text, user_prompt = _load_v2_instruction(
        cfg, "A", gl_block, tmp_dir, sidecar_block,
        _synopsis_prompt_block(synopsis))

    stage_cfg = cfg.stages[V2_STAGE_SLOT["A"]]
    client = _make_client(cfg, "A")
    result = _run_with_fallback(
        cfg, "A", client, todo, system_text=system_text,
        user_prompt=user_prompt, max_batch_size=cfg.batch_for(stage_cfg),
        # D1：禁用留空删除——空译文按缺行处理（客户端定向重试 → failed），
        # 最终仍无译文的行加 [未翻译] 标记保留（删除权收归闸门0）。
        allow_empty_deletions=False, emitter=emitter)

    out_entries = []
    for e in entries:
        i = e["index"]
        if i in exact:
            text = exact[i]
        elif i in result.translations:
            text = result.translations[i]
        else:
            # D1：不再物理删条——定向重试后仍失败的行（及若残余的删除
            # 标记）一律回退原文并加 [未翻译] 标记，交阶段B补译；阶段B
            # 仍失败则原样保留进终稿（宁多勿缺）。
            text = UNTRANSLATED_PREFIX + e["text"]
        out_entries.append({"index": i, "timing": e["timing"], "text": text})

    failed = set(result.failed)
    if failed:
        print(f"   ⚠️ {len(failed)} 条阶段A失败，交阶段B补译")
        # D7：缺行定向重试预算耗尽后的降级台账（缺行条目已按既有 failed
        # 链路置 [未翻译] 交阶段B补译，绝不整文件失败）
        from subtransjav.translate.llm_client import MISSING_RETRY_BUDGET
        logger.warning(
            "阶段A 缺行 %d 条（定向重试预算 %d 轮耗尽），"
            "置 [未翻译] 交阶段B补译", len(failed), MISSING_RETRY_BUDGET)
        if collector is not None:
            collector.add(stage="A", file=file_name,
                          reason=f"阶段A 缺行 {len(failed)} 条"
                                 f"（定向重试预算 {MISSING_RETRY_BUDGET} "
                                 f"轮耗尽）",
                          action="置 [未翻译] 交阶段B补译",
                          affected_count=len(failed),
                          severity=SEVERITY_WARNING)
    return StageAResult(entries=out_entries, deleted=set(result.deleted),
                        failed=failed, exact_hits=exact)


def _run_stage_b(cfg: RefineConfig, a_result: StageAResult, orig_entries: list,
                 tmp_dir: str, glossary: list, collector=None,
                 file_name: str = None, emitter=None,
                 sidecar: dict | None = None,
                 synopsis: str | None = None) -> list:
    """阶段B：对照日文原文审校+抛光。返回最终条目列表。"""
    # 协议标记：供 webview_gui 检测阶段切换，更新进度显示（勿删）
    print(f"[STAGE] {V2_STAGE_NAMES['B']}", flush=True)
    print("\n🔹 [阶段B 审校+抛光] 对照日文原文审核/补译/润色")

    # [未翻译] 形态判定统一走共享函数：提示词模板教给 LLM 的是无空格
    # "[未翻译]"，与生成侧常量 UNTRANSLATED_PREFIX（带尾空格）并存，
    # 输入清洗与输出组装必须两形态都识别
    from .post_validate import is_untranslated_text

    # 组装 B 输入：`日文原文 ||| 中文译文`
    # 日文参照按时间轴双指针对齐（同起点多条不会错配）
    aligned_orig = _align_orig_by_timing(a_result.entries, orig_entries)
    b_entries = []
    for ae, orig in zip(a_result.entries, aligned_orig, strict=False):
        ja = (orig["text"] or "").strip() if orig else ""
        zh = ae["text"]
        if is_untranslated_text(zh):
            zh = ""                      # 交阶段B补译
        b_entries.append({
            "index": ae["index"], "timing": ae["timing"],
            "text": f"{ja} ||| {zh}",
        })

    # 语法提示注入：日文原文参与审校，同样注入句法提示（如「で」中顿、
    # 僕たち定语结构）。未安装 sudachipy 时静默降级，不报错。
    # 提示拼接为『【语法提示】\n- ...\n原文：日文 ||| 译文』，与阶段A
    # 注入格式风格一致；残留可被 cleaner_rules.clean_grammar_hint_residue
    # 清理（该函数的『【语法提示】[\s\S]*?原文：』模式已覆盖此格式）。
    ja_entries = [
        {"index": ae["index"], "timing": ae["timing"],
         "text": (orig["text"] or "").strip() if orig else ""}
        for ae, orig in zip(a_result.entries, aligned_orig, strict=False)]
    hints = _collect_grammar_hints(ja_entries, ja_entries, verbose=False,
                                   tag="B", profile=cfg.v2_profile)
    if hints:
        print(f"   📝 语法提示已注入（审校）: {len(hints)}/{len(b_entries)} 条")
        for e in b_entries:
            h = hints.get(e["index"])
            if h:
                e["text"] = f"{h}\n原文：{e['text']}"

    src_text = "\n".join(e["text"] for e in b_entries)
    gl_block = _v2_glossary_block(cfg, "B", src_text, glossary)
    sidecar_block = _v2_sidecar_block(src_text, sidecar)
    system_text, user_prompt = _load_v2_instruction(
        cfg, "B", gl_block, tmp_dir, sidecar_block,
        _synopsis_prompt_block(synopsis))

    stage_cfg = cfg.stages[V2_STAGE_SLOT["B"]]
    client = _make_client(cfg, "B")
    result = _run_with_fallback(
        cfg, "B", client, b_entries, system_text=system_text,
        user_prompt=user_prompt, max_batch_size=cfg.batch_for(stage_cfg),
        # D1：禁用留空删除（空译文按缺行处理），缺译行走下方回退链。
        allow_empty_deletions=False, emitter=emitter)

    # 组装最终产物：B 结果优先 → 回退 A 译文 → 回退原文+[未翻译] 标记
    # （D1：删除权收归闸门0，本阶段任何失败路径都不再物理删条）。
    # B 输入带【语法提示】段，LLM 若回显残留则在此兜底清理
    # （clean_grammar_hint_residue 的模式覆盖阶段B注入格式）。
    from .cleaner_rules import clean_grammar_hint_residue
    kept_a = []                  # B 缺译文 → 回退 A 译文的条目 [(index, text)]
    kept_original = []           # A/B 双失败 → 保留原文的条目 [(index, 原文)]
    final = []
    for ae, orig in zip(a_result.entries, aligned_orig, strict=False):
        i = ae["index"]
        keep_flag = False
        if i in result.translations:
            text = clean_grammar_hint_residue(result.translations[i])
        elif not is_untranslated_text(ae["text"]):
            text = ae["text"]            # B 缺译文回退 A 译文（不丢行）
            kept_a.append((i, text))
        else:
            # A/B 双失败 → 回退原文并加 [未翻译] 前缀（宁多勿缺）。
            # 打内部标记：后续语言白名单过滤（zh）会把纯日文行判无效，
            # 带标记条目须跳过该过滤（见 _run_single_v2）。build_srt 只读
            # index/timing/text，该键不会影响产物。
            text = (orig["text"] or "").strip() if orig else ""
            if not text:
                text = ae["text"]        # 无原文可退：保留阶段A 原文+标记
            elif not is_untranslated_text(text):
                text = UNTRANSLATED_PREFIX + text   # 统一标记（防二次加标）
            keep_flag = True
            kept_original.append((i, text))
        # A2 残译清洗：无论残译从哪条路径进来（B 回显已带标记的
        # "[未翻译] Chicks。" 等），终稿落盘前统一规范化（只改 text，
        # 条目数不变；详见 _normalize_untranslated_marker）。
        text = _normalize_untranslated_marker(
            text, (orig["text"] or "") if orig else "")
        if not text:
            continue                     # 空正文不进终稿（D1 下仅防御性保留）
        entry = {"index": i, "timing": ae["timing"], "text": text}
        if keep_flag:
            entry["_keep_original"] = True
        final.append(entry)

    # ---- 回退链显式化：降级统计进风险收集器（事件流同步镜像）----
    if collector is not None:
        if kept_a:
            collector.add(stage="B", file=file_name,
                          reason="阶段B无译文（审校/补译失败）",
                          action="回退A译文",
                          affected_count=len(kept_a),
                          samples=[t for _, t in kept_a],
                          severity=SEVERITY_WARNING)
        if kept_original:
            collector.add(stage="B", file=file_name,
                          reason="阶段B仍无译文（回退链末环）",
                          action="保留日文原文",
                          affected_count=len(kept_original),
                          samples=[t for _, t in kept_original],
                          severity=SEVERITY_WARNING)
        # 整段未翻译判定：保留原文占非空条目 ≥80%，或阶段A整体失败
        a_all_failed = bool(a_result.entries) and all(
            e["text"].startswith(UNTRANSLATED_PREFIX) for e in a_result.entries)
        ratio = (len(kept_original) / len(final)) if final else 0.0
        if kept_original and (ratio >= 0.8 or a_all_failed):
            collector.mark_untranslated_majority(
                file=file_name, total=len(final), kept=len(kept_original))
    return final


def _apply_fallback_rules(cfg: RefineConfig, entries: list,
                          orig_entries: list,
                          collector=None, file_name: str = None) -> tuple:
    """兜底规则层（profile 驱动）：
    local(strict) → cleaner_rules 清洗 + post_validate 误译拦截；
    cloud(lenient) → 跳过（仅保留语言白名单等零维护校验）。
    返回 (entries, validator_warnings, clean_merged, flagged_indexes, clean_stats)：
      validator_warnings — post_validate 告警列表（传给质量报告）；
      clean_merged — cleaner 碎片合并减少的条数（int，lenient 档为 None；
        删除数不再混入，见 clean_stats["deleted"]）；
      flagged_indexes — post_validate 标记的行 index 集合（TM 学习准入用）；
      clean_stats — cleaner 结构化统计 dict（merged/deleted/deleted_by_rule/
        kept_by_source_evidence；lenient 档或清洗失败时为 None）。"""
    if cfg.v2_profile != "local":
        return entries, [], None, set(), None

    # post_validate：で误译修正 + 主语误判告警（YAML 单一数据源驱动）
    validator_warnings = []
    flagged_indexes: set = set()
    try:
        from .post_validate import check_and_fix_translation_errors
        fixes, warnings, flagged_indexes = check_and_fix_translation_errors(
            orig_entries, entries)
        if fixes:
            print(f"   🔍 兜底拦截: 修正 {fixes} 条误译")
        for w in warnings:
            logger.warning("post_validate: %s", w)
            print(f"   {w}")
        validator_warnings = warnings
    except Exception as e:
        print(f"   ⚠️ 兜底拦截失败（忽略）: {e}")
        if collector is not None:
            collector.add(stage="A", file=file_name,
                          reason=f"误译拦截失败: {e}",
                          action="跳过post_validate误译拦截",
                          affected_count=len(entries),
                          severity=SEVERITY_WARNING)

    # cleaner_rules：规则清洗（删除残余噪音/碎片）
    clean_merged = 0
    clean_stats = None
    try:
        from .cleaner_rules import clean_srt
        # 源侧证据门槛（v1.2.1 P0）：清洗前条目按时间轴对齐源文日文后
        # 传入 clean_srt——删除类规则（L3-L12）须源文佐证（任一源文行含
        # 汉字，或证据缺失）才免删；合并条目在 clean_srt 内部继承成员
        # 源文集合。对不齐的条目无证据 → fail-safe 保留。
        pre_aligned = _align_orig_by_timing(entries, orig_entries)
        source_map = {}
        for e, o in zip(entries, pre_aligned, strict=False):
            if o is not None and (o.get("text") or "").strip():
                source_map[e["timing"]] = o["text"]
        cleaned, clean_stats = clean_srt(build_srt(entries),
                                         config_dir=cfg.cleaner_config_dir or None,
                                         source_map=source_map)
        cleaned_entries = parse_srt(cleaned)
        clean_merged = int(clean_stats.get("merged", 0))
        n_deleted = int(clean_stats.get("deleted", 0))
        if clean_merged > 0 or n_deleted > 0:
            print(f"   🧹 兜底清洗: 合并碎片 {clean_merged} 条，"
                  f"规则删除 {n_deleted} 条"
                  f"（源侧证据免删 {int(clean_stats.get('kept_by_source_evidence', 0))} 条）")

        # ⚠️ 身份恢复（防错位的关键步骤）：cleaner 会按输出顺序重新编号，
        # 若不恢复，后续 阶段B 的日文参照与 TM 学习都会整体错位——这正是
        # legacy 管线毒化 TM 的同款路径。按时间轴双指针恢复原始编号
        # （同起点多条按顺序消费，不会互相覆盖）。
        aligned = _align_orig_by_timing(cleaned_entries, orig_entries)
        restored = 0
        for e, o in zip(cleaned_entries, aligned, strict=False):
            if o is not None and e["index"] != o["index"]:
                e["index"] = o["index"]
                restored += 1
        if restored:
            print(f"   🔧 已按时间轴恢复 {restored} 条原始编号（防错位）")
        return cleaned_entries, validator_warnings, clean_merged, flagged_indexes, clean_stats
    except Exception as e:
        print(f"   ⚠️ 兜底清洗失败（忽略）: {e}")
        if collector is not None:
            collector.add(stage="A", file=file_name,
                          reason=f"规则清洗失败: {e}",
                          action="跳过规则清洗",
                          affected_count=len(entries),
                          severity=SEVERITY_WARNING)
        return entries, validator_warnings, 0, flagged_indexes, None


def _filter_language(cfg: RefineConfig, entries: list, stage_idx: int) -> list:
    """语言白名单校验（零维护通用校验，两档 profile 均启用）。

    D1：非中文/乱码条目不再从终稿物理删除——改加 [未翻译] 前缀保留；
    已带 [未翻译] 标记的条目跳过校验（_keep_original 条目由调用方预先
    分流，不经此处）。明细仍归档 Errors/dropped_entries.log。
    """
    normal, marked = [], []
    for e in entries:
        if (e.get("text") or "").startswith(UNTRANSLATED_PREFIX):
            marked.append(e)         # 已标记条目跳过校验（防二次加标）
        else:
            normal.append(e)
    srt = build_srt(normal)
    kept_entries, dropped = filter_stage_output_srt(srt, stage_idx, "zh")
    # build_srt 会重排序号：按时间轴（条目的真实身份标识）映射回原条目——
    # 有效条目恢复原 index；无效条目加 [未翻译] 前缀后并回产物（不丢行）。
    by_timing = {e["timing"]: e for e in normal}
    kept_timings = set()
    for e in kept_entries:
        if e["timing"] in by_timing:
            e["index"] = by_timing[e["timing"]]["index"]
        else:
            # LLM 把「序号+时间码」写进条目正文时，_SRT_BLOCK 的前瞻会把
            # 它拆成独立伪条目（Errors/dropped_entries.log 实证
            # '1002\n01:37:10,439 --> 01:37:11,899' 形态），其 timing 不在
            # 由 normal 构建的 by_timing 中。此时回退保留 parse_srt 给出的
            # 临时编号（kept 条目必有 index 字段，缺省 0），不再抛 KeyError
            # ——否则异常经 _run_single_v2 穿透 _process_file 兜底 except，
            # 升级为整文件失败。产物排序按时间轴（_timing_span），不受
            # 临时编号影响。
            e["index"] = e.get("index", 0)
            logger.warning(
                "语言过滤：timing %r 不在原条目中（疑似 LLM 正文内嵌"
                "「序号+时间码」伪条目），保留临时编号 %s",
                e["timing"], e["index"])
        kept_timings.add(e["timing"])
    for e in normal:
        if e["timing"] not in kept_timings:
            kept_entries.append({"index": e["index"], "timing": e["timing"],
                                 "text": UNTRANSLATED_PREFIX + (e["text"] or "")})
    kept_entries.extend(marked)
    kept_entries.sort(key=lambda e: _timing_span(e["timing"])[0])
    if dropped:
        print(f"   🧹 乱码/幻觉残留：{dropped} 条加 [未翻译] 标记保留"
              f"（不删除，明细 -> Errors/dropped_entries.log）")
    return kept_entries


def _atomic_write_text(path: str, text: str):
    """原子写文本文件：同目录临时文件 + os.replace，防止中断留下半截产物。

    参考 filter_stage_output_srt 的 mkstemp+finally 模式；
    成功 replace 后 finally 中的 unlink 因临时文件已不存在而静默跳过。
    """
    import tempfile
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".srt.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# H3 闸门0 摘要（gate0_summary 事件 payload）/ H4a 上游 ASR 信号接线
# ---------------------------------------------------------------------------

def _asr_meta_min_coverage_pct(cfg) -> float:
    """上游语音覆盖率告警阈值；非法值静默回退默认 30（该链路一贯容错）。"""
    try:
        return float(getattr(cfg, "v2_asr_meta_min_coverage_pct", 30))
    except (TypeError, ValueError):
        return 30.0


def _build_gate0_report(source_name: str, stats: dict, upstream: dict,
                        samples: list, quarantine: dict = None) -> dict:
    """构造 gate0_summary NDJSON 事件 payload（schema 契约由
    tests/test_pipeline_v2.py 钉住；1.2.1 起 {stem}_幻觉处置报告.json
    不再落盘，本函数仅服务事件通道）。

    gate0_ran 恒为 True：闸门0 在管线头部无条件执行（受信 resume 下
    幂等——指纹校验保证规则/档位/信号语义与原次一致），摘要如实
    记录真实计数，不做归零处理（D2026-0914-01 追记裁决）。

    quarantine 参数：隔离区结论（{"candidates", "quarantined", "file"}）。
    省略时（直调/单测）为"回捞未执行"基线——candidates 按 stats 如实
    计数，quarantined/file 记 null（final 回捞尚未判定）。payload 另含
    noise_left_empty（历史字段：D1 后阶段A 不再留空删条，管线恒
    回填 0，仅为事件 schema 兼容保留）。
    """
    tripped = bool(stats.get("valve_tripped"))
    if quarantine is None:
        quarantine = {"candidates": len(stats.get("quarantine_candidates")
                                        or []),
                      "quarantined": None, "file": None}
    return {
        "report_version": 1,
        "source": source_name,
        "gate0_ran": True,
        "mode": stats.get("mode"),
        "total": stats.get("total", 0),
        "deleted": stats.get("deleted", 0),
        "detected_total": stats.get("detected_total", 0),
        "valve": {
            "tripped": tripped,
            "pct": stats.get("valve_pct"),
            "message": ("拦截率超阈值，本文件降级为只计数模式" if tripped else None),
        },
        "categories": stats.get("categories") or {},
        "samples": list(samples or []),
        "upstream": upstream,
        "quarantine": quarantine,
        "noise_left_empty": stats.get("noise_left_empty", 0),
    }


def _gate0_summary_line(stats: dict) -> str:
    """R8：每文件闸门0 计数行（经 collector.summary_lines 聚合输出）。"""
    valve_word = "触发降级只计数" if stats.get("valve_tripped") else "保险阀未触发"
    return (f"🚪 闸门0：删除 {stats.get('deleted', 0)}"
            f"/原始 {stats.get('total', 0)}，"
            f"检出计数 {stats.get('detected_total', 0)}"
            f"（{stats.get('mode', 'default')} 档，{valve_word}）")


def filter_stage_output_srt(srt_content: str, stage_index: int,
                            target: str) -> tuple:
    """对条目列表（而非文件路径）执行语言白名单校验。"""
    import tempfile

    from .language_validator import filter_stage_output
    fd, tmp = tempfile.mkstemp(suffix=".srt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(srt_content)
        kept_count, dropped = filter_stage_output(tmp, stage_index, target)
        kept_entries = parse_srt(Path(tmp).read_text(encoding="utf-8"))
        return kept_entries, dropped
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

# P1-6：词库学习后台线程收尾 join 总预算（秒）；超时记 warning 后放行退出
_LEARN_JOIN_TIMEOUT = 60.0


def _file_parallel_enabled(cfg: RefineConfig) -> bool:
    """云端多文件并行开关（P1-6，opt-in）。

    cfg.v2_file_parallel=True 且 A/B 两阶段均为云端服务商时才启用；
    本地服务商（LM Studio/Ollama 单并发）一律串行。
    """
    if not getattr(cfg, "v2_file_parallel", False):
        return False
    for tag in V2_STAGE_TAGS:
        stage_cfg = cfg.stages[V2_STAGE_SLOT[tag]]
        if stage_cfg.provider in ("lmstudio", "ollama"):
            return False
    return True


def _finish_learn_threads(threads: list, collector) -> None:
    """收尾词库学习后台线程：总预算内逐个 join；超时未完成的记 warning
    后放行退出（daemon 线程随进程终止，不影响主流程产物）。"""
    if not threads:
        return
    deadline = time.monotonic() + _LEARN_JOIN_TIMEOUT
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(t.is_alive() for t in threads):
        print(f"⚠️ 词库学习未完成（等待超时 {int(_LEARN_JOIN_TIMEOUT)}s），"
              f"放行退出")
        collector.add(stage="run", file=None,
                      reason="词库学习后台线程超时未完成",
                      action="词库学习未完成（等待超时）",
                      severity=SEVERITY_WARNING)


def run_v2(cfg: RefineConfig, *, summary_sink: dict | None = None,
           event_stream=None):
    """执行 v2 两阶段流水线。返回最后一个成功输出。

    summary_sink 非 None 时（CLI 传入），结束时填入任务汇总：
      {"files_ok","files_degraded","files_failed",
       "untranslated_majority","risk_count","summary_lines"}
    event_stream 非 None 时结构化事件写入该流（ndjson 模式）；text 模式
    下 emitter 为无操作，所有 print 字符串原样保留（GUI 旧解析依赖）。
    """
    errors = cfg.validate()
    if errors:
        raise RefineError("配置错误：\n  - " + "\n  - ".join(errors))
    if isinstance(cfg.inputs, str):
        cfg.inputs = [cfg.inputs]

    collector = RiskCollector()
    emitter = EventEmitter(
        stream=event_stream,
        enabled=(cfg.event_format == "ndjson"),
        heartbeat_interval=cfg.heartbeat_interval)
    collector.attach_emitter(emitter)

    files_ok = files_degraded = files_failed = 0
    results, failures = [], []
    total = len(cfg.inputs)
    learn_threads: list = []    # P1-6：逐文件词库学习后台线程（run 级注册）

    # P1-5 生效配置摘要：text 模式直接打印；ndjson 模式随 task_started 上报
    config_summary = cfg.effective_summary()
    if cfg.event_format != "ndjson":
        print(config_summary)

    def _finish_task(status: str) -> None:
        """发 task_finished 事件并回填 summary_sink。"""
        emitter.emit("task_finished", payload={
            "status": status,
            "files_ok": files_ok,
            "files_degraded": files_degraded,
            "files_failed": files_failed,
            "untranslated_majority": collector.untranslated_majority,
            "risk_count": len(collector.events),
        })
        if summary_sink is not None:
            summary_sink.clear()
            summary_sink.update({
                "files_ok": files_ok,
                "files_degraded": files_degraded,
                "files_failed": files_failed,
                "untranslated_majority": collector.untranslated_majority,
                "risk_count": len(collector.events),
                "summary_lines": collector.summary_lines(),
            })

    emitter.emit("task_started",
                 payload={"files": total, "profile": cfg.v2_profile,
                          "config_summary": config_summary})
    emitter.start_heartbeat()

    def _process_file(path):
        """单文件处理；异常隔离进记录，不拖垮其他文件。"""
        before = len(collector.events)
        was_untrans = collector.untranslated_majority
        try:
            out = _run_single_v2(cfg, path, collector=collector,
                                 emitter=emitter, learn_threads=learn_threads)
            return (path, out, None, before, was_untrans)
        except Exception as e:      # noqa: BLE001 单文件隔离
            return (path, None, e, before, was_untrans)

    def _absorb(record, idx: int) -> None:
        """聚合单文件记录到任务计数（主线程串行执行，无竞态）。"""
        nonlocal files_ok, files_degraded, files_failed
        path, out, err, before, was_untrans = record
        fname = Path(path).name
        if err is None:
            results.append(out)
            files_ok += 1
            # 本文件是否发生内容降级（口径与 RiskCollector.content_degraded
            # 一致：非 info 且有动作且有影响条数，或整段未翻译置位）
            new_risk = any(e.severity != SEVERITY_INFO and e.action
                           and e.affected_count > 0
                           for e in collector.events[before:])
            if new_risk or (collector.untranslated_majority
                            and not was_untrans):
                files_degraded += 1
            print(f"[refine-v2] 文件成功 ({idx}/{total})：{fname}")
        else:
            failures.append((path, err))
            files_failed += 1
            collector.add(stage="run", file=fname, reason=str(err),
                          action="该文件失败跳过",
                          severity=SEVERITY_CRITICAL)
            emitter.emit("error", file=fname, payload={"reason": str(err)})
            print(f"\n❌ [refine-v2] 文件失败 ({idx}/{total})："
                  f"{fname}\n   原因: {err}")

    try:
        if _file_parallel_enabled(cfg) and total > 1:
            # P1-6 云端多文件并行（opt-in）：max_workers=min(2, 文件数)
            max_workers = min(2, total)
            print(f"⚡ [refine-v2] 云端多文件并行：{max_workers} workers / "
                  f"{total} 个文件")
            with ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="refine-v2") as pool:
                records = list(pool.map(_process_file, list(cfg.inputs)))
            for idx, record in enumerate(records, 1):
                _absorb(record, idx)
        else:
            for idx, path in enumerate(list(cfg.inputs), 1):
                _absorb(_process_file(path), idx)

        # P1-6：收尾词库学习后台线程（join 超时记 warning 放行）；
        # task_finished 事件在其后的 _finish_task 中发出
        _finish_learn_threads(learn_threads, collector)

        if failures:
            print(f"\n⚠️ [refine-v2] 批量完成：成功 {len(results)} / "
                  f"失败 {len(failures)} / 共 {total}")
        if not results:
            _finish_task("failed")
            raise RefineError(f"全部 {len(failures)} 个文件均处理失败")
        _finish_task("success" if (files_degraded == 0 and files_failed == 0)
                     else "partial")
        return results[-1]
    finally:
        emitter.close()


def _prepare_manifest(cfg: RefineConfig, in_path: str, out_dir: str,
                      stem: str, tm) -> tuple:
    """创建/加载任务清单并做指纹校验。返回 (manifest, trusted)。

    trusted=True 表示清单指纹校验通过（或 force_resume 强制采信），
    配合 cfg.resume 才允许复用已有阶段产物；不复用时新建清单覆盖。
    """
    m_path = manifest_path(out_dir, stem)
    input_sha1 = compute_file_sha1(in_path)
    config_hash = compute_config_hash(cfg)
    gl_fp = _glossary_fingerprint(cfg)
    tm_fp = _tm_fingerprint(cfg, tm)
    manifest = load_manifest(m_path)
    if manifest is not None:
        reasons = validate_manifest(manifest, input_sha1=input_sha1,
                                    config_hash=config_hash,
                                    glossary_sha1=gl_fp, tm_sha1=tm_fp)
        if not reasons:
            return manifest, True
        if cfg.force_resume:
            # 外层前缀只描述"校验未通过"，具体变化明细以 reasons 为准，
            # 避免出现"配置已变化：配置已变化"式重复（O4）。
            print(f"⚠️ 强制复用（指纹校验不匹配：{'、'.join(reasons)}），"
                  f"继续复用已有产物")
            # 指纹刷新为当前值，保持清单自洽
            manifest.input_sha1 = input_sha1
            manifest.input_size = Path(in_path).stat().st_size
            manifest.config_hash = config_hash
            manifest.glossary_sha1 = gl_fp
            manifest.tm_sha1 = tm_fp
            save_manifest(m_path, manifest)
            return manifest, True
        print(f"⚠️ 不复用（{'、'.join(reasons)}），将重跑")
        # D3：拒绝复用不得立即重写清单文件——否则上一轮中断现场（如
        # stages.A=done）会被全新 pending 清单覆盖，--force-resume 随即
        # 失去复用前提。此处仅在内存中刷新指纹、保留磁盘原文件；待阶段
        # 实际重跑（mark_running -> save_manifest）时才落盘推进。
        manifest.input_sha1 = input_sha1
        manifest.input_size = Path(in_path).stat().st_size
        manifest.config_hash = config_hash
        manifest.glossary_sha1 = gl_fp
        manifest.tm_sha1 = tm_fp
        return manifest, False
    now = datetime.now().isoformat(timespec="seconds")
    manifest = TaskManifest(
        manifest_version=MANIFEST_VERSION,
        input_path=in_path, input_sha1=input_sha1,
        input_size=Path(in_path).stat().st_size,
        config_hash=config_hash, glossary_sha1=gl_fp, tm_sha1=tm_fp,
        models=_v2_models_payload(cfg), out_dir=out_dir, stem=stem,
        started_at=now, updated_at=now, run_pid=os.getpid())
    save_manifest(m_path, manifest)
    return manifest, False


def _run_single_v2(cfg: RefineConfig, in_path: str, collector=None,
                   emitter=None, learn_threads: list | None = None) -> str:
    if collector is None:
        collector = RiskCollector()
    if emitter is None:
        emitter = EventEmitter(enabled=False)   # 直调模式：不发事件
    ensure_language_support()

    in_path, out_dir, stem = _resolve_stage_paths(cfg, in_path)
    fname = Path(in_path).name
    tmp_dir = refine_tmp_dir(in_path, stem)
    with _tmp_dirs_lock:
        if tmp_dir not in CREATED_TMP_DIRS:
            CREATED_TMP_DIRS.append(tmp_dir)

    out_a_path = str(Path(out_dir) / f"{stem}_refine_A.srt")
    out_final_path = str(Path(out_dir) / f"{stem}_final_cn.srt")
    force = bool(getattr(cfg, "force", False))
    if Path(out_final_path).is_file() and not force:
        print(f"\n🔹 [v2] 终稿已存在，跳过：{Path(out_final_path).name}")
        _remove_tmp_dir(tmp_dir)    # 无事可做：顺手清掉刚建的临时工作区
        emitter.emit("phase_started", phase="final", file=fname)
        emitter.emit("phase_finished", phase="final", file=fname, payload={
            "entries": 0, "degraded_count": 0, "reused": True})
        return out_final_path
    if force:
        _backup_existing_outputs(out_dir, stem, collector=collector,
                                 file_name=fname)

    glossary = load_glossary_merged(cfg)
    # v1.2.2 C1 per-片语境 sidecar（{stem}.context.md）：与词库同点加载，
    # A/B 两阶段注入；cfg.context_sidecar=False 或文件不存在时为 None
    sidecar = _load_context_sidecar(cfg, in_path)
    tm = _init_tm(cfg)

    orig_entries = parse_srt(Path(in_path).read_text(encoding="utf-8"))
    if not orig_entries:
        raise RefineError("输入 SRT 无有效条目")

    # H4a：上游 ASR 运行信号（whisperjav_run.json）——先于闸门0 加载；
    # run 状态可疑时收紧闸门0（tighten），覆盖率过低仅告警。
    # asr_meta 模块只出信号不 import risk：风险接线由本层完成，全程容错。
    asr_meta = load_asr_meta(cfg, in_path)
    upstream_block = {
        "present": bool(asr_meta.get("present")),
        "status": asr_meta.get("status"),
        "mileage_pct": asr_meta.get("mileage_pct"),
        "stale": bool(asr_meta.get("stale")),
        "file": asr_meta.get("file"),
        "warnings": list(asr_meta.get("warnings") or []),
    }
    tighten = False
    status_signal = asr_meta.get("status")
    if status_signal in SUSPECT_STATUSES:
        tighten = True
        print(f"⚠️ 上游 ASR 信号：run 状态={status_signal}，转写可信度低"
              f"（闸门0 按收紧规则执行）")
        collector.add(stage="gate0", file=fname,
                      reason=f"上游 ASR 信号：run 状态={status_signal}，转写可信度低",
                      action="闸门0 按收紧覆盖块执行（tighten）",
                      severity=SEVERITY_WARNING)
        upstream_block["warnings"].append(
            f"run 状态={status_signal}，转写可信度低，闸门0 已收紧")
    mileage = asr_meta.get("mileage_pct")
    min_cov = _asr_meta_min_coverage_pct(cfg)
    if mileage is not None and mileage < min_cov:
        collector.add(stage="gate0", file=fname,
                      reason=f"上游 ASR 语音覆盖率 {mileage:g}% "
                             f"低于阈值 {min_cov:g}%",
                      suggestion="建议检查上游转写质量或重跑上游 ASR",
                      severity=SEVERITY_WARNING)
        upstream_block["warnings"].append(
            f"语音覆盖率 {mileage:g}% 低于阈值 {min_cov:g}%")

    # 闸门0：送翻前源侧幻觉检测（预合并前对原始条目生效，两档 profile 均执行；
    # gate0_stats 由质量报告【处置】章节与 gate0_summary 事件消费）。
    # H5：候选 position 指向本次检测输入，先留快照供条目编号对齐。
    gate0_input = orig_entries
    orig_entries, gate0_stats = apply_source_filter(
        orig_entries, cfg, source_name=fname, tighten=tighten,
        samples_limit=_GATE0_REPORT_SAMPLE_CAP)
    # H5：候选原始下标 → 条目编号 对齐表（隔离区回捞用；候选仅保险阀
    # 降级路径非空。D1 后 noise_left_empty 恒 0，不再需要计数类条目编号
    # 集合，原 gate0_noise_indexes 一并移除）
    _cands = gate0_stats.get("quarantine_candidates") or []
    gate0_source_lookup = {
        c["position"]: gate0_input[c["position"]].get("index")
        for c in _cands if 0 <= c["position"] < len(gate0_input)}

    # 保险阀触发：第三种入风险清单的情形（warning 级）
    if gate0_stats.get("valve_tripped"):
        collector.add(stage="gate0", file=fname,
                      reason=f"闸门0 保险阀触发（拦截率超过 "
                             f"{gate0_stats.get('valve_pct')}%），降级为只计数",
                      action="保留幻觉行送翻（只计数不删除）",
                      affected_count=int(gate0_stats.get("detected_total") or 0),
                      severity=SEVERITY_WARNING)
    # R8：每文件闸门0 计数行，走 summary_lines 聚合输出
    collector.add_summary_line(_gate0_summary_line(gate0_stats))
    # NDJSON 只增：每文件闸门0 执行后发一次 gate0_summary
    # （payload=闸门0 摘要，去 samples，含 valve；gate0 执行如实记 True；
    #   quarantine 的 quarantined/file 在此时尚未回捞判定，如实记 null）
    gate0_report_now = _build_gate0_report(
        fname, gate0_stats, upstream_block,
        samples=gate0_stats.get("samples") or [],
        quarantine={"candidates": len(_cands), "quarantined": None,
                    "file": None})
    emitter.emit("gate0_summary", phase="gate0", file=fname,
                 payload={k: v for k, v in gate0_report_now.items()
                          if k != "samples"})

    # 断句预合并：修复 ASR 错误切割（代码层确定性步骤，两档 profile 均启用）
    premerge_merged = 0
    if cfg.premerge_enabled:
        n0 = len(orig_entries)
        orig_entries = _premerge_entries(orig_entries, cfg)
        premerge_merged = n0 - len(orig_entries)
        if premerge_merged:
            print(f"   🔗 断句预合并: {n0} → {len(orig_entries)} 条"
                  f"（修复 ASR 错误切割）")
            if premerge_merged / n0 > 0.25:
                collector.add(stage="premerge", file=fname,
                              reason=f"预合并占比过高: {premerge_merged}/{n0} 条被合并（>25%）",
                              action="如非预期，可调低 premerge_max_span_ms / "
                                     "premerge_max_chars 或关闭 premerge_enabled",
                              severity=SEVERITY_INFO)

    # ---- v1.2.2 Beta 剧情自摘要（时点：闸门0+预合并后、阶段A 前）----
    # 手写 sidecar【剧情摘要】非空时手写优先；关闭/失败静默跳过。
    # 摘要文本只注入 A/B 提示词，绝不写入输出目录/终稿/质量报告；
    # 摘要内容与缓存不参与 manifest 指纹（与 sidecar 内容同款已知边界：
    # 换 --s1-model 或改采样行为后须 --force 重跑方生效）。
    synopsis_text = _ensure_auto_synopsis(cfg, orig_entries, sidecar,
                                          collector=collector,
                                          file_name=fname)

    print("=" * 60)
    print(f"🚀 [refine-v2] 输入: {Path(in_path).name}")
    for tag in V2_STAGE_TAGS:
        stage_cfg = cfg.stages[V2_STAGE_SLOT[tag]]
        m = stage_cfg.model or _provider_default_model(cfg, stage_cfg.provider)
        print(f"   {V2_STAGE_NAMES[tag]}: [{stage_cfg.provider}] {m} | "
              f"批量{cfg.batch_for(stage_cfg)} | profile={cfg.v2_profile}")
    print("=" * 60)

    # ---- 中断恢复：任务清单（创建/加载 + 指纹校验）----
    manifest, manifest_trusted = _prepare_manifest(cfg, in_path, out_dir,
                                                   stem, tm)
    m_path = manifest_path(out_dir, stem)

    try:
        # ---- 阶段A（--resume 时可复用上次已完成产物）----
        reused_a = False
        a_result = None
        a_rec = manifest.stages.get("A")
        if (cfg.resume and manifest_trusted and a_rec is not None
                and a_rec.status in ("done", "degraded")
                and Path(out_a_path).is_file()
                and manifest.stages["final"].status != "done"):
            a_entries = parse_srt(Path(out_a_path).read_text(encoding="utf-8"))
            if a_entries:
                print(f"🔹 [v2] 阶段A产物复用（--resume）："
                      f"{Path(out_a_path).name}")
                a_result = StageAResult(
                    entries=a_entries, deleted=set(),
                    failed={e["index"] for e in a_entries
                            if e["text"].startswith(UNTRANSLATED_PREFIX)},
                    exact_hits={})
                reused_a = True       # 清单中阶段A记录沿用，不改写
        if not reused_a:
            emitter.emit("phase_started", phase="A", file=fname)
            manifest.mark_running("A")
            save_manifest(m_path, manifest)
            a_result = _run_stage_a(cfg, orig_entries, tm, tmp_dir, glossary,
                                    collector=collector, file_name=fname,
                                    emitter=emitter, sidecar=sidecar,
                                    synopsis=synopsis_text)
            _atomic_write_text(out_a_path, build_srt(a_result.entries))
            print(f"   ✅ 阶段A完成 -> {Path(out_a_path).name} "
                  f"({len(a_result.entries)} 条)")
            manifest.mark_done("A", out_a_path, len(a_result.entries),
                               degraded_count=len(a_result.failed))
            if a_result.failed:
                manifest.stages["A"].status = "degraded"
            save_manifest(m_path, manifest)
        # H5-7/D1：删除权收归闸门0 后，阶段A 不再留空删条（allow_empty_
        # deletions=False），本字段"被 LLM 留空删除的数量"语义失效——
        # 恒记 0，仅为 gate0_summary 事件 schema 兼容保留（历史消费者：
        # 事件流/tests 契约）。
        gate0_stats["noise_left_empty"] = 0
        emitter.emit("phase_finished", phase="A", file=fname, payload={
            "entries": len(a_result.entries),
            "degraded_count": len(a_result.failed),
            "reused": reused_a})

        # ---- 兜底规则层（strict/lenient）----
        a_entries, validator_warnings, clean_merged, flagged_indexes, clean_stats = \
            _apply_fallback_rules(cfg, a_result.entries, orig_entries,
                                  collector=collector, file_name=fname)

        # ---- 阶段B ----
        a_for_b = _wrap_as_result(a_entries, a_result)
        emitter.emit("phase_started", phase="B", file=fname)
        manifest.mark_running("B")
        save_manifest(m_path, manifest)
        final_entries = _run_stage_b(cfg, a_for_b, orig_entries, tmp_dir,
                                     glossary, collector=collector,
                                     file_name=fname, emitter=emitter,
                                     sidecar=sidecar,
                                     synopsis=synopsis_text)
        # ---- 语言白名单过滤 ----
        # 带 _keep_original 标记的回退日文原条目跳过 zh 白名单过滤
        # （否则 keep_untranslated 回退的日文原文会被误判删除），
        # 其余条目照常过滤后按时间轴合并回原顺序。
        keep_entries = [e for e in final_entries if e.get("_keep_original")]
        normal_entries = [e for e in final_entries if not e.get("_keep_original")]
        normal_entries = _filter_language(cfg, normal_entries, 3)
        final_entries = sorted(normal_entries + keep_entries,
                               key=lambda e: _timing_span(e["timing"])[0])
        # ---- H5 翻译后回捞：源文高置信幻觉 × 译文流畅中文 → 隔离区 ----
        # 只移动不删除（候选条目落盘 {stem}_隔离区.srt 供人工复核）；
        # 无候选（default 正常删除 / off 档）时零开销跳过
        quarantine_entries: list = []
        if gate0_stats.get("quarantine_candidates"):
            final_entries, quarantine_entries = quarantine_review(
                final_entries, gate0_stats["quarantine_candidates"],
                gate0_source_lookup)
        n_kept = len(keep_entries)
        emitter.emit("phase_finished", phase="B", file=fname, payload={
            "entries": len(final_entries), "degraded_count": n_kept,
            "reused": False})

        # ---- v1.2.2 D1 术语冲突观察（终稿生成后计算；一次遍历两用）----
        # 冲突清单供 CSV/报告【术语冲突观察】小节；逐术语统计供
        # 【术语一致性】章节；冲突 span 集合在 glossary_conflict_block=True
        # 时阻断 TM 学习（入库前检查）。无词库时整段跳过（零开销）。
        conflict_data = None
        if glossary:
            try:
                conflict_data = scan_glossary_conflicts(
                    final_entries, orig_entries, glossary)
            except Exception as e:
                print(f"   ⚠️ 术语冲突扫描失败（忽略）: {e}")
                collector.add(stage="final", file=fname,
                              reason=f"术语冲突扫描失败: {e}",
                              action="跳过术语冲突观察",
                              severity=SEVERITY_INFO)
        conflict_spans = {_timing_span(c.get("timing", ""))
                          for c in (conflict_data or {}).get("conflicts") or []}

        # ---- final 终稿落盘 ----
        emitter.emit("phase_started", phase="final", file=fname)
        _atomic_write_text(out_final_path, build_srt(final_entries))
        print(f"   ✅ 完成 -> {Path(out_final_path).name} "
              f"({len(final_entries)} 条)")
        manifest.mark_done("B", out_final_path, len(final_entries),
                           degraded_count=n_kept)
        manifest.mark_done("final", out_final_path, len(final_entries),
                           degraded_count=n_kept)
        save_manifest(m_path, manifest)
        emitter.emit("phase_finished", phase="final", file=fname, payload={
            "entries": len(final_entries), "degraded_count": n_kept,
            "reused": False})

        # ---- TM 自学习（存阶段1 日→中 翻译对；终稿优先）----
        # 分歧采集提前到学习之前（collect_disagreement 纯读无副作用，
        # 与质量报告块共用同一次结果，避免重复读 pass1/pass2）：
        # TM 学习的第三层门槛（必看分歧行不学习）需要必看行集合。
        # 仅 gate 开（tm_learn_gate）且启用 TM 学习时才计算必看集合——
        # --no-tm-learn-gate 时三层全关，A/B 验证口径不变。
        pass_mode = None
        disag = None
        if cfg.quality_report or tm:
            try:
                from .pass_disagreement import collect_disagreement, probe_disagreement_mode
                pass_mode = probe_disagreement_mode(in_path)
                disag = collect_disagreement(in_path)
            except Exception as e:
                print(f"   ⚠️ 双引擎分歧采集失败（忽略）: {e}")
                collector.add(stage="final", file=fname,
                              reason=f"双引擎分歧采集失败: {e}",
                              action="跳过分歧采集（质量报告与必看门槛降级）",
                              severity=SEVERITY_INFO)
        must_see_spans = None
        if tm and cfg.tm_learn_gate and disag:
            try:
                from .quality_report import _split_disagreement_rows
                must_see, _optional, _artifacts = \
                    _split_disagreement_rows(disag.get("rows") or [])
                must_see_spans = {
                    _timing_span(r.get("timing", "")) for r in must_see}
            except Exception as e:
                print(f"   ⚠️ 必看分歧行集合计算失败（忽略）: {e}")
                collector.add(stage="final", file=fname,
                              reason=f"必看分歧行集合计算失败: {e}",
                              action="跳过必看分歧行门槛",
                              severity=SEVERITY_INFO)
        learned_count = None
        if tm:
            learned_count = _learn_to_tm(
                tm, orig_entries, final_entries,
                flagged=flagged_indexes, gate=cfg.tm_learn_gate,
                must_see_spans=must_see_spans,
                collector=collector, file_name=fname,
                conflict_spans=conflict_spans,
                block_conflicts=bool(getattr(cfg, "glossary_conflict_block",
                                             False)))

        # ---- 自动词库学习（cfg.auto_glossary，含防幻觉核验）----
        # v1.2.2 D3 治理开关 glossary_learn_enabled 的跳过判定在
        # _auto_learn_glossary 内部（打印/日志/风险清单计数）。
        # P1-6：学习调用放入后台线程（daemon，注册到 run 级 learn_threads），
        # 不阻塞主流程；run_v2 收尾统一 join。
        if cfg.auto_glossary:
            _auto_learn_glossary(cfg, in_path, out_final_path,
                                 collector=collector, file_name=fname,
                                 threads=learn_threads)

        # ---- 自动质量报告（cfg.quality_report，落盘到输出目录）----
        if cfg.quality_report:
            try:
                from .quality_report import build_quality_report, write_divergence_review_csv, write_quality_report
                # pass_mode/disag 已在 TM 学习块前采集并共用
                # （两者都关时此处保持 None，与原先 block 内采集等价）
                if pass_mode is None and disag is None:
                    from .pass_disagreement import collect_disagreement, probe_disagreement_mode
                    pass_mode = probe_disagreement_mode(in_path)
                    disag = collect_disagreement(in_path)
                merge_stats = {"premerge_merged": premerge_merged}
                # H5 隔离区回捞：移出主稿进 *_隔离区.srt 的条数入账条数
                # 恒等式（无隔离时为 0，报告中该项不显示）
                merge_stats["quarantine_moved"] = len(quarantine_entries)
                if clean_merged is not None:
                    merge_stats["clean_merged"] = clean_merged
                if clean_stats:
                    # 键名契约（质量报告渲染在后续任务改造，先保证稳定）：
                    # clean_deleted / clean_deleted_by_rule / clean_kept_by_evidence
                    merge_stats["clean_deleted"] = int(clean_stats.get("deleted", 0))
                    merge_stats["clean_deleted_by_rule"] = dict(
                        clean_stats.get("deleted_by_rule") or {})
                    merge_stats["clean_kept_by_evidence"] = int(
                        clean_stats.get("kept_by_source_evidence", 0))
                    # v1.2.2 C2 噪声闸门：纯假名实义源文免删的条数与时间轴
                    # （报告"纯假名实义保留"行及 [未翻译] 标记统计消费）
                    merge_stats["clean_kept_by_noise_gate"] = int(
                        clean_stats.get("kept_by_noise_gate", 0))
                    merge_stats["clean_kept_by_noise_gate_timings"] = list(
                        clean_stats.get("kept_by_noise_gate_timings") or [])
                # 闸门0 删除台账（处置章节消费）：samples 原始结构为
                # {number, category, text}（无 timing），按条目编号回查
                # 闸门0 输入的时间轴后转成报告渲染契约
                _timing_by_index = {e.get("index"): (e.get("timing") or "")
                                    for e in gate0_input}
                gate0_deletions = {
                    "total": int(gate0_stats.get("deleted", 0) or 0),
                    "by_category": {
                        label: int(v.get("deleted", 0) or 0)
                        for label, v in (gate0_stats.get("categories")
                                         or {}).items()},
                    "samples": [
                        {"index": s.get("number"),
                         "timing": _timing_by_index.get(s.get("number"), ""),
                         "reason": s.get("category", ""),
                         "text": s.get("text", "")}
                        for s in (gate0_stats.get("samples") or [])],
                }
                # ---- D5 乱码强译复核候选（终稿生成后计算）----
                # (源文命中闸门0计数类位置 OR strong_garble_signal 命中)
                # AND is_fluent_zh(终稿译文) AND 源文不含汉字
                _count_idx = {
                    gate0_input[p].get("index")
                    for p in (gate0_stats.get("count_positions") or [])
                    if isinstance(p, int) and 0 <= p < len(gate0_input)}
                garble_review = []
                for e, o in zip(final_entries,
                                _align_orig_by_timing(final_entries,
                                                      orig_entries),
                                strict=False):
                    src = (o.get("text") or "").strip() if o else ""
                    zh = (e.get("text") or "").strip()
                    if not src or _KANJI_SRC_RE.search(src):
                        continue       # 无对应源文 / 实义行（含汉字）不入
                    if not is_fluent_zh(zh):
                        continue       # 非流畅译文（含 [未翻译]）不入
                    sig = strong_garble_signal(src)
                    if sig is None and o.get("index") in _count_idx:
                        sig = "闸门0计数类检出"
                    if sig is None:
                        continue
                    garble_review.append({
                        "index": e.get("index"),
                        "timing": e.get("timing") or "",
                        "src_preview": src[:60],
                        "zh_preview": zh[:60],
                        "signal": sig,
                    })
                # ---- v1.2.2 C1 误听疑似改写留痕（终稿生成后计算）----
                # 源文命中任一误听疑似词的终稿条目全部列入（命中即列，
                # 不做是否改写的语义判定——保守审计口径，防审计缺口）。
                mishear_review = []
                if sidecar and (sidecar.get("mishear")):
                    for e, o in zip(final_entries,
                                    _align_orig_by_timing(final_entries,
                                                          orig_entries),
                                    strict=False):
                        src = (o.get("text") or "").strip() if o else ""
                        if not src:
                            continue
                        for a, b in sidecar["mishear"]:
                            if a and a in src:
                                mishear_review.append({
                                    "index": e.get("index"),
                                    "timing": e.get("timing") or "",
                                    "suspect": a,
                                    "correct": b,
                                    "zh_preview":
                                        (e.get("text") or "").strip()[:60],
                                })
                # 无误听怀疑表（sidecar 未启用/文件缺表）时传 None=整节省略
                sidecar_review = mishear_review \
                    if (sidecar and sidecar.get("mishear")) else None
                # ---- v1.2.2 D1 术语冲突观察：CSV 落盘 + 跨运行累计 ----
                # glossary_conflicts/term_consistency：有词库时传列表
                # （空列表 → 报告显示"无样本"）；无词库/扫描失败传 None
                # （章节整体省略）。观察闸 JSON 追加本次运行记录并取
                # 三态建议行（只评估不自动切换）。
                conflicts = (conflict_data or {}).get("conflicts")
                term_stats = (conflict_data or {}).get("term_stats")
                conflict_csv_path = None
                watch_advice = None
                if conflict_data is not None:
                    try:
                        if conflicts:
                            conflict_csv_path = write_conflict_csv(
                                str(Path(out_dir)
                                    / f"{stem}_术语冲突观察.csv"),
                                conflicts)
                            print(f"📊 术语冲突观察 CSV 已生成: "
                                  f"{Path(conflict_csv_path).name}")
                        per_term = {
                            t.get("term", ""): {
                                "candidates": t.get("hits", 0),
                                "conflicts": t.get("with_neither", 0)}
                            for t in term_stats or []}
                        watch_path = default_watch_path()
                        append_watch_record(watch_path, fname, per_term)
                        watch_advice = evaluate_watch(
                            load_watch_records(watch_path))
                    except Exception as e:
                        print(f"   ⚠️ 术语冲突观察落盘失败（忽略）: {e}")
                        collector.add(stage="final", file=fname,
                                      reason=f"术语冲突观察落盘失败: {e}",
                                      action="跳过观察闸 CSV/JSON 累计",
                                      severity=SEVERITY_INFO)
                # TM 摘要行取值：H=阶段A 精确命中数（阶段A 复用时取不到
                # → None）；L=本次学习入库数（未启用 TM → None）
                tm_exact_hits = (len(a_result.exact_hits)
                                 if (tm is not None and not reused_a) else None)
                report = build_quality_report(
                    orig_entries, final_entries, Path(in_path).name,
                    expected_entries=orig_entries,
                    merge_stats=merge_stats,
                    validator_warnings=validator_warnings,
                    pass_disagreement=disag,
                    pass_mode=pass_mode,
                    gate0_deletions=gate0_deletions,
                    garble_review=garble_review,
                    sidecar_review=sidecar_review,
                    orig_total=len(gate0_input),
                    profile=str(getattr(cfg, "v2_profile", "") or ""),
                    glossary_conflicts=conflicts,
                    term_consistency=term_stats,
                    conflict_watch_advice=watch_advice,
                    tm_exact_hits=tm_exact_hits,
                    tm_learned_count=learned_count)
                rp = write_quality_report(out_dir, stem, report)
                print(f"\n📋 质量报告已生成: {Path(rp).name}")
                print("\n".join(report.splitlines()[-3:]))
                # ---- 分歧复核 CSV（与质量报告同目录）----
                csv_path = str(Path(out_dir) / f"{stem}_分歧复核.csv")
                write_divergence_review_csv(
                    csv_path, (disag or {}).get("rows") or [],
                    Path(in_path).name, final_entries=final_entries)
                print(f"📊 分歧复核 CSV 已生成: {Path(csv_path).name}")
            except Exception as e:
                print(f"   ⚠️ 质量报告生成失败（忽略）: {e}")
                collector.add(stage="final", file=fname,
                              reason=f"质量报告生成失败: {e}",
                              action="跳过质量报告",
                              severity=SEVERITY_INFO)

        # ---- 风险清单报告（有风险才写 {stem}_风险清单.md/.json）----
        removed_stale = _remove_stale_risk_reports(out_dir, stem)
        if removed_stale:
            print(f"🧹 已清理上轮残留风险清单: {', '.join(removed_stale)}")
        reports = collector.write_reports(out_dir, stem)
        if reports:
            print(f"\n📋 风险清单已生成: {Path(reports['md']).name}")

        # ---- 任务成功完成：清理断点恢复类中间文件 ----
        # （清单已完成使命；{stem}_manifest.json、{stem}_refine_A.srt 与
        #   旧版残留的 {stem}_幻觉处置报告.json（1.2.1 起不再生成）留着
        #   只会误导下一次 --resume；tmp_dir 中的指令副本同样不再需要）
        delete_resume_artifacts(out_dir, stem)
        _remove_tmp_dir(tmp_dir)
        print("🧹 恢复类中间文件已清理")

        # ---- H5 隔离区落盘 ----
        # （置于恢复类清理之后一环，避免被本次运行的清理误删；
        #   隔离区仅非空时写，空则不落文件——上一轮残留已随清理删除。
        #   1.2.1 起 {stem}_幻觉处置报告.json 不再生成：闸门0 台账由质量
        #   报告【处置】章节承接，机器可读通道为 gate0_summary 事件）
        quarantine_file = None
        if quarantine_entries:
            quarantine_file = f"{stem}_隔离区.srt"
            _atomic_write_text(str(Path(out_dir) / quarantine_file),
                               build_srt(quarantine_entries))
            line = (f"📪 隔离区：{len(quarantine_entries)} 条存疑译文已移出主稿，"
                    f"见 {quarantine_file}")
            print(f"   {line}")
            # R8 口径：信息行走 summary_lines 聚合，不计入风险事件
            collector.add_summary_line(line)
    finally:
        if tm:
            with contextlib.suppress(Exception):
                tm.close()

    return out_final_path


def _remove_stale_risk_reports(out_dir: str, stem: str) -> list:
    """写入新风险清单前移除上一轮遗留的 {stem}_风险清单.md/.json。

    风险清单属最终产物而非断点恢复现场（不进 delete_resume_artifacts，
    否则成功路径会误删本轮刚写的新报告）；真实残留路径=上轮失败
    有清单无终稿、本轮无风险——写前清理与隔离区"先清后写"同款纪律。
    文件名与 risk.py 的 write_reports 双钉，契约测试防漂移。
    """
    removed = []
    for suffix in ("_风险清单.md", "_风险清单.json"):
        p = Path(out_dir) / f"{stem}{suffix}"
        if p.is_file():
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass
    return removed


def _backup_existing_outputs(out_dir: str, stem: str, collector=None,
                             file_name: str = None) -> None:
    """--force 重跑前的产物备份（仅精确匹配文件名，存在才备份）。

    对输出目录中确切名为 ``{stem}_final_cn.srt``、``{stem}_质量报告.txt``、
    ``{stem}_分歧复核.csv``、``{stem}_术语冲突观察.csv``、
    ``{stem}_风险清单.md``、``{stem}_风险清单.json`` 的文件，复制为同目录
    ``{原名去扩展}_bak_YYYYMMDD_HHMMSS.{原扩展}``；同一次运行共用同一时间戳。
    精确匹配保证旧的 ``*_bak_*`` 文件不会被再次备份。
    """
    import shutil
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for suffix in ("_final_cn.srt", "_质量报告.txt", "_分歧复核.csv",
                   "_术语冲突观察.csv", "_风险清单.md", "_风险清单.json"):
        p = Path(out_dir) / f"{stem}{suffix}"
        if not p.is_file():
            continue
        bak = p.with_name(f"{p.stem}_bak_{ts}{p.suffix}")
        try:
            shutil.copy2(p, bak)
            print(f"   💾 已备份: {p.name} -> {bak.name}")
        except Exception as e:
            print(f"   ⚠️ 备份失败（忽略）: {p.name} -> {e}")
            if collector is not None:
                collector.add(stage="force", file=file_name,
                              reason=f"产物备份失败: {p.name} -> {e}",
                              action="跳过备份直接覆盖",
                              severity=SEVERITY_WARNING)


def _auto_learn_glossary(cfg: RefineConfig, in_path: str, out_final_path: str,
                         collector=None, file_name: str = None,
                         threads: list | None = None):
    """自动词库学习：从 原文↔终稿 中提取术语对追加到 glossary_learned.csv。

    防污染：glossary_learn 内部做子串核验（原文/译文中必须真实存在），
    幻觉造词不会入库；learned 词库仅通过 load_glossary_merged 追加注入。

    v1.2.2 D3 治理开关：cfg.glossary_learn_enabled=False 时跳过本学习
    路径（跳过事实计数入日志与风险清单，止增不清退——存量清退用
    tools/glossary_learned_reset.py）。

    P1-6 异步化：学习调用放入后台 daemon 线程（注册到 run 级 threads
    列表，由 run_v2 收尾 join 超时 60s），不阻塞主流程；threads=None
    （无 run 上下文的直调）保持同步语义。
    """
    if not getattr(cfg, "glossary_learn_enabled", True):
        print("   ⏭️ 词库学习已被 glossary_learn_enabled=False 跳过")
        logger.info("glossary_learn_enabled=False，跳过 learned 词库学习"
                    "（file=%s）", file_name)
        if collector is not None:
            collector.add(stage="final", file=file_name,
                          reason="glossary_learn_enabled=False",
                          action="跳过 learned 词库学习（配置治理开关）",
                          severity=SEVERITY_INFO)
        return

    def _learn_once():
        try:
            from .glossary_learn import learn_from_s2_output
            try:
                endpoint = (cfg.resolve_endpoint("lmstudio")
                            or "http://localhost:1234/v1")
            except Exception:
                endpoint = "http://localhost:1234/v1"
            new_terms = learn_from_s2_output(
                in_path, out_final_path, learned_glossary_path(),
                endpoint=endpoint,
                timeout_http=getattr(cfg, "timeout_http", None),
                timeout_probe=getattr(cfg, "timeout_probe", None))
            if new_terms:
                print(f"   📚 词库学习：新增 {new_terms} 条术语 -> "
                      f"config/glossary_learned.csv")
        except Exception as e:
            print(f"   ⚠️ 词库学习失败（不影响翻译结果）: {e}")
            if collector is not None:
                collector.add(stage="final", file=file_name,
                              reason=f"词库学习失败: {e}",
                              action="跳过词库学习",
                              severity=SEVERITY_INFO)

    if threads is None:             # 直调模式：保持同步旧语义
        _learn_once()
        return
    t = threading.Thread(target=_learn_once, name="subtransjav-learn",
                         daemon=True)
    t.start()
    threads.append(t)


def _wrap_as_result(entries: list, a_result: StageAResult) -> StageAResult:
    """兜底规则可能增删条目，重建 StageAResult 供阶段B消费。"""
    return StageAResult(
        entries=entries,
        deleted=a_result.deleted,
        failed={e["index"] for e in entries
                if e["text"].startswith(UNTRANSLATED_PREFIX)},
        exact_hits=a_result.exact_hits,
    )


def _learn_to_tm(tm: TranslationMemory, orig_entries: list,
                 final_entries: list, flagged=None, gate=True,
                 must_see_spans=None, collector=None, file_name: str = None,
                 conflict_spans=None, block_conflicts=False) -> int | None:
    """终稿学习：日文原文 → 最终中文。

    ⚠️ 双指针对齐（同起点多条按序消费），只学时间轴完全一致
    （起点+终点均匹配）的 1:1 行：被兜底清洗合并/删除的行不入库——
    合并行的译文覆盖多条原文，按任何键对齐存入都会造成错位配对
    （legacy 毒化 TM 的同款路径）。

    Parameters
    ----------
    flagged : set | None
        post_validate 标记的行 index 集合（validator 类跳过）。
    gate : bool
        TM 学习准入门槛总开关。False 时跳过所有准入判定（A/B 验证用）。
    must_see_spans : set | None
        「必看分歧行」时间轴 span 集合（第三层门槛）。
        每元素为 ``_timing_span`` 返回的 ``(start_sec, end_sec)``，
        来自 merged 产物分歧行的 timing——与学习循环的 orig 侧
        （同一 merged 文件解析出的 entries）span 直接可比。
        该行 span 命中集合 → 一律不入库（双引擎严重分歧行，
        真问题富集区，学习风险大于收益）。仅 gate=True 时生效。
    conflict_spans : set | None
        术语冲突观察条目的时间轴 span 集合（v1.2.2 D1，glossary_conflict.
        scan_glossary_conflicts 产出冲突清单的 timing）。
    block_conflicts : bool
        cfg.glossary_conflict_block：True 时冲突条目禁止进入 TM 学习
        （入库前检查，冲突即跳过并计数）。独立于 gate（观察闸转阻断
        由用户裁决，不随 A/B 验证口径关断）。

    Returns
    -------
    int | None
        本次实际入库条数（store_batch 新增数；无候选 0）；学习过程
        异常时返回 None（报告 TM 摘要行按"取不到"处理）。
    """
    if flagged is None:
        flagged = set()
    if must_see_spans is None:
        must_see_spans = set()
    if conflict_spans is None:
        conflict_spans = set()
    try:
        from .post_validate import is_untranslated_text, scan_learn_defect
    except Exception:
        scan_learn_defect = None

        def is_untranslated_text(text: str) -> bool:
            # post_validate 不可用时的降级判定（保持既有降级语义：
            # 缺陷扫描层跳过，学习闸仍工作）
            return (text or "").strip().startswith("[未翻译]")

    # 准入跳过计数器（按类别）
    skip_kana = skip_leak = skip_src_prefix = skip_placeholder = 0
    skip_len_ratio = skip_validator = skip_must_see = 0
    skip_conflict = 0
    learned_count = 0

    try:
        pairs = []
        j = 0
        n = len(final_entries)
        eps = 1e-6
        for e in orig_entries:
            span = _timing_span(e["timing"])
            while j < n and _timing_span(final_entries[j]["timing"])[0] \
                    < span[0] - eps:
                j += 1                   # 该原文行被删除 → 跳过
            if j >= n:
                break
            fspan = _timing_span(final_entries[j]["timing"])
            if fspan[0] != span[0]:
                continue                 # 时间轴不对应 → 不学习
            if fspan == span:
                src = (e["text"] or "").strip()
                tgt = (final_entries[j]["text"] or "").strip()
                if (src and tgt
                        and not is_untranslated_text(tgt)
                        and tgt != src):         # 同文残留对不入库
                    # ---- 准入门槛 ----
                    # 层0：术语冲突观察闸转阻断（v1.2.2 D1）——独立于
                    # gate（用户显式开启 glossary_conflict_block 才生效）
                    if block_conflicts and fspan in conflict_spans:
                        skip_conflict += 1
                        j += 1
                        continue
                    if gate:
                        # 层3：必看分歧行（双引擎严重分歧，确定性阻断）
                        # 必看行 timing 来自 merged 产物（=in_path），学习
                        # 循环的 orig 侧即同一文件解析出的 entries，
                        # span 直接可比（时间轴是条目的真实身份标识）。
                        if span in must_see_spans:
                            skip_must_see += 1
                            j += 1
                            continue
                        # 层1：validator 类（で误译修正 / 主语误判）
                        if e["index"] in flagged:
                            skip_validator += 1
                            j += 1
                            continue
                        # 层2：scan_learn_defect 缺陷扫描
                        if scan_learn_defect is not None:
                            defect = scan_learn_defect(src, tgt)
                            if defect is not None:
                                if defect == "kana":
                                    skip_kana += 1
                                elif defect == "leak":
                                    skip_leak += 1
                                elif defect == "src_prefix":
                                    skip_src_prefix += 1
                                elif defect == "placeholder":
                                    skip_placeholder += 1
                                elif defect == "len_ratio":
                                    skip_len_ratio += 1
                                j += 1
                                continue
                    pairs.append((src, tgt, 1))
                j += 1                   # 精确匹配，消费该终稿行
            # fspan 起点相同但被合并（终点延长）→ 不学习也不消费，
            # 让被合并的后续原文行自然跳过
        if pairs:
            # v1.2.2 批次 A2：学习入库带上来源 srt 文件名 stem（TM
            # provenance，供后续 TM 清洗区分跨片同源句；file_name 为
            # Path(in_path).name，stem 即来源标识）
            source_stem = Path(file_name).stem if file_name else None
            added = tm.store_batch(pairs, source_name=source_stem)
            learned_count = int(added or 0)
            if added:
                print(f"   💾 翻译记忆库: 学习 {len(pairs)} 对"
                      f"（新增 {added} 条）")
        # 准入门槛统计行
        skip_total = (skip_kana + skip_leak + skip_src_prefix
                      + skip_placeholder + skip_len_ratio + skip_validator
                      + skip_must_see + skip_conflict)
        print(f"   🚫 学习门槛: 跳过 {skip_total} 行"
              f"（假名{skip_kana}/泄漏{skip_leak}/源残留{skip_src_prefix}"
              f"/占位符{skip_placeholder}/长度比{skip_len_ratio}"
              f"/validator {skip_validator}/必看{skip_must_see}"
              f"/术语冲突{skip_conflict}）")
        return learned_count
    except Exception as e:
        print(f"   ⚠️ 翻译记忆库学习失败: {e}")
        if collector is not None:
            collector.add(stage="final", file=file_name,
                          reason=f"翻译记忆库学习失败: {e}",
                          action="跳过TM学习",
                          severity=SEVERITY_INFO)
        return None
