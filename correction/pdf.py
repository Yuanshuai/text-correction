"""PDF 文件解析：按阅读顺序提取段落和表格单元格。

PyMuPDF 底下是 MuPDF（C 实现）：一份 75 页的合同，把所有字符读出来只要几十毫秒，
比纯 Python 的 pdfminer 快两个数量级。之后剩下的耗时几乎全在表格识别上——MuPDF 的表格识别
是 Python 写的，要逐页扫矢量线条、重建字符表，同一份合同约 1.7 秒，所以按页切块丢给多个
进程并行解析。逐页解析是确定的、跟别的页没关系，并行结果和单进程逐页跑完全一致。
"""

import re
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import pymupdf

from correction.config import PDF_PARSE_WORKERS
from correction.utils.log import log_info, log_warning
from correction.utils.text import SENTENCE_END

# 这个库推荐它的版面分析包的提示跟我们无关，关掉免得子进程刷日志
pymupdf.no_recommend_layout()


# 第一条最后那个「页」是可选的：整行并到一起之后才看得出，有些文档的页脚只排到「第 1 页 共 26」，
# 末尾那个「页」根本不是文字（是图片）。
footer_text_patterns = [
    r'^第\s*\d+\s*页(?:\s*共\s*[\[【]?\d+[\]】]?\s*页?)?$',
    r'^\d+\s*/\s*\d+$',
    r'^[—-]\s*\d+\s*[—-]$',
    r'^\d+\s+\d+$',
    r'^\d+$',
]
footer_patterns = [re.compile(pattern) for pattern in footer_text_patterns]


def is_footer(text: str) -> bool:
    for pattern in footer_patterns:
        if pattern.match(text):
            return True
    return False


def is_directory_line(line: str) -> bool:
    return re.search(r'\.{3,}', line) is not None


def filter_tables(table: str) -> bool:
    if table == "" or table is None:
        return True
    if re.match(r'^\d+$', table):
        return True
    punctuation_pattern = r'^[!"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~，。！？【】《》（）％＃＠……&～·；：‘’“”、|]+$'
    if re.match(punctuation_pattern, table):
        return True
    mixed_pattern = r'^[\d!"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~，。！？【】《》（）％＃＠……&～·；：‘’“”、|]+$'
    if re.match(mixed_pattern, table):
        return True
    return False


def cell_text(value: str | None) -> str:
    """单元格文字：去掉排版换行，让内容连成一行。

    窄表头里一个格子常常一个字占一行（表头「规\n格\n形\n号」）。照原样送进模型，它看到的
    是一竖列单字，认不出那是一个词——实测「规格形号」在换行版里纠不出来，连成一行立刻纠出来。
    结果页框的是「第几个字」，所以送模型的文字和逐字方框必须用同一份文字，归一放在这儿。
    """
    return _CJK_GAP.sub("", (value or "").replace("\r", "").replace("\n", ""))


def is_correction_cell(text: str) -> bool:
    """这一格要不要送去纠错：去掉首尾空白后不足 2 个字、纯数字、纯标点都不送。

    结果页展示单元格时用的是同一个判据——两边判据必须一致，否则某格被跳过、
    纠错结果却还留着，后面所有格子就会整体错位。
    """
    cell = (text or "").strip()
    if len(cell) < 2:
        return False
    return not filter_tables(cell)


