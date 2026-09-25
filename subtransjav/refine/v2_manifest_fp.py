"""指纹与 manifest 准备层（1.3.0 pipeline_v2 拆分批次 1·模块 C）
====================================================

职责：词库/TM/模型信息三指纹计算与任务清单（manifest）的载入、
校验、新建与指纹刷新。

迁出来源：subtransjav/refine/pipeline_v2.py 逐字迁移以下符号：
- _glossary_fingerprint
- _TM_FINGERPRINT_COLUMNS（含前置注释）
- _tm_fingerprint
- _v2_models_payload（依赖 V2_STAGE_TAGS/V2_STAGE_SLOT 常量，一并逐字带出）
- _prepare_manifest

行为等价声明：迁出函数体逐字零改动，与原模块行为 HRO-2 逐字节
等价，符合决策 D2026-0925-01 执行契约。本模块单向只依赖叶子模块
（manifest/pipeline_support/config），严禁反向依赖 pipeline_v2。
"""

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import RefineConfig
from .manifest import (
    MANIFEST_VERSION,
    TaskManifest,
    compute_config_hash,
    compute_file_sha1,
    compute_glossary_sha1,
    load_manifest,
    manifest_path,
    save_manifest,
    validate_manifest,
)
from .pipeline_support import learned_glossary_path


