"""quality_report 纯函数与渲染分支的单元测试（补 test_pipeline_v2.py 空缺）。

不重复漏覆盖双口径拆分场景（已在 test_pipeline_v2.py 覆盖），只补：
_is_untranslated / _split_disagreement_rows / _final_text_for_span /
render_disagreement_section / render_mishear_review_section /
write_divergence_review_csv / build_quality_report 的 TM 行与噪声闸门行。
"""

import re

from subtransjav.refine.quality_report import (
    _final_text_for_span,
    _is_untranslated,
    _split_disagreement_rows,
    build_quality_report,
    render_disagreement_section,
    render_mishear_review_section,
    write_divergence_review_csv,
    write_guide_json,
)  # noqa: I001

# ----------------------------------------------------------------------
# _is_untranslated
# ----------------------------------------------------------------------

def test_is_untranslated_prefix_variants():
    """前缀契约：带/不带尾空格、前缀后带残译文，均判未译。"""
    assert _is_untranslated("[未翻译] こんにちは")           # 带尾空格
    assert _is_untranslated("[未翻译]こんにちは")            # 无空格
    assert _is_untranslated("[未翻译]Chicks。")              # 前缀后残译文
    assert _is_untranslated("[未翻译]")                      # 裸前缀


def test_is_untranslated_not_flagged():
    """正文中部出现 [未翻译] 不误判；正常译文/空值不误判。"""
    assert not _is_untranslated("他说[未翻译]是什么意思")     # 中部出现
    assert not _is_untranslated("谢谢")
    assert not _is_untranslated("")
    assert not _is_untranslated(None)
    assert not _is_untranslated(" [未翻译]缩进空格不算前缀")  # 前导空格


# ----------------------------------------------------------------------
# _split_disagreement_rows
# ----------------------------------------------------------------------

def test_split_disagreement_rows_empty_and_single():
    """空列表返回三空组；单行按相似度落组。"""
    assert _split_disagreement_rows([]) == ([], [], [])

    m, o, a = _split_disagreement_rows([{"similarity": 0.10}])
    assert (m, o, a) == ([{"similarity": 0.10}], [], [])

    m, o, a = _split_disagreement_rows([{"similarity": 0.90}])
    assert (m, o, a) == ([], [], [])                # 正常行不入任何复核区


def test_split_disagreement_rows_multi_same_group_and_artifact():
    """多行同组保持原顺序；artifact 优先于相似度分流。"""
    rows = [{"similarity": 0.20}, {"similarity": 0.05},
            {"similarity": 0.35}, {"similarity": 0.10, "artifact": True}]
    m, o, a = _split_disagreement_rows(rows)
    assert m == [rows[0], rows[1]]
    assert o == [rows[2]]
    assert a == [rows[3]]


# ----------------------------------------------------------------------
# _final_text_for_span
# ----------------------------------------------------------------------

def test_final_text_for_span_hit_miss_and_best_overlap():
    """命中取重叠最大条目译文；不命中/非法时间轴/空列表均返回空串。"""
    final = [
        {"timing": "00:00:01,000 --> 00:00:02,000", "text": "短句"},
        {"timing": "00:00:01,500 --> 00:00:03,000", "text": "重叠更长的句子"},
    ]
    assert _final_text_for_span("00:00:01,000 --> 00:00:02,000", final) \
        == "短句"                                    # 取重叠秒数更大者
    assert _final_text_for_span("00:00:10,000 --> 00:00:11,000", final) == ""
    assert _final_text_for_span("00:00:01,000 --> 00:00:02,000", []) == ""
    assert _final_text_for_span("00:00:01,000 --> 00:00:02,000", None) == ""
    assert _final_text_for_span("非法时间轴", final) == ""


# ----------------------------------------------------------------------
# render_disagreement_section
# ----------------------------------------------------------------------

def test_render_disagreement_section_dual_with_rows():
    """dual 模式：按相似度分组渲染，三区计数与样本可见。"""
    pd = {
        "total": 3,
        "matched": 3,
        "rows": [
            {"timing": "00:00:01,000 --> 00:00:02,000",
             "similarity": 0.10, "pass1": "前文本", "pass2": "后文本",
             "artifact": False},
            {"timing": "00:00:03,000 --> 00:00:04,000",
             "similarity": 0.40, "pass1": "可选行1", "pass2": "可选行2",
             "artifact": False},
            {"timing": "00:00:05,000 --> 00:00:06,000",
             "similarity": 0.10, "pass1": "碎片", "pass2": "很长的实义句子",
             "artifact": True, "iou": 0.0, "offset": 0.5},
        ],
    }
    text = render_disagreement_section(pd, "dual")
    assert "分歧模式：双引擎（可对照 3/3 行）" in text
    assert "【必看 <0.30】共 1 行" in text
    assert "【可选 0.30-0.50】共 1 行" in text
    assert "【已过滤伪影 1 行】" in text
    assert "pass1: 前文本" in text
    assert "完整被过滤清单见分歧复核 CSV" in text


