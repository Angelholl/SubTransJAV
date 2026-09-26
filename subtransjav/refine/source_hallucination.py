"""
闸门0——送翻前源侧幻觉检测器（Gate 0: source-side hallucination filter）
====================================================================
在条目送入 LLM 之前（断句预合并之前、对原始条目序列）做一次纯代码层的
源侧幻觉检测，把 ASR 幻觉行（纯标点行、!串、不可发音辅音串、重复循环、
片尾元信息、孤立应答词、无意义音节连缀）在送翻前剔除，省 token 且避免
幻觉行污染翻译与学习链路。

设计约束：
- 不依赖 pipeline_v2（禁止反向 import）；时间轴解析在本模块自实现
  （条目 dict 只有 timing 字符串字段）；
- 与 cloud/local profile 无关，两档均执行（实现上不判断 profile）；
- 档位三档：default（仅明确幻觉删除）/ strict（叠加启发式删除）/
  off（完全不检测）；
- keep_list 白名单优先级最高：命中条目任何档位都不删不计数
  （规范化后整条精确匹配）；
- 保险阀：拦截率（待删/原始）超过 v2_source_filter_valve_pct 时，
  本文件降级为只计数模式（所有类别都不删，只计数 + 显著警告），
  绝不全量放行也不中断任务；
- H5 翻译后回捞（隔离区）：保险阀降级未删成的删五类条目作为候选随
  stats 暴露，管线在 final 组装后按 quarantine_review 复核——译文为
  流畅中文的移入 {stem}_隔离区.srt 供人工复核（只移动不删除）；
- H4b 条目级阈值自适应（tighten_entry_predicate）：谓词只决定删五类
  参数取 tighten 变体还是 base 变体（逐条目选择）；跨条目预计算
  （repeat 连续组 / end_meta 全文件窗口 / 计数类计数）仍在全量条目上
  单趟完成，计数类判定路径完全不感知谓词（永不解锁为删除）；
- resume 场景：阶段A 复用分支会跳过阶段A，闸门0 因此不重跑——
  这是设计内行为，由指纹失效机制兜底（档位/阈值/规则库任一变化
  都会改变 config_hash，旧阶段A产物即被判失效强制重跑）。

规则库：包内 defaults/source_hallucination.yaml（默认），用户目录
config/rules/source_hallucination.yaml 存在则整文件替换（仓库惯例，
不做深层合并）。解析失败或必需键缺失显式报错，不允许静默降级。
"""

import functools
import hashlib
import json
import os
import re

import yaml

from .post_validate import is_untranslated_text

# YAML 必须包含的键路径（点号表示层级），缺失即视为损坏。
# 每个检测类别必须带 example 证据样本（R7：防无据规则膨胀）。
_REQUIRED_KEYS = (
    "schema_version",
    "keep_list",
    "pure_punctuation.example",
    "exclamation.min_run",
    "exclamation.example",
    "unpronounceable.min_len",
    "unpronounceable.example",
    "repeat_loop.min_run",
    "repeat_loop.min_norm_len",
    "repeat_loop.example",
    "end_meta.window_ratio",
    "end_meta.words_ja",
    "end_meta.words_en",
    "end_meta.example",
    "isolated_response.words",
    "isolated_response.strict_min_run",
    "isolated_response.strict_max_ratio",
    "isolated_response.example",
    "nonsense_syllables.min_len",
    "nonsense_syllables.intra_repeat_min",
    "nonsense_syllables.unit_repeat_min",
    "nonsense_syllables.example",
)

_PACKAGE_RULES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "defaults",
    "source_hallucination.yaml")

# 档位与删除语义：default 档仅明确幻觉可删；strict 档全类别可删
# （孤立应答词/无意义音节连缀叠加启发式后才删）。
_DELETE_CATS_DEFAULT = frozenset((
    "exclamation", "pure_punctuation", "unpronounceable",
    "repeat_loop", "end_meta"))

# 计数类（default 档只计数不删除）：永不删除、永不进隔离区
# （白名单保护语义，H5 隔离区候选与 stats 位置核对共用本口径）。
_COUNT_CATS = frozenset(("isolated_response", "nonsense_syllables"))

# 类别键 -> 统计/归档 reason 口径（稳定标签，H3 报告直接消费）
_CATEGORY_LABELS = {
    "exclamation": "!串",
    "pure_punctuation": "纯标点行",
    "unpronounceable": "不可发音辅音串",
    "repeat_loop": "重复循环",
    "end_meta": "片尾元信息",
    "isolated_response": "孤立应答词",
    "nonsense_syllables": "无意义音节连缀",
}