def _table_grid(
    table_data: list[list],
    table_index: int,
    rects: list[list] | None = None,
    boxes: list[list] | None = None,
) -> list[dict]:
    """把一张表摊成带合并信息的单元格块（按行、按列）。

    extract() 里被合并盖住的槽位是 None：同一行左边已经有格子的，是横向合并（colspan）；
    左边一个都没有的，是被上面的格子竖着盖住（rowspan）。这些合并关系不还原出来，
    表格在结果页里就会散架——所以每个格子都带上 table / row / col / colspan / rowspan。
    被盖住的槽位本身不单独成块（它的文字属于盖住它的那一格）。

    table_index 是表号。并行解析时子进程只知道自己在这一页里排第几，全篇表号由主进程改。

    rects 是这张表逐格的方框（跟 extract() 的格子一一对应），带上它结果页才能把改动
    框在对应的那一格上；取不到就留空，展示时退回「只标格子内容」。boxes 是每格里逐字的
    方框（同样的下标），改动落在格内第几个字就靠它框出来。
    """
    rows = len(table_data)
    width = max((len(row) for row in table_data), default=0)
    if not rows or not width:
        return []

    owner: list[list] = [[None] * width for _ in range(rows)]
    for r in range(rows):
        row = table_data[r]
        for c in range(width):
            value = row[c] if c < len(row) else None
            if value is not None:
                owner[r][c] = (r, c)
                continue
            left = None
            for c2 in range(c - 1, -1, -1):
                if owner[r][c2] is not None:
                    left = owner[r][c2]
                    break
            if left is not None and left[0] == r:
                owner[r][c] = left
                continue
            up = None
            for r2 in range(r - 1, -1, -1):
                if owner[r2][c] is not None:
                    up = owner[r2][c]
                    break
            owner[r][c] = up if up is not None else (r, c)

    blocks: list[dict] = []
    for r in range(rows):
        for c in range(width):
            if owner[r][c] != (r, c):
                continue
            colspan = 0
            while c + colspan < width and owner[r][c + colspan] == (r, c):
                colspan += 1
            rowspan = 0
            while r + rowspan < rows and owner[r + rowspan][c] == (r, c):
                rowspan += 1
            value = table_data[r][c] if c < len(table_data[r]) else None
            rect = None
            if rects is not None and r < len(rects):
                row_rects = rects[r]
                if c < len(row_rects) and row_rects[c] is not None:
                    rect = [float(v) for v in row_rects[c][:4]]
            char_boxes = None
            if rect is not None and boxes is not None and r < len(boxes) and c < len(boxes[r]):
                char_boxes = boxes[r][c]
            blocks.append({
                "type": "row",
                "text": cell_text(value).strip(),
                "table": table_index,
                "row": r,
                "col": c,
                "colspan": max(colspan, 1),
                "rowspan": max(rowspan, 1),
                "merged": False,
                "rect": rect,
                "char_boxes": char_boxes,
            })
    return blocks


# 同一个视觉行内，不同字体/字号/上下标给出的 word 顶边能差出 2.9 磅（页脚「第 1 页 共 26 页」里的
# 数字、缩进点、目录点线都是这样），而真正相邻两行的行距最小也有 3.09 磅。容差取在中间：
# 差 3 磅以内的算同一行，免得把一句话劈成两段喂给模型。
LINE_TOP_TOLERANCE = 3.0


def _words_to_lines(words: list[dict], top_tolerance: float = LINE_TOP_TOLERANCE) -> list[list[dict]]:
    """将 word 按 y 坐标归并为行（左到右）。"""
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = [[sorted_words[0]]]
    for w in sorted_words[1:]:
        prev = lines[-1][-1]
        if abs(w["top"] - prev["top"]) <= top_tolerance:
            lines[-1].append(w)
        else:
            lines.append([w])
    # 每行内按 x0 排序
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


#: 汉字范围（含扩展 A 与兼容区）：用来认「排版塞在两个字中间的空格」。
#: 这种空格是表格/表单为了对齐撑出来的（「账 号：」「手 机：」），不是词与词的分隔，
#: 留在正文里，一个词在模型眼里就成了两个——实测「电子信 厢：」纠不出来，连起来立刻纠出来。
CJK_RANGE = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_GAP = re.compile(rf"(?<=[{CJK_RANGE}])[ \u3000]+(?=[{CJK_RANGE}])")


def _tighten_cjk_spaces(text: str, spans: list, boxes: list) -> tuple[str, list, list]:
    """吃掉夹在汉字之间的排版空格，方框和 span 起点跟着一起挪。

    只吃汉字与汉字之间的：汉字跟数字/字母之间的空格（「第 1 条」「型号：LYY 广」）保留，
    那是真的间隔，去掉会把两样东西粘成一个词。
    """
    drop = {index for match in _CJK_GAP.finditer(text) for index in range(match.start(), match.end())}
    if not drop:
        return text, spans, boxes
    shifted = [
        [start - sum(1 for index in drop if index < start), *rest]
        for start, *rest in spans
    ]
    return (
        "".join(char for index, char in enumerate(text) if index not in drop),
        shifted,
        [box for index, box in enumerate(boxes) if index not in drop],
    )


