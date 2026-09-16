"""
闸门0 黄金样本回归集基线统计工具
================================
读 tests/fixtures/hallucination_golden/golden_v1.json，逐条以 default 档
跑 apply_source_filter，机械核对 expected 与实际行为，输出：

1. 每样本判定结果（PASS/FAIL + 期望/实际行为）；
2. 七类别 precision/recall 混淆计数（delete 类以"删除"为预测正例，
   count 类以"检出"为预测正例，keep 样本作为各类别的负例来源）；
3. 末尾打印可复制进决策日志的基线摘要。

用法：
    python tools/gate0_golden_stats.py
    python tools/gate0_golden_stats.py --json 结果.json   # 另存机器可读结果

退出码：脚本正常完成恒为 0（样本 FAIL 不改变退出码，行为一致性由
tests/test_golden_set.py 作为硬守卫钉住）。

防循环验证契约：golden_v1.json 的 expected 字段独立于实现代码写就
（先按规则库文档化阈值手工判定，后跑本脚本核对），本脚本只做机械
核对，不反推样本预期。
"""

import json
import shutil
import sys
import tempfile
import types
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine.source_hallucination import apply_source_filter

# ---------------------------------------------------------------------------
# 口径常量（与 subtransjav/refine/source_hallucination.py 的标签映射同源；
# 此处复制而非 import 私有符号，保持脚本对实现的只读独立性）
# ---------------------------------------------------------------------------

DELETE_CATS = ("pure_punctuation", "exclamation", "unpronounceable",
               "repeat_loop", "end_meta")
COUNT_CATS = ("isolated_response", "nonsense_syllables")
ALL_CATS = DELETE_CATS + COUNT_CATS

CATEGORY_LABELS = {
    "pure_punctuation": "纯标点行",
    "exclamation": "!串",
    "unpronounceable": "不可发音辅音串",
    "repeat_loop": "重复循环",
    "end_meta": "片尾元信息",
    "isolated_response": "孤立应答词",
    "nonsense_syllables": "无意义音节连缀",
}

ACTION_WORDS = {"delete": "删除", "count": "计数", "keep": "保留"}

GOLDEN_PATH = (Path(__file__).resolve().parents[1] / "tests" / "fixtures"
               / "hallucination_golden" / "golden_v1.json")


def load_golden(path=None) -> dict:
    """读黄金样本集（缺省走仓库内置路径）。"""
    p = Path(path) if path else GOLDEN_PATH
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("cases"), list) or not data["cases"]:
        raise SystemExit(f"黄金样本集损坏（无 cases）: {p}")
    return data


def _timing(sec: int) -> str:
    """条目时间轴：起始 sec 秒、时长 1 秒（间隔 20s 避免跨条目干扰）。"""
    m1, s1 = divmod(sec, 60)
    m2, s2 = divmod(sec + 1, 60)
    return (f"00:{m1:02d}:{s1:02d},000 --> "
            f"00:{m2:02d}:{s2:02d},000")


def build_entries(case: dict) -> tuple:
    """按样本构造条目序列，返回 (entries, focus_index)。

    条目 = context（focus 前）+ focus + context_after（focus 后）；
    时间轴按每条 20s 间隔生成，样本带 timing 时覆盖 focus 的时间轴。
    """
    before = list(case.get("context") or [])
    after = list(case.get("context_after") or [])
    texts = before + [case["text"]] + after
    entries = [{"index": i + 1, "timing": _timing(20 * i), "text": t}
               for i, t in enumerate(texts)]
    if case.get("timing"):
        entries[len(before)]["timing"] = case["timing"]
    return entries, len(before) + 1     # 条目编号从 1 起，focus 恒存在


