"""FastAPI 入口：PDF / Word / 文本纠错，以及 DOCX 合同模板字段审查。

纠错只有一条路径（correction/readtool.py）：待校对文本就是模型眼里的 000.txt 文件，
模型用 read_text / find_text 自己决定看哪儿，改动写成 apply_patch 形态的小补丁
（引文定位 + 改前 + 改后），位置由服务端在 000.txt 里搜出来——模型一个位置都不用报。
000.txt 与模型改出来的 001.txt 逐字符比对，就是最终的逐字纠错结果。
"""

import asyncio
import ipaddress
import os
import re
import socket
import time
import uuid
from bisect import bisect_right
from urllib.parse import urlsplit

import httpx
import pymupdf
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from correction import objects, parse2json, records, report
from correction.config import (
    API_KEY, BASE_URL, ENABLE_TEST_MODE, ENABLE_WORD_CORRECTION, LLM_DISABLE_THINKING,
    MAX_CONCURRENCY, MAX_TEXT_LENGTH, MODEL_NAME, PUBLIC_BASE_URL, TOOL_MAX_TURNS,
)
from correction.convert import (
    convert_to_pdf, discard_workdir, document_path, is_office_file, new_workdir, remove_document,
    store_pdf, warm_convert_server,
)
from correction.debug_log import write_tool_debug
from correction.models.types import ContentModel, IgnoredModel, RecordModel
from correction.pdf import (
    filter_text_and_tables, is_correction_cell, shutdown_parse_pool, warm_parse_pool,
)
from correction.readtool import init_readtool_chain, run_document_correction
from correction.utils.log import (
    log_error, log_info, log_correction_stage, _request_id,
    log_warning, mark_engine_degraded, is_engine_degraded, reset_engine_degraded_flag,
)
from correction.utils.text import (
    build_result_from_errors, empty_result, is_skippable_text, normalize_text,
    split_long_text,
)
from correction.word import filter_text_and_tables_from_docx


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_readtool_chain()
    # 解析的进程池是第一次真解析才建的，建池要现开子进程、每个子进程再 import 一遍
    # pymupdf，实测约 0.85 秒——这笔开销本来会全算在第一份文档的「解析」耗时里。
    # 启动时先热上：放后台线程做，不拖慢启动，也不占启动那段路。
    asyncio.get_running_loop().run_in_executor(None, warm_parse_pool)
    # 转换服务（LibreOffice）也一样：闲一会儿它就把自己退了，第一份 Word 文档得多等约 0.25 秒。
    # 起个后台任务点它一下，不拖慢启动；转换服务没起也不影响什么（只记一条日志）。
    warmup_task = asyncio.create_task(warm_convert_server())
    # 纠错记录建表（幂等）：库连不上只记一条日志，纠错本身照常跑
    asyncio.get_running_loop().run_in_executor(None, records.init_store)
    # 原文对象存储：启动时确认桶在（幂等）。连不上只记日志，原文退回本机磁盘
    asyncio.get_running_loop().run_in_executor(None, objects.init_store)
    log_info(
        "service.start",
        model=MODEL_NAME,
        base_url=BASE_URL,
        llm_disable_thinking=LLM_DISABLE_THINKING,
        document_size=MAX_TEXT_LENGTH,
        max_turns=TOOL_MAX_TURNS,
        max_concurrency=MAX_CONCURRENCY,
        records="mysql" if records.available() else "local",
        objects=objects.describe(),
    )
    yield
    # 正常退出（重启、下线）时把解析进程池关掉，别把子进程留在那儿
    warmup_task.cancel()
    shutdown_parse_pool()
    records.shutdown_store()


app = FastAPI(title="Text Correction MVP", version="0.4.0", lifespan=lifespan)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = _request_id.set(rid)
    reset_engine_degraded_flag()  # 重置引擎降级标记
    started = time.perf_counter()
    log_info("http.start", method=request.method, path=request.url.path, client=request.client.host if request.client else "-")
    try:
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log_info("http.ok", method=request.method, path=request.url.path, status=response.status_code, elapsed_ms=elapsed_ms)
        response.headers["X-Request-ID"] = rid
        return response
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log_error("http.fail", exc=exc, method=request.method, path=request.url.path, elapsed_ms=elapsed_ms)
        raise
    finally:
        _request_id.reset(token)


# ── 阅读工具纠错（唯一引擎） ──

# 一份 000.txt 的并发闸门：同一批分片各跑一遍阅读工具，超过这个数就排队。按事件循环建一次
# ——一个进程里跑多个事件循环时（比如离线测试），不会拿着绑在旧循环上的信号量。
_piece_limiter: asyncio.Semaphore | None = None
_piece_limiter_loop: object = None


def _limiter() -> asyncio.Semaphore:
    global _piece_limiter, _piece_limiter_loop
    loop = asyncio.get_running_loop()
    if _piece_limiter is None or _piece_limiter_loop is not loop:
        _piece_limiter = asyncio.Semaphore(MAX_CONCURRENCY)
        _piece_limiter_loop = loop
    return _piece_limiter


async def _correct_piece(piece: str) -> tuple[list[dict], dict | None]:
    """跑一份 000.txt：模型自己读、自己改，返回逐字改动与调试信息。

    异常不向外抛：模型某一轮调用失败时引擎自己会带着已经落地的改动收工；真出别的错，
    也只让这一片没有改动，不让整个请求失败。
    """
    async with _limiter():
        try:
            return await run_document_correction(piece, max_turns=TOOL_MAX_TURNS)
        except Exception as exc:
            log_error("readtool.piece_fail", exc=exc, chars=len(piece))
            mark_engine_degraded()
            return [], None


def _owner_unit(starts: list[int], lengths: list[int], position: int) -> int | None:
    """把 000.txt 里的绝对位置映射回它属于哪个单元；落在单元之间的换行上返回 None。"""
    index = bisect_right(starts, position) - 1
    if index < 0 or position >= starts[index] + lengths[index]:
        return None
    return index


def table_cells_from_blocks(blocks: list[dict]) -> list[dict]:
    """把表格展平成「一个单元格一个纠错单元」，并给每格配一个跨表唯一的编号。

    编号形如 2R3C7：2 是第几张表，R3C7 是它在这张表里的行列。表格里本就有大量一模一样
    的格子（合同价款 %、元（大写：）），指望模型"抄一段唯一的引文"来定位是定不到的——
    实测同一张付款表里 4 处错字只检出 1 处。编号在整篇里只出现一次，模型抄行首那个就能
    定到某一格，再用 edit_cell 在格内改（见 readtool.TABLE_SYSTEM_PROMPT）。

    行列号取解析结果里的真实位置（blocks 的 table / row / col），跟结果页展示用的是同一套
    数字：合并单元格只在左上角出一格，被它盖住的槽位、以及空到不送纠错的格子都不占编号，
    所以编号会跳号（1R2C1、1R2C4，C2、C3 被横合并或空格吃掉了）——那只是没送纠错的槽位
    在跳号，合并关系仍由 colspan / rowspan 表达，纠错和展示都不受影响。
    """
    cells: list[dict] = []
    for block in blocks:
        if block.get("type") != "row":
            continue
        text = block.get("text") or ""
        if not is_correction_cell(text):
            continue
        table = (block.get("table") or 0) + 1
        row = (block.get("row") or 0) + 1
        col = (block.get("col") or 0) + 1
        cells.append({"id": f"{table}R{row}C{col}", "text": text})
    return cells