# ---------------------------------------------------------------------------
# 规则库加载（仿 rules_loader.py：用户目录优先、必填键校验、mtime 缓存）
# ---------------------------------------------------------------------------


class SourceRulesLoadError(Exception):
    """source_hallucination.yaml 缺失、损坏或必需键缺失。"""


def _config_rules_path(config_dir: str = None) -> str:
    base = config_dir
    if not base:
        from .config import CONFIG_DIR
        base = CONFIG_DIR
    return os.path.join(base, "rules", "source_hallucination.yaml")


def _resolve_rules_path(config_dir: str = None) -> str:
    """用户目录优先，包内默认回退（整文件替换语义）。"""
    p = _config_rules_path(config_dir)
    if os.path.isfile(p):
        return p
    return _PACKAGE_RULES_PATH


def resolve_source_rules_path(config_dir: str = None) -> str:
    """实际生效的 source_hallucination.yaml 路径（公开别名）。"""
    return _resolve_rules_path(config_dir)


def _nested_get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _validate_rules(rules: dict, path: str):
    for key in _REQUIRED_KEYS:
        v = _nested_get(rules, key)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise SourceRulesLoadError(
                f"{path}: 缺少必需键或值为空: {key}")
    if not isinstance(rules.get("keep_list"), list):
        raise SourceRulesLoadError(f"{path}: keep_list 必须是列表")


@functools.cache
def _load_cached(path: str, mtime: float) -> dict:
    with open(path, encoding="utf-8") as f:
        rules = yaml.safe_load(f)
    if not isinstance(rules, dict):
        raise SourceRulesLoadError(f"{path}: YAML 顶层必须是映射")
    _validate_rules(rules, path)
    return rules


def load_source_rules(config_dir: str = None) -> dict:
    """加载闸门0 规则库（用户目录优先，包内回退；带缓存）。

    返回解析后的语义对象（manifest 指纹对其做 json.dumps+sha1，
    路径/字节差异不影响指纹，只有语义内容变化才失效）。

    Raises
    ------
    SourceRulesLoadError
        YAML 缺失（用户与包内均无）、解析失败或必需键缺失。
    """
    path = _resolve_rules_path(config_dir)
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        raise SourceRulesLoadError(
            f"source_hallucination.yaml 不可访问: {path}: {e}") from e
    try:
        return _load_cached(path, mtime)
    except yaml.YAMLError as e:
        raise SourceRulesLoadError(f"{path}: YAML 解析失败: {e}") from e
    except SourceRulesLoadError:
        raise
    except OSError as e:
        raise SourceRulesLoadError(f"{path}: 读取失败: {e}") from e


# ---------------------------------------------------------------------------
# 基础工具：文本规范化 / 时间轴解析（自实现，不依赖 pipeline_v2）
# ---------------------------------------------------------------------------

# 规范化 = 去空白/标点/符号（保留字母数字与 CJK 文字）
_NON_WORD_RE = re.compile(r"[\W\s_]+", re.UNICODE)
# 含"文字内容"（字母/数字/下划线/CJK）的字符
_HAS_WORD_RE = re.compile(r"\w", re.UNICODE)
# 时间轴：HH:MM:SS,mmm --> HH:MM:SS,mmm（毫秒分隔符兼容 "."）
_TIMING_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
# 仅假名（含长音符・浊点等假名区字符）
_KANA_ONLY_RE = re.compile(r"^[\u3040-\u30ff]+$")
# 整条为单一假名重复（あああ/ううう 等真实台词形态，不触发）
_SINGLE_KANA_REPEAT_RE = re.compile(r"^([\u3040-\u30ff])\1+$")
# !串判定：除感叹/问号等标点与空白外无任何内容
_EXCL_FILLER_RE = re.compile(r"[!！?？.。…,，、;；:：\-—~～\s]+")


def _normalize_text(text: str) -> str:
    """规范化：去空白/标点/符号，保留文字与数字。"""
    return _NON_WORD_RE.sub("", text or "")


def _timing_span(timing: str) -> tuple:
    """解析时间轴为 (start_sec, end_sec)；解析失败返回 (None, None)。"""
    m = _TIMING_RE.match((timing or "").strip())
    if not m:
        return (None, None)
    g = list(map(int, m.groups()))
    start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
    end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
    return (start, end)


# ---------------------------------------------------------------------------
# 类别判定（单条谓词 + 跨条目预计算）
# ---------------------------------------------------------------------------

