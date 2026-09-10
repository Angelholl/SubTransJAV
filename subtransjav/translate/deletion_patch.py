"""
删除感知解析补丁（Deletion-aware parsing patch）
================================================
净语清洗协议（角色卡）规定：要删除的条目在 Translation> 后留空。
但 PySubtrans 原生校验把空译文当作错误：
  - SubtitleValidator.ValidateTranslations: 空文本 -> EmptyLinesError
  - 默认 retry_instructions 要求 "EVERY line has a corresponding translation"
结果：模型第一遍删掉的噪音行在校验失败后被重试指令填回，删除被系统性撤销。

此补丁改变解析语义：
  - 编号匹配但译文为空白 的条目 => 记入 parser.deletions（有意删除）
  - 不参与校验、不产生 UntranslatedLinesError/EmptyLinesError、不触发重试
  - 保存时自然消失（PySubtrans 跳过无译文行）

空译文语义按阶段切换（并发安全）：
  - 阶段1/3 清洗类：开启（空 = 有意删除）
  - 阶段2 翻译类：关闭（空走原生 EmptyLinesError -> retry_instructions 补译）
判断依据以「解析器实例的 options['allow_empty_deletions']」为准，
全局值仅作兼容兜底 —— 多文件并发时各文件各阶段互不干扰。
"""

import logging
import threading
from datetime import timedelta

import regex as _regex

from PySubtrans.SubtitleLine import SubtitleLine

# ---------------------------------------------------------------------------
# 删除语义开关（线程局部，支持并发文件各自独立的删除语义）
# ---------------------------------------------------------------------------
_allow_deletions_state = threading.local()


def set_allow_deletions(flag: bool):
    """按阶段设置空译文语义。refine 阶段1/3 传 True，阶段2 传 False。"""
    _allow_deletions_state.value = bool(flag)


def _allow_deletions() -> bool:
    return getattr(_allow_deletions_state, "value", False)


def _deletions_allowed_for(parser) -> bool:
    """读取解析器实例级删除语义，缺失则回退线程局部兜底。

    说明：生产路径中 PySubtrans 的 ``Options.GetSettings()`` 只返回
    ``default_settings`` 键（见 Options.py），``allow_empty_deletions``
    并非默认键，会被过滤掉、无法经 opt_kwargs 传递到 parser.options，
    故实际始终回退到线程局部值。而 PySubtrans 在调用线程内顺序处理批次
    （无工作线程池），``set_allow_deletions()`` 在调用线程置位即可被
    解析器正确读到。测试里则直接构造带该键的 Options，两种路径均覆盖。
    """
    fallback = getattr(_allow_deletions_state, "value", False)
    try:
        opts = getattr(parser, "options", None)
        if opts is not None:
            return bool(opts.get_bool("allow_empty_deletions", fallback))
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# 删除感知主正则（超集）：
# 原生默认模式对"空条目紧跟下一条"(Translation>\n#3) 存在贪婪回退缺陷——
# 可选 body 组优先吞掉后续整个条目，导致空译文从未被正确识别。
# 此模式以 lookahead 直接切分条目：body 惰性匹配到下一个 #N 或结尾，
# 空条目的 body 自然为空白。
#
# 超集相对早期版本的关键修复：
#   - 恢复「可选 Original>/原句 段」：PySubtrans 批次 prompt 自带
#     #N/Original>/Translation> 结构，模型会回显 Original>，缺失该段
#     会导致整个阶段 100% NoMatches（实测回归）。
#   - 支持多种译文标记：Translation> / 翻译> / 意译> / 译文(>：:)
#   - 容忍并剥离 **markdown 加粗**
#   - 仍保留原生全部 fallback 模式兜底（裸 #N+译文 等变体）
# ---------------------------------------------------------------------------
_SUPERSET_PATTERN = _regex.compile(
    r"#(?P<number>\d+)[ \t]*[\r\n]+[ \t]*"
    r"(?:(?:\*\*)?(?:Original|原句)[>：:][ \t]*(?:\*\*)?[\s\S]*?"
    r"(?=[\r\n]+[ \t]*(?:\*\*)?(?:Translation|翻译|意译|译文)[>：:]"
    r"|[ \t]*#\d+[ \t]*[\r\n]|[ \t]*\Z))?"
    r"(?:[\r\n]+[ \t]*(?:\*\*)?(?:Translation|翻译|意译|译文)[>：:][ \t]*(?:\*\*)?)?"
    r"(?P<body>[\s\S]*?)"
    r"(?=\s*#\d+\b|\Z)"
)