def _table_batches(cells: list[dict]) -> list[list[int]]:
    """按顺序把单元格分批：一批拼出来的 000.txt 不超过 MAX_TEXT_LENGTH，绝不把一格切成两半。

    表格跟正文一个口径：一份 000.txt 装不下就分批、并发各跑一遍阅读工具，再按编号合回
    各格。单格本身就超上限时它自成一批——单元格是不可再分的一个纠错单元。
    """
    batches: list[list[int]] = []
    current: list[int] = []
    size = 0
    for index, cell in enumerate(cells):
        cost = len(cell["id"]) + 1 + len(cell["text"]) + 1
        if current and size + cost > MAX_TEXT_LENGTH:
            batches.append(current)
            current, size = [], 0
        current.append(index)
        size += cost
    if current:
        batches.append(current)
    return batches


def _span_at(spans: list[tuple[str, int, int]], position: int) -> tuple[str, int, int] | None:
    """这个位置落在哪一格的正文里；落在行首编号或换行上返回 None。"""
    for span in spans:
        if span[1] <= position < span[1] + span[2]:
            return span
    return None


async def _correct_table_batch(
    whole: str,
    starts: list[int],
    batch: list[int],
    cells: list[dict],
) -> tuple[list[list[dict]], dict | None]:
    """跑一批表格单元格：这批拼成一份 000.txt，走表格那条路的阅读工具。

    异常不向外抛：某一批跑挂了只让这一批没有改动，不让整个请求失败（跟 _correct_piece 一个口径）。
    """
    base = starts[batch[0]]
    last = cells[batch[-1]]
    piece = whole[base:starts[batch[-1]] + len(last["id"]) + 1 + len(last["text"])]
    spans = [
        (cells[index]["id"], starts[index] - base + len(cells[index]["id"]) + 1, len(cells[index]["text"]))
        for index in batch
    ]
    try:
        async with _limiter():
            errors, info = await run_document_correction(
                piece, max_turns=TOOL_MAX_TURNS, table=True, cells=spans,
                corpus=whole, corpus_offset=base,
            )
    except Exception as exc:
        log_error("readtool.table_batch_fail", exc=exc, cells=len(batch))
        mark_engine_degraded()
        return [[] for _ in batch], None

    merged: list[list[dict]] = [[] for _ in batch]
    owner = {cells[index]["id"]: position for position, index in enumerate(batch)}
    for error in errors:
        span = _span_at(spans, error["position"])
        if span is None:
            # 落在行首编号或换行上：模型改不动编号，只可能是引擎算错，丢弃。
            continue
        key, start, _ = span
        merged[owner[key]].append({**error, "position": error["position"] - start})
    return merged, info


async def correct_table_cells(cells: list[dict], debug: bool = False) -> tuple[list[dict], list[dict]]:
    """表格纠错：一格一个单元，先按编号定位到某一格、再在格内改，按单元格分批并发跑。

    跟正文那条路（correct_units）同一个形状：拼一份 000.txt、超限就分批、并发跑完按偏移合回
    各单元。区别只有一个——定位靠行首那个跨表唯一的编号，所以不存在"引文定不到某一处"的情况。
    """
    if not cells:
        return [], []

    heads = [cell["id"] + ":" for cell in cells]
    starts: list[int] = []
    position = 0
    for head, cell in zip(heads, cells):
        starts.append(position)
        position += len(head) + len(cell["text"]) + 1
    whole = "\n".join(head + cell["text"] for head, cell in zip(heads, cells))

    batches = _table_batches(cells)
    gathered = await asyncio.gather(*(
        _correct_table_batch(whole, starts, batch, cells) for batch in batches
    ))

    merged: list[list[dict]] = [[] for _ in cells]
    debug_records: list[dict] = []
    for batch, (errors_by_cell, info) in zip(batches, gathered):
        for index, errors in zip(batch, errors_by_cell):
            merged[index] = errors
        if debug and info:
            debug_records.append({"unit_offset": starts[batch[0]], **info})

    results = [
        build_result_from_errors(cell["text"], merged[index])
        for index, cell in enumerate(cells)
    ]
    return results, debug_records


