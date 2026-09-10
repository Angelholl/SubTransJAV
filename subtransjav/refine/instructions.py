"""
Refine 指令模板构建（v2）：
- 角色卡 txt → 引擎模板格式（### prompt / ### instructions）
- 词库命中块追加（运行级：对整个输入文件预扫描命中）
- 阶段B（审校+抛光）自动拼接加固段

规则文本（句末助词/重试指令/硬性豁免）来自 translation_rules.yaml
单一数据源（rules_loader），不再在代码中硬编码，防止 YAML 与代码漂移。
legacy 管线（已删除）的模板加载函数不再保留。
"""

import functools
import os
import tempfile

from .rules_loader import load_rules

# ------------------------------------------------------------------
# 从 YAML 加载规则段（惰性加载：首次使用时才解析，导入期不读盘；
# 包内含回退副本，缺失/损坏时在使用点显式报错，而不是让整个
# CLI/webview 因 import 崩溃）。lru_cache 保证同一文件只解析一次。
# ------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _get_rules() -> dict:
    return load_rules()


def _prompt_sections() -> dict:
    return _get_rules()["prompt_sections"]


def retry_cleaning() -> str:
    """抛光阶段重试指令段（以 "\n" 开头，拼接在模板尾部形成空行分隔）。"""
    return "\n" + _prompt_sections()["retry_cleaning"]


def hardened_suffix() -> str:
    """硬性豁免段（以 "\n\n" 开头，拼接在模板尾部形成空行分隔）。"""
    return "\n\n" + _prompt_sections()["hardened_suffix"]


# 向后兼容：外部代码/测试仍可访问模块级常量 RETRY_CLEANING /
# HARDENED_SUFFIX，首次访问时惰性求值并缓存到模块字典。
_LAZY_ATTRS = {
    "RETRY_CLEANING": retry_cleaning,
    "HARDENED_SUFFIX": hardened_suffix,
}


def __getattr__(name: str):
    if name in _LAZY_ATTRS:
        value = _LAZY_ATTRS[name]()
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 引擎要求回复按编号协议逐条回填，格式：
#   #<编号>
#   Translation>
#   <正文>
# 绝不能输出标准 .srt 时间码块，否则解析器报 "No matches found"。
# （协议说明保留于此；规则文本本体见 translation_rules.yaml）


def write_effective_instructions(base_text: str, glossary_block: str = "",
                                 work_dir: str = None, tag: str = "instr",
                                 fixed_name: str = None) -> str:
    """写出最终生效的指令文件（模板 + 词库命中块），返回文件路径。

    fixed_name: 指定固定文件名（如 refine_v2_A.txt）时覆盖写入、不再堆积
    随机命名的历史文件；未指定时保持原随机命名行为。
    """
    if glossary_block:
        base_text = base_text.rstrip() + "\n\n" + glossary_block + "\n"
    if fixed_name:
        path = os.path.join(work_dir or tempfile.gettempdir(), fixed_name)
        # 内容未变时跳过写入，减少磁盘 I/O（并发场景下尤为明显）
        try:
            with open(path, encoding="utf-8") as _f:
                if _f.read() == base_text:
                    return path
        except (FileNotFoundError, OSError):
            pass
        with open(path, "w", encoding="utf-8") as f:
            f.write(base_text)
        return path
    fd, path = tempfile.mkstemp(prefix=f"refine_{tag}_", suffix=".txt", dir=work_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(base_text)
    return path
