"""000.txt / 001.txt 往返式纠错引擎。

模型眼里的文档有两个版本：

    000.txt  原文，只读，任何时候都不变。错别字都在它里面。
    001.txt  工作副本：模型调用修改工具后，改动记在它上面。

模型用 read_text / find_text 自己决定看哪一段、看多少（服务端不再替它切块），
看完就改，改完继续往下读，认为结束了调用 finish。finish 不接受"我觉得读完了"：
它先检查 000.txt 是否每个字符都被读到过，没读全就把漏掉的区间打回去让它接着读。

模型不数任何东西：它写下的每一处改动都长成一段小补丁（形态照抄 Codex 的 apply_patch）——
`@@` 那行是照抄的引文，只用来定位；`-`/`+` 两行才是"哪儿错了、改成什么"。位置由服务端在
000.txt 里把引文搜出来，模型不提供任何位置。实测模型自己数字符下标会数偏（把 3444 报成
3430），让它"抄引文"就没有这个误差。

纠错结果不靠模型自报，而是把 000.txt 与 001.txt 逐字符比对得到：某位字符变了是
replace，某位字符没了是 delete。位置一律是 000.txt 的绝对下标——因为 000.txt 不变，
改动落地后位置不会漂移（这是逐行渲染 + 行内下标做不到的）。
"""

from __future__ import annotations

import difflib
import itertools
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from correction.chains import get_model
from correction.config import TOOL_MAX_TURNS
from correction.typo_types import classify, classify_delete, classify_insert
from correction.utils.log import log_info
from correction.utils.text import SENTENCE_END, is_ignorable_diff_char

logger = logging.getLogger(__name__)

#: 提示词里的文件名，必须和下面的常量一致。
ORIGINAL_FILE = "000.txt"
WORKING_FILE = "001.txt"

#: 模型写在正文里的改动块（形态照抄 Codex 的 apply_patch，去掉了 JSON 这一层）。
CORRECTION_BEGIN = "*** Begin Correction"
CORRECTION_END = "*** End Correction"

# read_text 一次最多返回多少字；不传 length 时用默认值。
READ_MAX = 2000
READ_DEFAULT = 1500
# 渲染窗口内的行：一行一句，超长句才按这个上限硬切。
RENDER_MAX_LINE = 200

# 引文长度上限：长引文靠"在原文里唯一"兜底，这里只是防止模型整段整段地抄。
EDIT_MAX = 120
# find_text 最多返回多少处命中。
FIND_LIMIT = 30
# find_text 能搜多长的词：要够长，模型才能用它自查引文唯不唯一。
FIND_QUERY_MAX = 60
# find_text 每处命中返回前后多少字的上下文。
FIND_CONTEXT = 16
# 一轮里最多把多少字的新内容摆到模型眼前。超过了就让它先处理这一轮读到的，
# 下一轮再继续读——这样"计入已读"的内容一定真的给模型看过，覆盖率不会撒谎。
TURN_READ_BUDGET = 4000
# 每轮回灌时最多列出多少条已改记录（避免提示词随轮次无限增长）。
EDIT_LOG_KEEP = 8

# 一块改动里有的落地、有的没过门时，说明里用这个分隔：分隔前是落地的，分隔后是没落地的。
SKIPPED_MARK = "；没落地的："

# 同一批「块写错了」的把 finish 打回几次（见 run_document_correction）。实测：真该改的那几处
# （引文抄错、引文不唯一）第一次打回就回去改对了；本来就改不了的（想删「正常」、想重编条款号
# 这类不是错别字的东西）打到第三次也只是白读两轮再 finish。所以只打回一次。
BACKLOG_MAX = 1

# 保底出口：连着这么多轮「既没读到新内容、也没落地新改动」就当卡住了，收工。正常收尾也会
# 有几轮在原地复查、重写，实测最多连着 3 轮，所以这里放宽到远大于它，只当最后兜底
# （「还没读完就想结束」那条死循环另有正解：见下面 want_finish 那段——直接把没读到的
# 内容摆到模型眼前，覆盖就一定往前走）。
STALL_MAX = 12

_PROMPT_RULES = """这是一场中文文档纠错比赛，你在给 {original_file} 挑错别字。评分按段算，一段一段地给分
（你看到的一行就是一段）：
  · 这一段里的错别字一个不落、全部找出来，同时一个字都没改错，这一段才得分；
  · 漏掉一个错别字，这一段不得分；
  · 改错一处——把没写错的字改掉、删掉或者补上——这一段也不得分。改错和漏掉一样重，都会让
    这一段作废；删和补尤其容易看走眼（那是把原文的字抹掉、或者凭空多出一个字），没有十足
    把握就别动。
所以读的时候一段都不能跳，逐字看完再往下走，这一段有几个错字就找出几个；动手的时候只有十足
把握才写，要删要补没有十足把握就别动。
判错的标准是"平常人从头读一遍会不会觉得别扭"，不是语法书：一句话意思清楚、读着通顺，就算
写法不够规范、不够书面、不够严谨，也不算错字，一个字都不要动。拿不准的一律不动。

算错别字的有这几类：同音字（读音相同，声调可同可异）、近音字（平翘舌不分、前后鼻音不分：
搜/收、一/银、身/生）、字母数字写混（O↔0 这种）、同一个字连着写了两个以上（卖方方仓库→
卖方仓库、款款款款→款）、多打了一个字（开户航行→开户行）、漏写了一个字（邮编码→
邮政编码）。读起来不顺、用词不够好、语气不合适、语序还能调得更顺，都不是错字；缺个"的"、
少个量词、主语不完整、主动被动混用这类语法问题，平常人读着不别扭的也一律不动；数字、条款
编号、标点、空格一律不动。但连着两个相同的字、后面那个字又正好和它组成一个词（增值税税率、
开户行行号、符合合同），那是两个词的交界，是正常写法，不算错字。补字只能补在两个字正中间——
左右紧邻的都必须是汉字：挨着标点、换行、或者一行/一段的开头结尾都不给补，那里的邻字可能只是
被切走了，补上去反而多一个字。

文档里夹着一些"待填的占位"：字母数字混写的电话/账号/邮箱、下划线、"--"、"请勿删"之类的
标记，那些是特意留着的，不要动、也不要替它补全。但「字段名：值」这种一行一条的表单要逐字
看——里面的字写错了照样要改，别因为它看着像填表数据就跳过去。

表单里那片以「：」结尾的字段名也是正文，值空着照样逐字看；字段名里的错字照样是错字。
常见同音字写混：甲/假（「甲方」写成「假方」）、箱/厢（「信箱」写成「信厢」）、行/航
（「开户行」写成「开户航」）、账/帐、检/捡、及/急——出现这些必须改。

文档有两个版本：
  {original_file}  原文，只读，永远不会变。错别字都在它里面。
  {working_file}   修改稿：你写下的每一处改动都记到它上面。

"""

_MOVES_TEXT = """改动直接写在正文里（不要包进 JSON，不要用工具），一处一段：

*** Begin Correction
@@ 逐字照抄的原文一小段，用来定位；它必须在原文里只出现一次
-上面那段里写错了的那几个字，也是逐字照抄
+改完之后的样子：换字就与上一行一样长；删字就比上一行少一个字；漏写一个字就比上一行多一个字；同一个字连着写了两个以上就收成一个
*** End Correction

例：

*** Begin Correction
@@ 各单位应切实履行安全职责，承但相应的管理责任
-承但相应的管理责任
+承担相应的管理责任
*** End Correction

一段里只有一行 - 和一行 +。一句话里错好几处、一处里掺着好几种改法都没问题：两行一样长就逐字
改这段里写错的字（同音/近音/形近那种，一次改好几处也行）；删字就让改后那行少一个字；漏写了
一个字就让改后那行多一个字；同一个字连着写了两个以上就收成一个（三个以上可以一次收到剩一个）。
删和补要格外确定：多看一遍这两行，删掉/补上之后那句话还读得通、意思没变才写。
换字和增删混在一起、说不清谁对谁的时候就拆成两段分别写。
引文够唯一就行，不用抄太长；不够唯一就在前后多抄几个字。
你不需要数位置、行号、序号——抄准原文就行。

"""

