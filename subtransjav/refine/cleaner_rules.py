from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .source_hallucination import is_source_counting_noise

# Pre-compiled: sentence-fragment connector words (used in _merge_fragments)
_CONNECTOR_RE = re.compile(
    r'^(那个|这个|其实|但是|因为|所以|然后|就是|而且|不过|和|与|但|可|就|才|又|再|也|还|并|而|于|以|被|把|给|让|使|令|叫|请|帮)$'
)

# 源侧证据（v1.2.1 P0）：条目对应源文行含汉字 ⇒ 译文有真实源文支撑，
# 禁止删除；仅纯假名/符号噪声源文（如 ASR 幻觉"ああああ"）才放行 L 规则。
_HAS_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')


def resolve_data_file(
    name: str,
    config_dir: str | None = None,
    extra_roots: list[str] | None = None,
) -> Path:
    """按优先级回退链查找数据文件。

    查找顺序：
    1. config_dir（绝对路径直接用；相对路径按 cwd 解析）
    2. extra_roots 中每个目录（绝对路径或按 cwd 解析）
    3. <仓库根>/config/templates/（editable 安装/源码运行时命中）
    4. 包内默认 subtransjav/refine/defaults/（pip 安装时兜底）

    找不到时抛出 FileNotFoundError，列出所有搜索过的路径。
    """
    candidates: list[Path] = []

    # 1. 显式 config_dir
    if config_dir:
        p = Path(config_dir)
        if not p.is_absolute():
            p = Path.cwd() / p
        candidates.append(p / name)

    # 2. 额外搜索根目录
    if extra_roots:
        for root in extra_roots:
            r = Path(root)
            if not r.is_absolute():
                r = Path.cwd() / r
            candidates.append(r / name)

    # 3. 仓库根 config/templates（editable 安装）
    #    __file__ = .../subtransjav/refine/cleaner_rules.py
    #    parents[2] = 仓库根
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "config" / "templates" / name)
    # 部分规则文件位于仓库根 config/ 而非 config/templates/
    candidates.append(repo_root / "config" / name)

    # 4. 包内默认
    candidates.append(Path(__file__).parent / "defaults" / name)

    for c in candidates:
        if c.is_file():
            return c

    tried = "\n  ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"数据文件 '{name}' 未找到，已尝试以下路径：\n  {tried}"
    )


@dataclass
class Subtitle:
    index: int
    start: float
    end: float
    text: str


def parse_srt(content: str) -> list[Subtitle]:
    content = content.lstrip('\ufeff')
    items: list[Subtitle] = []
    blocks = re.split(r'\n\s*\n', content.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            lines[1].strip(),
        )
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600000 + g[1] * 60000 + g[2] * 1000 + g[3]
        end = g[4] * 3600000 + g[5] * 60000 + g[6] * 1000 + g[7]
        text = "\n".join(lines[2:]).strip()
        items.append(Subtitle(index=index, start=start, end=end, text=text))
    return items


