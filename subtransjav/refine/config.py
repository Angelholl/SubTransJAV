"""
Refine 配置：阶段定义 / 服务商预设 / 默认参数
"""

import json
import os
from dataclasses import dataclass, field

# ---- 批量默认值（需求定稿：本地 30 / 云端 30）----
DEFAULT_BATCH_LOCAL = 30
DEFAULT_BATCH_CLOUD = 30

# 本地模型单批硬上限（防止超出小模型能力）
LOCAL_BATCH_HARD_CAP = 50

# ---------------------------------------------------------------------------
# P1-5 配置单一来源：散点参数收口（默认值 = 历史硬编码现值，行为不变）
# ---------------------------------------------------------------------------
DEEPSEEK_BASE_DEFAULT = "https://api.deepseek.com/v1"

DEFAULT_TEMPERATURE_CLOUD = 0.5      # 云端服务商采样温度
DEFAULT_TEMPERATURE_LOCAL = 0.1      # 本地服务商采样温度（实测最优低温）
DEFAULT_PREMERGE_MAX_GAP_S = 8.0     # 断句预合并：gap 阈值占位（跨度上限职责已移交 premerge_max_span_ms）
DEFAULT_PREMERGE_MAX_ITEMS = 3       # 断句预合并：合并条数上限
DEFAULT_PREMERGE_MAX_SPAN_MS = 5000  # 断句预合并：合并后总时长硬上限，毫秒
DEFAULT_PREMERGE_MAX_CHARS = 80      # 断句预合并：合并后文本字符上限
DEFAULT_PREMERGE_MIN_FRAGMENT_CHARS = 6  # 断句预合并：语义断裂档下一行短碎片阈值，字符
DEFAULT_V2_CONCURRENCY_MAX = 5       # 批间并发钳制上限
DEFAULT_TIMEOUT_LLM = 900.0          # LLM 单批超时（秒；本地慢模型单批可达数分钟）
DEFAULT_TIMEOUT_HTTP = 60.0          # OpenAI 兼容 HTTP 客户端超时（秒）
DEFAULT_TIMEOUT_PROBE = 5.0          # 本地服务探测类 GET 超时（秒）
DEFAULT_HEARTBEAT_STALE_S = 45.0     # GUI 心跳超时阈值（秒；≈2.25×心跳间隔 20s，超时提示"最近活动 Ns 前"）
DEFAULT_DROPPED_LOG_ROTATE_MB = 5    # dropped_entries.log 轮转阈值（MB；超限轮转为 dropped_entries-<n>.log）
DEFAULT_V2_SOURCE_FILTER_VALVE_PCT = 50  # 闸门0 保险阀：拦截率超过该百分比降级只计数（1-100）
DEFAULT_V2_ASR_META_MIN_COVERAGE_PCT = 30  # 上游 ASR 语音覆盖率告警阈值（%，低于即记风险）
DEFAULT_V2_ASR_META_STALE_MAX_HOURS = 24   # 上游运行 manifest 新鲜度上限（小时，R6）

# 用户可调字段（config/user_settings.json / 环境变量 SUBTRANSJAV_<大写字段名>）。
# 优先级：默认 < 用户配置文件 < 环境变量 < CLI/GUI 显式赋值（构造后赋值天然最高）。
TUNABLE_FIELD_TYPES = {
    "temperature_cloud": float,
    "temperature_local": float,
    "premerge_max_gap_s": float,
    "premerge_max_items": int,
    "premerge_max_span_ms": int,
    "premerge_max_chars": int,
    "premerge_min_fragment_chars": int,
    "v2_concurrency_max": int,
    "timeout_llm": float,
    "timeout_http": float,
    "timeout_probe": float,
    "heartbeat_stale_s": float,
    "dropped_log_rotate_mb": int,
    "v2_source_filter_valve_pct": int,
    "v2_asr_meta_min_coverage_pct": int,
    "v2_asr_meta_stale_max_hours": int,
    "context_sidecar": bool,
    # v1.2.2 Beta 剧情自摘要：开关（bool 白名单字面量收敛）与采样字符预算
    "auto_synopsis": bool,
    "synopsis_max_chars": int,
}

# ---- 服务商预设 ----
# deepseek 走原生通道；其余统一以 provider='custom' + endpoint 调用
PROVIDER_TEXT = {
    "deepseek": "DeepSeek API",
    "zen": "Zen 免费",
    "lmstudio": "本地 LM Studio",
    "ollama": "本地 Ollama",
    "siliconflow": "硅基流动",
    "custom": "自定义兼容接口",
}

