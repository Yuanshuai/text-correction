#!/usr/bin/env python3
"""按正则切分合同段落并并行调用纠错接口。"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:32546"
CONCURRENCY = 10


def split_contract_paragraphs(text: str) -> list[str]:
    lines = text.splitlines()
    paragraphs: list[str] = []
    current: list[str] = []

    new_para_patterns = [
        r"^甲\s+方：",
        r"^乙\s+方：",
        r"^第[一二三四五六七八九十百千万]+条\b",
        r"^[一二三四五六七八九十]+、",
        r"^\d+\.\d+(?:\.\d+)?\b",
        r"^（\d+）",
        r"^【",
        r"^甲方（盖章）",
        r"^乙方（盖章）",
        r"^鉴于",
        r"^本合同由",
        r"^如本合同价款为暂定价款",
        r"^分期\t",
    ]
    new_para_re = re.compile("|".join(f"(?:{p})" for p in new_para_patterns))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append("\n".join(current).strip())
                current = []
            continue
        if new_para_re.match(stripped) and current:
            paragraphs.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)

    if current:
        paragraphs.append("\n".join(current).strip())
    return [p for p in paragraphs if p]


async def correct_paragraph(
    client: httpx.AsyncClient,
    index: int,
    text: str,
) -> dict:
    started = time.perf_counter()
    try:
        resp = await client.post("/correction_text", json={"content": text})
        elapsed = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            return {
                "index": index,
                "status": "error",
                "message": f"HTTP {resp.status_code}",
                "elapsed_ms": elapsed,
                "preview": text.splitlines()[0][:80],
            }
        payload = resp.json()
        result = payload.get("results") or (payload.get("data") or {}).get("results")
        if not isinstance(result, dict):
            return {
                "index": index,
                "status": "error",
                "message": "缺少 results",
                "elapsed_ms": elapsed,
                "preview": text.splitlines()[0][:80],
            }
        errors = result.get("errors") or []
        return {
            "index": index,
            "status": "ok",
            "elapsed_ms": elapsed,
            "preview": text.splitlines()[0][:80],
            "char_len": len(text),
            "error_count": len(errors),
            "original_text": result.get("original_text", text),
            "correct_text": result.get("correct_text", text),
            "errors": errors,
            "wrong_ids": result.get("wrong_ids") or [],
        }
    except Exception as exc:
        return {
            "index": index,
            "status": "error",
            "message": str(exc),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "preview": text.splitlines()[0][:80],
        }


async def main(input_path: Path, output_path: Path) -> int:
    text = input_path.read_text(encoding="utf-8")
    paragraphs = split_contract_paragraphs(text)
    print(f"段落数: {len(paragraphs)} | 并行: {CONCURRENCY} | 服务: {BASE_URL}")

    sem = asyncio.Semaphore(CONCURRENCY)
    started = time.perf_counter()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=300.0) as client:
        health = await client.get("/health")
        if health.status_code != 200:
            print("服务不可用", file=sys.stderr)
            return 1

        async def bounded(i: int, para: str) -> dict:
            async with sem:
                return await correct_paragraph(client, i, para)

        results = await asyncio.gather(
            *(bounded(i, p) for i, p in enumerate(paragraphs, 1))
        )

    total_ms = int((time.perf_counter() - started) * 1000)
    with_errors = [r for r in results if r.get("error_count", 0) > 0]
    failed = [r for r in results if r.get("status") != "ok"]

    report = {
        "source": str(input_path),
        "paragraph_count": len(paragraphs),
        "concurrency": CONCURRENCY,
        "total_elapsed_ms": total_ms,
        "paragraphs_with_errors": len(with_errors),
        "failed_requests": len(failed),
        "results": sorted(results, key=lambda x: x["index"]),
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成: {len(with_errors)} 段有修改 | {len(failed)} 段请求失败 | 耗时 {total_ms/1000:.1f}s")
    print(f"报告: {output_path}")
    return 0


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "test.txt"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "tests/reports/contract_correction.json"
    raise SystemExit(asyncio.run(main(inp, out)))