def run_case(case: dict, errors_dir: str = None) -> dict:
    """以 default 档跑单条样本，机械核对期望与实际。

    判定口径：
    - delete：focus 被删除，且期望类别的删除计数 ≥1；
    - count：focus 保留在主稿，且期望类别的检出计数 ≥1；
    - keep：focus 保留在主稿，且全文件零检出（负样本硬口径）。
    """
    if errors_dir is None:
        errors_dir = tempfile.mkdtemp(prefix="gate0_golden_")
    entries, focus_index = build_entries(case)
    cfg = types.SimpleNamespace(v2_source_filter="default",
                                v2_source_filter_valve_pct=50)
    # 静默运行：逐条结果以返回值呈现，避免检测器的过程输出刷屏
    with redirect_stdout(StringIO()):
        kept, stats = apply_source_filter(entries, cfg,
                                          errors_dir=errors_dir)
    kept_indexes = {e["index"] for e in kept}
    deleted = focus_index not in kept_indexes
    expected_cat = case.get("expected_category")
    expected_action = case.get("expected_action")
    label = CATEGORY_LABELS.get(expected_cat) if expected_cat else None

    ok = True
    detail = ""
    if expected_action == "delete":
        cat_deleted = stats["categories"][label]["deleted"] if label else 0
        if not deleted or cat_deleted < 1:
            ok = False
            detail = (f"期望删除（{label}），实际"
                      f"{'已删除' if deleted else '未删除'}"
                      f"/该类删除计数 {cat_deleted}")
    elif expected_action == "count":
        cat_detected = stats["categories"][label]["detected"] if label else 0
        if deleted or cat_detected < 1:
            ok = False
            detail = (f"期望计数保留（{label}，检出≥1），实际"
                      f"{'被删除' if deleted else '保留'}"
                      f"/该类检出 {cat_detected}")
    elif expected_action == "keep":
        if deleted or stats.get("detected_total", 0) != 0:
            ok = False
            detail = (f"期望零检出保留，实际"
                      f"{'被删除' if deleted else '保留'}"
                      f"/全文件检出 {stats.get('detected_total', 0)}")
    else:
        ok = False
        detail = f"未知期望行为: {expected_action!r}"

    # 检测器在本样本上的"类别活跃度"（供混淆计数）：delete 类看删除、
    # count 类看检出。样本构造保证 context 不引入无关检出。
    predicted = set()
    for key in DELETE_CATS:
        if stats["categories"][CATEGORY_LABELS[key]]["deleted"] > 0:
            predicted.add(key)
    for key in COUNT_CATS:
        if stats["categories"][CATEGORY_LABELS[key]]["detected"] > 0:
            predicted.add(key)

    return {
        "id": case["id"],
        "expected_category": expected_cat,
        "expected_action": expected_action,
        "deleted": deleted,
        "detected_total": stats.get("detected_total", 0),
        "predicted": sorted(predicted),
        "ok": ok,
        "detail": detail,
    }


def confusion_matrix(golden: dict, results: list) -> dict:
    """七类别混淆计数（以样本为观测单位）：

    TP=预测为该类且期望为该类；FP=预测为该类但期望非该类（含负样本）；
    FN=期望为该类但未预测到。precision=TP/(TP+FP)、recall=TP/(TP+FN)，
    分母为 0 时记 1.000（该类无任何观测，视为无退化）。
    """
    by_id = {r["id"]: r for r in results}
    matrix = {}
    for key in ALL_CATS:
        tp = fp = fn = 0
        for case in golden["cases"]:
            r = by_id[case["id"]]
            hit = key in r["predicted"]
            expected = (case.get("expected_category") == key)
            if hit and expected:
                tp += 1
            elif hit and not expected:
                fp += 1
            elif expected and not hit:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0
        matrix[key] = {"tp": tp, "fp": fp, "fn": fn,
                       "precision": round(precision, 3),
                       "recall": round(recall, 3)}
    return matrix


def main() -> int:
    # Windows 控制台默认非 UTF-8 编码，摘要含中文/emoji，稳妥起见重配置
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    golden = load_golden()
    tmp = tempfile.mkdtemp(prefix="gate0_golden_")
    try:
        results = [run_case(case, errors_dir=tmp)
                   for case in golden["cases"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n_pass = sum(1 for r in results if r["ok"])
    print("=" * 64)
    print(f"闸门0 黄金样本回归集 v{golden.get('version')}："
          f"共 {len(results)} 条，PASS {n_pass} / FAIL {len(results) - n_pass}")
    print("=" * 64)
    print("-- 逐条判定 --")
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        word = ACTION_WORDS.get(r["expected_action"],
                                r["expected_action"])
        cat = r["expected_category"] or "-"
        line = f"[{mark}] {r['id']}  期望={word}/{cat}"
        if not r["ok"]:
            line += f"  {r['detail']}"
        print(line)

    matrix = confusion_matrix(golden, results)
    print("-- 类别 precision/recall（delete=删除为正例，count=检出为正例）--")
    for key in ALL_CATS:
        m = matrix[key]
        print(f"{CATEGORY_LABELS[key]:<10} TP={m['tp']} FP={m['fp']} "
              f"FN={m['fn']}  precision={m['precision']:.3f} "
              f"recall={m['recall']:.3f}")

    macro_p = sum(m["precision"] for m in matrix.values()) / len(matrix)
    macro_r = sum(m["recall"] for m in matrix.values()) / len(matrix)
    n_delete = sum(1 for c in golden["cases"]
                   if c["expected_action"] == "delete")
    n_count = sum(1 for c in golden["cases"]
                  if c["expected_action"] == "count")
    n_keep = sum(1 for c in golden["cases"]
                 if c["expected_action"] == "keep")
    print("-- 基线摘要（可复制进决策日志）--")
    print(f"golden_v{golden.get('version')} 样本 {len(results)} 条"
          f"（delete {n_delete} / count {n_count} / keep {n_keep}），"
          f"行为一致 {n_pass}/{len(results)}；"
          f"七类别宏平均 precision={macro_p:.3f} recall={macro_r:.3f}")
    print("类别明细："
          + "；".join(f"{CATEGORY_LABELS[k]}"
                      f"P={matrix[k]['precision']:.3f}"
                      f"/R={matrix[k]['recall']:.3f}" for k in ALL_CATS))

    if len(sys.argv) > 2 and sys.argv[1] == "--json":
        out = {"version": golden.get("version"),
               "pass": n_pass, "fail": len(results) - n_pass,
               "cases": results, "confusion": matrix}
        Path(sys.argv[2]).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘: {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
