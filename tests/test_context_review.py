# -*- coding: utf-8 -*-
"""
上下文预审工具单测（全离线，不联网、不调真 LLM）
================================================
覆盖 tools/context_review.py：
1. 证据核验（窗口内/外、容差、缺字段、非类别1）；
2. 核验失败降级语义；
3. 排序（多片×多类别×沉底×相似度×时间轴、类别5尾块、影片两级）；
4. 头部统计块；
5. 抽样（可复现、跨片均匀、N 不足全取）；
6. dry-run 端到端（tmp_path mini 目录 → 增强 CSV 结构）；
7. LLM 解析（fake client：正常 JSON / 带 fence / 缺行）；
8. R2 理由→选边矛盾检测（#46 复刻 FAIL / 双侧支持 LOW_FLAG）；
9. 提示词 A/B 双版本（A 无终稿与 final_cn、B 保留 + 强制声明、
   R3/R4 规则与 few-shot、版本端到端审计留痕）。
"""

import csv
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import context_review as cr

_id_seq = itertools.count(1)


# ---------------------------------------------------------------------------
# 测试数据构造
# ---------------------------------------------------------------------------

def srt_text(n: int, start: float = 0.0, step: float = 4.0,
             prefix: str = "テキスト") -> str:
    """生成 n 条顺序 SRT（每条时长 3s、间隔 step）。"""
    out = []
    for i in range(1, n + 1):
        s = start + (i - 1) * step
        e = s + 3.0
        out.append(
            f"{i}\n{_fmt(s)} --> {_fmt(e)}\n{prefix}{i}\n")
    return "\n".join(out)


