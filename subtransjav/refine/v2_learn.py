"""
v2 学习层：译文回写 TM 与自动学习词表
======================================
职责：终稿条目按时间轴 1:1 对齐后准入回写翻译记忆库（_learn_to_tm）、
自动词库学习后台线程（_auto_learn_glossary，glossary_learned.csv 追加）。

迁出来源：subtransjav/refine/pipeline_v2.py（1.3.0 pipeline_v2 拆分
批次 2·模块 E）。代码体逐字迁移、零改动；行为等价依据 D2026-0922-03
HRO-2（逐字节等价）与 D2026-0925-01（执行契约）。

依赖说明：本模块只依赖叶子模块（config/pipeline_support/risk/tm 与
批次 1 的 v2_premerge），严禁反向依赖 pipeline_v2。logger 刻意沿用
"subtransjav.refine.pipeline_v2" 命名，保证日志名与拆分前逐字节一致。
"""

import logging
import threading
from pathlib import Path

from .config import RefineConfig
from .pipeline_support import learned_glossary_path
from .risk import SEVERITY_INFO
from .tm import TranslationMemory
from .v2_premerge import _timing_span

# 字节级日志名一致（R4）：拆分前后日志输出不含模块名差异
logger = logging.getLogger("subtransjav.refine.pipeline_v2")


def _auto_learn_glossary(cfg: RefineConfig, in_path: str, out_final_path: str,
                         collector=None, file_name: str = None,
                         threads: list | None = None):
    """自动词库学习：从 原文↔终稿 中提取术语对追加到 glossary_learned.csv。

    防污染：glossary_learn 内部做子串核验（原文/译文中必须真实存在），
    幻觉造词不会入库；learned 词库仅通过 load_glossary_merged 追加注入。

    v1.2.2 D3 治理开关：cfg.glossary_learn_enabled=False 时跳过本学习
    路径（跳过事实计数入日志与风险清单，止增不清退——存量清退用
    tools/glossary_learned_reset.py）。

    P1-6 异步化：学习调用放入后台 daemon 线程（注册到 run 级 threads
    列表，由 run_v2 收尾 join 超时 60s），不阻塞主流程；threads=None
    （无 run 上下文的直调）保持同步语义。
    """
    if not getattr(cfg, "glossary_learn_enabled", True):
        print("   ⏭️ 词库学习已被 glossary_learn_enabled=False 跳过")
        logger.info("glossary_learn_enabled=False，跳过 learned 词库学习"
                    "（file=%s）", file_name)
        if collector is not None:
            collector.add(stage="final", file=file_name,
                          reason="glossary_learn_enabled=False",
                          action="跳过 learned 词库学习（配置治理开关）",
                          severity=SEVERITY_INFO)
        return

    def _learn_once():
        try:
            from .glossary_learn import learn_from_s2_output
            try:
                endpoint = (cfg.resolve_endpoint("lmstudio")
                            or "http://localhost:1234/v1")
            except Exception:
                endpoint = "http://localhost:1234/v1"
            new_terms = learn_from_s2_output(
                in_path, out_final_path, learned_glossary_path(),
                endpoint=endpoint,
                timeout_http=getattr(cfg, "timeout_http", None),
                timeout_probe=getattr(cfg, "timeout_probe", None))
            if new_terms:
                print(f"   📚 词库学习：新增 {new_terms} 条术语 -> "
                      f"config/glossary_learned.csv")
        except Exception as e:
            print(f"   ⚠️ 词库学习失败（不影响翻译结果）: {e}")
            if collector is not None:
                collector.add(stage="final", file=file_name,
                              reason=f"词库学习失败: {e}",
                              action="跳过词库学习",
                              severity=SEVERITY_INFO)

    if threads is None:             # 直调模式：保持同步旧语义
        _learn_once()
        return
    t = threading.Thread(target=_learn_once, name="subtransjav-learn",
                         daemon=True)
    t.start()
    threads.append(t)


