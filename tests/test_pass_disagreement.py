"""
双引擎分歧采集单元测试：find_pass_siblings / probe_disagreement_mode /
judge_artifact / 内部辅助函数 / collect_disagreement。
"""
from subtransjav.refine.pass_disagreement import (
    REVIEW_STRICT_THRESHOLD,
    _best_overlap,
    _normalize,
    _sibling_paths,
    _similarity,
    _timing_span,
    collect_disagreement,
    find_pass_siblings,
    judge_artifact,
    probe_disagreement_mode,
)


def _srt(entries):
    """(index, timing, text) 列表 → SRT 文本。"""
    blocks = [f"{idx}\n{timing}\n{text}" for idx, timing, text in entries]
    return "\n\n".join(blocks) + "\n"


def test_find_pass_siblings(tmp_path):
    (tmp_path / "X.ja.pass1.srt").write_text("", encoding="utf-8")
    (tmp_path / "X.ja.pass2.srt").write_text("", encoding="utf-8")
    merged = tmp_path / "X.ja.merged.subtransjav.srt"
    merged.write_text("", encoding="utf-8")

    p1, p2 = find_pass_siblings(str(merged))
    assert p1 is not None and p2 is not None
    assert p1.name == "X.ja.pass1.srt"
    assert p2.name == "X.ja.pass2.srt"

    # 只存在 pass1 的场景 → (None, None)
    d = tmp_path / "only_p1"
    d.mkdir()
    (d / "Y.ja.pass1.srt").write_text("", encoding="utf-8")
    (d / "Y.ja.merged.subtransjav.srt").write_text("", encoding="utf-8")
    a, b = find_pass_siblings(str(d / "Y.ja.merged.subtransjav.srt"))
    assert (a, b) == (None, None)


def test_collect_disagreement(tmp_path):
    merged = _srt([
        (1, "00:00:01,000 --> 00:00:03,000", "同じ文本です"),
        (2, "00:00:04,000 --> 00:00:06,000", "完全不同的文本甲"),
        (3, "00:00:07,000 --> 00:00:09,000", "仅pass1有对应"),
    ])
    pass1 = _srt([
        (1, "00:00:01,000 --> 00:00:03,000", "同じ文本です"),
        (2, "00:00:04,000 --> 00:00:06,000", "完全不同的文本甲"),
        (3, "00:00:07,000 --> 00:00:09,000", "仅pass1有对应"),
    ])
    pass2 = _srt([
        (1, "00:00:01,000 --> 00:00:03,000", "同じ文本です"),
        (2, "00:00:04,000 --> 00:00:06,000", "まったく違う内容乙"),
    ])

    (tmp_path / "X.ja.pass1.srt").write_text(pass1, encoding="utf-8")
    (tmp_path / "X.ja.pass2.srt").write_text(pass2, encoding="utf-8")
    mp = tmp_path / "X.ja.merged.subtransjav.srt"
    mp.write_text(merged, encoding="utf-8")

    result = collect_disagreement(str(mp))
    assert result is not None
    assert result["total"] == 3
    assert result["matched"] == 2
    assert len(result["rows"]) == 2
    # rows 按 similarity 升序：低相似度行（index 2）在前，相似行（index 1）在后
    assert result["rows"][0]["index"] == 2
    assert result["rows"][1]["index"] == 1
    assert result["rows"][0]["similarity"] < result["rows"][1]["similarity"]
    assert result["rows"][1]["similarity"] > 0.9


def test_collect_disagreement_no_siblings(tmp_path):
    p = tmp_path / "Z.ja.merged.subtransjav.srt"
    p.write_text(_srt([(1, "00:00:01,000 --> 00:00:03,000", "x")]),
                 encoding="utf-8")
    assert collect_disagreement(str(p)) is None


# ======================================================================
# probe_disagreement_mode：四种模式的探测
# ======================================================================

