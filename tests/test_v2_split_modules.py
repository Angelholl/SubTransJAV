"""1.3.0 pipeline_v2 拆分批次 1/2 新模块直接单测（HRO-2 条款）
================================================================

契约：拆分后的叶子模块（v2_premerge / v2_context_blocks /
v2_manifest_fp / v2_outputs / v2_learn / v2_rules）每个模块至少一个
直接单测；并钉住 facade re-export 绑定与常量副本不漂移
（D2026-0925-01 执行契约、D2026-0922-03 HRO-2 行为等价）。
"""

import hashlib

import subtransjav.refine.pipeline_v2 as pv
import subtransjav.refine.v2_learn as v2_learn
import subtransjav.refine.v2_manifest_fp as v2_manifest_fp
import subtransjav.refine.v2_outputs as v2_outputs
import subtransjav.refine.v2_premerge as v2_premerge
import subtransjav.refine.v2_rules as v2_rules

# ---------------------------------------------------------------------------
# v2_premerge：时间轴解析与 [未翻译] 标记规范化
# ---------------------------------------------------------------------------

class TestPremerge:
    def test_timing_span_normal_and_invalid_and_millis(self):
        """_timing_span：正常解析、毫秒精度、非法输入回退 (-1, -1)。"""
        assert v2_premerge._timing_span(
            "00:00:01,000 --> 00:00:02,500") == (1.0, 2.5)
        assert v2_premerge._timing_span(
            "01:02:03,456 --> 01:02:04,999") == (3723.456, 3724.999)
        assert v2_premerge._timing_span("not a timing") == (-1.0, -1.0)
        assert v2_premerge._timing_span("") == (-1.0, -1.0)
        assert v2_premerge._timing_span(None) == (-1.0, -1.0)

    def test_normalize_untranslated_marker_idempotent(self):
        """_normalize_untranslated_marker：幂等——二次调用结果不变。"""
        once = v2_premerge._normalize_untranslated_marker(
            "[未翻译] Chicks。", "ひよこ。")
        twice = v2_premerge._normalize_untranslated_marker(once, "ひよこ。")
        assert once == twice == "[未翻译] ひよこ。"
        # 纯占位形态原样返回（防二次加标）
        assert v2_premerge._normalize_untranslated_marker(
            "[未翻译]", "x") == "[未翻译]"


# ---------------------------------------------------------------------------
# v2_context_blocks：注入块组装
# ---------------------------------------------------------------------------

class TestContextBlocks:
    def test_synopsis_prompt_block_contains_tag(self):
        """_synopsis_prompt_block：非空摘要时块含 _SYNOPSIS_BLOCK_TAG。"""
        from subtransjav.refine import v2_context_blocks
        block = v2_context_blocks._synopsis_prompt_block("梗概正文")
        assert v2_context_blocks._SYNOPSIS_BLOCK_TAG in block
        assert "梗概正文" in block
        assert v2_context_blocks._synopsis_prompt_block("  \n") == ""

    def test_v2_glossary_block_hit_rendered(self, monkeypatch):
        """_v2_glossary_block：A 档开关开启且命中时渲染词表块。"""
        from subtransjav.refine import v2_context_blocks

        class _Cfg:
            apply_glossary_stage1 = True
            apply_glossary_stage2 = False

        monkeypatch.setattr(v2_context_blocks, "match_glossary",
                            lambda src, gl: [("先生", "老师")])
        monkeypatch.setattr(v2_context_blocks, "format_glossary_block",
                            lambda hits: "- 先生 => 老师")
        out = v2_context_blocks._v2_glossary_block(
            _Cfg(), "A", "先生、おはよう", [("先生", "老师")])
        assert "先生 => 老师" in out
        # 开关关闭 → 不注入
        assert v2_context_blocks._v2_glossary_block(
            _Cfg(), "B", "先生", [("先生", "老师")]) == ""


# ---------------------------------------------------------------------------
# v2_manifest_fp：词库指纹
# ---------------------------------------------------------------------------

