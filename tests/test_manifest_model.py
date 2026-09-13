"""任务清单模型契约测试：原子往返 / 损坏容错 / 指纹敏感度 / 校验原因 / 断点清理。"""

import hashlib
import json
import types

from subtransjav.refine.config import StageConfig
from subtransjav.refine.manifest import (
    MANIFEST_VERSION,
    STAGE_STATUSES,
    StageRecord,
    TaskManifest,
    compute_config_hash,
    compute_dir_hash,
    compute_file_sha1,
    compute_glossary_sha1,
    delete_resume_artifacts,
    load_manifest,
    manifest_path,
    save_manifest,
    validate_manifest,
)

# ---------------------------------------------------------------------------
# 工具：用 SimpleNamespace 伪造 cfg（无需真实 RefineConfig）
# ---------------------------------------------------------------------------

def _make_cfg(**overrides) -> types.SimpleNamespace:
    cfg = types.SimpleNamespace()
    cfg.profile = None
    cfg.stages = [
        StageConfig(0, True, "lmstudio", "model-a"),
        StageConfig(2, True, "deepseek", "model-b"),
    ]
    cfg.endpoints = {"zen": "https://opencode.ai/zen/v1"}
    cfg.batch_local = 30
    cfg.batch_cloud = 30
    cfg.batch_size_stable = True
    cfg.v2_profile = "local"
    cfg.v2_concurrency = 1
    cfg.v2_ctx_local = 32768
    cfg.v2_keep_untranslated = "original"
    cfg.premerge_enabled = True
    cfg.tm_enabled = True
    cfg.tm_threshold = 0.85
    cfg.tm_fuzzy_inject = True
    cfg.tm_fuzzy_threshold = 0.98
    cfg.tm_learn_gate = True
    cfg.apply_glossary_stage1 = True
    cfg.apply_glossary_stage2 = True
    cfg.fallback_local = False
    cfg.fallback_model = ""
    cfg.templates_dir = ""
    cfg.cleaner_config_dir = ""
    # 以下字段不参与哈希（应不影响指纹结果）
    cfg.inputs = ["a.srt"]
    cfg.output_dir = "out"
    cfg.force = False
    cfg.api_key_deepseek = "sk-aaa"
    cfg.deepseek_key = "legacy-key"          # 旧字段名也一并验证
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_manifest(**kw) -> TaskManifest:
    m = TaskManifest(
        manifest_version=MANIFEST_VERSION,
        input_path="D:/videos/ep01.srt",
        input_sha1="a" * 40,
        input_size=12345,
        config_hash="c" * 40,
        glossary_sha1="g" * 40,
        tm_sha1="m" * 40,
        models={"A": {"provider": "lmstudio", "model": "model-a", "endpoint": "http://x/v1"},
                "B": {"provider": "deepseek", "model": "model-b", "endpoint": ""}},
        out_dir="D:/out",
        stem="ep01",
        outputs={"A": "D:/out/ep01_refine_A.srt", "final": None},
        stages={"A": StageRecord(status="done", completed_at="2026-09-12T10:00:00",
                                 output="D:/out/ep01_refine_A.srt", entries=300, degraded_count=2),
                "B": StageRecord(), "final": StageRecord()},
        started_at="2026-09-12T09:00:00",
        updated_at="2026-09-12T10:00:00",
        run_pid=4321,
    )
    for k, v in kw.items():
        setattr(m, k, v)
    return m


# ---------------------------------------------------------------------------
# 状态机与常量
# ---------------------------------------------------------------------------

def test_constants_and_stage_statuses():
    assert MANIFEST_VERSION == 1
    assert STAGE_STATUSES == ("pending", "running", "done", "degraded", "failed")


def test_mark_running_and_done_update_timestamps():
    m = _make_manifest()
    m.mark_running("B")
    assert m.stages["B"].status == "running"
    assert m.updated_at != ""
    m.mark_done("B", "D:/out/ep01_final.srt", entries=298, degraded_count=1)
    rec = m.stages["B"]
    assert rec.status == "done"
    assert rec.output == "D:/out/ep01_final.srt"
    assert rec.entries == 298 and rec.degraded_count == 1
    assert rec.completed_at is not None and rec.completed_at != ""


def test_manifest_path():
    assert manifest_path("D:/out", "ep01").name == "ep01_manifest.json"


# ---------------------------------------------------------------------------
# save/load 原子往返
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(tmp_path):
    m = _make_manifest()
    path = tmp_path / "ep01_manifest.json"
    save_manifest(path, m)
    loaded = load_manifest(path)
    assert loaded is not None
    assert loaded.to_dict() == m.to_dict()
    # 原子写不留 .tmp 残留
    assert not list(tmp_path.glob("*.tmp"))