def _line_text_and_spans(words: list[dict]) -> tuple[str, list[list], list]:
    """把一行 word 拼成字符串，同时给每个字配上方框。

    返回 (这一行的文字, 每个 word 的 [起点下标, x0, top, x1, bottom], 每个字的方框)。

    拼字规则（间距超过 10 磅补一个空格）跟以前一样，空格也算进字符串，所以下标跟纠错
    结果里的 position 是同一个坐标系——结果页就是靠它把「第 5 个字错了」翻成「这一页
    哪一块」。每个字的方框跟文字一一对齐（我们自己补的空格没有对应字符，那一格是 None，
    真要知道它在哪儿再拿左右两边的字拼出来）。
    """
    if not words:
        return "", [], []
    words_sorted = sorted(words, key=lambda w: w["x0"])
    parts: list[str] = []
    spans: list[list] = []
    boxes: list = []
    offset = 0
    prev_x1 = words_sorted[0]["x0"]
    for w in words_sorted:
        gap = w["x0"] - prev_x1
        if gap > 10:
            parts.append(" ")
            boxes.append(None)
            offset += 1
        spans.append([offset, w["x0"], w["top"], w["x1"], w["bottom"]])
        parts.append(w["text"])
        word_boxes = w.get("char_boxes")
        if not word_boxes or len(word_boxes) != len(w["text"]):
            word_boxes = [None] * len(w["text"])
        boxes.extend(word_boxes)
        offset += len(w["text"])
        prev_x1 = w["x1"]
    return "".join(parts), spans, boxes


def _trim_line(text: str, spans: list[list], boxes: list) -> tuple[str, list[list], list]:
    """去掉整行首尾的空白，并把 span 的起点跟着挪，保证下标仍然对得上。

    正常情况下拼出来的一行首尾不会有空白（空 word 本来就少），这是兜底：真出现时
    宁可丢掉卡在首尾那几个零星 word，也不能让整行所有改动的位置整体偏一位。
    """
    stripped = text.strip()
    if stripped == text:
        return text, spans, boxes
    lead = len(text) - len(text.lstrip())
    tail = len(text.rstrip())
    kept = [
        [start - lead, x0, top, x1, bottom]
        for start, x0, top, x1, bottom in spans
        if lead <= start < tail
    ]
    return stripped, kept, boxes[lead:tail]


def _line_to_text(words: list[dict]) -> str:
    """将一行 word 拼成字符串，word 间根据水平间距补空格。"""
    return _line_text_and_spans(words)[0]


#: 合并成段：到句末且已经够长就断开，超了上限也断。
PARA_MIN_CHARS = 120
PARA_MAX_CHARS = 400

#: 断句符：句末（readtool 那套）之外再补冒号——「字段名：」这种一行一条的表单，
#: 冒号结尾说明这行本身就是完整的，不该跟下一行的字段并成一段。
PARA_END = SENTENCE_END + "：:"


def _mergeable(paragraph: str) -> bool:
    if len(paragraph) >= PARA_MAX_CHARS:
        return False
    if len(paragraph) >= PARA_MIN_CHARS and paragraph[-1] in PARA_END:
        return False
    return True


def _table_between(table_tops: list[float], top: float, next_top: float) -> bool:
    return any(top < table_top < next_top for table_top in table_tops)


