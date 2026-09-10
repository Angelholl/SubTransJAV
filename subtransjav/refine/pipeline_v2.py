"""
v2 两阶段流水线编排
====================
阶段A 净语+翻译（ja→zh，LLM×1）：一次调用完成噪音删除与翻译；
阶段B 审校+抛光（zh→zh，LLM×1）：对照日文原文（`日文 ||| 中文` 格式）
审核误译/补译/润色，可整条删除杂音。

效率设计：
- 每批 LLM 调用 4+ 次（legacy 4 阶段）→ 2 次；
- TM 精确命中行直接替代译文，零 LLM 调用；
- 失败行由客户端定向重试 → 仍失败交阶段B补译 → 阶段B仍失败保留原文。

兜底规则层（profile 驱动，无需人工维护）：
- local  → strict：cleaner_rules 清洗 + post_validate 误译拦截（默认，本地弱模型）
- cloud  → lenient：仅格式/语言白名单（强模型产出可靠，避免规则误伤）

角色卡：config/templates/角色-净语翻译.txt（阶段A）、角色-审校抛光.txt（阶段B）。
"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import RefineConfig, ensure_language_support
from .filters import parse_srt, build_srt
from .glossary import match_glossary, format_glossary_block
from .instructions import write_effective_instructions, hardened_suffix
from .orchestrator import (
    CREATED_TMP_DIRS, _tmp_dirs_lock, RefineError, _init_tm,
    _resolve_stage_paths, load_glossary_merged, refine_tmp_dir,
    learned_glossary_path,
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
        "- 整条均为ASR噪音/填充/幻觉（无实义内容）的条目："
        "Translation> 后留空（表示删除），编号保持对齐\n"
        "- 其余条目按角色卡规范输出中文译文\n"
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
        "- 需要删除的条目（纯杂音冗余）在 Translation> 后留空\n"
        "- 禁止输出 .srt 时间码块、序号块、说明、总结或注释"
    ),
}

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
UNTRANSLATED_PREFIX = "[未翻译] "

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
_PREMERGE_MAX_SPAN = 8.0       # 合并后总时长上限（秒）
_PREMERGE_MAX_COUNT = 3        # 合并条数上限


def _premerge_entries(entries: list) -> list:
    """断句修复：按角色卡预处理标准合并被 ASR 错误切割的相邻碎片。"""
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

        # 合并判定（角色卡标准）
        allow = False
        if gap <= 1.0 and _PREMERGE_AUX_END.search(prev_text):
            allow = True                       # 助词/省略号结尾 → ≤1.0s 强制
        elif gap <= 0.5 and (
                _PREMERGE_INCOMPLETE_START.match(cur_text)
                or not _PREMERGE_COMPLETE_END.search(prev_text)):
            allow = True                       # 语义断裂 → ≤0.5s 合并
        # 否决：前条以省略号/波浪线收尾（刻意的戏剧停顿），后条自身是
        # 完整句 → 属独立字幕，不是 ASR 碎片（无论 gap 多小、走哪个档
        # 都不合并；如「ボクたち、水泳部の部長で…」+「誰もが一目置くエース。」）
        if _PREMERGE_TRAILING.search(prev_text) \
                and _PREMERGE_COMPLETE_END.search(cur_text):
            allow = False
        # 禁止：两条都是完整陈述句（各自语义完整）
        if allow and _PREMERGE_COMPLETE_END.search(prev_text) \
                and _PREMERGE_COMPLETE_END.search(cur_text) and gap > 0.2:
            allow = False
        # 禁止：超长/超条数
        n_prev = prev.get("_merge_count", 1)
        if merged_dur > _PREMERGE_MAX_SPAN or n_prev >= _PREMERGE_MAX_COUNT:
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


def _load_v2_instruction(cfg: RefineConfig, tag: str, gl_block: str,
                         tmp_dir: str) -> tuple:
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
        temperature = 0.5
        concurrency = max(1, cfg.v2_concurrency)
    elif provider == "lmstudio":
        base_url = cfg.resolve_endpoint("lmstudio") or "http://localhost:1234/v1"
        api_key = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")  # 本地端点占位
        n_ctx = cfg.v2_ctx_local
        temperature = 0.1   # 沿用本地实测最优低温
        concurrency = max(1, cfg.v2_concurrency)
    elif provider == "ollama":
        base_url = cfg.resolve_endpoint("ollama") or "http://localhost:11434/v1"
        api_key = "ollama"   # 本地服务免密钥，占位即可
        n_ctx = cfg.v2_ctx_local
        temperature = 0.1
        concurrency = max(1, cfg.v2_concurrency)
    else:
        base_url = cfg.resolve_endpoint(provider)
        api_key = cfg.resolve_api_key(provider)
        n_ctx = None
        temperature = 0.5
        concurrency = max(1, cfg.v2_concurrency)

    if not base_url:
        raise RefineError(f"{V2_STAGE_NAMES[tag]}: 服务商 [{provider}] 缺少接口地址")
    if not model:
        raise RefineError(f"{V2_STAGE_NAMES[tag]}: 未指定模型名")

    cc = ClientConfig(
        base_url=base_url, api_key=api_key or "", model=model,
        temperature=temperature, concurrency=concurrency, n_ctx=n_ctx,
    )
    return LLMClient(cc, log=print)


def _provider_default_model(cfg: RefineConfig, provider: str) -> str:
    from .config import PROVIDER_MODEL_DEFAULTS
    return PROVIDER_MODEL_DEFAULTS.get(provider, "")


# ---------------------------------------------------------------------------
# 阶段执行
# ---------------------------------------------------------------------------


@dataclass
class StageAResult:
    entries: list           # 阶段A产物条目 [{index,timing,text}]
    deleted: set            # 被删除的 index
    failed: set             # 重试后仍失败的 index（交阶段B补译）
    exact_hits: dict        # TM 精确命中 {index: zh}


def _make_fallback_client(cfg: RefineConfig):
    """本地接管客户端（LM Studio + fallback_model，低温串行）。"""
    from subtransjav.translate.llm_client import ClientConfig, LLMClient
    cc = ClientConfig(
        base_url=cfg.resolve_endpoint("lmstudio") or "http://localhost:1234/v1",
        api_key=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"),  # 本地端点占位
        model=cfg.fallback_model.strip(),
        temperature=0.1,
        concurrency=1,
        n_ctx=cfg.v2_ctx_local,
    )
    return LLMClient(cc, log=print)


def _fallback_enabled(cfg: RefineConfig, tag: str) -> bool:
    """云端故障本地接管条件：仅云端阶段且已启用并配置接管模型。
    本地服务商（lmstudio/ollama）不启用云端故障后的本地接管。"""
    return (cfg.fallback_local and bool(cfg.fallback_model.strip())
            and cfg.stages[V2_STAGE_SLOT[tag]].provider not in ("lmstudio", "ollama"))


def _run_with_fallback(cfg: RefineConfig, tag: str, client,
                       todo: list, *, system_text: str, user_prompt: str,
                       max_batch_size: int, allow_empty_deletions: bool):
    """云端阶段执行 + 故障接管：失败行（批失败/缺行）切换本地模型重跑一次。

    result.failed 中被本地接管修复的行移回 translations。
    """
    result = client.translate_entries(
        todo, system_text=system_text, user_prompt=user_prompt,
        max_batch_size=max_batch_size,
        allow_empty_deletions=allow_empty_deletions,
        progress=lambda m: print(f"   ⏳ {m}"))

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
                           verbose: bool = True) -> dict:
    """对 targets 逐条生成语法提示（SudachiPy 句法分析，legacy 同款）。

    context_entries 提供条目上下文（前后条参与分析），targets 为需要
    提示的条目。未安装 sudachipy 或分析失败时返回空 dict（静默降级）。
    """
    hints = {}
    try:
        from .grammar_hint import generate_grammar_hints, is_grammar_hint_available
        if not is_grammar_hint_available():
            if verbose:
                print("   ℹ️ 语法提示: 未安装 sudachipy，跳过句法分析")
            return hints
        srt_content = build_srt(context_entries)
        for e in targets:
            hint = generate_grammar_hints(
                srt_content, e["index"], entries=context_entries)
            if hint:
                hints[e["index"]] = hint
    except Exception as e:
        if verbose:
            print(f"   ⚠️ 语法提示注入失败（忽略）: {e}")
        return {}
    return hints


def _inject_stage_a_assists(cfg: RefineConfig, entries: list, todo: list,
                            tm) -> list:
    """阶段A 输入辅助注入（只改发给 LLM 的文本，不影响 TM 键与产物）：
      1) 语法提示（SudachiPy 句法分析，legacy 同款；未安装则自动跳过）；
      2) TM 模糊命中参考（高阈值旧译文，标注仅供参考防照抄；不写库）。
    返回新的 todo 列表（原 entries 不变）。
    """
    todo = [dict(e) for e in todo]

    # 1) 语法提示
    hints = _collect_grammar_hints(entries, todo)
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
                 glossary: list) -> StageAResult:
    # 协议标记：供 webview_gui 检测阶段切换，更新进度显示（勿删）
    print(f"[STAGE] {V2_STAGE_NAMES['A']}", flush=True)
    print("\n🔹 [阶段A 净语+翻译] 一次调用完成清洗与日译中")

    # TM 精确命中替代：命中行零 LLM 调用（查阶段1 日→中 翻译对）
    exact = {}
    if tm:
        for e in entries:
            src = (e["text"] or "").strip()
            hit = tm.lookup_exact(src, 1)
            # 防御：译文与原文相同（ja→ja 残留对）不替代，避免日文漏进中文产物
            if hit and hit.strip() != src:
                exact[e["index"]] = hit
        if exact:
            print(f"   💾 TM 精确命中 {len(exact)}/{len(entries)} 条，"
                  f"直接替代（跳过 LLM）")

    todo = [e for e in entries if e["index"] not in exact]

    # 辅助注入：语法提示（SudachiPy）+ TM 模糊命中参考
    todo = _inject_stage_a_assists(cfg, entries, todo, tm)

    src_text = "\n".join(e["text"] for e in entries)
    gl_block = _v2_glossary_block(cfg, "A", src_text, glossary)
    system_text, user_prompt = _load_v2_instruction(cfg, "A", gl_block, tmp_dir)

    stage_cfg = cfg.stages[V2_STAGE_SLOT["A"]]
    client = _make_client(cfg, "A")
    result = _run_with_fallback(
        cfg, "A", client, todo, system_text=system_text,
        user_prompt=user_prompt, max_batch_size=cfg.batch_for(stage_cfg),
        allow_empty_deletions=True)

    out_entries = []
    for e in entries:
        i = e["index"]
        if i in exact:
            text = exact[i]
        elif i in result.translations:
            text = result.translations[i]
        elif i in result.deleted:
            continue
        else:
            # 客户端定向重试后仍失败：标记交阶段B补译
            text = UNTRANSLATED_PREFIX + e["text"]
        out_entries.append({"index": i, "timing": e["timing"], "text": text})

    failed = set(result.failed)
    if failed:
        print(f"   ⚠️ {len(failed)} 条阶段A失败，交阶段B补译")
    return StageAResult(entries=out_entries, deleted=set(result.deleted),
                        failed=failed, exact_hits=exact)


def _run_stage_b(cfg: RefineConfig, a_result: StageAResult, orig_entries: list,
                 tmp_dir: str, glossary: list) -> list:
    """阶段B：对照日文原文审校+抛光。返回最终条目列表。"""
    # 协议标记：供 webview_gui 检测阶段切换，更新进度显示（勿删）
    print(f"[STAGE] {V2_STAGE_NAMES['B']}", flush=True)
    print("\n🔹 [阶段B 审校+抛光] 对照日文原文审核/补译/润色")

    # 组装 B 输入：`日文原文 ||| 中文译文`
    # 日文参照按时间轴双指针对齐（同起点多条不会错配）
    aligned_orig = _align_orig_by_timing(a_result.entries, orig_entries)
    b_entries = []
    for ae, orig in zip(a_result.entries, aligned_orig):
        ja = (orig["text"] or "").strip() if orig else ""
        zh = ae["text"]
        if zh.startswith(UNTRANSLATED_PREFIX):
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
        for ae, orig in zip(a_result.entries, aligned_orig)]
    hints = _collect_grammar_hints(ja_entries, ja_entries, verbose=False)
    if hints:
        print(f"   📝 语法提示已注入（审校）: {len(hints)}/{len(b_entries)} 条")
        for e in b_entries:
            h = hints.get(e["index"])
            if h:
                e["text"] = f"{h}\n原文：{e['text']}"

    src_text = "\n".join(e["text"] for e in b_entries)
    gl_block = _v2_glossary_block(cfg, "B", src_text, glossary)
    system_text, user_prompt = _load_v2_instruction(cfg, "B", gl_block, tmp_dir)

    stage_cfg = cfg.stages[V2_STAGE_SLOT["B"]]
    client = _make_client(cfg, "B")
    result = _run_with_fallback(
        cfg, "B", client, b_entries, system_text=system_text,
        user_prompt=user_prompt, max_batch_size=cfg.batch_for(stage_cfg),
        allow_empty_deletions=True)

    # 组装最终产物：B 结果优先 → 回退 A 译文 → 回退日文原文（宁多勿缺）
    # B 输入带【语法提示】段，LLM 若回显残留则在此兜底清理
    # （clean_grammar_hint_residue 的模式覆盖阶段B注入格式）。
    from .cleaner_rules import clean_grammar_hint_residue
    keep_untranslated = cfg.v2_keep_untranslated != "empty"
    final = []
    for ae, orig in zip(a_result.entries, aligned_orig):
        i = ae["index"]
        keep_flag = False
        if i in result.deleted:
            continue
        if i in result.translations:
            text = clean_grammar_hint_residue(result.translations[i])
        elif not ae["text"].startswith(UNTRANSLATED_PREFIX):
            text = ae["text"]            # B 失败回退 A 译文
        elif keep_untranslated:
            # 回退日文原文（宁多勿缺）。打内部标记：后续语言白名单过滤
            # （zh）会把纯日文行判无效，带标记条目须跳过该过滤（见
            # _run_single_v2）。build_srt 只读 index/timing/text，
            # 该键不会影响产物。
            text = (orig["text"] or "").strip() if orig else ""  # 回退日文原文
            keep_flag = True
        else:
            continue                     # 配置为 empty：整条移除
        if not text:
            continue                     # 空正文不进终稿
        entry = {"index": i, "timing": ae["timing"], "text": text}
        if keep_flag:
            entry["_keep_original"] = True
        final.append(entry)
    return final


def _apply_fallback_rules(cfg: RefineConfig, entries: list,
                          orig_entries: list) -> tuple:
    """兜底规则层（profile 驱动）：
    local(strict) → cleaner_rules 清洗 + post_validate 误译拦截；
    cloud(lenient) → 跳过（仅保留语言白名单等零维护校验）。
    返回 (entries, validator_warnings, clean_merged, flagged_indexes)：
      validator_warnings — post_validate 告警列表（传给质量报告）；
      clean_merged — clean_srt 前后条数差（lenient 档为 None）；
      flagged_indexes — post_validate 标记的行 index 集合（TM 学习准入用）。"""
    if cfg.v2_profile != "local":
        return entries, [], None, set()

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

    # cleaner_rules：规则清洗（删除残余噪音/碎片）
    clean_merged = 0
    try:
        from .cleaner_rules import clean_srt
        cleaned = clean_srt(build_srt(entries),
                            config_dir=cfg.cleaner_config_dir or None)
        cleaned_entries = parse_srt(cleaned)
        clean_merged = len(entries) - len(cleaned_entries)
        if clean_merged > 0:
            print(f"   🧹 兜底清洗: 规则引擎移除 {clean_merged} 条")
        elif clean_merged < 0:
            clean_merged = 0

        # ⚠️ 身份恢复（防错位的关键步骤）：cleaner 会按输出顺序重新编号，
        # 若不恢复，后续 阶段B 的日文参照与 TM 学习都会整体错位——这正是
        # legacy 管线毒化 TM 的同款路径。按时间轴双指针恢复原始编号
        # （同起点多条按顺序消费，不会互相覆盖）。
        aligned = _align_orig_by_timing(cleaned_entries, orig_entries)
        restored = 0
        for e, o in zip(cleaned_entries, aligned):
            if o is not None and e["index"] != o["index"]:
                e["index"] = o["index"]
                restored += 1
        if restored:
            print(f"   🔧 已按时间轴恢复 {restored} 条原始编号（防错位）")
        return cleaned_entries, validator_warnings, clean_merged, flagged_indexes
    except Exception as e:
        print(f"   ⚠️ 兜底清洗失败（忽略）: {e}")
        return entries, validator_warnings, 0, flagged_indexes


def _filter_language(cfg: RefineConfig, entries: list, stage_idx: int) -> list:
    """语言白名单校验（零维护通用校验，两档 profile 均启用）。"""
    srt = build_srt(entries)
    kept_entries, dropped = filter_stage_output_srt(srt, stage_idx, "zh")
    if dropped:
        print(f"   🧹 乱码/幻觉残留过滤：移除 {dropped} 条 -> "
              f"Errors/dropped_entries.log")
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
        try:
            os.unlink(tmp)
        except OSError:
            pass


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
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run_v2(cfg: RefineConfig):
    """执行 v2 两阶段流水线。返回最后一个成功输出。"""
    errors = cfg.validate()
    if errors:
        raise RefineError("配置错误：\n  - " + "\n  - ".join(errors))
    if isinstance(cfg.inputs, str):
        cfg.inputs = [cfg.inputs]

    results, failures = [], []
    total = len(cfg.inputs)
    for idx, path in enumerate(list(cfg.inputs), 1):
        try:
            out = _run_single_v2(cfg, path)
            results.append(out)
            print(f"[refine-v2] 文件成功 ({idx}/{total})：{Path(path).name}")
        except Exception as e:   # noqa: BLE001 单文件隔离
            failures.append((path, e))
            print(f"\n❌ [refine-v2] 文件失败 ({idx}/{total})："
                  f"{Path(path).name}\n   原因: {e}")

    if failures:
        print(f"\n⚠️ [refine-v2] 批量完成：成功 {len(results)} / "
              f"失败 {len(failures)} / 共 {total}")
    if not results:
        raise RefineError(f"全部 {len(failures)} 个文件均处理失败")
    return results[-1]


def _run_single_v2(cfg: RefineConfig, in_path: str) -> str:
    ensure_language_support()

    in_path, out_dir, stem = _resolve_stage_paths(cfg, in_path)
    tmp_dir = refine_tmp_dir(in_path, stem)
    with _tmp_dirs_lock:
        if tmp_dir not in CREATED_TMP_DIRS:
            CREATED_TMP_DIRS.append(tmp_dir)

    out_a_path = str(Path(out_dir) / f"{stem}_refine_A.srt")
    out_final_path = str(Path(out_dir) / f"{stem}_final_cn.srt")
    force = bool(getattr(cfg, "force", False))
    if Path(out_final_path).is_file() and not force:
        print(f"\n🔹 [v2] 终稿已存在，跳过：{Path(out_final_path).name}")
        return out_final_path
    if force:
        _backup_existing_outputs(out_dir, stem)

    glossary = load_glossary_merged(cfg)
    tm = _init_tm(cfg)

    orig_entries = parse_srt(Path(in_path).read_text(encoding="utf-8"))
    if not orig_entries:
        raise RefineError("输入 SRT 无有效条目")

    # 断句预合并：修复 ASR 错误切割（代码层确定性步骤，两档 profile 均启用）
    premerge_merged = 0
    if cfg.premerge_enabled:
        n0 = len(orig_entries)
        orig_entries = _premerge_entries(orig_entries)
        premerge_merged = n0 - len(orig_entries)
        if premerge_merged:
            print(f"   🔗 断句预合并: {n0} → {len(orig_entries)} 条"
                  f"（修复 ASR 错误切割）")

    print("=" * 60)
    print(f"🚀 [refine-v2] 输入: {Path(in_path).name}")
    for tag in V2_STAGE_TAGS:
        stage_cfg = cfg.stages[V2_STAGE_SLOT[tag]]
        m = stage_cfg.model or _provider_default_model(cfg, stage_cfg.provider)
        print(f"   {V2_STAGE_NAMES[tag]}: [{stage_cfg.provider}] {m} | "
              f"批量{cfg.batch_for(stage_cfg)} | profile={cfg.v2_profile}")
    print("=" * 60)

    try:
        # ---- 阶段A ----
        a_result = _run_stage_a(cfg, orig_entries, tm, tmp_dir, glossary)
        _atomic_write_text(out_a_path, build_srt(a_result.entries))
        print(f"   ✅ 阶段A完成 -> {Path(out_a_path).name} "
              f"({len(a_result.entries)} 条)")

        # ---- 兜底规则层（strict/lenient）----
        a_entries, validator_warnings, clean_merged, flagged_indexes = \
            _apply_fallback_rules(cfg, a_result.entries, orig_entries)

        # ---- 阶段B ----
        a_for_b = _wrap_as_result(a_entries, a_result)
        final_entries = _run_stage_b(cfg, a_for_b, orig_entries, tmp_dir,
                                     glossary)
        # ---- 语言白名单过滤 ----
        # 带 _keep_original 标记的回退日文原条目跳过 zh 白名单过滤
        # （否则 keep_untranslated 回退的日文原文会被误判删除），
        # 其余条目照常过滤后按时间轴合并回原顺序。
        keep_entries = [e for e in final_entries if e.get("_keep_original")]
        normal_entries = [e for e in final_entries if not e.get("_keep_original")]
        normal_entries = _filter_language(cfg, normal_entries, 3)
        final_entries = sorted(normal_entries + keep_entries,
                               key=lambda e: _timing_span(e["timing"])[0])
        _atomic_write_text(out_final_path, build_srt(final_entries))
        print(f"   ✅ 完成 -> {Path(out_final_path).name} "
              f"({len(final_entries)} 条)")

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
                from .pass_disagreement import (collect_disagreement,
                                                probe_disagreement_mode)
                pass_mode = probe_disagreement_mode(in_path)
                disag = collect_disagreement(in_path)
            except Exception as e:
                print(f"   ⚠️ 双引擎分歧采集失败（忽略）: {e}")
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
        if tm:
            _learn_to_tm(tm, orig_entries, final_entries,
                         flagged=flagged_indexes, gate=cfg.tm_learn_gate,
                         must_see_spans=must_see_spans)

        # ---- 自动词库学习（cfg.auto_glossary，含防幻觉核验）----
        if cfg.auto_glossary:
            _auto_learn_glossary(cfg, in_path, out_final_path)

        # ---- 自动质量报告（cfg.quality_report，落盘到输出目录）----
        if cfg.quality_report:
            try:
                from .quality_report import (build_quality_report,
                                             write_quality_report,
                                             write_divergence_review_csv)
                # pass_mode/disag 已在 TM 学习块前采集并共用
                # （两者都关时此处保持 None，与原先 block 内采集等价）
                if pass_mode is None and disag is None:
                    from .pass_disagreement import (collect_disagreement,
                                                    probe_disagreement_mode)
                    pass_mode = probe_disagreement_mode(in_path)
                    disag = collect_disagreement(in_path)
                merge_stats = {"premerge_merged": premerge_merged}
                if clean_merged is not None:
                    merge_stats["clean_merged"] = clean_merged
                report = build_quality_report(
                    orig_entries, final_entries, Path(in_path).name,
                    expected_entries=orig_entries,
                    merge_stats=merge_stats,
                    validator_warnings=validator_warnings,
                    pass_disagreement=disag,
                    pass_mode=pass_mode)
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
    finally:
        if tm:
            try:
                tm.close()
            except Exception:
                pass

    return out_final_path


def _backup_existing_outputs(out_dir: str, stem: str) -> None:
    """--force 重跑前的产物备份（仅精确匹配文件名，存在才备份）。

    对输出目录中确切名为 ``{stem}_final_cn.srt``、``{stem}_质量报告.txt``、
    ``{stem}_分歧复核.csv`` 的文件，复制为同目录
    ``{原名去扩展}_bak_YYYYMMDD_HHMMSS.{原扩展}``；同一次运行共用同一时间戳。
    精确匹配保证旧的 ``*_bak_*`` 文件不会被再次备份。
    """
    import shutil
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for suffix in ("_final_cn.srt", "_质量报告.txt", "_分歧复核.csv"):
        p = Path(out_dir) / f"{stem}{suffix}"
        if not p.is_file():
            continue
        bak = p.with_name(f"{p.stem}_bak_{ts}{p.suffix}")
        try:
            shutil.copy2(p, bak)
            print(f"   💾 已备份: {p.name} -> {bak.name}")
        except Exception as e:
            print(f"   ⚠️ 备份失败（忽略）: {p.name} -> {e}")


def _auto_learn_glossary(cfg: RefineConfig, in_path: str, out_final_path: str):
    """自动词库学习：从 原文↔终稿 中提取术语对追加到 glossary_learned.csv。

    防污染：glossary_learn 内部做子串核验（原文/译文中必须真实存在），
    幻觉造词不会入库；learned 词库仅通过 load_glossary_merged 追加注入。
    """
    try:
        from .glossary_learn import learn_from_s2_output
        try:
            endpoint = cfg.resolve_endpoint("lmstudio") or "http://localhost:1234/v1"
        except Exception:
            endpoint = "http://localhost:1234/v1"
        new_terms = learn_from_s2_output(
            in_path, out_final_path, learned_glossary_path(),
            endpoint=endpoint)
        if new_terms:
            print(f"   📚 词库学习：新增 {new_terms} 条术语 -> "
                  f"config/glossary_learned.csv")
    except Exception as e:
        print(f"   ⚠️ 词库学习失败（不影响翻译结果）: {e}")


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
                 must_see_spans=None):
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
    """
    if flagged is None:
        flagged = set()
    if must_see_spans is None:
        must_see_spans = set()
    try:
        from .post_validate import scan_learn_defect
    except Exception:
        scan_learn_defect = None

    # 准入跳过计数器（按类别）
    skip_kana = skip_leak = skip_src_prefix = skip_placeholder = 0
    skip_len_ratio = skip_validator = skip_must_see = 0

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
                        and not tgt.startswith(UNTRANSLATED_PREFIX)
                        and tgt != src):         # 同文残留对不入库
                    # ---- 准入门槛 ----
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
            added = tm.store_batch(pairs)
            if added:
                print(f"   💾 翻译记忆库: 学习 {len(pairs)} 对"
                      f"（新增 {added} 条）")
        # 准入门槛统计行
        skip_total = (skip_kana + skip_leak + skip_src_prefix
                      + skip_placeholder + skip_len_ratio + skip_validator
                      + skip_must_see)
        print(f"   🚫 学习门槛: 跳过 {skip_total} 行"
              f"（假名{skip_kana}/泄漏{skip_leak}/源残留{skip_src_prefix}"
              f"/占位符{skip_placeholder}/长度比{skip_len_ratio}"
              f"/validator {skip_validator}/必看{skip_must_see}）")
    except Exception as e:
        print(f"   ⚠️ 翻译记忆库学习失败: {e}")
