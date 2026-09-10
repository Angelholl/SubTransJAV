"""
双引擎分歧采集单元测试：find_pass_siblings / collect_disagreement。
"""
from subtransjav.refine.pass_disagreement import (
    find_pass_siblings, collect_disagreement)


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
