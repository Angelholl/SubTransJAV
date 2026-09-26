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
    # D4：target_pattern 收窄为单数——负向前瞻排除复数，"我们是…"不告警
    assert "(?![们們等])" in v["subject_misjudge"]["target_pattern"]
    # 批次 B2：反义误译规则（双侧锚定 + warn_only，仅 local/strict 档生效）
    for key in ("antonym_yamete", "antonym_saitei", "antonym_zurui"):
        assert key in v, f"缺少反义规则 {key}"
        assert v[key]["warn_only"] is True, f"{key} 必须为 warn_only"
        assert "仅 local/strict 档生效" in v[key]["description"], key
    y = v["antonym_yamete"]
    assert "(?!ないで)" in y["source_pattern"], "须负向排除 やめないで"
    assert "(?!るな)" in y["source_pattern"], "须负向排除 やめるな"
    assert "别停" in y["target_pattern"] and "不要停" in y["target_pattern"]
    assert "やめないで 才是'别停'" in y["description"]
    assert "最低" in v["antonym_saitei"]["source_pattern"]
    assert "真不错" in v["antonym_saitei"]["target_pattern"]
    assert "ずるい" in v["antonym_zurui"]["source_pattern"]
    assert "ずりー" in v["antonym_zurui"]["source_pattern"]
    assert v["antonym_zurui"]["target_pattern"] == "滑"
    # 批次 B 闭环：身体部位 / イク系变体规则（双侧锚定 + warn_only）
    for key in ("body_part_kubi", "climax_iku_variant"):
        assert key in v, f"缺少批次 B 规则 {key}"
        assert v[key]["warn_only"] is True, f"{key} 必须为 warn_only"
        assert "仅 local/strict 档生效" in v[key]["description"], key
    kubi = v["body_part_kubi"]
    assert "(?!が回ら)" in kubi["source_pattern"], "须负向排除 首が回らない"
    assert "(?!を長く)" in kubi["source_pattern"], "须负向排除 首を長くする"
    assert "(?!になる)" in kubi["source_pattern"], "须负向排除 首になる"
    assert "(?!をかしげ)" in kubi["source_pattern"], "须负向排除 首をかしげる"
    assert "摸头" in kubi["target_pattern"] and "头发" in kubi["target_pattern"]
    assert "身体语境 首=脖子非头" in kubi["description"]
    iku = v["climax_iku_variant"]
    assert "(?!行く|行きます|行った|行こう" in iku["source_pattern"], \
        "须显式负向排除汉字 行く系（契约要求）"
    assert "イク" in iku["source_pattern"], "须负向排除 glossary 精确形态 イク"
    assert "いっちゃう" in iku["source_pattern"], "须覆盖假名变体 いっちゃう"
    assert "要去了" in iku["target_pattern"] \
        and "快高潮了" in iku["target_pattern"]
    assert "普通'去'不受影响" in iku["description"]
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
    fixes, warnings, _flagged, _structured = \
    post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 1
    assert tgt[0]["text"] == "是部长，是王牌。"
    assert any("で误译修正" in w for w in warnings)


def test_dewei_toshite_not_touched():
    src, tgt = _entries(["学生として参加した。"], ["作为学生参加了。"])
    fixes, _, _flagged, _structured = \
    post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert tgt[0]["text"] == "作为学生参加了。"


def test_subject_misjudge_warn_only():
    src, tgt = _entries(["僕たち水泳部の部長で。"], ["我是游泳部的部长……"])
    fixes, warnings, _flagged, _structured = \
    post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 0          # 仅告警，不改动译文
    assert tgt[0]["text"] == "我是游泳部的部长……"
    assert any("主语误判" in w for w in warnings)


def test_no_false_positive():
    src, tgt = _entries(["今日はいい天気だ。"], ["今天天气真好。"])
    fixes, warnings, _flagged, _structured = \
    post_validate.check_and_fix_translation_errors(src, tgt)
    assert fixes == 0
    assert warnings == []


# ---------------------------------------------------------------------------
# 3. 一致性防漂移：instructions.py 组装结果 == YAML 数据
# ---------------------------------------------------------------------------

def test_prompt_sections_match_yaml():
    from subtransjav.refine import instructions as instr
    p = load_rules()["prompt_sections"]

    # RETRY_CLEANING = "\n" + YAML retry_cleaning
    assert "\n" + p["retry_cleaning"] == instr.RETRY_CLEANING
    # HARDENED_SUFFIX = "\n\n" + YAML hardened_suffix
    assert "\n\n" + p["hardened_suffix"] == instr.HARDENED_SUFFIX
    # legacy STAGE_PROMPTS 阶段表已删除（v2 用 pipeline_v2.V2_STAGE_PROMPTS）


def test_retry_cleaning_semantics():
    """重试指令必须保护'留空=有意删除'语义（防误回填）。"""
    from subtransjav.refine import instructions as instr
    assert "留空表示该条目被有意删除" in instr.RETRY_CLEANING
    assert "严禁为其编造或回填内容" in instr.RETRY_CLEANING


# ---------------------------------------------------------------------------
# 4. D5 反反转硬条款：四处同步防漂移
# ---------------------------------------------------------------------------