def install_deletion_patch():
    """对 PySubtrans.TranslationParser 打全局补丁（幂等）。"""
    import PySubtrans.TranslationParser as TP

    if getattr(TP.TranslationParser, "_deletion_patch_installed", False):
        return False

    _orig_pattern_fn = TP.TranslationParser.GetRegularExpressionPatterns

    def _patched_regular_expression_patterns(self, task_type):
        stock = _orig_pattern_fn(task_type)
        return [_SUPERSET_PATTERN] + stock

    TP.TranslationParser.GetRegularExpressionPatterns = _patched_regular_expression_patterns

    # ---------------------------------------------------------------
    # ProcessTranslation：拆出"空译文=删除"的编号，再走原校验逻辑
    # ---------------------------------------------------------------
    def _patched_process_translation(self, translation, validate=True):
        self.text = translation.text if hasattr(translation, "text") else str(translation)

        if not self.text:
            from PySubtrans.SubtitleError import TranslationError
            raise TranslationError("No translated text provided", translation=translation)

        from PySubtrans.Helpers.SubtitleHelpers import MergeTranslations

        matches = []
        for template in self.regex_patterns:
            matches = [
                {
                    "body": m.group("body"),
                    "number": m.groupdict().get("number"),
                    "start": m.groupdict().get("start"),
                    "end": m.groupdict().get("end"),
                    "original": m.groupdict().get("original"),
                }
                for m in template.finditer(f"{self.text}\n\n")
            ]
            if matches:
                break

        if not matches:
            from PySubtrans.SubtitleError import TranslationError
            logging.warning(
                f"No matches found in response (first 200 chars): {self.text[:200]!r}")
            raise TranslationError(
                f"No matches found in translation text using patterns: {self.regex_patterns}",
                translation=translation)

        subs = [SubtitleLine(match) for match in matches]

        def _normalize(text):
            """规整正文：剥离格式残留（**加粗** / 误吞的标签前缀）。"""
            if not text:
                return ""
            t = text.strip()
            t = t.strip("*")
            for marker in ("Translation>", "翻译>", "意译>", "译文>", "译文："):
                if t.startswith(marker):
                    t = t[len(marker):].strip()
                    break
            return t

        for sub in subs:
            sub.text = _normalize(sub.text)

        # 关键差异（仅当本阶段开启删除语义）：空白正文 => 有意删除，
        # 从翻译集剥离并记录。阶段2（翻译）关闭时保留空白条目，
        # 交给原生校验生成 EmptyLinesError -> 触发补译，而非删除。
        if _deletions_allowed_for(self):
            self.deletions = {sub.key for sub in subs
                              if not (sub.text or "").strip()}
            kept = [sub for sub in subs if sub.key not in self.deletions]
            if self.deletions:
                logging.debug(
                    f"Deletion-aware parser: {len(self.deletions)} entries "
                    f"marked as deleted")
        else:
            self.deletions = set()
            # 关闭清洗语义：保留空白条目本身，彻底清空标记残留，
            # 让原生校验据空白正文判 EmptyLinesError（触发补译）
            kept = subs
            for _sub in kept:
                if not (_sub.text or "").strip():
                    _sub.text = ""

        self.translations = {sub.key: sub for sub in kept}

        # 空批次补位：所有条目都被删除或解析失败时，用原文填充
        if not self.translations and not _deletions_allowed_for(self):
            # 翻译语义下整批失败 → 用原文补位，标记[未翻译]
            for match in matches:
                sub = SubtitleLine(match)
                sub.text = _normalize(sub.text)
                if not sub.text:
                    sub.text = "[未翻译] " + (match.get("original") or "").strip()
                self.translations[sub.key] = sub

        if not self.translations and _deletions_allowed_for(self):
            # 清洗语义下整批全删是合法输出（纯噪音批次）。
            # 注意：不得清空 self.translated —— 它跨批次累积（MergeTranslations），
            # 清空会抹掉本场景之前批次已累积的翻译（数据丢失）。
            self.errors = []
            return self.translated

        self.translated = MergeTranslations(
            self.translated, list(self.translations.values()))

        if validate:
            self.errors = self.ValidateTranslations()
            if self.errors and self.translated:
                self._fix_unclosed_tags()
                self.errors = self.ValidateTranslations()

        return self.translated

    # ---------------------------------------------------------------
    # MatchTranslations：已声明删除的编号不算"未翻译"
    # ---------------------------------------------------------------
    def _patched_match_translations(self, originals):
        deletions = getattr(self, "deletions", set())

        matched, unmatched = [], []

        for item in originals:
            translation = self.translations.get(item.key)
            if translation:
                translation.number = item.number
                translation.start = item.start or timedelta(seconds=0)
                translation.end = item.end or timedelta(seconds=0)
                translation.metadata = item.metadata

                from PySubtrans.Helpers.Text import IsTextContentEqual
                if translation.original and IsTextContentEqual(
                        translation.text, item.text):
                    translation.text = translation.original
                    translation.original = item.text

                item.translation = translation.text
                matched.append(translation)
            elif item.key in deletions:
                item.translation = ""   # 显式删除标记；保存时跳过
            else:
                if _deletions_allowed_for(self):
                    # 清洗语义（S3）：未匹配且未声明删除 -> 留空位（模型删行），
                    # 走删除判定，不误伤
                    item.translation = None
                    unmatched.append(item)
                else:
                    # 翻译语义（S2）：模型漏译个别行 -> 静默补位原文，
                    # 标记 [未翻译] 前缀，不丢批、不抛错、不写警告日志
                    _fallback = "[未翻译] " + (item.text or "").strip()
                    item.translation = _fallback
                    _sub = SubtitleLine(original=item.text)
                    _sub.number = item.number
                    _sub.start = item.start or timedelta(seconds=0)
                    _sub.end = item.end or timedelta(seconds=0)
                    _sub.text = _fallback
                    matched.append(_sub)

        if unmatched:
            self.TryFuzzyMatches(unmatched)

        if unmatched and _deletions_allowed_for(self):
            # 仅清洗语义下未匹配才算错误（触发重试/待补）；翻译语义下
            # 未匹配已在上方补位，不应再产生 UntranslatedLinesError
            from PySubtrans.SubtitleError import UntranslatedLinesError
            self.errors.append(UntranslatedLinesError(
                f"No translation found for {len(unmatched)} lines", lines=unmatched))

        return matched, unmatched

    TP.TranslationParser.ProcessTranslation = _patched_process_translation
    TP.TranslationParser.MatchTranslations = _patched_match_translations
    TP.TranslationParser._deletion_patch_installed = True
    return True