def test_probe_disagreement_mode_dual(tmp_path):
    """pass1/pass2 齐全 → dual。"""
    (tmp_path / "A.ja.pass1.srt").write_text("", encoding="utf-8")
    (tmp_path / "A.ja.pass2.srt").write_text("", encoding="utf-8")
    mp = tmp_path / "A.ja.merged.subtransjav.srt"
    mp.write_text("", encoding="utf-8")
    assert probe_disagreement_mode(str(mp)) == "dual"


def test_probe_disagreement_mode_missing_pass1(tmp_path):
    """仅缺 pass1 → missing_pass1。"""
    (tmp_path / "B.ja.pass2.srt").write_text("", encoding="utf-8")
    mp = tmp_path / "B.ja.merged.subtransjav.srt"
    mp.write_text("", encoding="utf-8")
    assert probe_disagreement_mode(str(mp)) == "missing_pass1"


def test_probe_disagreement_mode_missing_pass2(tmp_path):
    """仅缺 pass2 → missing_pass2。"""
    (tmp_path / "C.ja.pass1.srt").write_text("", encoding="utf-8")
    mp = tmp_path / "C.ja.merged.subtransjav.srt"
    mp.write_text("", encoding="utf-8")
    assert probe_disagreement_mode(str(mp)) == "missing_pass2"


def test_probe_disagreement_mode_none(tmp_path):
    """两个兄弟文件都缺失 → none。"""
    mp = tmp_path / "D.ja.merged.subtransjav.srt"
    mp.write_text("", encoding="utf-8")
    assert probe_disagreement_mode(str(mp)) == "none"


# ======================================================================
# judge_artifact：白名单兜底 + V3 长度组合门槛
# ======================================================================

def test_judge_artifact_whitelist_hit():
    """短侧命中应和/感叹白名单 → 判伪影（similarity 未知也生效）。"""
    assert judge_artifact(0.9, 0.0, "うん", "とても長い実義文です",
                          0.20, 3.0) is True
    assert judge_artifact(0.9, 0.0, "長い側の文章", "はい",
                          0.20, 3.0, similarity=0.99) is True


def test_judge_artifact_v3_length_gate():
    """短侧≤ARTIFACT_SHORT_MAX 且 长侧≥ARTIFACT_LONG_MIN 且低相似 → 伪影。"""
    sim = REVIEW_STRICT_THRESHOLD - 0.01
    # 短侧 3 字符、长侧 4 字符以上、低相似度 → 判伪影
    assert judge_artifact(0.9, 0.0, "あああ", "実義の長文です",
                          0.20, 3.0, similarity=sim) is True
    # 长侧不足 ARTIFACT_LONG_MIN → 不判伪影（两侧均避开白名单词）
    assert judge_artifact(0.9, 0.0, "いぬ", "ねこ",
                          0.20, 3.0, similarity=sim) is False
    # 相似度达到 REVIEW_STRICT_THRESHOLD → 不判伪影
    assert judge_artifact(0.9, 0.0, "あああ", "実義の長文です",
                          0.20, 3.0,
                          similarity=REVIEW_STRICT_THRESHOLD) is False
    # similarity 未知（-1.0）时跳过 V3 门槛 → 不判伪影
    assert judge_artifact(0.9, 0.0, "あああ", "実義の長文です",
                          0.20, 3.0, similarity=-1.0) is False


def test_judge_artifact_normal_not_artifact():
    """普通不命中白名单/门槛的分歧行 → 非伪影。"""
    assert judge_artifact(0.5, 1.0, "完全不同的文本甲", "まったく違う内容乙",
                          0.20, 3.0, similarity=0.1) is False


# ======================================================================
# 内部辅助函数：_timing_span / _normalize / _similarity / _best_overlap /
# _sibling_paths
# ======================================================================

def test_timing_span_invalid():
    """非法时间码（缺失/乱码/None）→ (-1.0, -1.0)。"""
    assert _timing_span("not a timing") == (-1.0, -1.0)
    assert _timing_span("00:00:01 --> 00:00:03") == (-1.0, -1.0)
    assert _timing_span("") == (-1.0, -1.0)
    assert _timing_span(None) == (-1.0, -1.0)


