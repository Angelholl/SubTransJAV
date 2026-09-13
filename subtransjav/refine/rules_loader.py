"""
翻译规则单一数据源加载器
========================
从 translation_rules.yaml 读取规则（用户目录 config/rules/ 优先，
包内 defaults/ 回退），供 post_validate.py（校验器参数）与
instructions.py（提示词生成段）消费。

设计约束：
- YAML 缺失/损坏时必须显式报错（不允许静默降级到硬编码规则，
  否则会出现"改 YAML 不生效"的漂移）；
- load_rules() 结果进程内缓存（同一文件只解析一次）。
"""

import functools
import os

import yaml

# YAML 必须包含的键路径（点号表示层级），缺失即视为损坏
_REQUIRED_KEYS = (
    "meta",
    "validator_rules.dewei_mistranslation.target_pattern",
    "validator_rules.dewei_mistranslation.source_must_contain",
    "validator_rules.dewei_mistranslation.source_must_not_contain",
    "validator_rules.dewei_mistranslation.replacement",
    "validator_rules.subject_misjudge.source_pattern",
    "validator_rules.subject_misjudge.target_pattern",
    "prompt_sections.sentence_final_particles",
    "prompt_sections.retry_cleaning",
    "prompt_sections.hardened_suffix",
)

_PACKAGE_RULES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "defaults", "translation_rules.yaml")


class RulesLoadError(Exception):
    """translation_rules.yaml 缺失、损坏或必需键缺失。"""


def _config_rules_path(config_dir: str = None) -> str:
    base = config_dir
    if not base:
        from .config import CONFIG_DIR
        base = CONFIG_DIR
    return os.path.join(base, "rules", "translation_rules.yaml")


def _resolve_rules_path(config_dir: str = None) -> str:
    """用户目录优先，包内默认回退。"""
    p = _config_rules_path(config_dir)
    if os.path.isfile(p):
        return p
    return _PACKAGE_RULES_PATH


def resolve_rules_path(config_dir: str = None) -> str:
    """实际生效的 translation_rules.yaml 路径（公开别名）。

    供 manifest 指纹计算等外部模块对齐"实际加载的文件"，
    避免跨模块依赖下划线私有名。
    """
    return _resolve_rules_path(config_dir)


def _nested_get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _validate(rules: dict, path: str):
    for key in _REQUIRED_KEYS:
        v = _nested_get(rules, key)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise RulesLoadError(
                f"{path}: 缺少必需键或值为空: {key}")


@functools.cache
def _load_cached(path: str, mtime: float) -> dict:
    with open(path, encoding="utf-8") as f:
        rules = yaml.safe_load(f)
    if not isinstance(rules, dict):
        raise RulesLoadError(f"{path}: YAML 顶层必须是映射")
    _validate(rules, path)
    return rules


def load_rules(config_dir: str = None) -> dict:
    """加载翻译规则（用户目录优先，包内回退；带缓存）。

    Raises
    ------
    RulesLoadError
        YAML 缺失（用户与包内均无）、解析失败或必需键缺失。
    """
    path = _resolve_rules_path(config_dir)
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        raise RulesLoadError(f"translation_rules.yaml 不可访问: {path}: {e}") from e
    try:
        return _load_cached(path, mtime)
    except yaml.YAMLError as e:
        raise RulesLoadError(f"{path}: YAML 解析失败: {e}") from e
    except RulesLoadError:
        raise
    except OSError as e:
        raise RulesLoadError(f"{path}: 读取失败: {e}") from e


def load_rules_safe(config_dir: str = None) -> tuple:
    """容错版：返回 (rules, error_msg)。成功时 error_msg 为 None，
    失败时 rules 为 None、error_msg 含路径与原因。"""
    try:
        return load_rules(config_dir), None
    except RulesLoadError as e:
        return None, str(e)
