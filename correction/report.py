"""把一次纠错的结果折成一条「纠错记录」（结果页要用的整份数据）。

记录本来是浏览器折好再存的（frontend/src/results.js）：纠错完把结果按 units / blocks /
highlights 三块折出来，POST 给 /records。对外接口 /openapi/contract-audit/web-correct
那边没有浏览器，所以服务端自己折一份——两种入口存出来的记录形状一致，结果页渲染也就一致。

「处数」（correctionCount）要跟前端同一个口径，否则记录列表里的数字会跟结果页页头对不上：
  · 有页面高亮框（highlights）时按框数算——PDF 那条路，结果页数的就是这些框；
  · 否则按「改动分组数」算：相邻的同类改动算一处（连着删几个字是一处、连着改几个字是一处、
    同一位置补几个字是一处）。规则见 merge_adjacent_errors，跟前端 frontend/src/diff.js
    的 mergeAdjacentErrors 逐条对应。
"""

from __future__ import annotations

from typing import Any

#: 文本纠错的记录名截前多少个字（跟前端一致）
SNIPPET_CHARS = 24

#: 记录里的耗时字段：纠错引擎回的是下划线，记录里跟前端统一用驼峰
_TIMING_FIELDS = (
    ("convertMs", "convert_ms"),
    ("parseMs", "parse_ms"),
    ("correctMs", "correct_ms"),
    ("totalMs", "total_ms"),
)


def engine_status(payload: dict[str, Any]) -> str:
    return "degraded" if payload.get("engine_status") == "degraded" else "ok"


def merge_adjacent_errors(errors: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把相邻的同类改动收成一处（前端 mergeAdjacentErrors 的服务端版本）。"""
    ordered = [
        err
        for err in sorted(
            (item for item in (errors or []) if isinstance(item, dict)),
            key=lambda item: (
                item["position"] if isinstance(item.get("position"), int) else -1,
                str(item.get("tag") or ""),
            ),
        )
        if isinstance(err.get("position"), int)
    ]
    merged: list[dict[str, Any]] = []
    for err in ordered:
        tag = err.get("tag")
        position = err["position"]
        if tag not in ("replace", "delete", "insert"):
            merged.append(dict(err))
            continue
        previous = merged[-1] if merged else None
        if previous is not None and previous.get("tag") == tag:
            if tag == "insert" and previous["position"] == position:
                previous["correct_char"] = (previous.get("correct_char") or "") + (err.get("correct_char") or "")
                continue
            source = previous.get("original_char") or ""
            if tag != "insert" and previous["position"] + len(source) == position:
                previous["original_char"] = source + (err.get("original_char") or "")
                previous["correct_char"] = (previous.get("correct_char") or "") + (err.get("correct_char") or "")
                continue
        merged.append(dict(err))
    return merged


def count_marks(text: str, errors: list[dict[str, Any]] | None) -> int:
    """一段原文上有几处改动（前端 buildUnitMarks 数出来的那个数）。

    对不上原文的改动不算——前端也是这么丢的（宁可不标，也不能把没写错的字标成错的）。
    """
    source = text or ""
    total = 0
    for err in merge_adjacent_errors(errors):
        at = err["position"]
        if at < 0 or at > len(source):
            continue
        tag = err.get("tag")
        if tag == "insert":
            if err.get("correct_char"):
                total += 1
            continue
        if tag not in ("replace", "delete"):
            continue
        original = err.get("original_char") or ""
        if not original or at >= len(source) or source[at:at + len(original)] != original:
            continue
        total += 1
    return total


def _unit(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "original_text": unit.get("original_text") or "",
        "correct_text": unit.get("correct_text") or "",
        "wrong_ids": unit.get("wrong_ids") or [],
        "errors": unit.get("errors") or [],
    }


def _units_with_marks(results: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], int]:
    """只留真正检出改动的单元，顺带数出总处数。"""
    units: list[dict[str, Any]] = []
    total = 0
    for unit in results or []:
        marks = count_marks(unit.get("original_text") or "", unit.get("errors"))
        if not marks:
            continue
        total += marks
        units.append(_unit(unit))
    return units, total


def _timings(payload: dict[str, Any], fallback_ms: int) -> dict[str, int] | None:
    raw = payload.get("timings")
    if isinstance(raw, dict):
        mapped = {
            name: int(raw[field])
            for name, field in _TIMING_FIELDS
            if isinstance(raw.get(field), (int, float))
        }
        if mapped:
            return mapped
    return {"correctMs": fallback_ms} if fallback_ms else None


def _snippet(text: str) -> str:
    flat = " ".join((text or "").split())
    return f"{flat[:SNIPPET_CHARS]}…" if len(flat) > SNIPPET_CHARS else flat


def text_record(payload: dict[str, Any], source: str, *, elapsed_ms: int = 0) -> dict[str, Any]:
    """一段文本纠错的记录：全文就是这段文本本身，直接拿它当分段。"""
    raw = payload.get("results")
    results = [raw] if isinstance(raw, dict) else list(raw or [])
    units, count = _units_with_marks(results)
    return {
        "kind": "text",
        "filename": f"[文本] {_snippet(source)}",
        "engineStatus": engine_status(payload),
        "units": units,
        "blocks": [
            {"type": "p", "text": unit["original_text"], "errors": unit["errors"]} for unit in units
        ],
        "doc": None,
        "highlights": None,
        "timings": {"correctMs": elapsed_ms} if elapsed_ms else None,
        "correctionCount": count,
        "unitsWithCorrection": len(units),
    }


def file_record(filename: str, payload: dict[str, Any], *, elapsed_ms: int = 0) -> dict[str, Any]:
    """文件纠错的记录：全文分段、页面高亮框和原文都来自 ?full=true 那份结果。"""
    units, marks = _units_with_marks(payload.get("results") or [])
    highlights = payload.get("highlights") or []
    doc_id = payload.get("doc_id")
    return {
        "kind": "file",
        "filename": filename,
        "engineStatus": engine_status(payload),
        "units": units,
        "blocks": payload.get("blocks") or None,
        "doc": {"docId": doc_id, "sourceKind": payload.get("source_kind") or "pdf"} if doc_id else None,
        "highlights": highlights or None,
        "timings": _timings(payload, elapsed_ms),
        # 处数跟结果页同一口径：有高亮框就数框（PDF 那条路），没有就数改动分组
        "correctionCount": len(highlights) if highlights else marks,
        "unitsWithCorrection": len(units),
    }