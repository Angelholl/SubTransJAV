"""
后置语言白名单校验（Language whitelist validation）
====================================================
根治模型输出乱码/幻觉残留：对每个阶段的产物逐条做语言签名校验，
不符合目标语言签名（无 CJK 字符、纯拉丁字母串、明显二进制乱码）的
条目视为"幻觉产物"，在写出前剔除并归档到 Errors/dropped_entries.log。

与清洗协议（留空=删除）互补：模型主动留空的由删除感知解析器处理；
这里兜底的是模型"编造出来"的乱码残留——例如输出日文阶段的纯拉丁
行 "I could.."，或中文阶段的 "放火被真有好一点" 之类无实义串。

规则（按阶段目标语言）：
  - 日文：必须含假名或汉字（CJK 统一区）；纯 ASCII 拉丁串 → 无效
  - 中文（S2/S3）：必须含 CJK 汉字；纯 ASCII 拉丁串 → 无效
  - 兜底：剔除率超过阈值时全量放行（避免误杀整批正常内容），并记录警告
"""

import logging
import os
import re
from datetime import datetime

# CJK 覆盖：汉字＋假名＋CJK标点＋全角字符
# （\uff00-\uffef 含半角片假名 \uff66-\uff9f：半角片假名明确计入
#   日文（CJK）侧，与 _LATIN_WITH_CJK_PUNCT_RE 的排除口径一致）
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf\u31f0-\u31ff"
    r"\u3000-\u303f\uff00-\uffef]"
)
# 纯拉丁/西文串（"I could.."、英文残留、乱码转写）
_LATIN_ONLY_RE = re.compile(r"^[A-Za-z\s.,!?'\"\-:;()/\\\d]+$")
# 纯拉丁+标点串（"I don't know this。""No、no。"）含 CJK 标点但仍为英文残留
# 注意：全角段只取 \uff01-\uff60 / \uffe0-\uffe6（全角 ASCII/符号），
# 刻意排除 \uff61-\uff9f——半角片假名（\uff66-\uff9f）属于日文，
# 否则 "Englishﾃｽﾄ" 之类的混排会被误判为英文残留删除
_LATIN_WITH_CJK_PUNCT_RE = re.compile(
    r"^[A-Za-z\s\d.,!?'\"\-:;()/\\\u3000-\u303f\uff01-\uff60\uffe0-\uffe6]+$"
)
# 明显二进制乱码（控制字符密度极高）
_CONTROL_HEAVY_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# === 白名单：合法短日文，防止误杀 ===
# 1. 肯定/否定回应词（常作为独立条目出现）
_RESPONSE_WORDS = {
    "はい", "いいえ", "うん", "ううん", "えっ", "ええ", "あー", "おう", "まあ",
    "わかった", "了解", "りょうかい", "OK", "おはよう", "おやすみ",
}
# 2. 高频情绪/反应词（剧情关键反应，非填充噪音）
_EMOTION_WORDS = {
    "すごい", "やばい", "だめ", "かわいい", "エロい", "痛い", "気持ちいい",
    "最高", "最悪", "本当", "うそ", "まじ", "まじで", "冗談", "びっくり",
    "やめて", "やめ", "だめだ", "まずい", "おいしい", "いい", "よい",
    "好き", "嫌い", "怖い", "嬉しい", "悲しい", "幸せ", "つらい",
    "エース", "嘘", "嘘だ", "マジか", "マジで",
    "うれしい", "かなしい", "たのしい", "さびしい",
    "うわ", "わあ", "ひゃー", "きもい", "キモい",
}
# 3. 称呼词（触发联动保留扫描，极短但极重要）
_ADDRESS_WORDS = {
    "先輩", "せんぱい", "部長", "ぶちょう", "先生", "せんせい",
    "お前", "お前さん", "君", "きみ", "お客様", "おきゃくさま",
    "社長", "しゃちょう", "課長", "かちょう", "隊長", "たいちょう",
    "宮下", "みやした", "森田", "翠名", "ママ", "パパ",
}
# 4. 叙事/内心独白标记词（第一人称/心理动词）
_NARRATIVE_MARKERS = {
    "僕は", "私は", "わたくし", "自分は", "思う", "感じる", "思った", "感じた", "思ってる", "感じてる",
}
# 5. 日语感叹词/语气词（独立短句，剧情关键）
_INTERJECTION_WORDS = {
    "ああ", "ええと", "うーん", "ん", "はぁ", "わあ", "あら", "あれ",
    "そう", "そうか", "そうね", "そうなんだ", "なるほど", "やっぱり",
    "ちょっと", "もう", "ほんと", "ほんとに", "いいよ", "いいの",
    "だめよ", "だめだよ", "やめてよ", "すごいね", "やばいね",
    "はいはい", "いいえ", "ううん",
    "あ", "いや", "やだ", "うわ", "ほら",
    "この", "こう", "そっか", "かな", "ねえ", "ねん",
    "さあ", "だよね", "もっと",
    "おお", "あああ", "ううう", "えええ",
}
# 6. 独立实义短词（2-3字假名，但在对话中有明确语义）
_INDEPENDENT_WORDS = {
    "ジン", "ベロ", "ザコ", "ザク", "マコ", "ママ",
    "うん", "ふん", "んん", "ええ",
    "ちょっと", "もうちょっと", "まだまだ",
    "どうぞ", "どうだ", "どうして", "どうすんの",
    "ありがとう", "すみません", "ごめん", "ごめんね",
}
# 合并白名单集合
_WHITELIST_JA = (
    _RESPONSE_WORDS | _EMOTION_WORDS | _ADDRESS_WORDS
    | _NARRATIVE_MARKERS | _INTERJECTION_WORDS | _INDEPENDENT_WORDS
)