class TestManifestFp:
    def test_glossary_fingerprint_stable_and_missing(self, tmp_path,
                                                     monkeypatch):
        """_glossary_fingerprint：tmp 词表文件 → 稳定 sha1；缺失 → None。

        源实现实际行为：人工词库与 learned 词库均缺失时返回 None
        （compute_glossary_sha1 对不存在路径返回 None，parts 过滤后为
        空即 None），不是空串。
        """
        gl = tmp_path / "glossary.csv"
        gl.write_text("先生,老师\n学生,生徒\n", encoding="utf-8")
        monkeypatch.setattr(v2_manifest_fp, "learned_glossary_path",
                            lambda: str(gl))

        class _Cfg:
            glossary_path = ""      # 人工词库不启用，仅 learned 参与

        fp1 = v2_manifest_fp._glossary_fingerprint(_Cfg())
        fp2 = v2_manifest_fp._glossary_fingerprint(_Cfg())
        expect = hashlib.sha1(
            v2_manifest_fp.compute_glossary_sha1(str(gl)).encode("utf-8")
        ).hexdigest()
        assert fp1 == fp2 == expect

        # 两词库均缺失 → None（跳过校验语义）
        gl.unlink()
        assert v2_manifest_fp._glossary_fingerprint(_Cfg()) is None

    def test_glossary_fingerprint_override_explicit(self, tmp_path,
                                                    monkeypatch):
        """v1.3.0 D2 终选：override 覆盖词表显式参与指纹。

        - override 内容变化 → 指纹变化；
        - override 未启用（空串）→ 指纹与旧两级链（人工+learned）一致。
        """
        gl = tmp_path / "glossary.csv"
        gl.write_text("先生,老师\n", encoding="utf-8")
        monkeypatch.setattr(v2_manifest_fp, "learned_glossary_path",
                            lambda: str(gl))

        class _Cfg:
            glossary_path = ""
            glossary_override_path = ""

        # 无 override：与旧版两级链指纹一致
        base_fp = v2_manifest_fp._glossary_fingerprint(_Cfg())
        expect = hashlib.sha1(
            v2_manifest_fp.compute_glossary_sha1(str(gl)).encode("utf-8")
        ).hexdigest()
        assert base_fp == expect

        # override 启用：指纹必变，且显式进入联合 sha1
        ovr = tmp_path / "override.csv"
        ovr.write_text("先生,覆盖老师\n", encoding="utf-8")

        class _CfgOvr:
            glossary_path = ""
            glossary_override_path = str(ovr)

        fp_ovr = v2_manifest_fp._glossary_fingerprint(_CfgOvr())
        assert fp_ovr != base_fp
        expect_ovr = hashlib.sha1("|".join([
            v2_manifest_fp.compute_glossary_sha1(str(gl)),
            v2_manifest_fp.compute_glossary_sha1(str(ovr)),
        ]).encode("utf-8")).hexdigest()
        assert fp_ovr == expect_ovr

        # override 内容变化 → 指纹变化
        ovr.write_text("先生,另一译\n", encoding="utf-8")
        assert v2_manifest_fp._glossary_fingerprint(_CfgOvr()) != fp_ovr


# ---------------------------------------------------------------------------
# v2_outputs：陈旧风险清单清理与原子写
# ---------------------------------------------------------------------------

