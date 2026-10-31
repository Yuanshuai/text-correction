#!/usr/bin/env python3
"""阅读工具引擎的离线单测：不调模型，只测确定性代码。

覆盖：改动段解析、引文定位、单字校验、错误类型门、覆盖率记账、000/001 比对，
以及用假模型跑通整轮循环（读到内容 → 写改动段 → finish 被打回 → 读完再 finish），
还有"块写错了"的欠账没清之前不让 finish。

跑法：python tests/test_readtool.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import correction.readtool as rt
from correction.pdf import is_directory_line, is_footer


FAILED: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  [ok] {name}")
        return
    FAILED.append(name)
    print(f"  [FAIL] {name} {detail}")


def block(anchor: str, old: str, new: str) -> str:
    """拼一个改动段，形态与提示词里给模型看的完全一致。"""
    return (
        f"{rt.CORRECTION_BEGIN}\n@@ {anchor}\n-{old}\n+{new}\n{rt.CORRECTION_END}\n"
    )


class _StubMessage:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _StubChain:
    """按剧本依次返回消息的假模型。"""

    def __init__(self, script: list[_StubMessage]) -> None:
        self.script = list(script)

    async def ainvoke(self, _payload):
        return self.script.pop(0) if self.script else _StubMessage()


# ── 改动段解析 ──


def test_parse_corrections() -> None:
    blocks, broken = rt.parse_corrections(block("中过某某集团", "中过某某集团", "中国某某集团"))
    check("解析出一个改动段", len(blocks) == 1 and not broken, broken)
    check(
        "引文 / 改前 / 改后都取到",
        (blocks[0].anchor, blocks[0].old, blocks[0].new)
        == ("中过某某集团", "中过某某集团", "中国某某集团"),
    )

    blocks, broken = rt.parse_corrections("改好了：\n```\n" + block("甲方", "甲", "乙") + "```\n")
    check("围栏代码块里的改动段也认", len(blocks) == 1 and not broken, broken)

    blocks, _ = rt.parse_corrections(f"{rt.CORRECTION_BEGIN}\n@@ 甲方\n-甲\n+乙\n")
    check("漏写结束行时读到正文结尾", len(blocks) == 1, blocks)

    bad = f"{rt.CORRECTION_BEGIN}\n@@ 甲方\n-甲\n-方\n+乙方\n{rt.CORRECTION_END}\n"
    blocks, broken = rt.parse_corrections(bad)
    check("一段里 -/+ 不止一行判为写坏", not blocks and len(broken) == 1, broken)

    # 实测踩到过：模型把 Begin 写重了，两个改动段挤在一起，整段被当成坏块丢掉。
    twice = (
        f"{rt.CORRECTION_BEGIN}\n@@ 甲    方：中过某某集团\n-中过某某集团\n+中国某某集团\n"
        f"{rt.CORRECTION_BEGIN}\n"
        f"{rt.CORRECTION_BEGIN}\n@@ 广东生广州市\n-广东生\n+广东省\n{rt.CORRECTION_END}\n"
    )
    blocks, broken = rt.parse_corrections(twice)
    check(
        "Begin 写重了也把两段都收下来",
        [(item.old, item.new) for item in blocks] == [("中过某某集团", "中国某某集团"), ("广东生", "广东省")],
        blocks,
    )
    check("  → 空的那一个 Begin 不算写坏的块", not broken, broken)


# ── 定位与类型门 ──


def test_edit_localization() -> None:
    text = "甲方：中过某某集团有限责任公司，承但相应责任。"
    doc = rt.Document(original=text)
    ok, note = rt.tool_edit_text(doc, "中过某某集团", "中过某某集团", "中国某某集团")
    check(
        "同音字按引文落到唯一那处",
        ok and doc.replaced == {text.index("过"): "国"},
        note,
    )

    doc = rt.Document(original="承但相应的管理责任")
    ok, note = rt.tool_edit_text(doc, "承但相应", "承但", "承担")
    check("同音不同调也放行", ok and doc.replaced == {1: "担"}, note)

    doc = rt.Document(original="开户行：中国人民一行")
    ok, note = rt.tool_edit_text(doc, "人民一行", "一", "银")
    check("前后鼻音不分按近音字放行", ok and note.endswith("（近音字）"), note)

    doc = rt.Document(original="作业人身应佩戴防护用品")
    ok, note = rt.tool_edit_text(doc, "作业人身", "身", "生")
    check("近音字双向都认", ok and note.endswith("（近音字）"), note)

    doc = rt.Document(original="安全第一")
    ok, note = rt.tool_edit_text(doc, "安全第一", "安", "啊")
    check("单韵母音节不算近音（安/啊 → 拒）", not ok and "不是可验证" in note, note)

    doc = rt.Document(original="乙方应履行义务")
    ok, note = rt.tool_edit_text(doc, "履约义务", "履行", "履约")
    check("引文不在原文里 → 拒绝", not ok and "没有" in note, note)

    doc = rt.Document(original="中国某某集团公司与中国某某集团公司")
    ok, note = rt.tool_edit_text(doc, "公司", "公", "工")
    check("引文出现两次 → 拒绝", not ok and "2 次" in note, note)

    doc = rt.Document(original="承但相应责任")
    ok, note = rt.tool_edit_text(doc, "承但相应责任", "承但相应", "承担责任")
    check(
        "一段里改多处的：过门的照常落地",
        ok and doc.replaced == {1: "担"},
        (doc.replaced, note),
    )
    check(
        "  → 没过门的那几处回灌给模型（不再拖累整块）",
        "没落地的" in note and note.count("不是可验证") == 2,
        note,
    )

    # 一段里把几处错字一次改掉：每一处都能机械验，就整段执行
    text = "住 所 地：海男省三亚是"
    doc = rt.Document(original=text)
    ok, note = rt.tool_edit_text(doc, "海男省三亚是", "海男省三亚是", "海南省三亚市")
    check("同音字一次改多处（海男省三亚是→海南省三亚市）", ok, note)
    check(
        "  → 两处都落地",
        doc.working() == "住 所 地：海南省三亚市"
        and doc.replaced == {text.index("男"): "南", text.index("是"): "市"},
        (doc.replaced, note),
    )

    doc = rt.Document(original="尾款款前")
    ok, note = rt.tool_edit_text(doc, "尾款款前", "款", "款款")
    check("补一个和左邻相同的字 → 拒绝（不是漏字）", not ok and "不是可验证的漏字" in note, note)

    # 漏写一个字：补的字要和左邻或右邻组成的两个字，在全文别处出现过
    text = "邮政编码以合同为准。填写邮编码：100000"
    doc = rt.Document(original=text)
    ok, note = rt.tool_edit_text(doc, "邮编码", "邮编码", "邮政编码")
    check("漏写一个字补上（邮编码→邮政编码）", ok and note.endswith("（漏写的字）"), note)
    check(
        "  → 001.txt 补在了对的位置",
        doc.working() == "邮政编码以合同为准。填写邮政编码：100000",
        doc.working(),
    )

    doc = rt.Document(original="填写邮编码：100000")
    ok, note = rt.tool_edit_text(doc, "邮编码", "邮编码", "邮政编码")
    check("补出来的两个字别处没出现过 → 拒绝", not ok and "不是可验证的漏字" in note, note)

    # 段落/行的开头结尾不给补字：那里的邻字不归这一段管，可能只是换行把词切开了
    doc = rt.Document(original="方应在收到通知后补齐，卖方应当配合。")
    ok, note = rt.tool_edit_text(doc, "方应在收到通知后", "方应在收到", "卖方应在收到")
    check("段落开头补字 → 拒绝（可能只是换行切走了）", not ok and "不是可验证的漏字" in note, note)

    doc = rt.Document(original="卖方应配合。买方发现短缺，卖")
    ok, note = rt.tool_edit_text(doc, "发现短缺，卖", "卖", "卖方")
    check("段落结尾补字 → 拒绝", not ok and "不是可验证的漏字" in note, note)

    # 整篇是把单元用 \n 拼成一整段的，行首那个 \n 不能当成"左邻字"
    doc = rt.Document(original="卖方的技\n术服务应符合合同。")
    ok, note = rt.tool_edit_text(doc, "术服务应符合合同。", "术服务", "技术服务")
    check("行首（换行之后）补字 → 拒绝", not ok and "不是可验证的漏字" in note, note)

    # 一句话里换字和删字分处：逐处判门、逐处落地
    doc = rt.Document(original="抽捡货物航行")
    ok, note = rt.tool_edit_text(doc, "抽捡货物航行", "抽捡货物航行", "抽检货物行")
    check("同一段里换字带删字都能落地", ok and doc.working() == "抽检货物行", (doc.working(), note))
    check("  → 一处记「同音字」、一处记「多打的字」", note.count("同音字") == 1 and note.count("多打的字") == 1, note)

    doc = rt.Document(original="甲方，乙方")
    ok, note = rt.tool_edit_text(doc, "甲方，乙", "，", "、")
    check("标点不在纠错范围内", not ok and "标点" in note, note)

    doc = rt.Document(original="项目已己经完成")
    ok, note = rt.tool_edit_text(doc, "已己经", "己", "已")
    check("形近但不同音 → 类型门拦下", not ok and "不是可验证" in note, note)

    doc = rt.Document(original="项目已经完成")
    ok, note = rt.tool_edit_text(doc, "已经完成", "经", "己")
    check("同韵母只差鼻韵尾（经→己）算近音", ok and note.endswith("（近音字）"), note)

    # 平翘舌不分（搜 sōu / 收 shōu、事 shì / 四 sì）也是近音
    doc = rt.Document(original="倒货验搜，卖方自行实施")
    ok, note = rt.tool_edit_text(doc, "验搜，卖方", "搜", "收")
    check("平翘舌不分算近音（搜→收）", ok and note.endswith("（近音字）"), note)

    doc = rt.Document(original="事后处理")
    ok, note = rt.tool_edit_text(doc, "事后处理", "事", "四")
    check("平翘舌不分算近音（事→四）", ok and note.endswith("（近音字）"), note)


def test_partial_block() -> None:
    """实测回归：模型把 8 处错字写在同一块里，当时因为其中一个平翘舌改动过不了门，
    整块被丢掉，另外几处真错字跟着一起没了。逐处落地之后不该再发生。"""
    text = "4.1买方有全对合同货物进行抽捡和倒货验搜，卖方应积极配和并提供抽捡所需的姿料和必要条键。"
    old = "有全对合同货物进行抽捡和倒货验搜，卖方应积极配和并提供抽捡所需的姿料和必要条键"
    new = "有权对合同货物进行抽检和到货验收，卖方应积极配合并提供抽检所需的资料和必要条件"
    doc = rt.Document(original=text)
    ok, note = rt.tool_edit_text(doc, text, old, new)
    check("整块 8 处一起落地（含平翘舌的搜→收）", ok and len(doc.replaced) == 8, (doc.replaced, note))
    check(
        "  → 001.txt 就是改好的那一句",
        doc.working() == "4.1买方有权对合同货物进行抽检和到货验收，卖方应积极配合并提供抽检所需的资料和必要条件。",
        doc.working(),
    )

    # 一块里夹一处真正过不了门的（形近不同音的 己→已），其余照常落地
    doc = rt.Document(original="项目已己经完成，承但责任")
    ok, note = rt.tool_edit_text(doc, "已己经完成，承但责任", "已己经完成，承但责任", "已已经完成，承担责任")
    check("夹一处过不了门的：其余照常落地", ok and doc.replaced == {9: "担"}, (doc.replaced, note))
    check("  → 过不了门的那处被回灌", "没落地的" in note and "己" in note, note)


def test_delete_char() -> None:
    # 两字连写：右边那个字和它组成不了别处出现过的词 → 是多打了一个，删掉。
    text = "首付款、进度款及尾款款前，甲方应支付。"
    doc = rt.Document(original=text)
    ok, note = rt.tool_edit_text(doc, "尾款款前", "款", "")
    check("两字连写、右边组不出词 → 删掉一个", ok and doc.working() == "首付款、进度款及尾款前，甲方应支付。", (doc.working(), note))
    check("  → 记成「同字连写」", note.endswith("（同字连写）"), note)

    # 两字连写、但它和右边那个字是全文里出现过的搭配（两个词的交界）→ 不动
    doc = rt.Document(original="增值税税率是百分之十三，税率不变。")
    ok, note = rt.tool_edit_text(doc, "增值税税", "税", "")
    check("两字连写但右边组得出词 → 不动（增值税税率）", not ok and "不是可执行的删字" in note, note)
    check("  → 001.txt 原样不动", not doc.deleted, doc.working())

    # 右边那个字别处只出现过一次、而且是同一种切法切出来的假词（买方仓库里的「方仓」）→ 不算证据，删
    doc = rt.Document(
        original="物资储备地点为买方仓库的，由买方负责；为卖方方仓库的，由卖方负责。"
    )
    ok, note = rt.tool_edit_text(doc, "卖方方仓库", "方", "")
    check(
        "同一种切法的假词不算证据 → 删掉一个",
        ok and "卖方仓库的，由卖方负责" in doc.working(),
        (doc.working(), note),
    )

    # 右边那个字别处只出现过一次、但用法不同（开发目标，）→ 是真词，不动
    doc = rt.Document(
        original="满足甲方的开发目标，则由甲方组织终验；1.2 项目目标：建设一套系统。"
    )
    ok, note = rt.tool_edit_text(doc, "项目目标", "目", "")
    check("用法不同的真词 → 不动（项目目标）", not ok and "不是可执行的删字" in note, note)

    # 孤零零的一个字：模型说它多打了就删（它和前后相邻的字都不相同）
    doc = rt.Document(original=text)
    ok, note = rt.tool_edit_text(doc, "首付款", "款", "")
    check("孤立字可以删", ok and note.endswith("（多打的字）"), note)

    # 多打了一个不相干的字：删掉它（开户航行 → 开户行）
    doc = rt.Document(original="开户航行：中国工商银行")
    ok, note = rt.tool_edit_text(doc, "开户航行", "航行", "行")
    check("删掉多打的「航」", ok and doc.working() == "开户行：中国工商银行", note)
    check("  → 记成「多打的字」", note.endswith("（多打的字）"), note)

    # 三个以上同一个字连写：一次收成一个
    doc = rt.Document(original="卖方应在接到退款款款款款通知后办理。")
    ok, note = rt.tool_edit_text(doc, "退款款款款款通知", "款款款款款", "款")
    check("连写 5 个「款」收成一个", ok and doc.working() == "卖方应在接到退款通知后办理。", note)
    check("  → 记成「同字连写」", note.endswith("（同字连写）"), note)

    doc = rt.Document(original="卖方应在接到退款款款款款通知后办理。")
    ok, note = rt.tool_edit_text(doc, "退款款款款款通知", "款款款款款", "")
    check("连写不能一段全删光", not ok, note)

    # 数字连写不算（5000 本来就这么写）
    doc = rt.Document(original="违约金 5000 元。")
    ok, note = rt.tool_edit_text(doc, "5000 元", "000", "0")
    check("数字连写不删", not ok, note)


# ── 比对与覆盖率 ──


def test_diff_errors() -> None:
    items = rt.diff_errors("中过某某集团", "中国某某集团")
    check(
        "比对出 1 处 replace",
        items == [{"position": 1, "original_char": "过", "correct_char": "国", "tag": "replace"}],
        items,
    )
    items = rt.diff_errors("尾款款前", "尾款前")
    check(
        "比对出 1 处 delete",
        items == [{"position": 2, "original_char": "款", "correct_char": "", "tag": "delete"}],
        items,
    )
    items = rt.diff_errors("接到退款款款款款通知", "接到退款通知")
    check(
        "连写收敛比对出连续 4 处 delete",
        [item["position"] for item in items] == [4, 5, 6, 7]
        and all(item["tag"] == "delete" for item in items),
        items,
    )
    items = rt.diff_errors("邮编码：100000", "邮政编码：100000")
    check(
        "比对出 1 处 insert",
        items == [{"position": 1, "original_char": "", "correct_char": "政", "tag": "insert"}],
        items,
    )
    check("没有改动时比对为空", rt.diff_errors("原文", "原文") == [])


def test_coverage() -> None:
    doc = rt.Document(original="abcdefghij")
    check("没读过时整篇都是未读", rt.unread_ranges(doc) == [(0, 9)], rt.unread_ranges(doc))
    check("下一处未读是开头", rt.next_unread(doc) == 0)

    rt.tool_read_text(doc, 0, 4)
    check("读过的区间记成已读", rt.unread_ranges(doc) == [(4, 9)], rt.unread_ranges(doc))
    check("读到位置 4", rt.next_unread(doc) == 4)

    text = rt.tool_read_text(doc, None, 100)
    check("不填 offset 就接着往下读", text.splitlines()[1].endswith("efghij"), text)
    check("读全之后没有未读区间", not rt.unread_ranges(doc))

    hits = rt.tool_find_text(rt.Document(original="中国某某集团有限责任公司"), "电网")
    check("find_text 报出位置与次数", "位置 4" in hits and "1 次" in hits, hits)


# ── 整轮循环 ──


def run_with(script: list[_StubMessage], text: str, max_turns: int = 6):
    saved = rt.tool_chain
    rt.tool_chain = _StubChain(script)
    try:
        return asyncio.run(rt.run_document_correction(text, max_turns=max_turns))
    finally:
        rt.tool_chain = saved


def test_run_loop() -> None:
    text = "承但相应的管理责任。"
    errors, debug = run_with(
        [
            _StubMessage(block("承但相应的", "承但", "承担")),
            _StubMessage(tool_calls=[{"name": "finish", "args": {}}]),
            _StubMessage(tool_calls=[{"name": "read_text", "args": {}}]),
            _StubMessage(tool_calls=[{"name": "finish", "args": {}}]),
        ],
        text,
    )
    check("整轮跑完并走到 finish", debug["finished"] is True, debug)
    check("漏读时 finish 被打回（共 4 轮）", len(debug["turns"]) == 4, debug["turns"])
    check("覆盖率 100%", debug["covered"] == len(text), debug)
    check(
        "比对出那一处同音字改动",
        errors == [{"position": 1, "original_char": "但", "correct_char": "担", "tag": "replace"}],
        errors,
    )

    errors, debug = run_with(
        [_StubMessage("我看完了，没问题。"), _StubMessage("确实没问题。"), _StubMessage("…")],
        text,
    )
    check("连着两轮既不读也不改就收工", len(debug["turns"]) == 2 and not errors, debug["turns"])


# ── 块写错了的欠账 ──


def test_rewrite_backlog() -> None:
    """「块写错了」的那几条打回要认得出，而且要和「这处不算错别字」分开。"""
    doc = rt.Document(original="承但相应的管理责任。")
    ok, note = rt.tool_edit_text(doc, "原文里没有这一段", "原文", "原问")
    check("引文没抄准 → 要求重写", not ok and rt._needs_rewrite(note), note)

    doc = rt.Document(original="承但相应的管理责任，承但相应的管理责任。")
    ok, note = rt.tool_edit_text(doc, "承但相应的", "承但", "承担")
    check("引文不唯一 → 要求重写", not ok and rt._needs_rewrite(note), note)

    doc = rt.Document(original="承但相应的管理责任。")
    ok, note = rt.tool_edit_text(doc, "相应的管理", "承但", "承担")
    check("引文里没有改前那行 → 要求重写", not ok and rt._needs_rewrite(note), note)

    doc = rt.Document(original="承但相应的管理责任。")
    ok, note = rt.tool_edit_text(doc, "承", "承", "成")
    check("引文只有 1 个字 → 要求重写", not ok and rt._needs_rewrite(note), note)

    doc = rt.Document(original="承但相应的管理责任。")
    ok, note = rt.tool_edit_text(doc, "承但相应的管理责任。", "", "承但")
    check("改前那行是空的 → 要求重写", not ok and rt._needs_rewrite(note), note)

    doc = rt.Document(original="承但相应的管理责任。")
    ok, note = rt.tool_edit_text(doc, "承但相应的", "承", "猫")
    check("过不了类型门 → 不算「块写错了」", not ok and not rt._needs_rewrite(note), note)


def test_backlog_blocks_finish() -> None:
    """实测回归：模型写了引文不唯一的块，回灌之后它直接调 finish，那处真错字就永远丢了。
    现在这类欠账没清就不让 finish。"""
    text = "承但相应的管理责任，承但相应的管理责任。"
    errors, debug = run_with(
        [
            _StubMessage(tool_calls=[{"name": "read_text", "args": {}}]),
            _StubMessage(block("承但相应的", "承但", "承担")),
            _StubMessage(tool_calls=[{"name": "finish", "args": {}}]),
            _StubMessage(block("承但相应的管理责任，", "承但", "承担")),
            _StubMessage(tool_calls=[{"name": "finish", "args": {}}]),
        ],
        text,
        max_turns=8,
    )
    check("引文不唯一那一轮没落地", debug["turns"][1]["applied"] == 0, debug["turns"][1])
    check("欠账没清时 finish 被打回（共 5 轮）", len(debug["turns"]) == 5, debug["turns"])
    check("重写之后走到 finish", debug["finished"] is True, debug)
    check(
        "那处真错字最后落地了",
        len(errors) == 1 and errors[0]["original_char"] == "但",
        errors,
    )

    # 一直写不对也不会把轮次烧光：打回满 BACKLOG_MAX 次就放它过。
    errors, debug = run_with(
        [
            _StubMessage(tool_calls=[{"name": "read_text", "args": {}}]),
            _StubMessage(block("承但相应的", "承但", "承担")),
            _StubMessage(tool_calls=[{"name": "finish", "args": {}}]),
            _StubMessage(tool_calls=[{"name": "finish", "args": {}}]),
        ],
        text,
        max_turns=10,
    )
    check(
        "打回满 BACKLOG_MAX 次就放它过",
        debug["finished"] is True and len(debug["turns"]) == 4,
        debug["turns"],
    )


def test_pdf_footer() -> None:
    """页脚判定：只认页码，别把带编号的正文行吃掉。"""
    for line in ("1 74", "第 3 页 共 74 页", "12 / 74", "- 12 -", "12"):
        check(f"页码「{line}」判为页脚", is_footer(line) is True)
    for line in (
        "1.1词语定义",
        "1.下列文件一起构成合同文件：",
        "1.1.1.2合同协议书：指买方和卖方共同千署的合同协议书。",
        "3.2供应量：卖方根据本协议所供应设备的预估需求量 630.00元 ，实际供应量",
        "2025-03-15",
    ):
        check(f"正文「{line[:12]}」不判为页脚", is_footer(line) is False)
    check(
        "目录行判为目录",
        is_directory_line("第一节合同协议书...........................................") is True,
    )


def main() -> int:
    for name, func in [
        ("PDF 页脚判定", test_pdf_footer),
        ("改动段解析", test_parse_corrections),
        ("定位与类型门", test_edit_localization),
        ("一块里逐处落地", test_partial_block),
        ("删字与连写", test_delete_char),
        ("000/001 比对", test_diff_errors),
        ("覆盖率记账", test_coverage),
        ("整轮循环", test_run_loop),
        ("块写错了的欠账", test_rewrite_backlog),
        ("欠账没清不让 finish", test_backlog_blocks_finish),
    ]:
        print(f"\n== {name} ==")
        func()
    print(f"\n失败 {len(FAILED)} 项" + ("：" + "、".join(FAILED) if FAILED else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
