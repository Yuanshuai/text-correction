"""文本工具：归一化、可忽略字符判定、结果构建与套用。"""

import re
import string
import unicodedata
from typing import Any

from pydantic import BaseModel

from correction.models.types import CorrectionErrorItem


PUNCTUATION_CHARS = (
    string.punctuation
    + "．—－-__~·—/—‘:!+=,%^：，,＂“”''‘’？丨、~～@#≤%……&*《》<>〔〕「」{}【】（）…()\"°。；口■丶;"
)


def is_punctuation(char: str) -> bool:
    if not char:
        return False
    return char in PUNCTUATION_CHARS or (
        len(char) == 1 and unicodedata.category(char).startswith("P")
    )


def is_ignorable_diff_char(char: str) -> bool:
    """空格与标点不计入纠错结果（标点改动被自动忽略）。"""
    if not char:
        return False
    if char.isspace():
        return True
    return is_punctuation(char)


# ── 文本归一化 ──

def normalize_text(text: str) -> str:
    return re.sub(r"^\s+|\s+$", "", text or "")


def is_skippable_text(text: str) -> bool:
    return not text.strip()


# ── 超长文本切分与分片 ──

# 句末标点：切分超长文本时优先在这里断开，避免把句子拦腰切断（残句会让模型漏掉错字）。
SENTENCE_END = "。！？；!?;"


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """把文本切成句子的 [起, 止) 区间；不足 6 个字的不当句子（避免被"啊。"这类碎片切开）。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char in SENTENCE_END and index + 1 - start >= 6:
            spans.append((start, index + 1))
            start = index + 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def split_long_text(text: str, max_chars: int) -> list[str]:
    """把超长文本切成若干片段，保证 "".join(结果) == 原文。

    优先在句末断开；单句本身就超过 max_chars 时按上限硬切。
    片段可以直接当成独立单元送去纠错，它在原文里的偏移就是前面所有片段的长度之和。
    max_chars <= 0 或文本本身没超限时原样返回一个片段。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    current_start: int | None = None
    current_end = 0
    for start, end in _sentence_spans(text):
        # 单句超限：先收掉手上这段，再把这句按上限硬切
        while end - start > max_chars:
            if current_start is not None:
                pieces.append(text[current_start:current_end])
                current_start = None
            pieces.append(text[start:start + max_chars])
            start += max_chars
        if current_start is None:
            current_start, current_end = start, end
        elif end - current_start <= max_chars:
            current_end = end
        else:
            pieces.append(text[current_start:current_end])
            current_start, current_end = start, end
    if current_start is not None:
        pieces.append(text[current_start:current_end])
    return pieces


# ── 结果构建 ──

def empty_result(original_text: str) -> dict[str, Any]:
    return {
        "original_text": original_text,
        "correct_text": original_text,
        "wrong_ids": [],
        "errors": [],
    }


def _error_key(err: dict[str, Any]) -> tuple[int, str, str, str]:
    return (err["position"], err.get("original_char", ""), err.get("correct_char", ""), err.get("tag", ""))


def consolidate_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """归一成单字粒度、按 position 排序。

    单项可能带着多字（例如 original_char="款项"），这里逐字摊开：replace 多出来的部分拆成
    delete / insert，insert 的 correct_char 按顺序整串拆成单字。插入项一律不去重——同一
    位上出现两个相同的字就是真的要插两个字（"重"+"重"），并成一个会少插一个字。
    """
    if not errors:
        return []
    expanded: list[dict[str, Any]] = []
    for err in sorted(errors, key=lambda e: (e["position"], e.get("tag", ""))):
        tag = err.get("tag", "")
        orig = err.get("original_char") or ""
        corr = err.get("correct_char") or ""
        if tag == "insert":
            for char in corr:
                expanded.append({"position": err["position"], "original_char": "", "correct_char": char, "tag": "insert"})
        elif tag == "replace" and (len(orig) > 1 or len(corr) > 1):
            common = min(len(orig), len(corr)) if orig and corr else 0
            for i in range(common):
                if orig[i] != corr[i]:
                    expanded.append({"position": err["position"] + i, "original_char": orig[i], "correct_char": corr[i], "tag": "replace"})
            for i in range(common, len(orig)):
                expanded.append({"position": err["position"] + i, "original_char": orig[i], "correct_char": "", "tag": "delete"})
            for i in range(common, len(corr)):
                expanded.append({"position": err["position"] + common, "original_char": "", "correct_char": corr[i], "tag": "insert"})
        elif tag == "delete" and len(orig) > 1:
            for i, char in enumerate(orig):
                expanded.append({"position": err["position"] + i, "original_char": char, "correct_char": "", "tag": "delete"})
        else:
            expanded.append(err)

    deduped: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for err in expanded:
        if err.get("tag") == "insert":
            continue
        deduped[_error_key(err)] = err
    inserts = [err for err in expanded if err.get("tag") == "insert"]
    return sorted(inserts + list(deduped.values()), key=lambda e: e["position"])


