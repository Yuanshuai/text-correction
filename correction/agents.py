"""合同字段审查：逐段给出审查意见，只审查、不改字。

纠错引擎不在本模块。模型改字的机制在 correction.readtool（000.txt / 001.txt 往返：
模型像改代码那样读原文、写 apply_patch 形态的小补丁，要改哪一处由服务端按引文搜出来），
提示词与实测数据见该模块开头。这里只保留 /review_contract 用的审查链。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from correction.chains import build_plain_json_chain, get_model
from correction.models.types import ContractReviewOutput
from correction.utils.log import log_error
from correction.utils.text import parse_structured_output

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  合同审查分析（仅审查，不纠错）
# ═══════════════════════════════════════════════════════════

CONTRACT_REVIEW_PROMPT_TEMPLATE = """你是合同审查员。逐段审查下面合同文本，结合字段信息和上下文给出审查意见。

审查要点：
1. 主体审查：合同主体信息是否完整、准确？
2. 条款完整性：是否缺少必要条款或约定？
3. 表述明确性：用语是否清晰、有无歧义或模糊表述？
4. 风险提示：有无对任一方明显不利的条款？有无遗漏重要约定？

输出格式：每条意见包含：
- approved: true（通过）/ false（有疑问，需关注）
- reason: 审查理由
- severity: "info"（信息提示）/ "warning"（需关注）/ "error"（严重问题）"""

contract_review_chain: Runnable | None = None


def init_contract_review(model: Any | None = None) -> None:
    global contract_review_chain
    contract_review_chain = build_plain_json_chain(
        ChatPromptTemplate.from_messages([
            ("system", CONTRACT_REVIEW_PROMPT_TEMPLATE),
            ("human", "字段信息：\n{field_info}\n\n上一段：\n{prev_paragraph}\n\n本段原文：\n{original_text}\n\n下一段：\n{next_paragraph}"),
        ]),
        ContractReviewOutput, model or get_model(),
    )
    logger.info("contract_review.init available=%s", contract_review_chain is not None)


async def run_contract_review(
    field_name: str,
    field_type: str,
    paragraph_text: str,
    prev_paragraph: str,
    next_paragraph: str,
    model: Any | None = None,
) -> dict:
    """对单个合同字段执行审查分析（不纠错）。"""
    if contract_review_chain is None:
        init_contract_review(model)

    field_info = f"字段名称：{field_name}\n字段类型：{field_type}\n所在段落：{paragraph_text}"
    try:
        raw = await contract_review_chain.ainvoke({
            "field_info": field_info,
            "prev_paragraph": prev_paragraph or "（无前文）",
            "original_text": paragraph_text,
            "next_paragraph": next_paragraph or "（无后文）",
        })
        parsed = parse_structured_output(raw, ContractReviewOutput)
        if parsed and parsed.reviews:
            return parsed.reviews[0].model_dump()
        return {"approved": True, "reason": "无审查意见", "severity": "info"}
    except Exception as exc:
        log_error("contract.review.fail", exc=exc, field=field_name)
        return {"approved": True, "reason": "审查调用失败", "severity": "info"}
