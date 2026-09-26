"""
上游 ASR 运行信号通道（H4a 薄版）
=================================
读取上游 WhisperJAV 运行 manifest（whisperjav_run.json），提取转写可信度
信号（run 状态 / 语音覆盖率），供两处消费：

- pipeline 闸门0：run 状态可疑时收紧检测（tighten），覆盖率过低时告警；
- manifest.compute_config_hash：语义指纹（fingerprint），信号有无/内容
  变化使旧产物失效（R1）。

H4b 场景级遥测通道（load_asr_telemetry）：读上游逐场景转写遥测
（raw_subs/<名>.asr_telemetry.jsonl），派生场景低信任信号
（scene_low_trust）供条目级阈值自适应消费；同样纯读取、全容错，
绝不抛异常、绝不阻断翻译管线。

设计约束：
- 纯读取、全容错：文件缺失/不可读/JSON 损坏/字段不识别一律降级为
  "无信号 + 警告"，绝不抛异常、绝不阻断翻译管线；
- 不 import pipeline_v2 / risk（接线由 pipeline 完成，本模块只出信号）；
- 显式 cfg.asr_meta 路径：file 直接用、dir 找其中 whisperjav_run.json，
  视为用户有意识提供的信号，不做新鲜度检查；自动发现（未配置时在 SRT
  同目录找 whisperjav_run.json）要求文件存在且新鲜（R6）；
- 指纹只由识别出的语义字段（status/mileage_pct）组成——路径与未识别
  字段不参与，防"路径变化误失效"（R1 语义）。
"""

import hashlib
import json
import math
import os

from .config import DEFAULT_V2_ASR_META_STALE_MAX_HOURS

# 上游运行 manifest 的旁车文件名（SRT 同目录自动发现）
RUN_META_NAME = "whisperjav_run.json"

# status 候选键（按序取首个可识别值，未识别字段一律忽略）
_STATUS_KEYS = ("status", "run_status", "result", "state")
# 覆盖率候选键（按序取首个可识别值）
_COVERAGE_KEYS = ("mileage", "mileage_pct", "coverage", "coverage_pct",
                  "speech_coverage", "speech_ratio")

# 可疑 run 状态集合：命中即收紧闸门0（消费方：pipeline_v2；本模块只透传）
SUSPECT_STATUSES = frozenset(("suspect", "empty", "failed"))

# ---------------------------------------------------------------------------
# H4b：场景级转写遥测（上游 Balanced 模式产出 raw_subs/<名>.asr_telemetry.jsonl，
# 逐行 JSONL dict，实测 schema 见 1.9.3：scene 为 1-based int，字段可为 null）
# ---------------------------------------------------------------------------

# 遥测旁车文件后缀（自动发现：SRT 同目录 raw_subs/ 下 <前缀>.asr_telemetry.jsonl）
ASR_TELEMETRY_SUFFIX = ".asr_telemetry.jsonl"

# SRT 文件名需逐段剥除的语言/管线后缀（发现遥测时定位文件名前缀用）。
# 真实命名：SRT=4k2.me@mihd-002.ja.whisperjav.srt 而遥测=4k2.me@mihd-002.
# asr_telemetry.jsonl——禁止 naive stem（剥 .srt 后直接拿来当匹配前缀必零命中）。
_STEM_STRIP_SUFFIXES = (".ja.whisperjav", ".whisperjav", ".ja")

# 遥测中保留的信号字段（media/scene/elapsed_s/wall_s 为身份与耗时元数据，
# 不参与信任判定，不入 scenes）；字段值为 null → 该信号不可用（刻意
# 不记 0、不判低信任），仅在 dict 中缺省。
_TELEMETRY_SIGNAL_FIELDS = (
    "produced_output", "model_epoch", "rtf", "n_segments",
    "max_temperature", "fallback_segments", "min_avg_logprob",
    "mean_avg_logprob", "max_compression_ratio", "max_no_speech_prob",
    "cuda_used_mb", "cuda_allocated_mb", "cuda_reserved_mb", "rss_mb",
)

# 场景低信任判定常量（实测出处：whisperjav 1.9.3 Balanced 模式 4 份 BAL
# 样本 .abtest/out/BAL__*/raw_subs/*.asr_telemetry.jsonl——硬信号在正常
# 转写中稀有，可单独收紧；max_no_speech_prob 单独命中约 40% 场景
# （静音/音乐段常态），单独使用会大范围误收紧，故与 mean_avg_logprob 合取）。
_TRUST_MAX_COMPRESSION_RATIO = 2.4   # 压缩比硬上限（>此值多为循环/幻听）
_TRUST_NO_SPEECH_PROB = 0.6          # 无语音概率软信号阈值
_TRUST_MIN_MEAN_LOGPROB = -1.0       # 平均 logprob 软信号阈值（越负越不可信）


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _empty_meta() -> dict:
    """无信号元数据（present=False 为常态：上游未产出旁车文件）。"""
    return {"present": False, "status": None, "mileage_pct": None,
            "stale": False, "file": None, "warnings": []}


