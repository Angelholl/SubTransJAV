#!/usr/bin/env python
"""guard_banned_paths — 敏感路径入库守卫（v1.3.1 D10，D2026-0925-02）。

三条纪律铁律：
1. 只看 git tracked/indexed 文件（``git ls-files``），绝不扫描工作树
   含 ignored 文件（ignored 树外签注面不属于仓库基线）。
2. 路径名单制，不做内容扫描——命中名单即违规，不判断文件内容。
3. 命中 tracked/indexed 文件匹配名单 → 打印违规项并退出码 1，否则 0。

用法::

    python tools/guard_banned_paths.py            # 全量 tracked 文件
    python tools/guard_banned_paths.py --staged   # 只查 staged（git diff --cached --name-only）

名单来源：.gitignore 敏感段现值 + docs/decision-log.md 第 139 行
（1.2 收尾清单："internal_docs/、api_keys.bin、sexual_terms.csv、
真实 glossary、敏感 Temp 脚本移至仓库树外"）。逐条对照见 BANNED_PATTERNS。
"""

import argparse
import contextlib
import fnmatch
import subprocess
import sys

# 排除名单（路径模式，fnmatch 语义）。source 标注每条的契约出处，
# 由 tests/test_guard_banned_paths.py 做名单-契约防漂移断言。
BANNED_PATTERNS = [
    # ---- 来源 1：.gitignore 敏感数据段（字面规则现值）----
    ("config/api_keys.bin", ".gitignore: 敏感数据（严禁入库）"),
    ("config/glossary.csv", ".gitignore: 敏感数据（严禁入库）"),
    ("config/glossary.csv.bak-*", ".gitignore: 敏感数据（严禁入库）"),
    ("config/glossary_learned.csv", ".gitignore: 敏感数据（严禁入库）"),
    ("create_shortcut.py", ".gitignore: 敏感数据（严禁入库）"),
    ("Temp/*", ".gitignore: 运行数据 Temp/"),
    # ---- 来源 2：decision-log :139 收尾清单（树外同级目录契约）----
    ("internal_docs/*", "decision-log:139 internal_docs/ 移至树外"),
    ("sexual_terms.csv", "decision-log:139 sexual_terms.csv 移至树外"),
]


def _git(args: list) -> list:
    """跑 git 取文件列表，逐行返回。"""
    out = subprocess.run(["git"] + args, capture_output=True, text=True,
                         check=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def list_tracked() -> list:
    """全量 tracked/indexed 文件（不含工作树新增、不含 ignored）。"""
    return _git(["ls-files"])


def list_staged() -> list:
    """只查 staged 文件。"""
    return _git(["diff", "--cached", "--name-only"])


def match_any(path: str) -> list:
    """返回命中的 (pattern, source) 列表。"""
    hits = []
    norm = path.replace("\\", "/")
    for pattern, source in BANNED_PATTERNS:
        if fnmatch.fnmatch(norm, pattern):
            hits.append((pattern, source))
    return hits


def main(argv=None) -> int:
    # Windows CI 控制台常为 cp1252 等窄码页，中文文案直接 encode 即崩
    # （UnicodeEncodeError，回归自 test_clean_repo_exits_0 的 CI 失败）。
    # 只放宽 errors 不改 encoding：本地 cp936 中文照常显示，窄码页降级为
    # \uXXXX 转义而非崩溃；流不可 reconfigure（已关闭/被替换）时静默跳过，
    # 不影响守卫判定。
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        with contextlib.suppress(OSError, ValueError):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        prog="guard_banned_paths",
        description="敏感路径入库守卫：tracked/indexed 文件命中排除名单即违规")
    parser.add_argument("--staged", action="store_true",
                        help="只查 staged 文件（默认查全量 tracked）")
    args = parser.parse_args(argv)

    files = list_staged() if args.staged else list_tracked()
    violations = []
    for path in files:
        for pattern, source in match_any(path):
            violations.append(f"{path}  [命中名单: {pattern} — {source}]")

    if violations:
        print("[违规] 以下 tracked/indexed 文件命中敏感路径排除名单：",
              file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(f"共 {len(violations)} 项违规（名单契约见 tools/guard_banned_paths.py"
              " 与 docs/decision-log.md :139）", file=sys.stderr)
        return 1
    scope = "staged" if args.staged else "tracked"
    print(f"[OK] {scope} 文件共 {len(files)} 个，均未命中敏感路径排除名单")
    return 0


if __name__ == "__main__":
    sys.exit(main())
