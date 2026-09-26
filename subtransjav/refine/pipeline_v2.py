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
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .artifact_lock import (
    ArtifactLockConflict,
    acquire_artifact_lock,
    release_artifact_lock,
)
from .asr_meta import (
    SUSPECT_STATUSES,
    load_asr_meta,
    load_asr_telemetry,
    scene_low_trust,
)
from .config import (
    DEEPSEEK_BASE_DEFAULT,
    RefineConfig,
    ensure_language_support,
)
from .events import EventEmitter
from .filters import build_srt, parse_srt
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
    delete_resume_artifacts,
    manifest_path,
    save_manifest,
)
from .pipeline_support import (
    CREATED_TMP_DIRS,
    RefineError,
    _init_tm,
    _resolve_stage_paths,
    _tmp_dirs_lock,
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
from .v2_context_blocks import (  # noqa: F401
    _SIDECAR_MISHEAR_HEADER,
    _SIDECAR_MISHEAR_TAG,
    _SIDECAR_SUMMARY_HEADER,
    _SIDECAR_SUMMARY_TAG,
    _SYNOPSIS_BLOCK_TAG,
    SIDECAR_MISHEAR_NOTICE,
    SIDECAR_SUMMARY_NOTICE,
    _load_context_sidecar,
    _synopsis_prompt_block,
    _v2_glossary_block,
    _v2_sidecar_block,
    load_context_sidecar,
)

# ---- 1.3.0 拆分批次 2：兜底规则层与学习层迁出（facade re-export——同批次 1 契约）----
from .v2_learn import _auto_learn_glossary, _learn_to_tm  # noqa: F401
from .v2_manifest_fp import (  # noqa: F401
    _TM_FINGERPRINT_COLUMNS,
    _glossary_fingerprint,
    _prepare_manifest,
    _tm_fingerprint,
    _v2_models_payload,
)
from .v2_outputs import (  # noqa: F401
    _BATCH_PROGRESS_RE,
    _GATE0_REPORT_SAMPLE_CAP,
    _asr_meta_min_coverage_pct,
    _atomic_write_text,
    _backup_existing_outputs,
    _build_gate0_report,
    _emit_batch_progress,
    _gate0_summary_line,
    _remove_stale_risk_reports,
    _remove_tmp_dir,
    filter_stage_output_srt,
)

# ---- 1.3.0 拆分批次 1：纯函数层迁出（facade re-export——保持 pv.<name> 可解析、
#      可 monkeypatch；调用点全部在本模块内，patch 语义零变更，见 D2026-0922-03 HRO-2）----
from .v2_premerge import (  # noqa: F401
    _UNTRANSLATED_MARK_LOCAL,
    UNTRANSLATED_PREFIX,
    _align_orig_by_timing,
    _normalize_untranslated_marker,
    _premerge_entries,
    _strip_trailing_pause,
    _timing_span,
)
from .v2_rules import _apply_fallback_rules, _filter_language  # noqa: F401

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

# D5 乱码强译复核：源文含汉字判定（含汉字即视为实义行，不入复核候选）
_KANJI_SRC_RE = re.compile(r"[\u4e00-\u9fff]")

# ---------------------------------------------------------------------------
# P1-6 语法提示跨阶段缓存：键 (sha1(条目文本), 阶段tag, profile)。
# A/B 两阶段与多文件批次共享；同一文本（ASR 重复行极常见）只分析一次。
# 容量满 50000 时按 LRU 逐条淘汰最旧键（v1.3.0 D3 终选：
# D2026-0925-01 补充裁决——OrderedDict+move_to_end+popitem(last=False)，
# 不再整表 clear()，热键跨淘汰轮保留；负缓存 None 同样入缓存）。
# ---------------------------------------------------------------------------
_GRAMMAR_CACHE: "OrderedDict" = OrderedDict()
_GRAMMAR_CACHE_MAX = 50000
_GRAMMAR_CACHE_LOCK = threading.Lock()


def _grammar_cache_key(text: str, tag: str, profile: str) -> tuple:
    return (hashlib.sha1((text or "").encode("utf-8")).hexdigest(),
            tag, profile)


# ---------------------------------------------------------------------------
# 指令/客户端构建
# ---------------------------------------------------------------------------


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
        # LRU 淘汰（D3 终选）：触顶不再整表清空，逐条弹出最旧键，
        # 直到低于容量——循环以应对一次批量可能跨多条淘汰
        while len(_GRAMMAR_CACHE) >= _GRAMMAR_CACHE_MAX:
            _GRAMMAR_CACHE.popitem(last=False)
        for e in targets:
            key = _grammar_cache_key(e.get("text"), tag, profile)
            if key in _GRAMMAR_CACHE:
                _GRAMMAR_CACHE.move_to_end(key)
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
                # 同款 LRU 淘汰（D3 终选）：写回前先逐条弹出最旧键
                while len(_GRAMMAR_CACHE) >= _GRAMMAR_CACHE_MAX:
                    _GRAMMAR_CACHE.popitem(last=False)
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
        except Exception as exc:
            print(f"   ⚠️ TM 模糊参考注入失败（忽略）: {exc}")
            if collector is not None:
                collector.add(stage="A", file=file_name,
                              reason=f"TM 模糊参考注入失败: {exc}",
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
            except Exception as exc:
                print(f"   ⚠️ TM 批量查询失败，回退逐条（忽略）: {exc}")
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


def map_entries_to_scenes(entries: list, scenes: dict) -> dict:
    """H4b：条目 → 场景归属映射（dict[条目编号 index → 场景号 scene_no]）。

    场景按 scene_no 升序累计 audio_duration_s 得时间边界（秒）；条目按
    timing 中点落格（复用 _timing_span 解析，放本层以保持 asr_meta 零新
    依赖）。超出末场景累计边界的条目不映射（禁外推）；timing 不可解析
    或缺条目编号的条目同样不映射。

    注意：场景归属仅供档位划分、非真值——场景边界为"时长累计"近似
    （上游按音频分段产出遥测，无时间码），与真实时间轴可能存在漂移。
    """
    if not scenes:
        return {}
    boundaries: list = []
    acc = 0.0
    for scene_no in sorted(scenes):
        scene = scenes[scene_no] or {}
        try:
            dur = float(scene.get("audio_duration_s") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        acc += max(0.0, dur)
        boundaries.append((scene_no, acc))
    total = boundaries[-1][1]
    mapping: dict = {}
    for e in entries:
        idx = e.get("index")
        if idx is None:
            continue
        start, end = _timing_span(e.get("timing"))
        # v2_premerge._timing_span 解析失败约定返回 (-1, -1)（非 None）
        if start is None or end is None or start < 0 or end < 0:
            continue
        mid = (start + end) / 2.0
        if mid > total:
            continue                  # 超出末场景累计边界：不映射（禁外推）
        for scene_no, upper in boundaries:
            if mid <= upper:
                mapping[idx] = scene_no
                break
    return mapping


def _run_single_v2(cfg: RefineConfig, in_path: str, collector=None,
                   emitter=None, learn_threads: list | None = None) -> str:
    """产物锁挂点（v1.3.0 D4，D2026-0925-01 终选）：锁键=输入 sha1+output_dir。

    锁在 _resolve_stage_paths 之后、写任何产物之前获取。三态：持锁成功→
    正常处理；同键冲突（ArtifactLockConflict）→ 打印中文错误并抛
    RefineError，由上层既有单文件失败路径兜底；锁机制不可用（返回
    None）→ 降级无锁继续，不拒绝文件。finally 释放，进程死 OS 自动释放。
    """
    resolved_in, resolved_out, _stem = _resolve_stage_paths(cfg, in_path)
    lock = None
    try:
        lock = acquire_artifact_lock(resolved_in, resolved_out)
    except ArtifactLockConflict as e:
        print(f"❌ [v2] 产物锁被其他进程占用，拒绝处理该文件："
              f"{Path(resolved_in).name}（输出目录 {resolved_out}）")
        raise RefineError(
            f"产物锁被其他进程占用：{Path(resolved_in).name}") from e
    try:
        return _run_single_v2_impl(cfg, in_path, collector=collector,
                                   emitter=emitter,
                                   learn_threads=learn_threads)
    finally:
        if lock is not None:
            release_artifact_lock(lock)


def _run_single_v2_impl(cfg: RefineConfig, in_path: str, collector=None,
                        emitter=None, learn_threads: list | None = None) -> str:
    if collector is None:
        collector = RiskCollector()
    if emitter is None:
        emitter = EventEmitter(enabled=False)   # 直调模式：不发事件
    ensure_language_support()

    in_path, out_dir, stem = _resolve_stage_paths(cfg, in_path)
    fname = Path(in_path).name
    # D11 契约④：行动层重翻台账存在 → 本轮是重翻后的管线重算，产物将
    # 另起快照。只告警不阻断、此处不删除——旧台账由本轮写前清
    # （_remove_stale_risk_reports）按"成品伴生件写前清旧"纪律清掉。
    if (Path(out_dir) / f"{stem}_重翻记录.json").is_file():
        print("⚠️ 检测到重翻台账，本轮产物将另起快照；台账保留供审计")
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

    # H4b：场景级转写遥测（Balanced 模式 raw_subs/<名>.asr_telemetry.jsonl）
    # → 场景低信任 → 条目级阈值自适应（仅收紧删五类参数，计数类永不解锁）。
    # 映射/谓词组装在本层完成（asr_meta 保持零新依赖，不 import v2_premerge）。
    telemetry = load_asr_telemetry(cfg, in_path)
    telemetry_scenes = telemetry.get("scenes") or {}
    adaptive_opt_in = bool(getattr(cfg, "adaptive_thresholds", False))
    adaptive = (adaptive_opt_in and bool(telemetry.get("present"))
                and not bool(telemetry.get("stale")))
    low_trust_scene_nos = {no for no, sc in telemetry_scenes.items()
                           if scene_low_trust(sc)}
    scene_of_entry: dict = {}
    adaptive_entries = 0
    if adaptive:
        scene_of_entry = map_entries_to_scenes(orig_entries, telemetry_scenes)
        adaptive_entries = sum(
            1 for e in orig_entries
            if scene_of_entry.get(e.get("index")) in low_trust_scene_nos)
        print(f"   📡 上游场景遥测：{len(telemetry_scenes)} 场景 / "
              f"低信任 {len(low_trust_scene_nos)} 场景 / "
              f"自适应收紧条目 {adaptive_entries} 条"
              f"（跳行 {telemetry.get('skipped_lines', 0)}）")
    elif adaptive_opt_in:
        # 红标（限定触发域=仅 opt-in 后）：遥测缺失/超龄 → 默认阈值执行
        collector.add(stage="gate0", file=fname,
                      reason="自适应阈值已启用但未发现可用 asr_telemetry"
                             "（仅上游 Balanced 模式产出），本轮按默认阈值执行",
                      action="按默认档位执行",
                      severity=SEVERITY_WARNING)
    # 三可数指标并入 upstream block（键= present/stale/file/scenes/low_trust/
    # adaptive_entries/skipped_lines）。条件并入：opt-in 或确有遥测时才挂
    # telemetry 子块——既有 gate0_summary payload 契约测试
    # （tests/test_pipeline_v2.py）钉死无信号场景 upstream 键集，
    # 缺省路径必须零变化。
    telemetry_block = {
        "present": bool(telemetry.get("present")),
        "stale": bool(telemetry.get("stale")),
        "file": telemetry.get("file"),
        "scenes": len(telemetry_scenes),
        "low_trust": len(low_trust_scene_nos),
        "adaptive_entries": adaptive_entries,
        "skipped_lines": int(telemetry.get("skipped_lines") or 0),
    }
    if adaptive_opt_in or telemetry.get("present") or telemetry.get("stale"):
        upstream_block["telemetry"] = telemetry_block

    # 闸门0：送翻前源侧幻觉检测（预合并前对原始条目生效，两档 profile 均执行；
    # gate0_stats 由质量报告【处置】章节与 gate0_summary 事件消费）。
    # H5：候选 position 指向本次检测输入，先留快照供条目编号对齐。
    # H4b：自适应开启时按"场景低信任 → 条目"谓词逐条收紧删五类参数。
    gate0_input = orig_entries
    tight_pred = None
    if adaptive:
        def _tight_pred(e, _m=scene_of_entry, _lt=low_trust_scene_nos):
            return _m.get(e.get("index")) in _lt
        tight_pred = _tight_pred
    orig_entries, gate0_stats = apply_source_filter(
        orig_entries, cfg, source_name=fname, tighten=tighten,
        samples_limit=_GATE0_REPORT_SAMPLE_CAP,
        tighten_entry_predicate=tight_pred)
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
        a_entries, validator_warnings, clean_merged, flagged_indexes, clean_stats, \
            structured_warnings = \
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
            except Exception as exc:
                print(f"   ⚠️ 术语冲突扫描失败（忽略）: {exc}")
                collector.add(stage="final", file=fname,
                              reason=f"术语冲突扫描失败: {exc}",
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
            except Exception as exc:
                print(f"   ⚠️ 双引擎分歧采集失败（忽略）: {exc}")
                collector.add(stage="final", file=fname,
                              reason=f"双引擎分歧采集失败: {exc}",
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
            except Exception as exc:
                print(f"   ⚠️ 必看分歧行集合计算失败（忽略）: {exc}")
                collector.add(stage="final", file=fname,
                              reason=f"必看分歧行集合计算失败: {exc}",
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

        # 写前清陈旧（风险清单 md/json + 质量报告导读 json 三件），无条件
        # 执行；必须先于本轮最早的伴生成品写点——本轮要写的导读 json 也
        # 在清理表内，清理若在其后会把新导读同轮误删（D11 HRO-1）。
        removed_stale = _remove_stale_risk_reports(out_dir, stem)
        if removed_stale:
            print(f"🧹 已清理上轮残留风险清单/导读: {', '.join(removed_stale)}")

        # ---- 自动质量报告（cfg.quality_report，落盘到输出目录）----
        if cfg.quality_report:
            try:
                from .quality_report import (
                    build_quality_report,
                    write_divergence_review_csv,
                    write_guide_json,
                    write_quality_report,
                )
                # pass_mode/disag 已在 TM 学习块前采集并共用
                # （两者都关时此处保持 None，与原先 block 内采集等价）
                if pass_mode is None and disag is None:
                    from .pass_disagreement import collect_disagreement, probe_disagreement_mode
                    pass_mode = probe_disagreement_mode(in_path)
                    disag = collect_disagreement(in_path)
                merge_stats: dict = {"premerge_merged": premerge_merged}
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
                guide: dict = {}
                report = build_quality_report(
                    orig_entries, final_entries, Path(in_path).name,
                    expected_entries=orig_entries,
                    merge_stats=merge_stats,
                    validator_warnings=validator_warnings,
                    structured_warnings=structured_warnings,
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
                    tm_learned_count=learned_count,
                    guide_sink=guide)
                rp = write_quality_report(out_dir, stem, report)
                write_guide_json(out_dir, stem, guide)
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


def _wrap_as_result(entries: list, a_result: StageAResult) -> StageAResult:
    """兜底规则可能增删条目，重建 StageAResult 供阶段B消费。"""
    return StageAResult(
        entries=entries,
        deleted=a_result.deleted,
        failed={e["index"] for e in entries
                if e["text"].startswith(UNTRANSLATED_PREFIX)},
        exact_hits=a_result.exact_hits,
    )