def test_render_disagreement_section_degraded_and_empty():
    """非 dual 模式整段降级只出模式行；dual 无分歧行显示无分歧行。"""
    assert "降级" in render_disagreement_section(None, "missing_pass1")
    assert "不可用" in render_disagreement_section(None, "none")
    text = render_disagreement_section({"total": 2, "matched": 2, "rows": []},
                                       "dual")
    assert "无分歧行（双引擎匹配 2/2）" in text
    assert "【必看" not in text


# ----------------------------------------------------------------------
# render_mishear_review_section
# ----------------------------------------------------------------------

def test_render_mishear_review_section_variants():
    """None 整节省略；空列表显示无样本；有条目渲染嫌疑词与译文预览。"""
    assert render_mishear_review_section(None) == ""
    assert "无样本" in render_mishear_review_section([])

    review = [{"index": 3,
               "timing": "00:00:05,000 --> 00:00:06,000",
               "suspect": "騙す", "correct": "惹く",
               "zh_preview": "骗人的吧"}]
    text = render_mishear_review_section(review)
    assert "【误听疑似改写】" in text
    assert "共 1 条" in text
    assert "[騙す→疑为惹く]" in text
    assert "译: 骗人的吧" in text


# ----------------------------------------------------------------------
# write_divergence_review_csv
# ----------------------------------------------------------------------

def test_write_divergence_review_csv(tmp_path):
    """落盘行数 = 必看全部 + 可选（含上限内） + 伪影全部 + 表头。"""
    out = tmp_path / "分歧.csv"
    rows = [
        {"timing": "00:00:01,000 --> 00:00:02,000", "similarity": 0.10,
         "iou": 0.5, "offset": 0.1, "artifact": False,
         "pass1": "甲侧", "pass2": "乙侧"},
        {"timing": "00:00:03,000 --> 00:00:04,000", "similarity": 0.40,
         "iou": 0.2, "offset": 0.2, "artifact": False,
         "pass1": "丙侧", "pass2": "丁侧"},
        {"timing": "00:00:05,000 --> 00:00:06,000", "similarity": 0.05,
         "iou": 0.0, "offset": 0.0, "artifact": True,
         "pass1": "碎片", "pass2": "很长的实义句子"},
    ]
    final = [{"timing": "00:00:01,000 --> 00:00:02,000", "text": "终稿译"}]
    write_divergence_review_csv(str(out), rows, "demo", final)

    import csv as _csv
    with open(out, encoding="utf-8-sig", newline="") as f:
        data = list(_csv.reader(f))
    assert len(data) == 4                            # 表头 + 3 行全收
    assert data[0][-1] == "final_cn译文"
    by_group = {r[1]: r for r in data[1:]}
    assert by_group["必看"][2] == "00:00:01,000 --> 00:00:02,000"
    assert by_group["必看"][-1] == "终稿译"          # 时间轴对回终稿译文
    assert by_group["可选"][-1] == ""                # 无终稿命中留空
    assert by_group["已过滤"][6] == "是"             # artifact 列


# ----------------------------------------------------------------------
# build_quality_report 统计行分支（TM 行 / 噪声闸门行）
# ----------------------------------------------------------------------

def test_build_quality_report_tm_line_branches():
    """TM 行三态：双值齐全 / 单侧缺省显示"无样本" / 均缺省整行无样本。"""
    e = {"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
         "text": "テスト"}
    full = build_quality_report([e], [e], "demo",
                                tm_exact_hits=3, tm_learned_count=5)
    assert "TM: 精确命中 3 条 | 本次学习入库 5 条" in full

    half = build_quality_report([e], [e], "demo", tm_exact_hits=3)
    assert "TM: 精确命中 3 条 | 本次学习入库 无样本" in half

    empty = build_quality_report([e], [e], "demo")
    assert "TM: 无样本" in empty