def test_anti_reversal_clause_synced_across_prompts():
    """D5 反反转硬条款必须四处同步：V2_STAGE_PROMPTS A/B + 两张角色卡。

    背景：乱码源文曾被 LLM 强译成与原意相反的中文（拒绝→邀请方向反转）。
    本测试钉住内置阶段提示词与角色卡模板的硬条款同时在场，任一处删除
    即失败（语义一致由措辞关键词钉扎，允许句式微调）。
    """
    from pathlib import Path

    import subtransjav.refine.rules_loader as rl
    from subtransjav.refine.pipeline_v2 import V2_STAGE_PROMPTS

    repo_root = Path(rl.__file__).resolve().parents[2]
    tpl_dir = repo_root / "config" / "templates"
    sources = {
        "V2_STAGE_PROMPTS[A]": V2_STAGE_PROMPTS["A"],
        "V2_STAGE_PROMPTS[B]": V2_STAGE_PROMPTS["B"],
        "角色-净语翻译.txt":
            (tpl_dir / "角色-净语翻译.txt").read_text(encoding="utf-8"),
        "角色-审校抛光.txt":
            (tpl_dir / "角色-审校抛光.txt").read_text(encoding="utf-8"),
    }
    for name, text in sources.items():
        assert "源文为转录乱码/残缺时" in text, name
        assert "禁止臆测情节" in text, name
        assert "禁止补充原文不存在的动作或语义" in text, name
        assert "严禁反转语义方向" in text, name
        assert "拒绝↔邀请" in text and "停止↔继续" in text \
            and "否定↔肯定" in text, name
        assert "严禁从零编造" in text, name
    # B 卡审校清单必须含语义方向核查项
    assert "对照日文核查语义方向是否被反转" in sources["角色-审校抛光.txt"]

    # 批次 B2：反义误译条款同步——规则 YAML 键 + 两张角色卡新增行钉扎
    rules_v = load_rules()["validator_rules"]
    for key in ("antonym_yamete", "antonym_saitei", "antonym_zurui"):
        assert key in rules_v, f"规则键缺失: {key}"
        assert rules_v[key]["warn_only"] is True, key
    a_card = sources["角色-净语翻译.txt"]
    assert "禁止默认译'别停/不要停'" in a_card, "A 卡缺 やめて 反义条款"
    assert "やめないで 才是'别停'" in a_card, "A 卡缺 やめないで 正译说明"
    assert "最低=差劲" in a_card, "A 卡缺 最低 反义条款"
    assert "ずるい/ずりー=狡猾/不公平" in a_card, "A 卡缺 ずるい 反义条款"
    b_card = sources["角色-审校抛光.txt"]
    assert "对照日文核查三类反义误译" in b_card, "B 卡审校清单缺反义误译核查项"
    assert "やめないで 才是\"别停\"" in b_card, "B 卡缺 やめないで 正译说明"
    assert "最低=差劲" in b_card and "狡猾/不公平" in b_card, "B 卡三类反义不齐"

    # 批次 B 闭环：两卡新增行同步钉扎（首=脖子 / イク系变体）——
    # 新增行在 A 卡（反义条款旁）与 B 卡（审校核查项旁）必须同时在场
    for card_name in ("角色-净语翻译.txt", "角色-审校抛光.txt"):
        card = sources[card_name]
        assert "身体语境下 首=脖子（不是头）" in card, \
            f"{card_name} 缺 首=脖子 条款"
        assert "首が回らない、首を長くする、首になる、首をかしげる" in card, \
            f"{card_name} 缺习语负向排除例示"
        assert "イク 系（イく/イク/イっ/イきそう/いっちゃう）" in card, \
            f"{card_name} 缺 イク系 变体条款"
        assert "禁止泛化为普通\"去\"" in card, f"{card_name} 缺泛化禁令"
        assert "行く/行きます/行った/行こう 不受影响" in card, \
            f"{card_name} 缺普通行く系豁免"
    # 对应规则 YAML 键同步在场
    for key in ("body_part_kubi", "climax_iku_variant"):
        assert key in rules_v, f"批次 B 规则键缺失: {key}"
        assert rules_v[key]["warn_only"] is True, key


# ---------------------------------------------------------------------------
# 5. D4 拟声行条款：四处同步防漂移（v1.2.2 批次 D）
# ---------------------------------------------------------------------------

def test_onomatopoeia_clause_synced_across_prompts():
    """D4 拟声行条款必须四处同步：V2_STAGE_PROMPTS A/B + 两张角色卡。

    背景：用户裁决的准确性口径——呻吟行也须有实义产出（中文拟声），
    但禁止音译假名/生造汉字词；疑似误听词豁免本条。本测试沿用 D5 的
    四源钉扎模式，任一处删除即失败（语义一致由措辞关键词钉扎）。
    """
    from pathlib import Path

    import subtransjav.refine.rules_loader as rl
    from subtransjav.refine.pipeline_v2 import V2_STAGE_PROMPTS

    repo_root = Path(rl.__file__).resolve().parents[2]
    tpl_dir = repo_root / "config" / "templates"
    sources = {
        "V2_STAGE_PROMPTS[A]": V2_STAGE_PROMPTS["A"],
        "V2_STAGE_PROMPTS[B]": V2_STAGE_PROMPTS["B"],
        "角色-净语翻译.txt":
            (tpl_dir / "角色-净语翻译.txt").read_text(encoding="utf-8"),
        "角色-审校抛光.txt":
            (tpl_dir / "角色-审校抛光.txt").read_text(encoding="utf-8"),
    }
    for name, text in sources.items():
        assert "仅当源文确定为无实义的拟声/呻吟（纯假名噪声）时" in text, name
        assert "译为中文拟声（唔…/嗯…/啊…）或省略号" in text, name
        assert "禁止音译成假名词或生造汉字词" in text, name
        assert "疑似误听词（见误听怀疑清单）不适用本条" in text, name
        assert "按误听语义翻译" in text, name
    # A 卡条款命名行 / B 卡审校清单核查项在场
    assert "拟声行条款：" in sources["角色-净语翻译.txt"]
    assert "拟声行核查：" in sources["角色-审校抛光.txt"]