def _is_exclamation_run(raw: str, min_run: int) -> bool:
    """!串：连续 ! ≥min_run 且除标点/空白外无任何实质内容。"""
    if not raw:
        return False
    if not re.search(rf"[!！]{{{max(2, int(min_run))},}}", raw):
        return False
    return _EXCL_FILLER_RE.fullmatch(raw) is not None


def _is_pure_punctuation(raw: str) -> bool:
    """纯标点行：非空且不含任何文字/数字内容。"""
    return bool(raw) and not _HAS_WORD_RE.search(raw)


def _is_unpronounceable(norm: str, cfg: dict) -> bool:
    """不可发音辅音串：纯 ASCII 字母、无开元音（sss/kk/thhr 类）。

    仅对拉丁字母条目生效（日文假名串不适用——假名皆开元音节）；
    全大写豁免（TV/CD/GPS 等缩略词不是辅音噪音）。
    """
    try:
        min_len = int(cfg.get("min_len", 2))
    except (TypeError, ValueError):
        min_len = 2
    if not norm or len(norm) < max(2, min_len):
        return False
    if not (norm.isascii() and norm.isalpha()):
        return False
    if cfg.get("keep_acronyms", True) and norm.isupper():
        return False
    return not re.search(r"[aeiou]", norm, re.IGNORECASE)


def _detect_repeat_flags(norms: list, min_run: int, min_norm_len: int) -> set:
    """重复循环：相邻同文条目 ≥min_run 连且规范化长度 ≥min_norm_len。

    返回需删除的位置集合。keep_list 命中的位置由主循环提前放行，
    不依赖本函数剔除。
    """
    flags: set[int] = set()
    n = len(norms)
    i = 0
    while i < n:
        v = norms[i]
        if not v or len(v) < min_norm_len:
            i += 1
            continue
        j = i
        while j + 1 < n and norms[j + 1] == v:
            j += 1
        if j - i + 1 >= min_run:
            flags.update(range(i, j + 1))
        i = j + 1
    return flags


def _detect_repeat_flags_adaptive(norms: list, base_cfg: dict,
                                  tight_cfg: dict,
                                  tight_positions: set) -> set:
    """H4b 条目级重复循环判定（连续组就紧原则）。

    "连续组" = 相邻位置规范化文本完全相同的极大段。跨条目组归属规则：
    组内任一条目为低信任（tighten_entry_predicate 命中）→ 整组按
    tighten 参数评估（收紧侧就紧：防低信任条目渗漏进默认组漏删，
    也防默认条目被牵连误放）；纯默认条目组不受影响。删除判定取
    base/tighten 两参数集的并集——防用户收紧块误配出更宽阈值时
    自适应反而漏删（自适应仅收紧铁律）。

    计数类判定路径完全不感知本函数（谓词不参与）。
    """
    min_run_b = int(base_cfg.get("min_run", 4))
    min_len_b = int(base_cfg.get("min_norm_len", 2))
    min_run_t = int(tight_cfg.get("min_run", min_run_b))
    min_len_t = int(tight_cfg.get("min_norm_len", min_len_b))
    floor_len = min(min_len_b, min_len_t)
    flags: set[int] = set()
    n = len(norms)
    i = 0
    while i < n:
        v = norms[i]
        if not v or len(v) < floor_len:
            i += 1
            continue
        j = i
        while j + 1 < n and norms[j + 1] == v:
            j += 1
        run = j - i + 1
        if any(p in tight_positions for p in range(i, j + 1)):
            if (run >= min_run_t and len(v) >= min_len_t) \
                    or (run >= min_run_b and len(v) >= min_len_b):
                flags.update(range(i, j + 1))
        elif run >= min_run_b and len(v) >= min_len_b:
            flags.update(range(i, j + 1))
        i = j + 1
    return flags


def _detect_iso_flags(norms: list, iso_words: set, keep_set: set,
                      min_run: int, max_ratio: float) -> set:
    """孤立应答词 strict 删除标记：同词连续 ≥min_run 或检出占比 >max_ratio。

    仅统计 keep_list 未保护的候选（白名单条目由主循环放行，
    不参与计数与占比，防止口径互相污染）。
    """
    positions = [i for i, v in enumerate(norms)
                 if v and v in iso_words and v not in keep_set]
    flags: set[int] = set()
    if not positions:
        return flags
    i = 0
    while i < len(positions):
        j = i
        while (j + 1 < len(positions)
               and positions[j + 1] == positions[j] + 1
               and norms[positions[j + 1]] == norms[positions[i]]):
            j += 1
        if j - i + 1 >= min_run:
            flags.update(positions[i:j + 1])
        i = j + 1
    if len(positions) and len(positions) / len(norms) > max_ratio:
        flags.update(positions)
    return flags