def test_timing_span_valid():
    """合法时间码（毫秒逗号/点号兼容）。"""
    assert _timing_span("00:00:01,500 --> 00:00:03,000") == (1.5, 3.0)
    assert _timing_span("01:02:03.250 --> 01:02:04.750") == (3723.25, 3724.75)


def test_normalize_and_similarity_empty_or_punct():
    """空串/纯标点归一化后为空，相似度返回 0.0。"""
    assert _normalize("") == ""
    assert _normalize(None) == ""
    assert _normalize("，。！？ ") == ""
    assert _similarity("", "あいうえお") == 0.0
    assert _similarity("あいうえお", "") == 0.0
    assert _similarity("、。！", "，？") == 0.0
    # 非空正常情况：相同文本相似度 1.0
    assert _similarity("同じテキスト", "同じテキスト") == 1.0


def test_best_overlap_no_overlap():
    """无重叠 / 负坐标 span → 返回 None。"""
    entries = [{"timing": "00:00:01,000 --> 00:00:02,000", "text": "a"}]
    # span 起点在条目结束之后 → 无正重叠
    assert _best_overlap((2.0, 3.0), entries) is None
    # span 在条目开始之前 → 无正重叠
    assert _best_overlap((0.0, 0.5), entries) is None
    # 负坐标（非法时间码解析结果）→ 直接返回 None
    assert _best_overlap((-1.0, -1.0), entries) is None
    # entries 内条目时间码非法 → 被跳过
    bad = [{"timing": "bad", "text": "b"}]
    assert _best_overlap((1.0, 2.0), bad) is None


def test_sibling_paths_lang_and_markers():
    """语言后缀解析；缺省语言码为 ja；merged 可带 .subtransjav 后缀。"""
    # 带语言后缀
    p, p1, p2 = _sibling_paths("/tmp/X.ja.merged.subtransjav.srt")
    assert p1.name == "X.ja.pass1.srt"
    assert p2.name == "X.ja.pass2.srt"
    # 其他语言码
    _, p1, p2 = _sibling_paths("/tmp/X.zh.merged.subtransjav.srt")
    assert p1.name == "X.zh.pass1.srt"
    assert p2.name == "X.zh.pass2.srt"
    # merged 不带 .subtransjav 后缀
    _, p1, p2 = _sibling_paths("/tmp/X.ja.merged.srt")
    assert p1.name == "X.ja.pass1.srt"
    assert p2.name == "X.ja.pass2.srt"
    # 无语言后缀 → 默认 ja
    _, p1, p2 = _sibling_paths("/tmp/X.merged.subtransjav.srt")
    assert p1.name == "X.ja.pass1.srt"
    assert p2.name == "X.ja.pass2.srt"
    # 传入 pass1 文件本身 → 兄弟路径与其重合（只拼路径不查存在性）
    p, p1, p2 = _sibling_paths("/tmp/X.ja.pass1.srt")
    assert p1.name == "X.ja.pass1.srt"
    assert p2.name == "X.ja.pass2.srt"


def test_collect_disagreement_no_timeline_overlap(tmp_path):
    """有兄弟文件但时间轴完全无重叠的行 → 不产生分歧行。"""
    merged = _srt([
        (1, "00:00:10,000 --> 00:00:12,000", "时间轴无重叠"),
    ])
    pass1 = _srt([
        (1, "00:00:01,000 --> 00:00:02,000", "别的片段一"),
    ])
    pass2 = _srt([
        (1, "00:00:03,000 --> 00:00:04,000", "别的片段二"),
    ])
    (tmp_path / "E.ja.pass1.srt").write_text(pass1, encoding="utf-8")
    (tmp_path / "E.ja.pass2.srt").write_text(pass2, encoding="utf-8")
    mp = tmp_path / "E.ja.merged.subtransjav.srt"
    mp.write_text(merged, encoding="utf-8")

    result = collect_disagreement(str(mp))
    assert result is not None
    assert result["total"] == 1
    assert result["matched"] == 0
    assert result["rows"] == []