def _stale_max_hours(cfg) -> float:
    """manifest 新鲜度上限（小时）；非法值静默回退默认 24（该链路一贯容错）。"""
    try:
        hours = float(getattr(cfg, "v2_asr_meta_stale_max_hours",
                              DEFAULT_V2_ASR_META_STALE_MAX_HOURS))
    except (TypeError, ValueError):
        return float(DEFAULT_V2_ASR_META_STALE_MAX_HOURS)
    return hours if hours >= 0 else float(DEFAULT_V2_ASR_META_STALE_MAX_HOURS)


def _is_fresh(meta_path: str, srt_path: str, cfg) -> bool:
    """新鲜度（R6）：manifest mtime 比 SRT mtime 旧不超过阈值小时数。

    manifest 比 SRT 新（age≤0）视为新鲜；SRT mtime 不可得时放行
    （无从比较，宁给信号不误杀）。
    """
    try:
        meta_mtime = os.path.getmtime(meta_path)
    except OSError:
        return False                # meta 自身 mtime 不可得：按过期处理（警告可见）
    try:
        srt_mtime = os.path.getmtime(srt_path)
    except OSError:
        return True                 # SRT mtime 不可得：放行（宁给信号不误杀）
    max_age_s = _stale_max_hours(cfg) * 3600.0
    return (srt_mtime - meta_mtime) <= max_age_s


def _parse_status(data: dict) -> str | None:
    """候选键匹配提取 run 状态（转小写字符串；非标量/空值跳过）。"""
    for key in _STATUS_KEYS:
        if key not in data:
            continue
        v = data[key]
        if isinstance(v, str):
            s = v.strip().lower()
            if s:
                return s
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v).lower()
    return None