_TOOLS_TEXT = """这三个才是工具，需要时调用：
  read_text()        读 {original_file} 还没读过的下一段（不用填参数）；一轮最多给你
                     {turn_budget} 字，返回的每行形如「  1234|正文」，数字只是位置标记。
  find_text(query)   在 {original_file} 里搜 1~{find_query_max} 个字，看它出现在哪、前后是什么；
                     拿不准引文够不够唯一时，先搜一下再写改动段。
  finish()           全文读完、该改的都改完了，调用它。

"""

_MOVES_TABLE = """表格这边说的"一段"就是一个单元格：一格一行，每行行首是这一格的编号。改动不写在正文里，
用工具提交，一处一次：

  edit_cell(cell_id="1R2C4", old="合同价宽    %", new="合同价款    %")

cell_id 就是行首那个编号，形如 1R2C4——前面的 1 是第几张表，R2C4 是它在这张表里的行列。
一个编号在整篇里只出现一次（跨表也不重号），所以先把编号定到那一格，再在那一格里改，
不用抄引文。行跟行可以不一样长（有的行少几列、有的格是合并出来的），照着行首那个编号
抄就行。

old 是这一格里写错的那几个字（从这一格的内容里逐字照抄），new 是改完的样子：两行一样长
就逐字改这一格里的错字（一格里有好几个错字也行，一次改好几处也行）；少一个字就是删字；
多一个字就是补上一个漏写的字；同一个字连着写了两个以上就收成一个（三个以上可以一次
收到剩一个）。删和补要格外确定：多看一遍这两行，删掉/补上之后那句话还读得通、意思没变
才写。一次只改 cell_id 指的那一格，不要跨格；换字和增删混在一起、说不清谁对谁的时候就
分两次写。抄 old 之前可以先 get_cell 把这一格取出来看一眼，照着抄最稳。

"""

_TOOLS_TABLE = """这几个才是工具，需要时调用：
  read_text()         读 {original_file} 里的表格：不填参数就接着还没读过的地方往下读；
                      也可以挑着读——read_text(table=2) 是第 2 张表的全部格子、
                      read_text(row=3) 是各表里的第 3 行、read_text(col=4) 是各表里的第 4 列
                      （可以连用，一次拿很多格）。一轮最多给你 {turn_budget} 字，返回的每行
                      形如「  1234|1R2C4:正文」——前面那个数字只是位置标记，行首的编号才是
                      这一格的地址。
  get_cell(cell_id)   按编号取某一格的完整内容，逐字返回（抄 old 之前先看一眼最稳）。
  edit_cell(cell_id, old, new)  在指定编号的那一格里提交一处字级改动（见上）。
  find_cell(query)    在 {original_file} 里搜 1~{find_query_max} 个字，看它出现在哪几格、
                      前后是什么；拿不准该改哪一格时，先搜一下再动手。
  finish()            全文读完、该改的都改完了，调用它。

"""

_PROMPT_TAIL = """每一轮：先处理提示里给你的{tail_hint}，有错字就{tail_move}，然后往下读
下一段。必须把 {original_file} 从头读到尾——finish 会检查你有没有漏读的区间。
直接动手：只{tail_move2}，不要复述原文、不要写分析过程。没把握就不要改。"""


def _fill_tail(prompt: str, hint: str, move: str, move2: str) -> str:
    """{tail_hint} 这些占位只在收尾那段里出现，按正文 / 表格两种说法填上。"""
    return (prompt.replace("{tail_hint}", hint)
                  .replace("{tail_move2}", move2)
                  .replace("{tail_move}", move))


#: 正文那条路：定位靠"逐字照抄的引文"（见 _PROMPT_RULES 里的说明）。
READTOOL_SYSTEM_PROMPT = _fill_tail(
    _PROMPT_RULES + _MOVES_TEXT + _TOOLS_TEXT + _PROMPT_TAIL,
    "那段原文正文", "写改动段（可以写多段）", "写改动段或调用工具",
)

#: 表格那条路：定位靠行首那个编号（1R2C4 这种），先在格里定位、再在格内改，见 _MOVES_TABLE。
TABLE_SYSTEM_PROMPT = _fill_tail(
    _PROMPT_RULES + _MOVES_TABLE + _TOOLS_TABLE + _PROMPT_TAIL,
    "那些单元格", "调用 edit_cell 改（可以改好几格）",
    "调用工具（read_text / get_cell / edit_cell / find_cell）",
)



TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_text",
            "description": f"读 {ORIGINAL_FILE} 里的一段：从第 offset 个字开始，最多 length 个字",
            "parameters": {
                "type": "object",
                "properties": {
                    "offset": {
                        "type": "integer",
                        "description": "起始位置（0 开始）；不填就接着还没读过的地方往下读",
                    },
                    "length": {
                        "type": "integer",
                        "description": f"读多少个字，不传默认 {READ_DEFAULT}，最多 {READ_MAX}",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_text",
            "description": (
                f"在 {ORIGINAL_FILE} 里搜索 1~{FIND_QUERY_MAX} 个字，返回每一处出现的位置和上下文"
                "（用来确认某段话出现在哪、或者某段引文唯不唯一）"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": FIND_QUERY_MAX,
                        "description": "要找的字、词或一小段原文（1~%d 个字）" % FIND_QUERY_MAX,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "声明已经读完全文、该改的都改完了（漏读区间会被打回）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TOOL_NAMES = frozenset(item["function"]["name"] for item in TOOLS)

def _tool_schema(name: str) -> dict[str, Any]:
    """正文和表格两条路共用的工具定义（read_text / finish）。"""
    return next(item for item in TOOLS if item["function"]["name"] == name)


#: 表格这条路专用的工具：先把编号定到某一格，再在那一格里改。
#: 增、删、改都用 edit_cell 的 old / new 两行表达（一样长=换字，少一字=删字，多一字=补字，
#: 同一个字连着写了两个以上=收成一个）；查有三条：read_text 顺着读、get_cell 按编号取、
#: find_cell 按内容找。定位靠行首那个编号，所以不存在"引文不唯一"这种定不了位的情况。
TABLE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_text",
            "description": (
                f"读 {ORIGINAL_FILE} 里的表格内容。不填参数就接着还没读过的地方往下读；"
                "也可以按 第几张表 / 第几行 / 第几列 挑着读一次拿很多格（三者可以任意组合）"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "integer", "description": "第几张表（1 开始）"},
                    "row": {"type": "integer", "description": "第几行（1 开始）；不填表号就是各表里的这一行"},
                    "col": {"type": "integer", "description": "第几列（1 开始）；不填表号就是各表里的这一列"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cell",
            "description": "按编号取某一格的完整内容，逐字返回（用来把 old 抄准）",
            "parameters": {
                "type": "object",
                "properties": {
                    "cell_id": {"type": "string", "description": "单元格编号，形如 1R2C4"},
                },
                "required": ["cell_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_cell",
            "description": (
                "在指定编号的那一格里提交一处字级改动。old 是这一格里写错了的那几个字（逐字照抄），"
                "new 是改完的样子：两行一样长是换字，少一个字是删字，多一个字是补字，"
                "同一个字连着写了两个以上就收成一个"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cell_id": {"type": "string", "description": "单元格编号，形如 1R2C4"},
                    "old": {"type": "string", "description": "这一格里写错了的那几个字，逐字照抄"},
                    "new": {"type": "string", "description": "改完的样子"},
                },
                "required": ["cell_id", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_cell",
            "description": (
                f"在 {ORIGINAL_FILE} 里按内容搜 1~{FIND_QUERY_MAX} 个字，返回含它的单元格编号、"
                "内容与前后文（用来确认该改哪一格，或者某一格出现过几次）"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": FIND_QUERY_MAX,
                        "description": "要找的字、词或一小段原文（1~%d 个字）" % FIND_QUERY_MAX,
                    },
                },
                "required": ["query"],
            },
        },
    },
    _tool_schema("finish"),
]


READTOOL_PROMPT = ChatPromptTemplate.from_messages([
    ("system", READTOOL_SYSTEM_PROMPT),
    ("human", "{instruction}"),
])

TABLE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", TABLE_SYSTEM_PROMPT),
    ("human", "{instruction}"),
])

tool_chain: Runnable | None = None
table_chain: Runnable | None = None


def init_readtool_chain(model: Any | None = None, table: bool = False) -> None:
    """构建工具链：提示词 -> 绑定了读写工具的模型。表格那条路换一套提示词和一套工具。"""
    global tool_chain, table_chain
    model = model or get_model()
    if table:
        table_chain = TABLE_PROMPT | model.bind_tools(TABLE_TOOLS, tool_choice="auto")
    else:
        tool_chain = READTOOL_PROMPT | model.bind_tools(TOOLS, tool_choice="auto")
    logger.info(
        "readtool.chain.init available=%s table=%s",
        (table_chain if table else tool_chain) is not None,
        table,
    )


def _get_chain(table: bool = False) -> Runnable:
    if table:
        if table_chain is None:
            init_readtool_chain(table=True)
        return table_chain
    if tool_chain is None:
        init_readtool_chain()
    return tool_chain


# ── 改动块 ──


@dataclass
class Correction:
    """模型写下的一个改动块：引文（只用来定位）、改前、改后。"""

    anchor: str
    old: str
    new: str


def parse_corrections(content: str) -> tuple[list[Correction], list[str]]:
    """从模型正文里抠出改动块，返回 (改动块, 写坏的块)。

    容忍四件事：整块被缩进、围栏代码块（```）、漏写 *** End Correction（读到下一个
    *** Begin Correction 或正文结尾为止）、把 *** Begin Correction 多写了一次（第二个
    Begin 当作新块的开始，不当作正文）。
    """
    blocks: list[Correction] = []
    broken: list[str] = []
    lines = (content or "").splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() != CORRECTION_BEGIN:
            index += 1
            continue
        index += 1
        anchor: str | None = None
        olds: list[str] = []
        news: list[str] = []
        body: list[str] = []
        while index < len(lines):
            head = lines[index].strip()
            if head == CORRECTION_END or head == CORRECTION_BEGIN:
                break
            raw = lines[index].rstrip()
            body.append(raw)
            index += 1
            line = raw.lstrip()
            if line.startswith("@@"):
                # `@@` 后面那一个空格是格式里的分隔符，不算正文；再往后一律原样保留。
                anchor = line[2:][1:] if line[2:].startswith(" ") else line[2:]
            elif line.startswith("-"):
                olds.append(line[1:])
            elif line.startswith("+"):
                news.append(line[1:])
        if index < len(lines) and lines[index].strip() == CORRECTION_END:
            index += 1
        kept = [item for item in body if item.strip() and not item.strip().startswith("```")]
        if not kept:
            # 多打的一个 Begin 后面什么都没有：不算写坏的块，也不用回灌给模型。
            continue
        if anchor is None or len(olds) != 1 or len(news) != 1:
            broken.append(" / ".join(kept) or "(空)")
            continue
        blocks.append(Correction(anchor=anchor, old=olds[0], new=news[0]))
    return blocks, broken


# ── 文档状态 ──


@dataclass
class Document:
    """000.txt 与 001.txt 的状态。原文只在 original 里，任何工具都不会改它。"""

    original: str
    #: 整篇文本（不止这一片）。删字判据要拿它当语料——只看这一片会把别处的搭配看漏。
    corpus: str = ""
    #: 这一片在 corpus 里的起点
    corpus_offset: int = 0
    #: 000 下标 -> 改成的那个字（replace 落在这里）
    replaced: dict[int, str] = field(default_factory=dict)
    #: 被删掉的 000 下标（delete 落在这里）
    deleted: set[int] = field(default_factory=set)
    #: 000 下标 -> 补在这个下标那个字前面的字（insert 落在这里；等于长度就是补在末尾）
    inserted: dict[int, str] = field(default_factory=dict)
    #: 已经被返回给模型的 000 下标（finish 的覆盖率检查用它）
    covered: set[int] = field(default_factory=set)
    #: 本轮已经摆到模型眼前的字数（见 TURN_READ_BUDGET），每轮清零
    turn_read: int = 0
    #: 已落地的改动，用于回灌和调试
    edits: list[dict[str, Any]] = field(default_factory=list)

    def working(self) -> str:
        """001.txt 的正文：原文按 insert / replace / delete 套一遍。"""
        parts: list[str] = []
        for index in range(len(self.original) + 1):
            if index in self.inserted:
                parts.append(self.inserted[index])
            if index < len(self.original) and index not in self.deleted:
                parts.append(self.replaced.get(index, self.original[index]))
        return "".join(parts)


def diff_errors(original: str, working: str) -> list[dict[str, Any]]:
    """对比 000.txt 与 001.txt，得到逐字改动。

    001 由 000 换字 / 删字 / 补字而来，所以对齐交给 difflib：replace 段里一一对应的是换字，
    多出来的原文位是删字、多出来的 001 位是补字；delete 段里每一位都是删字；insert 段是补字。
    补字的 position 是"插在 000 这个下标那个字的前面"（等于 000 的长度就是补在末尾）。
    连写收敛（款款款款款→款）会落成连续几位 delete；位置一律是 000.txt 的绝对下标。
    """
    items: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=original, b=working, autojunk=False)
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for offset in range(b1, b2):
                items.append({
                    "position": a1,
                    "original_char": "",
                    "correct_char": working[offset],
                    "tag": "insert",
                })
            continue
        if tag == "replace":
            pairs = min(a2 - a1, b2 - b1)
            for offset in range(pairs):
                items.append({
                    "position": a1 + offset,
                    "original_char": original[a1 + offset],
                    "correct_char": working[b1 + offset],
                    "tag": "replace",
                })
            for position in range(a1 + pairs, a2):
                items.append({
                    "position": position,
                    "original_char": original[position],
                    "correct_char": "",
                    "tag": "delete",
                })
            for offset in range(b1 + pairs, b2):
                items.append({
                    "position": a2,
                    "original_char": "",
                    "correct_char": working[offset],
                    "tag": "insert",
                })
            continue
        for position in range(a1, a2):
            items.append({
                "position": position,
                "original_char": original[position],
                "correct_char": "",
                "tag": "delete",
            })
    return items


def unread_ranges(document: Document) -> list[tuple[int, int]]:
    """还没返回给模型的区间，闭区间 [起, 止]。"""
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(len(document.original)):
        if index in document.covered:
            if start is not None:
                ranges.append((start, index - 1))
                start = None
        elif start is None:
            start = index
    if start is not None:
        ranges.append((start, len(document.original) - 1))
    return ranges


def _describe_ranges(ranges: list[tuple[int, int]], keep: int = 5) -> str:
    shown = "、".join(f"{start}-{end}" for start, end in ranges[:keep])
    return shown + ("…" if len(ranges) > keep else "")