def build_result_from_errors(original: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    """校验并套用单字纠错项，产出对外契约形状的结果。

    只接受能对上原文的项：position 越界、字符与原文不符、改动只涉及空格/标点的一律丢弃。
    """
    if not errors:
        return empty_result(original)

    validated: list[dict[str, Any]] = []
    for e in consolidate_errors(errors):
        pos = e.get("position")
        tag = e.get("tag", "")
        if not isinstance(pos, int) or tag not in ("replace", "insert", "delete"):
            continue
        if tag == "insert":
            if not (0 <= pos <= len(original)):
                continue
        elif not (0 <= pos < len(original)):
            continue

        orig_ch = e.get("original_char", "")
        corr_ch = e.get("correct_char", "")
        # 三项都必须"带着东西"：insert 要说明插入什么，replace / delete 要说明改哪个字。
        # 空操作的项不能进结果，否则 errors / wrong_ids 会报出一处其实没动过的位置。
        if tag == "insert" and not corr_ch:
            continue
        if tag != "insert" and not orig_ch:
            continue
        if tag == "insert" and is_ignorable_diff_char(corr_ch):
            continue
        if tag == "delete" and is_ignorable_diff_char(orig_ch):
            continue
        if tag == "replace" and (is_ignorable_diff_char(orig_ch) or is_ignorable_diff_char(corr_ch)):
            continue
        if tag == "replace" and orig_ch == corr_ch:
            continue

        if orig_ch and original[pos] != orig_ch:
            # original_char 与原文对不上，说明这一项指的不是它说的那个字：一律丢弃。
            # 这里不能"以原文为准"顺手把那个位置的字改成 correct_char——那正是最危险的错位修改。
            continue

        validated.append({"position": pos, "original_char": orig_ch, "correct_char": corr_ch, "tag": tag})

    if not validated:
        return empty_result(original)
    items = [
        CorrectionErrorItem(
            position=e["position"],
            original_char=e["original_char"],
            correct_char=e["correct_char"],
            tag=e["tag"],
        )
        for e in validated
    ]
    corrected = apply_llm_errors(original, items)
    return {
        "original_text": original,
        "correct_text": corrected,
        "wrong_ids": sorted({e["position"] for e in validated}),
        "errors": validated,
    }


# ── 套用修改 ──

def apply_llm_errors(original: str, errors: list[CorrectionErrorItem]) -> str:
    """对 CorrectionErrorItem 列表调用统一 apply_errors_to_text。"""
    if not errors:
        return original
    return apply_errors_to_text(original, [e.model_dump() for e in errors])


def apply_errors_to_text(original: str, errors: list[dict[str, Any]]) -> str:
    """按 position 升序重放纠错项，返回修正后的文本。

    这是整个项目中唯一的 apply_errors 实现，所有模块都通过此函数应用纠错。
    errors 应为 dict 列表，每项包含 position、original_char、correct_char、tag，且是单字
    粒度；同一个 position 上可能有多条，含义按下面三条固定，不依赖除了列表顺序以外的任何东西：

    1. insert 把 correct_char 插在该位置"原来那个字符"的前面，同位多条按列表顺序首尾相接
       （["增","值","税"] 得到 "增值税"，不是 "税值增"）；position 允许等于文本长度，表示追加到末尾。
    2. replace / delete 作用在原文的这一个字符上；同位最多生效一条，replace 优先于 delete。
    3. 只有 original_char 与原文该位置逐字相符的 replace / delete 才生效，对不上的直接丢弃——
       宁可少改一处，也不能把别的字改成 correct_char。

    升序重放而不是在可变缓冲上原地倒序改：插进来的字不会被后面的操作当成原文，同位多条的顺序
    也才有确定含义（原地倒序时，每条 insert 都插在同一个下标上，多条会被倒着摞起来）。
    """
    inserts: dict[int, list[str]] = {}
    consuming: dict[int, tuple[str, str, str]] = {}
    for err in errors:
        tag = err.get("tag")
        position = err.get("position")
        if not isinstance(position, int) or position < 0 or position > len(original):
            continue
        if tag == "insert":
            correct = err.get("correct_char") or ""
            if correct:
                inserts.setdefault(position, []).append(correct)
            continue
        if tag not in ("replace", "delete"):
            continue
        original_char = err.get("original_char") or ""
        if not original_char or position >= len(original):
            continue
        if original[position:position + len(original_char)] != original_char:
            continue
        if position not in consuming or tag == "replace":
            consuming[position] = (tag, original_char, err.get("correct_char") or "")

    parts: list[str] = []
    cursor = 0
    for position in sorted(set(inserts) | set(consuming)):
        parts.append(original[cursor:position])
        parts.extend(inserts.get(position, ()))
        cursor = max(cursor, position)
        if position in consuming:
            tag, original_char, correct = consuming[position]
            cursor = max(cursor, position + len(original_char))
            if tag == "replace":
                parts.append(correct)
    parts.append(original[cursor:])
    return "".join(parts)


# ── 共享工具函数 ──

def parse_structured_output(raw: Any, model_class: type[BaseModel]) -> Any | None:
    """解析 LLM 结构化输出，兼容 pydantic 模型、dict(含 parsed 键) 等格式。"""
    if isinstance(raw, model_class):
        return raw
    if isinstance(raw, dict):
        if "parsed" in raw and isinstance(raw["parsed"], model_class):
            return raw["parsed"]
        try:
            return model_class.model_validate(raw)
        except Exception:
            return None
    return None
