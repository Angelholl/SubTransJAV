"""H6 黄金样本回归集测试：防闸门0 检测器行为退化的硬守卫。

防循环验证契约：golden_v1.json 的 expected 字段独立于实现代码写就
（先样本后核对，见 tests/fixtures/hallucination_golden/README.md），
本测试把"期望行为 == default 档实际行为"钉死；检测器阈值/语义回归时
此处必须显式失败并走黄金集版本解冻流程，禁止改期望值让测试通过。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import gate0_golden_stats as gs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = (Path(__file__).resolve().parent / "fixtures"
               / "hallucination_golden" / "golden_v1.json")
SCRIPT_PATH = REPO_ROOT / "tools" / "gate0_golden_stats.py"

_GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


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
                       capture_output=True, text=True, timeout=120,
                       cwd=str(REPO_ROOT), env=env)
    assert r.returncode == 0, r.stderr
    assert "基线摘要" in r.stdout
    assert f"共 {len(_GOLDEN['cases'])} 条" in r.stdout