def test_build_quality_report_noise_gate_line_branches():
    """噪声闸门行：键缺失不显示；键存在时统计终稿 [未翻译] 标记数。"""
    exp = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "テスト"},
           {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000",
            "text": "テスト"}]
    final = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
              "text": "[未翻译]テスト"},
             {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000",
              "text": "已译"}]

    # 键缺失（规则清洗未运行）：整行不出现
    absent = build_quality_report(exp, final, "demo")
    assert "纯假名实义保留" not in absent

    # 键存在：N=免删条数，M=对回终稿后带 [未翻译] 前缀的数量
    stats = {"clean_kept_by_noise_gate": 2,
             "clean_kept_by_noise_gate_timings":
                 ["00:00:01,000 --> 00:00:02,000",
                  "00:00:03,000 --> 00:00:04,000"]}
    shown = build_quality_report(exp, final, "demo", merge_stats=stats)
    assert "纯假名实义保留: 2（其中 [未翻译] 标记 1）" in shown


# ----------------------------------------------------------------------
# build_quality_report 白话导读区（两分支）与章节注解行
# ----------------------------------------------------------------------

def test_plain_guide_branch_a_with_items():
    """有复核项：导读在场、N 与【结论】行同源、时间戳格式规整。"""
    exp = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "汉字原文テスト"}]
    report = build_quality_report(exp, [], "demo")
    lines = report.splitlines()
    assert "【白话导读】基于本次运行" in report
    assert any(ln.startswith("　① 总体：需人工复核 1 处") for ln in lines)
    # 与【结论】行 N 同源
    concl = next(ln for ln in lines if ln.startswith("【结论】"))
    assert "需人工复核 1 处" in concl
    # 时间戳与报告头第 3 行同一 datetime 串（格式 YYYY-MM-DD HH:MM:SS）
    ts_lines = [ln for ln in lines if re.search(r"时间: \d{4}-\d{2}-\d{2} "
                                                r"\d{2}:\d{2}:\d{2}", ln)]
    assert len(ts_lines) >= 2                        # 头部行 3 + 导读行
    assert ts_lines[0].split("时间: ")[1].split(" |")[0] \
        == ts_lines[1].split("时间: ")[1].split("）：")[0]
    # 有条目场景：导读 ④ 不出现（无 [未翻译] 残留）
    assert "　④ 未翻译残留" not in report


def test_plain_guide_branch_b_clean_report():
    """无复核项：导读显示可直接使用；整份报告无"疑似"。"""
    report = build_quality_report([], [], "demo")
    assert "　① 总体：未发现需人工复核的条目，终稿可直接使用" in report
    assert "【白话导读】" in report
    assert "疑似" not in report
    assert "　④ 未翻译残留" not in report            # 条件行缺席


def test_section_notes_inserted_and_absent():
    """章节标题下一行插入全角空格注解；条件缺席章节无注解。"""
    garble = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
               "src_preview": "んああああああ", "zh_preview": "你好呀",
               "signal": "无意义音节连缀"}]
    report = build_quality_report([{"index": 1,
                                    "timing": "00:00:01,000 --> 00:00:02,000",
                                    "text": "テスト"}],
                                  [{"index": 1,
                                    "timing": "00:00:01,000 --> 00:00:02,000",
                                    "text": "你好呀"}],
                                  "demo", garble_review=garble)
    lines = report.splitlines()
    i = next(k for k, ln in enumerate(lines)
             if ln.startswith("【乱码强译复核】"))
    note = lines[i + 1]
    assert note.startswith("　（")
    assert "疑似" not in note
    # 无术语冲突输入：对应注解不出现
    assert "术语口径统一时裁定" not in report
    # 统计区注解恒在
    j = lines.index("【统计】")
    assert lines[j + 1].startswith("　（本次运行全部可核对指标")


def test_plain_guide_numbers_same_source_as_stats():
    """同源守护：导读漏覆盖数与【统计】行"实义内容漏覆盖: N/" 一致。"""
    exp = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "汉字原文一"},
           {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000",
            "text": "汉字原文二"}]
    final = [{"index": 1, "timing": "00:00:03,000 --> 00:00:04,000",
              "text": "已译"}]
    report = build_quality_report(exp, final, "demo")
    guide_n = int(re.search(r"漏覆盖：实义内容漏覆盖 (\d+) 条", report).group(1))
    stats_n = int(re.search(r"实义内容漏覆盖: (\d+)/", report).group(1))
    assert guide_n == stats_n == 1                   # 仅第 1 条整条缺失
    assert "条数核对不平" in report                  # 1 条终稿对 2 条原文


# ----------------------------------------------------------------------
# W1a：guide_sink 采集 / 无 sink 零行为 / write_guide_json 落盘
# ----------------------------------------------------------------------

