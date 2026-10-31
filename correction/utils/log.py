import logging
import contextvars
from typing import Any

from correction.config import LOG_LEVEL, LOG_TEXT_PREVIEW, OPENAI_LOG_LEVEL

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("correction")
logging.getLogger("openai._base_client").setLevel(getattr(logging, OPENAI_LOG_LEVEL, logging.DEBUG))

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_engine_degraded: contextvars.ContextVar[bool] = contextvars.ContextVar("engine_degraded", default=False)


def _rid() -> str:
    return _request_id.get()


def mark_engine_degraded() -> None:
    """标记当前请求的纠错引擎已降级（链不可用/解析失败等非正常路径）。"""
    _engine_degraded.set(True)


def is_engine_degraded() -> bool:
    return _engine_degraded.get()


def reset_engine_degraded_flag() -> None:
    """重置引擎降级标记为 False（请求开始时调用）。"""
    _engine_degraded.set(False)


def _preview_text(text: str, limit: int | None = None) -> str:
    limit = limit or LOG_TEXT_PREVIEW
    compact = text.replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}…({len(text)}字)"


def log_info(event: str, **fields: Any) -> None:
    parts = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.info("[%s] %s %s", _rid(), event, parts)


def log_warning(event: str, **fields: Any) -> None:
    parts = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.warning("[%s] %s %s", _rid(), event, parts)


def log_error(event: str, exc: BaseException | None = None, **fields: Any) -> None:
    parts = " ".join(f"{k}={v!r}" for k, v in fields.items())
    if exc is not None:
        logger.error("[%s] %s %s error=%s", _rid(), event, parts, exc, exc_info=True)
    else:
        logger.error("[%s] %s %s", _rid(), event, parts)


def log_correction_stage(stage: str, original: str, result: dict[str, Any] | None = None, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "char_len": len(original),
        "preview": _preview_text(original, 80),
        **extra,
    }
    if result is not None:
        errors = result.get("errors") or []
        payload["error_count"] = len(errors)
        payload["wrong_ids"] = result.get("wrong_ids", [])
        if errors:
            payload["errors_sample"] = errors[:5]
    log_info(f"correction.{stage}", **payload)