def _merge_lines(lines: list[dict], tables: list[dict]) -> list[dict]:
    """把被排版切开的连续行并回一段。

    PDF 里一句话常被排成好几行（…「卖方应积极配和并提」/「供抽捡所需的姿料…」）。逐行
    当一段送去纠错，模型看到的每段都是半句，跨行的错字串不起来——实测同一段文字，整句
    送 15 处全检出，拆成视觉行送一处都检不出。所以按「到句末且够长就断」把连续的行并回
    段，并出来的段不再含半句；逐字方框跟着并、下标一一对应，结果页照旧框在原字上。

    表格夹在两行之间的（页面里表格和正文按纵向位置交错）就在那儿断开，不把表格前后的
    正文并成一段；跨页也不并——方框不带页码，一段跨页高亮就贴错页了。
    """
    merged: list[dict] = []
    current: dict | None = None
    table_tops = sorted(table["top"] for table in tables)

    for line in lines:
        if current is not None and (
            _table_between(table_tops, current["last_top"], line["top"])
            or not _mergeable(current["text"])
        ):
            merged.append(current)
            current = None
        if current is None:
            current = {
                "top": line["top"],
                "last_top": line["top"],
                "text": line["text"],
                "spans": [list(span) for span in line["spans"]],
                "boxes": list(line["boxes"]),
            }
            continue
        offset = len(current["text"])
        current["text"] += line["text"]
        current["spans"].extend([span[0] + offset, *span[1:]] for span in line["spans"])
        current["boxes"].extend(line["boxes"])
        current["last_top"] = line["top"]
    if current is not None:
        merged.append(current)
    return merged


def _page_chars(page) -> tuple[list[str], list[list]]:
    """这一页的字符流：每个字符 + 它的方框，跳过半角空格。

    跳过空格是为了跟 get_text("words") 的分词口径一致（两者都是按内容流顺序出来的，
    实测整份语料——含扫描件转的 PDF、带水印的 PDF——逐页都能一个字一个字对上），
    这样「段里第几个字」才能一对一落到页面的方框上。
    """
    texts: list[str] = []
    boxes: list[list] = []
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if char["c"] == " ":
                        continue
                    x0, top, x1, bottom = char["bbox"][:4]
                    texts.append(char["c"])
                    boxes.append([round(x0, 1), round(top, 1), round(x1, 1), round(bottom, 1)])
    return texts, boxes


def _word_char_ranges(texts: list[str], char_texts: list[str]) -> list[list] | None:
    """每个词占字符流里的哪一段（[起点, 终点)）。

    两个序列同序，顺序走一遍即可；对不上（字体缺字、编码怪）就返回 None，那一页干脆
    不出方框——宁可没有高亮，也不能把框标到别的字上。
    """
    ranges: list[list] = []
    cursor = 0
    for word in texts:
        start = -1
        for offset, char in enumerate(word):
            if cursor >= len(char_texts) or char_texts[cursor] != char:
                return None
            if offset == 0:
                start = cursor
            cursor += 1
        if start < 0:
            return None
        ranges.append([start, cursor])
    return ranges


def _table_cell_rects(table) -> list[list]:
    """一张表逐格的方框：跟 extract() 的格子一一对应，被合并盖住的槽位是 None。

    MuPDF 的 rows[i].cells[j] 和 extract() 的 [i][j] 是同一套下标（75 页那份合同 550 格
    逐格比过，合并格子的 None 位置完全一致），所以合并关系和方框能对上号。
    """
    rects: list[list] = []
    for row in getattr(table, "rows", None) or []:
        rects.append([
            None if cell is None else [float(v) for v in cell[:4]]
            for cell in row.cells
        ])
    return rects


def _rect_chars(chars: list[str], boxes: list[list], rect: list) -> tuple[list[str], list[list]]:
    """落在某个方框里的字符，按阅读顺序排（先上后下，同一行从左到右）。

    表格单元格的文字要用它来定位：extract() 只给文字不给坐标，而页面的字符流里每个字都
    带着方框，中心点落在格子里的就是这一格的字。
    """
    x0, top, x1, bottom = rect[:4]
    picked = [
        (char, box) for char, box in zip(chars, boxes)
        if x0 <= (box[0] + box[2]) / 2 <= x1 and top <= (box[1] + box[3]) / 2 <= bottom
    ]
    if not picked:
        return [], []
    picked.sort(key=lambda item: item[1][1])
    lines: list[list[tuple[str, list]]] = [[picked[0]]]
    for item in picked[1:]:
        if abs(item[1][1] - lines[-1][0][1][1]) <= LINE_TOP_TOLERANCE:
            lines[-1].append(item)
        else:
            lines.append([item])
    ordered: list[tuple[str, list]] = []
    for line in lines:
        ordered.extend(sorted(line, key=lambda item: item[1][0]))
    return [char for char, _ in ordered], [box for _, box in ordered]


