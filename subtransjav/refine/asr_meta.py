"""
上游 ASR 运行信号通道（H4a 薄版）
=================================
读取上游 WhisperJAV 运行 manifest（whisperjav_run.json），提取转写可信度
信号（run 状态 / 语音覆盖率），供两处消费：

- pipeline 闸门0：run 状态可疑时收紧检测（tighten），覆盖率过低时告警；
- manifest.compute_config_hash：语义指纹（fingerprint），信号有无/内容
  变化使旧产物失效（R1）。

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