async def correct_units(
    units: list[str],
    debug: bool = False,
    strip: bool = True,
    prefixes: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """把一批单元拼成一份 000.txt 交给阅读工具纠错，再按偏移把改动映射回各单元。

    单元之间用换行连接（换行不属于任何单元，模型也改不动它）。整份文本超过
    MAX_TEXT_LENGTH 时在句末切成多片、并发各跑一遍阅读工具，再按偏移合回各单元：
    不按字数拒绝请求，也不丢字符，只是片变多、耗时变长。

    strip=True 时先去除每个文本的首尾空白（文件纠错路径）；
    strip=False 时原样处理，保证结果坐标能直接映射回调用方传入的原文。

    prefixes 给每个单元的行首挂一个标签（表格单元格用 R 行 C 列坐标）。标签跟换行一样
    不属于任何单元：改动落在标签上就丢弃（见 _owner_unit），返回值里也看不到标签——
    单元本身始终是标签后面那段原文。
    """
    if not units:
        return [], []

    normalized = [normalize_text(unit) if strip else unit for unit in units]
    labels = list(prefixes) if prefixes is not None else [""] * len(units)
    if len(labels) != len(units):
        raise ValueError(f"prefixes 要和 units 一一对应，收到 {len(labels)} 个标签 {len(units)} 个单元")
    starts: list[int] = []
    position = 0
    for label, unit in zip(labels, normalized):
        position += len(label)
        starts.append(position)
        position += len(unit) + 1
    document = "\n".join(label + unit for label, unit in zip(labels, normalized))

    if is_skippable_text(document):
        return [empty_result(unit) for unit in normalized], []

    pieces: list[tuple[int, str]] = []
    offset = 0
    for piece in split_long_text(document, MAX_TEXT_LENGTH):
        pieces.append((offset, piece))
        offset += len(piece)

    gathered = await asyncio.gather(*(_correct_piece(piece) for _, piece in pieces))

    lengths = [len(unit) for unit in normalized]
    merged: dict[int, list[dict]] = {}
    debug_records: list[dict] = []
    for (base, _), (piece_errors, piece_debug) in zip(pieces, gathered):
        for error in piece_errors:
            absolute = error["position"] + base
            owner = _owner_unit(starts, lengths, absolute)
            if owner is None:
                # 落在单元之间的那个换行上：模型改不动换行，只可能是引擎算错，丢弃。
                continue
            merged.setdefault(owner, []).append(
                {**error, "position": absolute - starts[owner]}
            )
        if debug and piece_debug:
            debug_records.append({"unit_offset": base, **piece_debug})

    results = [
        build_result_from_errors(unit, merged.get(index) or [])
        for index, unit in enumerate(normalized)
    ]
    return results, debug_records


async def correct_document(source: str) -> dict:
    """对整段文本跑一遍阅读工具纠错（/correction_text 用）。"""
    results, _ = await correct_units([source], strip=False)
    return results[0]


def _test_mock_response(content: str) -> dict:
    """测试模式：根据输入文字生成假纠错结果，不调用 LLM。"""
    original = normalize_text(content)
    # 预置常见错误模式: (错误文本, 正确文本)
    patterns = [
        ("天汽", "天气"),
        ("中国人民一行", "中国人民银行"),
        ("注竟", "注意"),
        ("必需", "必须"),
        ("损失由其自行", "损失由其自行"),
    ]
    for wrong, correct in patterns:
        if wrong in original:
            pos = original.index(wrong)
            errors = []
            for i in range(len(wrong)):
                errors.append({
                    "position": pos + i,
                    "original_char": wrong[i],
                    "correct_char": correct[i] if i < len(correct) else "",
                    "tag": "replace",
                })
            corrected = original.replace(wrong, correct, 1)
            result = {
                "original_text": original,
                "correct_text": corrected,
                "wrong_ids": [e["position"] for e in errors],
                "errors": errors,
            }
            return nestjs_correction_response(result)
    return nestjs_correction_response(empty_result(original))


def collect_diff_results(processed: list[dict]) -> list[dict]:
    """只保留真正检出错误的条目。"""
    return [item for item in processed if item.get("errors")]


def build_document_blocks(
    blocks: list[dict],
    paragraph_results: list[dict],
    table_results: list[dict],
) -> list[dict]:
    """把解析出的正文块和纠错结果对上，给结果页一份「全文」。

    blocks 是解析出来的正文顺序（段落、表格单元格交错）；段落结果和表格结果的顺序都是
    各自解析时的顺序，所以这里分开走两个指针。表格的两边（解析出的单元格、送去纠错的
    单元格）用的是同一个判据 is_correction_cell，所以指针一定对得齐；那些太短、纯数字的
    格子照样按原位列出来，只是没有改动。

    表格单元格会原样带上 table / row / col / colspan / rowspan，结果页据此把一格一格的
    结果重新拼回一张表；空单元格也要留着——少一格整张表就错位了。
    """
    output: list[dict] = []
    paragraph_index = 0
    table_index = 0

    for block in blocks:
        text = block.get("text") or ""
        if block.get("type") == "p":
            if paragraph_index >= len(paragraph_results):
                # 被 limit 截掉、没送去纠错的段落：结果页也不展示
                continue
            item = paragraph_results[paragraph_index]
            paragraph_index += 1
            output.append({
                "type": "p",
                "text": item.get("original_text") or text,
                "errors": item.get("errors") or [],
                "wrong_ids": item.get("wrong_ids") or [],
            })
            continue

        # 竖合并盖起来的续格：它顶上的格子已经带着它了，不单独展示；但它自己要是占了
        # 一个纠错单元，指针照样得往前走一格，不然后面全错位。
        if block.get("merged"):
            if is_correction_cell(text):
                table_index += 1
            continue

        item = None
        if is_correction_cell(text) and table_index < len(table_results):
            # 送去纠错的那一格，用结果里的原文（跟标记位置是同一份文字）
            item = table_results[table_index]
            table_index += 1

        cell = {
            "type": block.get("type"),
            "text": (item or {}).get("original_text") or text,
            "errors": (item or {}).get("errors") or [],
            "wrong_ids": (item or {}).get("wrong_ids") or [],
        }
        for key in ("table", "row", "col"):
            if block.get(key) is not None:
                cell[key] = block[key]
        cell["colspan"] = block.get("colspan") or 1
        cell["rowspan"] = block.get("rowspan") or 1
        output.append(cell)

    return output


HIGHLIGHT_TAGS = ("replace", "delete", "insert")


def _merge_errors(errors: list[dict]) -> list[dict]:
    """把相邻的同类改动收成一处，规则跟前端 diff.js 的 mergeAdjacentErrors 完全一致。

    连着删几个字算一处、连着改几个字算一处、同一位置补几条算一处；类型不同、或中间隔着
    没改的字就分成两处。结果页的清单和页面上的框都按这个口径出，处数才跟记录里一致。
    """
    merged: list[dict] = []
    for error in sorted(
        (item for item in errors if isinstance(item, dict)),
        key=lambda item: (int(item.get("position") or 0), str(item.get("tag") or "")),
    ):
        position = error.get("position")
        tag = error.get("tag")
        if not isinstance(position, int) or tag not in HIGHLIGHT_TAGS:
            merged.append({**error})
            continue
        last = merged[-1] if merged else None
        if last is not None and last.get("tag") == tag and isinstance(last.get("position"), int):
            if tag == "insert" and last["position"] == position:
                last["correct_char"] = (last.get("correct_char") or "") + (error.get("correct_char") or "")
                continue
            if tag != "insert" and last["position"] + len(last.get("original_char") or "") == position:
                last["original_char"] = (last.get("original_char") or "") + (error.get("original_char") or "")
                last["correct_char"] = (last.get("correct_char") or "") + (error.get("correct_char") or "")
                continue
        merged.append({**error})
    return merged


def _highlight_rects(boxes: list, at: int, length: int, cell_rect: list | None) -> list[list]:
    """把改动占的那几个字翻成页面上的方框；同一行的相邻字合成一个框。

    length=0 是插入：没有占原文的字，就在锚点处画一根细竖线（插在末尾时挂在前一个字右边）。
    字级方框取不到就退回整格方框（表格单元格），再取不到就返回空——清单里照样有这一条，
    只是页面上框不出来。
    """
    picked = [
        [float(v) for v in boxes[index]]
        for index in range(at, at + length)
        if 0 <= index < len(boxes) and boxes[index]
    ]
    if not picked and length == 0:
        for index in (at, at - 1):
            if 0 <= index < len(boxes) and boxes[index]:
                box = [float(v) for v in boxes[index]]
                x = box[0] if index == at else box[2] - 2.0
                picked = [[x, box[1], x + 2.0, box[3]]]
                break
    if not picked and cell_rect:
        picked = [[float(v) for v in cell_rect[:4]]]
    merged: list[list] = []
    for box in picked:
        if merged:
            previous = merged[-1]
            same_line = not (box[1] >= previous[3] - 1 or box[3] <= previous[1] + 1)
            if same_line and box[0] - previous[2] <= 1.0:
                previous[1] = min(previous[1], box[1])
                previous[2] = max(previous[2], box[2])
                previous[3] = max(previous[3], box[3])
                continue
        merged.append(list(box))
    return merged


def build_document_highlights(
    blocks: list[dict],
    paragraph_results: list[dict],
    table_results: list[dict],
) -> list[dict]:
    """把每一处改动翻成结果页要贴的方框（PDF 上那种高亮）。

    blocks 是解析出来的正文顺序（带页码、每个字的方框），results 是纠错结果。这里跟
    build_document_blocks 用同一套指针走法，但只输出「标在页面哪儿」：页码 + 方框 +
    改前改后。方框用的是 PDF 自己的坐标系（左上角原点、单位磅），前端按同一个缩放贴到
    画布上，所以框一定落在原字上，跟浏览器里直接看 PDF 是一回事。

    页面上没有坐标的（Word 直接解析出来的、页面被旋转过、字符流跟文字对不齐）不给框；
    连页码都没有的整条丢掉——那种记录结果页会退回「整篇文字重排」那种渲染。
    """
    highlights: list[dict] = []
    paragraph_index = 0
    table_index = 0

    for block in blocks:
        text = block.get("text") or ""
        if block.get("type") == "row":
            # 竖合并盖起来的续格不单独展示，但它占了一个纠错单元时指针也得往前走
            if block.get("merged"):
                if is_correction_cell(text):
                    table_index += 1
                continue
            item = None
            if is_correction_cell(text) and table_index < len(table_results):
                item = table_results[table_index]
                table_index += 1
        else:
            if paragraph_index >= len(paragraph_results):
                continue
            item = paragraph_results[paragraph_index]
            paragraph_index += 1

        errors = (item or {}).get("errors") or []
        page = block.get("page")
        if not errors or not isinstance(page, int):
            continue
        source = (item or {}).get("original_text") or text
        boxes = block.get("char_boxes") or block.get("boxes") or []
        cell_rect = block.get("rect")

        for error in _merge_errors(errors):
            tag = error.get("tag")
            at = error.get("position")
            if tag not in HIGHLIGHT_TAGS or not isinstance(at, int) or at < 0:
                continue
            original = error.get("original_char") or ""
            corrected = error.get("correct_char") or ""
            if tag == "insert":
                if not corrected:
                    continue
                length = 0
            else:
                if not original or source[at:at + len(original)] != original:
                    continue
                length = len(original)
            highlights.append({
                "index": len(highlights),
                "page": page,
                "tag": tag,
                "rects": _highlight_rects(boxes, at, length, cell_rect),
                "original": original,
                "corrected": corrected,
            })
    return highlights


def success_response(results: any) -> dict:
    return {
        "code": 0,
        "results": results,
        "data": {"results": results},
        "messages": "任务完成",
        "engine_status": "degraded" if is_engine_degraded() else "ok",
    }


def nestjs_correction_response(results: dict) -> dict:
    payload = {
        "original_text": results.get("original_text", ""),
        "correct_text": results.get("correct_text", ""),
        "wrong_ids": results.get("wrong_ids") or [],
        "errors": results.get("errors") or [],
    }
    return {
        "results": payload,
        "code": 0,
        "data": {"results": payload},
        "messages": "任务完成",
        "engine_status": "degraded" if is_engine_degraded() else "ok",
    }


# ── 健康检查 ──

_STARTED_AT = time.monotonic()


@app.get("/health")
async def health():
    """轻量存活检查：不连模型、不花 token。

    容器的健康检查每 30 秒打一次这里，所以只做纯本地判断：进程还在、配置齐了就算 ok。
    模型通不通、会不会调工具，打 /health/correction —— 那条会真跑一次纠错，会花 token，
    只在需要时手动调。
    """
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "base_url": BASE_URL,
        "max_text_length": MAX_TEXT_LENGTH,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT),
    }


