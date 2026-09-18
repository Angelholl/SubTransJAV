"""
v2 共享工具（临时目录/TM 初始化/词库合并/路径解析）
====================================================
pipeline_v2（两阶段管线）复用的公共工具：
  - 临时目录登记与退出清理（CREATED_TMP_DIRS / cleanup_created_tmp_dirs）
  - 翻译记忆库初始化（_init_tm）
  - 词库合并加载（load_glossary_merged / learned_glossary_path）
  - 阶段路径解析（_resolve_stage_paths / refine_tmp_dir / strip_lang_suffix）
legacy 管线（已删除）的编排器（run 流程与单文件执行及其阶段辅助函数）不再保留。
"""

import hashlib
import os
import threading
from pathlib import Path

# 本次进程创建过的临时目录（供退出时清理）
CREATED_TMP_DIRS = []
_tmp_dirs_lock = threading.Lock()

from .config import TEMP_DIR, RefineConfig  # noqa: E402  # 延迟导入规避循环依赖
from .glossary import load_glossary_ex  # noqa: E402  # 延迟导入规避循环依赖


class RefineError(Exception):
    pass


def _init_tm(cfg: RefineConfig):
    """初始化翻译记忆库（如启用）。返回 TranslationMemory 或 None。"""
    if not cfg.tm_enabled:
        return None
    try:
        from .tm import TranslationMemory
        tm = TranslationMemory(cfg.tm_db_path) if cfg.tm_db_path else TranslationMemory()
        return tm
    except Exception as e:
        print(f"⚠️ [refine] 翻译记忆库初始化失败，已忽略: {e}")
        return None


def strip_lang_suffix(stem: str) -> str:
    """去掉文件名 stem 中的语言后缀（.japanese/.chinese/.translated）。

    单一实现：产物命名与 webview_gui/api.py 临时目录
    计算均复用本函数，避免两处实现漂移。
    """
    for suf in (".japanese", ".chinese", ".translated"):
        if stem.endswith(suf):
            return stem[: -len(suf)]
    return stem


def _resolve_stage_paths(cfg: RefineConfig, in_path: str):
    p = Path(in_path).resolve()
    out_dir = Path(cfg.output_dir).resolve() if cfg.output_dir else p.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = strip_lang_suffix(p.stem)    # 清理上游可能带来的语言后缀
    return str(p), str(out_dir), stem


def refine_tmp_dir(in_path: str, stem: str) -> str:
    """输入文件对应的流水线工作区，统一收束到 项目根/Temp/。

    目录名 = 输入文件名 + 输入完整路径哈希（跨目录同名文件互不冲突）。
    GUI 退出清理与 CLI 清理均基于此单一来源。
    """
    h = hashlib.sha1(Path(in_path).resolve().as_posix().lower().encode("utf-8")).hexdigest()[:10]
    d = Path(TEMP_DIR) / f"{stem}.{h}.refine_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def learned_glossary_path() -> str:
    """自动学习词库路径（项目根/config/glossary_learned.csv）。"""
    return os.path.join(
        os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
        "config", "glossary_learned.csv")


def load_glossary_merged(cfg: RefineConfig) -> list:
    """加载人工词库 + 自动学习词库（learned 追加，不覆盖人工条目）。

    v1.2.2 D：返回三元组 ``[(src, dst, aliases), ...]``——第三列为
    人工词库可选列 target_aliases（`|` 分隔，缺列/空 = 无别名）；
    learned 自学习词库不生成别名（恒为空元组）。两列消费者
    （match_glossary / format_glossary_block）已兼容三列词条。
    """
    glossary = load_glossary_ex(cfg.glossary_path) if cfg.glossary_path else []
    _learned = load_glossary_ex(learned_glossary_path())
    if _learned:
        _existing_srcs = {s for s, _d, _a in glossary}
        for s, d, a in _learned:
            if s not in _existing_srcs:
                glossary.append((s, d, a))
    if glossary:
        print(f"📚 [refine] 词库已加载：{len(glossary)} 条")
    return glossary


def cleanup_created_tmp_dirs():
    """删除本进程创建过的全部 .refine_tmp 临时目录（忽略错误）"""
    import shutil
    with _tmp_dirs_lock:
        snapshot = list(CREATED_TMP_DIRS)
        CREATED_TMP_DIRS.clear()
    cleaned = []
    for d in snapshot:
        if d and Path(d).is_dir():
            shutil.rmtree(d, ignore_errors=True)
            cleaned.append(d)
    return cleaned