def test_save_creates_parent_dirs(tmp_path):
    deep = tmp_path / "x" / "y"
    save_manifest(deep / "m.json", _make_manifest())
    assert (deep / "m.json").is_file()


def test_load_missing_or_corrupt_or_version_mismatch(tmp_path):
    # 不存在
    assert load_manifest(tmp_path / "nope.json") is None
    # JSON 损坏
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")
    assert load_manifest(bad) is None
    # 合法 JSON 但不是对象
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    assert load_manifest(arr) is None
    # 版本不符
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"manifest_version": 99, "input_sha1": "x"}), encoding="utf-8")
    assert load_manifest(old) is None


# ---------------------------------------------------------------------------
# validate_manifest 四类原因
# ---------------------------------------------------------------------------

def test_validate_manifest_ok_and_reasons():
    m = _make_manifest()
    # 全部匹配 -> 可复用
    assert validate_manifest(m, input_sha1="a" * 40, config_hash="c" * 40,
                             glossary_sha1="g" * 40, tm_sha1="m" * 40) == []
    # 单项变化 -> 对应中文原因
    assert validate_manifest(m, input_sha1="b" * 40, config_hash="c" * 40) == ["输入文件已变化"]
    assert validate_manifest(m, input_sha1="a" * 40, config_hash="d" * 40) == ["配置已变化"]
    assert validate_manifest(m, input_sha1="a" * 40, config_hash="c" * 40,
                             glossary_sha1="h" * 40) == ["术语表已变化"]
    assert validate_manifest(m, input_sha1="a" * 40, config_hash="c" * 40,
                             tm_sha1="n" * 40) == ["翻译记忆库已变化"]
    # 多项同时变化 -> 全部列出
    reasons = validate_manifest(m, input_sha1="b" * 40, config_hash="d" * 40,
                                glossary_sha1="h" * 40, tm_sha1="n" * 40)
    assert reasons == ["输入文件已变化", "配置已变化", "术语表已变化", "翻译记忆库已变化"]
    # glossary/tm 为 None -> 跳过对应检查（即使清单里记录了旧指纹）
    assert validate_manifest(m, input_sha1="a" * 40, config_hash="c" * 40,
                             glossary_sha1=None, tm_sha1=None) == []


# ---------------------------------------------------------------------------
# compute_config_hash 敏感度
# ---------------------------------------------------------------------------

def test_config_hash_stable_for_identical_cfg():
    h1 = compute_config_hash(_make_cfg())
    h2 = compute_config_hash(_make_cfg())
    assert h1 == h2 and len(h1) == 40


def test_config_hash_insensitive_to_keys_inputs_switches(tmp_path):
    base = compute_config_hash(_make_cfg())
    # API key / inputs / 输出目录 / 运行开关变化 -> 哈希不变
    assert compute_config_hash(_make_cfg(api_key_deepseek="sk-bbb")) == base
    assert compute_config_hash(_make_cfg(deepseek_key="new-key")) == base
    assert compute_config_hash(_make_cfg(inputs=["b.srt"])) == base
    assert compute_config_hash(_make_cfg(output_dir="other")) == base
    assert compute_config_hash(_make_cfg(force=True)) == base


def test_config_hash_sensitive_to_meaningful_fields():
    base = compute_config_hash(_make_cfg())
    cases = [
        _make_cfg(v2_profile="cloud"),                       # 档位
        _make_cfg(batch_local=20),                           # 批大小
        _make_cfg(batch_cloud=25),
        _make_cfg(tm_enabled=False),                         # TM 开关
        _make_cfg(tm_threshold=0.9),
        _make_cfg(v2_keep_untranslated="empty"),
        _make_cfg(fallback_local=True),
        _make_cfg(endpoints={"zen": "http://changed/v1"}),   # 端点
    ]
    for i, cfg in enumerate(cases):
        assert compute_config_hash(cfg) != base, f"case {i} 未引起哈希变化"


def test_config_hash_sensitive_to_stage_fields():
    base_cfg = _make_cfg()
    base = compute_config_hash(base_cfg)
    # 改 provider / model / enabled / endpoint / instructions 都必须变
    for mutate in (
        lambda c: setattr(c.stages[0], "provider", "ollama"),
        lambda c: setattr(c.stages[0], "model", "other-model"),
        lambda c: setattr(c.stages[1], "enabled", False),
        lambda c: setattr(c.stages[1], "endpoint", "http://override/v1"),
        lambda c: setattr(c.stages[0], "instructions", "tpl.txt"),
    ):
        cfg = _make_cfg()
        mutate(cfg)
        assert compute_config_hash(cfg) != base