@app.get("/config")
async def config():
    """前端要的那点开关，别的不回。

    配了外部解析服务（DocParser）时，页面上「文本纠错」「文档纠错」这两条手工入口就收起来：
    配了解析服务意味着纠错主要走 API 调用进来，网页只管看纠错记录和按链接调用，
    再摆两个手工入口只会让人绕着走。
    """
    return {"parse2json": parse2json.available()}


# 内置纠错健康检测文本（约 50 字，含 1 处明确错别字：但→担）
HEALTH_CHECK_TEXT = "甲方应按合同约定向乙方支付货款，逾期未支付的，应承但违约责任，并赔偿乙方因此产生的全部损失。"
HEALTH_EXPECTED_CORRECTIONS = [("承但", "承担")]


@app.get("/health/correction")
async def health_correction():
    """跑一次完整纠错链路，验证纠错引擎是否真正可用。

    生产接入封装模型（智能体路由）后第一件事就是打这个接口：它既看得到内置错别字
    「承但→承担」有没有改对，也看得到模型有没有真的调用工具。路由不支持 tool_calls 时
    模型不会去读原文，纠错会**静默为空**——只有这里能一眼看出来（tool_calls=0）。
    """
    started = time.perf_counter()
    try:
        results, debug = await correct_units([HEALTH_CHECK_TEXT], debug=True, strip=False)
        result = results[0]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        errors = result.get("errors") or []
        correct_text = result.get("correct_text", "") or ""
        checks = [
            {
                "original": original,
                "expected": expected,
                "matched": original not in correct_text and expected in correct_text,
            }
            for original, expected in HEALTH_EXPECTED_CORRECTIONS
        ]
        matched_count = sum(1 for check in checks if check["matched"])

        # 引擎自证：模型有没有调工具读原文、有没有读完、有没有走到 finish
        turns = [turn for record in debug for turn in (record.get("turns") or [])]
        tool_calls = sum(int(turn.get("calls") or 0) for turn in turns)
        covered = sum(int(record.get("covered") or 0) for record in debug)
        finished = bool(debug) and all(record.get("finished") for record in debug)
        reasons: list[str] = []
        if not tool_calls:
            reasons.append(
                "模型一次工具都没调用：这条模型路由可能不支持 tool_calls（纠错靠它读原文）"
            )
        if not finished:
            reasons.append("没有走到 finish：模型没把原文读完")
        if matched_count < len(HEALTH_EXPECTED_CORRECTIONS):
            reasons.append("内置错别字没有改对")

        return {
            "status": "ok" if not reasons else "degraded",
            "reason": "；".join(reasons),
            "elapsed_ms": elapsed_ms,
            "engine": "readtool",
            "test_text": HEALTH_CHECK_TEXT,
            "turns": len(turns),
            "tool_calls": tool_calls,
            "covered_chars": covered,
            "finished": finished,
            "error_count": len(errors),
            "matched_count": matched_count,
            "expected_count": len(HEALTH_EXPECTED_CORRECTIONS),
            "checks": checks,
            "errors": errors,
            "correct_text": correct_text,
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "status": "error",
            "elapsed_ms": elapsed_ms,
            "error": str(exc),
        }


# ── 文件批量纠错 ──


def _parse_error_status(code: int) -> int:
    """解析服务的错误码映射成 HTTP 状态码。

    4 开头的是「你给的链接/文件有问题」（不是 PDF、下不来、页数超限），照 400 回，调用方一眼
    知道该改什么；其余（连不上、超时、服务内部错）算我们这条链路的问题，回 500。
    """
    return 400 if 40000 <= (code or 0) < 50000 else 500


async def _page_source(
    *, source_url: str | None, filename: str, data: bytes | None, workdir: str
) -> tuple[str | None, str, int, str | None]:
    """解析跑通之后，把「结果页要铺的那份原件」弄到手。

    返回 (doc_id, source_kind, convert_ms, 本机那份 PDF 的路径)；最后一截是给坐标换算用的
    （见 _rescale_parse_boxes），调用方不需要时就当它不存在。

    结果页是把原文件一页页铺出来、再把改动框在原字上，只有 PDF 做得到，所以 Office 还得先转。
    这活儿特意放在解析成功之后做：解析失败的请求（比如链接那头根本不是这个解析服务吃的格式）
    一步多余的事都不做。

    拿不到就回 (None, 'pdf', 0)——纠错结果已经出来了，不因为展示这一步再失败一次。
    """
    try:
        blob = data
        if blob is None:
            if not source_url:
                return None, "pdf", 0, None
            _, blob = await _download_file(source_url)
        if not is_office_file(filename):
            doc_id, path = store_pdf(blob, workdir)
            return doc_id, "pdf", 0, path
        convert_started = time.perf_counter()
        pdf_bytes = await convert_to_pdf(filename, blob)
        if not pdf_bytes:
            log_warning("request.correction_file.page_source_no_convert", filename=filename)
            return None, "pdf", 0, None
        doc_id, path = store_pdf(pdf_bytes, workdir)
        return doc_id, "office", int((time.perf_counter() - convert_started) * 1000), path
    except Exception as exc:
        log_warning("request.correction_file.page_source_failed", filename=filename, exc=str(exc))
        return None, "pdf", 0, None


def _page_sizes_in_points(path: str | None) -> dict[int, tuple[float, float]]:
    """PDF 每页多大（磅，跟 PDF.js 的 viewport(scale=1) 一个口径）；读不了就回空 dict。"""
    if not path:
        return {}
    try:
        with pymupdf.open(path) as doc:
            return {
                index: (float(page.rect.width), float(page.rect.height))
                for index, page in enumerate(doc)
            }
    except Exception as exc:
        log_warning("request.correction_file.page_size_failed", path=path, exc=str(exc))
        return {}


def _rescale_parse_boxes(blocks: list[dict], page_images: dict, pdf_path: str | None) -> int:
    """外部解析的方框（渲染图像素）→ PDF 磅。开文件是阻塞活，调用方放到线程里跑。"""
    return parse2json.rescale_to_points(blocks, page_images, _page_sizes_in_points(pdf_path))


