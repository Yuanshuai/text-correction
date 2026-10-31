from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

import json
import logging
import re
from typing import get_origin, get_args

from correction.model import create_chat_model

logger = logging.getLogger(__name__)

_model = None


def get_model():
    """懒加载 ChatOpenAI 实例，避免模块导入时因缺少 API key 崩溃。"""
    global _model
    if _model is None:
        _model = create_chat_model()
    return _model


def _build_json_example(schema: type[BaseModel]) -> str:
    """从 pydantic schema 生成简洁的 JSON 格式示例，注入到 prompt 中。

    对 list 字段：若是嵌套 pydantic 模型则递归生成一项示例；否则展示 ["<item>"]。
    """
    example = {}
    for name, info in schema.model_fields.items():
        annotation = info.annotation
        origin = get_origin(annotation)
        if origin is list:
            args = get_args(annotation)
            if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
                inner = _build_json_example(args[0])
                inner = inner.replace('{{', '{').replace('}}', '}')
                example[name] = [json.loads(inner)]
            else:
                example[name] = ["<item>"]
        elif annotation is str:
            example[name] = "<string>"
        elif annotation is int:
            example[name] = 0
        elif annotation is float:
            example[name] = 0.0
        elif annotation is bool:
            example[name] = False
        else:
            example[name] = "..."
    result = json.dumps(example, ensure_ascii=False)
    # LangChain 模板会把 { } 解析为变量，必须双花括号转义
    return result.replace('{', '{{').replace('}', '}}')


def _make_json_instruction(schema: type[BaseModel]) -> str:
    """生成 JSON 输出格式指令字符串。"""
    example = _build_json_example(schema)
    return (
        "\n\n【JSON 输出格式——严格遵守】\n"
        "你必须输出一个合法的 JSON 对象，不要输出任何其他文本，不要用 markdown 代码块包裹。\n"
        f"格式示例：{example}"
    )


def _repair_truncated_json(text: str) -> str | None:
    """尝试修复被截断的 JSON：补全缺失的括号和引号。"""
    s = text.strip()
    # 找到最后一个完整对象 { ... }
    brace_count = 0
    last_complete = -1
    in_string = False
    escape_next = False
    for i, ch in enumerate(s):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0:
                last_complete = i
    if last_complete > 0:
        s = s[:last_complete + 1]

    # 先闭合末尾未闭合的字符串（否则补的括号会在字符串内部，JSON 仍非法）
    quote_count = s.count('"')
    if quote_count % 2 != 0:
        s += '"'

    # 再补全未闭合的括号
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    s += '}' * open_braces + ']' * open_brackets

    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        return None


def _empty_for_schema(schema: type[BaseModel]) -> BaseModel:
    """返回 schema 的空默认实例。"""
    try:
        return schema()
    except Exception:
        return schema.model_validate({})


def _extract_json_from_text(text: str) -> str | None:
    """从混合文本中提取 JSON 对象或数组（使用括号计数，比正则更可靠）。

    用于处理 LLM 在 JSON 前后附加说明文字的情况（qwen 系列模型的常见行为）。
    """
    stripped = text.strip()

    # 1) markdown 代码块
    m = re.search(r"```(?:json)?\s*([\s\S]*?)(?:```|$)", stripped, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # 2) 找最外层 { ... } 使用括号深度计数
    brace_start = stripped.find('{')
    if brace_start != -1:
        depth = 0
        for i in range(brace_start, len(stripped)):
            ch = stripped[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return stripped[brace_start:i + 1]

    # 3) 找最外层 [ ... ]
    bracket_start = stripped.find('[')
    if bracket_start != -1:
        depth = 0
        for i in range(bracket_start, len(stripped)):
            ch = stripped[i]
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    return stripped[bracket_start:i + 1]

    return None


def build_plain_json_chain(
    prompt: ChatPromptTemplate,
    schema: type[BaseModel],
    llm=None,
) -> Runnable:
    """构建纯文本调用 + 手动 JSON 解析的 LLM 链。

    用于兼容 with_structured_output 实现有缺陷的模型（如 qwen3.5-27b）：
    - json_schema 模式返回巨大整数字符串（"500000...0"）
    - function_calling 模式将嵌套数组序列化为 JSON 字符串而非数组
    - 纯文本模式下模型可以可靠地输出合法 JSON

    自动将 JSON 格式指令注入 system message，无需调用方修改 prompt。
    链结构：(inject_json | prompt) | llm | json_parser
    """

    llm = llm or get_model() or create_chat_model()
    json_instruction = _make_json_instruction(schema)

    # 重新构建 prompt，在 system 消息末尾追加 JSON 指令
    from langchain_core.prompts import SystemMessagePromptTemplate
    augmented_messages = []
    for msg in prompt.messages:
        if isinstance(msg, SystemMessagePromptTemplate):
            augmented_messages.append(
                SystemMessagePromptTemplate.from_template(msg.prompt.template + json_instruction)
            )
        else:
            augmented_messages.append(msg)
    augmented_prompt = ChatPromptTemplate.from_messages(augmented_messages)

    def _parse_json_output(raw) -> BaseModel:
        if isinstance(raw, schema):
            return raw
        content = raw.content if hasattr(raw, "content") else str(raw)
        cleaned = content.strip()
        # 去除可能的 markdown 代码块包裹
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:])
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 模型可能在 JSON 前后附加了说明文字（qwen 系列常见行为）
            # 尝试用括号计数从混合文本中提取 JSON
            extracted = _extract_json_from_text(content)
            if extracted:
                try:
                    data = json.loads(extracted)
                    logger.info("plain_json.extracted schema=%s", schema.__name__)
                except json.JSONDecodeError:
                    logger.warning("plain_json.parse_fail schema=%s content_preview=%s",
                                   schema.__name__, cleaned[:200])
                    # 尝试修复截断的 JSON
                    repaired = _repair_truncated_json(extracted)
                    if repaired:
                        try:
                            data = json.loads(repaired)
                            logger.info("plain_json.repaired schema=%s", schema.__name__)
                        except json.JSONDecodeError:
                            # 无法修复 → 返回空结果
                            return _empty_for_schema(schema)
                    else:
                        return _empty_for_schema(schema)
            else:
                # 最后手段：尝试修复截断的 JSON
                repaired = _repair_truncated_json(content)
                if repaired:
                    try:
                        data = json.loads(repaired)
                        logger.info("plain_json.repaired schema=%s", schema.__name__)
                    except json.JSONDecodeError:
                        repaired = None
                if not repaired:
                    logger.warning("plain_json.parse_fail schema=%s content_preview=%s",
                                   schema.__name__, cleaned[:200])
                    # 无法修复 → 返回空结果（不抛异常，让上层降级处理）
                    return _empty_for_schema(schema)

        # 模型可能返回裸数组 [item, ...] 而非 {field_name: [item, ...]}
        # 自动匹配 schema 中第一个 list 类型字段进行包装
        if isinstance(data, list):
            for field_name, field_info in schema.model_fields.items():
                if get_origin(field_info.annotation) is list:
                    data = {field_name: data}
                    logger.debug("plain_json.auto_wrap schema=%s field=%s", schema.__name__, field_name)
                    break

        return schema.model_validate(data)

    return augmented_prompt | llm | RunnableLambda(_parse_json_output, name=f"{schema.__name__}_parser")
