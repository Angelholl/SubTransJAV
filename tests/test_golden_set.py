"""H6 黄金样本回归集测试：防闸门0 检测器行为退化的硬守卫。

防循环验证契约：golden_v1.json 的 expected 字段独立于实现代码写就
（先样本后核对，见 tests/fixtures/hallucination_golden/README.md），
本测试把"期望行为 == default 档实际行为"钉死；检测器阈值/语义回归时
此处必须显式失败并走黄金集版本解冻流程，禁止改期望值让测试通过。
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import gate0_golden_stats as gs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = (Path(__file__).resolve().parent / "fixtures"
               / "hallucination_golden" / "golden_v1.json")
SEMANTIC_PATH = (Path(__file__).resolve().parent / "fixtures"
                 / "hallucination_golden" / "semantic_v1.json")
ANTONYM_PATH = (Path(__file__).resolve().parent / "fixtures"
                / "hallucination_golden" / "antonym_v1.json")
SCRIPT_PATH = REPO_ROOT / "tools" / "gate0_golden_stats.py"

_GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
_SEMANTIC = json.loads(SEMANTIC_PATH.read_text(encoding="utf-8"))
_ANTONYM = json.loads(ANTONYM_PATH.read_text(encoding="utf-8"))


def test_golden_version_frozen():
    """版本冻结契约：当前集为 1.0，禁止无声变更。"""
    assert _GOLDEN["version"] == "1.0"
    assert _GOLDEN["frozen"] is True


def test_golden_coverage_matrix():
    """覆盖底线：七类别正样本各 ≥3，近邻负样本（keep）≥10。"""
    cases = _GOLDEN["cases"]
    for cat in gs.ALL_CATS:
        n = sum(1 for c in cases
                if c.get("expected_category") == cat
                and c["expected_action"] in ("delete", "count"))
        assert n >= 3, f"类别 {cat} 正样本不足 3 条（现 {n}）"
    n_keep = sum(1 for c in cases if c["expected_action"] == "keep")
    assert n_keep >= 10, f"近邻负样本不足 10 条（现 {n_keep}）"
    # 每条样本必须带构造溯源字段（防无据样本膨胀，R7 同源约束）
    for c in cases:
        assert c.get("origin") == "constructed" and c.get("annotator") \
            and c.get("generated_by"), f"{c['id']} 缺少溯源字段"


@pytest.mark.parametrize("case", _GOLDEN["cases"], ids=lambda c: c["id"])
def test_golden_case_expected_behavior(case, tmp_path):
    """硬守卫：每条样本的期望行为必须与 default 档实际行为一致。"""
    result = gs.run_case(case, errors_dir=str(tmp_path))
    assert result["ok"], (
        f"{result['id']} 黄金样本失配：{result['detail']}。"
        f"请先区分检测器回归与规则演进，禁止直接改期望值。")


def test_stats_script_smoke():
    """统计脚本可执行（subprocess 真跑）且退出码 0、产出基线摘要。"""
    import os
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT_PATH)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120,
                       cwd=str(REPO_ROOT), env=env)
    assert r.returncode == 0, r.stderr
    assert "基线摘要" in r.stdout
    assert f"共 {len(_GOLDEN['cases'])} 条" in r.stdout


# ---------------------------------------------------------------------------
# D5 语义反转防护黄金集（semantic_v1.json）：乱码强译复核回归
# ---------------------------------------------------------------------------

# 源文含汉字判定（与管线 D5 口径一致：CJK 统一表意文字基本区）
_SRC_KANJI_RE = re.compile(r"[\u4e00-\u9fff]")


def test_semantic_version_frozen():
    """版本冻结契约：当前集为 1.0，禁止无声变更。"""
    assert _SEMANTIC["version"] == "1.0"
    assert _SEMANTIC["frozen"] is True


def test_semantic_coverage_matrix():
    """覆盖底线：正样本（必须进复核）≥4，负样本（不得进）≥4；
    每条样本必须带构造溯源字段（R7 同源约束）。"""
    cases = _SEMANTIC["cases"]
    n_pos = sum(1 for c in cases if c["expected_in_review"] is True)
    n_neg = sum(1 for c in cases if c["expected_in_review"] is False)
    assert n_pos >= 4, f"正样本不足 4 条（现 {n_pos}）"
    assert n_neg >= 4, f"负样本不足 4 条（现 {n_neg}）"
    for c in cases:
        assert c.get("origin") == "constructed" and c.get("annotator") \
            and c.get("generated_by"), f"{c['id']} 缺少溯源字段"


@pytest.mark.parametrize("case", _SEMANTIC["cases"], ids=lambda c: c["id"])
def test_semantic_case_expected_review(case):
    """D5 硬守卫：乱码源文×通顺中文→进复核清单；实义行/[未翻译]→不进。

    判定口径与管线终稿后复核候选一致：
    (strong_garble_signal 源文命中) AND is_fluent_zh(译文) AND 源文不含汉字
    """
    from subtransjav.refine.source_hallucination import (
        is_fluent_zh,
        strong_garble_signal,
    )
    src = case["source"]
    zh = case["translation"]
    hit = (strong_garble_signal(src) is not None
           and is_fluent_zh(zh)
           and not _SRC_KANJI_RE.search(src))
    assert hit == case["expected_in_review"], (
        f"{case['id']} 语义黄金集失配：期望 "
        f"expected_in_review={case['expected_in_review']}，实际 {hit}。"
        f"请先区分检测器回归与规则演进，禁止直接改期望值。")


# ---------------------------------------------------------------------------
# 批次 B2 反义误译黄金集（antonym_v1.json）：antonym_* 规则回归
# ---------------------------------------------------------------------------

def test_antonym_version_frozen():
    """版本冻结契约：当前集为 1.0，禁止无声变更。"""
    assert _ANTONYM["version"] == "1.0"
    assert _ANTONYM["frozen"] is True


def test_antonym_coverage_matrix():
    """覆盖底线：正样本（必须告警）≥3，负样本（不得告警）≥3；
    每条样本必须带构造溯源字段（R7 同源约束）。"""
    cases = _ANTONYM["cases"]
    n_pos = sum(1 for c in cases if c["expected_warn"] is True)
    n_neg = sum(1 for c in cases if c["expected_warn"] is False)
    assert n_pos >= 3, f"正样本不足 3 条（现 {n_pos}）"
    assert n_neg >= 3, f"负样本不足 3 条（现 {n_neg}）"
    for c in cases:
        assert c.get("origin") == "constructed" and c.get("annotator") \
            and c.get("generated_by"), f"{c['id']} 缺少溯源字段"


@pytest.mark.parametrize("case", _ANTONYM["cases"], ids=lambda c: c["id"])
def test_antonym_case_expected_warn(case):
    """B2 硬守卫：双侧锚定（源文命中指定形态 AND 译文命中目标集）→ 告警；
    任一侧不成立 → 不告警。判定口径与 check_and_fix_translation_errors
    的 antonym_* 检测一致（compiled 规则逐条判定，warn_only 不改文本）。"""
    from subtransjav.refine.post_validate import _get_compiled
    compiled = _get_compiled()
    src = case["source"]
    zh = case["translation"]
    hit = any(rule["source"].search(src) and rule["target"].search(zh)
              for name, rule in compiled.items()
              if name.startswith("antonym_"))
    assert hit == case["expected_warn"], (
        f"{case['id']} 反义黄金集失配：期望 "
        f"expected_warn={case['expected_warn']}，实际 {hit}。"
        f"请先区分检测器回归与规则演进，禁止直接改期望值。")