def _unit_repeat_len(norm: str, unit_min: int, max_unit_len: int):
    """2-max_unit_len 字假名单元整条平铺 ≥unit_min 次时返回单元长，否则 None。"""
    n = len(norm)
    for u in range(2, max_unit_len + 1):
        if n % u:
            continue
        if n // u < unit_min:
            continue
        if norm[:u] * (n // u) == norm:
            return u
    return None


def _is_nonsense(norm: str, cfg: dict) -> bool:
    """无意义音节连缀（检出信号即 strict 删除的叠加启发式，同源）：

    - 形态门槛：规范化后仅假名、长度达标，且不是整条单假名重复
      （あああ/ううう 等长假名重复是本领域真实台词形态，不触发）；
    - 信号 1（条目内重复）：同一假名连续 ≥intra_repeat_min 次；
    - 信号 2（单元重复）：2-4 字假名单元整条平铺 ≥unit_repeat_min 次。
    """
    try:
        min_len = int(cfg.get("min_len", 3))
        intra_min = int(cfg.get("intra_repeat_min", 6))
        unit_min = int(cfg.get("unit_repeat_min", 4))
        max_unit = int(cfg.get("max_unit_len", 4))
    except (TypeError, ValueError):
        min_len, intra_min, unit_min, max_unit = 3, 6, 4, 4
    if not norm or len(norm) < min_len:
        return False
    if not _KANA_ONLY_RE.match(norm):
        return False
    if _SINGLE_KANA_REPEAT_RE.match(norm):
        return False
    if re.search(rf"([\u3040-\u30ff])\1{{{max(2, intra_min) - 1},}}", norm):
        return True
    return _unit_repeat_len(norm, max(2, unit_min), max(2, max_unit)) is not None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _resolve_valve_pct(cfg) -> int:
    """保险阀阈值（1-100）；非法值静默回退默认 50（该链路一贯容错）。"""
    try:
        pct = int(getattr(cfg, "v2_source_filter_valve_pct", 50))
    except (TypeError, ValueError):
        return 50
    return pct if 1 <= pct <= 100 else 50


def _default_errors_dir() -> str:
    """Errors 目录（与 language_validator.filter_stage_output 同口径：
    项目根/Errors，dropped_entries.log 所在地）。"""
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "Errors")


def _archive_dropped(entries, delete_positions, cat_by_pos, errors_dir,
                     source_name):
    """删除条目追加归档到 Errors/dropped_entries.log（静默容错）。"""
    if not delete_positions:
        return
    try:
        from .language_validator import DroppedEntryLog
        log = DroppedEntryLog(errors_dir or _default_errors_dir())
        for pos in sorted(delete_positions):
            e = entries[pos]
            reason = f"闸门0-{_CATEGORY_LABELS[cat_by_pos[pos]]}"
            log.append(source_name, 0, e.get("index"),
                       e.get("text") or "", reason)
    except OSError:
        pass


