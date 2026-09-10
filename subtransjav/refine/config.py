"""
Refine 配置：阶段定义 / 服务商预设 / 默认参数
"""

import os
from dataclasses import dataclass, field

# ---- 批量默认值（需求定稿：本地 30 / 云端 30）----
DEFAULT_BATCH_LOCAL = 30
DEFAULT_BATCH_CLOUD = 30

# 本地模型单批硬上限（防止超出小模型能力）
LOCAL_BATCH_HARD_CAP = 50

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
    # 自定义净语规则配置目录（空=使用内置默认）
    cleaner_config_dir: str = ""

    # ------------------------------------------------------------------
    # v2 两阶段流水线参数
    # ------------------------------------------------------------------
    v2_profile: str = "local"       # local=strict兜底(cleaner+误译拦截) | cloud=lenient(仅通用校验)
    v2_concurrency: int = 1         # 批间并发数（1-5，默认1为串行，对所有服务商生效）
    v2_ctx_local: int = 32768       # 本地模型上下文窗口（批大小/max_tokens 预算依据）
    v2_keep_untranslated: str = "original"   # 阶段B仍失败时: original=保留日文原文 | empty=删除
    force: bool = False             # v2: 忽略已有产物强制重跑（覆盖前自动备份）
    tm_learn_gate: bool = True      # TM 学习准入门槛总开关（False 用于 A/B 验证）

    def __post_init__(self):
        # v2_concurrency 钳制到 1-5（防 GUI/CLI 传参越界，越界值静默回退）
        try:
            n = int(self.v2_concurrency)
        except (TypeError, ValueError):
            n = 1
        self.v2_concurrency = max(1, min(5, n))

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
        return errors


def ensure_language_support():
    """放行同语言目标（阶段2 zh→zh 审核需要）。
    运行时补丁，不修改上游文件。"""
    from subtransjav.translate.providers import SUPPORTED_TARGETS
    SUPPORTED_TARGETS |= {"japanese", "chinese"}