# 预编译：纯假名短词（1-2字）但不在白名单 → 判定为碎片/幻觉
_KANA_SHORT_RE = re.compile(r"^[\u3040-\u30ff]{1,2}[。！？,.]?$")
# 日语助词：短句含助词→视为有语法结构的有效台词
_JA_PARTICLES_RE = re.compile(r'[はがをにでともねよか]|から|まで|へ')


def is_valid_stage_text(text: str, target: str = "ja") -> bool:
    """校验一条产物是否符合目标语言签名。target: 'ja' 或 'zh'。"""
    if not text or not text.strip():
        return True  # 空条目交给删除感知语义，不进此过滤器
    t = text.strip()

    # 阶段B输入格式"日文 ||| 中文"被回显进中文产物 → 无效
    # （clean_grammar_hint_residue 负责清理，此处兜底拒绝）
    if target == "zh" and " ||| " in t:
        return False

    # === 日文阶段：白名单提前放行（防误杀，优先于签名检查） ===
    # 移至此处的原因：CJK_RE 未覆盖 CJK 标点（U+3000-U+303F），
    # 导致 "OK。" 等仅含拉丁+句号的白名单条目被"无 CJK"规则误杀。
    if target == "ja":
        for w in _WHITELIST_JA:
            if w in t:
                return True

    # 硬乱码：明显控制字符 → 无效
    if _CONTROL_HEAVY_RE.search(t):
        return False

    # 纯拉丁串（无任何 CJK）→ 幻觉/乱码转写，无效
    if _LATIN_ONLY_RE.match(t):
        return False

    # 纯拉丁+标点串（含 CJK 标点但仍为英文残留）→ 无效
    # 仅当文本含 ASCII 字母时才判定为英文残留（纯标点条目不在此列）
    if re.search(r"[A-Za-z]", t) and _LATIN_WITH_CJK_PUNCT_RE.match(t):
        return False

    # 无 CJK 字符的混合串（仅数字/符号/其他脚本）→ 无效
    if not _CJK_RE.search(t):
        return False

    # === 日文阶段：语法/形态规则 ===
    if target == "ja":
        # 短句含日语助词 → 视为有语法结构的有效台词
        if _JA_PARTICLES_RE.search(t):
            return True
        # 纯假名 1-2 字但不在白名单 → 判定为碎片/幻觉
        if _KANA_SHORT_RE.match(t):
            return False
        # 纯汉字 ≤6 字 → 有效（日语中常见为称呼/感叹/人名/复合词）
        if re.match(r'^[\u4e00-\u9fff]{1,6}[。！？,.]?$', t):
            return True
        # 中英混排无实义（少量汉字+逗号/空格+英文）→ ASR幻觉
        if re.match(r'^[\u4e00-\u9fff]{1,3}[,\s]*[A-Za-z]', t):
            return False

    # 目标语言细化（可选）：中文阶段需汉字（含假名的纯日文串在 S2 视为残留）
    if target == "zh":
        han = re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", t)
        if not han:
            # 纯假名（无汉字）在中文产物中属于"未翻译原文残留"
            kana = re.search(r"[\u3040-\u30ff]", t)
            if kana:
                return False
    return True