class TestOutputs:
    def test_remove_stale_risk_reports(self, tmp_path):
        """_remove_stale_risk_reports：清四件（W1a 扩的导读 json 与 D11
        契约④扩的重翻台账——此处在"写前清陈旧"语义下，目录里的是上一轮
        遗留件）并返回文件名名单；新写成品不碰。"""
        md = tmp_path / "movie_风险清单.md"
        js = tmp_path / "movie_风险清单.json"
        stale_guide = tmp_path / "movie_质量报告导读.json"   # 上一轮陈旧件
        stale_ledger = tmp_path / "movie_重翻记录.json"      # 上一轮台账（D11）
        keep = tmp_path / "movie_final_cn.srt"
        for p in (md, js, stale_guide, stale_ledger, keep):
            p.write_text("x", encoding="utf-8")
        removed = v2_outputs._remove_stale_risk_reports(str(tmp_path),
                                                        "movie")
        assert sorted(removed) == ["movie_质量报告导读.json",
                                   "movie_重翻记录.json",
                                   "movie_风险清单.json",
                                   "movie_风险清单.md"]
        assert not md.exists() and not js.exists()
        assert not stale_guide.exists()   # 陈旧导读 json 已清（写前）
        assert not stale_ledger.exists()  # 陈旧重翻台账已清（写前）
        assert keep.exists()        # 成品不碰
        # 幂等：再清一次返回空名单
        assert v2_outputs._remove_stale_risk_reports(str(tmp_path),
                                                     "movie") == []

    def test_atomic_write_text_roundtrip(self, tmp_path):
        """_atomic_write_text：写后内容一致，且不残留临时文件。"""
        target = tmp_path / "out.srt"
        v2_outputs._atomic_write_text(str(target), "1\n00:00:00,000 --> "
                                                "00:00:01,000\n你好\n")
        assert target.read_text(encoding="utf-8") == (
            "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
        assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# v2_learn：TM 学习准入纯分支
# ---------------------------------------------------------------------------

class _StubTM:
    def __init__(self):
        self.batches = []

    def store_batch(self, pairs, source_name=None):
        self.batches.append((list(pairs), source_name))
        return len(pairs)


class TestLearn:
    def test_learn_to_tm_gate_branches(self):
        """_learn_to_tm 纯准入分支：正常 1:1 入库；必看行/validator 行
        被门槛拦截；时间轴不对齐行不学习。"""
        tm = _StubTM()
        orig = [
            {"index": 0, "timing": "00:00:01,000 --> 00:00:02,000",
             "text": "おはよう"},
            {"index": 1, "timing": "00:00:03,000 --> 00:00:04,000",
             "text": "行くぞ"},
            {"index": 2, "timing": "00:00:05,000 --> 00:00:06,000",
             "text": "危ない"},
        ]
        final = [
            {"index": 0, "timing": "00:00:01,000 --> 00:00:02,000",
             "text": "早上好"},
            {"index": 1, "timing": "00:00:03,000 --> 00:00:04,000",
             "text": "上吧"},
            {"index": 2, "timing": "00:00:05,000 --> 00:00:06,000",
             "text": "危险"},
        ]
        must_see = {v2_premerge._timing_span(
            "00:00:03,000 --> 00:00:04,000")}
        # 必看行（层3）先于 validator（层1）判定：index 1 被必看拦下，
        # index 2 被 validator 拦下，只有 index 0 入库。
        n = v2_learn._learn_to_tm(
            tm, orig, final, flagged={2}, gate=True,
            must_see_spans=must_see, file_name="ep01.srt")
        assert n == 1                     # 只有第 0 条入库
        pairs, source_stem = tm.batches[0]
        assert pairs == [("おはよう", "早上好", 1)]
        assert source_stem == "ep01"      # TM provenance：来源 stem

    def test_learn_to_tm_no_candidates(self):
        """无候选（同文残留对）→ 入库 0，store_batch 不被调用。"""
        tm = _StubTM()
        e = {"index": 0, "timing": "00:00:01,000 --> 00:00:02,000",
             "text": "同じ"}
        assert v2_learn._learn_to_tm(tm, [e], [dict(e)]) == 0
        assert tm.batches == []


# ---------------------------------------------------------------------------
# v2_rules：语言过滤 + [未翻译] 回填 + 时间轴排序
# ---------------------------------------------------------------------------

class TestRules:
    def test_filter_language_keeps_marks_and_sorts(self, monkeypatch):
        """_filter_language：被过滤条目加 [未翻译] 前缀并回（不丢行）、
        有效条目恢复原 index、产物按时间轴排序、已标记条目跳过校验。"""
        entries = [
            {"index": 0, "timing": "00:00:01,000 --> 00:00:02,000",
             "text": "こんにちは"},
            {"index": 1, "timing": "00:00:03,000 --> 00:00:04,000",
             "text": "中文正常"},
            {"index": 2, "timing": "00:00:00,000 --> 00:00:00,500",
             "text": "[未翻译] なに"},
        ]

        def _fake_filter(srt, stage_idx, target):
            # 模拟语言白名单：第 0 条通过，第 1 条被判非中文
            kept = [{"index": 9, "timing": entries[0]["timing"],
                     "text": "你好"}]
            return kept, 1

        monkeypatch.setattr(v2_rules, "filter_stage_output_srt",
                            _fake_filter)

        class _Cfg:
            v2_profile = "local"

        out = v2_rules._filter_language(_Cfg(), entries, 3)
        assert [e["timing"] for e in out] == [
            "00:00:00,000 --> 00:00:00,500",   # 排序按时间轴起点
            "00:00:01,000 --> 00:00:02,000",
            "00:00:03,000 --> 00:00:04,000",
        ]
        assert out[0] == {"index": 2,                        # 已标记条目原样保留
                          "timing": "00:00:00,000 --> 00:00:00,500",
                          "text": "[未翻译] なに"}
        assert out[1]["index"] == 0                         # 有效条目恢复原 index
        assert out[1]["text"] == "你好"
        assert out[2]["text"] == "[未翻译] 中文正常"          # 被过滤条目加标回填

    def test_apply_fallback_rules_lenient_short_circuit(self):
        """_apply_fallback_rules：cloud(lenient) 档直通，不触发清洗。"""
        entries = [{"index": 0, "timing": "00:00:01,000 --> 00:00:02,000",
                    "text": "x"}]

        class _Cfg:
            v2_profile = "cloud"

        out = v2_rules._apply_fallback_rules(_Cfg(), entries, [])
        assert out[0] is entries
        assert out[1] == [] and out[2] is None and out[3] == set() \
            and out[4] is None


# ---------------------------------------------------------------------------
# 副本一致性钉与 facade 绑定钉（防漂移）
# ---------------------------------------------------------------------------

class TestFacadeBinding:
    def test_stage_constants_leaf_copy_matches_facade(self):
        """批次 1 带出叶子模块的 V2_STAGE_SLOT/TAGS 常量副本与 facade
        逐值对齐（防两处漂移）。"""
        assert v2_manifest_fp.V2_STAGE_SLOT == pv.V2_STAGE_SLOT
        assert v2_manifest_fp.V2_STAGE_TAGS == pv.V2_STAGE_TAGS

    def test_reexport_binding_same_object(self):
        """职责边界回归：pipeline_v2 的 re-export 与叶子模块是同一对象
        （re-export 绑定不断裂）。"""
        assert pv._remove_stale_risk_reports \
            is v2_outputs._remove_stale_risk_reports
        assert pv._backup_existing_outputs \
            is v2_outputs._backup_existing_outputs
        # 批次 2 迁出符号同样钉住
        import subtransjav.refine.v2_learn as _vl
        import subtransjav.refine.v2_rules as _vr
        assert pv._learn_to_tm is _vl._learn_to_tm
        assert pv._auto_learn_glossary is _vl._auto_learn_glossary
        assert pv._apply_fallback_rules is _vr._apply_fallback_rules
        assert pv._filter_language is _vr._filter_language
