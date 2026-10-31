#!/usr/bin/env python3
"""大模型并发压力测试：探测当前模型可稳定处理的最大并发数。

两阶段：
1. 粗探：依次压测 1 / 10 / 100 / 1000 / 10000，确定「通过 / 失败」区间；
2. 二分：在区间内逼近，得到大概的并发上限。

某个并发级别的判定：同时发起 N 个请求，成功率 >= --pass-rate 记为通过。
若最高档仍通过，会自动翻倍上探直到失败或触达 --hard-cap。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from correction.config import BASE_URL, LLM_TIMEOUT, LLM_TLS_VERIFY, MODEL_NAME
from correction.model import create_chat_model

DEFAULT_PROBES = "1,10,100,1000,10000"
DEFAULT_PROMPT = "请只回复两个字：收到"


@dataclass
class Failure:
    kind: str
    detail: str


@dataclass
class LevelResult:
    concurrency: int
    total: int
    ok: int = 0
    elapsed_ms: int = 0
    failures: list[Failure] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.ok / self.total if self.total else 0.0

    def passed(self, pass_rate: float) -> bool:
        return self.success_rate >= pass_rate


def _classify(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "timeout"
    for code in ("429", "503", "502", "500", "401", "403", "404"):
        if code in low:
            return f"http_{code}"
    if "connection" in low or "connect" in low or "reset" in low:
        return "connection"
    return type(exc).__name__


def _content_of(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        content = "".join(parts)
    return content or ""


async def run_level(concurrency: int, prompt: str, timeout: float, pass_rate: float) -> LevelResult:
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    client = httpx.AsyncClient(verify=LLM_TLS_VERIFY, timeout=timeout, limits=limits)
    model = create_chat_model(http_async_client=client, max_retries=0, timeout=timeout)
    result = LevelResult(concurrency=concurrency, total=concurrency)
    started = time.perf_counter()

    async def one() -> None:
        try:
            message = await model.ainvoke([HumanMessage(content=prompt)])
            if _content_of(message).strip():
                result.ok += 1
            else:
                result.failures.append(Failure("empty", "模型返回空内容"))
        except Exception as exc:
            result.failures.append(Failure(_classify(exc), str(exc)[:300]))

    try:
        await asyncio.gather(*(one() for _ in range(concurrency)))
    finally:
        await client.aclose()

    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


def print_level(result: LevelResult, pass_rate: float) -> None:
    status = "PASS" if result.passed(pass_rate) else "FAIL"
    kinds: dict[str, int] = {}
    for f in result.failures:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    detail = " ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
    print(
        f"  并发 {result.concurrency:>6}  {status:<4}  "
        f"成功 {result.ok}/{result.total} ({result.success_rate:.1%})  "
        f"{result.elapsed_ms}ms  {detail}",
        flush=True,
    )


async def probe(concurrency: int, args: argparse.Namespace) -> LevelResult:
    print(f"[阶段1/粗探] 并发 {concurrency} ...", flush=True)
    result = await run_level(concurrency, args.prompt, args.timeout, args.pass_rate)
    print_level(result, args.pass_rate)
    return result


async def find_bracket(
    probes: list[int], args: argparse.Namespace
) -> tuple[LevelResult | None, LevelResult | None, list[LevelResult]]:
    """返回 (最大通过结果, 最小失败结果, 全部结果)。"""
    results: list[LevelResult] = []
    for n in probes:
        results.append(await probe(n, args))

    passing = [r for r in results if r.passed(args.pass_rate)]
    failing = [r for r in results if not r.passed(args.pass_rate)]

    best_pass = max(passing, key=lambda r: r.concurrency) if passing else None
    worst_fail = min(failing, key=lambda r: r.concurrency) if failing else None

    while worst_fail is None and best_pass is not None and best_pass.concurrency < args.hard_cap:
        n = min(best_pass.concurrency * 2, args.hard_cap)
        print(f"[阶段1/上探] 最高档仍通过，翻倍测试并发 {n} ...", flush=True)
        results.append(await run_level(n, args.prompt, args.timeout, args.pass_rate))
        print_level(results[-1], args.pass_rate)
        if results[-1].passed(args.pass_rate):
            best_pass = results[-1]
        else:
            worst_fail = results[-1]

    return best_pass, worst_fail, results


async def binary_search(
    best_pass: LevelResult | None,
    worst_fail: LevelResult | None,
    args: argparse.Namespace,
) -> tuple[int, int, list[LevelResult]]:
    lo = best_pass.concurrency if best_pass else 0
    hi = worst_fail.concurrency if worst_fail else lo
    results: list[LevelResult] = []

    if best_pass is None:
        return lo, hi, results
    if worst_fail is None:
        print(f"[阶段2/二分] 未找到失败点，实际并发上限 >= {lo}", flush=True)
        return lo, hi, results

    print(f"[阶段2/二分] 在 ({lo}, {hi}) 内逼近，容差 {args.tolerance} ...", flush=True)
    while hi - lo > args.tolerance:
        mid = (lo + hi) // 2
        if mid <= lo or mid >= hi:
            break
        print(f"[阶段2/二分] 测试并发 {mid} ...", flush=True)
        result = await run_level(mid, args.prompt, args.timeout, args.pass_rate)
        print_level(result, args.pass_rate)
        results.append(result)
        if result.passed(args.pass_rate):
            lo = mid
        else:
            hi = mid

    return lo, hi, results


def _level_payload(result: LevelResult, pass_rate: float) -> dict:
    data = asdict(result)
    data["success_rate"] = round(result.success_rate, 4)
    data["passed"] = result.passed(pass_rate)
    return data


def save_report(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="大模型并发上限压力测试（粗探 + 二分）")
    parser.add_argument("--probes", default=DEFAULT_PROBES,
                        help=f"粗探并发档位，逗号分隔（默认 {DEFAULT_PROBES}）")
    parser.add_argument("--pass-rate", type=float, default=0.99,
                        help="判定通过的请求成功率阈值（默认 0.99）")
    parser.add_argument("--tolerance", type=int, default=1,
                        help="二分终止容差，区间宽度 <= 该值即停止（默认 1）")
    parser.add_argument("--timeout", type=float, default=float(LLM_TIMEOUT),
                        help=f"单个请求超时秒数（默认 {LLM_TIMEOUT}，来自 LLM_TIMEOUT）")
    parser.add_argument("--hard-cap", type=int, default=20000,
                        help="上探的并发硬上限（默认 20000）")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="压测使用的提示词（默认极短提示以降低单次耗时）")
    parser.add_argument("--report", default="",
                        help="报告输出路径（默认 tests/reports/stress_concurrency_<时间>.json）")
    parser.add_argument("--yes", action="store_true",
                        help="跳过高并发风险确认，直接开始")
    return parser.parse_args(argv)


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    probes = sorted({int(x) for x in args.probes.split(",") if x.strip()})
    if not probes:
        print("错误: --probes 不能为空", file=sys.stderr)
        return 2

    print(f"目标模型: {MODEL_NAME}")
    print(f"网关地址: {BASE_URL}")
    print(f"粗探档位: {probes} | 通过阈值: {args.pass_rate:.0%} | 单请求超时: {args.timeout:.0f}s")
    if max(probes) >= 1000 and not args.yes:
        print("警告: 即将对模型发起上千并发请求，可能造成持续压力。使用 --yes 可跳过此确认。")
        reply = input("确认继续? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("已取消")
            return 1

    started = time.perf_counter()
    best_pass, worst_fail, probe_results = await find_bracket(probes, args)

    if best_pass is None:
        lo, hi = 0, (worst_fail.concurrency if worst_fail else 0)
    elif worst_fail is None:
        lo, hi = best_pass.concurrency, best_pass.concurrency
    else:
        lo, hi, bs_results = await binary_search(best_pass, worst_fail, args)
        probe_results.extend(bs_results)

    total_ms = int((time.perf_counter() - started) * 1000)
    print("\n==== 结果 ====")
    if worst_fail is None:
        print(f"并发上限 >= {lo}（未触发失败，可提高 --hard-cap 继续上探）")
    elif best_pass is None:
        print(f"并发上限 < {hi}（最低档 {hi} 已失败，请检查模型/网关是否可用）")
    else:
        print(f"并发上限约在 [{lo}, {hi}]（通过最大 {lo}，失败最小 {hi}）")

    report_path = Path(args.report) if args.report else (
        Path(__file__).resolve().parent.parent / "tests" / "reports"
        / f"stress_concurrency_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    payload = {
        "model": MODEL_NAME,
        "base_url": BASE_URL,
        "pass_rate": args.pass_rate,
        "tolerance": args.tolerance,
        "timeout": args.timeout,
        "prompt": args.prompt,
        "probes": probes,
        "total_elapsed_ms": total_ms,
        "capacity_lower": lo,
        "capacity_upper": hi,
        "levels": [_level_payload(r, args.pass_rate) for r in probe_results],
    }
    save_report(payload, report_path)
    print(f"报告: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(sys.argv[1:])))
    except KeyboardInterrupt:
        print("\n已中断")
        raise SystemExit(130)
