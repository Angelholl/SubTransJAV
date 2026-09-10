# -*- coding: utf-8 -*-
"""
删除感知解析补丁（deletion_patch）单元测试
==========================================
覆盖：
  - 删除放行语义：空译文条目被记入 deletions 并剥离（清洗阶段 S1/S3）
  - 删除拦截语义：空译文条目保留并交给原生校验触发补译（翻译阶段 S2）
  - 线程局部删除开关：并发文件各自独立，互不干扰
  - S2 翻译语义下模型漏译个别行 -> 静默补位原文并标记 [未翻译]
  - 补丁安装幂等性
"""

from datetime import timedelta
import threading

import pytest

from PySubtrans.Options import Options
from PySubtrans.SubtitleLine import SubtitleLine
from PySubtrans.TranslationParser import TranslationParser

import subtransjav.translate.deletion_patch as dp


# ---------------------------------------------------------------------------
# 公共构造辅助
# ---------------------------------------------------------------------------

def _make_parser(allow_empty_deletions):
    """构造带阶段语义选项的 TranslationParser（对应 refine 阶段1/3 与阶段2）。"""
    opts = Options({
        "max_characters": 1000,
        "max_newlines": 10,
        "allow_empty_deletions": allow_empty_deletions,
    })
    return TranslationParser("translation", opts)


def _make_originals():
    """构造 4 行原文（编号 1..4）；text 与 original 均显式赋值以便断言补位内容。"""
    originals = []
    for n, txt in [(1, "はい。"), (2, "うんうん"), (3, "宮下先輩。"), (4, "頑張る")]:
        line = SubtitleLine(original=txt)
        line.number = n
        line.text = txt
        line.start = timedelta(seconds=n)
        line.end = timedelta(seconds=n + 2)
        originals.append(line)
    return originals


def _match(parser, originals):
    """调用 MatchTranslations 并把返回值归一化为 (matched, unmatched) 二元组。

    PySubtrans 各版本返回值形态不同（匹配列表 或 (匹配, 未匹配) 元组），
    此处兼容两种形态。
    """
    result = parser.MatchTranslations(originals)
    if isinstance(result, tuple):
        return result[0], result[1]
    return result, []


@pytest.fixture(autouse=True)
def _reset_deletions_state():
    """每个用例结束后复位线程局部删除开关，防止用例间残留污染。"""
    yield
    dp.set_allow_deletions(False)


@pytest.fixture()
def patches():
    """安装三个运行时补丁（幂等），供解析行为类用例使用。"""
    dp.install_deletion_patch()
    dp.install_transient_retry_patch()
    dp.install_zero_batch_skip_patch()
    yield


# ---------------------------------------------------------------------------
# 删除放行 / 拦截语义
# ---------------------------------------------------------------------------

def test_allow_deletions_true_filters_blank_entries(patches):
    """删除放行语义（清洗阶段）：空白条目记入 deletions，不参与校验、不触发补译。"""
    parser = _make_parser(allow_empty_deletions=True)
    parser.ProcessTranslation("#1\nTranslation>\n\n#2\nTranslation>\n嗯。\n\n", validate=True)

    originals = _make_originals()
    assert originals[0].key in parser.deletions, "空白条目应被记入删除集"
    assert originals[1].key not in parser.deletions

    matched, unmatched = _match(parser, originals[:2])
    assert unmatched == []
    assert [m.number for m in matched] == [2], "仅 #2 应进入匹配结果"
    assert matched[0].text == "嗯。"
    assert originals[0].translation == "", "删除条目应显式标记为空（保存时跳过）"


def test_allow_deletions_false_keeps_blank_entries(patches):
    """删除拦截语义（翻译阶段）：空白条目保留并触发原生校验错误（补译信号）。"""
    parser = _make_parser(allow_empty_deletions=False)
    parser.ProcessTranslation("#1\nTranslation>\n\n#2\nTranslation>\n嗯。\n\n", validate=True)

    originals = _make_originals()
    assert parser.deletions == set(), "拦截语义下不应声明任何删除"
    assert originals[0].key in parser.translations, "空白条目应留在翻译集"
    assert parser.errors, "空白条目应触发 EmptyLinesError 校验错误（重试补译信号）"


