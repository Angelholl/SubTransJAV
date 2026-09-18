"""
翻译质量后验检测（日译中阶段）：检测并修正系统性误译

规则参数来自 config/rules/translation_rules.yaml（单一数据源，
经 rules_loader 加载）。更换本地模型时只需复核该 YAML。
"""

import logging
import re

from .rules_loader import load_rules

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 翻译质量后验拦截（日译中阶段）
# 规则由 YAML 的 validator_rules 段驱动：
#   dewei_mistranslation: で误译（"作为"误用）→ 自动修正
#   subject_misjudge:     主语误判（僕たち→"我"）→ 仅告警
#   antonym_*:            反义误译（やめて→"别停"等）→ 仅告警（批次 B2，
#                         双侧锚定：源文命中指定形态 且 译文命中目标集）
# 动作由各规则的 warn_only 字段决定（YAML 单一数据源）：
#   warn_only=true  → 告警加入 warnings 列表，不改动译文；
#   warn_only=false → 升级为"硬性告警"：warnings 中加 [硬性] 前缀并
#                     logger.error 记录；可安全自动修正的规则（dewei）
#                     仍自动修正，无可靠自动修正手段的规则（subject/
#                     antonym）只告警，绝不发明自动改写逻辑。
# ------------------------------------------------------------------


def _compile_validator_rules():
    """从 YAML 编译校验规则。返回 {name: {regexes..., action...}}。

    在模块首次使用时惰性编译；YAML 损坏时抛 RulesLoadError
    （不允许静默跳过校验——那会掩盖配置问题）。
    """
    rules = load_rules().get("validator_rules", {})

    compiled = {}
    d = rules.get("dewei_mistranslation", {})
    compiled["dewei"] = {
        "target": re.compile(d["target_pattern"]),
        "src_must": d["source_must_contain"],      # 子串存在性检查
        "src_must_not": d["source_must_not_contain"],
        "replacement": d["replacement"],
        "warn_only": bool(d.get("warn_only", False)),
    }
    s = rules.get("subject_misjudge", {})
    compiled["subject"] = {
        "source": re.compile(s["source_pattern"]),
        "target": re.compile(s["target_pattern"]),
        "warn_only": bool(s.get("warn_only", True)),
    }
    # 双侧锚定 warn_only 规则（批次 B2 antonym_* + 批次 B 闭环
    # body_part_kubi / climax_iku_variant）：按统一"双侧锚定"结构编译
    # （source_pattern + target_pattern + warn_only），新增同前缀规则
    # 无需改检测代码。规则仅 local/strict 档生效（_apply_fallback_rules
    # 既有执行条件，lenient 档整层跳过）。
    for name, r in rules.items():
        if not name.startswith(("antonym_", "body_part_", "climax_")):
            continue
        compiled[name] = {
            "source": re.compile(r["source_pattern"]),
            "target": re.compile(r["target_pattern"]),
            "warn_only": bool(r.get("warn_only", True)),
        }
    return compiled


_compiled = None


def _get_compiled():
    global _compiled
    if _compiled is None:
        _compiled = _compile_validator_rules()
    return _compiled


def _emit_warning(warnings: list[str], text: str, warn_only: bool):
    """把告警写入 warnings 列表；非 warn_only 规则升级为硬性告警。"""
    if warn_only:
        warnings.append(text)
    else:
        msg = f"[硬性] {text}"
        warnings.append(msg)
        logger.error(msg)


