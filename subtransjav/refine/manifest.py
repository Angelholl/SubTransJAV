"""
任务状态/清单模型（v1.1 P0 前置共用模块，中断恢复用）。

- TaskManifest 记录一次翻译任务的输入指纹、配置指纹、模型信息、
  各阶段状态与产物路径；中断后据此判断已有产物能否安全复用。
- 原子写与 pipeline_v2._atomic_write_text 同款思路：同目录 .tmp + os.replace。
- 仅依赖标准库，保证 GUI 侧可轻量导入。
"""

import contextlib
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# 清单协议版本：字段布局发生不兼容变更时递增
MANIFEST_VERSION = 1

# 阶段状态机：pending -> running -> done / degraded / failed
STAGE_STATUSES = ("pending", "running", "done", "degraded", "failed")

# 阶段键（v2 两阶段流水线：A=净语+翻译，B=审校+抛光，final=终稿）
_STAGE_KEYS = ("A", "B", "final")


def _iso_now() -> str:
    """秒级 ISO 时间戳（与 events/risk 模块保持同款格式）。"""
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class StageRecord:
    """单个阶段的执行状态。"""

    status: str = "pending"
    completed_at: str | None = None
    output: str | None = None          # 产物文件路径
    entries: int = 0                   # 产物条目数
    degraded_count: int = 0            # 降级条目数（保留原文等）


