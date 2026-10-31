"""纠错调试日志：记录送入模型的文件、模型的工具调用、机械门的裁决与失败原因。

固定写入 tool_debug_current.jsonl；新请求到达时先清空上一次请求的日志，
再把当前请求的所有批次追加到同一个文件，每行一个批次。
"""

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# 调试日志目录，可通过环境变量覆盖
DEBUG_LOG_DIR = os.environ.get("DEBUG_LOG_DIR", os.path.join(os.path.dirname(__file__), "..", "debug_logs"))
DEBUG_LOG_FILENAME = "tool_debug_current.jsonl"

_last_request_id: str | None = None
_write_lock = threading.Lock()


def _ensure_dir() -> str:
    os.makedirs(DEBUG_LOG_DIR, exist_ok=True)
    return DEBUG_LOG_DIR


def _current_request_id() -> str:
    from correction.utils.log import _rid
    return _rid() or "unknown"


def write_tool_debug(records: list[dict[str, Any]]) -> str | None:
    """把一个请求的纠错调试记录写入 JSONL，返回写入的文件路径。"""
    if not records:
        return None

    global _last_request_id
    _ensure_dir()
    request_id = _current_request_id()
    jsonl_path = os.path.join(DEBUG_LOG_DIR, DEBUG_LOG_FILENAME)

    with _write_lock:
        if request_id != _last_request_id:
            for name in os.listdir(DEBUG_LOG_DIR):
                if name.startswith("tool_debug_") and name.endswith(".jsonl"):
                    try:
                        os.remove(os.path.join(DEBUG_LOG_DIR, name))
                    except OSError:
                        pass
            _last_request_id = request_id

        with open(jsonl_path, "a", encoding="utf-8") as f:
            for record in records:
                payload = {"request_id": request_id, **record}
                f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    logger.info(
        "debug_log.written request_id=%s batches=%d jsonl=%s",
        request_id, len(records), jsonl_path,
    )
    return jsonl_path