PROVIDER_ENDPOINT_DEFAULTS = {
    "zen": "https://opencode.ai/zen/v1",
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "custom": "",  # 必须由用户指定
}

# 各服务商默认模型（可被每阶段配置覆盖）
PROVIDER_MODEL_DEFAULTS = {
    "deepseek": "deepseek-v4-flash",
    "zen": "x-preview-f-free",
    "lmstudio": "",
    "siliconflow": "",
    "custom": "",
}

# Zen 实测免费名单（2026-08；官方轮换，以测试为准）
ZEN_FREE_MODELS = [
    "x-preview-f-free",
    "hy3-free",
    "mimo-v2.5-free",
    "nemotron-3-ultra-free",
    "deepseek-v4-flash-free",   # 新上线，上游偶发宕机
]

# 槽位名（v2 语义：槽0=阶段A 净语+翻译，槽2=阶段B 审校+抛光；槽1/3 为 v2 未用占位槽）
STAGE_NAMES = ["阶段A 净语+翻译(s1槽位)", "（v2 未用）", "阶段B 审校+抛光(s3槽位)", "（v2 未用）"]

# 旧工作目录（角色卡与词库的历史所在地），作为 GUI 默认值
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")
LEGACY_WORKDIR = CONFIG_DIR

# 运行日志目录（项目根目录/Logs）
LOGS_DIR = os.path.abspath(os.path.join(CONFIG_DIR, "..", "Logs"))

# 流水线临时工作区（项目根目录/Temp）：各输入文件的工作文件统一收束于此
TEMP_DIR = os.path.abspath(os.path.join(CONFIG_DIR, "..", "Temp"))


def default_templates_dir() -> str:
    tpl = os.path.join(CONFIG_DIR, "templates")
    return tpl if os.path.isdir(tpl) else LEGACY_WORKDIR


def default_glossary_path() -> str:
    cfg = os.path.join(CONFIG_DIR, "glossary.csv")
    if os.path.isfile(cfg):
        return cfg
    legacy = os.path.join(LEGACY_WORKDIR, "glossary.csv")
    return legacy if os.path.isfile(legacy) else cfg


# ---------------------------------------------------------------------------
# P1-5 配置分层：默认 < 用户配置文件 < 环境变量 < CLI/GUI 显式赋值
# ---------------------------------------------------------------------------

def user_settings_path() -> str:
    """用户配置文件路径：<repo_root>/config/user_settings.json。

    文件格式（浅合并，仅 TUNABLE_FIELD_TYPES 中的字段生效）：
        {"temperature_cloud": 0.3, "timeout_llm": 600}
    """
    return os.path.join(CONFIG_DIR, "user_settings.json")


def _coerce_tunable(raw, typ):
    """按字段类型收敛配置值；类型非法返回 None（调用方警告后忽略）。"""
    if typ is bool:
        # bool 不走 bool(raw)（bool("false")/bool("0") 均为 True，会误开
        # 开关）：仅接受真 bool 与白名单字面量，其余视为非法忽略。
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            v = raw.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        return None
    try:
        return typ(raw)
    except (TypeError, ValueError):
        return None


def load_user_settings() -> dict:
    """读取用户配置文件（不存在则返回 {}，零开销常态）。

    - 非法 JSON / 顶层非对象 -> print 警告并忽略（不 crash）；
    - 未知字段直接忽略；值类型非法的字段在覆盖时再按类型收敛并忽略。
    """
    path = user_settings_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"⚠️ 用户配置文件解析失败，已忽略: {path} ({e})")
        return {}
    if not isinstance(data, dict):
        print(f"⚠️ 用户配置文件顶层应为 JSON 对象，已忽略: {path}")
        return {}
    return {k: v for k, v in data.items() if k in TUNABLE_FIELD_TYPES}


