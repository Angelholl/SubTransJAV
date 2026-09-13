"""
TM 学习第三层门槛测试
=============================================
必看分歧行（similarity<0.30 且非伪影）一律不进 TM 学习：
- _learn_to_tm 行为单测（must_see_spans 命中/未命中、gate 总开关覆盖）；
- 统计行含「必看x」计数；
- 端到端最小验证：tmp_path 构造 merged+pass1+pass2 → collect_disagreement
  → _split_disagreement_rows → 必看 span 集合 → _learn_to_tm 跳过。
"""

from subtransjav.refine import pipeline_v2 as pv
from subtransjav.refine.pass_disagreement import collect_disagreement, probe_disagreement_mode
from subtransjav.refine.quality_report import _split_disagreement_rows
from subtransjav.refine.tm import TranslationMemory

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _make_entries(texts):
    """构造条目列表：index 从1开始，时间轴间隔1秒（起点=索引-1 秒）。"""
    return [{"index": i, "timing": f"00:00:0{i},000 --> 00:00:0{i},500",
             "text": t} for i, t in enumerate(texts, 1)]


def _srt(entries):
    """(index, timing, text) 列表 → SRT 文本。"""
    blocks = [f"{idx}\n{timing}\n{text}" for idx, timing, text in entries]
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# _learn_to_tm 行为单测（直接构造必看 span 集合）
# ---------------------------------------------------------------------------

def test_mustsee_rows_not_learned(tmp_path):
    """gate=True + must_see_spans：必看行不入库，非必看行正常学习。"""
    tm = TranslationMemory(str(tmp_path / "test_tm.db"))

    orig = _make_entries([
        "うん。",               # 1: 干净（必看行 → 应被第三层拦截）
        "今日はいい天気だ。",     # 2: 干净（非必看 → 正常入库）
        "さようなら。",          # 3: 干净（必看行 → 应被第三层拦截）
    ])
    final = _make_entries([
        "嗯。",               # 1
        "今天天气真好。",        # 2
        "再见。",             # 3
    ])

    # 必看集合只含第 1、3 行的 span（与 orig 侧 timing 同源可直接比对）
    must_see_spans = {pv._timing_span(orig[0]["timing"]),
                      pv._timing_span(orig[2]["timing"])}
    pv._learn_to_tm(tm, orig, final, gate=True,
                    must_see_spans=must_see_spans)

    # 必看行（1、3）不入库；非必看行（2）正常入库
    assert not tm.has_exact("うん。", stage=1)
    assert tm.has_exact("今日はいい天気だ。", stage=1)
    assert not tm.has_exact("さようなら。", stage=1)

    tm.close()


def test_mustsee_gate_off_master_switch_wins(tmp_path):
    """gate=False：总开关覆盖第三层，必看行也入库（A/B 验证模式）。"""
    tm = TranslationMemory(str(tmp_path / "test_tm.db"))

    orig = _make_entries(["うん。", "今日はいい天気だ。"])
    final = _make_entries(["嗯。", "今天天气真好。"])

    must_see_spans = {pv._timing_span(orig[0]["timing"])}
    pv._learn_to_tm(tm, orig, final, gate=False,
                    must_see_spans=must_see_spans)

    # gate=False：三层全关，必看行同样入库
    assert tm.has_exact("うん。", stage=1)
    assert tm.has_exact("今日はいい天気だ。", stage=1)

    tm.close()


def test_mustsee_default_none_no_extra_skip(tmp_path):
    """must_see_spans 缺省 None：行为与改动前一致（无额外跳过）。"""
    tm = TranslationMemory(str(tmp_path / "test_tm.db"))

    orig = _make_entries(["うん。", "今日はいい天気だ。"])
    final = _make_entries(["嗯。", "今天天气真好。"])

    pv._learn_to_tm(tm, orig, final, gate=True)   # 不传 must_see_spans

    assert tm.has_exact("うん。", stage=1)
    assert tm.has_exact("今日はいい天気だ。", stage=1)

    tm.close()


def test_mustsee_stat_line_contains_count(tmp_path, capsys):
    """统计行含「必看x」计数；总跳过数含第三层拦截行。"""
    tm = TranslationMemory(str(tmp_path / "test_tm.db"))

    orig = _make_entries(["うん。", "今日はいい天気だ。", "さようなら。"])
    final = _make_entries(["嗯。", "今天天气真好。", "再见。"])

    must_see_spans = {pv._timing_span(orig[0]["timing"])}   # 仅第 1 行必看
    pv._learn_to_tm(tm, orig, final, gate=True,
                    must_see_spans=must_see_spans)

    out = capsys.readouterr().out
    assert "🚫 学习门槛: 跳过 1 行" in out
    assert "必看1" in out
    # 其余计数为零且仍完整输出
    assert "假名0/泄漏0/源残留0/占位符0/长度比0/validator 0/必看1" in out

    tm.close()