def _cell_char_boxes(text: str, chars: list[str], boxes: list[list]) -> list | None:
    """给一个单元格的每个字配上方框（空白没有对应字符，那一格是 None）。

    格子文字来自 extract()、坐标来自页面字符流，两边是同一批字，只是空白数可能不一样
    （半角空格、全角空格都可能出现），所以两边都按 isspace() 跳过空白再逐字对齐；对不上
    就返回 None，结果页退回「整格标注」。
    """
    if not text:
        return None
    kept = [(char, box) for char, box in zip(chars, boxes) if not char.isspace()]
    if [char for char, _ in kept] != [char for char in text if not char.isspace()]:
        return None
    result: list = []
    index = 0
    for char in text:
        if char.isspace():
            result.append(None)
        else:
            result.append(kept[index][1])
            index += 1
    return result


def _table_cell_boxes(table_data: list[list], rects: list[list], chars: list[str], boxes: list[list]) -> list[list]:
    """一张表逐格的逐字方框，下标跟 extract() 的格子一一对应，取不到的格子是 None。"""
    grid: list[list] = []
    for r, row in enumerate(table_data):
        row_rects = rects[r] if r < len(rects) else []
        row_boxes: list = []
        for c in range(len(row)):
            rect = row_rects[c] if c < len(row_rects) else None
            value = cell_text(row[c] or "")
            if rect is None or not value:
                row_boxes.append(None)
                continue
            cell_chars, cell_boxes = _rect_chars(chars, boxes, rect)
            cell = _cell_char_boxes(value, cell_chars, cell_boxes)
            if cell is not None:
                # 格子文字在块里是 strip() 过的，方框要跟着切掉首尾，两边下标才对齐
                lead = len(value) - len(value.lstrip())
                cell = cell[lead:lead + len(value.strip())]
            row_boxes.append(cell)
        grid.append(row_boxes)
    return grid


def _parse_pages(pdf_path: str, first: int, last: int) -> list[dict]:
    """解析 [first, last) 这些页，交出每页的原始素材。

    单独成一个函数是为了能整块丢给子进程：每个进程自己开一次 PDF、只管自己那几页。
    交出去的是中间结果——「这一页第一行是不是页眉」要看它全篇出现几次才作数，只有主进程
    知道，所以这里只把候选报上去；表格也只按页内序号编号，全篇表号同样归主进程。
    """
    pages_data: list[dict] = []
    with pymupdf.open(pdf_path) as doc:
        for index in range(first, last):
            page = doc[index]

            # 页眉候选：这一页的第一行。原文只在 text 非空时才记，第一行本身可能是空串，
            # 这个细节照搬，免得页眉集合跟改动前对不上。
            text = page.get_text()
            header = text.split("\n")[0].strip() if text else None

            # find_tables() 只认矢量线条，不靠文字位置猜表；出岔子时会返回 None，
            # 这里当没有表处理
            finder = page.find_tables()
            tables = finder.tables if finder is not None else []
            table_bboxes = [table.bbox for table in tables]
            table_items: list[dict] = []
            # 页面本身被旋转过的，取出来的坐标跟浏览器里那套 PDF 坐标不是一回事，
            # 索性一个方框都不带（只影响高亮，不影响文字和纠错）
            rotated = page.rotation != 0
            # 这一页的字符流（每个字 + 方框）。页面被旋转过时取出来的坐标跟浏览器里那套
            # 不是一回事，索性一个方框都不带。
            char_texts, char_boxes = ([], []) if rotated else _page_chars(page)

            for table in tables:
                # 一格文字都没有的表 extract() 会给 None，归一成空表，表号才不会错位
                table_data = table.extract() or []
                rects = [] if rotated else _table_cell_rects(table)
                cell_boxes = _table_cell_boxes(table_data, rects, char_texts, char_boxes)
                table_items.append({
                    "data": table_data,
                    "top": table.bbox[1],
                    "blocks": _table_grid(table_data, len(table_items), rects, cell_boxes),
                })

            # 表格里的字已经整格进 all_tables 了，正文就不要再重复算一遍
            # get_text("words") 给的是 (x0, top, x1, bottom, 文字, ...) 的元组，先摆成字典
            raw_words = page.get_text("words")
            # 每个词占字符流里的哪一段：必须拿**全量**的词去对——字符流里表格的字也在，
            # 只拿正文那几个词去对，表格那几百个字一进来就整体错位了。对上了再按表格范围过滤。
            ranges = None if rotated else _word_char_ranges([word[4] for word in raw_words], char_texts)
            words = []
            for word_index, word in enumerate(raw_words):
                x0, top, x1, bottom = word[0], word[1], word[2], word[3]
                in_table = False
                for bbox in table_bboxes:
                    bx0, btop, bx1, bbottom = bbox
                    if x0 >= bx0 and x1 <= bx1 and top >= btop and bottom <= bbottom:
                        in_table = True
                        break
                if in_table:
                    continue
                item = {"x0": x0, "x1": x1, "top": top, "bottom": bottom, "text": word[4]}
                if ranges is not None:
                    start, end = ranges[word_index]
                    item["char_boxes"] = char_boxes[start:end]
                words.append(item)

            # 每行独立成段；页脚、目录行在这儿就剔掉，页眉得等全篇次数算出来，留给主进程
            lines: list[dict] = []
            for line_words in _words_to_lines(words):
                line_text, spans, boxes = _trim_line(*_line_text_and_spans(line_words))
                if not line_text:
                    continue
                if is_footer(line_text):
                    continue
                if is_directory_line(line_text):
                    continue
                # 页眉/页脚/目录行都判完了（判据看的是原样的行），再收拾排版空格：
                # 「账 号：」这种缝去掉之后模型才看得到一个整词
                line_text, spans, boxes = _tighten_cjk_spaces(line_text, spans, boxes)
                lines.append({
                    "top": min(w["top"] for w in line_words),
                    "text": line_text,
                    "spans": [] if rotated else spans,
                    "boxes": [] if rotated else boxes,
                })

            pages_data.append({
                "page": index,
                "header": header,
                "tables": table_items,
                "lines": lines,
            })
    return pages_data


