#!/usr/bin/env python3
"""并行运行纠错测试用例并生成报告。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# 确保能导入项目代码（tests/ 的父目录）
_sys_root = Path(__file__).resolve().parent.parent
if str(_sys_root) not in sys.path:
    sys.path.insert(0, str(_sys_root))

from correction.utils.text import apply_errors_to_text

from cases import TEST_CASES
from contract_cases import CONTRACT_CASES

BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:32546")
REPORT_DIR = Path(__file__).parent / "reports"
DEFAULT_CONCURRENCY = int(os.environ.get("TEST_CONCURRENCY", "10"))
MIN_CONCURRENCY = 2  # 禁止串行，所有测试必须并行执行

SUITES: dict[str, list[dict[str, Any]]] = {
    "general": TEST_CASES,
    "contract": CONTRACT_CASES,
    "all": TEST_CASES + CONTRACT_CASES,
}


@dataclass
class CaseResult:
    case_id: int
    category: str
    expect: str
    status: str
    message: str
    content_preview: str
    response: dict[str, Any] | None = None
    elapsed_ms: int = 0


@dataclass
class Report:
    total: int = 0
    passed: int = 0
    failed: int = 0
    warned: int = 0
    results: list[CaseResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    total_elapsed_ms: int = 0
    concurrency: int = DEFAULT_CONCURRENCY


def preview(text: str, limit: int = 50) -> str:
    compact = text.replace("\n", "\\n").replace("\t", "\\t")
    suffix = f" [{len(text)}字]"
    if len(compact) <= limit:
        return compact + suffix
    return compact[:limit] + "…" + suffix


def _normalize_request_text(text: str) -> str:
    """与服务端 normalize_text 一致：仅裁剪首尾空白。"""
    return re.sub(r"^\s+|\s+$", "", text or "")


def validate_error_positions(
    original: str,
    errors: list[dict[str, Any]],
    wrong_ids: list[Any],
    correct_text: str,
) -> tuple[bool, str]:
    """逐条校验 errors 的 position / original_char，并验证套用后等于 correct_text。"""
    for i, err in enumerate(errors):
        pos = err["position"]
        tag = err["tag"]
        orig = err.get("original_char") or ""
        corr = err.get("correct_char") or ""

        if tag == "insert":
            if not (0 <= pos <= len(original)):
                return False, f"errors[{i}] insert position={pos} 越界(len={len(original)})"
            if orig:
                return False, f"errors[{i}] insert 的 original_char 应为空，实际={orig!r}"
            if not corr:
                return False, f"errors[{i}] insert 的 correct_char 为空"
            if len(corr) != 1:
                return False, f"errors[{i}] insert correct_char 非单字: {corr!r}"
            continue

        # replace / delete
        if not (0 <= pos < len(original)):
            return False, f"errors[{i}] {tag} position={pos} 越界(len={len(original)})"
        if not orig:
            return False, f"errors[{i}] {tag} 的 original_char 为空"
        if len(orig) != 1:
            return False, f"errors[{i}] {tag} original_char 非单字: {orig!r}"
        actual = original[pos]
        if actual != orig:
            ctx = original[max(0, pos - 3): pos + 4]
            return False, (
                f"errors[{i}] position={pos} 错位: "
                f"original_char={orig!r} 但原文[{pos}]={actual!r} "
                f"上下文={ctx!r}"
            )
        if tag == "replace":
            if not corr:
                return False, f"errors[{i}] replace 的 correct_char 为空"
            if len(corr) != 1:
                return False, f"errors[{i}] replace correct_char 非单字: {corr!r}"
            if corr == orig:
                return False, f"errors[{i}] replace 无变化: {orig!r}->{corr!r}"
        elif tag == "delete":
            if corr:
                return False, f"errors[{i}] delete 的 correct_char 应为空，实际={corr!r}"

    # wrong_ids 与 errors.position 一一对应
    error_positions = sorted({e["position"] for e in errors})
    wrong_sorted = sorted({p for p in wrong_ids if isinstance(p, int)})
    if wrong_sorted != error_positions:
        return False, (
            f"wrong_ids 与 errors.position 不一致: "
            f"wrong_ids={wrong_sorted} errors={error_positions}"
        )

    # 套用 errors 后必须等于 correct_text
    rebuilt = apply_errors_to_text(original, errors)
    if rebuilt != correct_text:
        # 找首个差异便于定位
        limit = min(len(rebuilt), len(correct_text))
        diff_at = next((j for j in range(limit) if rebuilt[j] != correct_text[j]), limit)
        return False, (
            f"按 errors 重建的文本与 correct_text 不一致 "
            f"(重建len={len(rebuilt)} correctlen={len(correct_text)} "
            f"首差@{diff_at} 重建={rebuilt[max(0,diff_at-2):diff_at+3]!r} "
            f"correct={correct_text[max(0,diff_at-2):diff_at+3]!r})"
        )

    return True, f"position 校验通过({len(errors)}处)"


def validate_structure(payload: dict[str, Any], request_content: str | None = None) -> tuple[bool, str]:
    # NestJS 兼容：顶层 results；同时兼容旧 data.results
    results = payload.get("results")
    if not isinstance(results, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            results = data.get("results")
    if not isinstance(results, dict):
        return False, "缺少 results（顶层或 data.results）"
    for key in ("original_text", "correct_text", "wrong_ids", "errors"):
        if key not in results:
            return False, f"缺少字段 {key}"
    if not isinstance(results["errors"], list):
        return False, "errors 应为数组"
    if not isinstance(results["wrong_ids"], list):
        return False, "wrong_ids 应为数组"

    original = results["original_text"]
    if not isinstance(original, str) or not isinstance(results["correct_text"], str):
        return False, "original_text/correct_text 应为字符串"

    if request_content is not None:
        expected_original = _normalize_request_text(request_content)
        if original != expected_original:
            return False, (
                f"original_text 与请求 content 不一致 "
                f"(请求len={len(expected_original)} 返回len={len(original)})"
            )

    for i, err in enumerate(results["errors"]):
        if not isinstance(err, dict):
            return False, f"errors[{i}] 格式错误"
        for key in ("position", "original_char", "correct_char", "tag"):
            if key not in err:
                return False, f"errors[{i}] 缺少 {key}"
        pos = err["position"]
        if not isinstance(pos, int) or pos < 0 or pos > len(original):
            return False, f"errors[{i}] position={pos} 越界(len={len(original)})"
        if err["tag"] not in ("replace", "insert", "delete"):
            return False, f"errors[{i}] 非法 tag={err['tag']}"
        # NestJS：单字粒度
        if err["tag"] in ("replace", "delete") and len(err.get("original_char") or "") > 1:
            return False, f"errors[{i}] original_char 非单字: {err.get('original_char')!r}"
        if err["tag"] in ("replace", "insert") and len(err.get("correct_char") or "") > 1:
            return False, f"errors[{i}] correct_char 非单字: {err.get('correct_char')!r}"

    for j, pos in enumerate(results["wrong_ids"]):
        if not isinstance(pos, int) or pos < 0 or pos > len(original):
            return False, f"wrong_ids[{j}] 越界 position={pos}"

    # 逐条校验 position 与原文对齐，并验证 correct_text 可由 errors 重建
    ok, msg = validate_error_positions(
        original,
        results["errors"],
        results["wrong_ids"],
        results["correct_text"],
    )
    if not ok:
        return False, msg

    return True, msg if results["errors"] else "结构合法"


def validate_correct_text_sanity(results: dict[str, Any]) -> tuple[bool, str]:
    """检测 correct_text 异常：JSON 泄漏、空串、长度离谱。"""
    original = results["original_text"]
    corrected = results["correct_text"]
    errors = results["errors"]

    if corrected == "" and original:
        return False, "correct_text 为空但原文非空（会导致全文误删）"

    if (
        '"correct_text"' in corrected
        and (
            corrected.strip().startswith("{")
            or "```json" in corrected.lower()
            or "**纠错" in corrected
        )
    ):
        return False, "correct_text 疑似 JSON 泄漏"

    if len(original) > 0 and len(errors) > len(original) * 1.5:
        return False, f"error_count={len(errors)} 远超原文长度 {len(original)}，疑似假阳性"

    return True, "correct_text 正常"


def evaluate(
    expect: str,
    payload: dict[str, Any],
    min_errors: int = 0,
    request_content: str | None = None,
) -> tuple[str, str]:
    if expect == "http_422":
        return "fail", "不应返回 200"

    ok, msg = validate_structure(payload, request_content=request_content)
    if not ok:
        return "fail", msg

    results = payload.get("results")
    if not isinstance(results, dict):
        results = payload.get("data", {}).get("results")
    sane, sane_msg = validate_correct_text_sanity(results)
    if not sane:
        return "fail", sane_msg

    errors = results["errors"]
    original = results["original_text"]
    corrected = results["correct_text"]
    error_count = len(errors)

    if expect == "skip":
        if errors:
            return "warn", f"跳过了但仍返回 {error_count} 个错误"
        if original != corrected:
            return "warn", "跳过文本被修改"
        return "pass", "正确跳过"

    if expect == "no_error":
        if errors:
            return "warn", f"无错句被检出 {error_count} 处"
        return "pass", "未检出错误"

    if expect == "has_error":
        if not errors:
            return "warn", "有错句未检出错误"
        if min_errors and error_count < min_errors:
            return "warn", f"检出 {error_count} 处，期望至少 {min_errors} 处"
        return "pass", f"检出 {error_count} 处错误(position已校验)"

    return "pass", f"结构合法，{error_count} 处错误(position已校验)"


async def run_case(client: httpx.AsyncClient, case: dict[str, Any]) -> CaseResult:
    started = time.perf_counter()
    content = case["content"]
    expect = case["expect"]

    try:
        if expect == "http_422":
            resp = await client.post("/correction_text", json={"content": content})
            elapsed = int((time.perf_counter() - started) * 1000)
            if resp.status_code == 422:
                return CaseResult(
                    case_id=case["id"],
                    category=case["category"],
                    expect=expect,
                    status="pass",
                    message="正确返回 422",
                    content_preview=preview(content),
                    elapsed_ms=elapsed,
                )
            return CaseResult(
                case_id=case["id"],
                category=case["category"],
                expect=expect,
                status="fail",
                message=f"期望 422，实际 {resp.status_code}",
                content_preview=preview(content),
                response=resp.json() if "application/json" in resp.headers.get("content-type", "") else None,
                elapsed_ms=elapsed,
            )

        resp = await client.post("/correction_text", json={"content": content})
        elapsed = int((time.perf_counter() - started) * 1000)

        if resp.status_code != 200:
            return CaseResult(
                case_id=case["id"],
                category=case["category"],
                expect=expect,
                status="fail",
                message=f"HTTP {resp.status_code}",
                content_preview=preview(content),
                elapsed_ms=elapsed,
            )

        payload = resp.json()
        status, message = evaluate(
            expect,
            payload,
            case.get("min_errors", 0),
            request_content=content,
        )
        return CaseResult(
            case_id=case["id"],
            category=case["category"],
            expect=expect,
            status=status,
            message=message,
            content_preview=preview(content),
            response=payload,
            elapsed_ms=elapsed,
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return CaseResult(
            case_id=case["id"],
            category=case["category"],
            expect=expect,
            status="fail",
            message=f"异常: {exc}",
            content_preview=preview(content),
            elapsed_ms=elapsed,
        )


async def run_all(cases: list[dict[str, Any]], concurrency: int) -> Report:
    report = Report(
        total=len(cases),
        started_at=datetime.now().isoformat(timespec="seconds"),
        concurrency=concurrency,
    )
    started = time.perf_counter()
    sem = asyncio.Semaphore(concurrency)
    completed = 0
    lock = asyncio.Lock()

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=300.0, limits=limits) as client:
        health = await client.get("/health")
        if health.status_code != 200:
            print(f"服务不可用: {BASE_URL}", file=sys.stderr)
            sys.exit(1)

        async def bounded(case: dict[str, Any]) -> CaseResult:
            nonlocal completed
            async with sem:
                result = await run_case(client, case)
            async with lock:
                completed += 1
                icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}.get(result.status, "?")
                print(
                    f"[{completed:03d}/{len(cases)}] {icon} #{result.case_id:03d} "
                    f"{result.category:<18} {result.status:<4} "
                    f"{result.elapsed_ms:>5}ms  {result.message}",
                    flush=True,
                )
            return result

        results = await asyncio.gather(*(bounded(case) for case in cases))

    report.results = sorted(results, key=lambda r: r.case_id)
    for result in report.results:
        if result.status == "pass":
            report.passed += 1
        elif result.status == "fail":
            report.failed += 1
        else:
            report.warned += 1

    report.finished_at = datetime.now().isoformat(timespec="seconds")
    report.total_elapsed_ms = int((time.perf_counter() - started) * 1000)
    return report


def save_report(report: Report, cases: list[dict[str, Any]], suite: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"run_{suite}_{ts}.json"
    case_by_id = {c["id"]: c for c in cases}

    payload = {
        "summary": {
            "suite": suite,
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "warned": report.warned,
            "pass_rate": round(report.passed / report.total * 100, 1) if report.total else 0,
            "concurrency": report.concurrency,
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "total_elapsed_ms": report.total_elapsed_ms,
        },
        "results": [
            {
                "id": r.case_id,
                "category": r.category,
                "expect": r.expect,
                "status": r.status,
                "message": r.message,
                "content_preview": r.content_preview,
                "elapsed_ms": r.elapsed_ms,
                "error_count": (
                    len((r.response.get("results") or (r.response.get("data") or {}).get("results") or {}).get("errors") or [])
                    if r.response else None
                ),
                "tags": case_by_id.get(r.case_id, {}).get("tags", []),
                "response": r.response,
            }
            for r in report.results
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_latest_log(lines: list[str]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "latest_run.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="并行运行文本纠错测试（不支持串行，并发数至少为 2）",
    )
    parser.add_argument(
        "-s", "--suite",
        choices=sorted(SUITES.keys()),
        default="general",
        help="测试集：general(100通用) / contract(100合同) / all(200全部)",
    )
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"并行并发数（默认 {DEFAULT_CONCURRENCY}，至少 {MIN_CONCURRENCY}）",
    )
    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=0,
        help="限制运行用例数量（0 表示全部）",
    )
    args = parser.parse_args()

    if args.concurrency < MIN_CONCURRENCY:
        print(
            f"错误: 并发数 {args.concurrency} 过小，测试必须并行执行（至少 {MIN_CONCURRENCY}）",
            file=sys.stderr,
        )
        return 2

    cases = SUITES[args.suite]
    if args.limit > 0:
        cases = cases[:args.limit]

    def safe_print(*a, **kw):
        """绕过 Windows GBK 控制台编码问题。"""
        try:
            print(*a, **kw)
        except UnicodeEncodeError:
            kw.pop("file", None)
            try:
                print(*(str(x).encode("ascii", errors="replace").decode("ascii") for x in a), **kw)
            except Exception:
                pass

    print(f"目标服务: {BASE_URL}")
    print(f"测试集: {args.suite}")
    print(f"用例数量: {len(cases)}")
    print(f"并行并发: {args.concurrency}")
    print("-" * 72)

    log_lines = [
        f"目标服务: {BASE_URL}",
        f"测试集: {args.suite}",
        f"用例数量: {len(cases)}",
        f"并行并发: {args.concurrency}",
        "-" * 72,
    ]

    report = asyncio.run(run_all(cases, args.concurrency))
    report_path = save_report(report, cases, args.suite)

    for i, r in enumerate(report.results, 1):
        icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}.get(r.status, "?")
        log_lines.append(
            f"[{i:03d}/{len(cases)}] {icon} #{r.case_id:03d} "
            f"{r.category:<18} {r.status:<4} "
            f"{r.elapsed_ms:>5}ms  {r.message}"
        )

    summary = (
        f"完成: 通过 {report.passed} | 警告 {report.warned} | 失败 {report.failed} "
        f"| 通过率 {report.passed / report.total * 100:.1f}% "
        f"| 总耗时 {report.total_elapsed_ms / 1000:.1f}s "
        f"| 并行 {report.concurrency}"
    )
    log_lines.extend([summary, f"报告: {report_path}"])

    safe_print("-" * 72)
    safe_print(summary)
    safe_print(f"报告: {report_path}")

    if report.failed > 0:
        safe_print("\n失败用例:")
        log_lines.append("")
        log_lines.append("失败用例:")
        for r in report.results:
            if r.status == "fail":
                line = f"  #{r.case_id:03d} [{r.category}] {r.message} | {r.content_preview}"
                safe_print(line)
                log_lines.append(line)

    if report.warned > 0:
        safe_print("\n警告用例 (LLM 判断偏差):")
        log_lines.append("")
        log_lines.append("警告用例 (LLM 判断偏差):")
        for r in report.results:
            if r.status == "warn":
                line = f"  #{r.case_id:03d} [{r.category}] {r.message} | {r.content_preview}"
                safe_print(line)
                log_lines.append(line)

    write_latest_log(log_lines)

    return 1 if report.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
