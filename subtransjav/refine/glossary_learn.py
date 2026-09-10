"""
自动词库学习：从 S2 翻译结果中提取术语对
===========================================
使用本地模型分析日中对照文本，识别专业术语/专有名词/一致性翻译，
自动追加到 learned 词库文件。
"""

import csv
import os
import logging
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 词库提取的 prompt
EXTRACT_PROMPT = """你是一个术语提取专家。分析以下日中字幕翻译对照，提取专业术语、专有名词、固定译法。

规则：
1. 只提取名词性术语（人名、地名、品牌、专业词汇等）
2. 不提取普通动词、形容词、日常用语
3. 每条格式：日文原文,中文译文
4. 最多提取 20 条最重要的术语
5. 如果没有值得记录的术语，输出空

输出格式（纯 CSV，无标题行）：
原文1,译文1
原文2,译文2

--- 日中对照文本 ---
{src_text}

--- 中文翻译 ---
{translated_text}"""


def extract_glossary_from_pair(src_text: str, translated_text: str,
                               endpoint: str = "http://localhost:1234/v1",
                               model: str = "") -> list:
    """从一对日中翻译文本中提取术语。

    返回 [(原文, 译文), ...] 列表。
    """
    if not src_text.strip() or not translated_text.strip():
        return []

    # 截取前 4000 字符避免超长
    src_preview = src_text[:4000]
    trans_preview = translated_text[:4000]

    prompt = EXTRACT_PROMPT.format(src_text=src_preview, translated_text=trans_preview)

    try:
        from openai import OpenAI
        client = OpenAI(base_url=endpoint, api_key="lm-studio", timeout=60)

        # 如果未指定模型，尝试获取已加载模型
        if not model:
            try:
                import requests
                r = requests.get(endpoint.replace("/v1", "") + "/v1/models", timeout=5)
                models = r.json().get("data", [])
                if models:
                    model = models[0].get("id", "")
            except Exception:
                pass

        if not model:
            logger.warning("无法确定本地模型，跳过词库提取")
            return []

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.1,
            stream=False
        )

        content = (response.choices[0].message.content or "").strip()
        return _parse_csv_pairs(content)

    except Exception as e:
        logger.warning(f"词库提取失败: {e}")
        return []


def _parse_csv_pairs(text: str) -> list:
    """解析 CSV 格式的术语对。"""
    pairs = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 支持逗号和制表符分隔
        for sep in (",", "\t", "，"):
            if sep in line:
                parts = line.split(sep, 1)
                if len(parts) == 2:
                    src, dst = parts[0].strip(), parts[1].strip()
                    if src and dst and src != dst:
                        pairs.append((src, dst))
                break
    return pairs[:20]  # 最多 20 条


def load_learned_glossary(path: str) -> list:
    """加载已学习的词库。"""
    entries = []
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f):
                    if len(row) >= 2 and row[0].strip() and row[1].strip():
                        entries.append((row[0].strip(), row[1].strip()))
        except Exception:
            pass
    return entries


def save_learned_glossary(path: str, entries: list):
    """保存已学习的词库（追加模式去重后全量写入；原子写入防崩溃损坏）。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    # 去重
    seen = set()
    unique = []
    for src, dst in entries:
        key = (src.lower() if src.isascii() else src, dst)
        if key not in seen:
            seen.add(key)
            unique.append((src, dst))

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".glossary", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            for src, dst in unique:
                w.writerow([src, dst])
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _acquire_glossary_lock(learned_path: str, timeout: float = 5.0,
                           stale_after: float = 60.0):
    """尝试获取词库写锁（跨进程）；返回锁文件路径，未获取返回 None。

    用于消除 load-modify-save 之间的 TOCTOU 竞态：并发 refine 同时加载
    同一 learned 词库再各自追加时，后写者会静默覆盖先写者的新增项。
    通过 .lock 文件互斥；进程崩溃残留的陈旧锁（超过 stale_after 秒）自动清除。
    """
    lock_path = learned_path + ".lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > stale_after:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.1)


def _release_glossary_lock(lock_path):
    if lock_path:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def learn_from_s2_output(s2_input_path: str, s2_output_path: str,
                         learned_path: str,
                         endpoint: str = "http://localhost:1234/v1",
                         model: str = "") -> int:
    """从 S2 输入/输出中学习术语，追加到 learned 词库。

    返回新追加的术语数量。
    """
    try:
        src_text = Path(s2_input_path).read_text(encoding="utf-8")
        trans_text = Path(s2_output_path).read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"读取 S2 输入/输出失败: {e}")
        return 0

    # 提取术语
    new_pairs = extract_glossary_from_pair(src_text, trans_text, endpoint, model)

    # 防污染校验：术语对必须真实存在于源文与译文中（防 LLM 幻觉造词入库）
    # 幻觉词一旦入库会随词库注入后续所有任务的提示词，长期扩散
    verified = [(s, d) for s, d in new_pairs
                if s in src_text and d in trans_text and s != d]
    dropped = len(new_pairs) - len(verified)
    if dropped:
        logger.warning(f"词库提取: 过滤 {dropped} 条未通过原文/译文核验的候选术语")
    new_pairs = verified
    if not new_pairs:
        return 0

    # 锁内完成 load → merge → save，消除并发丢更新竞态
    lock = _acquire_glossary_lock(learned_path)
    try:
        existing = load_learned_glossary(learned_path)
        existing_set = {(s.lower() if s.isascii() else s) for s, _ in existing}

        truly_new = [(s, d) for s, d in new_pairs
                     if (s.lower() if s.isascii() else s) not in existing_set]

        if truly_new:
            save_learned_glossary(learned_path, existing + truly_new)
            logger.info(f"词库学习：新增 {len(truly_new)} 条术语")
    finally:
        _release_glossary_lock(lock)

    return len(truly_new)