def _parse_chunk(job: tuple[str, int, int]) -> list[dict]:
    """进程池的入口，必须留在模块级：Windows 上子进程要靠这个名字重新 import 它。"""
    pdf_path, first, last = job
    return _parse_pages(pdf_path, first, last)


_parse_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    """进程池只建一次，之后所有请求共用。

    子进程启动要各自 import 一遍 pymupdf，每份文档重建一个池的话，
    省下来的时间又还回去了。
    """
    global _parse_pool
    if _parse_pool is None:
        _parse_pool = ProcessPoolExecutor(max_workers=PDF_PARSE_WORKERS)
    return _parse_pool


def _worker_count(page_count: int) -> int:
    """这次解析开几个进程。页数太少摊不开进程开销，直接单进程跑。"""
    if PDF_PARSE_WORKERS < 2 or page_count < 4:
        return 1
    return min(PDF_PARSE_WORKERS, page_count)


def _warm_worker(_: int) -> int:
    """预热用的空活：只为了让子进程真的起来、把 pymupdf 导入完。"""
    return 0


def warm_parse_pool() -> None:
    """提前把解析用的进程池建起来（服务启动时调一次）。

    池是第一次真解析时才建的，而建池要现开子进程、子进程还要 import 一遍 pymupdf：实测
    第一个子进程 0.47 秒，后面的并行开出来一共只要 0.04 秒（进口的东西 OS 已经缓存了）。
    这笔开销本来会整个算进第一份文档的「解析」耗时，所以启动时先开一个——大头就没了，
    只留一个进程占着内存，剩下的等真要用时再开也不花时间。
    """
    try:
        list(_get_pool().map(_warm_worker, range(1)))
    except Exception as exc:
        # 热不起来不影响功能：第一次真解析照样会建池，只是那一份文档多等一会儿
        log_warning("pdf.parse.warm_failed", exc=exc)


def shutdown_parse_pool() -> None:
    """收工：把解析进程池关掉，子进程跟着退出。

    不关的话子进程会一直留着——服务每重启一次就多攒一批（实测攒到过上百个），
    每个都占着几十兆。
    """
    global _parse_pool
    if _parse_pool is not None:
        _parse_pool.shutdown(wait=False, cancel_futures=True)
        _parse_pool = None
        log_info("pdf.parse.pool_closed")