def apply_source_filter(entries: list, cfg, *, source_name: str = "",
                        config_dir: str = None, rules: dict = None,
                        errors_dir: str = None, tighten: bool = False,
                        samples_limit: int = 0,
                        tighten_entry_predicate=None) -> tuple:
    """闸门0 主入口：对原始条目序列做源侧幻觉检测。

    参数
    ----
    entries : [{"index": int, "timing": str, "text": str}, ...]（预合并前）
    cfg : RefineConfig（读 v2_source_filter / v2_source_filter_valve_pct）
    source_name : 归档日志的"来源"字段（通常为文件名）
    config_dir / rules : 规则库来源（默认走用户目录→包内回退链）
    errors_dir : 删除条目归档目录（默认项目根/Errors）
    tighten : 上游 ASR 信号收紧（H4a）：仅 default 档对删五类应用 YAML
              tighten 覆盖块（strict 本就全删、off 不检测，均不受影响）；
              计数类（孤立应答词/无意义音节连缀）在任何信号下都不得
              解锁为删除——契约由 tests/test_source_hallucination.py 钉住
    samples_limit : >0 时 stats 附带 "samples"（已删条目样本，H3 报告
              消费，上限即本参数，防爆体积）；默认 0 不附（保持既有
              stats 字段契约不变）
    tighten_entry_predicate : 条目级自适应谓词（H4b），
              Callable[[dict], bool] | None——按条目决定删五类参数取
              tighten 变体还是 base 变体。约束（缺省路径零变化）：
              * 谓词只选参数变体，跨条目预计算仍在全量条目上单趟完成；
              * 跨条目组归属就紧原则：repeat_loop 等"连续组"组内任一
                条目谓词为真 → 整组按 tighten 参数评估（收紧侧就紧）；
              * end_meta 窗口仍锚定全文件 span（同一 window 内逐条目选
                参数，禁止分区局部重锚）；
              * 计数类（isolated_response/nonsense_syllables）判定路径
                完全不感知谓词——永不解锁为删除；
              * strict/off 档谓词不生效；None 时行为与本参数加入前
                逐字节一致（positions 恒为全局输入索引、categories/
                total/deleted 为两配置合并统计、阀门按全量 rate 计）。

    返回
    ----
    (kept_entries, stats)：stats 含 mode/total/deleted/detected_total/
    valve_tripped/valve_pct/categories（每类 detected/deleted 计数，
    H3 结构化报告消费；计数类类别本批不归档、仅在 stats 呈现）。

    H5 追加键（只增不改原七键语义）：
    - quarantine_candidates：[{position, category, text}]——保险阀降级时
      "删五类未删成"的条目候选（position 为本次检测输入的原始下标，
      category 为中文标签）；default 正常删除、strict 正常删除与 off 档
      （用户显式关闭即自负其责）恒为空列表。计数类永不入选。
    - count_positions：[int]——计数类（孤立应答词/无意义音节连缀）检出
      条目的原始下标（含白名单保护命中），供阶段A 留空执行率核对。
    - noise_left_empty：int——计数类/候选类条目中被 LLM 留空删除的数量；
      本函数恒记 0，由管线在阶段A 完成后回填（H5-7）。
    """
    mode = str(getattr(cfg, "v2_source_filter", "default") or "default")
    mode = mode.strip().lower()
    if mode not in ("strict", "default", "off"):
        mode = "default"                # 未知档位按 default 兜底
    valve_pct = _resolve_valve_pct(cfg)
    stats: dict = {
        "mode": mode,
        "total": len(entries),
        "deleted": 0,
        "detected_total": 0,
        "valve_tripped": False,
        "valve_pct": valve_pct,
        "categories": {label: {"detected": 0, "deleted": 0}
                       for label in _CATEGORY_LABELS.values()},
        # H5 翻译后回捞：保险阀降级路径的隔离区候选（其余路径恒为空）
        "quarantine_candidates": [],
        # H5-7 阶段A 留空执行率核对：计数类检出位置（含白名单保护命中）
        "count_positions": [],
        # 计数类/候选类条目中被 LLM 留空删除的数量（管线在阶段A 后回填）
        "noise_left_empty": 0,
    }
    if mode == "off" or not entries:
        return list(entries), stats

    if rules is None:
        rules = load_source_rules(config_dir)
    keep_set = {_normalize_text(w) for w in (rules.get("keep_list") or [])
                if _normalize_text(w)}

    # H4a/H4b：tighten 收紧覆盖块（仅 default 档删五类生效；键缺省回退
    # 基础值）。全局 tighten=True（H4a run 信号）→ 全部条目取 tighten
    # 变体；H4b 谓词 → 逐条目选择（预计算一次低信任位置集，谓词异常按
    # 非低信任处理——链路容错惯例）。tighten_entry_predicate=None 且
    # tighten=False 时 tight_positions 恒空：缺省路径零变化。
    adaptive_tighten = mode == "default" and (
        tighten or tighten_entry_predicate is not None)
    tighten_block = (rules.get("tighten") or {}) if adaptive_tighten else {}
    if adaptive_tighten and tighten:
        tight_positions = set(range(len(entries)))
    elif adaptive_tighten and tighten_entry_predicate is not None:
        tight_positions = set()
        for _p, _e in enumerate(entries):
            try:
                if tighten_entry_predicate(_e):
                    tight_positions.add(_p)
            except Exception:           # 谓词异常按非低信任处理（容错惯例）
                continue
    else:
        tight_positions = set()

    def _cat_cfg(name: str, pos: int = None) -> dict:
        """类别参数：base 变体；pos 命中低信任集时叠加 tighten 覆盖块。"""
        base = dict(rules.get(name) or {})
        if pos is not None and pos in tight_positions:
            base.update(tighten_block.get(name) or {})
        return base

    def _tight_cfg(name: str, base: dict) -> dict:
        """tighten 变体（预计算用）：base 叠加该类收紧覆盖块。"""
        merged = dict(base)
        merged.update(tighten_block.get(name) or {})
        return merged

    norms = [_normalize_text(e.get("text")) for e in entries]
    raws = [(e.get("text") or "").strip() for e in entries]

    # 跨条目预计算：重复循环 / 片尾窗口 / 孤立应答词 strict 标记
    # （单趟全量预计算语义保持：低信任只决定参数变体选择，预计算集合
    #   仍对全量条目计算，禁止分区局部预计算）
    cfg_repeat = _cat_cfg("repeat_loop")
    cfg_repeat_tight = (_tight_cfg("repeat_loop", cfg_repeat)
                        if tight_positions else cfg_repeat)
    if tight_positions:
        repeat_flags = _detect_repeat_flags_adaptive(
            norms, cfg_repeat, cfg_repeat_tight, tight_positions)
    else:
        repeat_flags = _detect_repeat_flags(
            norms, int(cfg_repeat.get("min_run", 4)),
            int(cfg_repeat.get("min_norm_len", 2)))
    starts = [_timing_span(e.get("timing"))[0] for e in entries]
    cfg_meta = _cat_cfg("end_meta")
    cfg_meta_tight = (_tight_cfg("end_meta", cfg_meta)
                      if tight_positions else cfg_meta)
    words_ja = [w for w in (cfg_meta.get("words_ja") or []) if w]
    words_en = [str(w).lower() for w in (cfg_meta.get("words_en") or []) if w]
    min_start = min((s for s in starts if s is not None), default=None)
    max_end = max((en for en in (_timing_span(e.get("timing"))[1]
                                 for e in entries) if en is not None),
                  default=None)
    window_start = None
    if min_start is not None and max_end is not None \
            and max_end > min_start:
        window_start = min_start + (max_end - min_start) * \
            (1.0 - float(cfg_meta.get("window_ratio", 0.1)))
    # H4b：tighten 窗口（0.1→0.2 等）——仍锚定全文件 span（同一 window 内
    # 逐条目选参数，禁止按低信任分区局部重锚）
    window_start_tight = None
    if tight_positions and min_start is not None and max_end is not None \
            and max_end > min_start:
        window_start_tight = min_start + (max_end - min_start) * \
            (1.0 - float(cfg_meta_tight.get(
                "window_ratio", cfg_meta.get("window_ratio", 0.1))))
    cfg_iso = rules.get("isolated_response") or {}
    iso_words = {(str(w) or "").strip()
                 for w in (cfg_iso.get("words") or []) if str(w).strip()}
    iso_flags = _detect_iso_flags(
        norms, iso_words, keep_set,
        int(cfg_iso.get("strict_min_run", 4)),
        float(cfg_iso.get("strict_max_ratio", 0.5)))
    cfg_nonsense = rules.get("nonsense_syllables") or {}

    # 逐条判定（keep_list 最高优先级：白名单词任何档位不删除；
    # 计数类命中仅计入检出统计——H3 报告/摘要可见性，
    # D2026-0914-01 裁决点1 附加条件"不得静默"）
    cat_by_pos = {}
    count_positions = []       # H5-7：计数类检出位置（含白名单保护命中）
    for pos in range(len(entries)):
        norm, raw = norms[pos], raws[pos]
        if norm and norm in keep_set:
            if norm in iso_words:
                stats["categories"][_CATEGORY_LABELS["isolated_response"]][
                    "detected"] += 1
                count_positions.append(pos)
            elif _is_nonsense(norm, cfg_nonsense):
                stats["categories"][_CATEGORY_LABELS["nonsense_syllables"]][
                    "detected"] += 1
                count_positions.append(pos)
            continue
        cat = None
        if _is_exclamation_run(raw, _cat_cfg(
                "exclamation", pos).get("min_run", 2)):
            cat = "exclamation"
        elif _is_pure_punctuation(raw):
            cat = "pure_punctuation"
        elif _is_unpronounceable(norm, _cat_cfg("unpronounceable", pos)):
            cat = "unpronounceable"
        elif pos in repeat_flags:
            cat = "repeat_loop"
        elif (((window_start is not None and starts[pos] is not None
                and starts[pos] >= window_start)
               or (pos in tight_positions and window_start_tight is not None
                   and starts[pos] is not None
                   and starts[pos] >= window_start_tight))
              and _match_meta_words(raw, words_ja, words_en)):
            cat = "end_meta"
        elif norm and norm in iso_words:
            cat = "isolated_response"
        elif _is_nonsense(norm, cfg_nonsense):
            cat = "nonsense_syllables"
        if cat is None:
            continue
        cat_by_pos[pos] = cat
        stats["categories"][_CATEGORY_LABELS[cat]]["detected"] += 1
        if cat in _COUNT_CATS:
            count_positions.append(pos)
    stats["count_positions"] = count_positions

    # 当前档位下的"待删集合"（保险阀判定输入）
    if mode == "default":
        would_delete = {p for p, c in cat_by_pos.items()
                        if c in _DELETE_CATS_DEFAULT}
    else:   # strict
        would_delete = {
            p for p, c in cat_by_pos.items()
            if c != "isolated_response" or p in iso_flags}

    # 保险阀：拦截率超阈值 → 全文件降级为只计数模式（不删，也不放行重跑）
    total = len(entries)
    rate = round(100.0 * len(would_delete) / total) if total else 0
    if len(would_delete) * 100 > valve_pct * total:
        stats["valve_tripped"] = True
        print(f"⚠️ 闸门0 保险阀触发（拦截率 {rate}% > {valve_pct}%），"
              f"本文件降级为只计数模式")
        # H5：降级后未删成的删五类条目成为隔离区候选（会被送翻，LLM 可能
        # 给乱码编出通顺中文）；计数类永不入选（白名单保护语义）
        stats["quarantine_candidates"] = [
            {"position": p, "category": _CATEGORY_LABELS[cat_by_pos[p]],
             "text": entries[p].get("text") or ""}
            for p in sorted(would_delete)
            if cat_by_pos[p] in _DELETE_CATS_DEFAULT]
        would_delete = set()

    stats["deleted"] = len(would_delete)
    stats["detected_total"] = sum(
        v["detected"] for v in stats["categories"].values())
    for p in would_delete:
        stats["categories"][_CATEGORY_LABELS[cat_by_pos[p]]]["deleted"] += 1
    if samples_limit and samples_limit > 0:
        # H3 报告样本：已删条目（编号/类别/原文），按条目序截断防爆体积
        stats["samples"] = [
            {"number": entries[p].get("index"),
             "category": _CATEGORY_LABELS[cat_by_pos[p]],
             "text": entries[p].get("text") or ""}
            for p in sorted(would_delete)][:max(0, int(samples_limit))]

    if would_delete:
        _archive_dropped(entries, would_delete, cat_by_pos, errors_dir,
                         source_name)
        kept = [e for pos, e in enumerate(entries)
                if pos not in would_delete]
    else:
        kept = list(entries)

    if stats["deleted"] or stats["detected_total"]:
        print(f"   🚪 闸门0 源侧幻觉检测（{mode} 档）：删除 {stats['deleted']} 条"
              f" / 检出计数 {stats['detected_total']} 处 / 原始 {total} 条")
    return kept, stats