def _learn_to_tm(tm: TranslationMemory, orig_entries: list,
                 final_entries: list, flagged=None, gate=True,
                 must_see_spans=None, collector=None, file_name: str = None,
                 conflict_spans=None, block_conflicts=False) -> int | None:
    """终稿学习：日文原文 → 最终中文。

    ⚠️ 双指针对齐（同起点多条按序消费），只学时间轴完全一致
    （起点+终点均匹配）的 1:1 行：被兜底清洗合并/删除的行不入库——
    合并行的译文覆盖多条原文，按任何键对齐存入都会造成错位配对
    （legacy 毒化 TM 的同款路径）。

    Parameters
    ----------
    flagged : set | None
        post_validate 标记的行 index 集合（validator 类跳过）。
    gate : bool
        TM 学习准入门槛总开关。False 时跳过所有准入判定（A/B 验证用）。
    must_see_spans : set | None
        「必看分歧行」时间轴 span 集合（第三层门槛）。
        每元素为 ``_timing_span`` 返回的 ``(start_sec, end_sec)``，
        来自 merged 产物分歧行的 timing——与学习循环的 orig 侧
        （同一 merged 文件解析出的 entries）span 直接可比。
        该行 span 命中集合 → 一律不入库（双引擎严重分歧行，
        真问题富集区，学习风险大于收益）。仅 gate=True 时生效。
    conflict_spans : set | None
        术语冲突观察条目的时间轴 span 集合（v1.2.2 D1，glossary_conflict.
        scan_glossary_conflicts 产出冲突清单的 timing）。
    block_conflicts : bool
        cfg.glossary_conflict_block：True 时冲突条目禁止进入 TM 学习
        （入库前检查，冲突即跳过并计数）。独立于 gate（观察闸转阻断
        由用户裁决，不随 A/B 验证口径关断）。

    Returns
    -------
    int | None
        本次实际入库条数（store_batch 新增数；无候选 0）；学习过程
        异常时返回 None（报告 TM 摘要行按"取不到"处理）。
    """
    if flagged is None:
        flagged = set()
    if must_see_spans is None:
        must_see_spans = set()
    if conflict_spans is None:
        conflict_spans = set()
    try:
        from .post_validate import is_untranslated_text, scan_learn_defect
    except Exception:
        scan_learn_defect = None

        def is_untranslated_text(text: str) -> bool:
            # post_validate 不可用时的降级判定（保持既有降级语义：
            # 缺陷扫描层跳过，学习闸仍工作）
            return (text or "").strip().startswith("[未翻译]")

    # 准入跳过计数器（按类别）
    skip_kana = skip_leak = skip_src_prefix = skip_placeholder = 0
    skip_len_ratio = skip_validator = skip_must_see = 0
    skip_conflict = 0
    learned_count = 0

    try:
        pairs = []
        j = 0
        n = len(final_entries)
        eps = 1e-6
        for e in orig_entries:
            span = _timing_span(e["timing"])
            while j < n and _timing_span(final_entries[j]["timing"])[0] \
                    < span[0] - eps:
                j += 1                   # 该原文行被删除 → 跳过
            if j >= n:
                break
            fspan = _timing_span(final_entries[j]["timing"])
            if fspan[0] != span[0]:
                continue                 # 时间轴不对应 → 不学习
            if fspan == span:
                src = (e["text"] or "").strip()
                tgt = (final_entries[j]["text"] or "").strip()
                if (src and tgt
                        and not is_untranslated_text(tgt)
                        and tgt != src):         # 同文残留对不入库
                    # ---- 准入门槛 ----
                    # 层0：术语冲突观察闸转阻断（v1.2.2 D1）——独立于
                    # gate（用户显式开启 glossary_conflict_block 才生效）
                    if block_conflicts and fspan in conflict_spans:
                        skip_conflict += 1
                        j += 1
                        continue
                    if gate:
                        # 层3：必看分歧行（双引擎严重分歧，确定性阻断）
                        # 必看行 timing 来自 merged 产物（=in_path），学习
                        # 循环的 orig 侧即同一文件解析出的 entries，
                        # span 直接可比（时间轴是条目的真实身份标识）。
                        if span in must_see_spans:
                            skip_must_see += 1
                            j += 1
                            continue
                        # 层1：validator 类（で误译修正 / 主语误判）
                        if e["index"] in flagged:
                            skip_validator += 1
                            j += 1
                            continue
                        # 层2：scan_learn_defect 缺陷扫描
                        if scan_learn_defect is not None:
                            defect = scan_learn_defect(src, tgt)
                            if defect is not None:
                                if defect == "kana":
                                    skip_kana += 1
                                elif defect == "leak":
                                    skip_leak += 1
                                elif defect == "src_prefix":
                                    skip_src_prefix += 1
                                elif defect == "placeholder":
                                    skip_placeholder += 1
                                elif defect == "len_ratio":
                                    skip_len_ratio += 1
                                j += 1
                                continue
                    pairs.append((src, tgt, 1))
                j += 1                   # 精确匹配，消费该终稿行
            # fspan 起点相同但被合并（终点延长）→ 不学习也不消费，
            # 让被合并的后续原文行自然跳过
        if pairs:
            # v1.2.2 批次 A2：学习入库带上来源 srt 文件名 stem（TM
            # provenance，供后续 TM 清洗区分跨片同源句；file_name 为
            # Path(in_path).name，stem 即来源标识）
            source_stem = Path(file_name).stem if file_name else None
            added = tm.store_batch(pairs, source_name=source_stem)
            learned_count = int(added or 0)
            if added:
                print(f"   💾 翻译记忆库: 学习 {len(pairs)} 对"
                      f"（新增 {added} 条）")
        # 准入门槛统计行
        skip_total = (skip_kana + skip_leak + skip_src_prefix
                      + skip_placeholder + skip_len_ratio + skip_validator
                      + skip_must_see + skip_conflict)
        print(f"   🚫 学习门槛: 跳过 {skip_total} 行"
              f"（假名{skip_kana}/泄漏{skip_leak}/源残留{skip_src_prefix}"
              f"/占位符{skip_placeholder}/长度比{skip_len_ratio}"
              f"/validator {skip_validator}/必看{skip_must_see}"
              f"/术语冲突{skip_conflict}）")
        return learned_count
    except Exception as e:
        print(f"   ⚠️ 翻译记忆库学习失败: {e}")
        if collector is not None:
            collector.add(stage="final", file=file_name,
                          reason=f"翻译记忆库学习失败: {e}",
                          action="跳过TM学习",
                          severity=SEVERITY_INFO)
        return None