def _scan_pages(pdf_path: str, page_count: int) -> list[dict]:
    """把整份 PDF 解析成逐页素材：能并行就并行，并行不了就单进程按顺序跑。"""
    workers = _worker_count(page_count)
    if workers == 1:
        return _parse_pages(pdf_path, 0, page_count)

    # 块数比进程多几倍：页与页的耗时差很多（几十毫秒到几百毫秒），块切小一点才摊得平，
    # 谁先干完谁接下一块
    chunk = max(1, page_count // (workers * 4))
    jobs = [(pdf_path, start, min(start + chunk, page_count)) for start in range(0, page_count, chunk)]
    try:
        # map 按 chunks 的顺序回结果，拼起来就是原始页序
        return [page for chunk_pages in _get_pool().map(_parse_chunk, jobs) for page in chunk_pages]
    except Exception as exc:
        # 子进程崩了（内存不够之类）不能让整份文档解析失败：退回单进程照样给出结果
        log_warning("pdf.parse.parallel_failed", exc=exc, pages=page_count)
        return _parse_pages(pdf_path, 0, page_count)


def filter_text_and_tables(pdf_path: str) -> tuple[list[str], list, list[dict]]:
    """按阅读顺序提取段落，返回 (paragraphs, tables, blocks)。

    paragraphs：list[str]，每个元素是一个段落文本，按阅读顺序排列。
    tables：原始表格数据。
    blocks：按阅读顺序的 [{"type": "p"|"row", "text": ..., "page": 页码, ...}]，段落和
            表格单元格交错，给结果页展示整篇文档用。表格按单元格成块（纠错就是一个单元格
            一个单元），每格额外带 table / row / col / colspan / rowspan 和自己在页面上的
            方框 rect，结果页据此把表格拼回原样、并把改动框在对应的格子上。
            段落还带 spans：每个词在这一段文字里的起点和方框，boxes：每个字在页面上的方框
            （空格没有对应字符，那一格是 None），结果页靠它们把「第几个字」翻成「页面上哪一块」。
            页面被旋转过时 spans / boxes / rect 一律为空。
    """
    # 先只数页数：真正的解析交给 _scan_pages，那儿能按页并行
    with pymupdf.open(pdf_path) as doc:
        page_count = doc.page_count
    pages_data = _scan_pages(pdf_path, page_count)

    # 页眉：全篇第一行出现过两次以上的，才当成页眉剔掉
    header_counter = Counter(
        page["header"] for page in pages_data if page["header"] is not None
    )
    headers = {line for line, count in header_counter.items() if count >= 2}

    all_paragraphs: list[str] = []
    all_tables = []
    all_blocks: list[dict] = []

    for page in pages_data:
        # 这一页的段落和表格按纵向位置排成阅读顺序；
        # 页与页之间本来就是顺序的，所以只在页内排一次就够。
        page_blocks: list[tuple[float, dict]] = []

        for item in page["tables"]:
            # 表号按全篇顺序排（跨表唯一，结果页靠它把格子拼回各自的表）
            table_index = len(all_tables)
            all_tables.append(item["data"])
            for block in item["blocks"]:
                block["table"] = table_index
                block["page"] = page["page"]
                page_blocks.append((item["top"], block))

        # 排版把一句话切成好几行的，先并回完整的一段再送纠错（见 _merge_lines）；
        # 页眉是整行剔掉的，并段只并过滤之后的正文行。
        page_lines = [line for line in page["lines"] if line["text"] not in headers]
        for paragraph in _merge_lines(page_lines, page["tables"]):
            all_paragraphs.append(paragraph["text"])
            page_blocks.append((paragraph["top"], {
                "type": "p",
                "text": paragraph["text"],
                "page": page["page"],
                "spans": paragraph["spans"],
                "boxes": paragraph["boxes"],
            }))

        page_blocks.sort(key=lambda item: item[0])
        all_blocks.extend(block for _, block in page_blocks)

    return all_paragraphs, all_tables, all_blocks