def _match_meta_words(raw: str, words_ja: list, words_en: list) -> bool:
    """片尾元信息词命中（英语大小写不敏感子串，日语原样子串）。"""
    low = raw.lower()
    return any(w in low for w in words_en) \
        or any(w in raw for w in words_ja)


# ---------------------------------------------------------------------------
# H5 翻译后回捞（隔离区）：源文高置信幻觉 × 译文流畅中文 → 移出主稿
# ---------------------------------------------------------------------------

# 流畅中文判定代理：译文含 ≥2 个汉字（CJK 统一表意文字基本区）
_HANZI_RE = re.compile(r"[\u4e00-\u9fff]")


def is_fluent_zh(text: str, untranslated_prefix: str = "[未翻译]") -> bool:
    """流畅中文判定代理：译文含 ≥2 个汉字且非 [未翻译] 前缀。

    [未翻译] 判定统一走 post_validate.is_untranslated_text（兼容带/
    不带尾空格两形态：生成侧常量带尾空格，提示词模板教的是无空格
    形态）。untranslated_prefix 参数保留向后兼容：旧调用方显式传入
    自定义前缀时仍按其字面值做开头匹配。

    这是"LLM 给源文幻觉编出通顺中文"的廉价代理判定（H5 评议拍板口径）：
    不追求语义真伪，只捕捉"乱码源文却被译成多字中文"的形态异常。
    """
    t = text or ""
    if untranslated_prefix and t.startswith(untranslated_prefix):
        return False
    if is_untranslated_text(t):
        return False
    return len(_HANZI_RE.findall(t)) >= 2