def next_unread(document: Document) -> int:
    """最前面还没读过的地方；全文都读过了就返回末尾位置。"""
    unread = unread_ranges(document)
    return unread[0][0] if unread else len(document.original)


# ── 渲染 ──


def _split_piece(piece: str, max_line: int) -> list[str]:
    """把一段文本切成若干行：只在句末断句，超长句才按上限硬切。

    一行 = 一句话。把句子拦腰切断（哪怕切在逗号后）会让模型只盯着残句看，实测会漏掉
    错字，所以宁可让行变长也不从中间切。
    """
    if not piece:
        return [""]
    segments: list[str] = []
    buffer: list[str] = []
    for char in piece:
        buffer.append(char)
        if char in SENTENCE_END and len(buffer) >= 6:
            segments.append("".join(buffer))
            buffer = []
    if buffer:
        segments.append("".join(buffer))

    lines: list[str] = []
    for segment in segments:
        start = 0
        while start < len(segment):
            lines.append(segment[start:start + max_line])
            start += max_line
    return lines or [""]


def render_window(text: str, start: int) -> str:
    """把一段原文渲染成带绝对位置的行。和原文逐字符一致，不丢字。"""
    lines = _split_piece(text, RENDER_MAX_LINE)
    position = start
    out: list[str] = []
    for line in lines:
        out.append(f"{position:>6}|{line}")
        position += len(line)
    return "\n".join(out)


# ── 工具实现 ──