def test_config_hash_tracks_role_card_content_only(tmp_path):
    """D2 回归：指纹只跟踪管线实际加载的角色卡内容，不哈希 templates_dir 整树。"""
    tpl = tmp_path / "templates"
    tpl.mkdir()
    card_a = tpl / "角色-净语翻译.txt"
    card_a.write_text("角色卡A", encoding="utf-8")
    (tpl / "角色-审校抛光.txt").write_text("角色卡B", encoding="utf-8")
    h1 = compute_config_hash(_make_cfg(templates_dir=str(tpl)))
    # 角色卡内容变化 -> 指纹变化（指令变了产物必须失效）
    card_a.write_text("角色卡A改", encoding="utf-8")
    h2 = compute_config_hash(_make_cfg(templates_dir=str(tpl)))
    assert h1 != h2
    # 目录内无关文件增删（含子目录，模拟上一轮运行写出的 Temp/Logs 产物）
    # -> 指纹不变（旧实现整树哈希会把同配置 --resume 误判为"配置已变化"）
    (tpl / "unrelated.txt").write_text("无关文件", encoding="utf-8")
    sub = tpl / "Temp"
    sub.mkdir()
    (sub / "run.log").write_text("上一轮运行产物", encoding="utf-8")
    assert compute_config_hash(_make_cfg(templates_dir=str(tpl))) == h2


def test_config_hash_templates_dir_dot_aligned_with_loader(tmp_path, monkeypatch):
    """D2 回归：templates_dir="."（CLI 默认，=进程 CWD）必须与加载侧同规则
    解析为默认模板目录——同配置跨运行指纹稳定，不受 CWD 运行产物影响。"""
    from subtransjav.refine.config import default_templates_dir
    assert compute_config_hash(_make_cfg(templates_dir=".")) == \
        compute_config_hash(_make_cfg(templates_dir=default_templates_dir()))
    assert compute_config_hash(_make_cfg(templates_dir="./")) == \
        compute_config_hash(_make_cfg(templates_dir=".."))
    # 不同 CWD（含运行期写出的 Temp/Logs 假产物）下指纹一致
    d1 = tmp_path / "run1"
    (d1 / "Temp").mkdir(parents=True)
    (d1 / "Temp" / "x.tmp").write_text("junk", encoding="utf-8")
    (d1 / "Logs").mkdir()
    (d1 / "Logs" / "log.txt").write_text("日志", encoding="utf-8")
    d2 = tmp_path / "run2"
    d2.mkdir()
    cfg = _make_cfg(templates_dir=".")
    monkeypatch.chdir(d1)
    h1 = compute_config_hash(cfg)
    monkeypatch.chdir(d2)
    assert compute_config_hash(cfg) == h1


def test_config_hash_sensitive_to_rules_yaml(tmp_path, monkeypatch):
    """指令源之一 translation_rules.yaml（阶段B加固段数据源）内容变化 -> 指纹变化。"""
    import subtransjav.refine.manifest as mf
    rules = tmp_path / "translation_rules.yaml"
    rules.write_text("prompt_sections: {hardened_suffix: v1}", encoding="utf-8")
    monkeypatch.setattr(mf, "_rules_yaml_path", lambda: str(rules))
    h1 = compute_config_hash(_make_cfg())
    rules.write_text("prompt_sections: {hardened_suffix: v2}", encoding="utf-8")
    assert compute_config_hash(_make_cfg()) != h1


def test_v2_template_constants_pinned_to_pipeline():
    """契约：manifest 侧角色卡文件名/槽位必须与 pipeline_v2 加载侧一致（防漂移）。"""
    import subtransjav.refine.manifest as mf
    from subtransjav.refine import pipeline_v2 as pv
    assert mf._V2_TEMPLATE_FILES == pv.V2_TEMPLATE_FILES
    assert tuple(mf._V2_STAGE_SLOTS) == tuple(pv.V2_STAGE_SLOT.items())