def strong_garble_signal(text: str) -> str | None:
    """D5 乱码强译复核：源文命中强乱码信号时返回信号名，否则 None。

    复用闸门0 计数类"无意义音节连缀"的既有强信号判定（同一假名连打
    ≥intra_repeat_min、2-4 字假名单元整条平铺 ≥unit_repeat_min，与
    _is_nonsense 同源同阈值，规则库同样走用户目录→包内回退链），
    不新造假名比启发式；单假名拖长音豁免（あああ 等真实台词形态）
    与 _is_nonsense 保持一致。
    """
    norm = _normalize_text(text)
    if not norm:
        return None
    cfg = load_source_rules().get("nonsense_syllables") or {}
    if _is_nonsense(norm, cfg):
        return _CATEGORY_LABELS["nonsense_syllables"]
    return None


def is_source_counting_noise(text: str, config_dir: str = None,
                             rules: dict = None) -> bool:
    """源侧计数类噪声判定（v1.2.2 C2，公开纯函数；cleaner 的 L7/L8/L11
    删除闸门消费）。

    复用闸门0 计数类"无意义音节连缀"的两大信号（与 _is_nonsense 同源
    同阈值，规则库走用户目录→包内回退链）：
      1) 条目内重复：同一假名连续 ≥ nonsense_syllables.intra_repeat_min；
      2) 单元重复：2-4 字假名单元整条平铺 ≥ unit_repeat_min。

    与 _is_nonsense 的刻意差异（消费场景不同：本函数服务删除决策侧，
    _is_nonsense 服务闸门0 检测/删除侧）：
    - keep_list 白名单词不算噪声（はい/うん 等是实义应答，白名单保护
      语义一致）；
    - 含汉字文本不算噪声（实义行）；
    - 不做"整条单假名拖长音豁免"：纯假名长连打（あ×6 及以上）即噪声
      证据——闸门0 的豁免（_SINGLE_KANA_REPEAT_RE）只保护其自身的
      检出/删除语义（あああ 等真实台词形态），不外溢到本判定；
    - 证据缺失（空串/纯标点）→ False（保守：无噪声证据不判噪）。

    重复循环（repeat_loop）为跨条目特征，单条文本无法判定，不参与本
    判定；该维度的防误删由 cleaner 自身的 L11 同感官根去重门槛承担。
    """
    norm = _normalize_text(text)
    if not norm:
        return False
    if rules is None:
        rules = load_source_rules(config_dir)
    keep_set = {_normalize_text(w)
                for w in (rules.get("keep_list") or []) if _normalize_text(w)}
    if norm in keep_set:
        return False
    if _HANZI_RE.search(norm):
        return False
    cfg = rules.get("nonsense_syllables") or {}
    try:
        intra_min = max(2, int(cfg.get("intra_repeat_min", 6)))
        unit_min = max(2, int(cfg.get("unit_repeat_min", 4)))
        max_unit = max(2, int(cfg.get("max_unit_len", 4)))
    except (TypeError, ValueError):
        intra_min, unit_min, max_unit = 6, 4, 4
    if re.search(rf"([\u3040-\u30ff])\1{{{intra_min - 1},}}", norm):
        return True
    return _unit_repeat_len(norm, unit_min, max_unit) is not None


