"""D11 契约④⑤ 行动层重翻执行器：基于质量报告导读 json 的定点重翻。

职责边界（与 v2 管线的关系）：
- 输入：{stem}_质量报告导读.json（version=2 快照）+ 现有 {stem}_final_cn.srt；
- 条目定位以 timing 为身份标识做精确字符串匹配，命中多块/未命中/空 timing
  一律记 failed 跳过，绝不猜测（resolve_final_block 的 index 口径仅用于
  导读快照 current_text 重定位）；
- 串行逐条调用（并发 1），永不写翻译记忆库；
- 恒等式硬断言：apply 只改选中块 text，其余条目逐字节不变，断言失败视为
  程序 bug 抛异常退出且不落盘；
- apply 不另做备份文件：{stem}_重翻记录.json 台账中的 old_text 即回滚依据；
- 可离线重建与标陈旧的逐件边界见 docs/行动层可离线重建与标陈旧清单.md。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .filters import build_srt, parse_srt
from .quality_report import resolve_final_block, write_guide_json
from .source_hallucination import is_fluent_zh
from .v2_outputs import _atomic_write_text

if TYPE_CHECKING:
    from subtransjav.translate.llm_client import LLMClient

# 导读快照刷新时重算存在性的伴生成品后缀（与 write_guide_json 六件同源，
# 台账 {stem}_重翻记录.json 不入 companions：它是执行器自己的审计件）
_COMPANION_SUFFIXES = ("_final_cn.srt", "_质量报告.txt", "_分歧复核.csv",
                       "_风险清单.md", "_风险清单.json", "_术语冲突观察.csv")

# 重翻后需管线态重算、离线不可得而标陈旧的伴生成品（打印口径，双钉于
# docs/行动层可离线重建与标陈旧清单.md）
_STALE_AFTER_RETRANSLATE = ("_质量报告.txt", "_分歧复核.csv",
                            "_术语冲突观察.csv")

# 与 quality_report.build_quality_report 的 generated_at 同源时间串格式
_TS_FMT = "%Y-%m-%d %H:%M:%S"

_ACTION_SYSTEM_PROMPT = (
    "你是一名影视字幕重翻执行器，负责针对单条字幕做定点修正重翻。\n"
    "输入为该条字幕的日文原文、现有中文译文与质量告警（类别与说明）。\n"
    "硬性要求：只输出重翻后的中文正文一行——不要解释、不要引号、"
    "不要编号、不要输出日文或英文原文。")

# 成对引号表（响应清洗只剥一层：模型偶发给译文套引号）
_QPAIRS = {"\"": "\"", "'": "'",
           "\u201c": "\u201d", "\u2018": "\u2019",
           "「": "」", "『": "』"}


class ActionRetranslateError(Exception):
    """行动层前置校验失败（清单缺失/格式不符等用户可修错误）。"""


def parse_entries_arg(spec: str) -> set[int]:
    """解析 --entries 选择串：逗号分隔的单值与闭区间（如 3,7,12-15）。

    非法格式（空段/非数字/区间倒序）抛 ValueError，由调用方报错退出 2。
    """
    spec = (spec or "").strip()
    if not spec:
        return set()
    selected: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            raise ValueError(f"--entries 含空段: {spec!r}")
        if "-" in token:
            a, _, b = token.partition("-")
            if not a.strip().isdigit() or not b.strip().isdigit():
                raise ValueError(f"--entries 区间非法: {token!r}")
            lo, hi = int(a), int(b)
            if lo > hi:
                raise ValueError(f"--entries 区间倒序: {token!r}")
            selected.update(range(lo, hi + 1))
        elif token.isdigit():
            selected.add(int(token))
        else:
            raise ValueError(f"--entries 条目非法: {token!r}")
    return selected


def _clean_response(text: str) -> str:
    """响应清洗：剥首尾空白与一层成对引号。"""
    t = (text or "").strip()
    if len(t) >= 2 and _QPAIRS.get(t[0]) == t[-1]:
        t = t[1:-1].strip()
    return t


def _passes_min_quality_gate(new_text: str, old_text: str) -> tuple[bool, str]:
    """失败最小质量门（D11 契约④）：非空 / 异于现有译文 / 合格中文。

    is_fluent_zh 与隔离区回捞同口径，一并覆盖两项契约要求：不含
    "[未翻译]" 前缀、非纯 ASCII（≥2 汉字才算合格中文）。
    返回 (是否通过, 失败原因)。
    """
    if not new_text:
        return False, "重翻结果为空"
    if new_text == old_text:
        return False, "重翻结果与现有译文相同"
    if not is_fluent_zh(new_text):
        return False, "未通过合格中文判定（需≥2汉字且无[未翻译]前缀）"
    return True, ""


def _make_action_client(cfg, model_override: str = ""):
    """行动层 LLM 客户端注入点（模块级函数，便于测试 monkeypatch）。

    复用管线槽 B 客户端工厂；model_override 非空时以 dataclasses.replace
    构造 cfg 副本仅换槽 B 模型（不改入参 cfg 本体）。串行由执行器主循环
    保证（逐条调用），客户端并发参数不参与。
    """
    import dataclasses

    from .pipeline_v2 import _make_client
    if model_override:
        stages = list(cfg.stages)
        stages[2] = dataclasses.replace(stages[2], model=model_override)
        cfg = dataclasses.replace(cfg, stages=stages)
    return _make_client(cfg, "B")


def _resolve_action_model(cfg, model_override: str) -> str:
    """台账 model_used 取值：显式覆盖优先，否则槽 B 模型（含服务商默认）。"""
    from .config import PROVIDER_MODEL_DEFAULTS
    if model_override:
        return model_override
    stage = cfg.stages[2]
    return stage.model or PROVIDER_MODEL_DEFAULTS.get(stage.provider, "")


def _load_guide(path: str) -> tuple[dict, Path, str]:
    """读导读清单并校验 v2 快照契约，返回 (guide, 所在目录, stem)。"""
    p = Path(path)
    if not p.is_file():
        raise ActionRetranslateError(f"导读清单不存在: {p}")
    try:
        guide = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ActionRetranslateError(f"导读清单不是合法 JSON: {e}") from e
    if not isinstance(guide, dict) or guide.get("version") != 2:
        raise ActionRetranslateError("导读清单 version != 2（需 v2 快照）")
    if not isinstance(guide.get("items"), list):
        raise ActionRetranslateError("导读清单缺少 items 列表")
    stem = guide.get("stem")
    if not stem:
        raise ActionRetranslateError("导读清单缺顶层 stem 字段")
    return guide, p.parent, str(stem)


def _load_source_map(source_path: str, selected: list) -> dict[str, str]:
    """--action-source 的 timing→源文映射（精确匹配首个，后到不覆盖）；
    未提供或文件不存在时打印退化提示（N=退化条数，映射不可得即全部选中
    条目退化，故 N=len(selected)，0 条不打印）。"""
    if not source_path:
        if selected:
            print(f"⚠️ [行动层] 未提供 --action-source（原始日文 SRT），"
                  f"{len(selected)} 条退化为导读摘录（源文截断，重翻质量受限；"
                  "传入原始 SRT 可按 timing 对齐恢复完整源文）")
        return {}
    p = Path(source_path)
    if not p.is_file():
        print(f"⚠️ [行动层] 指定的原始日文 SRT 不存在: {p}，"
              f"{len(selected)} 条退化为导读摘录")
        return {}
    mapping: dict[str, str] = {}
    for e in parse_srt(p.read_text(encoding="utf-8")):
        mapping.setdefault(e["timing"], e["text"])
    return mapping


def _select_items(items: list, wanted: set) -> list:
    """条目选择：--entries 按导读 index 精确选取；缺省=全部 open 且
    current_text 非 None 的 items。"""
    if wanted:
        chosen = [it for it in items if it.get("index") in wanted]
        missing = sorted(wanted - {it.get("index") for it in chosen})
        if missing:
            print(f"⚠️ [行动层] 条目不在导读清单中（忽略）: {missing}")
        return chosen
    return [it for it in items
            if it.get("status") == "open" and it.get("current_text") is not None]


def _print_plan(selected: list, source_map: dict, guide_path: Path,
                final_path: Path, apply_mode: bool) -> None:
    """dry-run 计划打印（零写入）：index/类别/timing/现译前 50 字/源文可得性。"""
    mode = "apply（落盘改写）" if apply_mode else "dry-run（零写入，加 --apply 生效）"
    print(f"📋 [行动层] 重翻计划: 清单={guide_path.name} "
          f"终稿={final_path.name} | 模式: {mode} | 选中 {len(selected)} 条")
    for it in selected:
        timing = it.get("timing") or ""
        src_avail = ("完整源文（--action-source 已对齐）"
                     if timing and timing in source_map else "仅导读摘录")
        preview = (it.get("current_text") or "")[:50]
        print(f"   #{it.get('index')} [{it.get('category') or ''}] {timing}"
              f" | 现译: {preview} | 源文: {src_avail}")


def _assert_apply_invariants(old_entries: list, new_entries: list,
                             applied_positions: set) -> None:
    """apply 恒等式硬断言（D11 契约④）：条目数一致 / 逐块 timing 全等 /
    仅选中块 text 变化。失败=程序 bug，抛 AssertionError 且不得落盘。"""
    if len(old_entries) != len(new_entries):
        raise AssertionError(
            f"恒等式断言失败：条目数 {len(old_entries)} -> {len(new_entries)}")
    for i, (o, n) in enumerate(zip(old_entries, new_entries, strict=True)):
        if o["timing"] != n["timing"]:
            raise AssertionError(f"恒等式断言失败：第 {i + 1} 块 timing 被改动")
        if i not in applied_positions and o["text"] != n["text"]:
            raise AssertionError(
                f"恒等式断言失败：非选中块 {i + 1} 文本被改动")


def _append_ledger(ledger_path: Path, records: list) -> int:
    """台账累积写入：已存在则追加（JSON 数组）；损坏件不阻断、重新起账。
    返回写入后的累计条数。"""
    existing = []
    if ledger_path.is_file():
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
        except (OSError, ValueError):
            print(f"⚠️ [行动层] 重翻台账损坏，重新起账: {ledger_path.name}")
    existing.extend(records)
    ledger_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return len(existing)


def _refresh_guide(guide: dict, out_dir: str, stem: str,
                   new_entries: list) -> None:
    """导读 json 快照刷新：generated_at 换当前同源时间串、items 的
    current_text 按新 final 重定位（resolve_final_block 精确 index 匹配，
    未命中保持原值不猜）、顶层加 retranslated_at；conclusions/sections/
    extras 原样保留。companions 六件存在性由 write_guide_json 统一重算。"""
    now = datetime.now().strftime(_TS_FMT)
    guide["generated_at"] = now
    guide["retranslated_at"] = now
    for it in guide.get("items") or []:
        idx = it.get("index")
        if idx is None:
            continue
        fe = resolve_final_block(new_entries, idx)
        if fe is not None:
            it["current_text"] = fe.get("text")
    write_guide_json(out_dir, stem, guide)


def run_action_retranslate(cfg, args) -> int:
    """行动层重翻执行器主入口。返回退出码：0=成功 / 1=全败或执行失败 /
    2=--entries 非法 / 3=部分降级（沿用 cli 口径）。"""
    try:
        guide, guide_dir, stem = _load_guide(args.action_retranslate)
    except ActionRetranslateError as e:
        print(f"❌ [行动层] {e}")
        return 1
    try:
        wanted = parse_entries_arg(getattr(args, "entries", "") or "")
    except ValueError as e:
        print(f"❌ [行动层] {e}")
        return 2

    out_dir = str(guide_dir)
    final_path = guide_dir / f"{stem}_final_cn.srt"
    ledger_path = guide_dir / f"{stem}_重翻记录.json"
    apply_mode = bool(getattr(args, "apply", False))
    sample_n = int(getattr(args, "action_sample", 0) or 0)

    selected = _select_items(guide.get("items") or [], wanted)
    if sample_n > 0:
        selected = selected[:sample_n]
    if not selected:
        print("📋 [行动层] 无待重翻条目（选择集为空），无事可做")
        return 0

    if not final_path.is_file():
        print(f"❌ [行动层] 终稿不存在，先跑完管线再执行行动层: {final_path}")
        return 1
    old_entries = parse_srt(final_path.read_text(encoding="utf-8"))
    if not old_entries:
        print(f"❌ [行动层] 终稿无有效条目: {final_path.name}")
        return 1

    source_map = _load_source_map(getattr(args, "action_source", "") or "",
                                  selected)

    if not apply_mode:
        _print_plan(selected, source_map, Path(args.action_retranslate),
                    final_path, apply_mode=False)
        return 0

    # ---- apply：串行逐条重翻（并发 1，永不写 TM）----
    new_entries = [dict(e) for e in old_entries]
    applied_positions: set[int] = set()
    records: list[dict] = []
    client: LLMClient | None = None
    client_err: BaseException | None = None
    model_used = _resolve_action_model(cfg, getattr(args, "action_model", "") or "")
    run_ts = datetime.now().strftime(_TS_FMT)

    for it in selected:
        index = it.get("index")
        timing = (it.get("timing") or "").strip()
        category = it.get("category") or ""
        base = {"index": index, "timing": timing, "category": category,
                "old_text": None, "new_text": None,
                "model_used": model_used, "outcome": "failed",
                "reason": "", "ts": run_ts, "source_partial": True}

        if not timing:
            base["reason"] = "timing 为空，无法定位终稿块"
            records.append(base)
            continue
        hits = [i for i, e in enumerate(old_entries) if e["timing"] == timing]
        if len(hits) > 1:
            base["reason"] = f"timing 歧义（命中 {len(hits)} 块），不猜测跳过"
            records.append(base)
            continue
        if not hits:
            base["reason"] = "timing 未命中终稿块，不猜测跳过"
            records.append(base)
            continue
        pos = hits[0]
        old_text = old_entries[pos]["text"]
        base["old_text"] = old_text

        # 源文恢复：--action-source 精确命中→完整源文；否则退化为导读摘录
        source_text = source_map.get(timing)
        source_partial = source_text is None
        if source_partial:
            source_text = it.get("source_excerpt") or ""
        base["source_partial"] = source_partial

        # 客户端懒构造：构造失败只作废本条，不中断整批
        if client is None and client_err is None:
            try:
                client = _make_action_client(
                    cfg, getattr(args, "action_model", "") or "")
            except Exception as e:   # noqa: BLE001 与管线同口径兜底
                client_err = e
        if client is None:
            base["reason"] = f"客户端构造失败: {client_err}"
            base["new_text"] = None
            records.append(base)
            continue

        user_text = (f"日文原文：{source_text}\n"
                     f"现有中文译文：{old_text}\n"
                     f"告警类别：{category}\n"
                     f"告警说明：{it.get('message') or ''}\n"
                     "请只输出重翻后的中文正文一行。")
        try:
            raw = client._chat(_ACTION_SYSTEM_PROMPT, user_text)
        except Exception as e:   # noqa: BLE001 单条调用失败不中断整批
            base["reason"] = f"LLM 调用失败: {e}"
            records.append(base)
            continue

        new_text = _clean_response(raw)
        base["new_text"] = new_text
        ok, reason = _passes_min_quality_gate(new_text, old_text)
        if not ok:
            base["reason"] = reason   # 质量门不过：保留原文，只记台账
            records.append(base)
            continue

        new_entries[pos]["text"] = new_text
        applied_positions.add(pos)
        base["outcome"] = "applied"
        records.append(base)
        print(f"   ✅ #{index} 重翻应用: {old_text[:30]} -> {new_text[:30]}")

    n_applied = len(applied_positions)
    n_failed = len(records) - n_applied

    if applied_positions:
        new_srt = build_srt(new_entries)
        _assert_apply_invariants(old_entries, new_entries, applied_positions)
        _atomic_write_text(str(final_path), new_srt)
        _refresh_guide(guide, out_dir, stem, new_entries)
        print(f"📖 [行动层] 导读快照已刷新: {stem}_质量报告导读.json")
        print("🏷️ [行动层] 已标陈旧（重翻后需重跑管线重算，离线不可得）：")
        for suffix in _STALE_AFTER_RETRANSLATE:
            print(f"   - {stem}{suffix}")
        print("   详见 docs/行动层可离线重建与标陈旧清单.md")

    if records:
        total = _append_ledger(ledger_path, records)
        print(f"📒 [行动层] 重翻台账已更新: {ledger_path.name}（累计 {total} 条）")

    print(f"🏁 [行动层] 完成: 应用 {n_applied} / 失败 {n_failed}"
          f" / 选中 {len(selected)}")
    if n_applied == len(selected):
        return 0
    if n_applied:
        return 3
    return 1