def _w1a_report_inputs():
    """构造一份带漏覆盖与未翻译残留的报告输入（供 sink 案共用）。"""
    exp = [{"index": 1, "timing": "00:00:01,000 --> 00:00:02,000",
            "text": "汉字原文一"},
           {"index": 2, "timing": "00:00:03,000 --> 00:00:04,000",
            "text": "汉字原文二"}]
    final = [{"index": 1, "timing": "00:00:03,000 --> 00:00:04,000",
              "text": "已译"},
             {"index": 2, "timing": "00:00:05,000 --> 00:00:06,000",
              "text": "[未翻译]テスト"}]
    return exp, final


def test_guide_sink_collects_snapshot_same_source_as_report():
    """sink 采集：conclusions 的 N 与【结论】行同源；sections 标题集 ==
    txt 实际【】标题集；basis 字段契约为"基于本次运行"。"""
    exp, final = _w1a_report_inputs()
    sink: dict = {}
    report = build_quality_report(exp, final, "demo", guide_sink=sink)
    assert sink["version"] == 1
    assert sink["source"] == "demo"
    assert sink["basis"] == "基于本次运行"
    # generated_at 与报告头时间戳同串（同源变量 now_str）
    header_ts = next(ln for ln in report.splitlines()
                     if ln.startswith("来源: ")).split("时间: ")[1] \
        .split(" |")[0]
    assert sink["generated_at"] == header_ts
    # conclusions 的复核数与【结论】行 N 同源
    m_concl = re.search(r"需人工复核 (\d+) 处", report)
    m_sink = next(c for c in sink["conclusions"]
                  if "需人工复核" in c)
    assert int(m_concl.group(1)) == int(re.search(r"复核 (\d+) 处",
                                                  m_sink).group(1))
    # sections 标题集 == 报告文本中实际出现的已知九章节【】标题集
    # （【结论】/【白话导读】非注解章节，不在采集范围）
    from subtransjav.refine.quality_report import _SECTION_NOTES
    known = {prefix for prefix, _ in _SECTION_NOTES}
    txt_titles = {ln.split("】")[0] + "】" for ln in report.splitlines()
                  if ln.startswith("【")
                  and any(ln.startswith(k) for k in known)}
    sink_titles = {s["title"] for s in sink["sections"]}
    assert sink_titles == txt_titles
    assert all(s["note"].startswith("　（") for s in sink["sections"])


def test_guide_sink_absent_keeps_output_byte_identical():
    """无 sink 零行为：同输入两次 build（带/不带 sink），txt 逐字节相等。"""
    exp, final = _w1a_report_inputs()
    plain = build_quality_report(exp, final, "demo")
    sink: dict = {}
    with_sink = build_quality_report(exp, final, "demo", guide_sink=sink)
    assert plain == with_sink
    assert isinstance(plain, str)


def test_write_guide_json_roundtrip_and_empty_noop(tmp_path):
    """write_guide_json：落盘+回读结构齐全；空 guide 直接返回空串不落盘。"""
    import json
    guide = {"version": 1, "source": "demo", "generated_at": "2026-01-01 "
             "00:00:00", "basis": "基于本次运行",
             "conclusions": ["总体：未发现需人工复核的条目"],
             "sections": [{"title": "【统计】", "note": "　（注）"}],
             "extras": {"对齐率": "100.0%"}}
    p = write_guide_json(str(tmp_path), "movie", guide)
    assert p.endswith("movie_质量报告导读.json")
    data = json.loads((tmp_path / "movie_质量报告导读.json")
                      .read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["stem"] == "movie"
    assert data["conclusions"] == ["总体：未发现需人工复核的条目"]
    assert data["sections"] == [{"title": "【统计】", "note": "　（注）"}]
    # companions：终稿在、其余缺 → 存在性布尔如实
    (tmp_path / "movie_final_cn.srt").write_text("1", encoding="utf-8")
    write_guide_json(str(tmp_path), "movie", data)
    data2 = json.loads((tmp_path / "movie_质量报告导读.json")
                       .read_text(encoding="utf-8"))
    assert data2["companions"] == {
        "movie_final_cn.srt": True,
        "movie_质量报告.txt": False,
        "movie_分歧复核.csv": False,
        "movie_风险清单.md": False,
        "movie_风险清单.json": False,
        "movie_术语冲突观察.csv": False,
    }
    # 空 guide：不落盘，返回空串
    empty_path = write_guide_json(str(tmp_path), "other", {})
    assert empty_path == ""
    assert not (tmp_path / "other_质量报告导读.json").exists()
