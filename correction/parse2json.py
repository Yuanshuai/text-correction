"""外部文档解析（DocParser 的 /async/parse2json）：把「原文 → 段落 + 表格单元格」那段外包出去。

本地那份解析（correction/pdf.py，PyMuPDF）快、离线、没有外部依赖，但扫描件没有文字层、
没有框线的表格也只能靠文字位置猜。DocParser 那套是版面模型 + OCR：块按阅读顺序出、
表格带行列与合并信息、扫描件也能认字；代价是原文得有一个它能自己下载到的地址（见 file_url）。

这里把它的输出翻译成内部那三份（paragraphs / tables / blocks，形状跟
pdf.filter_text_and_tables 完全一致），所以 correct_file 之后那条链路（展平单元格、送纠错、
拼结果页）一个字都不用改。

**配了它就是唯一那条路**：没配能下载的地址、下载失败、服务报错、等超时——一律抛 ParseError 报给
调用方，不再偷偷换回本地解析。理由是两套解析器给的东西不一样（分块方式、表格行列、扫描件认不认字），
悄悄换一条，用户看到的纠错效果就跟着变，出了问题还查不出原因。想跑本地那套，就把
PARSE2JSON_BASE_URL 清掉、或者 PARSE2JSON_ENABLED=0，两条路各跑各的。

一种情况不算失败：解析服务跑通了、但这文档里一个可纠错的字都没有（没有文字层的扫描件、又没开
OCR）——那照实回三份空的，跟本地解析遇到同样文档的表现一致，是「没有东西可纠」。
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

import httpx

from correction import objects
from correction.config import (
    PARSE2JSON_BASE_URL, PARSE2JSON_ENABLED, PARSE2JSON_FILE_URL, PARSE2JSON_MAX_PAGES,
    PARSE2JSON_MAX_WAIT, PARSE2JSON_MAX_WAIT_MIN, PARSE2JSON_OCR, PARSE2JSON_POLL_INTERVAL,
    PARSE2JSON_TIMEOUT,
    PARSE2JSON_TOKEN, S3_PUBLIC_ENDPOINT,
)
# 汉字之间排版撑出来的空格怎么收拾、单元格文字怎么归一，本地解析那套已经写好了，这边照用，
# 两边对同一份文档必须收拾成一个样子
from correction.pdf import _tighten_cjk_spaces, cell_text
from correction.utils.log import log_info, log_warning

#: 提交和取结果是同一个路径：POST 提交，GET {路径}/{taskId} 取结果
SUBMIT_PATH = "/async/parse2json"

#: 信封里的 errorCode：102 表示任务还在跑（HTTP 状态码恒为 200，只能看它）
TASK_RUNNING = 102

#: 正文类块（带 text + lines）：一个块当一段送纠错，块与块之间就是阅读顺序。
#: image 块只有开了 PARSE2JSON_OCR 才有 OCR 文字，扫描件正是靠它。
#: 剩下的都不进纠错——header / footer / seal / header_image / footer_image 是版面装饰，
#: catalog 整块是点线页码（本地解析也是整行剔掉的，见 pdf.is_directory_line），formula 不是
#: 文字错别字的活。
TEXT_TYPES = {"title", "text", "reference", "image"}


class ParseError(RuntimeError):
    """解析服务报的错（或等超时）。带上它的 errorCode，日志里一眼能看出是 License 还是页数超限。"""

    def __init__(self, message: str, code: int = 0):
        super().__init__(f"{message}（errorCode={code}）" if code else message)
        self.code = code


def available() -> bool:
    """配了解析服务地址、开关也没关，才走外部解析。"""
    return bool(PARSE2JSON_ENABLED and PARSE2JSON_BASE_URL)


def file_url(doc_id: str | None) -> str | None:
    """给解析服务下载原文的地址；拿不到就返回 None（调用方按「配置没配全」直接报错）。

    解析服务是自己按 URL 去下载的，我们手上这份本机文件它拿不到。优先用对象存储的签名地址
    （配了 S3_PUBLIC_ENDPOINT 说明那个地址外面也够得着），否则按 PARSE2JSON_FILE_URL 这个
    模板拼本站的 /document/<id>。
    """
    if not doc_id:
        return None
    if S3_PUBLIC_ENDPOINT and objects.available():
        presigned = objects.presigned_url(doc_id)
        if presigned:
            return presigned
    if PARSE2JSON_FILE_URL:
        return PARSE2JSON_FILE_URL.format(doc_id=doc_id)
    return None


def _headers() -> dict:
    return {"X-Token": PARSE2JSON_TOKEN} if PARSE2JSON_TOKEN else {}


def _envelope(response: httpx.Response) -> dict:
    """拆信封：这个服务的 HTTP 状态码恒为 200，成败都在 errorCode 里。"""
    if response.status_code != 200:
        raise ParseError(f"解析服务回了 HTTP {response.status_code}", code=response.status_code)
    try:
        payload = response.json()
    except ValueError:
        raise ParseError("解析服务回的不是 JSON") from None
    if not isinstance(payload, dict):
        raise ParseError("解析服务回的信封不是对象")
    try:
        code = int(payload.get("errorCode") or 0)
    except (TypeError, ValueError):
        raise ParseError(f"解析服务的 errorCode 不是数字：{payload.get('errorCode')!r}") from None
    if code not in (0, TASK_RUNNING):
        data = payload.get("data")
        detail = data.get("detail") if isinstance(data, dict) else None
        raise ParseError(str(detail or payload.get("message") or "解析失败"), code=code)
    return payload


async def _submit(client: httpx.AsyncClient, target: str) -> str:
    body: dict = {"fileUrl": target, "maxPages": PARSE2JSON_MAX_PAGES}
    if PARSE2JSON_OCR:
        body["enableImageBlockOCR"] = True
    response = await client.post(
        f"{PARSE2JSON_BASE_URL}{SUBMIT_PATH}", json=body, headers=_headers()
    )
    data = _envelope(response).get("data")
    task_id = data.get("taskId") if isinstance(data, dict) else None
    if not task_id:
        raise ParseError("解析服务没回 taskId")
    return str(task_id)


async def _poll(client: httpx.AsyncClient, task_id: str) -> dict:
    url = f"{PARSE2JSON_BASE_URL}{SUBMIT_PATH}/{task_id}"
    deadline = time.monotonic() + PARSE2JSON_MAX_WAIT
    while True:
        data = _envelope(await client.get(url, headers=_headers())).get("data")
        if isinstance(data, dict):
            return data
        # 102「还在跑」就接着等；等到超时为止（默认一小时，见 PARSE2JSON_MAX_WAIT_MIN）。
        # 卡住的文档宁可报错，也不能把一次纠错请求无限挂着（前端那边就是干等）。
        if time.monotonic() >= deadline:
            raise ParseError(f"等解析结果超过 {PARSE2JSON_MAX_WAIT_MIN} 分钟")
        await asyncio.sleep(PARSE2JSON_POLL_INTERVAL)


async def parse_units(target: str, filename: str | None = None):
    """跑一趟外部解析，返回 (paragraphs, tables, blocks)。

    配了解析服务，这条路就是唯一的：连不上、服务报错、等超时一律抛 ParseError 让调用方报给用户，
    不退回本地解析（理由见模块开头）。解析服务跑通、但一个可纠错的字都没有时回三份空的——那是
    「没有东西可纠」，不是解析失败。要跑本地那套就把解析服务关掉。
    """
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=PARSE2JSON_TIMEOUT) as client:
            payload = await _poll(client, await _submit(client, target))
    except ParseError as exc:
        log_warning("parse2json.failed", exc=str(exc), filename=filename, file_url=target)
        raise
    except Exception as exc:
        log_warning("parse2json.failed", exc=str(exc), filename=filename, file_url=target)
        raise ParseError(f"连不上解析服务：{exc}") from None
    units = to_units(payload)
    if units is None:
        log_warning(
            "parse2json.empty", filename=filename,
            pages=len(payload.get("pages") or []), blocks=len(payload.get("blocks") or []),
        )
        return [], [], [], {}
    log_info(
        "parse2json.done", filename=filename, paragraphs=len(units[0]), tables=len(units[1]),
        blocks=len(units[2]), ms=int((time.perf_counter() - started) * 1000),
    )
    return units


def _box(raw) -> list[float] | None:
    """坐标统一成 [x1, y1, x2, y2] 浮点：这个服务里块和单元格是 float，字符是 int。"""
    if not raw or len(raw) < 4:
        return None
    try:
        return [float(value) for value in raw[:4]]
    except (TypeError, ValueError):
        return None


def _text_and_boxes(lines: list) -> tuple[str, list]:
    """把若干行拼成一段，并给出每个字在页面上的方框。

    行与行之间不补分隔（跟本地解析一个口径：中文排版的换行不是词的边界）；方框跟文字一格
    一格对齐——结果页就是靠它把「第几个字错了」翻成页面上哪一块。
    """
    parts: list[str] = []
    boxes: list = []
    for line in lines or []:
        for char in (line or {}).get("chars") or []:
            parts.append(char.get("c") or "")
            boxes.append(_box(char.get("b")))
    text, _, boxes = _tighten_cjk_spaces("".join(parts), [], boxes)
    lead = len(text) - len(text.lstrip())
    text = text.strip()
    return text, boxes[lead:lead + len(text)]


def _cell_grid(payload: dict) -> dict:
    """单元格 id -> 在表里的 (行, 列, 行跨度, 列跨度)。

    逐页的表格块只有文字和方框，行列与合并信息在「跨页合并后的逻辑块」里：那边的 MergedCell
    自己不带文字，靠 sourceCellIds 回链到逐页的 Cell，所以两边按 id 对起来。
    """
    grid: dict[str, tuple[int, int, int, int]] = {}
    for block in payload.get("blocks") or []:
        if (block or {}).get("blockType") != "table":
            continue
        for cell in block.get("cells") or []:
            position = (
                int(cell.get("row") or 0), int(cell.get("col") or 0),
                max(int(cell.get("rowspan") or 1), 1), max(int(cell.get("colspan") or 1), 1),
            )
            for cell_id in cell.get("sourceCellIds") or []:
                grid.setdefault(str(cell_id), position)
    return grid


def _grid_by_bbox(cells: list[dict]) -> list[tuple[int, int, int, int]]:
    """查不到合并信息时按方框推行列：上下有重叠的算同一行，行内按左边排序算第几列。

    合并关系推不出来，一律按 1 处理——表格照样能展示、能纠错，只是被合并的那几格会各自成格。
    """
    boxes = [tuple(_box(cell.get("bbox")) or (0.0, 0.0, 0.0, 0.0)) for cell in cells]
    rows: list[list[int]] = []
    for index in sorted(range(len(cells)), key=lambda i: (boxes[i][1], boxes[i][0])):
        box = boxes[index]
        for row in rows:
            top = max(boxes[i][1] for i in row)
            bottom = min(boxes[i][3] for i in row)
            height = min(bottom - top, box[3] - box[1])
            if height > 0 and min(bottom, box[3]) - max(top, box[1]) > height * 0.5:
                row.append(index)
                break
        else:
            rows.append([index])

    positions = [(0, 0, 1, 1)] * len(cells)
    for row_index, row in enumerate(rows):
        for col_index, index in enumerate(sorted(row, key=lambda i: boxes[i][0])):
            positions[index] = (row_index, col_index, 1, 1)
    return positions


def _table_cells(
    block: dict, table_index: int, grid_map: dict, page_index: int
) -> tuple[list[dict], list[list[str]]]:
    """一张逐页表格 -> (单元格块列表, 表格文字网格)。

    单元格块跟本地解析出来的一模一样（type / text / table / row / col / colspan / rowspan /
    page / rect / char_boxes），结果页拿它把一格一格重新拼回表格、把改动框在格里的字上。
    """
    cells = [cell for cell in (block.get("cells") or []) if isinstance(cell, dict)]
    positions = [grid_map.get(str(cell.get("id"))) for cell in cells]
    if any(position is None for position in positions):
        positions = _grid_by_bbox(cells)
    # 跨页的表在合并块里是「一整张」：第二页那半边的行号接着第一页往下数。结果页是一页一张
    # 表（跟本地解析一个口径），所以把行号拉回从 0 开始，不然一张表顶上会空出好几行。
    base_row = min((row for row, _, _, _ in positions), default=0)

    blocks: list[dict] = []
    texts: dict[tuple[int, int], str] = {}
    row_count = 0
    for cell, position in zip(cells, positions):
        row, col, rowspan, colspan = position
        row -= base_row
        text, boxes = _text_and_boxes(cell.get("lines") or [])
        if not text:
            # 这一格没回逐行/逐字信息：文字照用，只是框不了具体的字，展示时标整格
            text = cell_text(cell.get("text") or "").strip()
        row_count = max(row_count, row + 1)
        texts[(row, col)] = text
        blocks.append({
            "type": "row",
            "text": text,
            "table": table_index,
            "row": row,
            "col": col,
            "colspan": colspan,
            "rowspan": rowspan,
            "merged": False,
            "page": page_index,
            "rect": _box(cell.get("bbox")),
            "char_boxes": boxes or None,
        })

    width = max((col for _, col in texts), default=-1) + 1
    grid = [[texts.get((row, col), "") for col in range(width)] for row in range(row_count)]
    return blocks, grid


def to_units(payload: dict):
    """把解析服务的 data 翻成内部那三份 + 每页渲染图的尺寸；没内容可纠时返回 None。

    走的是逐页的原始块（带页码、每个字的方框），因为结果页要「原文件长什么样 + 改动标在
    哪一页哪一块」；跨页合并后的逻辑块只拿来补齐表格的行列与合并信息。

    第四份是 {页码: (图宽, 图高)}：这里出去的方框都是那个渲染图上的**像素**，不是 PDF 的磅
    （A4 一页它给 1190×1682，正好是 72dpi 的 2 倍）。调用方拿到原件后用页面尺寸换算，见
    rescale_to_points。
    """
    grid_map = _cell_grid(payload)
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []
    blocks: list[dict] = []
    page_images: dict[int, tuple[float, float]] = {}

    for position, page in enumerate(payload.get("pages") or []):
        index = (page or {}).get("pageIndex")
        page_index = position if index is None else int(index)
        width, height = (page or {}).get("imageWidth"), (page or {}).get("imageHeight")
        if width and height:
            page_images[page_index] = (float(width), float(height))
        for block in page.get("blocks") or []:
            kind = (block or {}).get("blockType")
            if kind not in TEXT_TYPES and kind != "table":
                continue
            if kind == "table":
                cells, grid = _table_cells(block, len(tables), grid_map, page_index)
                tables.append(grid)
                blocks.extend(cells)
                continue
            text, boxes = _text_and_boxes(block.get("lines") or [])
            if not text:
                text = cell_text(block.get("text") or "").strip()
                boxes = []
            if not text:
                continue
            paragraphs.append(text)
            blocks.append({
                "type": "p", "text": text, "page": page_index, "spans": [], "boxes": boxes,
            })

    if not paragraphs and not blocks:
        return None
    return paragraphs, tables, blocks, page_images


def _scale_box(box, ratio: tuple[float, float]):
    """一个方框按 (x 比例, y 比例) 缩一下；不是方框（None / 不成形状）就原样回。"""
    if not box or len(box) < 4:
        return box
    return [
        round(float(box[0]) * ratio[0], 2), round(float(box[1]) * ratio[1], 2),
        round(float(box[2]) * ratio[0], 2), round(float(box[3]) * ratio[1], 2),
    ]


def rescale_to_points(
    blocks: list[dict],
    page_images: dict[int, tuple[float, float]],
    page_sizes: dict[int, tuple[float, float]],
) -> int:
    """把方框从「解析服务渲染图上的像素」缩回 PDF 自己的坐标系（磅），返回缩了几个块。

    结果页是 PDF.js 按磅铺页、再把方框乘缩放贴上去的，所以框必须是磅。不缩回去，框就会大
    一圈（2 倍）、还整片偏到右下角去。这份 PDF 就在我们手上（结果页要铺的就是它），
    逐页「页宽 ÷ 图宽」一除就是比例；逐页算，文档里混着不同尺寸的页也不会错位。

    拿不到页面尺寸（或压根没有图尺寸）就什么都不动，返回 0——宁可维持原样，也不拿一个
    瞎猜的比例去缩。页面对不上时按最常见那个比例来，总比整页贴歪强。
    """
    if not blocks or not page_images or not page_sizes:
        return 0
    ratios: dict[int, tuple[float, float]] = {}
    for index, (width, height) in page_images.items():
        size = page_sizes.get(index)
        if size and width > 0 and height > 0:
            ratios[index] = (size[0] / width, size[1] / height)
    if not ratios:
        return 0
    fallback = Counter(ratios.values()).most_common(1)[0][0]
    scaled = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        ratio = ratios.get(block.get("page"), fallback)
        if abs(ratio[0] - 1.0) < 1e-9 and abs(ratio[1] - 1.0) < 1e-9:
            continue
        changed = False
        for key in ("boxes", "char_boxes"):
            boxes = block.get(key)
            if boxes:
                block[key] = [_scale_box(box, ratio) for box in boxes]
                changed = True
        if block.get("rect"):
            block["rect"] = _scale_box(block["rect"], ratio)
            changed = True
        if changed:
            scaled += 1
    return scaled