async def correct_file(
    filename: str,
    data: bytes | None,
    *,
    limit: int = 0,
    debug: bool = False,
    full: bool = False,
    source_url: str | None = None,
    use_parser: bool = True,
) -> dict:
    """整份文件跑一遍纠错：/correction 和对外接口 /openapi/contract-audit/web-correct 共用。

    两边只有「文件从哪来」不一样，转换、解析、纠错、拼结果这一整条链路是同一段。

    `data` 为 None 表示「这份文件我们手上没有，只有它的链接」（对外接口收 fileUrl 的那种）：
    配了解析服务就把链接**原样转交**过去——我们不下载、不转格式、不换地址，它报什么错就
    如实报给调用方。等它真解析出来了，才为了结果页去把原件取回来（见 _page_source）。

    use_parser 决定这份文件走不走外部解析服务（DocParser）。网页和内部 /correction 用它；
    老的对外接口显式传 False，一律按它自己原来那套本地解析走——配没配外部服务跟它无关，
    传的是哪个接口就按哪个接口的逻辑做。
    """
    log_info(
        "request.correction_file", filename=filename, limit=limit, debug=debug, full=full,
        use_parser=use_parser,
    )
    # 一趟纠错开一个临时目录：上传的原件、转出来的 PDF 都放里头，请求结束无论成败整个删掉。
    # （原来是按原文件名直接放进系统临时目录，两个人同时传同名文件会互相覆盖。）
    workdir = new_workdir()
    try:
        # 解析（文件 -> 段落 + 表格单元格）和纠错（模型那段）分开计时：纠错记录里要展示
        # 这两段各花了多久，混成一个总数看不出瓶颈在哪儿。
        started = time.perf_counter()
        file_path = os.path.join(workdir, os.path.basename(filename))
        if data is not None:
            with open(file_path, "wb") as f:
                f.write(data)

        ext = os.path.splitext(filename)[1].lower()
        source_kind = "pdf"
        doc_id = None
        parsed_from_pdf = True
        convert_ms = 0
        # 外部解析那条路专用的两个抓手：解析服务那版渲染图的尺寸（方框是那上面的像素），
        # 以及结果页要铺的那份 PDF 在本机的路径——两者一除就把方框缩回 PDF 的磅。
        page_images: dict[int, tuple[float, float]] = {}
        parser_pdf: str | None = None

        if data is None:
            # ── 只给了链接：这份文件我们不去碰，原样交给解析服务 ──
            # 它自己去下载、自己判格式；报什么错就如实报给调用方（比如「不是 PDF」），
            # 我们不替它猜、也不悄悄换成我们自己那套解析。
            if not parse2json.available():
                raise HTTPException(
                    status_code=500,
                    detail="只给了链接，但没配外部解析服务（PARSE2JSON_BASE_URL）：请配上，或直接传文件",
                )
            # 结果页要铺的原件跟解析同时去弄：解析我们只能干等它，取原件（下载 + 转格式）
            # 我们等得起，两件事没有先后依赖，串着做就是白等一段时间。
            page_task = None
            if full:
                page_task = asyncio.ensure_future(_page_source(
                    source_url=source_url, filename=filename, data=None, workdir=workdir
                ))
            try:
                paragraphs, tables, blocks, page_images = await parse2json.parse_units(
                    source_url, filename=filename
                )
            except parse2json.ParseError as exc:
                # 解析都没成，原件取来也没用：把它撤掉，别白占带宽和转换服务
                if page_task is not None:
                    page_task.cancel()
                raise HTTPException(
                    status_code=_parse_error_status(exc.code),
                    detail=f"外部解析服务解析失败：{exc}",
                ) from None
            parser = "parse2json"
            if page_task is not None:
                # 上面那趟多半已经跑完了，这里只是收一下（拿不到就退回文字重排，
                # 不因为展示这一步把已经跑好的纠错结果废掉）
                doc_id, source_kind, convert_ms, parser_pdf = await page_task
            parsed = time.perf_counter()
        else:
            # Word/Excel 先转成 PDF：结果页要「跟直接打开原文件一个样」，只有 PDF 做得到——
            # 浏览器里把 PDF 一页页铺出来，再把改动的字贴上高亮框（跟线上那套一个做法）。
            # 转换服务不可用时退回直接解析 docx（没有坐标，结果页就只能重排文字）；但配了外部
            # 解析服务就没得退——它只收 PDF，见下面那一支。
            if is_office_file(filename):
                convert_started = time.perf_counter()
                pdf_bytes = await convert_to_pdf(filename, data)
                if pdf_bytes:
                    doc_id, file_path = store_pdf(pdf_bytes, workdir)
                    source_kind = "office"
                    convert_ms = int((time.perf_counter() - convert_started) * 1000)
                    log_info(
                        "request.correction_file.converted",
                        filename=filename, bytes=len(pdf_bytes), convert_ms=convert_ms,
                    )
                elif use_parser and parse2json.available():
                    # 配了解析服务，Word/Excel 也必须先转成 PDF——它只收 PDF。转不出来就是错，
                    # 不退回本地那套 docx 直读（两套结果不一样）
                    raise HTTPException(
                        status_code=500,
                        detail="文档转换服务不可用，且配了外部解析服务（它只收 PDF）：请检查转换服务，或直接传 PDF",
                    )
                elif ENABLE_WORD_CORRECTION and ext in (".docx", ".doc"):
                    paragraphs, tables, blocks = filter_text_and_tables_from_docx(file_path)
                    source_kind = "office_text"
                    parsed_from_pdf = False
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="文档转换服务不可用：这种格式需要先转成 PDF 才能纠错，请上传 PDF 或联系管理员检查转换服务",
                    )
            elif full:
                # 上传的本来就是 PDF：原样留一份，结果页直接铺出来就是「打开原文件」的样子
                # （只给要展示整篇文档的调用方存，别的调用方不必占这份磁盘）
                doc_id, file_path = store_pdf(data, workdir)

            parser = "local"
            if parsed_from_pdf:
                if use_parser and parse2json.available():
                    # 配了解析服务，它就跑定了：版面模型 + OCR，扫描件、没有框线的表格比本地
                    # PyMuPDF 强。这份内容在我们手上（上传的），它自己取不到，所以存成对象、
                    # 给它一个我们这边的地址；连不上、服务报错、等超时一律报错给用户，
                    # 不悄悄换回本地解析——两套解析出来的结果不一样。
                    if doc_id is None:
                        doc_id, file_path = store_pdf(data, workdir)
                    target = await asyncio.to_thread(parse2json.file_url, doc_id)
                    if not target:
                        raise HTTPException(
                            status_code=500,
                            detail="外部解析服务要一个它自己够得着的原文地址：请配 PARSE2JSON_FILE_URL（带 {doc_id}）或对象存储的对外地址",
                        )
                    try:
                        paragraphs, tables, blocks, page_images = await parse2json.parse_units(
                            target, filename=filename
                        )
                    except parse2json.ParseError as exc:
                        raise HTTPException(
                            status_code=_parse_error_status(exc.code),
                            detail=f"外部解析服务解析失败：{exc}",
                        ) from None
                    parser = "parse2json"
                    parser_pdf = file_path
                else:
                    paragraphs, tables, blocks = filter_text_and_tables(file_path)
            parsed = time.perf_counter()

        # 外部解析给的方框是它自己那版渲染图的像素（A4 一页 1190×1682，正好 72dpi 的 2 倍），
        # 结果页要的是 PDF 自己的磅：趁原件就在手上，逐页按「页宽 ÷ 图宽」缩回去。不缩，框
        # 就会大一圈、还整片偏到右下角去。只有要出结果页（full）时才用得上这些方框。
        if full and parser == "parse2json" and page_images:
            scaled = await asyncio.to_thread(_rescale_parse_boxes, blocks, page_images, parser_pdf)
            log_info("request.correction_file.rescaled", blocks=scaled, has_pdf=bool(parser_pdf))

        # Word 表格跟 PDF 一个口径：展平成单元格送去纠错（一格一个纠错单元），
        # 每格行首挂一个跨表唯一的编号（2R3C7），见 table_cells_from_blocks。
        table_cells = table_cells_from_blocks(blocks)

        log_info(
            "request.correction_file.parsed",
            paragraphs=len(paragraphs), table_segments=len(table_cells), parser=parser,
            convert_ms=convert_ms, parse_ms=int((parsed - started) * 1000) - convert_ms,
        )

        # 限制处理段落数（用于快速测试前几页）
        if limit > 0 and len(paragraphs) > limit:
            paragraphs = paragraphs[:limit]
            log_info("request.correction_file.limited", paragraphs=len(paragraphs))

        (paragraph_results, paragraph_debug), (table_results, _) = await asyncio.gather(
            correct_units(paragraphs, debug=debug),
            correct_table_cells(table_cells, debug=debug),
        )

        corrected = time.perf_counter()

        diff_result = collect_diff_results(paragraph_results) + collect_diff_results(table_results)
        log_info(
            "request.correction_file.done",
            diff_count=len(diff_result),
            parse_ms=int((parsed - started) * 1000),
            correct_ms=int((corrected - parsed) * 1000),
        )
        for i, item in enumerate(diff_result[:10]):
            log_correction_stage(f"result[{i}]", item.get("original_text", ""), item, path="/correction")

        response_data = success_response(diff_result)
        if full:
            response_data["blocks"] = build_document_blocks(blocks, paragraph_results, table_results)
            # 结果页要的是「原文件长什么样 + 改动标在哪儿」：靠 blocks 重排文字做不到
            # 「跟原文件一个样」，所以另回一份贴框用的数据，前端拿它铺 PDF。
            response_data["highlights"] = build_document_highlights(blocks, paragraph_results, table_results)
            response_data["source_kind"] = source_kind
            if doc_id:
                response_data["doc_id"] = doc_id
                response_data["pdf_url"] = f"/document/{doc_id}"
            # 纠错记录里要展示解析 / 纠错各花了多久，所以跟 blocks 一样只在 full 时多回一段。
            response_data["timings"] = {
                "convert_ms": convert_ms,
                "parse_ms": int((parsed - started) * 1000),
                "correct_ms": int((corrected - parsed) * 1000),
                "total_ms": int((time.perf_counter() - started) * 1000),
            }
        if debug:
            response_data["debug"] = paragraph_debug
            write_tool_debug(paragraph_debug)
        return response_data
    except HTTPException:
        # 明确的 400（比如「这种格式要先把 PDF 转出来」）原样抛出去，别被下面统一成 500
        raise
    except Exception as exc:
        log_error("request.correction_file.fail", exc=exc, filename=filename)
    finally:
        # 清理必须在「没人再引用那个异常」之后：解析库碰到打不开的文件时会在失败路径上
        # 漏一个文件句柄，而那个句柄挂在这个异常对象上——异常只要还被引用着，文件就被占
        # 着，当场删不掉（Windows 不许删正在被打开的文件）。所以上面不写 `from exc`：
        # 日志里已经有完整堆栈（log_error 带 exc_info），让异常自然消失、句柄立刻关闭，
        # 临时目录就能当场清掉。
        await asyncio.to_thread(discard_workdir, workdir)
    raise HTTPException(status_code=500, detail="文档纠错任务失败")