def format_srt(items: list[Subtitle]) -> str:
    parts: list[str] = []
    for i, item in enumerate(items, 1):
        item.index = i
        s = item.start
        e = item.end
        sh, sm, ss, sms = int(s // 3600000), int(s % 3600000 // 60000), int(s % 60000 // 1000), int(s % 1000)
        eh, em, es, ems = int(e // 3600000), int(e % 3600000 // 60000), int(e % 60000 // 1000), int(e % 1000)
        ts = f"{sh:02d}:{sm:02d}:{ss:02d},{sms:03d}"
        te = f"{eh:02d}:{em:02d}:{es:02d},{ems:03d}"
        parts.append(f"{i}\n{ts} --> {te}\n{item.text}")
    return "\n\n".join(parts) + "\n"


class ChineseCleaner:

    def __init__(self, config_dir: str | None = None):
        self._config_dir = config_dir
        self._load_vocabularies()
        self._load_configs()

    def _load_vocabularies(self) -> None:
        self.sensory_roots: list[str] = [
            "舒服", "爽", "享受", "棒", "赞", "厉害", "痛苦",
            "好吃", "开心", "痛", "冷", "热", "难过", "可怕", "恶心",
        ]
        self.short_responses: set[str] = {
            "好", "行", "嗯", "对", "是", "哦", "噢", "嗯嗯", "好好",
            "不行", "不要", "不可以", "够了", "停下", "拜托", "知道了", "明白",
        }
        self.pure_exclamations: set[str] = {
            "啊", "哎呀", "哎哟", "哇", "诶", "嘿", "嘿嘿", "哈哈哈", "呵呵",
            "嘻嘻", "嘶", "唔", "嗯…", "啊…", "啊——", "呼", "呼呼", "呼噜",
            "咕啾", "滋溜", "咝咝", "噗",
        }
        self.pure_exclamations_chars: set[str] = set()
        for ex in self.pure_exclamations:
            for ch in ex:
                if ch not in "…——":
                    self.pure_exclamations_chars.add(ch)
        self.filler_words: set[str] = {
            "那个", "这个", "嗯…", "呃…", "就是说…", "怎么说呢…", "其实", "总觉得", "怎么说",
        }
        self.address_suffixes: set[str] = {
            "同学", "老师", "先生", "小姐", "前辈", "后辈", "部长", "经理", "主任",
            "校长", "哥", "姐", "弟", "妹", "爸", "妈", "叔", "姨", "桑", "酱", "君",
        }
        self.action_markers: list[str] = [
            "你", "我", "他", "她", "我们", "你们", "他们",
            "去", "来", "要", "给", "让", "把", "被", "帮", "弄", "干", "做",
            "说", "看", "听", "走", "跑", "跳", "停", "开始", "继续",
        ]

    def _load_configs(self) -> None:
        hardened_path = resolve_data_file("hardened_phrases.yaml", self._config_dir)
        hallucination_path = resolve_data_file("hallucination_patterns.yaml", self._config_dir)

        self._hardened_keywords_set: set[str] = set()
        self._hardened_patterns_compiled: list[re.Pattern[str]] = []
        with open(hardened_path, encoding="utf-8") as f:
            hdata = yaml.safe_load(f) or {}
        for kw in hdata.get("keywords", []):
            self._hardened_keywords_set.add(kw)
        for pat in hdata.get("patterns", []):
            self._hardened_patterns_compiled.append(re.compile(pat))

        self._strong_meta: list[str] = []
        self._weak_meta: list[str] = []
        self._garbage_patterns: list[re.Pattern[str]] = []
        self._parenthetical_re: re.Pattern[str] | None = None
        with open(hallucination_path, encoding="utf-8") as f:
            hdata = yaml.safe_load(f) or {}
        vm = hdata.get("video_meta", {})
        self._strong_meta = vm.get("strong", [])
        self._weak_meta = vm.get("weak", [])
        for pat in hdata.get("garbage", []):
            self._garbage_patterns.append(re.compile(pat))
        pstr = hdata.get("parenthetical", "")
        if pstr:
            self._parenthetical_re = re.compile(pstr)

    def _is_cqs_candidate(self, item: Subtitle) -> bool:
        return len(item.text) <= 10 and not self._has_substantive(item.text)

    def _mark_cqs_indices(self, items: list[Subtitle]) -> set[int]:
        cqs: set[int] = set()
        i = 0
        n = len(items)
        while i < n:
            if not self._is_cqs_candidate(items[i]):
                i += 1
                continue
            j = i
            while j + 1 < n:
                if (
                    not self._has_substantive(items[j + 1].text)
                    and len(items[j + 1].text) <= 10
                    and items[j + 1].start - items[j].end <= 3000
                ):
                    j += 1
                else:
                    break
            if j - i + 1 >= 3:
                for k in range(i, j + 1):
                    cqs.add(k)
            i = j + 1
        return cqs

    def _split_interaction_segments(self, items: list[Subtitle], gap_ms: float = 1500) -> list[list[int]]:
        if not items:
            return []
        segments: list[list[int]] = []
        current: list[int] = [0]
        for i in range(1, len(items)):
            if items[i].start - items[i - 1].end <= gap_ms:
                current.append(i)
            else:
                segments.append(current)
                current = [i]
        segments.append(current)
        return segments

    def _has_substantive(self, text: str) -> bool:
        if not text:
            return False
        cleaned = re.sub(r'[，。、！？\s,\.!?…~～]', '', text)
        remaining = cleaned
        for root in self.sensory_roots:
            remaining = remaining.replace(root, "")
        for p in ["啊", "呀", "哦", "喔", "呢", "吧", "嘛", "了"]:
            remaining = remaining.replace(p, "")
        degree_words = {"好", "太", "真", "非常", "超级", "特别", "真的好"}
        for dw in degree_words:
            remaining = remaining.replace(dw, "")
        return any(marker in remaining for marker in self.action_markers)

    def _extract_emotion_root(self, text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        for root in self.sensory_roots:
            if root in stripped:
                return root
        cleaned = re.sub(r'[，。、！？\s,\.!?…~～]', '', stripped)
        if cleaned and cleaned[0] in self.pure_exclamations_chars:
            return cleaned[0]
        return None

    def _is_video_meta(self, text: str) -> bool:
        stripped = text.strip()
        if len(stripped) <= 30:
            for kw in self._strong_meta:
                if kw in stripped:
                    return True
        if len(stripped) <= 40:
            has_strong = any(kw in stripped for kw in self._strong_meta)
            has_weak = any(kw in stripped for kw in self._weak_meta)
            if has_strong and has_weak:
                return True
        return False

    def _is_parenthetical_meta(self, text: str) -> bool:
        stripped = text.strip()
        if not self._parenthetical_re or not self._parenthetical_re.match(stripped):
            return False
        inner = stripped[1:-1]
        if inner in self._hardened_keywords_set:
            return False
        return all(not pat.search(inner) for pat in self._hardened_patterns_compiled)

    def _is_garbage(self, text: str) -> bool:
        stripped = text.strip()
        return any(pat.match(stripped) for pat in self._garbage_patterns)

    def _is_pure_exclamation(self, text: str) -> bool:
        stripped = re.sub(r'[，。、！？\s,\.!?…~～…—]', '', text.strip())
        return stripped in self.pure_exclamations

    def _is_isolated_sensory(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        for root in self.sensory_roots:
            if root in stripped:
                cleaned = re.sub(r'[，。、！？\s,\.!?…~～]', '', stripped)
                cleaned = cleaned.replace(root, "")
                for p in ["啊", "呀", "哦", "喔", "呢", "吧", "嘛", "了", "好", "太", "真"]:
                    cleaned = cleaned.replace(p, "")
                if len(cleaned) == 0:
                    return True
        return False

    def _is_short_response(self, text: str) -> bool:
        stripped = re.sub(r'[，。、！？\s,\.!?…~～]', '', text.strip())
        return stripped in self.short_responses

    def _is_filler(self, text: str) -> bool:
        stripped = re.sub(r'[，。、！？\s,\.!?…~～]', '', text.strip())
        return stripped in self.filler_words

    def _is_isolated_address(self, text: str) -> bool:
        stripped = re.sub(r'[，。、！？\s,\.!?…~～]', '', text.strip())
        for suffix in self.address_suffixes:
            if stripped == suffix:
                return True
            if stripped.endswith(suffix):
                prefix = stripped[: -len(suffix)]
                if len(prefix) <= 2:
                    return True
        return False

    def _check_60s_dedup(self, idx: int, items: list[Subtitle], segment_indices: set[int]) -> bool:
        current_time = items[idx].start
        cutoff = current_time - 60000
        root = self._extract_emotion_root(items[idx].text)
        if not root:
            return False
        for j in range(idx - 1, -1, -1):
            if items[j].start < cutoff:
                break
            if j in segment_indices:
                continue
            other_root = self._extract_emotion_root(items[j].text)
            if other_root and other_root == root:
                return True
        return False

    def _is_hardened(self, text: str) -> bool:
        stripped = text.strip()
        for kw in self._hardened_keywords_set:
            if kw in stripped:
                return True
        return any(pat.search(stripped) for pat in self._hardened_patterns_compiled)

    def _is_linkage_keep(self, item: Subtitle, items: list[Subtitle], idx: int) -> bool:
        """联动保留：向后扫描3秒内是否有实义承接"""
        current_end = item.end
        for j in range(idx + 1, len(items)):
            if items[j].start - current_end > 3000:
                break
            if self._has_substantive(items[j].text) or self._is_hardened(items[j].text):
                return True
        return False

    def _get_segment_indices(self, idx: int, segments: list[list[int]]) -> set[int]:
        """获取idx所在段的所有索引"""
        for seg in segments:
            if idx in seg:
                return set(seg)
        return set()

    @staticmethod
    def _gate_delete(rule: str, source_texts: list[str] | None,
                     noise_gate: bool = False) -> tuple[bool, str]:
        """L3-L12 删除提交前的源侧证据门槛：
        - 证据缺失（source_map 未提供/该条目未对齐到源文）→ fail-safe 保留；
        - 任一源文行含汉字 → 保留（kept-by-source-evidence，计入统计）；
        - v1.2.2 C2（noise_gate=True，仅 L7/L8/L11）：源文虽不含汉字，但
          未命中闸门0 计数类噪声特征 → 保留（kept-by-noise-gate，计入
          统计）。合并条目取成员并集判定：任一成员为实义假名（白名单词
          或非噪声串）即整体保留，防合并行连带丢实义；
        - 源文全部为纯假名且（noise_gate 时）全部命中计数类噪声特征
          → 放行删除（返回规则名）。"""
        if not source_texts:
            return False, "kept-no-evidence"
        if any(_HAS_KANJI_RE.search(t or "") for t in source_texts):
            return False, "kept-by-source-evidence"
        if noise_gate and not all(
                is_source_counting_noise(t or "") for t in source_texts):
            return False, "kept-by-noise-gate"
        return True, rule

    def _should_delete(
        self,
        item: Subtitle,
        items: list[Subtitle],
        idx: int,
        cqs_indices: set[int],
        segments: list[list[int]],
        source_texts: list[str] | None = None,
    ) -> tuple[bool, str]:
        """判定是否删除。source_texts 为该条目时间轴对应的源文日文行
        （合并条目为多条）；任何删除类规则（L3-L12）提交前须过源侧证据
        门槛（见 _gate_delete）：证据缺失或源文含汉字一律保留。
        v1.2.2 C2：L7/L8/L11 追加"源侧噪声证据"——源文不含汉字时还须
        命中闸门0 计数类噪声特征才允许删除（实义纯假名源文如 やめて
        一律保留）；L3-L6/L9-L12 维持原门槛。
        L0-L2 为保留路径，不受影响。source_texts=None 时可独立测试，
        此时按 fail-safe 只保留不删。"""
        text = item.text.strip()

        if self._is_hardened(text):
            return False, "L0-hardened"

        if self._is_linkage_keep(item, items, idx):
            return False, "L1-linked"

        seg = self._get_segment_indices(idx, segments)
        same_seg_has_content = False
        for j in seg:
            if j == idx:
                continue
            if (abs(items[j].start - item.start) <= 3000
                    and (self._has_substantive(items[j].text) or self._is_hardened(items[j].text))):
                same_seg_has_content = True
                break
        if same_seg_has_content:
            return False, "L2-segment-content"

        if self._is_video_meta(text):
            return self._gate_delete("L3-meta", source_texts)

        if self._is_parenthetical_meta(text):
            return self._gate_delete("L4-parenthetical", source_texts)

        if self._is_garbage(text):
            return self._gate_delete("L5-garbage", source_texts)

        if self._is_pure_exclamation(text):
            return self._gate_delete("L6-pure-exclamation", source_texts)

        if self._is_isolated_sensory(text):
            return self._gate_delete("L7-sensory", source_texts,
                                     noise_gate=True)

        if self._is_short_response(text):
            return self._gate_delete("L8-short-response", source_texts,
                                     noise_gate=True)

        if self._is_filler(text):
            return self._gate_delete("L9-filler", source_texts)

        if self._is_isolated_address(text):
            return self._gate_delete("L10-address", source_texts)

        if self._check_60s_dedup(idx, items, seg):
            return self._gate_delete("L11-60s-dedup", source_texts,
                                     noise_gate=True)

        cleaned = re.sub(r'[\s,\.!?…~～，。、！？]', '', text)
        if len(cleaned) <= 2:
            has_chinese = bool(re.search(r'[\u4e00-\u9fff]', cleaned))
            if not has_chinese:
                return self._gate_delete("L12-non-chinese-short", source_texts)

        return False, "keep"

    def _merge_fragments(self, items: list[Subtitle]) -> list[Subtitle]:
        if len(items) < 2:
            return items
        merged: list[Subtitle] = []
        i = 0
        while i < len(items):
            current = items[i]
            candidates = [current]
            j = i + 1
            while j < len(items):
                if items[j].start - candidates[-1].end > 500:
                    break
                if len(candidates) >= 3:
                    break
                total_duration = items[j].end - current.start
                if total_duration > 8000:
                    break
                prev_text = candidates[-1].text
                # 仅前条以未完结标点收尾、或整体是连接词时合并；
                # 后条以连接词开头不作为合并依据（独立完整句常以"不过/但是"起头），
                # 省略号收尾也常是完整句
                ends_incomplete = prev_text.endswith(("，", "、", ":", "：", "——", ","))

                prev_cleaned = re.sub(r'[，。、！？\s,\.!?…~～]', '', prev_text)
                is_prev_connector = _CONNECTOR_RE.match(prev_cleaned) is not None
                if ends_incomplete or is_prev_connector:
                    candidates.append(items[j])
                    j += 1
                else:
                    break
            if len(candidates) == 1:
                merged.append(current)
                i += 1
            else:
                merged_text = " ".join([c.text for c in candidates])
                new_item = Subtitle(
                    index=current.index,
                    start=current.start,
                    end=candidates[-1].end,
                    text=merged_text,
                )
                merged.append(new_item)
                i = j
        for idx, item in enumerate(merged, 1):
            item.index = idx
        return merged

    @staticmethod
    def _map_evidence_to_merged(
        pre_items: list[Subtitle],
        merged_items: list[Subtitle],
        source_evidence: list[list[str] | None] | None,
    ) -> dict[int, list[str]]:
        """把"清洗前条目→源文"证据按时间轴包含关系归并到合并后条目：
        合并条目继承其全部成员的源文集合（任一成员含汉字即触发保留）。"""
        if not source_evidence:
            return {}
        result: dict[int, list[str]] = {}
        eps = 1.0
        for mi, m in enumerate(merged_items):
            texts: list[str] = []
            for pi, p in enumerate(pre_items):
                if pi >= len(source_evidence):
                    break
                if p.start >= m.start - eps and p.end <= m.end + eps:
                    ev = source_evidence[pi]
                    if ev:
                        texts.extend(ev)
            if texts:
                result[mi] = texts
        return result

    def filter(
        self,
        items: list[Subtitle],
        source_evidence: list[list[str] | None] | None = None,
    ) -> tuple[list[Subtitle], dict]:
        """碎片合并 + 规则删除。source_evidence 与 items（清洗前）等长，
        每项为该条目对应的源文日文行列表（None=无证据 → fail-safe 保留）。
        返回 (保留条目, stats)，stats 含 merged / deleted / deleted_by_rule /
        kept_by_source_evidence / kept_by_noise_gate（v1.2.2 C2：因未命中
        噪声证据而免删的条数）/ kept_by_noise_gate_timings（对应条目的
        时间轴串列表，质量报告"纯假名实义保留 [未翻译] 标记"统计消费）。"""
        stats: dict = {"merged": 0, "deleted": 0,
                       "deleted_by_rule": {}, "kept_by_source_evidence": 0,
                       "kept_by_noise_gate": 0,
                       "kept_by_noise_gate_timings": []}
        if not items:
            return [], stats
        pre_items = items
        items = self._merge_fragments(items)
        stats["merged"] = len(pre_items) - len(items)
        evidence = self._map_evidence_to_merged(pre_items, items, source_evidence)
        cqs_indices = self._mark_cqs_indices(items)
        segments = self._split_interaction_segments(items)
        kept: list[Subtitle] = []
        for idx, item in enumerate(items):
            if idx in cqs_indices:
                kept.append(item)
                continue
            should_del, reason = self._should_delete(
                item, items, idx, cqs_indices, segments,
                source_texts=evidence.get(idx))
            if should_del:
                stats["deleted"] += 1
                stats["deleted_by_rule"][reason] = \
                    stats["deleted_by_rule"].get(reason, 0) + 1
            else:
                if reason == "kept-by-source-evidence":
                    stats["kept_by_source_evidence"] += 1
                elif reason == "kept-by-noise-gate":
                    stats["kept_by_noise_gate"] += 1
                    stats["kept_by_noise_gate_timings"].append(
                        _timing_key(item))
                kept.append(item)
        for new_idx, item in enumerate(kept, 1):
            item.index = new_idx
        return kept, stats


# 语法提示残留清理模式：
# 1/2) 【语法提示】块整块（含到"原文："前缀）或单行残留；
# 3)   "原文：" 行整行删除（MULTILINE）——阶段B注入格式为
#      『【语法提示】…\n原文：日文 ||| 中文』，LLM 回显时"原文："总在
#      行首，且该行剩余部分是"日文 ||| 中文"回显，须整行删除；
#      行首限定保证正文中恰好包含"原文："字样的内容不受影响
_GRAMMAR_HINT_PATTERNS = [
    re.compile(r'【语法提示】[\s\S]*?原文：'),
    re.compile(r'【语法提示】.*?(?=\n|$)'),
    re.compile(r'^原文：.*$', re.MULTILINE),
]


def clean_grammar_hint_residue(text: str) -> str:
    """清理 LLM 输出中可能残留的语法提示标记。"""
    cleaned = text
    for pattern in _GRAMMAR_HINT_PATTERNS:
        cleaned = pattern.sub('', cleaned)
    # 兜底：阶段B输入格式"日文 ||| 中文"被整体回显时，只保留
    # 最后一个分隔符之后的中文侧
    if " ||| " in cleaned:
        cleaned = cleaned.rsplit(" ||| ", 1)[1]
    return cleaned.strip()


def _fmt_ts(ms: float) -> str:
    """毫秒 → "HH:MM:SS,mmm"（与 format_srt 输出一致）。"""
    h = int(ms // 3600000)
    m = int(ms % 3600000 // 60000)
    s = int(ms % 60000 // 1000)
    msec = int(ms % 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def _timing_key(item: Subtitle) -> str:
    """条目时间轴标识（与 SRT 时间轴行/caller entry["timing"] 同格式）。"""
    return f"{_fmt_ts(item.start)} --> {_fmt_ts(item.end)}"


def clean_srt(
    srt_content: str,
    config_dir: str | None = None,
    source_map: dict[str, str | list[str]] | None = None,
) -> tuple[str, dict]:
    """规则清洗 SRT，返回 (cleaned_srt, stats)。

    source_map（可选，v1.2.1 P0 源侧证据门槛）：清洗前条目的 timing 串
    （如 "00:00:01,000 --> 00:00:02,000"）→ 该条目时间轴对应的源文日文
    （str 或多行 list[str]）。删除类规则（L3-L12）提交前须过源侧证据：
    任一源文行含汉字、或证据缺失 → 保留（fail-safe）；合并条目继承其
    成员的全部源文。v1.2.2 C2：L7/L8/L11 追加源侧噪声证据门槛——源文
    不含汉字时还须命中闸门0 计数类噪声特征才允许删除。
    stats 结构（键名契约，质量报告消费）：
      merged — 碎片合并减少的条数；
      deleted — 删除条数；
      deleted_by_rule — 按规则名计数，如 {"L8-short-response": 2}；
      kept_by_source_evidence — 因源侧证据（含汉字）而免删的条数；
      kept_by_noise_gate — 因无噪声证据而免删的条数（仅 L7/L8/L11）；
      kept_by_noise_gate_timings — 对应条目的时间轴串列表（报告消费）。
    """
    cleaner = ChineseCleaner(config_dir)
    items = parse_srt(srt_content)
    stats: dict = {"merged": 0, "deleted": 0,
                   "deleted_by_rule": {}, "kept_by_source_evidence": 0,
                   "kept_by_noise_gate": 0, "kept_by_noise_gate_timings": []}
    if not items:
        return srt_content, stats
    # 清理语法提示残留（S2 预处理注入的兜底）
    for item in items:
        item.text = clean_grammar_hint_residue(item.text)
    # 源侧证据：清洗前条目 timing → 源文行列表（缺失 → None → fail-safe）
    source_evidence: list[list[str] | None] | None = None
    if source_map:
        source_evidence = []
        for it in items:
            ev = source_map.get(_timing_key(it))
            if ev is None:
                source_evidence.append(None)
            elif isinstance(ev, str):
                source_evidence.append([ev])
            else:
                source_evidence.append(list(ev))
    filtered, stats = cleaner.filter(items, source_evidence)
    return format_srt(filtered), stats