def resolve_tunable(name: str):
    """解析单个可调字段的生效值：默认 < 用户配置文件 < 环境变量。

    供无 cfg 上下文的调用点（如 GUI 并发钳制）使用；CLI/GUI 层在
    拿到配置对象后自行赋值，天然位于本函数之后的最高优先级。
    """
    typ = TUNABLE_FIELD_TYPES.get(name)
    if typ is None:
        raise KeyError(f"未知可调配置字段: {name}")
    value = RefineConfig.__dataclass_fields__[name].default
    user = load_user_settings()
    if name in user:
        coerced = _coerce_tunable(user[name], typ)
        if coerced is not None:
            value = coerced
        else:
            print(f"⚠️ 用户配置字段 {name} 类型非法，已忽略: {user[name]!r}")
    env_raw = os.environ.get(f"SUBTRANSJAV_{name.upper()}")
    if env_raw is not None and env_raw.strip() != "":
        coerced = _coerce_tunable(env_raw.strip(), typ)
        if coerced is not None:
            value = coerced
        else:
            print(f"⚠️ 环境变量 SUBTRANSJAV_{name.upper()} 类型非法，已忽略: "
                  f"{env_raw!r}")
    return value


@dataclass
class StageConfig:
    index: int                      # 0..3
    enabled: bool = True
    provider: str = "local"         # deepseek / zen / lmstudio / custom
    model: str = ""                 # 空则用 PROVIDER_MODEL_DEFAULTS
    instructions: str = ""          # 指令模板文件路径（空则在 templates_dir 找默认名）
    endpoint: str = ""              # 覆盖 PROVIDER_ENDPOINT_DEFAULTS

    @property
    def name(self) -> str:
        return STAGE_NAMES[self.index]