@app.post("/correction")
async def correct_texts_endpoint(
    file: UploadFile = File(...),
    limit: int = Query(0, description="限制处理段落数，0 表示不限制"),
    debug: bool = Query(False, description="返回阅读工具纠错调试数据"),
    full: bool = Query(False, description="额外返回按正文顺序的全文分段与页面高亮框（结果页展示整篇文档用）"),
):
    return await correct_file(file.filename, file.file.read(), limit=limit, debug=debug, full=full)


# ── 结果页要展示的文档 ──

@app.get("/document/{doc_id}")
async def get_document(doc_id: str, request: Request):
    """取回一份上传过的 PDF（原始 PDF，或 Office 转出来的那份）。

    结果页就是拿这个地址当 PDF 阅读器的数据源：浏览器按需取字节，一页页画出来，
    高亮框再按坐标贴上去。

    配了对象存储时优先走它（那份是长期的、多副本都取得到）：配了对外地址就直接 307 到
    带签名的临时地址，浏览器直连对象存储、后端一个字节都不搬；没配就由后端转发（支持
    Range，PDF.js 靠它渐进加载）。没配对象存储时，本机那份就是唯一的原文：只留
    DOC_TTL_HOURS，被纠错记录引用的跟着记录一起留（记录删掉之后才按 TTL 清）；
    配了的话本机不留长期那份，只有对象里取不到才会走到下面这个兜底。
    """
    if objects.available() and await asyncio.to_thread(objects.exists, doc_id):
        presigned = await asyncio.to_thread(objects.presigned_url, doc_id)
        if presigned:
            return RedirectResponse(presigned, status_code=307)
        try:
            total = await asyncio.to_thread(objects.size, doc_id)
        except Exception as exc:
            log_warning("document.stat_failed", doc_id=doc_id, exc=str(exc))
            raise HTTPException(status_code=404, detail="文档不存在或已过期") from exc
        return _stream_document(doc_id, request, total)

    path = document_path(doc_id)
    if not path:
        raise HTTPException(status_code=404, detail="文档不存在或已过期")
    # 内容按 id 寻址、永不改变，所以可以让浏览器放心缓存
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=86400",
        },
    )


# Range 头只认这三种写法：bytes=起点-终点 / bytes=起点- / bytes=-末尾多少字节
_RANGE = re.compile(r"bytes=(\d*)-(\d*)\Z")


def _range_span(raw: str, total: int) -> tuple[int, int] | None:
    """把 Range 头翻译成要发的 (起点, 终点)；没有或写得不合规就返回 None（整份发）。"""
    matched = _RANGE.match((raw or "").strip())
    if not matched:
        return None
    start_raw, end_raw = matched.groups()
    if start_raw:
        start = int(start_raw)
        end = int(end_raw) if end_raw else total - 1
    elif end_raw:
        start = max(0, total - int(end_raw))
        end = total - 1
    else:
        return None
    if start >= total or end < start:
        return None
    return start, min(end, total - 1)


def _stream_document(doc_id: str, request: Request, total: int) -> Response:
    """把对象存储里的原文转发给浏览器（没配对外地址时走这条路）。"""
    headers = {
        "Content-Disposition": "inline",
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=86400",
    }
    span = _range_span(request.headers.get("range") or "", total)
    if span is None:
        headers["Content-Length"] = str(total)
        return StreamingResponse(
            objects.stream(doc_id, 0, total), media_type="application/pdf", headers=headers,
        )
    start, end = span
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        objects.stream(doc_id, start, length), status_code=206,
        media_type="application/pdf", headers=headers,
    )


# ── 纠错记录（MySQL）──
#
# 记录原先只落在浏览器 localStorage 里：换台机器、清一次缓存就没了，链接也分享不出去。
# 现在统一存进 MySQL，结果页按 id 取。没配 MYSQL_DSN 时下面这几个接口回 503，
# 前端自动退回本地存储，纠错本身照常跑。

async def _record_call(func, *args, **kwargs):
    """把存储层的异常翻译成 HTTP：503 让前端退回本地存储，404 说明这条记录不在。"""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except records.RecordStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="这条纠错记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_error("records.op_failed", action=getattr(func, "__name__", "-"), exc=exc)
        raise HTTPException(status_code=503, detail="纠错记录存储暂时不可用") from exc


@app.get("/records")
async def list_records_endpoint(
    limit: int = Query(50, ge=1, le=200, description="一页多少条"),
    offset: int = Query(0, ge=0, description="从第几条开始"),
):
    """纠错记录列表（按时间倒序），不带全文——结果页再按 id 取整份。"""
    data = await _record_call(records.list_records, limit, offset)
    return {**data, "storage": "mysql"}


