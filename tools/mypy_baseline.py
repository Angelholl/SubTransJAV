#!/usr/bin/env python3
"""mypy 基线门禁脚本（D9 决策，1.3.1）。

两种模式：
- 默认 ``--check``：跑 ``mypy subtransjav``，解析 ``file:line: error: msg [code]``，
  与 mypy-baseline.txt 做集合差——不在基线内的错误即退出码 1；基线内存量放行。
- ``--update``：重写基线文件并在 stdout 打印提示，供决策日志留痕。

退出码校验契约（2026-09-26 审计 M1 项）：mypy 退出码 0=无错；1=有错误行；
退出码非 0/1（含负值信号终止）=环境故障（进程崩溃、参数错等）。退出码 1 但解析不到任何错误行同样判
环境故障——不校验则 ``--check`` 落入空差集假绿放行（典型=No module named
mypy）。环境故障一律脚本退出码 1 并拒绝判定，``--update`` 同时拒绝写
基线文件，防基线被垃圾输出覆盖。

历史注记（保留）：本脚本强制 ``--python-version 3.12``，不可覆盖。
pyproject 3.10 时代 legacy：早期 target <3.12 时直跑会因 numpy stub 语法差异
误报中止；pyproject 已改 3.12，此约束仅作历史防线保留。

已否决 per-module 上限表方案，勿实现。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = PROJECT_ROOT / "mypy-baseline.txt"
TARGET = "subtransjav"

# 历史注记见模块 docstring：强制 3.12，不可覆盖。
FORCED_PYTHON_VERSION = "3.12"

# 匹配 mypy 错误行：path:line: error: msg [code]
ERROR_PREFIX = ": error: "


def run_mypy() -> tuple[int, str]:
    """运行 mypy，返回 (退出码, 合并输出)；退出码契约见模块 docstring。"""
    cmd = [
        sys.executable,
        "-m",
        "mypy",
        TARGET,
        "--python-version",
        FORCED_PYTHON_VERSION,
    ]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout + proc.stderr


def parse_errors(output: str) -> set[str]:
    """解析 mypy 输出，返回 ``path:line: [code]`` 集合。"""
    errors: set[str] = set()
    for line in output.splitlines():
        idx = line.find(ERROR_PREFIX)
        if idx == -1:
            continue
        location = line[:idx].rstrip()
        message = line[idx + len(ERROR_PREFIX):]
        # 提取尾部 [code]（无 code 的行则整个 message 作 code 位）
        code = ""
        if message.endswith("]") and " [" in message:
            code = message[message.rindex(" [") + 1 :]
        path, _, lineno = location.rpartition(":")
        key = f"{path}:{lineno}: {code}" if path else f"{location}: {code}"
        errors.add(key)
    return errors


def load_baseline() -> set[str]:
    """读取基线文件；缺失视为空基线。"""
    if not BASELINE_FILE.exists():
        return set()
    lines = BASELINE_FILE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def write_baseline(errors: set[str]) -> None:
    sorted_errors = sorted(errors)
    if sorted_errors:
        content = "# mypy baseline — regenerate with: python tools/mypy_baseline.py --update\n" + "\n".join(sorted_errors) + "\n"
    else:
        content = "# mypy baseline (empty) — hard gate active since 1.3.1\n"
    BASELINE_FILE.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="mypy 基线门禁（--check 默认 / --update 重写基线）")
    parser.add_argument("--check", action="store_true", help="对照基线检查，新错误即退出码 1（默认行为）")
    parser.add_argument("--update", action="store_true", help="用当前 mypy 结果重写基线文件")
    args = parser.parse_args()

    returncode, output = run_mypy()
    current = parse_errors(output)

    # 退出码校验契约见模块 docstring：退出码非 0/1（含负值信号终止）=环境故障；退出码 1 但零错误行=
    # 假绿防护（典型=No module named mypy）。两分支对 --check/--update
    # 共用：环境故障一律退出 1，--update 拒绝写基线，防基线被垃圾输出覆盖。
    if returncode not in (0, 1):
        print(f"mypy 异常退出（returncode={returncode}），视为环境故障，拒绝判定与更新基线", file=sys.stderr)
        return 1
    if returncode == 1 and not current:
        print("mypy 退出码 1 但未解析到任何错误行，视为环境故障（假绿防护），拒绝判定与更新基线", file=sys.stderr)
        return 1

    if args.update:
        write_baseline(current)
        print(f"基线已更新 {len(current)} 条——须决策日志留痕")
        return 0

    baseline = load_baseline()
    new_errors = current - baseline
    if new_errors:
        print(f"发现 {len(new_errors)} 条基线外 mypy 错误：", file=sys.stderr)
        for err in sorted(new_errors):
            print(f"  {err}", file=sys.stderr)
        return 1
    print(f"mypy 基线门禁通过（存量 {len(baseline)} 条，当前 {len(current)} 条，基线外 0 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
