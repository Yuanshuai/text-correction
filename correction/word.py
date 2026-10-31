"""Word 文件解析：提取段落和表格文本。"""

from docx import Document
from docx.oxml.ns import qn


def _text_of(element) -> str:
    """把一个元素里的 w:t 文字按文档顺序全取出来。

    不能用 paragraph.text：它只认 w:p 的直接子 run，内容控件（w:sdt）里的字全被漏掉。
    客户模板正是用内容控件填字段和贴条款——实测一份合同有 2159 个字藏在控件里，包括整条
    「第5条 价款支付…」和填入的「海男省三亚是」，模型从来没见过这些字。
    """
    return "".join(node.text or "" for node in element.iter(qn("w:t")))


def _cells_of(table) -> list[list[dict]]:
    """表格按 w:tr / w:tc 取每个单元格的文字，外加它在表格里的位置和合并信息。

    不用 row.cells：它会把合并单元格重复返回，一行里出现「一、需求单位: 一、需求单位:」
    这种假重复，模型会想去删。

    每个格子带 col（第几列）、colspan（w:gridSpan，横着并了几列）、rowspan（竖合并盖了
    几行）、merged（自己是不是竖合并的续格）。只有带上这些，结果页才拼得回一张表；
    纠错照旧是一个单元格一个单元。
    """
    grid: list[list[dict]] = []
    for row in table.findall(qn("w:tr")):
        cells: list[dict] = []
        for cell in row.findall(qn("w:tc")):
            colspan = 1
            vertical = None
            props = cell.find(qn("w:tcPr"))
            if props is not None:
                span = props.find(qn("w:gridSpan"))
                if span is not None:
                    colspan = int(span.get(qn("w:val")) or 1)
                merge = props.find(qn("w:vMerge"))
                if merge is not None:
                    vertical = merge.get(qn("w:val")) or "continue"
            cells.append({
                "text": _text_of(cell).strip(),
                "colspan": colspan,
                # 竖合并只有起始格带 val="restart"，跟着的都是续格
                "merged": vertical == "continue",
                "rowspan": 1,
            })
        grid.append(cells)

    for row in grid:
        col = 0
        for cell in row:
            cell["col"] = col
            col += cell["colspan"]

    for row_index, row in enumerate(grid):
        for cell in row:
            if cell["merged"]:
                continue
            span = 1
            for below in grid[row_index + 1:]:
                follower = next((c for c in below if c["col"] == cell["col"]), None)
                if follower is None or not follower["merged"]:
                    break
                span += 1
            cell["rowspan"] = span
    return grid


def _walk(container, paragraphs: list[str], tables: list[list[list[str]]], blocks: list[dict]) -> None:
    """按文档顺序走一遍正文：段落、表格，以及内容控件（w:sdt）里面的段落和表格。

    3 份产出各有用处：paragraphs / tables 是拿去纠错的单元，blocks 是同一批内容按
    **正文顺序**排的一份清单（段落、表格单元格交错），结果页展示整篇文档时按它来。
    表格按单元格送纠错（跟 PDF 那边一个口径），所以这里也是一个单元格一块。
    """
    for child in container:
        tag = child.tag
        if tag == qn("w:p"):
            text = _text_of(child).strip()
            if text:
                paragraphs.append(text)
                blocks.append({"type": "p", "text": text})
        elif tag == qn("w:tbl"):
            grid = _cells_of(child)
            tables.append([[cell["text"] for cell in row] for row in grid])
            for row_index, row in enumerate(grid):
                for cell in row:
                    blocks.append({
                        "type": "row",
                        "text": cell["text"],
                        "table": len(tables) - 1,
                        "row": row_index,
                        "col": cell["col"],
                        "colspan": cell["colspan"],
                        "rowspan": cell["rowspan"],
                        "merged": cell["merged"],
                    })
        elif tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                _walk(content, paragraphs, tables, blocks)


def filter_text_and_tables_from_docx(docx_path: str) -> tuple[list[str], list[list[list[str]]], list[dict]]:
    """解析 Word 文件，返回 (段落列表, 表格列表, 正文块列表)。

    paragraphs：list[str]，每个元素是一个段落文本（含内容控件里的段落）。
    tables：原始表格数据，结构为 table[row][col]。
    blocks：按正文顺序的 [{"type": "p"|"row", "text": ...}]，段落和表格单元格交错，
            给结果页展示整篇文档用；表格单元格还带 table / row / col / colspan / rowspan，
            结果页据此把表格拼回原样（纠错仍然是一个单元格一个单元）。
    """
    document = Document(docx_path)
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []
    blocks: list[dict] = []
    _walk(document.element.body, paragraphs, tables, blocks)
    return paragraphs, tables, blocks