@app.post("/records")
async def save_record_endpoint(record: RecordModel):
    """存一条纠错记录：前端纠错完成后调这个，再跳结果页 #/result/<id>。"""
    saved = await _record_call(records.save_record, record.model_dump())
    log_info("request.records.save", record_id=saved.get("id"), filename=record.filename, kind=record.kind)
    return {**saved, "storage": "mysql"}


@app.get("/records/{record_id}")
async def get_record_endpoint(record_id: str):
    """取一条记录的整份内容：结果页靠它——换台机器打开同一个链接也看得到。"""
    record = await _record_call(records.get_record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="这条纠错记录不存在")
    return {"record": record, "storage": "mysql"}


@app.put("/records/{record_id}/ignored")
async def set_ignored_endpoint(record_id: str, payload: IgnoredModel):
    """标记哪些是误报、不用改（传空数组就是全部恢复）。"""
    ignored = await _record_call(records.set_ignored, record_id, payload.ignored)
    log_info("request.records.ignored", record_id=record_id, count=len(ignored))
    return {"ignored": ignored or None}


@app.delete("/records/{record_id}")
async def delete_record_endpoint(record_id: str):
    """删一条记录；它引用的原文要是没人再用了，顺手把磁盘上那份也删掉。"""
    found = await _record_call(records.delete_record, record_id)
    if not found:
        raise HTTPException(status_code=404, detail="这条纠错记录不存在")
    doc_id = found.get("docId")
    # 记录删了，它引用的原文要是没人再用，对象存储里那份和本机缓存那份一起清掉
    if doc_id and not await asyncio.to_thread(records.is_document_referenced, doc_id):
        await asyncio.to_thread(remove_document, doc_id)
    log_info("request.records.delete", record_id=record_id, doc_id=doc_id)
    return {"deleted": True}


# ── 单条文本纠错 ──

async def correct_text(content: str) -> dict:
    """一段文本跑一遍纠错：/correction_text 和对外接口 /openapi/contract-audit/web-correct 共用。"""
    try:
        # 测试模式：直接返回假数据
        if ENABLE_TEST_MODE:
            return _test_mock_response(content)
        # 将字面 \\n 转为真正的换行符（兼容请求方发字面文本的场景）
        if "\\n" in content and "\n" not in content:
            content = content.replace("\\n", "\n")
        # 只去首尾空白；之后 original_text 与所有 position 都以这段文本为准
        source = normalize_text(content)
        if len(source) > MAX_TEXT_LENGTH:
            # 超长不拒绝：correct_units 会按 MAX_TEXT_LENGTH 在句末切片、分片并发纠错后合并。
            # 这里只记一笔，方便按字数排查耗时。
            pieces = split_long_text(source, MAX_TEXT_LENGTH)
            log_info(
                "request.split_long",
                char_len=len(source),
                pieces=len(pieces),
                max_text_length=MAX_TEXT_LENGTH,
            )

        # 整段没有任何可校对内容：按空文本返回，不调用模型
        if is_skippable_text(source):
            log_info("request.skipped", reason="skippable_text")
            return nestjs_correction_response(empty_result(source))

        result = await correct_document(source)
        log_correction_stage("result", source, result, path="/correction_text")
        return nestjs_correction_response(result)
    except HTTPException:
        raise
    except Exception as exc:
        log_error("request.correction_text.fail", exc=exc)
        raise HTTPException(status_code=500, detail="任务因未知原因失败") from exc


@app.post("/correction_text")
async def correction_text(content_data: ContentModel):
    return await correct_text(content_data.content)


# ── 对外接口：合同审查那边的网页纠错 ──

#: 只有结果页用得上、不开 ?full 就不回给调用方的字段
_PAGE_ONLY_FIELDS = ("blocks", "highlights", "timings", "doc_id", "pdf_url", "source_kind")

#: 对外接口可以直接给一个文件链接，由我们下载：下载超时和大小上限
_FETCH_TIMEOUT = 120
_FETCH_MAX_BYTES = 50 * 1024 * 1024


def _fetch_target(url: str) -> str:
    """校验一下要下载的链接（顺带挡住几种不该去连的地址）。

    只收 http/https。环回（127.0.0.1、localhost）和链路本地（169.254.169.254 那种云上取
    凭据的地址）一律拒绝——那是拿我们当跳板去打别人；内网（10 / 172.16 / 192.168）放行：
    合同审查那边给的文件链接经常就在内网文件服务上。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=400, detail="fileUrl 必须是 http/https 的文件链接")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"fileUrl 的域名解析不了：{exc}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise HTTPException(status_code=400, detail=f"fileUrl 指向了不该访问的地址：{address}")
    return url


async def _download_file(url: str) -> tuple[str, bytes]:
    """把链接里那份文件取回来，返回 (文件名, 字节)。

    取回来之后跟直接上传走同一条链路（转 PDF、解析、纠错、存原文），所以结果页、纠错记录、
    页面高亮都跟上传一份文件完全一样。
    """
    target = await asyncio.to_thread(_fetch_target, url)
    name = os.path.basename(urlsplit(target).path) or "document.pdf"
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(target)
            response.raise_for_status()
            data = response.content
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"下载 fileUrl 失败：{exc}") from None
    if len(data) > _FETCH_MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"文件超过 {_FETCH_MAX_BYTES // (1024 * 1024)}MB，请直接上传")
    if not data:
        raise HTTPException(status_code=400, detail="fileUrl 下载回来是空的")
    if content_type == "text/html" or data[:5].lower() in (b"<!doc", b"<html"):
        raise HTTPException(status_code=400, detail="fileUrl 指向的是一个网页、不是文件")
    return name, data


def _public_base_url(request: Request) -> str:
    """结果页链接挂在哪个地址上。

    优先用 PUBLIC_BASE_URL（部署时钉死，最稳）；没配就按这次请求的地址推——外部系统调的
    是前端那个端口，nginx 把 Host 原样透传过来，所以推出来的就是用户能打开的那个地址。
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        return ""
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    return f"{scheme}://{host}"


async def _save_result_link(request: Request, record: dict) -> dict:
    """把这次结果存成一条纠错记录，回一个能直接打开的结果页链接。

    库连不上不拦纠错（纠错才是主活儿）：记一条日志，链接回 null。
    """
    try:
        saved = await asyncio.to_thread(records.save_record, record)
    except Exception as exc:
        log_warning("openapi.record_save_failed", exc=str(exc), kind=record.get("kind"))
        return {"record_id": None, "url": None}
    record_id = saved.get("id")
    base = _public_base_url(request)
    log_info("openapi.record_saved", record_id=record_id, kind=record.get("kind"), base=base)
    return {"record_id": record_id, "url": f"{base}/#/result/{record_id}" if base else None}


