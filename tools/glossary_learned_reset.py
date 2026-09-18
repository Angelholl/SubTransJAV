#!/usr/bin/env python
"""glossary_learned_reset — learned 自学习词库重置工具（v1.2.2 批次 D3）。

与 tools/tm_purge.py 同款纪律（dry-run 默认 / 备份先行 / 可审计）：
  - 默认 dry-run，零写入：只显示将清除的行数与备份路径；
  - --yes 才实际执行，且执行前先落带时间戳的备份副本
    （<原名>.bak-YYYYMMDD_HHMMSS，同目录）；
  - --keep-term 白名单：按源词（第一列，ascii 忽略大小写）保留指定
    词条，其余清除；不带 --keep-term 时清空全部词条；
  - 全程只触碰 glossary_learned.csv，绝不写人工词库 glossary.csv
    （真实 TM 库/人工词库只读纪律与本工具无关但同源）。

背景：glossary_learned.csv 由自动学习（glossary_learn）追加，幻觉/
毒化条目一旦入库会随 load_glossary_merged 注入后续所有任务的提示词
长期扩散；config.glossary_learn_enabled=False 只能止增，本工具提供
可审计的存量清退路径。

用法:
  python tools/glossary_learned_reset.py                        # dry-run（默认路径，零写入）
  python tools/glossary_learned_reset.py --path X/glossary_learned.csv
  python tools/glossary_learned_reset.py --keep-term "IKU,ムラムラ"   # dry-run 预览保留
  python tools/glossary_learned_reset.py --yes                  # 备份后清空
  python tools/glossary_learned_reset.py --yes --keep-term "IKU"  # 备份后仅保留 IKU

退出码: 0=成功；1=执行错误；2=用法错误（词库文件不存在等）。
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtransjav.refine.glossary_learn import load_learned_glossary  # noqa: E402
from subtransjav.refine.pipeline_support import learned_glossary_path  # noqa: E402


def normalize_keep_term(term: str) -> str:
    """白名单词条归一：去空白；ascii 词忽略大小写。"""
    t = (term or "").strip()
    return t.lower() if t.isascii() else t


def plan_reset(rows: list, keep_terms) -> tuple[list, list]:
    """按白名单拆分 rows -> (kept, removed)。keep_terms 为空即全清。"""
    keep_set = {normalize_keep_term(t) for t in (keep_terms or [])
                if normalize_keep_term(t)}
    if not keep_set:
        return [], list(rows)
    kept, removed = [], []
    for row in rows:
        src = normalize_keep_term(row[0])
        (kept if src in keep_set else removed).append(row)
    return kept, removed


def backup_path_for(path: str, now: datetime | None = None) -> str:
    """带时间戳的备份副本路径（同目录：<原名>.bak-YYYYMMDD_HHMMSS）。"""
    p = Path(path)
    ts = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return str(p.with_name(f"{p.name}.bak-{ts}"))


def write_rows(path: str, rows: list) -> None:
    """全量写回（utf-8-sig + newline=""，与 save_learned_glossary 同款格式）。"""
    import csv
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for row in rows:
            w.writerow(list(row)[:2])


def execute_reset(path: str, keep_terms=None) -> dict:
    """--yes 执行：先备份 → 再按白名单写回。返回执行摘要。"""
    rows = load_learned_glossary(path)
    kept, removed = plan_reset(rows, keep_terms)
    bak = backup_path_for(path)
    shutil.copy2(path, bak)
    write_rows(path, kept)
    return {"backup": bak, "removed": len(removed), "kept": len(kept)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="learned 自学习词库重置（dry-run 默认，--yes 才执行）")
    parser.add_argument("--path", default="",
                        help="glossary_learned.csv 路径（空=默认路径）")
    parser.add_argument("--keep-term", default="",
                        help="逗号分隔的保留源词（第一列；ascii 忽略大小写）")
    parser.add_argument("--yes", action="store_true",
                        help="实际执行（先备份带时间戳副本，再清除/保留写回）")
    args = parser.parse_args(argv)

    path = args.path or learned_glossary_path()
    p = Path(path)
    if not p.is_file():
        print(f"❌ 词库文件不存在: {path}")
        return 2
    keep_terms = [t for t in args.keep_term.split(",") if t.strip()] \
        if args.keep_term else []

    rows = load_learned_glossary(path)
    kept, removed = plan_reset(rows, keep_terms)
    bak = backup_path_for(path)

    print(f"📄 词库: {path}")
    print(f"   现有词条 {len(rows)} 条 | 将清除 {len(removed)} 条"
          f" | 保留 {len(kept)} 条")
    if keep_terms:
        print(f"   保留白名单: {', '.join(keep_terms)}")
    print(f"   备份路径: {bak}")
    if not args.yes:
        print("🔍 dry-run（默认）：未写入任何文件；确认后加 --yes 执行"
              "（与 tm_purge 同款纪律：备份先行、可审计）")
        return 0
    try:
        result = execute_reset(path, keep_terms)
    except OSError as e:
        print(f"❌ 执行失败: {e}")
        return 1
    print(f"✅ 已执行：备份 -> {result['backup']}；"
          f"清除 {result['removed']} 条，保留 {result['kept']} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
