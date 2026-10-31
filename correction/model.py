import httpx
from langchain_openai import ChatOpenAI

from correction.config import (
    BASE_URL,
    MODEL_NAME,
    API_KEY,
    LLM_DISABLE_THINKING,
    LLM_TIMEOUT,
    LLM_TLS_VERIFY,
)


def _parse_json_env(name: str) -> dict:
    import json, os
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _disable_thinking_kwargs() -> dict:
    if not LLM_DISABLE_THINKING:
        return {}
    # 生成模型为 Qwen3.5-397b，禁用思考的标准开关是 chat_template_kwargs.enable_thinking=False
    # （vLLM/SGLang 的 Qwen3 chat template 约定）；同时下发顶层 enable_thinking 兼容直通网关。
    # 其它网关专用字段（如 Anthropic 风格的 thinking.type）通过 LLM_EXTRA_BODY 注入。
    extra_body = {
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
    }
    extra_body.update(_parse_json_env("LLM_EXTRA_BODY"))
    kwargs: dict = {"extra_body": extra_body}
    reasoning_effort = (__import__("os").environ.get("LLM_REASONING_EFFORT") or "").strip()
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


def _chat_kwargs(**overrides) -> dict:
    timeout = httpx.Timeout(timeout=LLM_TIMEOUT + 10, connect=10.0, read=LLM_TIMEOUT, write=10.0, pool=5.0)
    kwargs: dict = {
        "base_url": BASE_URL,
        "model": MODEL_NAME,
        "temperature": 0,
        "max_tokens": 4096,
        "max_retries": 2,
        "timeout": timeout,
        "http_client": httpx.Client(verify=LLM_TLS_VERIFY, timeout=timeout),
        "http_async_client": httpx.AsyncClient(verify=LLM_TLS_VERIFY, timeout=timeout),
    }
    # 统一的 OpenAI 兼容接法：Bearer token
    kwargs["api_key"] = API_KEY or "sk-none"
    # 必须禁用思考：Qwen3 系列默认把思考过程写进 content，纠错结果就没法解析了。
    # 实测：不禁用时 content 是一段思考散文（以 </think> 收尾）、completion_tokens 63；
    # 禁用后 content 干净、14 tokens。
    kwargs.update(_disable_thinking_kwargs())
    kwargs.update(overrides)
    return kwargs


def create_chat_model(**overrides) -> ChatOpenAI:
    return ChatOpenAI(**_chat_kwargs(**overrides))