async def _run_web_correct(
    request: Request,
    *,
    content: str | None,
    file: UploadFile | None,
    file_url: str | None,
    full: bool,
    use_parser: bool,
) -> dict:
    """两个对外接口共用的一段，只差「走不走外部解析服务」。

    三个都给时以 `file` 为先、其次是 `fileUrl`。

    use_parser 为真（新接口）且配了外部解析服务时，三路都交给它：`fileUrl` 原样转交（我们不
    下载、不转格式），上传的文件先存一份再给它地址；为假（原接口）就一律走本地那套，链接由
    我们下载回来、再走跟直接上传一样的链路——配没配外部服务都不影响它。

    几路都会把这次结果存成一条纠错记录，响应里的 `url` 就是能直接打开的结果页链接
    （`record_id` 是那条记录的 id），点开看到的就是本站在结果页看到的那份东西。

    `type` 标明这次走的是哪条：`file`（`file` 和 `fileUrl` 都算）时 `results` 是逐段、逐格的
    纠错差异（带 `?full=true` 就多回全文分段和高亮框），`text` 时是逐字结果
    （`original_text` / `correct_text` / `errors`）。
    """
    has_file = file is not None and bool(file.filename)
    has_url = bool(file_url and file_url.strip())
    has_text = bool(content and content.strip())
    log_info(
        "request.web_correct",
        source="file" if has_file else "fileUrl" if has_url else "text" if has_text else "-",
        filename=file.filename if has_file else file_url if has_url else None,
        full=full,
        use_parser=use_parser,
    )
    if has_file:
        started = time.perf_counter()
        # 结果页要「原文件长什么样 + 改动标在哪儿」，这份一律按 full 出（顺带把原文存下来）
        payload = await correct_file(
            file.filename, file.file.read(), full=True, use_parser=use_parser
        )
        record = report.file_record(
            file.filename, payload, elapsed_ms=int((time.perf_counter() - started) * 1000)
        )
        link = await _save_result_link(request, record)
        body = payload if full else {key: value for key, value in payload.items() if key not in _PAGE_ONLY_FIELDS}
        return {"type": "file", "correction_count": record["correctionCount"], **link, **body}
    if has_url:
        started = time.perf_counter()
        source = file_url.strip()
        if use_parser and parse2json.available():
            # 新接口 + 配了解析服务：链接原样转交，我们不去碰这份文件。它报什么错就原样显示
            # 什么错——调用方看到的是解析服务判定的事实，不是我们代它猜的结果。
            filename = os.path.basename(urlsplit(source).path) or "document.pdf"
            payload = await correct_file(filename, None, full=True, source_url=source)
        else:
            # 原接口，或新接口但没配解析服务：文件由我们下载回来，再走跟直接上传一样的
            # 链路（原接口这里 use_parser 是假，下载回来也走本地解析，不碰外部服务）
            filename, data = await _download_file(source)
            payload = await correct_file(filename, data, full=True, use_parser=use_parser)
        record = report.file_record(
            filename, payload, elapsed_ms=int((time.perf_counter() - started) * 1000)
        )
        result_link = await _save_result_link(request, record)
        body = payload if full else {key: value for key, value in payload.items() if key not in _PAGE_ONLY_FIELDS}
        return {"type": "file", "correction_count": record["correctionCount"], **result_link, **body}
    if has_text:
        started = time.perf_counter()
        payload = await correct_text(content)
        record = report.text_record(payload, content, elapsed_ms=int((time.perf_counter() - started) * 1000))
        link = await _save_result_link(request, record)
        return {"type": "text", "correction_count": record["correctionCount"], **link, **payload}
    raise HTTPException(
        status_code=400,
        detail="content（文本）、file（文件）和 fileUrl（文件链接）至少要给一个",
    )


@app.post("/openapi/contract-audit/web-correct")
async def web_correct(
    request: Request,
    content: str | None = Form(None, description="待纠错的文本，和 file / fileUrl 三选一"),
    file: UploadFile | None = File(None, description="待纠错的文件（PDF / Word / Excel），和 content / fileUrl 三选一"),
    fileUrl: str | None = Form(None, description="待纠错文件的链接（http/https），由我们下载后走和 file 一样的链路；和 content / file 三选一"),
    full: bool = Query(False, description="文件纠错时额外回全文分段与页面高亮框（结果页链接几路都会给）"),
):
    """原有接口：行为保持不变——文本、文件、文件链接都按它自己那套链路走，跟有没有配
    外部解析服务无关。

    文本走 `content`、文件走 `file`、文件链接走 `fileUrl`（都是表单字段 / Multipart），
    三个都给时以 `file` 为先、其次是 `fileUrl`；`fileUrl` 由我们下载回来，再走 `file` 那条链路。

    要走外部解析服务（链接原样转交、上传的文件交给它解析）用
    `POST /openapi/contract-audit/web-correct-DocParser`。

    几路都会把这次结果存成一条纠错记录，响应里的 `url` 就是能直接打开的结果页链接
    （`record_id` 是那条记录的 id）。

    `type` 标明这次走的是哪条：`file` 时 `results` 是逐段、逐格的纠错差异（带 `?full=true`
    就多回全文分段和高亮框），`text` 时是逐字结果（`original_text` / `correct_text` / `errors`）。
    """
    return await _run_web_correct(
        request, content=content, file=file, file_url=fileUrl, full=full, use_parser=False
    )


@app.post("/openapi/contract-audit/web-correct-DocParser")
async def web_correct_docparser(
    request: Request,
    fileUrl: str | None = Form(None, description="待纠错文件的链接（http/https）。配了解析服务时原样转交给它"),
    full: bool = Query(False, description="额外回全文分段与页面高亮框（结果页链接照常给）"),
):
    """新接口：只收文件链接（`fileUrl`），配了解析服务时原样转交。

    转交就是转交：我们不下载、不转格式，它认不认这个链接是它的事——它报什么错（比如「这个
    链接不是 PDF」）就原样回给调用方，HTTP 状态码按它 `errorCode` 的段位来（`4xxxx` 回
    `400`、其余回 `500`）。等解析跑通了，才为了结果页去把原件取回来。

    文本纠错、上传文件走原接口 `/openapi/contract-audit/web-correct`。
    """
    if not (fileUrl and fileUrl.strip()):
        raise HTTPException(status_code=400, detail="fileUrl（文件链接）必须给一个")
    return await _run_web_correct(
        request, content=None, file=None, file_url=fileUrl, full=full, use_parser=True
    )


# ── 合同字段审查 ──

@app.post("/review_contract")
async def review_contract_endpoint(file: UploadFile = File(...)):
    log_info("request.review_contract", filename=file.filename)
    try:
        from correction.contract import parse_contract
        from correction.agents import init_contract_review, run_contract_review

        import tempfile
        file_path = os.path.join(tempfile.gettempdir(), file.filename)
        with open(file_path, "wb") as f:
            f.write(file.file.read())

        fields, all_paragraphs = parse_contract(file_path)
        log_info("request.review_contract.parsed", fields=len(fields), paragraphs=len(all_paragraphs))

        init_contract_review()

        total = len(fields)
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def _review_one(fi: int, field):
            para_idx = field.paragraph_index
            if para_idx >= 0:
                prev_para = all_paragraphs[para_idx - 1] if para_idx > 0 else ""
                para_text = all_paragraphs[para_idx] if para_idx < len(all_paragraphs) else ""
                next_para = all_paragraphs[para_idx + 1] if para_idx < len(all_paragraphs) - 1 else ""
            else:
                prev_para = ""
                para_text = field.full_paragraph
                next_para = ""

            async with sem:
                log_info("request.review_contract.field", index=f"{fi+1}/{total}", field=field.field_name, type=field.field_type)
                review_result = await run_contract_review(
                    field_name=field.field_name,
                    field_type=field.field_type,
                    paragraph_text=para_text,
                    prev_paragraph=prev_para,
                    next_paragraph=next_para,
                )

            review_result["field_index"] = fi
            review_result["field_name"] = field.field_name
            review_result["field_type"] = field.field_type
            review_result["paragraph_index"] = para_idx
            if field.table_index is not None:
                review_result["table_location"] = f"表格{field.table_index}行{field.table_row}列{field.table_col}"
            return review_result

        results = await asyncio.gather(
            *(_review_one(fi, field) for fi, field in enumerate(fields)),
            return_exceptions=False,
        )
        results.sort(key=lambda r: r["field_index"])

        log_info("request.review_contract.done", total=len(results))
        return success_response(results)
    except HTTPException:
        raise
    except Exception as exc:
        log_error("request.review_contract.fail", exc=exc, filename=file.filename)
        raise HTTPException(status_code=500, detail="合同审查任务失败") from exc
