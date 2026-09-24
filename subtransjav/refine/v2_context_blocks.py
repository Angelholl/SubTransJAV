"""
v2 语境注入块层：sidecar / 剧情摘要 / 词表与提示块
==================================================
职责：per-片语境 sidecar（{stem}.context.md）加载与注入块组装、
剧情自摘要注入块组装、按阶段词库开关的词表命中块构建。

迁出来源：subtransjav/refine/pipeline_v2.py（1.3.0 pipeline_v2 拆分
批次 1·模块 B）。除新增本 docstring 与 import 外，代码体逐字迁移、
零改动；行为等价依据 D2026-0922-03 HRO-2（逐字节等价）与
D2026-0925-01（执行契约）。
"""

from pathlib import Path

from .config import RefineConfig
from .glossary import format_glossary_block, match_glossary

# ---------------------------------------------------------------------------
# v1.2.2 C1 per-片语境 sidecar（{stem}.context.md，与输入 srt 同目录同名）：
# 一机制两用途——剧情摘要块（A/B 提示词均注入）+ 误听怀疑表（按当前批次
# 源文命中注入词条，复用 glossary 的命中风格）。挂载方式仿 glossary 块。
# ---------------------------------------------------------------------------
_SIDECAR_SUMMARY_TAG = "【剧情摘要】"
_SIDECAR_MISHEAR_TAG = "【误听怀疑】"
_SIDECAR_SUMMARY_HEADER = "【剧情摘要 - 语境参考】"
_SIDECAR_MISHEAR_HEADER = "【误听怀疑对照】"
# 冻结措辞（验收口径逐字比对，勿改动任何字）：
# 剧情摘要块引导语
SIDECAR_SUMMARY_NOTICE = (
    "以下为剧情背景参考，仅用于消解歧义；与单句字面义和术语表冲突时，"
    "以句子本身和术语表为准；不得改写原文中没有的信息。")
# 误听怀疑词条措辞（{疑似词}/{疑似正解} 为占位符）
SIDECAR_MISHEAR_NOTICE = (
    "该词在 ASR 转写中曾出现误听（{疑似词}→疑为{疑似正解}）。"
    "仅当上下文无法按字面义解读、且存在语义更合理的解读时方可按疑似义翻译；"
    "可两可时一律照字面译。")

# ---------------------------------------------------------------------------
# v1.2.2 Beta 剧情自摘要：闸门0+预合并后自动抽样整片剧情行，一次独立
# LLM 调用生成梗概，注入 A/B 提示词。手写 sidecar【剧情摘要】非空时
# 手写优先；任何失败静默跳过。摘要文本只进提示词，绝不写入输出目录/
# 终稿/质量报告；摘要缓存独立于 TM（Temp/synopsis_cache/）。摘要内容
# 与缓存不参与 manifest 指纹（与 sidecar 内容同款已知边界：换
# --s1-model 或改采样行为后须 --force 重跑方生效，见手册 §11）。
# ---------------------------------------------------------------------------
_SYNOPSIS_BLOCK_TAG = "【剧情背景（自动摘要·beta）】"


def _synopsis_prompt_block(synopsis_text: str | None) -> str:
    """组装自动摘要注入块（A/B 同措辞；引导语沿用 sidecar 冻结措辞）。"""
    if not synopsis_text or not synopsis_text.strip():
        return ""
    return "\n".join([_SYNOPSIS_BLOCK_TAG,
                      SIDECAR_SUMMARY_NOTICE,
                      synopsis_text.strip()])


def _v2_glossary_block(cfg: RefineConfig, tag: str, src_text: str,
                       glossary: list) -> str:
    """按阶段复用 legacy 词库生效开关：A→stage1 开关，B→stage2 开关。"""
    if not glossary:
        return ""
    enabled = (tag == "A" and cfg.apply_glossary_stage1) or \
              (tag == "B" and cfg.apply_glossary_stage2)
    if not enabled:
        return ""
    hits = match_glossary(src_text, glossary)
    if hits:
        print(f"   📚 词库命中 {len(hits)} 条，注入提示词")
        return format_glossary_block(hits)
    return ""


def load_context_sidecar(in_path: str) -> dict | None:
    """加载 per-片语境 sidecar（v1.2.2 C1）。

    文件约定：与输入 srt 同目录同名，后缀 ``.context.md``（如
    ``movie.srt`` -> ``movie.context.md``）。两小节：
      【剧情摘要】自由文本若干行；
      【误听怀疑】每行 ``疑似词 => 疑似正解``（如 ``ペソ => おへそ``）。

    文件不存在返回 None（=无注入）；解析按可得内容降级（缺小节=该块
    不注入；# 开头行视为模板注释跳过；误听行缺 "=>" 或侧为空则跳过）。
    返回 {"summary": [str, ...], "mishear": [(疑似词, 疑似正解), ...]}。
    """
    p = Path(in_path).with_suffix(".context.md")
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        print(f"   ⚠️ 语境 sidecar 读取失败，忽略: {p.name} ({e})")
        return None
    summary: list = []
    mishear: list = []
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(_SIDECAR_SUMMARY_TAG):
            section = "summary"
            rest = s[len(_SIDECAR_SUMMARY_TAG):].strip()
            if rest:
                summary.append(rest)
            continue
        if s.startswith(_SIDECAR_MISHEAR_TAG):
            section = "mishear"
            continue
        if not s or s.startswith("#"):
            continue
        if section == "summary":
            summary.append(s)
        elif section == "mishear" and "=>" in s:
            a, _, b = s.partition("=>")
            a, b = a.strip(), b.strip()
            if a and b:
                mishear.append((a, b))
    return {"summary": summary, "mishear": mishear}


def _load_context_sidecar(cfg: RefineConfig, in_path: str) -> dict | None:
    """cfg 开关接线的 sidecar 加载：context_sidecar=False 禁用（返回 None）；
    文件不存在=无注入。加载成功打印摘要行（可见性）。"""
    if not getattr(cfg, "context_sidecar", True):
        return None
    sidecar = load_context_sidecar(in_path)
    if sidecar and (sidecar["summary"] or sidecar["mishear"]):
        print(f"   📄 语境 sidecar: 剧情摘要 {len(sidecar['summary'])} 行 / "
              f"误听怀疑 {len(sidecar['mishear'])} 条"
              f"（{Path(in_path).with_suffix('.context.md').name}）")
    return sidecar


def _v2_sidecar_block(src_text: str, sidecar: dict | None) -> str:
    """组装语境 sidecar 注入块（A/B 同措辞；仿 glossary 块的命中风格）：

    - 剧情摘要块：sidecar 存在且小节非空即注入（冻结措辞引导语在前）；
    - 误听怀疑块：仅当当前批次源文含疑似词才注入该词条（未命中条目
      不注入，防无关词条噪音）。
    两块均无内容时返回空串（不挂载）。
    """
    if not sidecar:
        return ""
    parts = []
    summary = [ln for ln in (sidecar.get("summary") or []) if ln.strip()]
    if summary:
        parts.append("\n".join([_SIDECAR_SUMMARY_HEADER,
                                SIDECAR_SUMMARY_NOTICE] + summary))
    hits = [(a, b) for a, b in (sidecar.get("mishear") or [])
            if a and a in (src_text or "")]
    if hits:
        lines = [_SIDECAR_MISHEAR_HEADER]
        lines.extend(SIDECAR_MISHEAR_NOTICE.format(疑似词=a, 疑似正解=b)
                     for a, b in hits)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