@dataclass
class RefineConfig:
    inputs: list = field(default_factory=list)   # 输入 SRT 路径列表
    output_dir: str = ""            # 空 = 与各输入同目录
    stages: list = field(default_factory=lambda: [
        StageConfig(0, True, "lmstudio", ""),
        StageConfig(1, True, "deepseek", ""),
        StageConfig(2, True, "lmstudio", ""),
        StageConfig(3, True, "lmstudio", ""),
    ])
    templates_dir: str = ""         # 角色卡所在目录（默认 = 输入文件目录）
    batch_local: int = DEFAULT_BATCH_LOCAL
    batch_cloud: int = DEFAULT_BATCH_CLOUD
    glossary_path: str = ""         # 空 = 不使用词库
    apply_glossary_stage1: bool = True
    apply_glossary_stage2: bool = True
    # API Keys（空则依次查 环境变量 → DPAPI 密钥库）
    api_key_deepseek: str = ""
    api_key_zen: str = ""
    api_key_siliconflow: str = ""
    api_key_custom: str = ""
    endpoints: dict = field(default_factory=dict)   # provider -> endpoint 覆盖
    verbose: bool = False
    # 云端故障本地接管（仅作用于云端阶段；本地→云端方向默认关闭）
    fallback_local: bool = False
    fallback_model: str = ""        # 接管用的本地模型名（LM Studio）
    # 翻译记忆库 (TM)
    tm_enabled: bool = True         # 是否启用翻译记忆库
    tm_db_path: str = ""            # TM 数据库路径（空=默认路径）
    tm_threshold: float = 0.85      # 模糊匹配阈值
    tm_fuzzy_inject: bool = True    # v2: TM 模糊命中注入参考译文（高阈值、不替代、不入库）
    tm_fuzzy_threshold: float = 0.98  # 模糊注入相似度下限（0-1，高阈值防照抄错误译文）
    quality_report: bool = True     # v2: 每次任务结束自动生成质量报告（{stem}_质量报告.txt）
    premerge_enabled: bool = True   # v2: 断句预合并（修复 ASR 错误切割，代码层确定性步骤）
    # 缓存优化
    batch_size_stable: bool = True   # 固定批次大小（提高缓存命中率）
    # 自动词库学习
    auto_glossary: bool = False     # S1 完成后自动提取术语
    # v1.2.2 D3 learned 自学习治理开关：False 时跳过 glossary_learned
    # 学习路径（跳过计数入日志）。影响学习行为，须入 manifest 指纹。
    glossary_learn_enabled: bool = True
    # v1.2.2 D1 术语冲突观察闸：False=仅观察（默认，冲突只落 CSV/JSON
    # 与报告小节）；True=冲突条目禁止进入 TM 学习（_learn_to_tm 入库前
    # 检查，冲突即跳过并计数）。转阻断与否由用户裁决，系统不自动切换。
    glossary_conflict_block: bool = False
    # 自定义净语规则配置目录（空=使用内置默认）
    cleaner_config_dir: str = ""

    # ------------------------------------------------------------------
    # v2 两阶段流水线参数
    # ------------------------------------------------------------------
    v2_profile: str = "local"       # local=strict兜底(cleaner+误译拦截) | cloud=lenient(仅通用校验)
    v2_concurrency: int = 1         # 批间并发数（1-5，默认1为串行，对所有服务商生效）
    v2_ctx_local: int = 32768       # 本地模型上下文窗口（批大小/max_tokens 预算依据）
    v2_keep_untranslated: str = "original"   # D1 后仅兼容保留：A/B 双失败一律回退原文+[未翻译] 标记（原 original/empty 两档已并轨，不再删行）
    # 闸门0 送翻前源侧幻觉检测（预合并前对原始条目生效，两档 profile 均执行；
    # 规则库见 refine/defaults/source_hallucination.yaml）
    v2_source_filter: str = "default"       # strict | default | off（仿 v2_profile 档位声明）
    # 保险阀：拦截率超过该百分比则全文件降级为只计数（1-100）
    v2_source_filter_valve_pct: int = DEFAULT_V2_SOURCE_FILTER_VALVE_PCT
    # 上游 WhisperJAV 运行 manifest（H4a 信号通道）：文件或目录路径，空=自动
    # 发现 SRT 同目录 whisperjav_run.json。刻意不进 manifest._CONFIG_FIELDS
    # （路径变化不应误失效；信号内容经 asr_meta_sha1 入指纹，R1）
    asr_meta: str = ""
    # 上游 ASR 信号阈值：覆盖率告警下限（%）/ 运行 manifest 新鲜度上限（小时）
    v2_asr_meta_min_coverage_pct: int = DEFAULT_V2_ASR_META_MIN_COVERAGE_PCT
    v2_asr_meta_stale_max_hours: int = DEFAULT_V2_ASR_META_STALE_MAX_HOURS
    # v1.2.2 C1 per-片语境 sidecar（{stem}.context.md，与输入 srt 同目录
    # 同名）：剧情摘要 + 误听怀疑表注入 A/B 提示词。默认开；文件不存在=
    # 无注入；False=禁用。可经 user_settings.json / SUBTRANSJAV_CONTEXT_SIDECAR
    # 覆盖（TUNABLE_FIELD_TYPES 注册，bool 白名单字面量收敛）。
    context_sidecar: bool = True
    # v1.2.2 Beta 剧情自摘要（auto_synopsis）：闸门0+预合并后自动抽样整片
    # 剧情行，一次独立 LLM 调用生成 3-5 行中文梗概，仅注入 A/B 提示词
    # （绝不写入输出目录/终稿/质量报告）；手写 sidecar【剧情摘要】非空时
    # 手写优先。摘要缓存独立目录 Temp/synopsis_cache/（与 TM 完全隔离）；
    # 摘要文本与缓存不参与 manifest 指纹（与 sidecar 内容同款已知边界）。
    # 可经 user_settings.json / SUBTRANSJAV_AUTO_SYNOPSIS 覆盖。
    auto_synopsis: bool = True
    synopsis_max_chars: int = 6000  # 抽样文本字符预算（桶采样总限幅）
    force: bool = False             # v2: 忽略已有产物强制重跑（覆盖前自动备份）
    tm_learn_gate: bool = True      # TM 学习准入门槛总开关（False 用于 A/B 验证）
    # 断点续跑（清单指纹校验见 manifest 模块）
    resume: bool = False            # v2: 复用上次中断任务已完成的阶段A产物
    force_resume: bool = False      # v2: 指纹校验不匹配时仍强制复用旧产物（隐含 resume）
    # 结构化事件（GUI 进度通道）
    event_format: str = "text"      # 事件输出格式: text=人类可读（默认）| ndjson=结构化事件行
    heartbeat_interval: float = 20.0  # ndjson 心跳间隔（秒）
    # ---- P1-5 配置单一来源（散点收口；默认值=历史硬编码现值，行为不变）----
    temperature_cloud: float = DEFAULT_TEMPERATURE_CLOUD
    temperature_local: float = DEFAULT_TEMPERATURE_LOCAL
    premerge_max_gap_s: float = DEFAULT_PREMERGE_MAX_GAP_S
    premerge_max_items: int = DEFAULT_PREMERGE_MAX_ITEMS
    premerge_max_span_ms: int = DEFAULT_PREMERGE_MAX_SPAN_MS
    premerge_max_chars: int = DEFAULT_PREMERGE_MAX_CHARS
    premerge_min_fragment_chars: int = DEFAULT_PREMERGE_MIN_FRAGMENT_CHARS
    v2_concurrency_max: int = DEFAULT_V2_CONCURRENCY_MAX
    timeout_llm: float = DEFAULT_TIMEOUT_LLM
    timeout_http: float = DEFAULT_TIMEOUT_HTTP
    timeout_probe: float = DEFAULT_TIMEOUT_PROBE
    heartbeat_stale_s: float = DEFAULT_HEARTBEAT_STALE_S
    dropped_log_rotate_mb: int = DEFAULT_DROPPED_LOG_ROTATE_MB
    # ---- P1-6 云端多文件并行（opt-in，默认关；本地服务商一律串行）----
    # 休眠开关：暂无 env/CLI/GUI 开启通道（未列入 TUNABLE_FIELD_TYPES），
    # 预留 2.0，当前恒为关闭（O10）。
    v2_file_parallel: bool = False

    def __post_init__(self):
        # 不变式：force_resume 隐含 resume——强制复用必须以"允许复用"为前提，
        # 否则复用分支（cfg.resume and manifest_trusted）会静默失效（O4）。
        if self.force_resume:
            self.resume = True
        # 分层配置接入（构造入口）：默认 < 用户配置文件 < 环境变量 <
        # CLI/GUI 显式赋值。必须在并发钳制之前，使环境变量覆盖的
        # v2_concurrency_max 生效。
        self._apply_layered_overrides()
        # v2_concurrency 钳制到 1-v2_concurrency_max（防 GUI/CLI 传参越界，
        # 越界值静默回退）
        try:
            n = int(self.v2_concurrency)
        except (TypeError, ValueError):
            n = 1
        try:
            n_max = int(self.v2_concurrency_max)
        except (TypeError, ValueError):
            n_max = DEFAULT_V2_CONCURRENCY_MAX
        self.v2_concurrency = max(1, min(n_max, n))

    def _apply_layered_overrides(self):
        """分层配置：仅当字段仍为 dataclass 默认值时允许用户文件/环境变量覆盖。

        CLI/GUI 在构造时显式传入的非默认值保持最高优先级，不被覆盖。
        已知取舍：显式传入恰好等于默认值时无法与"未传"区分，此时用户
        文件/环境变量仍会生效（现 CLI/GUI 均不暴露这些字段，实际不受影响）。
        """
        try:
            user = load_user_settings()
        except Exception as e:          # 防御：用户配置永不阻塞主流程
            print(f"⚠️ 用户配置加载失败，已忽略: {e}")
            user = {}
        fields = type(self).__dataclass_fields__
        for name, typ in TUNABLE_FIELD_TYPES.items():
            spec = fields.get(name)
            if spec is None:
                continue
            if getattr(self, name, None) != spec.default:
                continue                # 已被 CLI/GUI 显式赋值，不覆盖
            env_raw = os.environ.get(f"SUBTRANSJAV_{name.upper()}")
            value = None
            if env_raw is not None and env_raw.strip() != "":
                value = _coerce_tunable(env_raw.strip(), typ)
                if value is None:
                    print(f"⚠️ 环境变量 SUBTRANSJAV_{name.upper()} 类型非法，"
                          f"已忽略: {env_raw!r}")
            if value is None and name in user:
                value = _coerce_tunable(user[name], typ)
                if value is None:
                    print(f"⚠️ 用户配置字段 {name} 类型非法，已忽略: "
                          f"{user[name]!r}")
            if value is not None:
                setattr(self, name, value)

    def effective_summary(self) -> str:
        """生效配置摘要（多行文本）：散点收口字段 + profile/模型。"""

        def _slot(slot: int) -> str:
            if len(self.stages) > slot:
                s = self.stages[slot]
                return f"[{s.provider}] {self.resolve_model(s) or '(未指定)'}"
            return "(未配置)"

        return "\n".join([
            "⚙️ 生效配置:",
            f"   profile={self.v2_profile} | "
            f"模型 A={_slot(0)} B={_slot(2)}",
            f"   temperature_cloud={self.temperature_cloud} "
            f"temperature_local={self.temperature_local}",
            f"   premerge: max_gap_s={self.premerge_max_gap_s} "
            f"max_items={self.premerge_max_items} "
            f"max_span_ms={self.premerge_max_span_ms} "
            f"max_chars={self.premerge_max_chars} "
            f"min_fragment_chars={self.premerge_min_fragment_chars}",
            f"   v2_concurrency_max={self.v2_concurrency_max}",
            f"   timeout: llm={self.timeout_llm}s http={self.timeout_http}s "
            f"probe={self.timeout_probe}s",
        ])

    # ------------------------------------------------------------------
    def resolve_endpoint(self, provider: str) -> str:
        if provider == "deepseek":
            return ""
        custom = self.endpoints.get(provider, "")
        if custom:
            return custom
        return PROVIDER_ENDPOINT_DEFAULTS.get(provider, "")

    def resolve_api_key(self, provider: str) -> str:
        from .secrets import read_secret
        if provider == "deepseek":
            return (self.api_key_deepseek
                    or os.environ.get("DEEPSEEK_API_KEY", "")
                    or read_secret("deepseek"))
        if provider == "zen":
            return (self.api_key_zen
                    or os.environ.get("OPENCODE_API_KEY", "")
                    or read_secret("zen"))
        if provider == "custom":
            return (self.api_key_custom
                    or os.environ.get("CUSTOM_API_KEY", "")
                    or read_secret("custom"))
        if provider == "siliconflow":
            return (self.api_key_siliconflow
                    or os.environ.get("SILICONFLOW_API_KEY", "")
                    or read_secret("siliconflow"))
        return ""   # lmstudio / ollama 本地服务无需密钥

    def resolve_model(self, stage: StageConfig) -> str:
        return stage.model or PROVIDER_MODEL_DEFAULTS.get(stage.provider, "")

    def batch_for(self, stage: StageConfig) -> int:
        # lmstudio / ollama 均为本地服务商，走 batch_local 语义（含硬上限）
        b = (self.batch_local if stage.provider in ("lmstudio", "ollama")
             else self.batch_cloud)
        if stage.provider in ("lmstudio", "ollama"):
            b = min(b, LOCAL_BATCH_HARD_CAP)
        return max(1, int(b))

    def validate(self):
        errors = []
        enabled = [s for s in self.stages if s.enabled]
        if not enabled:
            errors.append("至少需要启用一个阶段")
        for s in enabled:
            if s.provider != "deepseek":
                ep = self.resolve_endpoint(s.provider)
                if not ep:
                    errors.append(f"{s.name}: 服务商 [{s.provider}] 缺少接口地址(endpoint)")
            if not (self.resolve_model(s) or ""):
                errors.append(f"{s.name}: 未指定模型名")
            if s.provider not in ("lmstudio", "ollama") \
                    and s.provider != "custom" \
                    and not self.resolve_api_key(s.provider):
                errors.append(f"{s.name}: 服务商 [{s.provider}] 缺少 API Key")
        if not self.inputs:
            errors.append("未指定输入文件")
        for p in self.inputs:
            if not os.path.isfile(p):
                errors.append(f"输入文件不存在：{p}")
        if self.fallback_local and not self.fallback_model.strip():
            errors.append("已启用云端故障本地接管，但未指定接管模型(--fallback-model)")
        if self.event_format not in ("text", "ndjson"):
            errors.append(f"event_format 仅支持 text/ndjson，当前: {self.event_format}")
        if self.v2_source_filter not in ("strict", "default", "off"):
            errors.append(
                f"v2_source_filter 仅支持 strict/default/off，"
                f"当前: {self.v2_source_filter}")
        try:
            valve = int(self.v2_source_filter_valve_pct)
        except (TypeError, ValueError):
            valve = -1
        if not 1 <= valve <= 100:
            errors.append(
                "v2_source_filter_valve_pct 取值范围 1-100，当前: "
                f"{self.v2_source_filter_valve_pct}")
        # 断句预合并硬上限：必须为正整数（跨度毫秒 / 字符数 / 碎片阈值）
        for name in ("premerge_max_span_ms", "premerge_max_chars",
                     "premerge_min_fragment_chars"):
            try:
                v = int(getattr(self, name))
            except (TypeError, ValueError):
                v = 0
            if v <= 0:
                errors.append(
                    f"{name} 必须为正整数，当前: {getattr(self, name)}")
        return errors


def ensure_language_support():
    """放行同语言目标（阶段2 zh→zh 审核需要）。
    运行时补丁，不修改上游文件。"""
    from subtransjav.translate.providers import SUPPORTED_TARGETS
    SUPPORTED_TARGETS |= {"japanese", "chinese"}