class DroppedEntryLog:
    """追加写入 Errors/dropped_entries.log，记录被剔除的幻觉条目。

    大小阈值轮转：写入前检查主文件大小，超过 dropped_log_rotate_mb
    （分层配置，默认 5MB）时把现有文件改名为 dropped_entries-<n>.log
    （<n> 自 1 起取首个空位）后新建继续写，防止追加累积无限增长。
    """

    _DEFAULT_ROTATE_MB = 5  # 与 config.DEFAULT_DROPPED_LOG_ROTATE_MB 一致

    def __init__(self, errors_dir: str):
        self.errors_dir = errors_dir
        self.path = os.path.join(errors_dir, "dropped_entries.log")
        self._rotate_bytes = self._resolve_rotate_mb() * 1024 * 1024
        self._ensure_dir()

    @classmethod
    def _resolve_rotate_mb(cls) -> int:
        """读取轮转阈值（MB，分层配置）；配置非法（<=0）或解析失败时
        静默回退默认 5MB，绝不抛异常（该日志链路一贯静默容错）。"""
        try:
            from .config import resolve_tunable
            mb = int(resolve_tunable("dropped_log_rotate_mb"))
        except Exception:
            return cls._DEFAULT_ROTATE_MB
        return mb if mb > 0 else cls._DEFAULT_ROTATE_MB

    def _ensure_dir(self):
        if self.errors_dir and not os.path.isdir(self.errors_dir):
            os.makedirs(self.errors_dir, exist_ok=True)

    def _maybe_rotate(self):
        """主文件超过阈值时轮转为 dropped_entries-<n>.log（静默容错）。"""
        try:
            if not os.path.isfile(self.path) \
                    or os.path.getsize(self.path) < self._rotate_bytes:
                return
            seq = 1
            while True:
                candidate = os.path.join(
                    self.errors_dir, f"dropped_entries-{seq}.log")
                if not os.path.exists(candidate):
                    break
                seq += 1
            os.replace(self.path, candidate)
        except OSError:
            pass

    def append(self, source: str, stage_index: int, number: int,
               text: str, reason: str):
        self._maybe_rotate()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (f"[{ts}] 来源={source} 阶段={stage_index + 1} "
                f"编号={number} 原因={reason}\n    原文: {text!r}\n")
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logging.warning(f"dropped_entries.log 写入失败: {e}")


def filter_stage_output(srt_path: str, stage_index: int,
                        target_lang: str, log=None) -> tuple:
    """对阶段产物执行语言白名单过滤（就地重写 .srt）。

    返回 (保留数, 剔除数)。剔除条目追加到 Errors/dropped_entries.log。
    """
    from .filters import build_srt, parse_srt

    if log is None:
        log = logging.getLogger(__name__)

    try:
        with open(srt_path, encoding="utf-8") as _f:
            entries = parse_srt(_f.read())
    except OSError as e:
        log.warning(f"language-validate 读取失败: {e}")
        return 0, 0

    if not entries:
        return 0, 0

    target = "zh" if target_lang in ("chinese", "zh") else "ja"
    kept, dropped = [], []

    # 为避免单批误杀，先粗扫全文件剔除率
    total_invalid = sum(
        1 for e in entries
        if not is_valid_stage_text(e["text"], target))
    if total_invalid and len(entries) > 0 \
            and total_invalid / len(entries) > 0.5:
        log.warning(
            f"[language-validate] 剔除率 {total_invalid}/{len(entries)} "
            f"过高，疑似校验失效，本次全部放行以免误杀")
        return len(entries), 0

    dropped_log = DroppedEntryLog(
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "Errors"))

    for e in entries:
        if is_valid_stage_text(e["text"], target):
            kept.append(e)
        else:
            dropped.append(e)
            dropped_log.append(
                os.path.basename(srt_path), stage_index,
                e.get("index"), e["text"] or "",
                f"乱码/语言签名不符({target})")

    if dropped:
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(build_srt(kept))

    if dropped:
        log.info(f"[language-validate] 剔除 {len(dropped)} 条乱码残留 -> "
                 f"{dropped_log.path}")
    return len(kept), len(dropped)