@dataclass
class TaskManifest:
    """一次翻译任务的状态清单（持久化为 {stem}_manifest.json）。"""

    manifest_version: int
    input_path: str
    input_sha1: str
    input_size: int
    config_hash: str
    glossary_sha1: str | None = None
    tm_sha1: str | None = None
    models: dict = field(default_factory=dict)
    # {"A": {"provider","model","endpoint"}, "B": {...}}
    out_dir: str = ""
    stem: str = ""
    outputs: dict = field(default_factory=lambda: {"A": None, "final": None})
    stages: dict = field(default_factory=lambda: {k: StageRecord() for k in _STAGE_KEYS})
    started_at: str = ""
    updated_at: str = ""
    run_pid: int | None = None

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """递归序列化（嵌套 StageRecord 一并转 dict）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d) -> "TaskManifest":
        """从 dict 反序列化；缺失字段回退默认值（容忍旧文件/手工编辑）。"""
        stages_raw = d.get("stages") or {}
        stages = {}
        for key in _STAGE_KEYS:
            raw = stages_raw.get(key)
            stages[key] = StageRecord(**raw) if isinstance(raw, dict) else StageRecord()
        outputs = {"A": None, "final": None}
        outputs.update(d.get("outputs") or {})
        return cls(
            manifest_version=d.get("manifest_version", MANIFEST_VERSION),
            input_path=d.get("input_path", ""),
            input_sha1=d.get("input_sha1", ""),
            input_size=int(d.get("input_size", 0)),
            config_hash=d.get("config_hash", ""),
            glossary_sha1=d.get("glossary_sha1"),
            tm_sha1=d.get("tm_sha1"),
            models=dict(d.get("models") or {}),
            out_dir=d.get("out_dir", ""),
            stem=d.get("stem", ""),
            outputs=outputs,
            stages=stages,
            started_at=d.get("started_at", ""),
            updated_at=d.get("updated_at", ""),
            run_pid=d.get("run_pid"),
        )

    # ------------------------------------------------------------------
    def mark_running(self, stage) -> None:
        """标记阶段开始运行，并刷新 updated_at。"""
        self.stages.setdefault(stage, StageRecord()).status = "running"
        self.updated_at = _iso_now()

    def mark_done(self, stage, output, entries, degraded_count=0) -> None:
        """标记阶段完成（记录产物/条目数/降级数），并刷新 updated_at。"""
        rec = self.stages.setdefault(stage, StageRecord())
        rec.status = "done"
        rec.output = output
        rec.entries = int(entries)
        rec.degraded_count = int(degraded_count)
        rec.completed_at = _iso_now()
        self.updated_at = _iso_now()


# ----------------------------------------------------------------------
# 路径与持久化
# ----------------------------------------------------------------------

def manifest_path(out_dir, stem) -> Path:
    """清单文件路径：{out_dir}/{stem}_manifest.json。"""
    return Path(out_dir) / f"{stem}_manifest.json"


def save_manifest(path, manifest) -> None:
    """原子写清单：同目录 .tmp + os.replace，防止中断留下半截 JSON。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest.to_dict(), ensure_ascii=True, indent=2)
    tmp = p.with_name(p.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def load_manifest(path):
    """读取清单；不存在 / JSON 损坏 / 版本不符 -> None（调用方按"不可复用"处理）。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("manifest_version") != MANIFEST_VERSION:
        return None
    return TaskManifest.from_dict(data)


def delete_resume_artifacts(out_dir, stem) -> list:
    """删除断点续跑相关产物（存在才删），返回实际删除的文件名列表。"""
    removed = []
    names = (manifest_path(out_dir, stem).name, f"{stem}_refine_A.srt",
             # H3：幻觉处置报告 1.2.1 起不再生成（台账由质量报告【处置】
             # 章节承接），保留清理以扫除旧版运行残留
             f"{stem}_幻觉处置报告.json",
             # H5：上一轮的隔离区同属恢复类现场，本轮无存疑译文时不落文件
             f"{stem}_隔离区.srt")
    for name in names:
        p = Path(out_dir) / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(name)
            except OSError:
                pass                    # 被占用等场景：放弃删除，由调用方决策
    return removed


# ----------------------------------------------------------------------
# 指纹计算
# ----------------------------------------------------------------------

def compute_file_sha1(path) -> str:
    """文件内容 sha1（分块读取，避免大文件占内存）。"""
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_dir_hash(path):
    """目录指纹：所有文件按相对路径排序后联合 sha1；目录不存在返回 None。"""
    if not path:
        return None
    root = Path(path)
    if not root.is_dir():
        return None
    files = sorted((p for p in root.rglob("*") if p.is_file()),
                   key=lambda p: p.relative_to(root).as_posix())
    digest = hashlib.sha1()
    for p in files:
        digest.update(p.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(compute_file_sha1(p).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def compute_glossary_sha1(path):
    """术语表内容 sha1；path 为 None 或文件不存在 -> None（跳过检查）。"""
    if not path or not Path(path).is_file():
        return None
    return compute_file_sha1(path)


# v2 管线实际读取的角色卡文件名与阶段槽位——必须与加载侧
# pipeline_v2.V2_TEMPLATE_FILES / V2_STAGE_SLOT 保持一致
# （tests/test_manifest_model.py 有契约测试钉住，防止两处漂移）。
_V2_TEMPLATE_FILES = {"A": "角色-净语翻译.txt", "B": "角色-审校抛光.txt"}
_V2_STAGE_SLOTS = (("A", 0), ("B", 2))
# 加载侧（pipeline_v2._load_v2_instruction）把这些占位目录解析为默认模板目录
_TEMPLATE_DIR_PLACEHOLDERS = (".", "./", "..")


def _resolve_templates_dir(cfg):
    """解析角色卡目录，规则与加载侧一致：空/"."/"./"/".." → default_templates_dir()。

    CLI 默认 templates_dir="."（= 进程 CWD）：指纹若按字面值做整树哈希，
    上一轮运行写出的 Temp/Logs 文件就会改变指纹，导致同配置 --resume
    也被判"配置已变化"（v1.1.0-beta D2 根因），且全树扫描耗时数秒。
    """
    td = getattr(cfg, "templates_dir", None)
    if not td or td in _TEMPLATE_DIR_PLACEHOLDERS:
        from .config import default_templates_dir
        td = default_templates_dir()
    return td or None


def _rules_yaml_path():
    """translation_rules.yaml 实际生效路径（用户目录优先，包内回退）。

    该文件经 hardened_suffix/retry_cleaning 拼入阶段B指令，属"改了产物
    必失效"的指令源；解析不可得（依赖缺失等）时返回 None 跳过。
    """
    try:
        from .rules_loader import resolve_rules_path
        p = resolve_rules_path()
    except Exception:
        return None
    return p if p and os.path.isfile(p) else None


def _gate0_rules_sha1():
    """闸门0 规则库语义内容 sha1（实际生效的那份：用户覆盖优先，包内回退）。

    对解析后的规则对象做 json.dumps(sort_keys=True, ensure_ascii=False)
    后哈希——不是原始文件字节 sha1，防路径/键序/非语义差异导致误失效；
    keep_list 与 schema_version 在该对象内天然被覆盖。解析不可得时
    返回 None 跳过（与 _rules_yaml_path 同款容错）。
    """
    try:
        from .source_hallucination import gate0_rules_sha1
        return gate0_rules_sha1()
    except Exception:
        return None


def _asr_meta_sha1(cfg):
    """上游 ASR 运行信号语义指纹（H4a/R1）：asr_meta.fingerprint 的结果。

    - cfg.asr_meta 路径刻意不进 _CONFIG_FIELDS：路径变化不应误失效；
      信号有无/内容变化经指纹自然使旧产物失效（这正是 R1 要的语义）。
    - 解析不可得/无信号返回 None 跳过（与 _gate0_rules_sha1 同款容错）。
    """
    try:
        from .asr_meta import fingerprint, load_asr_meta
        return fingerprint(load_asr_meta(cfg, ""))
    except Exception:
        return None


def _v2_stage_prompts_sha1():
    """v2 内置阶段提示词（pipeline_v2.V2_STAGE_PROMPTS）语义指纹（D1）。

    V2_STAGE_PROMPTS 与角色卡共同决定模型行为，却不落任何文件——不纳入
    指纹的话，改提示词后 --resume 会复用旧提示词产出的阶段产物。按阶段
    tag 排序后 json 序列化再 sha1，保证跨进程确定性。延迟导入规避模块级
    循环依赖（pipeline_v2 -> manifest）；导入不可得时返回 None 跳过
    （与 _gate0_rules_sha1 同款容错）。
    """
    try:
        from .pipeline_v2 import V2_STAGE_PROMPTS
        payload = {tag: V2_STAGE_PROMPTS[tag]
                   for tag in sorted(V2_STAGE_PROMPTS)}
        text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha1(text.encode("utf-8")).hexdigest()
    except Exception:
        return None


def instruction_source_files(cfg) -> list:
    """收集管线实际会读取的指令源文件（存在才收录），对齐加载侧
    pipeline_v2._load_v2_instruction 的读取集合：

    - 阶段A/B 角色卡：stage.instructions 指向存在的文件时用之，
      否则用 templates_dir 下的默认角色卡（_V2_TEMPLATE_FILES）；
    - translation_rules.yaml（提示词加固段等的数据源）。
    """
    files = []
    stages = getattr(cfg, "stages", None) or []
    td = _resolve_templates_dir(cfg)
    for tag, slot in _V2_STAGE_SLOTS:
        stage = stages[slot] if slot < len(stages) else None
        explicit = (getattr(stage, "instructions", "") or "") \
            if stage is not None else ""
        if explicit and os.path.isfile(explicit):
            files.append(explicit)          # 与 _read_v2_card 同判定：显式文件优先
            continue
        if td:
            p = os.path.join(td, _V2_TEMPLATE_FILES[tag])
            if os.path.isfile(p):
                files.append(p)
    rules = _rules_yaml_path()
    if rules:
        files.append(rules)
    return files


# 影响"复用产物是否仍有效"的配置字段（getattr 缺失记 None）。
# 刻意排除：API key 类、inputs、output_dir、force/resume/event_format 等运行开关——
# 它们不影响产物内容，不应导致已有产物被判为失效。
_CONFIG_FIELDS = (
    "profile",
    "endpoints",
    "batch_local",
    "batch_cloud",
    "batch_size_stable",
    "v2_profile",
    "v2_concurrency",
    "v2_ctx_local",
    "v2_keep_untranslated",
    # 闸门0：档位与保险阀阈值都直接影响送翻条目集合，必须参与指纹
    "v2_source_filter",
    "v2_source_filter_valve_pct",
    "premerge_enabled",
    # P1-5：影响产物内容的收口字段（温度/预合并阈值变化须使旧 manifest 失效）。
    # 刻意不加入：timeout_llm/timeout_http/timeout_probe/v2_concurrency_max——
    # 仅影响执行时长与并发度，不影响产物内容。
    "temperature_cloud",
    "temperature_local",
    "premerge_max_gap_s",
    "premerge_max_items",
    # 预合并硬上限（RC3）：跨度/字符/碎片阈值直接决定合并结果，
    # 影响产物内容，必须参与指纹。
    "premerge_max_span_ms",
    "premerge_max_chars",
    "premerge_min_fragment_chars",
    "tm_enabled",
    "tm_threshold",
    "tm_fuzzy_inject",
    "tm_fuzzy_threshold",
    "tm_learn_gate",
    "apply_glossary_stage1",
    "apply_glossary_stage2",
    # v1.2.2 D1/D3：影响学习行为的开关——conflict_block 决定冲突条目
    # 能否入库，learn_enabled 决定 learned 词库学习路径是否执行；
    # 两者都改变后续产物内容，必须参与指纹
    "glossary_conflict_block",
    "glossary_learn_enabled",
    # v1.3.0 D2 终选（D2026-0925-01 补充裁决）：最高优先覆盖词表路径
    # 改变送入提示词的词条集合，必须参与指纹（空串=不启用，合法）
    "glossary_override_path",
    # v1.2.2 C1：per-片语境 sidecar 注入开关直接影响 A/B 提示词内容，
    # 必须参与指纹（sidecar 文件内容本身暂不参与指纹：同片修改 sidecar
    # 后复用旧阶段产物属已知边界，用法上以 --force 重跑兜底）
    "context_sidecar",
    # v1.2.2 Beta：剧情自摘要开关与采样预算直接影响 A/B 提示词内容，
    # 必须参与指纹（摘要文本与缓存本身不参与指纹：与 sidecar 内容同款
    # 已知边界，换 --s1-model 或改采样行为后须 --force 重跑兜底）
    "auto_synopsis",
    "synopsis_max_chars",
    "fallback_local",
    "fallback_model",
)

# 每个阶段参与指纹的字段
_STAGE_FIELDS = ("index", "enabled", "provider", "model", "instructions",
                 "endpoint")


def compute_config_hash(cfg) -> str:
    """计算配置指纹：影响产物有效性的字段联合 sha1。

    - 对 cfg 用 getattr 取值，缺失字段记 None（容忍简易 cfg 对象）。
    - stages 逐个提取 provider/model/instructions/index/enabled/endpoint。
    - 指令源按"管线实际加载的文件内容"参与（角色卡 + translation_rules.yaml，
      见 instruction_source_files）；内置阶段提示词（V2_STAGE_PROMPTS）不落
      文件，按其合成内容单独纳入（_v2_stage_prompts_sha1，D1）；不对
      templates_dir 做整树哈希——CLI
      默认 "." 指向进程 CWD，整树哈希会把上一轮运行写出的 Temp/Logs 文件
      算进指纹，导致同配置 --resume 必拒绝复用且单次计算耗时数秒（D2）。
      同配置跨运行指纹必须稳定；旧 manifest 指纹失配可接受（force-resume
      或重跑一次即可），不做迁移。
    """
    payload = {name: getattr(cfg, name, None) for name in _CONFIG_FIELDS}
    files = instruction_source_files(cfg)
    payload["instruction_files_sha1"] = (
        hashlib.sha1(
            "".join(compute_file_sha1(p) + "\n" for p in files).encode("ascii")
        ).hexdigest() if files else None)
    payload["cleaner_config_dir_hash"] = compute_dir_hash(getattr(cfg, "cleaner_config_dir", None))
    payload["gate0_rules_sha1"] = _gate0_rules_sha1()
    payload["asr_meta_sha1"] = _asr_meta_sha1(cfg)
    payload["v2_stage_prompts_sha1"] = _v2_stage_prompts_sha1()
    payload["stages"] = [
        {name: getattr(s, name, None) for name in _STAGE_FIELDS}
        for s in (getattr(cfg, "stages", None) or [])
    ]
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def validate_manifest(manifest, *, input_sha1, config_hash, glossary_sha1=None, tm_sha1=None) -> list:
    """校验旧清单对应的产物能否复用；返回中文不匹配原因列表（空列表=可复用）。

    - glossary_sha1 / tm_sha1 为 None 时跳过对应检查（本次任务未启用该资源）。
    """
    reasons = []
    if manifest.input_sha1 != input_sha1:
        reasons.append("输入文件已变化")
    if manifest.config_hash != config_hash:
        reasons.append("配置已变化")
    if glossary_sha1 is not None and manifest.glossary_sha1 != glossary_sha1:
        reasons.append("术语表已变化")
    if tm_sha1 is not None and manifest.tm_sha1 != tm_sha1:
        reasons.append("翻译记忆库已变化")
    return reasons