def quarantine_review(final_entries: list, candidates: list,
                      source_lookup: dict,
                      untranslated_prefix: str = "[未翻译]") -> tuple:
    """H5 翻译后回捞：把"源文高置信幻觉 + 译文流畅中文"的条目移入隔离区。

    场景：闸门0 保险阀降级时（--source-filter off 档不做候选——用户显式
    关闭即自负其责），删五类高置信幻觉条目会送入 LLM，LLM 常给乱码编出
    通顺中文（第二重幻觉最隐蔽形态）。本函数在 final 组装后按候选清单
    复核终稿，只移动不删除：

    - 候选对齐：candidates 元素为 {position, category, text}（position
      为闸门0 检测输入的原始下标）；source_lookup 提供 position → 条目
      编号（index）映射（由管线在闸门0 前快照上构建），final 条目按
      index 命中候选；
    - 回捞判定：命中条目译文为"流畅中文"（≥2 个汉字且非 [未翻译] 前缀，
      见 is_fluent_zh）且非日文原文回退条目（_keep_original 标记）→
      移入隔离区；否则保留主稿（缺编号映射的候选同样保守放行）。

    返回 (main_entries, quarantine_entries)，各自保持原相对顺序。
    """
    want = {}
    for c in candidates or []:
        idx = (source_lookup or {}).get(c.get("position"))
        if idx is not None:
            want[idx] = c
    main, quarantine = [], []
    for e in final_entries:
        if (e.get("index") in want
                and not e.get("_keep_original")
                and is_fluent_zh(e.get("text"), untranslated_prefix)):
            quarantine.append(e)
        else:
            main.append(e)
    return main, quarantine


def gate0_rules_sha1(rules: dict = None) -> str:
    """规则库语义内容 sha1（json.dumps(sort_keys=True) 后哈希）。

    manifest 指纹消费：用户覆盖文件与包内默认谁生效就用谁，
    路径/键序/字节差异不影响指纹，语义内容变化才使旧产物失效。
    """
    if rules is None:
        rules = load_source_rules()
    payload = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