def _as_int(value: Any) -> int | None:
    """模型经常把整数写成字符串，甚至写成「第一行」——一律收敛成 int 或 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def tool_read_text(document: Document, offset: Any, length: Any = None) -> str:
    """读一段原文。返回给模型看的文本，同时把这段计入"已读"。"""
    total = len(document.original)
    if offset is None:
        start = next_unread(document)
        if start >= total:
            return (
                f"{ORIGINAL_FILE} 已经读到末尾，全 {total} 个字都读过了。"
                "该改的都改完了就调用 finish；还有想复查的地方，可以用 find_text 找。"
            )
    else:
        start = _as_int(offset)
        if start is None:
            return f"offset 必须是整数（0 开始），收到 {offset!r}"
        start = max(0, min(start, total))
    size = _as_int(length) if length is not None else None
    if size is None:
        size = READ_DEFAULT
    if size <= 0:
        return f"length 必须是正整数，收到 {length!r}"
    size = min(size, READ_MAX)
    end = min(start + size, total)
    if start >= end:
        return f"{ORIGINAL_FILE} 共 {total} 个字，位置 {start} 已到末尾，没有内容可读。"
    if document.turn_read + (end - start) > TURN_READ_BUDGET:
        return (
            "这一轮读到的东西够多了，先把上面这些处理完（有错字就写改动段），下一轮再回来接着读。"
        )
    document.turn_read += end - start
    document.covered.update(range(start, end))
    return f"{ORIGINAL_FILE} 第 {start}~{end - 1} 个字：\n" + render_window(document.original[start:end], start)


def tool_find_text(
    document: Document,
    query: Any,
    cells: list[CellSpan] | None = None,
) -> str:
    """在原文里搜字/词。返回每一处的位置与上下文，位置附近计入"已读"。

    表格那条路传 cells：结果按单元格编号报，不报 000.txt 的绝对下标。
    """
    if not isinstance(query, str) or not query.strip():
        return f"query 必须是非空的字或词（1~{FIND_QUERY_MAX} 个字）"
    if len(query) > FIND_QUERY_MAX:
        return f"query 最多 {FIND_QUERY_MAX} 个字，收到 {len(query)} 个字"
    hits: list[int] = []
    found = document.original.find(query)
    while found != -1 and len(hits) < FIND_LIMIT:
        hits.append(found)
        found = document.original.find(query, found + 1)
    if not hits:
        return f"{ORIGINAL_FILE} 里没有找到「{query}」。"
    lines: list[str] = []
    for hit in hits:
        head = max(0, hit - FIND_CONTEXT)
        tail = min(len(document.original), hit + len(query) + FIND_CONTEXT)
        document.covered.update(range(head, tail))
        before = document.original[head:hit]
        after = document.original[hit + len(query):tail]
        if cells is None:
            lines.append(f"位置 {hit}：…{before}【{query}】{after}…")
        else:
            lines.append(f"{_cell_at(cells, hit) or '行首编号上'}：…{before}【{query}】{after}…")
    more = "" if len(hits) < FIND_LIMIT else f"\n（只列出前 {FIND_LIMIT} 处）"
    return f"「{query}」在 {ORIGINAL_FILE} 里出现 {len(hits)} 次：\n" + "\n".join(lines) + more


def _type_reason(tag: str, wrong: str, right: str) -> str:
    """过不了类型门时回给模型的说明。"""
    if tag == "replace":
        return f"「{wrong}」改成「{right}」不是可验证的错别字类型（同音字/近音字/字母数字），已忽略"
    if tag == "collapse":
        return f"「{wrong}」这段连写不是可执行的删字：连着两个以上同一个字才能收到剩一个"
    if tag == "insert":
        return (
            f"补「{right}」不是可验证的漏字：补的得是单个汉字、不能和左右相同、左右紧邻的都要是"
            f"汉字（挨着标点、换行、一行或一段的开头结尾不给补，那里可能只是换行把词切开了），"
            f"而且它和左邻或右邻组成的两个字要在全文别处出现过（邮编码→邮政编码），已忽略"
        )
    return (
        f"删「{wrong}」不是可执行的删字：连着两个以上同一个字可以收成一个（款款款款→款、卖方方仓库"
        f"→卖方仓库）；正好连写两个、而它和右边那个字又是全文里出现过的搭配（增值税税率、开户行"
        f"行号）时，那是两个词的交界，不动；另外可以删掉一个和前后都不相同的字（开户航行→开户行）"
    )


def _run_span(text: str, index: int) -> tuple[int, int]:
    """text[index] 那个字所在的"同字连写"区间 [起, 止]（闭区间）；下标越界返回 (0, -1)。"""
    if not 0 <= index < len(text):
        return 0, -1
    char = text[index]
    left = index
    while left > 0 and text[left - 1] == char:
        left -= 1
    right = index
    while right + 1 < len(text) and text[right + 1] == char:
        right += 1
    return left, right


def _edit_ops(old: str, new: str) -> list[tuple[int, str, str, str]] | None:
    """把 old → new 拆成一组"字级"改动；拆不干净就返回 None。

    返回 [(改动在 old 里的下标, tag, 原字串, 改后字串)]，tag 有四类：

      replace   换 1 个字；一段里可以有好几处（海男省三亚是→海南省三亚市）
      delete    删 1 个字：多打了一个字（开户航行→开户行）
      insert    补 1 个字：漏写了一个字，插在 old 的这个下标之前（邮编码→邮政编码）
      collapse  一段 ≥3 个连续相同的字收成 1 个（款款款款→款）

    一句话里混着换字 / 删字 / 补字都行，只要 SequenceMatcher 能把它们切成上面这些形状。
    切不开（同一段里换字和增删糊在一起，说不清谁对应谁）就返回 None，让模型拆成两段再写。
    """
    ops: list[tuple[int, str, str, str]] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_len, new_len = i2 - i1, j2 - j1
        if tag == "replace" and old_len == new_len:
            for offset in range(old_len):
                ops.append((i1 + offset, "replace", old[i1 + offset], new[j1 + offset]))
            continue
        if tag == "insert" and new_len == 1:
            ops.append((i1, "insert", "", new[j1]))
            continue
        if tag != "delete":
            return None
        if old_len == 1:
            ops.append((i1, "delete", old[i1], ""))
            continue
        chunk = old[i1:i2]
        char = chunk[0]
        left = old[i1 - 1] if i1 > 0 else ""
        right = old[i2] if i2 < len(old) else ""
        if len(set(chunk)) == 1 and (left == char or right == char):
            ops.append((i1, "collapse", chunk, char))
            continue
        return None
    return ops or None


def _kind_of(document: Document, tag: str, wrong: str, right: str, position: int) -> str | None:
    """按 000 下标判这一处改动属于哪类错别字。"""
    if tag == "replace":
        return classify("replace", wrong, right)
    if tag == "insert":
        left = document.original[position - 1] if position > 0 else ""
        near = document.original[position] if position < len(document.original) else ""
        return classify_insert(right, left, near, document.original)
    start, end = _run_span(document.original, position)
    following = document.original[end + 1] if end + 1 < len(document.original) else ""
    # 语料用整篇而不是这一片：只看这一片会把别处的搭配看漏（「尾款款前」就吃过这个亏）。
    corpus = document.corpus or document.original
    offset = document.corpus_offset if document.corpus else 0
    return classify_delete(wrong, end - start + 1, following, corpus, offset + start)


def tool_edit_text(document: Document, anchor: Any, old: Any, new: Any) -> tuple[bool, str]:
    """落实模型写下的一个改动块。

    引文只用来定位（照抄、在原文里唯一），改哪一段由改前/改后两行说明。
    位置一律由服务端在 000.txt 里搜出来，模型不提供任何位置。

    一块里可以写好几处改动，逐处判门、逐处落地：过不了门的那一处只是它自己不落地，
    不会把同一块里其它真错字一起扔掉（扔掉的话模型还会以为整块都改好了，再不回头）。
    换字、删字、补字可以是同一块里的不同处，只要每处都只落在单个字上。
    """
    if not isinstance(anchor, str) or not isinstance(old, str) or not isinstance(new, str):
        return False, "改动块里的引文、改前、改后三行都必须是文字"
    if len(anchor) < 2:
        return False, "引文至少要 2 个字，这样才好在原文里定到唯一的一处"
    if len(anchor) > EDIT_MAX:
        return False, f"引文最多 {EDIT_MAX} 个字，收到 {len(anchor)} 个字"
    if not old:
        return False, "改前那一行不能是空的"
    if old == new:
        return False, "改前和改后一样，这处没有改动"
    ops = _edit_ops(old, new)
    if ops is None:
        return False, (
            f"「{old}」和「{new}」不是可执行的字级改动：两行一样长就逐字改错字；少一个字就是删字"
            f"（同一个字连着写了两个以上可以一次收成一个）；多一个字就是补上一个漏写的字。"
            f"如果这一段里换字和增删混在一起、说不清谁对谁，就拆成两段分别写"
        )
    for _, _, wrong, right in ops:
        if (wrong and is_ignorable_diff_char(wrong[0])) or (right and is_ignorable_diff_char(right)):
            return False, "空格和标点不在纠错范围内，已忽略"
    count = document.original.count(anchor)
    if count == 0:
        return False, f"原文里没有「{anchor}」这一段，引文必须逐字照抄原文，一个字都不能改"
    if count > 1:
        return False, (
            f"「{anchor}」在原文里出现了 {count} 次，没法确定你指的是哪一处；"
            "请在前后多抄几个字，让 anchor 在原文里只出现一次"
        )
    start = document.original.index(anchor)
    spots = [index for index in range(len(anchor)) if anchor.startswith(old, index)]
    if not spots:
        return False, f"你抄的引文「{anchor}」里没有「{old}」这段，改前那行要从引文里逐字照抄"

    def kinds_at(base: int) -> list[str] | None:
        """这一段的每一处改动是不是都过得了门（引文里有多处候选时，用它挑位置）。"""
        kinds = [_kind_of(document, tag, wrong, right, base + offset) for offset, tag, wrong, right in ops]
        return None if any(kind is None for kind in kinds) else kinds

    legal = [s for s in spots if kinds_at(start + s) is not None]
    if len(legal) > 1 and len({anchor[:s] + new + anchor[s + len(old):] for s in legal}) > 1:
        # 引文里有多处都说得通，改哪个结果不一样：定位不唯一，请它把引文抄窄。
        return False, (
            f"你抄的这段里有 {len(legal)} 处「{old}」都说得通，改哪一个结果不一样，没法确定；"
            "请改抄一段只包含一处的那几个字"
        )
    if legal:
        base = start + legal[0]
    elif len(spots) == 1:
        # 引文里只可能改这一处：那就逐处判门、逐处落地。
        base = start + spots[0]
    else:
        # 引文里有多处候选，而且没有一处能整段过门：不敢替它挑，按第一处报原因。
        for offset, op_tag, wrong, right in ops:
            if _kind_of(document, op_tag, wrong, right, start + spots[0] + offset) is None:
                return False, _type_reason(op_tag, wrong, right)
        first = ops[0]
        return False, _type_reason(first[1], first[2], first[3])

    return _land_ops(document, ops, base)


def _land_ops(
    document: Document,
    ops: list[tuple[int, str, str, str]],
    base: int,
    label: str = "",
    cell: str = "",
) -> tuple[bool, str]:
    """把一组字级改动落到 000.txt 的 base 处起，逐处判门、逐处落地。

    过不了门的那一处只是它自己不落地，不会把同一批里其它真错字一起扔掉（扔掉的话模型还会
    以为整批都改好了，再不回头）。换字、删字、补字可以是同一批里的不同处，只要每处都只落在
    单个字上。

    label 不为空时，回给模型的说明里用它在代替绝对下标——表格那条路用单元格编号（1R2C4），
    模型看到的编号比 000.txt 的下标有意义得多。
    """
    notes: list[str] = []
    skipped: list[str] = []
    for offset, tag, wrong, right in ops:
        position = base + offset
        at = label or str(position)
        if tag == "insert":
            if position in document.inserted:
                skipped.append(f"{at} 前面已经补过「{document.inserted[position]}」了")
                continue
        else:
            if position in document.deleted:
                skipped.append(f"{at}「{wrong[0]}」这一处已经被你删掉了")
                continue
            if position in document.replaced:
                skipped.append(f"{at}「{wrong[0]}」这一处已经改成「{document.replaced[position]}」了")
                continue
        kind = _kind_of(document, tag, wrong, right, position)
        if kind is None:
            skipped.append(_type_reason(tag, wrong, right))
            continue
        if tag == "replace":
            document.replaced[position] = right
            notes.append(f"{at}「{wrong}」→「{right}」（{kind}）")
        elif tag == "insert":
            document.inserted[position] = right
            notes.append(f"{at} 前面补上「{right}」（{kind}）")
        else:
            for step in range(len(wrong)):
                document.deleted.add(position + step)
            if tag == "collapse":
                notes.append(f"删掉 {at} 起 {len(wrong)} 个「{wrong[0]}」，只留一个（{kind}）")
            else:
                notes.append(f"删掉 {at}「{wrong}」（{kind}）")
        document.edits.append({
            "position": position, "original_char": wrong,
            "correct_char": right, "tag": tag, "kind": kind, "cell": cell,
        })
    if not notes:
        return False, skipped[0] if skipped else "改前和改后一样，这处没有改动"
    if skipped:
        return True, "；".join(notes) + SKIPPED_MARK + "；".join(skipped)
    return True, "；".join(notes)



# ── 表格专用工具：先按编号定位到某一格，再在格内改 ──

#: 表格这一片的单元格：(编号, 在 000.txt 里的起点, 长度)，按行顺序排。
CellSpan = tuple[str, int, int]


def _known_cells(cells: list[CellSpan]) -> str:
    shown = "、".join(key for key, _, _ in cells[:10])
    return f"这一片里有这些编号：{shown}" + ("…" if len(cells) > 10 else "")


def _cell_at(cells: list[CellSpan], position: int) -> str | None:
    """这个位置落在哪一格里；落在行首编号或换行上返回 None。"""
    for key, start, length in cells:
        if start <= position < start + length:
            return key
    return None


def _resolve_cell(cells: list[CellSpan], cell_id: Any) -> tuple[str, int, int] | None:
    """把模型给的编号收敛成这一片里真实存在的那一格。

    容错三件事：大小写、多写一个冒号、漏写表号（形如 R2C4——只在全片唯一时才认）。
    收敛不成功就返回 None，让调用方把这一片有哪些编号回给模型。
    """
    if not isinstance(cell_id, str):
        return None
    key = cell_id.strip().rstrip("：:").upper()
    for item in cells:
        if item[0] == key:
            return item
    match = re.fullmatch(r"R(\d+)C(\d+)", key)
    if match:
        tail = key
        hits = [item for item in cells if item[0].endswith(tail)]
        if len(hits) == 1:
            return hits[0]
    return None


def _select_cells(
    cells: list[CellSpan],
    table: int | None,
    row: int | None,
    col: int | None,
) -> list[CellSpan]:
    """按 第几张表 / 第几行 / 第几列 挑单元格；不填的维度不筛（编号形如 2R3C7，照它拆）。"""
    picked: list[CellSpan] = []
    for span in cells:
        found = re.fullmatch(r"(\d+)R(\d+)C(\d+)", span[0])
        if found is None:
            continue
        here_table, here_row, here_col = (int(value) for value in found.groups())
        if table is not None and here_table != table:
            continue
        if row is not None and here_row != row:
            continue
        if col is not None and here_col != col:
            continue
        picked.append(span)
    return picked


def tool_read_table(document: Document, cells: list[CellSpan], args: dict[str, Any]) -> str:
    """表格那条路的读：不填参数就顺着往下读，也可以按 第几张表 / 第几行 / 第几列 挑着读。

    挑着读一次能把一行、一列、一整张表端出来（方便顺着行看一格和它左右的关系），返回的
    一行一格、行首是编号，跟顺读看到的是同一种样子。只把挑中的格子（连同它前面那个换行）
    计入"已读"，不然 finish 的覆盖率检查会卡在那些换行上。
    """
    table = _as_int(args.get("table"))
    row = _as_int(args.get("row"))
    col = _as_int(args.get("col"))
    if table is None and row is None and col is None:
        return tool_read_text(document, None, None)
    picked = _select_cells(cells, table, row, col)
    if not picked:
        where = "、".join(
            text for text in (
                f"第 {table} 张表" if table is not None else "",
                f"第 {row} 行" if row is not None else "",
                f"第 {col} 列" if col is not None else "",
            ) if text
        )
        return f"这一片里没有{where}的单元格。" + _known_cells(cells)
    size = sum(len(key) + 1 + length for key, _, length in picked)
    if document.turn_read + size > TURN_READ_BUDGET:
        return (
            "这一轮读到的东西够多了，先把上面这些处理完（有错字就用 edit_cell 改），"
            "下一轮再回来接着读。"
        )
    lines: list[str] = []
    for key, start, length in picked:
        head = start - len(key) - 1
        document.covered.update(range(max(0, head - 1), start + length))
        lines.append(f"{head:>6}|{key}:{document.original[start:start + length]}")
    document.turn_read += size
    return f"{ORIGINAL_FILE} 里挑中的 {len(picked)} 个单元格：\n" + "\n".join(lines)


def tool_get_cell(document: Document, cells: list[CellSpan], cell_id: Any) -> str:
    """按编号取某一格的完整内容。取过的那一格计入"已读"。"""
    found = _resolve_cell(cells, cell_id)
    if found is None:
        return f"{ORIGINAL_FILE} 里没有编号为「{cell_id}」的单元格。cell_id 照抄行首那个编号，形如 1R2C4；" + _known_cells(cells)
    key, start, length = found
    document.covered.update(range(start, start + length))
    return f"{key} 这一格的内容（逐字照抄这一格里的字）：\n{document.original[start:start + length]}"


def tool_edit_cell(
    document: Document,
    cells: list[CellSpan],
    cell_id: Any,
    old: Any,
    new: Any,
) -> tuple[bool, str]:
    """在指定编号的单元格里落实一处字级改动。

    跟正文那条路（tool_edit_text）的区别只有一个：定位不靠引文，靠行首那个编号。编号在
    整篇里唯一，所以不存在"引文不唯一"这种定不了位的情况；模型也不用数任何下标。
    """
    if not isinstance(old, str) or not isinstance(new, str):
        return False, "edit_cell 的 old / new 都必须是文字"
    found = _resolve_cell(cells, cell_id)
    if found is None:
        return False, f"没有编号为「{cell_id}」的单元格。cell_id 照抄行首那个编号，形如 1R2C4；" + _known_cells(cells)
    key, start, length = found
    cell = document.original[start:start + length]
    if not old:
        return False, f"old 不能是空的：把 {key} 这一格里写错的那几个字逐字照抄过来"
    if old == new:
        return False, "old 和 new 一样，这处没有改动"
    count = cell.count(old)
    if count == 0:
        return False, f"{key} 这一格里没有「{old}」。这一格的内容是「{cell}」，old 要从中逐字照抄"
    if count > 1:
        return False, f"「{old}」在 {key} 这一格里出现了 {count} 次；请在前后多抄几个字，让 old 在这一格里只出现一次"
    ops = _edit_ops(old, new)
    if ops is None:
        return False, (
            f"「{old}」和「{new}」不是可执行的字级改动：两行一样长就逐字改错字；少一个字就是删字"
            f"（同一个字连着写了两个以上可以一次收成一个）；多一个字就是补上一个漏写的字。"
            f"如果这一处里换字和增删混在一起、说不清谁对谁，就分两次写"
        )
    for _, _, wrong, right in ops:
        if (wrong and is_ignorable_diff_char(wrong[0])) or (right and is_ignorable_diff_char(right)):
            return False, "空格和标点不在纠错范围内，已忽略"
    return _land_ops(document, ops, start + cell.index(old), label=key, cell=key)


# 打回里属于「这个块本身没写对」的那几条：引文太短太长、引文没抄准、引文不唯一、引文里没有
# 改前那行、引文里有多处都说得通、块格式不对。这几种不是「这处不是错别字」的判决——门还没
# 判到内容，只是模型这次没把块写对，重写就能过。主循环把它们当欠账挂着，没清账就不允许
# finish（见 run_document_correction）。
RETRYABLE_MARKS = (
    "引文至少要 2 个字",
    "引文最多",
    "改动块里的引文、改前、改后三行都必须是文字",
    "改前那一行不能是空的",
    "引文必须逐字照抄原文",
    "没法确定你指的是哪一处",
    "改前那行要从引文里逐字照抄",
    "都说得通，改哪一个结果不一样",
    "改动块的格式不对",
    # 表格那条路：编号没定到某一格，或者 old 没在这一格里抄准。
    "没有编号为「",
    "这一格里没有「",
    "在这一格里出现了",
)


def _needs_rewrite(note: str) -> bool:
    """这条打回是不是「块本身没写对」（而不是「这处不算错别字」）。"""
    return any(mark in note for mark in RETRYABLE_MARKS)


# ── 主循环 ──


def _message_tool_calls(raw: Any) -> list[dict[str, Any]]:
    calls = getattr(raw, "tool_calls", None)
    return list(calls) if calls else []


def _edit_place(item: dict[str, Any]) -> str:
    """一处改动回灌时用哪个说法定位：表格那条路是单元格编号，正文那条路是 000.txt 的下标。"""
    return item.get("cell") or str(item["position"])


def _progress(document: Document) -> str:
    # 刻意不报"共多少字、读到百分之几"：实测模型一看到"才读了 11%"就开始赶进度，
    # 同一段文字在整篇里会漏掉的错字，单独喂给它时反而能抓到。这里只告诉它当前位置。
    lines: list[str] = []
    if document.edits:
        shown = document.edits[-EDIT_LOG_KEEP:]
        detail = "；".join(
            (
                f"{_edit_place(item)} 补「{item['correct_char']}」"
                if item["tag"] == "insert"
                else f"{_edit_place(item)} {item['original_char']}→{item['correct_char'] or '(删除)'}"
            )
            for item in shown
        )
        if len(document.edits) > EDIT_LOG_KEEP:
            detail += f"（共 {len(document.edits)} 处，这里只列最近 {EDIT_LOG_KEEP} 处）"
        lines.append("已改：" + detail)
    else:
        lines.append("已改：暂无。")
    lines.append("全文都读过了。" if not unread_ranges(document) else "还没读到末尾。")
    return "\n".join(lines)


def _instruction(
    document: Document,
    last_result: str,
    feedback: str,
    tail: str = "",
    table: bool = False,
) -> str:
    """拼下一轮的指令。tail 不为空时用它替换收尾那句——打回时要模型改写，
    不能还叫它往下读（实测它就会听话地一直读、把轮次烧光）。"""
    blocks = [_progress(document)]
    if last_result:
        blocks.append("【上一轮读到的正文】下面这段就是原文内容，现在检查它：\n" + last_result)
    if feedback:
        blocks.append(feedback)
    if tail:
        blocks.append(tail)
    elif last_result and table:
        blocks.append(
            "先处理上面这些单元格：有错字就用 edit_cell 在那一格里改（可以改好几格），"
            "没写错就直接往下读。处理完再用 read_text 读下一格；全文读完且都改完了，调用 finish。"
        )
    elif last_result:
        blocks.append(
            "先处理上面这段原文：有错字就写改动段（可以写多段），没写错就直接往下读。"
            "处理完再用 read_text 读下一段；全文读完且都改完了，调用 finish。"
        )
    elif table:
        blocks.append("现在用 read_text 读还没读过的下一格。")
    else:
        blocks.append("现在用 read_text 读还没读过的下一段。")
    return "\n\n".join(blocks)


async def run_document_correction(
    text: str,
    max_turns: int = TOOL_MAX_TURNS,
    table: bool = False,
    cells: list[CellSpan] | None = None,
    corpus: str = "",
    corpus_offset: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按 000.txt / 001.txt 跑一次文档纠错，返回 (逐字改动, 调试信息)。

    table=True 走表格那条路：定位靠单元格编号（cells 给出每个编号在 000.txt 里的位置），
    改动一律用 edit_cell 提交，正文里写 @@ 改动块会被打回。corpus / corpus_offset 用来把
    "整篇表格文本"当删字判据的语料——表格是分批送的，只看这一片会把别处的搭配看漏。
    """
    document = Document(original=text, corpus=corpus, corpus_offset=corpus_offset)
    turns: list[dict[str, Any]] = []
    last_result = ""
    feedback = ""
    finished = False
    idle_turns = 0
    # 还挂着的「块写错了」打回，以及这一批已经被打回几次（见 RETRYABLE_MARKS / BACKLOG_MAX）。
    backlog: list[str] = []
    backlog_push = 0
    #: 下一轮收尾那句（见 _instruction 的 tail）。
    tail = ""
    #: 连着几轮没有往前挪（见 STALL_MAX）：每轮开头记一次，轮末对一下。
    stalled = 0

    turn_iter: Iterable[int] = itertools.count(1) if max_turns <= 0 else range(1, max_turns + 1)
    for turn in turn_iter:
        document.turn_read = 0
        covered_before = len(document.covered)
        edits_before = len(document.edits)
        instruction = _instruction(document, last_result, feedback, tail, table)
        try:
            raw = await _get_chain(table).ainvoke({
                "original_file": ORIGINAL_FILE,
                "working_file": WORKING_FILE,
                "turn_budget": TURN_READ_BUDGET,
                "find_query_max": FIND_QUERY_MAX,
                "instruction": instruction,
            })
        except Exception as exc:
            logger.warning("readtool.llm_fail turn=%s exc=%s", turn, exc)
            turns.append({"turn": turn, "error": f"调用失败：{exc}"})
            break

        calls = _message_tool_calls(raw)
        blocks, broken = parse_corrections(getattr(raw, "content", "") or "")
        applied: list[str] = []
        rejected: list[str] = []
        ignored: list[str] = []
        reads: list[str] = []
        want_finish = False

        def land(ok: bool, note: str, where: str) -> None:
            """一处改动落地了没有。没过门的那几处必须回灌：不然模型会以为这处改好了，
            再也不回头（实测就是这么漏掉过真错字）。"""
            if ok:
                landed, _, skipped = note.partition(SKIPPED_MARK)
                applied.append(landed)
                if skipped:
                    rejected.append(where + "里没落地的：" + skipped)
            elif "没有改动" in note:
                ignored.append(note)
            else:
                rejected.append(note)

        if table:
            # 表格这条路上定位靠编号、不靠引文：写了 @@ 改动块等于没改，必须说清楚。
            if blocks:
                rejected.append(
                    f"表格这条路上不写 @@ 改动块（收到 {len(blocks)} 段，都没用上）：要在哪一格"
                    "里改就调用 edit_cell(cell_id, old, new)，cell_id 用行首那个编号。"
                )
            for item in broken:
                rejected.append(f"改动块的格式不对，表格这条路上用不到它：{item[:160]}")
        else:
            for block in blocks:
                ok, note = tool_edit_text(document, block.anchor, block.old, block.new)
                land(ok, note, "这一块")
            for item in broken:
                rejected.append(
                    "这一段改动块的格式不对（缺行，或者 - / + 写了不止一行）。一段只能是："
                    f"@@ 引文 / 一行 - / 一行 +，已忽略：{item[:160]}"
                )

        for call in calls:
            name = call.get("name")
            args = call.get("args") or {}
            if name == "read_text":
                if table:
                    reads.append(tool_read_table(document, cells or [], args))
                else:
                    reads.append(tool_read_text(document, args.get("offset"), args.get("length")))
            elif name == "find_text":
                reads.append(tool_find_text(document, args.get("query")))
            elif name == "find_cell":
                reads.append(tool_find_text(document, args.get("query"), cells or []))
            elif name == "get_cell":
                reads.append(tool_get_cell(document, cells or [], args.get("cell_id")))
            elif name == "edit_cell":
                ok, note = tool_edit_cell(
                    document, cells or [], args.get("cell_id"), args.get("old"), args.get("new")
                )
                land(ok, note, "这一格")
            elif name == "finish":
                want_finish = True
            elif table:
                rejected.append(
                    f"没有名为 {name!r} 的工具。表格这条路上只有这几个：read_text 顺着读、"
                    "get_cell 按编号取一格、edit_cell 在某一格里改、find_cell 按内容找、finish 收工。"
                )
            else:
                rejected.append(
                    f"没有名为 {name!r} 的工具；改错别字不用工具，请直接在正文里写 "
                    f"{CORRECTION_BEGIN} … {CORRECTION_END} 改动块。"
                )

        rewrites = [note for note in rejected if _needs_rewrite(note)]
        if rewrites:
            # 这一轮又写出了「块写错了」的打回：欠账换成这一批，接着挂着。
            backlog = rewrites
        elif blocks or broken or applied:
            # 这一轮重写了块（或者用 edit_cell 改成了）、而且没再出写法问题：上一批欠账销账。
            backlog = []
            backlog_push = 0

        turns.append({
            "turn": turn,
            "calls": len(calls),
            "text": (getattr(raw, "content", "") or "")[:300],
            "applied": len(applied),
            "rejected": len(rejected),
            "rejected_notes": list(rejected),
            "ignored": list(ignored),
            "blocks": len(blocks),
            "broken_blocks": len(broken),
            "read": len(reads),
            "applied_notes": list(applied),
        })

        # 保底出口（见 STALL_MAX）：这一轮既没读到新内容、也没落地新改动，就记一笔；
        # 连着几轮这样，说明模型卡在原地空转，再催也是白烧轮次——直接收工。它这一轮
        # 要是本来就喊了 finish，就当它结束（剩下的区间算没读到，debug 里看得到）。
        if len(document.covered) > covered_before or len(document.edits) > edits_before:
            stalled = 0
        else:
            stalled += 1
            if stalled >= STALL_MAX:
                logger.warning(
                    "readtool.stalled turn=%s covered=%s/%s unread=%s",
                    turn, len(document.covered), len(document.original),
                    len(unread_ranges(document)),
                )
                finished = want_finish
                break

        if want_finish:
            unread = unread_ranges(document)
            if unread:
                # 没读全就不让结束。光叫它自己去读未必听——实测模型每轮都调 finish、
                # 一个字不读，被打回也照旧，就这么空转几百轮，一次请求十几分钟不返回。
                # 所以这里直接替它把这段读出来、摆到下一轮的正文里：一来它真的看到了这段字，
                # 二来读过就计入已读，每一轮覆盖都在往前走，这个循环自己就收住了。
                shown: list[str] = []
                handed: list[tuple[int, int]] = []
                for start, end in unread:
                    room = TURN_READ_BUDGET - document.turn_read
                    if room <= 0:
                        break
                    size = min(end - start + 1, room)
                    shown.append(tool_read_text(document, start, size))
                    handed.append((start, start + size - 1))
                logger.warning(
                    "readtool.unread_handover handed=%s covered=%s/%s",
                    _describe_ranges(handed), len(document.covered), len(document.original),
                )
                tail = ""
                if handed:
                    feedback = (
                        f"还不能结束：{ORIGINAL_FILE} 里 {_describe_ranges(handed)} 这几段你刚才没读过，"
                        "现在直接摆给你了，就在下面那段正文里——有错字就写改动块，看完再调用 finish。"
                    )
                else:
                    # 这一轮模型自己就把「一次能看多少字」的额度用完了，下一轮再摆给它
                    feedback = (
                        f"还不能结束：{ORIGINAL_FILE} 里还有没读过的区间 {_describe_ranges(unread)}。"
                        "下一轮用 read_text 把它读完，读完再调用 finish。"
                    )
                last_result = "\n\n".join(shown)
                continue
            if backlog and backlog_push < BACKLOG_MAX:
                # 还有「块写错了」的欠账：那几处连「是不是错别字」都还没判到，不算「原文没
                # 问题」。放它过去就等于这几处真错字直接丢了（实测发生过），打回去重写。
                backlog_push += 1
                if table:
                    tail = (
                        "这一轮不要再往下读：就对着上面这几处重写改动（cell_id 用行首那个编号、"
                        "old 从这一格的内容里逐字照抄），写完直接调用 finish。"
                    )
                    feedback = (
                        "还不能结束：下面这几处你写了改动，但没通过校验，而且不是「这一格没问题」"
                        "的结论，是编号写错、old 没抄准这类写法上的问题："
                        + "；".join(backlog)
                        + "。请先把这几处重写对——cell_id 用行首那个编号、old 先 get_cell 取出来"
                        "再逐字照抄、new 按换字 / 删字 / 补字写对；重写完再调用 finish。"
                    )
                else:
                    tail = (
                        "这一轮不要再往下读：就对着上面这几处重写改动块"
                        "（引文逐字照抄原文、前后多抄几个字让它在原文里只出现一次、改前那行"
                        "必须是引文里逐字照抄的一段），写完直接调用 finish。"
                    )
                    feedback = (
                        "还不能结束：下面这几处你写了改动，但没通过校验，而且不是「原文没问题」的"
                        "结论，是引文没抄准、引文不唯一这类写法上的问题："
                        + "；".join(backlog)
                        + "。请先把这几处重写对——引文逐字照抄原文、前后多抄几个字让它在原文里只"
                        "出现一次、改前那行必须是引文里逐字照抄的一段、一段只写一行 - 和一行 +；"
                        "重写完再调用 finish。"
                    )
                last_result = ""
                continue
            finished = True
            break

        if not calls and not blocks:
            # 模型整轮只说话、不读也不改：催它一次，连着两轮这样才收工（否则会停在半路，
            # finish 的覆盖率检查根本没机会兜住这种"没读完就跑"）。
            idle_turns += 1
            if idle_turns >= 2:
                break
            tail = ""
            if table:
                feedback = (
                    "上一轮你没有读、也没有改。请用 read_text 读还没读过的下一格，"
                    "或者调用 edit_cell 在某一格里改一处，或者调用 finish。"
                )
            else:
                feedback = (
                    "上一轮你没有读、也没有改。请用 read_text 读还没读过的下一段，"
                    f"或者写下 {CORRECTION_BEGIN} 改动块，或者调用 finish。"
                )
            last_result = ""
            continue
        idle_turns = 0

        # 只把最近一轮的查阅结果带进下一轮；本轮读了多少字由 TURN_READ_BUDGET 卡住，
        # 所以这里不需要再截断（截断会让"已计入已读"的正文其实没给模型看过）。
        last_result = "\n\n".join(reads)
        tail = ""
        feedback = ""
        if rejected and table:
            feedback = (
                "上一轮这些改动没有通过校验、已忽略：" + "；".join(rejected)
                + "。如果那一格里确实写错了：先用 get_cell 把这一格取出来，old 从中逐字照抄；把错字"
                "换成读音相同/相近的字；删字就写成比 old 少一个字的 new（同一个字连着写了两个以上可以"
                "一次收成一个）；漏写一个字就写成比 old 多一个字的 new（补的字左右紧邻的都要是汉字、"
                "要和左邻或右邻组成别处出现过的两个字）；这一格没问题就继续往下读。"
            )
        elif rejected:
            feedback = (
                "上一轮这些改动没有通过校验、已忽略：" + "；".join(rejected)
                + "。如果原文那里确实写错了，把引文和改前那行都逐字抄准再试一次（引文必须在原文里"
                "一模一样地出现且只出现一次），把错字换成读音相同/相近或形近的字；删字就写成比上一"
                "行少一个字的 + 行（同一个字连着写了两个以上可以一次收成一个）；漏写一个字就写成比上一"
                "行多一个字的 + 行（补的字左右紧邻的都要是汉字、要和左邻或右邻组成别处出现过的两个字，"
                "挨着标点、换行、一行或一段的开头结尾不给补）；原文没问题就"
                "继续往下读。"
            )

    working = document.working()
    errors = diff_errors(document.original, working)
    debug_info = {
        "file": ORIGINAL_FILE,
        "working_file": WORKING_FILE,
        "chars": len(document.original),
        "mode": "readtool.table" if table else "readtool",
        "turns": turns,
        "finished": finished,
        "covered": len(document.covered),
        "unread": [[start, end] for start, end in unread_ranges(document)],
        "applied": len(document.edits),
        "errors_from_diff": len(errors),
    }
    log_info(
        "readtool.done",
        chars=len(document.original),
        turns=len(turns),
        applied=len(document.edits),
        diff=len(errors),
        finished=finished,
        covered=len(document.covered),
    )
    return errors, debug_info