# ---------------------------------------------------------------------------
# 端到端最小验证：真实 collect_disagreement → 必看集合 → _learn_to_tm
# ---------------------------------------------------------------------------

def test_end_to_end_disagreement_to_learn_gate(tmp_path, capsys):
    """merged+pass1+pass2 落盘 → 采集分歧 → 必看集合 → 学习跳过。

    构造 3 行 merged 条目：
    - 行1：pass1/pass2 完全不同（similarity≈0.16）→ 必看 → 不入库；
    - 行2：pass1/pass2 相同 → 非必看 → 入库；
    - 行3：应和词「うん」在伪影白名单 → 已过滤伪影（非必看）→
      不受第三层约束，正常入库（必看= similarity<0.30 且非伪影）。
    """
    merged_path = tmp_path / "X.ja.merged.subtransjav.srt"
    merged = _srt([
        (1, "00:00:01,000 --> 00:00:03,000", "学校のプールでの練習風景"),
        (2, "00:00:04,000 --> 00:00:06,000", "今日はいい天気ですね"),
        (3, "00:00:07,000 --> 00:00:08,000", "うん"),
    ])
    pass1 = _srt([
        (1, "00:00:01,000 --> 00:00:03,000", "学校のプールでの練習風景"),
        (2, "00:00:04,000 --> 00:00:06,000", "今日はいい天気ですね"),
        (3, "00:00:07,000 --> 00:00:08,000", "うん"),
    ])
    pass2 = _srt([
        # 行1 两侧完全不同的实义长句 → similarity≈0.16 → 必看
        (1, "00:00:01,000 --> 00:00:03,000", "まったく別の内容の長い文章"),
        (2, "00:00:04,000 --> 00:00:06,000", "今日はいい天気ですね"),
        # 行3 同文应和 → 命中应和/感叹白名单 → 判伪影（非必看）
        (3, "00:00:07,000 --> 00:00:08,000", "うん"),
    ])
    (tmp_path / "X.ja.pass1.srt").write_text(pass1, encoding="utf-8")
    (tmp_path / "X.ja.pass2.srt").write_text(pass2, encoding="utf-8")
    merged_path.write_text(merged, encoding="utf-8")

    # 1) 采集分歧 + 分组（与 _run_single_v2 数据通路一致）
    assert probe_disagreement_mode(str(merged_path)) == "dual"
    disag = collect_disagreement(str(merged_path))
    assert disag is not None
    must_see, optional, artifacts = _split_disagreement_rows(disag["rows"])
    assert len(must_see) == 1                       # 仅行1 必看
    assert must_see[0]["index"] == 1
    assert must_see[0]["similarity"] < 0.30
    assert optional == []
    assert len(artifacts) == 1                       # 行3 伪影（非必看）
    assert artifacts[0]["index"] == 3
    must_see_spans = {pv._timing_span(r.get("timing", ""))
                      for r in must_see}
    assert must_see_spans == {(1.0, 3.0)}

    # 2) 学习循环的 orig 侧 = 同一 merged 文件解析出的 entries
    from subtransjav.refine.filters import parse_srt
    orig = parse_srt(merged_path.read_text(encoding="utf-8"))
    final = [
        {"index": 1, "timing": orig[0]["timing"], "text": "学校泳池的训练风景"},
        {"index": 2, "timing": orig[1]["timing"], "text": "今天天气真好。"},
        {"index": 3, "timing": orig[2]["timing"], "text": "嗯。"},
    ]

    tm = TranslationMemory(str(tmp_path / "test_tm.db"))
    pv._learn_to_tm(tm, orig, final, gate=True,
                    must_see_spans=must_see_spans)

    # 必看行（行1）不入库；非必看行（行2）正常入库；
    # 行3 伪影行非必看 → 不受第三层约束，正常入库
    assert not tm.has_exact("学校のプールでの練習風景", stage=1)
    assert tm.has_exact("今日はいい天気ですね", stage=1)
    assert tm.has_exact("うん", stage=1)

    # 统计行含必看计数
    out = capsys.readouterr().out
    assert "必看1" in out
    assert "🚫 学习门槛: 跳过 1 行" in out

    tm.close()
