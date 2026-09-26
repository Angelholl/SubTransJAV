"""resolve_final_block 官方映射契约测试（D11 地基二）。

四例各自构造最小 fixture 复现"报告 index 与终稿块错位"的四类位移机制：
精确 index 匹配为主、同 index 取列表顺序首个、找不到返回 None
（禁 ±1 算术外推）。
"""

from subtransjav.refine.quality_report import resolve_final_block
from subtransjav.refine.v2_premerge import _align_orig_by_timing, _premerge_entries


def test_premerge_merge_creates_index_hole():
    """① 预合并合并：两条 timing 相邻触发 _premerge_entries 合并，
    合并块保留首条 index，被并条目 index 成空洞——resolve(首条) 命中
    合并块，resolve(被并条目) 返回 None（绝不外推到合并块）。"""
    entries = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:01,800", "text": "僕は"},
        {"index": 2, "timing": "00:00:02,000 --> 00:00:03,000", "text": "部長です"},
    ]
    merged = _premerge_entries(entries)
    assert len(merged) == 1                        # 两条确已合并
    assert merged[0]["index"] == 1                 # 合并块保留首条 index
    assert merged[0]["text"] == "僕は部長です"
    assert resolve_final_block(merged, 1) is merged[0]
    assert resolve_final_block(merged, 2) is None  # 被并 index 成空洞


def test_cleaner_delete_then_index_restore():
    """② cleaner 删除重编号：复现 v2_rules "按时间轴双指针恢复原始编号"
    （cleaner 按输出顺序重编号 1、2 → 恢复为原始 1、3，原 #2 已被规则
    删除）后的形态——index 恢复但存在删除洞，仍精确命中、缺失返 None。"""
    orig = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "甲"},
        {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000", "text": "乙"},
        {"index": 3, "timing": "00:00:05,000 --> 00:00:06,000", "text": "丙"},
    ]
    # cleaner 输出：原 #2 被规则删除，按输出顺序重编号为 1、2
    cleaned = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "甲"},
        {"index": 2, "timing": "00:00:05,000 --> 00:00:06,000", "text": "丙"},
    ]
    aligned = _align_orig_by_timing(cleaned, orig)
    for e, o in zip(cleaned, aligned, strict=False):
        if o is not None and e["index"] != o["index"]:
            e["index"] = o["index"]                # v2_rules 同款恢复
    assert [e["index"] for e in cleaned] == [1, 3]
    assert resolve_final_block(cleaned, 1) is cleaned[0]
    assert resolve_final_block(cleaned, 3) is cleaned[1]
    assert resolve_final_block(cleaned, 2) is None  # 删除洞不 ±1 猜


def test_quarantine_moved_out_keeps_other_indexes():
    """③ 隔离区移出：source_hallucination.quarantine_review 语义——
    被移出条目从主稿消失但其余 index 原样保留（不重编号）。"""
    from subtransjav.refine.source_hallucination import quarantine_review
    final = [
        {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000", "text": "第一条"},
        {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000", "text": "流畅中文句子"},
        {"index": 3, "timing": "00:00:05,000 --> 00:00:06,000", "text": "第三条"},
    ]
    main, quarantined = quarantine_review(
        final, [{"position": 0, "category": "幻觉候选", "text": "源文"}],
        {0: 2})
    assert [e["index"] for e in main] == [1, 3]    # 其余 index 原样保留
    assert [e["index"] for e in quarantined] == [2]
    assert resolve_final_block(main, 1) is main[0]
    assert resolve_final_block(main, 3) is main[1]
    assert resolve_final_block(main, 2) is None    # 被移出 index 返回 None


def test_pseudo_entry_index_zero_collides_first_wins():
    """④ 语言过滤伪条目：伪条目 index 缺省 0 可与真实 #0 重号
    （v2_rules 语言过滤回退分支的形态）——resolve(0) 按 final_entries
    列表顺序首个命中，非单射语义有钉。"""
    final = [
        {"index": 0, "timing": "00:00:01,000 --> 00:00:02,000", "text": "真实零号"},
        {"index": 0, "timing": "00:00:03,000 --> 00:00:04,000", "text": "伪条目"},
    ]
    assert resolve_final_block(final, 0) is final[0]
    assert resolve_final_block(final, 1) is None
    # 非单射契约钉在 docstring（同 index 多块取列表顺序首个）
    assert "非单射" in (resolve_final_block.__doc__ or "")