def _parse_coverage(data: dict) -> float | None:
    """候选键匹配提取覆盖率（float；0-1 视为比例 ×100，>1 视为百分比）。

    负值/NaN/inf/不可转换的类型漂移值一律忽略（继续尝试下一候选键）；
    结果定点化到 4 位小数——0.55×100 的浮点噪声（55.000…01）与直写的
    55 必须产生同一语义值，否则指纹会把同义信号误判为变化（R1）。
    """
    for key in _COVERAGE_KEYS:
        if key not in data:
            continue
        v = data[key]
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f) or f < 0:
            continue
        if f <= 1:
            return round(f * 100.0, 4)
        return round(f, 4)
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def load_asr_meta(cfg, srt_path: str) -> dict:
    """加载上游 ASR 运行信号（全容错，绝不阻断管线）。

    参数
    ----
    cfg : RefineConfig（读 asr_meta / v2_asr_meta_stale_max_hours）
    srt_path : 当前输入 SRT 路径（自动发现 whisperjav_run.json 的锚点；
               为空且未显式配置时直接返回无信号，如 config 指纹场景）

    返回
    ----
    {"present": bool, "status": str|None, "mileage_pct": float|None,
     "stale": bool, "file": basename|None, "warnings": [str]}
    """
    meta = _empty_meta()
    data = None
    try:
        explicit = str(getattr(cfg, "asr_meta", "") or "").strip()
        if explicit:
            path = explicit
            if os.path.isdir(path):
                path = os.path.join(path, RUN_META_NAME)
            if not os.path.isfile(path):
                meta["warnings"].append(f"显式上游 manifest 不存在: {explicit}")
                return meta
            meta["file"] = os.path.basename(path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            if not srt_path:
                return meta        # 无 SRT 上下文（如指纹计算）：不自动发现
            srt_dir = os.path.dirname(os.path.abspath(srt_path))
            path = os.path.join(srt_dir, RUN_META_NAME)
            if not os.path.isfile(path):
                return meta        # 无旁车文件：常态无信号，不警告
            if not _is_fresh(path, srt_path, cfg):
                meta["stale"] = True
                meta["file"] = os.path.basename(path)
                meta["warnings"].append(
                    f"上游 manifest 已超龄（比 SRT 旧超过 "
                    f"{_stale_max_hours(cfg):g} 小时），信号弃用: {RUN_META_NAME}")
                return meta
            meta["file"] = os.path.basename(path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, ValueError) as e:
        # 文件不可读 / JSON 损坏 / 顶层非对象：无信号 + 警告，绝不阻断
        meta["present"] = False
        meta["warnings"].append(f"上游 manifest 读取失败（忽略）: {e}")
        return meta
    if not isinstance(data, dict):
        meta["warnings"].append("上游 manifest 顶层不是 JSON 对象（忽略）")
        return meta
    meta["present"] = True
    meta["status"] = _parse_status(data)
    meta["mileage_pct"] = _parse_coverage(data)
    return meta


def fingerprint(meta) -> str | None:
    """上游信号语义指纹：仅由识别出的语义字段（status/mileage_pct）组成
    规范化对象后 sha1——不是原始文件字节 sha1，路径不参与。

    无信号（present=False，或两个语义字段都未识别出）返回 None。
    """
    if not isinstance(meta, dict) or not meta.get("present"):
        return None
    payload: dict = {}
    if meta.get("status") is not None:
        payload["status"] = str(meta["status"])
    if meta.get("mileage_pct") is not None:
        payload["mileage_pct"] = float(meta["mileage_pct"])
    if not payload:
        return None
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# H4b：场景级转写遥测（load_asr_telemetry / scene_low_trust）
# ---------------------------------------------------------------------------

def _num_signal(v) -> float | None:
    """宽容取数：bool/字符串/NaN/inf → None（信号不可用），其余 → float。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _strip_stem_suffixes(stem: str) -> str:
    """剥 SRT 文件名的语言/管线后缀（逐段剥，直至无可剥）。"""
    changed = True
    while changed and stem:
        changed = False
        low = stem.lower()
        for suf in _STEM_STRIP_SUFFIXES:
            if low.endswith(suf) and len(stem) > len(suf):
                stem = stem[: -len(suf)]
                changed = True
                break
    return stem


def _discover_telemetry_path(cfg, srt_path: str, warnings: list) -> str | None:
    """遥测路径解析（副作用仅写 warnings，绝不抛）。

    - 显式 cfg.asr_telemetry：直接用、不做新鲜度（沿 asr_meta 显式路径
      先例：视为用户有意识提供的信号）；
    - 未配置：在 SRT 同目录 raw_subs/ 下前缀匹配——SRT stem 剥语言/管线
      后缀后作为前缀，恰一个命中才用；零或多命中 → None + 警告。
    """
    explicit = str(getattr(cfg, "asr_telemetry", "") or "").strip()
    if explicit:
        if not os.path.isfile(explicit):
            warnings.append(f"显式 asr_telemetry 不存在: {explicit}")
            return None
        return explicit
    if not srt_path:
        return None
    srt_dir = os.path.dirname(os.path.abspath(srt_path))
    stem = _strip_stem_suffixes(
        os.path.splitext(os.path.basename(srt_path))[0])
    raw_dir = os.path.join(srt_dir, "raw_subs")
    hits: list = []
    if os.path.isdir(raw_dir):
        try:
            for name in os.listdir(raw_dir):
                if not name.lower().endswith(ASR_TELEMETRY_SUFFIX):
                    continue
                if name[: -len(ASR_TELEMETRY_SUFFIX)].startswith(stem):
                    hits.append(os.path.join(raw_dir, name))
        except OSError as e:
            warnings.append(f"asr_telemetry 目录扫描失败（忽略）: {e}")
            return None
    if not hits:
        warnings.append(
            f"未发现 asr_telemetry（SRT 同目录 raw_subs/ 下无文件名前缀 "
            f"{stem!r} 匹配的 *{ASR_TELEMETRY_SUFFIX}）")
        return None
    if len(hits) > 1:
        warnings.append(
            "asr_telemetry 前缀匹配到多个文件，弃用: "
            + ", ".join(sorted(os.path.basename(h) for h in hits)))
        return None
    return hits[0]


def _parse_telemetry_line(line: str):
    """解析单行遥测：返回 (scene_no, 信号 dict)；坏行返回 None。

    - scene 非整数 / audio_duration_s 缺失或坏值 → 整行坏行；
    - 字段值为 null → 该信号不可用（不入 dict，不当 0、不判低信任）；
    - 数值字段类型漂移（bool/字符串/NaN/inf）→ 该信号不可用（跳过）。
    """
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    scene = obj.get("scene")
    if isinstance(scene, bool) or not isinstance(scene, int):
        return None
    try:
        dur = float(obj.get("audio_duration_s"))
    except (TypeError, ValueError):
        return None
    if math.isnan(dur) or math.isinf(dur) or dur < 0:
        return None
    scene_data: dict = {"audio_duration_s": dur}
    for key in _TELEMETRY_SIGNAL_FIELDS:
        v = obj.get(key)
        if v is None:
            continue                      # null=信号不可用（刻意不记 0）
        if key == "produced_output":
            if isinstance(v, bool):
                scene_data[key] = v
            continue
        if _num_signal(v) is None:
            continue
        scene_data[key] = v
    return scene, scene_data


def load_asr_telemetry(cfg, srt_path: str) -> dict:
    """加载场景级 ASR 转写遥测（H4b；全容错，任何异常不抛、不阻断管线）。

    参数
    ----
    cfg : RefineConfig（读 asr_telemetry 显式路径 / v2_asr_meta_stale_max_hours）
    srt_path : 当前输入 SRT 路径（自动发现锚点；显式路径时可为空）

    返回
    ----
    {"present": bool, "stale": bool, "file": str|None,
     "scenes": {scene_no: {非 None 信号字段..., "audio_duration_s": float}},
     "skipped_lines": int, "warnings": [str]}

    约束：
    - 显式路径直接用、不做新鲜度；自动发现要求文件新鲜（R6，沿 _is_fresh
      手法）：telemetry mtime 比 SRT mtime 旧超 cfg.v2_asr_meta_stale_max_hours
      → stale=True 且信号弃用；
    - 坏行（非 dict/缺 scene/scene 非整数/坏数值）跳过并计数；重复 scene
      行按坏行计（保留首行）；无场景文本/无时间码——遥测只含逐场景统计。
    """
    result: dict = {"present": False, "stale": False, "file": None,
                    "scenes": {}, "skipped_lines": 0, "warnings": []}
    try:
        path = _discover_telemetry_path(cfg, srt_path, result["warnings"])
        if not path:
            return result
        result["file"] = os.path.basename(path)
        # 仅自动发现做新鲜度检查（显式路径视为有意识提供，沿先例）
        if (not str(getattr(cfg, "asr_telemetry", "") or "").strip()
                and not _is_fresh(path, srt_path, cfg)):
            result["stale"] = True
            result["warnings"].append(
                f"asr_telemetry 已超龄（比 SRT 旧超过 "
                f"{_stale_max_hours(cfg):g} 小时），信号弃用")
            return result
        scenes: dict = {}
        skipped = 0
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                parsed = _parse_telemetry_line(line)
                if parsed is None:
                    skipped += 1
                    continue
                scene_no, scene_data = parsed
                if scene_no in scenes:
                    skipped += 1        # 重复 scene 行：按坏行计（保留首行）
                    continue
                scenes[scene_no] = scene_data
        result["present"] = True
        result["scenes"] = scenes
        result["skipped_lines"] = skipped
    except Exception as e:              # 全容错红线：遥测故障绝不阻断管线
        result["present"] = False
        result["scenes"] = {}
        result["warnings"].append(f"asr_telemetry 读取失败（忽略）: {e}")
    return result


def scene_low_trust(scene: dict) -> bool:
    """场景低信任判定（H4b 信任派生，独立于解析的纯函数；输入非 dict/信号
    缺失一律不判低信任，防御性返回 False）。

    硬信号任一命中即低信任：produced_output 为 False / fallback_segments>0 /
    max_temperature>0 / max_compression_ratio>2.4；
    软信号合取（None 信号不参与、不判低信任）：max_no_speech_prob>0.6
    且 mean_avg_logprob<-1.0。

    常量实测出处见模块级 _TRUST_* 注释（1.9.3 Balanced 模式 4 份 BAL 样本：
    硬信号稀有、nsp 单独命中约 40% 故合取）。

    铁律：自适应仅收紧闸门0 删五类参数（YAML tighten 覆盖块），计数类
    （孤立应答词/无意义音节连缀）永不解锁为删除——契约由
    tests/test_source_hallucination.py 钉住，不得松动。
    """
    if not isinstance(scene, dict):
        return False
    if scene.get("produced_output") is False:
        return True
    fb = _num_signal(scene.get("fallback_segments"))
    if fb is not None and fb > 0:
        return True
    temp = _num_signal(scene.get("max_temperature"))
    if temp is not None and temp > 0:
        return True
    cr = _num_signal(scene.get("max_compression_ratio"))
    if cr is not None and cr > _TRUST_MAX_COMPRESSION_RATIO:
        return True
    nsp = _num_signal(scene.get("max_no_speech_prob"))
    mlp = _num_signal(scene.get("mean_avg_logprob"))
    if nsp is None or mlp is None:
        return False                    # 软信号缺一：不参与合取、不判低信任
    return (nsp > _TRUST_NO_SPEECH_PROB
            and mlp < _TRUST_MIN_MEAN_LOGPROB)