def test_all_deleted_batch_preserves_accumulated_translations(patches):
    """清洗语义：整批全删不应清空之前批次已累积的翻译（数据丢失回归）。"""
    parser = _make_parser(allow_empty_deletions=True)
    # 第一批：正常翻译 2 行，累积到 self.translated
    parser.ProcessTranslation("#1\nTranslation>\n嗯。\n\n#2\nTranslation>\n好的。\n\n", validate=False)
    assert len(parser.translated) == 2

    # 第二批：整批全删（纯噪音批次）
    parser.ProcessTranslation("#3\nTranslation>\n\n#4\nTranslation>\n\n", validate=False)

    # 之前累积的翻译必须保留，而非被清空
    assert len(parser.translated) == 2, "整批全删清空了之前批次的累积翻译"


# ---------------------------------------------------------------------------
# 线程局部开关
# ---------------------------------------------------------------------------

def test_allow_deletions_thread_local_isolation():
    """线程局部：主线程与工作线程各自 set_/读回，互不干扰。"""
    dp.set_allow_deletions(True)
    seen = []

    def worker():
        dp.set_allow_deletions(False)
        seen.append(dp._allow_deletions())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen == [False], "工作线程应读回自己的 False"
    assert dp._allow_deletions() is True, "主线程应读回自己的 True"


def test_deletions_allowed_falls_back_to_thread_local(patches):
    """实例未声明 allow_empty_deletions 时，删除语义回退到线程局部开关。"""
    parser = TranslationParser(
        "translation", Options({"max_characters": 1000, "max_newlines": 10}))
    dp.set_allow_deletions(True)
    assert dp._deletions_allowed_for(parser) is True
    dp.set_allow_deletions(False)
    assert dp._deletions_allowed_for(parser) is False


# ---------------------------------------------------------------------------
# S2 漏译静默补位
# ---------------------------------------------------------------------------

def test_missing_translations_silently_filled(patches):
    """S2 翻译语义：模型输出漏行时静默补位——不丢批、不抛错、不触发重试。"""
    parser = _make_parser(allow_empty_deletions=False)
    # 模型漏译 #2 与 #4，其余有译文
    resp_text = "#1\nTranslation>\n嗯。\n\n#3\nTranslation>\n宫下前辈。\n\n"
    parser.ProcessTranslation(resp_text, validate=True)

    originals = _make_originals()
    matched, unmatched = _match(parser, originals)

    assert unmatched == [], "翻译语义下不应有未匹配行"
    assert not parser.errors, "漏译补位不应产生错误"
    assert [m.number for m in matched] == [1, 2, 3, 4], "应补出完整编号序列"
    assert all(m.text for m in matched), "补位后不应存在空白译文"


def test_fill_placeholder_marked_untranslated(patches):
    """漏译补位行带 [未翻译] 前缀，且内容保留原句。"""
    parser = _make_parser(allow_empty_deletions=False)
    parser.ProcessTranslation("#2\nTranslation>\nうんうん。\n\n", validate=True)

    originals = _make_originals()
    matched, _ = _match(parser, originals)

    filled = {m.number for m in matched if m.text.startswith("[未翻译]")}
    assert filled == {1, 3, 4}, "漏译的 1/3/4 行应带 [未翻译] 前缀补位"
    assert originals[3].translation == "[未翻译] 頑張る", "补位内容应保留原句文本"


# ---------------------------------------------------------------------------
# 补丁安装幂等性
# ---------------------------------------------------------------------------

def test_patch_installation_idempotent():
    """补丁安装幂等：重复安装返回 False，不重复包装/覆盖。"""
    import importlib

    import PySubtrans.Providers.Clients.CustomClient as CC
    import PySubtrans.TranslationParser as TP
    ST = importlib.import_module("PySubtrans.SubtitleTranslator")

    checks = [
        (dp.install_deletion_patch, TP.TranslationParser, "_deletion_patch_installed"),
        (dp.install_transient_retry_patch,
         CC.CustomClient, "_transient_retry_patch_installed"),
        (dp.install_zero_batch_skip_patch,
         ST.SubtitleTranslator, "_zero_batch_skip_patch_installed"),
    ]
    for install, cls, attr in checks:
        was_installed = getattr(cls, attr, False)
        first = install()
        second = install()
        assert second is False, f"{attr} 第二次安装应幂等返回 False"
        assert install() is False, f"{attr} 第三次安装应幂等返回 False"
        if not was_installed:
            assert first is True, f"{attr} 首次安装应返回 True"