def check_and_fix_translation_errors(
    src_entries: list[dict],
    tgt_entries: list[dict],
) -> tuple[int, list[str], set]:
    """
    翻译质量后验拦截：逐条比对源文与目标文，检测并修正系统性误译。
    返回：(修正条数, 警告列表, flagged_indexes)
    """
    fixes = 0
    warnings = []
    flagged_indexes: set = set()

    rules = _get_compiled()
    dewei = rules["dewei"]
    subject = rules["subject"]

    # 按 index 对齐
    tgt_map = {e["index"]: e for e in tgt_entries}

    for src in src_entries:
        src_text = (src.get("text") or "").strip()
        idx = src.get("index")
        tgt = tgt_map.get(idx)
        if not tgt:
            continue
        tgt_text = (tgt.get("text") or "").strip()
        if not tgt_text:
            continue

        # 检测 1：で误译（"作为"误用）→ 替换修正（可安全自动修正，
        # 与 warn_only 无关；warn_only=false 时告警升级为硬性）
        if dewei["target"].search(tgt_text) \
                and dewei["src_must"] in src_text \
                and dewei["src_must_not"] not in src_text:
            # lambda 使 replacement 字面化：YAML 中的 \1 等序列按原样
            # 写入而不被解释为反向引用
            fixed = dewei["target"].sub(lambda m: dewei["replacement"], tgt_text)
            tgt["text"] = fixed
            tgt_text = fixed          # 同步局部变量，供检测 2 使用
            fixes += 1
            flagged_indexes.add(idx)
            _emit_warning(
                warnings,
                f"⚠️ #{idx} で误译修正: '{dewei['target'].pattern}' → "
                f"'{dewei['replacement']}' | 源: {src_text[:30]}",
                dewei["warn_only"])

        # 检测 2：主语误判（僕たち→单数"我"）—— 主语推断需上下文，没有
        # 可靠的自动修正手段，无论 warn_only 取值都不改动译文；
        # warn_only=false 时仅升级为硬性告警。
        # 告警文案动态引用实际命中的译文开头（前 6 字），并保留"主语误判"
        # 四字（quality_report 按 `"主语误判" in w` 单独归类）。
        if subject["source"].search(src_text) \
                and subject["target"].match(tgt_text):
            flagged_indexes.add(idx)
            _emit_warning(
                warnings,
                f"⚠️ #{idx} 主语误判待复核: 源含'僕たち/我们'但目标以"
                f"'{tgt_text[:6]}'开头 | 源: {src_text[:30]}",
                subject["warn_only"])

        # 检测 3：双侧锚定误译（批次 B2 antonym_* + 批次 B 闭环
        # body_part_*/climax_*）—— 源文命中指定形态 且 译文命中目标集才告警
        # （如源含 やめて 且译文出现"别停/不要停"）。
        # 误译判断依赖语境，没有可靠的自动修正手段，无论 warn_only 取值
        # 都不改动译文；warn_only=false 时仅升级为硬性告警。
        # 告警文案含规则名标识（antonym_* 等），供测试与质量报告归类。
        # flagged_indexes 经既有机制阻断该翻译对进入 TM 学习（零新代码）。
        for name, rule in rules.items():
            if not name.startswith(("antonym_", "body_part_", "climax_")):
                continue
            if rule["source"].search(src_text) \
                    and rule["target"].search(tgt_text):
                flagged_indexes.add(idx)
                head = ("反义误译待复核" if name.startswith("antonym_")
                        else "误译待复核")
                _emit_warning(
                    warnings,
                    f"⚠️ #{idx} {head}[{name}]: 源文命中该形态"
                    f"但译文出现'{rule['target'].pattern}'"
                    f" | 源: {src_text[:30]}",
                    rule["warn_only"])

    return fixes, warnings, flagged_indexes


def scan_learn_defect(src: str, tgt: str) -> "str | None":
    """TM 学习准入扫描：返回缺陷类别名，干净返回 None。

    规则口径全部来自 2026-09-07 TM 审计实证（见 TM学习准入门槛方案.md）。
    规则按顺序判定，命中即返回类别名。所有规则只做检测，绝不修改文本。

    权重口径：CJK 字符计 1，非 CJK 计 0.5（与 quality_report._weight 一致）。
    """
    # 1. 翻译标记泄漏（LLM 协议残留）
    if 'Translation' in tgt:
        return "leak"

    # 2. 假名残留（tgt 含假名）
    #    若 tgt==src（同文残留），已被上游"译文=原文"防线挡住，此处豁免
    if tgt != src and re.search(r"[\u3040-\u30ff\uff66-\uff9f]", tgt):
        return "kana"

    # 3. 源文前缀残留（LLM 只在源文后追加译文）
    if len(tgt) > len(src) and tgt.startswith(src[:max(1, len(src) - 1)]):
        return "src_prefix"

    # 4. 占位符
    if '无对应条目' in tgt:
        return "placeholder"

    # 5. 长度比离群（源文权重 <4 豁免：短叹词合法映射保护）
    w_src = sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in src)
    if w_src >= 4:
        w_tgt = sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in tgt)
        r = w_tgt / w_src
        if r < 0.25 or r > 8.0:
            return "len_ratio"

    return None