def _fmt(sec: float) -> str:
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_csv(path: Path, rows: list, label: str = "demo.ja.merged.subtransjav.srt") -> None:
    """写一个 mini 分歧复核 CSV。rows: [(分组, 时间轴, 相似度, pass1, pass2, final)]。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cr.ORIGINAL_COLUMNS)
        for grp, timing, sim, p1, p2, fin in rows:
            w.writerow([label, grp, timing, f"{sim:.4f}", "0.5000", "1.000",
                        "否", p1, p2, fin])


def make_row(grp: str = "必看", timing: str = "00:00:20,000 --> 00:00:23,000",
             sim: float = 0.10, cat: int = 4, verify: str = "N/A",
             conf: str = "low", film: str = "a.csv") -> dict:
    """构造带结果字段的内存行（排序/统计测试用）。id 全局唯一自增。"""
    r = {k: "" for k in cr.ORIGINAL_COLUMNS}
    r.update({"文件名": "demo", "分组": grp, "时间轴": timing,
              "相似度": f"{sim:.4f}", "final_cn译文": "终稿",
              "pass1": "あ", "pass2": "い", "artifact": "否",
              "id": f"{film}#{next(_id_seq)}", "_film": film,
              "_sim": sim, "_start": 20.0,
              "_category": cat, "_verify": verify, "_confidence": conf,
              "_verdict": {"category": cat, "side": None,
                           "evidence_timing": "", "evidence_pass": None,
                           "reason": "r", "confidence": conf}})
    return r


class FakeClient:
    """LLM fake：预置回复内容（自动携带 1..n 行序号），记录 prompt。"""

    def __init__(self, content_fn=None, usage=None):
        self.content_fn = content_fn      # f(n_rows, prompt) -> str
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 50}
        self.calls = []

    def chat(self, system_text: str, user_text: str):
        n = user_text.count("### 第")
        self.calls.append(user_text)
        return self.content_fn(n, user_text), dict(self.usage)

    def list_models(self):
        return ["fake-model"]


def verdict_json(row_no: int, **kw) -> str:
    item = {"row": row_no, "category": kw.get("category", 4),
            "side": kw.get("side"), "evidence_timing": kw.get("evidence_timing"),
            "evidence_pass": kw.get("evidence_pass"),
            "reason": kw.get("reason", "理由"), "confidence": kw.get("confidence", "low")}
    return json.dumps([item], ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. 证据核验
# ---------------------------------------------------------------------------

class TestVerifyEvidence:
    """窗口内存在→PASS；窗口外→FAIL；容差；缺字段；非类别1→N/A。"""

    def setup_method(self):
        self.win = [
            {"index": i, "timing": f"00:00:{10 + i:02d},000 --> 00:00:{10 + i:02d},900",
             "text": f"行{i}"} for i in range(1, 12)]
        self.ctx = {"pass1": self.win, "pass2": self.win, "final_cn": self.win}

    def _v(self, **kw):
        base = {"category": 1, "side": "pass1", "evidence_timing": "00:00:12,000",
                "evidence_pass": "pass1", "reason": "r", "confidence": "high"}
        base.update(kw)
        return base

    def test_in_window_pass(self):
        assert cr.verify_evidence(self._v(), self.ctx) == "PASS"

    def test_out_of_window_fail(self):
        """窗口 11 行（中心=目标行），引到差 6 行开外的时间轴 → FAIL。"""
        v = self._v(evidence_timing="00:00:40,000")
        assert cr.verify_evidence(v, self.ctx) == "FAIL"

    def test_tolerance_1s(self):
        """时间戳与窗口行 start 差 ≤1.0s 算存在；>1.0s FAIL。"""
        # 窗口行 start 序列 = 00:00:11,000 ... 00:00:21,000
        assert cr.verify_evidence(
            self._v(evidence_timing="00:00:15,900"), self.ctx) == "PASS"  # 15.0±0.9
        assert cr.verify_evidence(
            self._v(evidence_timing="00:00:22,100"), self.ctx) == "FAIL"  # 距 21.0 差 1.1s

    def test_missing_fields_fail(self):
        for kw in ({"side": None}, {"evidence_timing": ""},
                   {"evidence_pass": None}):
            assert cr.verify_evidence(self._v(**kw), self.ctx) == "FAIL"

    def test_other_categories_na(self):
        for cat in (2, 3, 4, 5):
            assert cr.verify_evidence(self._v(category=cat), self.ctx) == "N/A"

    def test_two_timestamps_uses_first(self):
        """两时间戳都给时按第一个（start）匹配。"""
        v = self._v(evidence_timing="00:00:12,000 --> 00:00:99,999")
        assert cr.verify_evidence(v, self.ctx) == "PASS"

    def test_timestamp_formats(self):
        for ts in ("00:00:12,000", "00:00:12.000", "00:00:12"):
            assert cr.parse_evidence_timestamps(ts) == pytest.approx([12.0])


class TestCitedFragmentVerification:
    """R3 引用文本真实性核验（v1 收尾）。

    回归背景：微调轮出现编造引用——reason 引「お前の運動神経の良さを
    見て」并给出真实存在的时间轴，但该 pass 该行实为「…旦那様かっこ。」，
    骗过时间轴核验得到 PASS。本组测试将其锁定为固定回归样本。
    """

    FAKE_CITE = "お前の運動神経の良さを見て"

    def _ctx(self, target_text):
        win = [{"index": i,
                "timing": "00:08:22,560 --> 00:08:26,490" if i == 6
                else f"00:08:{10 + i:02d},000 --> 00:08:{10 + i:02d},900",
                "text": ("other line" if i != 6 else target_text)}
               for i in range(1, 12)]
        return {"pass1": win, "pass2": win, "final_cn": win}

    def _v(self, reason):
        return {"category": 1, "side": "pass1",
                "evidence_pass": "pass1",
                "evidence_timing": "00:08:22,56 --> 00:08:26,49",
                "reason": reason, "confidence": "high"}

    def test_regression_fabricated_quote_fail(self):
        """回归样本：时间轴真实但引用文本不在窗口 → 整体 FAIL。"""
        reason = (f"pass1 '{self.FAKE_CITE}' 与终稿 '看看你出色的运动神经'"
                  " 对应，且时间轴匹配")
        assert cr.verify_evidence(self._v(reason),
                                  self._ctx("…旦那様かっこ。")) == "FAIL"

    def test_regression_quote_present_pass(self):
        """对照：同一 reason，窗口行确实含该引文 → PASS。"""
        reason = (f"pass1 '{self.FAKE_CITE}' 与终稿 '看看你出色的运动神经'"
                  " 对应，且时间轴匹配")
        assert cr.verify_evidence(self._v(reason),
                                  self._ctx(self.FAKE_CITE)) == "PASS"

    def test_no_quote_reason_pass(self):
        """无引文 reason（纯说明文字）→ 不触发，时间轴通过则 PASS。"""
        reason = "pass1 语义与上下文一致，衔接自然，时间轴匹配"
        assert cr.verify_evidence(self._v(reason),
                                  self._ctx("…旦那様かっこ。")) == "PASS"

    def test_quote_with_ellipsis_prefix_hit(self):
        """窗口行带「…」省略号前缀，归一化剥除后命中 → 不 FAIL。"""
        reason = f"pass1「…{self.FAKE_CITE}」与上下文衔接自然"
        assert cr.verify_evidence(self._v(reason),
                                  self._ctx(f"…{self.FAKE_CITE}。")) == "PASS"

    def test_pure_chinese_quote_ignored(self):
        """纯中文引文（无假名）不参与核验，不在窗口也不 FAIL。"""
        reason = "reason 引用 “看看你出色的运动神经” 对应终稿"
        assert cr.verify_evidence(self._v(reason),
                                  self._ctx("…旦那様かっこ。")) == "PASS"

    def test_short_fragment_ignored(self):
        """<4 字符短片段（如「を」）不触发核验。"""
        assert cr.extract_cited_jp_fragments("引「を」") == []
        reason = "pass1「を」处衔接自然，时间轴匹配"
        assert cr.verify_evidence(self._v(reason),
                                  self._ctx("…旦那様かっこ。")) == "PASS"

    def test_long_quote_skipped(self):
        """>40 字符长引文视为概括转述，跳过核验。"""
        long_q = "とても長い引用文" * 6  # 48 字符，含假名
        assert cr.extract_cited_jp_fragments(f"引「{long_q}」") == []
        assert cr.verify_evidence(self._v(f"引「{long_q}」"),
                                  self._ctx("…旦那様かっこ。")) == "PASS"

    def test_unpaired_quote_not_extracted(self):
        """未配对引号不提取。"""
        assert cr.extract_cited_jp_fragments(
            f"pass1「{self.FAKE_CITE} 与终稿不一致") == []

    def test_extract_basic_pairs(self):
        """四种配对引号均可提取含假名片段。"""
        for q in (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’")):
            frags = cr.extract_cited_jp_fragments(f"a{q[0]}{self.FAKE_CITE}{q[1]}b")
            assert frags == [self.FAKE_CITE]


# ---------------------------------------------------------------------------
# 2. 降级语义
# ---------------------------------------------------------------------------

class TestDegrade:
    def test_fail_keeps_category_lowers_confidence(self):
        v = {"category": 1, "side": "pass1", "evidence_timing": "x",
             "evidence_pass": "pass1", "reason": "r", "confidence": "high"}
        out = cr.apply_review(v, "FAIL")
        assert out["category"] == 1            # 类别不变
        assert out["verify"] == "FAIL"
        assert out["confidence"] == "low"      # 置信强制 LOW

    def test_pass_keeps_confidence(self):
        v = {"category": 1, "side": "pass1", "evidence_timing": "x",
             "evidence_pass": "pass1", "reason": "r", "confidence": "high"}
        out = cr.apply_review(v, "PASS")
        assert out["confidence"] == "high" and out["verify"] == "PASS"

    def test_fail_row_not_in_high_confidence(self):
        rows = [
            make_row(cat=1, verify="PASS", film="a.csv"),
            make_row(cat=1, verify="FAIL", film="a.csv"),
            make_row(cat=2, verify="FAIL", film="a.csv"),
            make_row(cat=2, verify="N/A", film="a.csv"),
        ]
        assert cr.high_confidence_count(rows) == 1   # 仅第 1 行


# ---------------------------------------------------------------------------
# 3. 排序
# ---------------------------------------------------------------------------

class TestSorting:
    def test_row_sort_key_full_order(self):
        """类别优先 → 核验沉底 → 相似度升序 → 时间轴。"""
        r1 = make_row(cat=1, verify="PASS", sim=0.20, film="f.csv")
        r2 = make_row(cat=1, verify="FAIL", sim=0.05, film="f.csv")  # 沉底
        r3 = make_row(cat=2, verify="PASS", sim=0.01, film="f.csv")
        r4 = make_row(cat=4, verify="N/A", sim=0.30, film="f.csv")
        r5 = make_row(cat=5, verify="N/A", sim=0.00, film="f.csv")
        ordered = sorted([r5, r4, r3, r2, r1], key=cr.row_sort_key)
        assert [r["_category"] for r in ordered] == [1, 1, 2, 4, 5]
        assert ordered[0] is r1 and ordered[1] is r2   # 类内 PASS 在 FAIL 前

    def test_sort_output_rows_cat5_tail_block(self):
        rows = [make_row(cat=5, film="a.csv"), make_row(cat=2, film="a.csv"),
                make_row(cat=3, film="a.csv")]
        stats = [cr.compute_film_stat("a.csv", rows, {r["id"] for r in rows})]
        out = cr.sort_output_rows(rows, stats)
        assert out[-1]["_category"] == 5
        assert out[-2]["__comment__"] == cr.CATEGORY5_HEADER
        assert [r["_category"] for r in out[:2]] == [2, 3]

    def test_film_order_by_true_problem_rate(self):
        """全覆盖审核：按估计真问题率降序。"""
        f1_rows = [make_row(cat=1, film="f1.csv"), make_row(cat=4, film="f1.csv")]
        f2_rows = [make_row(cat=5, film="f2.csv"), make_row(cat=4, film="f2.csv")]
        s1 = cr.compute_film_stat("f1.csv", f1_rows, {r["id"] for r in f1_rows})
        s2 = cr.compute_film_stat("f2.csv", f2_rows, {r["id"] for r in f2_rows})
        # make_row 全为必看：两片 must_total=2 且 must_done==must_total（全覆盖）
        assert s1["must_total"] == s2["must_total"] == 2
        assert s1["true_problem_rate"] == 0.5   # 类别1 一行
        assert s2["true_problem_rate"] == 0.0   # 类别1/2 均 0
        ordered = sorted([s2, s1], key=cr.film_sort_key)
        assert [s["name"] for s in ordered] == ["f1.csv", "f2.csv"]

    def test_film_order_by_must_total_when_partial(self):
        """未审核完（抽样模式）：按该片必看行总量降序。"""
        s_small = {"name": "small.csv", "must_total": 5, "must_done": 2,
                   "cat_counts": {}, "true_problem_rate": 0.4}
        s_big = {"name": "big.csv", "must_total": 50, "must_done": 3,
                 "cat_counts": {}, "true_problem_rate": 0.1}
        ordered = sorted([s_small, s_big], key=cr.film_sort_key)
        assert [s["name"] for s in ordered] == ["big.csv", "small.csv"]

    def test_multi_film_multi_category(self):
        """多片×多类别集成：片序（按总量）→ 类别 → 沉底 → 相似度。"""
        fa = [make_row(cat=1, verify="PASS", sim=0.2, film="a.csv"),
              make_row(cat=1, verify="FAIL", sim=0.1, film="a.csv")]
        fb = [make_row(cat=2, verify="N/A", sim=0.3, film="b.csv"),
              make_row(cat=1, verify="PASS", sim=0.05, film="b.csv")]
        sa = {"name": "a.csv", "must_total": 10, "must_done": 2,
              "cat_counts": {1: 2, 2: 0, 3: 0, 4: 0, 5: 0},
              "true_problem_rate": 0.2}
        sb = {"name": "b.csv", "must_total": 5, "must_done": 2,
              "cat_counts": {1: 1, 2: 1, 3: 0, 4: 0, 5: 0},
              "true_problem_rate": 0.4}
        out = cr.sort_output_rows(fa + fb, sorted([sb, sa], key=cr.film_sort_key))
        films = [r["_film"] for r in out if "__comment__" not in r]
        assert films == ["a.csv"] * 2 + ["b.csv", "b.csv"]
        b_part = [r for r in out if "__comment__" not in r and r["_film"] == "b.csv"]
        assert [r["_category"] for r in b_part] == [1, 2]


# ---------------------------------------------------------------------------
# 4. 统计块
# ---------------------------------------------------------------------------

class TestStats:
    def test_compute_film_stat(self):
        rows = [
            make_row(grp="必看", cat=1, film="a.csv"),
            make_row(grp="必看", cat=2, film="a.csv"),
            make_row(grp="必看", cat=5, film="a.csv"),
            make_row(grp="必看", cat=1, film="a.csv"),   # 未抽中
            make_row(grp="可选", cat=4, film="a.csv"),
        ]
        reviewed = {r["id"] for r in rows[:3] + rows[4:]}
        st = cr.compute_film_stat("a.csv", rows, reviewed)
        assert st["must_total"] == 4
        assert st["must_done"] == 3
        assert st["cat_counts"] == {1: 1, 2: 1, 3: 0, 4: 1, 5: 1}
        assert st["true_problem_rate"] == pytest.approx(2 / 4)

    def test_build_stat_block(self):
        rows = [make_row(cat=1, verify="PASS", film="a.csv"),
                make_row(cat=1, verify="FAIL", film="a.csv"),
                make_row(cat=2, verify="N/A", film="a.csv")]
        st = cr.compute_film_stat("a.csv", rows, {r["id"] for r in rows})
        lines = cr.build_stat_block([st], rows)
        assert all(ln.startswith("#") for ln in lines)
        assert "必看行 3" in lines[1]
        assert "类别1=2 类别2=1" in lines[1]
        assert "估计真问题率 100.0%" in lines[1]
        assert "高置信行数 1" in lines[2]     # 类别1/2 且 PASS 仅 1 行


# ---------------------------------------------------------------------------
# 5. 抽样
# ---------------------------------------------------------------------------

class TestSampling:
    def _films(self):
        a = [make_row(sim=s, film="a.csv") for s in (0.1, 0.2, 0.3)]
        b = [make_row(sim=s, film="b.csv") for s in (0.15, 0.25)]
        for r in a + b:
            r["id"] = f"{r['_film']}#{r['_sim']}"
        return [("a.csv", a), ("b.csv", b)]

    def test_reproducible(self):
        p1 = cr.sample_rows(self._films(), 3, seed=1)
        p2 = cr.sample_rows(self._films(), 3, seed=2)   # 确定性：种子无关
        assert sorted(p1) == sorted(p2)
        assert len(p1) == 3

    def test_round_robin_uniform(self):
        """每片轮流取 1 行：5 片行抽 3 → a 取 2、b 取 1。"""
        picked = cr.sample_rows(self._films(), 3)
        films = [r["_film"] for r in picked.values()]
        assert sorted(films) == ["a.csv", "a.csv", "b.csv"]

    def test_consumes_lowest_similarity_first(self):
        """片内按相似度升序消费（先取最分歧行）。"""
        picked = cr.sample_rows(self._films(), 4)
        a_sims = sorted(r["_sim"] for r in picked.values() if r["_film"] == "a.csv")
        assert a_sims == [0.1, 0.2]

    def test_n_exceeds_total_takes_all(self):
        picked = cr.sample_rows(self._films(), 99)
        assert len(picked) == 5

    def test_n_zero_empty(self):
        assert cr.sample_rows(self._films(), 0) == {}


# ---------------------------------------------------------------------------
# 7. LLM 解析（fake client 注入）
# ---------------------------------------------------------------------------

class TestParseVerdicts:
    def test_normal_json(self):
        text = json.dumps([
            {"row": 1, "category": 1, "side": "pass1",
             "evidence_timing": "00:00:12,000", "evidence_pass": "pass1",
             "reason": "上文衔接", "confidence": "high"},
            {"row": 2, "category": 5, "side": None, "evidence_timing": None,
             "evidence_pass": None, "reason": "详略", "confidence": "medium"},
        ], ensure_ascii=False)
        out, fails = cr.parse_verdicts(text, 2)
        assert fails == 0
        assert out[1]["category"] == 1 and out[1]["side"] == "pass1"
        assert out[2]["category"] == 5

    def test_code_fence(self):
        text = "```json\n" + verdict_json(1, category=2) + "\n```"
        out, fails = cr.parse_verdicts(text, 1)
        assert fails == 0 and out[1]["category"] == 2

    def test_missing_row_filled_cat4(self):
        """某行缺失 → 补类别4/LOW，计 parse_fail。"""
        text = verdict_json(1, category=3)
        out, fails = cr.parse_verdicts(text, 2)
        assert fails == 1
        assert out[2] == {"category": 4, "side": None, "evidence_timing": "",
                          "evidence_pass": None, "reason": "",
                          "confidence": "low"}

    def test_invalid_fields_dropped(self):
        """字段非法（category 越界/side 非法）→ 该条丢弃按缺行补位。"""
        text = json.dumps([
            {"row": 1, "category": 9, "side": "pass1", "evidence_timing": "",
             "evidence_pass": None, "reason": "", "confidence": "low"},
            {"row": 2, "category": 1, "side": "passX", "evidence_timing": "",
             "evidence_pass": None, "reason": "", "confidence": "low"},
        ])
        out, fails = cr.parse_verdicts(text, 2)
        assert fails == 2

    def test_no_array(self):
        out, fails = cr.parse_verdicts("模型跑题了没有数组", 1)
        assert fails == 1 and out[1]["category"] == 4


class TestReviewBatch:
    def test_fake_client_ok(self):
        """fake client 注入 + 正常解析 + usage 记录。"""
        def content(n, prompt):
            return json.dumps([
                {"row": i, "category": 3, "side": None, "evidence_timing": None,
                 "evidence_pass": None, "reason": "错位", "confidence": "medium"}
                for i in range(1, n + 1)], ensure_ascii=False)
        client = FakeClient(content_fn=content)
        ctxs = [{"row": make_row(), "pass1": [], "pass2": [], "final_cn": []}
                for _ in range(3)]
        verdicts, usage = cr.review_batch_with_client(client, ctxs, retries=0)
        assert len(verdicts) == 3 and usage["parse_fail"] == 0
        assert usage["prompt_tokens"] == 100 and usage["rows"] == 3
        assert "elapsed_sec" in usage

    def test_bad_content_counts_parse_fail(self):
        client = FakeClient(content_fn=lambda n, p: "跑题文本")
        ctxs = [{"row": make_row()} for _ in range(2)]
        verdicts, usage = cr.review_batch_with_client(client, ctxs, retries=0)
        assert usage["parse_fail"] == 2
        assert all(v["category"] == 4 for v in verdicts.values())


# ---------------------------------------------------------------------------
# 6. dry-run 端到端
# ---------------------------------------------------------------------------

def build_mini_dir(tmp: Path, name: str = "demo",
                   timings: list = None) -> Path:
    """tmp 下构造 1 片 mini 数据（CSV + pass1/pass2/final_cn 各 12 行）。

    命名与生产一致：merged={name}.ja.merged.subtransjav.srt，
    CSV={name}.ja.merged.subtransjav_分歧复核.csv，
    pass1/pass2={name}.ja.pass1.srt / {name}.ja.pass2.srt，
    final_cn={name}.ja.merged.subtransjav_final_cn.srt。
    """
    d = tmp / name
    d.mkdir(parents=True)
    p1 = srt_text(12, prefix="パス１_")
    p2 = srt_text(12, prefix="パス２_")
    fin = srt_text(12, prefix="中文_")
    (d / f"{name}.ja.pass1.srt").write_text(p1, encoding="utf-8")
    (d / f"{name}.ja.pass2.srt").write_text(p2, encoding="utf-8")
    (d / f"{name}.ja.merged.subtransjav.srt").write_text(p1, encoding="utf-8")
    (d / f"{name}.ja.merged.subtransjav_final_cn.srt").write_text(fin, encoding="utf-8")
    # 分歧行 3 条必看（时间轴对应 SRT 第 4/5/6 条）+ 1 可选 + 1 已过滤
    timings = timings or [
        "00:00:12,000 --> 00:00:15,000",
        "00:00:16,000 --> 00:00:19,000",
        "00:00:20,000 --> 00:00:23,000",
    ]
    rows = [("必看", t, 0.05 * (i + 1), f"候{i}甲", f"候{i}乙", f"译{i}")
            for i, t in enumerate(timings)]
    rows.append(("可选", "00:00:24,000 --> 00:00:27,000", 0.40, "可甲", "可乙", "可译"))
    rows.append(("已过滤", "00:00:28,000 --> 00:00:31,000", 0.01, "滤甲", "滤乙", "滤译"))
    csv_path = d / f"{name}.ja.merged.subtransjav{cr.CSV_SUFFIX}"
    write_csv(csv_path, rows, label=f"{name}.ja.merged.subtransjav.srt")
    return d


class TestDryRunEndToEnd:
    def test_guard_output_rejects_outside_repo(self, tmp_path, capsys):
        """--output 越界路径（项目外且非系统临时目录）须被安全闸拒绝。"""
        d = build_mini_dir(tmp_path)          # 合法输入（tmp_path 系统临时域）
        outside = Path("D:/_ctx_review_guard_should_reject") / "x.csv"
        rc = cr.main([str(d), "--dry-run", "--output", str(outside)])
        assert rc != 0
        assert "越界" in capsys.readouterr().out
        assert not outside.exists()

    def test_directory_dry_run(self, tmp_path, capsys):
        d = build_mini_dir(tmp_path)
        out = d / "上下文预审_复核_增强_test.csv"
        rc = cr.main([str(d), "--dry-run", "--output", str(out)])
        captured = capsys.readouterr().out
        assert rc == 0
        assert out.is_file()
        assert (Path(str(out) + ".usage.json")).is_file()
        # usage sidecar 结构
        side = json.loads((Path(str(out) + ".usage.json")).read_text(encoding="utf-8"))
        assert side["summary"]["total_rows"] == 3
        assert side["calls"][0]["note"] == "dry-run"
        # 读回 CSV
        with open(out, encoding="utf-8-sig", newline="") as f:
            recs = list(csv.reader(f))
        assert all(r[0].startswith("#") for r in recs[:len(recs) - 4])
        header = recs[-4]
        assert header == cr.ORIGINAL_COLUMNS + cr.EXTRA_COLUMNS
        data = recs[-3:]
        assert len(data) == 3                    # 行数=必看行数
        assert all(r[10] == "4 无法判断" for r in data)   # dry-run 类别4
        assert all(r[13] == "N/A" for r in data)         # 核验 N/A
        assert all(r[14] == "low" for r in data)
        assert all(r[15] == "dry-run" for r in data)
        # 排序：相似度升序
        sims = [float(r[3]) for r in data]
        assert sims == sorted(sims)
        # stdout 汇总
        assert "性能汇总" in captured

    def test_single_csv_default_output_name(self, tmp_path):
        d = build_mini_dir(tmp_path)
        csv_path = next(d.glob(f"*{cr.CSV_SUFFIX}"))
        rc = cr.main([str(csv_path), "--dry-run"])
        expected = d / (csv_path.name[:-len(cr.CSV_SUFFIX)] + "_复核_增强.csv")
        assert rc == 0 and expected.is_file()

    def test_similarity_max_filter(self, tmp_path):
        d = build_mini_dir(tmp_path)
        out = d / "filtered.csv"
        rc = cr.main([str(d), "--dry-run", "--similarity-max", "0.06",
                      "--output", str(out)])
        assert rc == 0
        # 过滤行为测试通用谓词（相对某行）：统计块 + 表头 + 数据行
        def is_data(r):
            return bool(r) and not r[0].startswith("#") and r[0] != "文件名"
        with open(out, encoding="utf-8-sig", newline="") as f:
            recs = list(csv.reader(f))
        data = [r for r in recs if is_data(r)]
        assert len(data) == 1 and float(data[0][3]) <= 0.06

    def test_group_optional(self, tmp_path):
        d = build_mini_dir(tmp_path)
        out = d / "opt.csv"
        rc = cr.main([str(d), "--dry-run", "--group", "optional",
                      "--output", str(out)])
        assert rc == 0
        with open(out, encoding="utf-8-sig", newline="") as f:
            recs = list(csv.reader(f))
        data = [r for r in recs if r and not r[0].startswith("#")
                and r[0] != "文件名"]
        assert len(data) == 1 and data[0][1] == "可选"

    def test_sample_dry_run(self, tmp_path):
        d = build_mini_dir(tmp_path)
        out = d / "sampled.csv"
        rc = cr.main([str(d), "--dry-run", "--sample", "2", "--seed", "7",
                      "--output", str(out)])
        assert rc == 0
        with open(out, encoding="utf-8-sig", newline="") as f:
            recs = list(csv.reader(f))
        data = [r for r in recs if r and not r[0].startswith("#")
                and r[0] != "文件名"]
        assert len(data) == 2

    def test_no_input_found(self, tmp_path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        rc = cr.main([str(empty), "--dry-run"])
        assert rc == 1
        assert "未找到" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 杂项纯函数
# ---------------------------------------------------------------------------

class TestMisc:
    def test_collect_review_csvs_no_recursion(self, tmp_path):
        (tmp_path / "a_分歧复核.csv").write_text("", encoding="utf-8-sig")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b_分歧复核.csv").write_text("", encoding="utf-8-sig")
        found = cr.collect_review_csvs(tmp_path)
        assert [p.name for p in found] == ["a_分歧复核.csv"]

    def test_collect_single_file(self, tmp_path):
        p = tmp_path / "x_分歧复核.csv"
        p.write_text("", encoding="utf-8-sig")
        assert cr.collect_review_csvs(p) == [p]

    def test_sibling_paths_for_csv(self, tmp_path):
        csv_path = tmp_path / "sample_movie_abc123.ja.merged.subtransjav_分歧复核.csv"
        sibs = cr.sibling_paths_for_csv(csv_path)
        assert sibs["pass1"] == tmp_path / "sample_movie_abc123.ja.pass1.srt"
        assert sibs["pass2"] == tmp_path / "sample_movie_abc123.ja.pass2.srt"
        assert sibs["final_cn"] == \
            tmp_path / "sample_movie_abc123.ja.merged.subtransjav_final_cn.srt"

    def test_usage_summary(self):
        recs = [
            {"rows": 10, "prompt_tokens": 1000, "completion_tokens": 130,
             "elapsed_sec": 60.0, "parse_fail": 0},
            {"rows": 5, "prompt_tokens": 500, "completion_tokens": 65,
             "elapsed_sec": 30.0, "parse_fail": 1},
        ]
        s = cr.usage_summary(recs)
        assert s["total_rows"] == 15 and s["total_calls"] == 2
        assert s["total_prompt_tokens"] == 1500
        assert s["total_completion_tokens"] == 195
        assert s["tokens_per_row"] == pytest.approx(1695 / 15, abs=0.1)
        assert s["total_elapsed_sec"] == pytest.approx(90.0)
        assert s["avg_sec_per_row"] == pytest.approx(6.0)

    def test_format_evidence(self):
        v = {"category": 1, "side": "pass1",
             "evidence_timing": "00:01:23,456", "evidence_pass": "pass1"}
        assert cr._format_evidence(v) == "pass1@00:01:23,456"
        assert cr._format_evidence({**v, "category": 3}) == ""

    def test_build_prompt_contains_context(self):
        win = [{"index": 5, "timing": "00:00:20,000 --> 00:00:23,000",
                "text": "中文_5"}]
        ctx = {"row": make_row(), "pass1": win, "pass2": win, "final_cn": win}
        p = cr.build_prompt([ctx])
        for kw in ("五分类", "单侧可修", "双侧不可靠", "错配存疑",
                   "无法判断", "同义详略", "会いたかった", "严格 JSON"):
            assert kw in p, f"提示词缺关键词: {kw}"
        assert "中文_5" in p and "00:00:20,000" in p


# ---------------------------------------------------------------------------
# 8. R2 理由→选边矛盾检测
# ---------------------------------------------------------------------------

def _evidence_ctx() -> dict:
    """证据核验用上下文窗口（与 TestVerifyEvidence 同构）。"""
    win = [{"index": i,
            "timing": f"00:00:{10 + i:02d},000 --> 00:00:{10 + i:02d},900",
            "text": f"行{i}"} for i in range(1, 12)]
    return {"pass1": win, "pass2": win, "final_cn": win}


class TestReasonContradiction:
    """R2：理由与选边明确矛盾 → FAIL；双侧均支持 → LOW_FLAG + 复核标记。"""

    def _v(self, **kw):
        base = {"category": 1, "side": "pass1", "evidence_timing": "00:00:12,000",
                "evidence_pass": "pass1", "reason": "r", "confidence": "high"}
        base.update(kw)
        return base

    def test_case46_replica_fail(self):
        """轮1 #46 复刻：理由通篇支持 pass1 却输出 side=pass2 → FAIL。"""
        v = self._v(side="pass2", evidence_pass="pass2",
                    reason="pass1 与上下文语义连贯，时间轴对齐，为完整句。"
                           "pass2 为碎片，无法与上下文对应。")
        assert cr._reason_contradiction(v) == "FAIL"
        # 证据时间轴本身在 pass2 窗口内真实存在，仍因矛盾判 FAIL
        assert cr.verify_evidence(v, _evidence_ctx()) == "FAIL"

    def test_contradiction_lowers_confidence_keeps_category(self):
        """矛盾 FAIL 走既有降级语义：类别不变、置信强制 LOW。"""
        v = self._v(side="pass2", evidence_pass="pass2",
                    reason="pass1 正确，与上下文一致。")
        out = cr.apply_review(v, cr.verify_evidence(v, _evidence_ctx()))
        assert out["category"] == 1 and out["verify"] == "FAIL"
        assert out["confidence"] == "low"

    def test_normal_side_reason_unaffected(self):
        """正常行（理由支持所选侧）→ 不触发，核验仍 PASS。"""
        v = self._v(reason="pass1 与上下文语义连贯，时间轴对齐。")
        assert cr._reason_contradiction(v) == ""
        assert cr.verify_evidence(v, _evidence_ctx()) == "PASS"

    def test_opposite_side_negative_clause_not_fail(self):
        """理由提及另一侧但为否定/错位描述（非支持性）→ 不误伤。"""
        v = self._v(reason="pass1 与上下文语义连贯；pass2 为相邻行内容，"
                           "时间轴错位。")
        assert cr._reason_contradiction(v) == ""
        assert cr.verify_evidence(v, _evidence_ctx()) == "PASS"

    def test_both_sides_low_flag(self):
        """理由对双侧均给出支持性描述、无法区分强度 → LOW_FLAG。"""
        v = self._v(reason="pass1 与上下文语义连贯；pass2 亦成立。")
        assert cr._reason_contradiction(v) == "LOW_FLAG"
        # 核验结果本身不变（仍 PASS），置信强制 LOW + 依据说明前加标记
        assert cr.verify_evidence(v, _evidence_ctx()) == "PASS"
        out = cr.apply_review(v, "PASS")
        assert out["confidence"] == "low"
        assert out["reason"].startswith("[需人工复核]")
        assert out["category"] == 1 and out["verify"] == "PASS"

    def test_low_flag_reason_clipped_in_csv_cell(self):
        """标记写入 reason 后仍走 REASON_CLIP 截断逻辑（超长不炸）。"""
        v = self._v(reason="pass1 与上下文语义连贯；pass2 亦成立。" + "长" * 300)
        out = cr.apply_review(v, "PASS")
        cell = (out["reason"])[:cr.REASON_CLIP]
        assert cell.startswith("[需人工复核]")
        assert len(cell) == cr.REASON_CLIP

    def test_negated_keyword_not_support(self):
        """支持性关键词被否定（如「不成立」「不正确」）→ 不计支持。"""
        v = self._v(reason="pass1 不成立，pass2 不正确，证据不足。")
        assert cr._reason_contradiction(v) == ""

    def test_non_cat1_or_no_side_noop(self):
        assert cr._reason_contradiction(self._v(category=2)) == ""
        assert cr._reason_contradiction(self._v(side=None)) == ""
        assert cr._reason_contradiction(self._v(reason="")) == ""