def test_instruction_source_files_match_loader(tmp_path, monkeypatch):
    """契约：指纹侧收集的指令源文件必须与加载侧 _load_v2_instruction 实际
    读取一致（角色卡目录解析、显式 instructions 优先）。"""
    import subtransjav.refine.manifest as mf
    from subtransjav.refine import pipeline_v2 as pv

    tpl = tmp_path / "templates"
    tpl.mkdir()
    (tpl / "角色-净语翻译.txt").write_text("A卡", encoding="utf-8")
    (tpl / "角色-审校抛光.txt").write_text("B卡", encoding="utf-8")
    cfg = _make_cfg(
        templates_dir=str(tpl),
        stages=[StageConfig(0, True, "lmstudio", ""),
                StageConfig(1, False, "deepseek", ""),
                StageConfig(2, True, "lmstudio", ""),
                StageConfig(3, False, "lmstudio", "")])

    seen = []
    monkeypatch.setattr(pv, "_read_v2_card",
                        lambda tag, explicit, td:
                        seen.append((tag, explicit, td)) or "### prompt\nx")
    work = tmp_path / "work"
    work.mkdir()
    pv._load_v2_instruction(cfg, "A", "", str(work))
    pv._load_v2_instruction(cfg, "B", "", str(work))
    # 加载侧实际使用的目录 == 指纹侧解析结果
    assert seen and all(td == mf._resolve_templates_dir(cfg)
                        for _, _, td in seen)
    # 指纹侧收录两个默认角色卡 + translation_rules.yaml
    files = mf.instruction_source_files(cfg)
    assert str(tpl / "角色-净语翻译.txt") in files
    assert str(tpl / "角色-审校抛光.txt") in files
    assert any(p.endswith("translation_rules.yaml") for p in files)
    # 显式 instructions 指向存在的文件时优先生效（与 _read_v2_card 同判定）
    explicit = tmp_path / "custom_card.txt"
    explicit.write_text("自定义卡", encoding="utf-8")
    cfg.stages[0].instructions = str(explicit)
    assert str(explicit) in mf.instruction_source_files(cfg)


def test_config_hash_tolerates_bare_namespace():
    # 缺失字段全部记 None，不抛异常
    h = compute_config_hash(types.SimpleNamespace())
    assert len(h) == 40


# ---------------------------------------------------------------------------
# 指纹工具函数
# ---------------------------------------------------------------------------

def test_compute_file_sha1_matches_hashlib(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello world")
    assert compute_file_sha1(f) == hashlib.sha1(b"hello world").hexdigest()


def test_compute_glossary_sha1_skip_on_none(tmp_path):
    assert compute_glossary_sha1(None) is None
    assert compute_glossary_sha1(tmp_path / "missing.csv") is None
    g = tmp_path / "glossary.csv"
    # 用 write_bytes 绕开 Windows 文本模式的 \n -> \r\n 翻译，保证字节级一致
    g.write_bytes("なに,什么\n".encode())
    assert compute_glossary_sha1(g) == hashlib.sha1("なに,什么\n".encode()).hexdigest()


def test_compute_dir_hash(tmp_path):
    assert compute_dir_hash(tmp_path / "missing") is None
    assert compute_dir_hash("") is None
    d = tmp_path / "cfg"
    d.mkdir()
    empty_hash = compute_dir_hash(d)
    assert empty_hash is not None            # 空目录也有指纹
    (d / "b.txt").write_text("2", encoding="utf-8")
    (d / "a.txt").write_text("1", encoding="utf-8")
    full_hash = compute_dir_hash(d)
    assert full_hash != empty_hash
    # 内容变化 -> 指纹变化
    (d / "a.txt").write_text("1!", encoding="utf-8")
    assert compute_dir_hash(d) != full_hash


# ---------------------------------------------------------------------------
# delete_resume_artifacts 只删目标文件
# ---------------------------------------------------------------------------

def test_delete_resume_artifacts_deletes_only_targets(tmp_path):
    keep1 = tmp_path / "ep01_final.srt"
    keep2 = tmp_path / "unrelated.txt"
    keep1.write_text("final", encoding="utf-8")
    keep2.write_text("x", encoding="utf-8")
    assert delete_resume_artifacts(str(tmp_path), "ep01") == []   # 无文件可删
    manifest_file = tmp_path / "ep01_manifest.json"
    refine_a = tmp_path / "ep01_refine_A.srt"
    manifest_file.write_text("{}", encoding="utf-8")
    refine_a.write_text("1\n00:00:00,000 --> 00:00:01,000\n原\n", encoding="utf-8")
    removed = delete_resume_artifacts(str(tmp_path), "ep01")
    assert sorted(removed) == ["ep01_manifest.json", "ep01_refine_A.srt"]
    assert not manifest_file.exists() and not refine_a.exists()
    assert keep1.exists() and keep2.exists()               # 其余产物不动