def _glossary_fingerprint(cfg) -> str | None:
    """词库指纹：覆盖词表/人工词库/自动学习词库三者联合 sha1。

    v1.3.0 D2 终选（D2026-0925-01 补充裁决）：override 覆盖词表
    **显式**参与指纹（第三项），启用与否/内容变化都会让旧产物失效；
    空串/None 时跳过该项（与既有 None/missing→None 语义对齐）。
    三层全缺 -> None（校验时跳过）；任一存在则参与联合指纹，
    保证自动学习追加的词库变化也会让旧产物失效。
    """
    parts = [
        compute_glossary_sha1(getattr(cfg, "glossary_path", "") or None),
        compute_glossary_sha1(learned_glossary_path()),
        # 显式进指纹：override 是用户显式传入的最高优先词表，非自动
        compute_glossary_sha1(
            getattr(cfg, "glossary_override_path", "") or None),
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# TM 指纹参与的内容列（按 content_hash, stage 稳定排序，跨进程确定性）。
# 刻意排除 hit_count / created_at 等簿记列（D5）：阶段A 每次精确命中都会
# 自增 hit_count 并提交（tm.exact_map / lookup_exact），变更还可能滞留
# WAL、被下一次连接打开时回放——若对整文件做 sha1，manifest 创建后 TM
# 文件字节几乎必然变化，同配置 --resume 必拒绝复用阶段A。
_TM_FINGERPRINT_COLUMNS = ("content_hash", "stage", "source_text", "target_text")


def _tm_fingerprint(cfg, tm) -> str | None:
    """翻译记忆库指纹：tm_entries 内容列按稳定排序的流式 sha1（D5）。

    - 只取内容列（_TM_FINGERPRINT_COLUMNS），排除 hit_count/created_at
      等簿记列：命中只改簿记、不改翻译结果，不应使已有阶段产物失效；
      内容条目新增/修改则指纹必变。
    - 未启用/文件缺失/无表/文件损坏 -> None（校验侧按"跳过"处理）。
    """
    db = getattr(tm, "db_path", None) if tm is not None else None
    if not db:
        db = getattr(cfg, "tm_db_path", "") or None
    if not db or not Path(db).is_file():
        return None
    digest = hashlib.sha1()
    try:
        conn = sqlite3.connect(db)
        try:
            # 列清单与排序键均为模块顶 _TM_FINGERPRINT_COLUMNS 同源的固定字面量
            # （SQLite 标识符不可参数绑定；单行内联字面量，零拼接面——Mimosa L2 2026-09-25；
            #   改列须同步常量与本地量，指纹用例护航）
            cursor = conn.execute("SELECT content_hash, stage, source_text, target_text FROM tm_entries ORDER BY content_hash, stage")  # 游标只建一次，fetchmany 顺序推进
            while True:
                rows = cursor.fetchmany(4096)
                if not rows:
                    break
                for row in rows:
                    digest.update(
                        "\x1f".join(str(v) for v in row).encode("utf-8"))
                    digest.update(b"\n")
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return digest.hexdigest()


V2_STAGE_TAGS = ("A", "B")
# v2 阶段复用 legacy 各阶段槽位的服务商/模型配置：A→stages[0]，B→stages[2]
V2_STAGE_SLOT = {"A": 0, "B": 2}


def _v2_models_payload(cfg: RefineConfig) -> dict:
    """清单用模型信息：{"A": {provider,model,endpoint}, "B": {...}}。"""
    models = {}
    for tag in V2_STAGE_TAGS:
        s = cfg.stages[V2_STAGE_SLOT[tag]]
        models[tag] = {
            "provider": s.provider,
            "model": cfg.resolve_model(s) or "",
            "endpoint": cfg.resolve_endpoint(s.provider) or "",
        }
    return models


def _prepare_manifest(cfg: RefineConfig, in_path: str, out_dir: str,
                      stem: str, tm) -> tuple:
    """创建/加载任务清单并做指纹校验。返回 (manifest, trusted)。

    trusted=True 表示清单指纹校验通过（或 force_resume 强制采信），
    配合 cfg.resume 才允许复用已有阶段产物；不复用时新建清单覆盖。
    """
    m_path = manifest_path(out_dir, stem)
    input_sha1 = compute_file_sha1(in_path)
    config_hash = compute_config_hash(cfg)
    gl_fp = _glossary_fingerprint(cfg)
    tm_fp = _tm_fingerprint(cfg, tm)
    manifest = load_manifest(m_path)
    if manifest is not None:
        reasons = validate_manifest(manifest, input_sha1=input_sha1,
                                    config_hash=config_hash,
                                    glossary_sha1=gl_fp, tm_sha1=tm_fp)
        if not reasons:
            return manifest, True
        if cfg.force_resume:
            # 外层前缀只描述"校验未通过"，具体变化明细以 reasons 为准，
            # 避免出现"配置已变化：配置已变化"式重复（O4）。
            print(f"⚠️ 强制复用（指纹校验不匹配：{'、'.join(reasons)}），"
                  f"继续复用已有产物")
            # 指纹刷新为当前值，保持清单自洽
            manifest.input_sha1 = input_sha1
            manifest.input_size = Path(in_path).stat().st_size
            manifest.config_hash = config_hash
            manifest.glossary_sha1 = gl_fp
            manifest.tm_sha1 = tm_fp
            save_manifest(m_path, manifest)
            return manifest, True
        print(f"⚠️ 不复用（{'、'.join(reasons)}），将重跑")
        # D3：拒绝复用不得立即重写清单文件——否则上一轮中断现场（如
        # stages.A=done）会被全新 pending 清单覆盖，--force-resume 随即
        # 失去复用前提。此处仅在内存中刷新指纹、保留磁盘原文件；待阶段
        # 实际重跑（mark_running -> save_manifest）时才落盘推进。
        manifest.input_sha1 = input_sha1
        manifest.input_size = Path(in_path).stat().st_size
        manifest.config_hash = config_hash
        manifest.glossary_sha1 = gl_fp
        manifest.tm_sha1 = tm_fp
        return manifest, False
    now = datetime.now().isoformat(timespec="seconds")
    manifest = TaskManifest(
        manifest_version=MANIFEST_VERSION,
        input_path=in_path, input_sha1=input_sha1,
        input_size=Path(in_path).stat().st_size,
        config_hash=config_hash, glossary_sha1=gl_fp, tm_sha1=tm_fp,
        models=_v2_models_payload(cfg), out_dir=out_dir, stem=stem,
        started_at=now, updated_at=now, run_pid=os.getpid())
    save_manifest(m_path, manifest)
    return manifest, False