# ---------------------------------------------------------------------------
# 9. 提示词 A/B 双版本（R1+R3+R4）
# ---------------------------------------------------------------------------

def _ab_ctx() -> dict:
    """A/B 提示词测试用上下文（三通道文本可区分）。"""
    win1 = [{"index": 5, "timing": "00:00:20,000 --> 00:00:23,000",
             "text": "パス１_5"}]
    win2 = [{"index": 5, "timing": "00:00:20,000 --> 00:00:23,000",
             "text": "パス２_5"}]
    winF = [{"index": 5, "timing": "00:00:20,000 --> 00:00:23,000",
             "text": "中文_F5"}]
    row = make_row()
    row["pass1"], row["pass2"], row["final_cn译文"] = "甲乙", "丙丁", "终稿甲译"
    return {"row": row, "pass1": win1, "pass2": win2, "final_cn": winF}


class TestPromptVersions:
    """A=激进（无终稿/final_cn）；B=保守（保留 + 不得作为选边依据）。"""

    def test_default_version_is_a(self):
        assert cr.build_prompt([_ab_ctx()]) == cr.build_prompt([_ab_ctx()], "A")
        assert cr.DEFAULT_PROMPT_VERSION == "A"

    def test_version_a_excludes_final(self):
        """A 版不含「终稿译文」行、final_cn 通道与终稿上下文内容。"""
        p = cr.build_prompt([_ab_ctx()], "A")
        assert "终稿" not in p
        assert "final_cn" not in p
        assert "中文_F5" not in p and "终稿甲译" not in p
        assert "パス１_5" in p and "パス２_5" in p
        # R3/R4 规则 + few-shot 两版都要有
        assert "双侧碎片/幻觉判定" in p
        assert "类别 1/3 边界" in p
        assert "仅供理解分类，不得照抄内容" in p
        assert "タイム、アリマスタマ" in p
        assert "カンペンさま" in p
        assert "多分" in p and "ちゅーして" in p

    def test_version_b_keeps_final(self):
        """B 版保留终稿列与 final_cn 上下文，并含强制声明。"""
        p = cr.build_prompt([_ab_ctx()], "B")
        assert "终稿译文: 终稿甲译" in p
        assert "final_cn 终稿上下文" in p and "中文_F5" in p
        assert "终稿仅供参考" in p and "不得作为选边依据" in p
        assert "应判类别 2" in p
        # R3（B 版保留「与终稿译文匹配」分句）+ R4
        assert "与终稿译文匹配" in p
        assert "类别 1/3 边界" in p
        assert "双侧碎片/幻觉判定" in p

    def test_shared_sections_both_versions(self):
        for ver in ("A", "B"):
            p = cr.build_prompt([_ab_ctx()], ver)
            for kw in ("五分类", "同音/同形异义", "严格 JSON", "±5行",
                       "仅供理解分类", "时间轴"):
                assert kw in p, f"[{ver}] 缺关键词: {kw}"

    def test_review_batch_passes_version(self):
        """review_batch_with_client 把 version 透传到 build_prompt。"""
        client = FakeClient(content_fn=lambda n, p: verdict_json(1))
        cr.review_batch_with_client(client, [_ab_ctx()], retries=0, version="B")
        assert "终稿译文: 终稿甲译" in client.calls[0]
        cr.review_batch_with_client(client, [_ab_ctx()], retries=0, version="A")
        assert "终稿" not in client.calls[1]


