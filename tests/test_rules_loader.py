"""
规则单一数据源（translation_rules.yaml）测试：
1. YAML 可加载、必需键存在
2. post_validate 行为回归（YAML 参数驱动与原硬编码行为一致）
3. 一致性防漂移：YAML 生成的提示词段与代码组装结果逐字节一致
"""

import pytest

from subtransjav.refine import post_validate
from subtransjav.refine.rules_loader import (
    RulesLoadError,
    load_rules,
    load_rules_safe,
)


# ---------------------------------------------------------------------------
# 1. 加载与结构
# ---------------------------------------------------------------------------

def test_rules_loadable():
    rules = load_rules()
    assert isinstance(rules, dict)
    assert rules["meta"]["version"] >= 1


def test_rules_required_keys():
    rules = load_rules()
    v = rules["validator_rules"]
    assert v["dewei_mistranslation"]["target_pattern"] == "作为"
    assert v["dewei_mistranslation"]["replacement"] == "是"
    assert v["dewei_mistranslation"]["warn_only"] is False
    assert "僕" in v["subject_misjudge"]["source_pattern"]
    assert "たち" in v["subject_misjudge"]["source_pattern"]
    assert "で" in v["subject_misjudge"]["source_pattern"]   # 定语构造收紧
    assert "(?!は)" in v["subject_misjudge"]["source_pattern"]  # 僕たちは…不误伤
    p = rules["prompt_sections"]
    assert p["sentence_final_particles"].startswith("- 句末助词处理")
    assert "### retry_instructions" in p["retry_cleaning"]
    assert "硬性豁免规则" in p["hardened_suffix"]


def test_rules_missing_config_dir_falls_back_to_package(tmp_path):
    """config_dir 指向不存在目录时回退包内默认副本，不报错。"""
    rules, err = load_rules_safe(str(tmp_path / "nonexistent"))
    assert err is None
    assert rules["meta"]["version"] >= 1


def test_rules_corrupt_yaml_raises(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    (d / "translation_rules.yaml").write_text(
        "validator_rules: [ broken", encoding="utf-8")
    with pytest.raises(RulesLoadError):
        load_rules(str(tmp_path))


def test_rules_missing_key_raises(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    (d / "translation_rules.yaml").write_text(
        "meta:\n  version: 1\n", encoding="utf-8")
    with pytest.raises(RulesLoadError):
        load_rules(str(tmp_path))


def test_user_rules_yaml_matches_package_default():
    """防漂移：用户目录副本与包内回退副本应逐字节一致。

    config/rules/translation_rules.yaml 是用户目录副本（load_rules 的
    首选来源），subtransjav/refine/defaults/translation_rules.yaml
    是包内回退副本（用户副本缺失时兜底）。两者内容不一致时，
    "改包内 YAML 不生效"或"回退行为与用户配置不同"的漂移就会静默发生；
    本测试锁定两者同步更新（如未来允许差异，应改为断言 defaults 是
    用户副本的合法超集，并同步修改 docstring）。
    """
    from pathlib import Path
    import subtransjav.refine.rules_loader as rl

    repo_root = Path(rl.__file__).resolve().parents[2]
    user_copy = repo_root / "config" / "rules" / "translation_rules.yaml"
    package_copy = Path(rl._PACKAGE_RULES_PATH)
    assert user_copy.is_file(), f"用户目录规则副本缺失: {user_copy}"
    assert user_copy.read_bytes() == package_copy.read_bytes(), (
        "config/rules/translation_rules.yaml 与包内 defaults/translation_rules.yaml "
        "不一致，请同步更新两份副本")


# ---------------------------------------------------------------------------
# 2. post_validate 行为回归（与原硬编码行为一致）
# ---------------------------------------------------------------------------

def _entries(src, tgt):
    src_e = [{"index": i, "timing": f"0:00:0{i},000", "text": s}
             for i, s in enumerate(src, 1)]
    tgt_e = [{"index": i, "timing": f"0:00:0{i},000", "text": s}
             for i, s in enumerate(tgt, 1)]
    return src_e, tgt_e


def test_dewei_mistranslation_fixed():
    src, tgt = _entries(["部長で、エースで。"], ["作为部长，作为王牌。"])
    fixes, warnings, _flagged = post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 1
    assert tgt[0]["text"] == "是部长，是王牌。"
    assert any("で误译修正" in w for w in warnings)


def test_dewei_toshite_not_touched():
    src, tgt = _entries(["学生として参加した。"], ["作为学生参加了。"])
    fixes, _, _flagged = post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert tgt[0]["text"] == "作为学生参加了。"


def test_subject_misjudge_warn_only():
    src, tgt = _entries(["僕たち水泳部の部長で。"], ["我是游泳部的部长……"])
    fixes, warnings, _flagged = post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 0          # 仅告警，不改动译文
    assert tgt[0]["text"] == "我是游泳部的部长……"
    assert any("主语误判" in w for w in warnings)


def test_no_false_positive():
    src, tgt = _entries(["今日はいい天気だ。"], ["今天天气真好。"])
    fixes, warnings, _flagged = post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert warnings == []


# ---------------------------------------------------------------------------
# 3. 一致性防漂移：instructions.py 组装结果 == YAML 数据
# ---------------------------------------------------------------------------

def test_prompt_sections_match_yaml():
    from subtransjav.refine import instructions as instr
    p = load_rules()["prompt_sections"]

    # RETRY_CLEANING = "\n" + YAML retry_cleaning
    assert instr.RETRY_CLEANING == "\n" + p["retry_cleaning"]
    # HARDENED_SUFFIX = "\n\n" + YAML hardened_suffix
    assert instr.HARDENED_SUFFIX == "\n\n" + p["hardened_suffix"]
    # legacy STAGE_PROMPTS 阶段表已删除（v2 用 pipeline_v2.V2_STAGE_PROMPTS）


def test_retry_cleaning_semantics():
    """重试指令必须保护'留空=有意删除'语义（防误回填）。"""
    from subtransjav.refine import instructions as instr
    assert "留空表示该条目被有意删除" in instr.RETRY_CLEANING
    assert "严禁为其编造或回填内容" in instr.RETRY_CLEANING