def install_transient_retry_patch():
    """瞬时限流重试补丁（幂等）。

    PySubtrans 原生只把 5xx 视为可重试：429（限流）/408（请求超时）作为
    ClientResponseError 直接抛穿，一次限流即杀死整个批量任务。对免费通道
    （Zen 免费模型 429 常见）与高峰期云端这是落地级隐患。此补丁将这两类
    瞬态 4xx 转为 ServerResponseError，进入既有的退避重试循环。
    """
    import PySubtrans.Providers.Clients.CustomClient as CC

    if getattr(CC.CustomClient, "_transient_retry_patch_installed", False):
        return False

    from PySubtrans.SubtitleError import ClientResponseError, ServerResponseError
    _RETRYABLE = (408, 429)

    def _as_retryable(exc):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status in _RETRYABLE:
            return ServerResponseError(str(exc), response=resp)
        return exc

    _orig_nonstreaming = CC.CustomClient._handle_non_streaming_request

    def _patched_nonstreaming(self, request_body):
        try:
            return _orig_nonstreaming(self, request_body)
        except ClientResponseError as e:
            raise _as_retryable(e) from e

    _orig_streaming = CC.CustomClient._handle_streaming_request

    def _patched_streaming(self, request, request_body):
        try:
            return _orig_streaming(self, request, request_body)
        except ClientResponseError as e:
            raise _as_retryable(e) from e

    CC.CustomClient._handle_non_streaming_request = _patched_nonstreaming
    CC.CustomClient._handle_streaming_request = _patched_streaming
    CC.CustomClient._transient_retry_patch_installed = True
    return True


def install_zero_batch_skip_patch():
    """零行批次跳过补丁（幂等）。

    PySubtrans 场景切分/断点续跑会生成"待翻译行=0"的空批次（日志反复出现
    "Scene X batch Y: 0 lines and 0 untranslated"），仍会发起无意义的 API
    请求，浪费 token/时间并在日志刷屏。此补丁包装 SubtitleTranslator
    .TranslateBatch：进入时若批次无待翻译行，静默返回、不发起请求、不写日志。
    """
    import importlib
    # 注意：PySubtrans/__init__.py 用 `from .SubtitleTranslator import
    # SubtitleTranslator` 把包内名字绑定成类，遮蔽了同名子模块。`import A.B as x`
    # 会取到包属性的"类"而非模块 —— 必须用 importlib.import_module 取真实模块。
    ST = importlib.import_module("PySubtrans.SubtitleTranslator")

    if getattr(ST.SubtitleTranslator, "_zero_batch_skip_patch_installed", False):
        return False

    _orig_translate_batch = ST.SubtitleTranslator.TranslateBatch

    def _patched_translate_batch(self, batch, line_numbers=None, context=None):
        pending = getattr(batch, "untranslated", None)
        if not pending:
            # 0 待翻译行：静默跳过，不调用 API、不输出批次信息
            return
        return _orig_translate_batch(self, batch, line_numbers, context)

    ST.SubtitleTranslator.TranslateBatch = _patched_translate_batch
    ST.SubtitleTranslator._zero_batch_skip_patch_installed = True
    return True