class TestPromptVersionAudit:
    """版本审计留痕：统计块末行 + CSV 末列「提示词版本」。"""

    def test_extra_columns_end_with_version(self):
        assert cr.EXTRA_COLUMNS[-1] == "提示词版本"

    def test_stat_block_version_line(self):
        rows = [make_row(cat=1, verify="PASS", film="a.csv")]
        st = cr.compute_film_stat("a.csv", rows, {r["id"] for r in rows})
        assert cr.build_stat_block([st], rows, version="B")[-1] == \
            f"# 提示词版本: {cr.PROMPT_VERSION_LABELS['B']}"
        assert cr.build_stat_block([st], rows)[-1].endswith("A（激进-无终稿）")

    def test_dry_run_versions_a_and_b(self, tmp_path):
        """dry-run 两版端到端：新列值与统计行均带版本标记。"""
        d = build_mini_dir(tmp_path)
        for ver in ("A", "B"):
            out = d / f"enh_{ver}.csv"
            assert cr.main([str(d), "--dry-run", "--prompt-version", ver,
                            "--output", str(out)]) == 0
            with open(out, encoding="utf-8-sig", newline="") as f:
                recs = [r for r in csv.reader(f) if r]
            assert f"# 提示词版本: {cr.PROMPT_VERSION_LABELS[ver]}" \
                in [r[0] for r in recs]
            header = recs[-4]
            assert header[-1] == "提示词版本"
            data = recs[-3:]
            assert len(data) == 3
            assert all(r[-1] == ver for r in data)

