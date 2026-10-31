"""纠错记录的存储（MySQL）。

纠错记录原先只存在浏览器本地（localStorage）：换台机器、清一次缓存就没了，链接也分享不
出去。这里把它落进 MySQL——记录本身进库，原文 PDF 仍留在磁盘上、库里只存文档 id（结果
页按 id 去取那份原文）。

没配 MYSQL_DSN 时这一层整体关闭（available() 返回 False）：记录接口回 503、前端自动退回
「记录只存本机浏览器」的老做法，纠错功能一点不受影响。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlparse
from uuid import uuid4

import pymysql
from pymysql.cursors import DictCursor

from correction.config import (
    MYSQL_CONNECT_TIMEOUT, MYSQL_DSN, MYSQL_POOL_SIZE, MYSQL_RECORD_TABLE, MYSQL_TIMEOUT,
)
from correction.utils.log import log_info, log_warning


class RecordStoreUnavailable(RuntimeError):
    """记录存储没配、或连不上：接口据此回 503，让前端退回本地存储。"""


# 记录 id：沿用前端原来的「20261016-120000-ab12cd」长相，分享出去的链接好认
_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}")
# 表名要拼进 SQL（标识符没法用占位符传），所以只允许字母数字下划线
_TABLE = re.compile(r"[A-Za-z0-9_]{1,64}")
# 结果页那份原文 PDF 的 id（见 convert.store_pdf）
_DOC_ID = re.compile(r"[0-9a-f]{32}")

# 建表：列表页要读的字段单独做列，整份结果塞 payload。
# 原文 PDF 不进库——几十页的 PDF 塞进 LONGTEXT 只会把库撑大，磁盘上按 id 取更省事。
_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  id              VARCHAR(64)  NOT NULL,
  kind            VARCHAR(16)  NOT NULL DEFAULT 'file',
  filename        VARCHAR(512) NOT NULL DEFAULT '',
  engine_status   VARCHAR(16)  NOT NULL DEFAULT 'ok',
  source_kind     VARCHAR(16)  NULL,
  doc_id          CHAR(32)     NULL,
  issue_count     INT          NOT NULL DEFAULT 0,
  paragraph_count INT          NOT NULL DEFAULT 0,
  convert_ms      INT          NULL,
  parse_ms        INT          NULL,
  correct_ms      INT          NULL,
  total_ms        INT          NULL,
  ignored         TEXT         NULL,
  payload         LONGTEXT     NOT NULL,
  created_at      DATETIME(3)  NOT NULL,
  PRIMARY KEY (id),
  KEY idx_created (created_at),
  KEY idx_doc (doc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_SUMMARY_COLUMNS = (
    "id, kind, filename, engine_status, source_kind, doc_id, issue_count, paragraph_count, "
    "convert_ms, parse_ms, correct_ms, total_ms, created_at"
)

_pool = None
_pool_lock = threading.Lock()
# 建表成功之前每个请求都试一次会刷屏（库暂时连不上时），失败后退避一会儿再试
_ready_until = 0.0
_RETRY_SECONDS = 60


def available() -> bool:
    """配置里有没有 MySQL：没有就是「记录只存浏览器本地」的老做法。"""
    return bool(MYSQL_DSN)


def _table() -> str:
    if not _TABLE.fullmatch(MYSQL_RECORD_TABLE):
        raise RecordStoreUnavailable(
            f"MYSQL_RECORD_TABLE 只能由字母、数字、下划线组成，实际为 {MYSQL_RECORD_TABLE!r}"
        )
    return MYSQL_RECORD_TABLE


def _connect_kwargs() -> dict:
    """从 MYSQL_DSN 解析连接参数（mysql://用户:密码@主机:端口/库名?charset=utf8mb4）。"""
    raw = MYSQL_DSN
    parsed = urlparse(raw if "://" in raw else f"mysql://{raw}")
    if parsed.scheme not in ("mysql", "mysql+pymysql"):
        raise RecordStoreUnavailable(
            f"MYSQL_DSN 只认 mysql:// 开头的地址，实际为 {raw!r}"
        )
    charset = "utf8mb4"
    for key, value in parse_qsl(parsed.query):
        if key.lower() == "charset" and value:
            charset = value
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": unquote(parsed.path.lstrip("/")) or None,
        "charset": charset,
        "cursorclass": DictCursor,
        # 记录很小、一次一条，超时给短一点：库有问题时早点失败，别把请求挂在那儿
        "connect_timeout": MYSQL_CONNECT_TIMEOUT,
        "read_timeout": MYSQL_TIMEOUT,
        "write_timeout": MYSQL_TIMEOUT,
        # 连接池自己管提交，读写都是一条语句，不需要事务
        "autocommit": True,
    }


def _new_connection():
    try:
        return pymysql.connect(**_connect_kwargs())
    except Exception as exc:
        raise RecordStoreUnavailable(f"连接 MySQL 失败：{exc}") from exc


def _borrow():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = queue.LifoQueue()
    try:
        connection = _pool.get_nowait()
    except queue.Empty:
        return _new_connection()
    # 池子里的连接可能已被库那边掐掉（wait_timeout），交出去前先确认还活着
    try:
        connection.ping(reconnect=True)
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        return _new_connection()
    return connection


def _give_back(connection) -> None:
    if _pool is not None and _pool.qsize() < MYSQL_POOL_SIZE:
        _pool.put(connection)
        return
    try:
        connection.close()
    except Exception:
        pass


@contextmanager
def _cursor():
    connection = _borrow()
    try:
        with connection.cursor() as cursor:
            yield cursor
    except Exception:
        # 出错时连接状态不明（可能半途断了），直接丢掉，别把坏连接放回池子
        try:
            connection.close()
        except Exception:
            pass
        raise
    else:
        _give_back(connection)


def init_store() -> bool:
    """建表（幂等）。没配 DSN 或连不上就返回 False：只记日志，不影响纠错本身。"""
    global _ready_until
    if not available():
        return False
    try:
        with _cursor() as cursor:
            cursor.execute(_DDL.format(table=_table()))
    except Exception as exc:
        log_warning("records.init_failed", exc=str(exc))
        _ready_until = time.monotonic() + _RETRY_SECONDS
        return False
    _ready_until = float("inf")
    log_info("records.ready", table=_table())
    return True


def _ensure() -> None:
    """每次读写前确认存储可用；不可用就抛 RecordStoreUnavailable（接口回 503）。"""
    if not available():
        raise RecordStoreUnavailable("服务未配置 MySQL：纠错记录只存在浏览器本地")
    if _ready_until > time.monotonic():
        return
    if not init_store():
        raise RecordStoreUnavailable("MySQL 暂时不可用：纠错记录只存在浏览器本地")


def shutdown_store() -> None:
    """退出时把池子里的连接关干净（gunicorn 重启 worker 不留半开连接）。"""
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is None:
        return
    while True:
        try:
            connection = pool.get_nowait()
        except queue.Empty:
            break
        try:
            connection.close()
        except Exception:
            pass


def _utc_now() -> datetime:
    # 库里统一存 UTC 的 DATETIME：换台机器看记录，时间不会跟着本机时区跑偏
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def _int_or_none(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _new_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"


def _clean_id(value) -> str:
    text = str(value or "").strip()
    if not text:
        return _new_id()
    if not _ID.fullmatch(text):
        raise ValueError(f"记录 id 不合法：{text!r}")
    return text


def _clean_doc_id(value) -> str | None:
    text = str(value or "").strip().lower()
    return text if _DOC_ID.fullmatch(text) else None


def _summary(row: dict) -> dict:
    timings = {}
    for key, column in (
        ("convertMs", "convert_ms"),
        ("parseMs", "parse_ms"),
        ("correctMs", "correct_ms"),
        ("totalMs", "total_ms"),
    ):
        value = row.get(column)
        if value is not None:
            timings[key] = int(value)
    return {
        "id": row.get("id"),
        "kind": row.get("kind") or "file",
        "filename": row.get("filename") or "",
        "engineStatus": row.get("engine_status") or "ok",
        "correctionCount": int(row.get("issue_count") or 0),
        "unitsWithCorrection": int(row.get("paragraph_count") or 0),
        "createdAt": _iso(row.get("created_at")),
        "timings": timings or None,
        "hasDoc": bool(row.get("doc_id")),
    }


def save_record(record: dict) -> dict:
    """存一条纠错记录（id 已存在就覆盖，方便把浏览器里的老记录导进来）。

    调用方（前端）把结果页要用的整份数据交过来：units（逐段纠错结果）、blocks（全文分段）、
    highlights（改动贴在页面哪儿）进 payload；列表页要读的字段单独做列。
    """
    _ensure()
    record_id = _clean_id(record.get("id"))
    kind = "text" if str(record.get("kind") or "").strip() == "text" else "file"
    filename = str(record.get("filename") or "").strip()[:512] or "未命名"
    engine_status = "degraded" if str(record.get("engineStatus") or "") == "degraded" else "ok"
    doc = record.get("doc") if isinstance(record.get("doc"), dict) else None
    doc_id = _clean_doc_id(doc.get("docId")) if doc else None
    source_kind = None
    if doc_id:
        source_kind = str(doc.get("sourceKind") or "").strip()[:16] or "pdf"
    timings = record.get("timings") if isinstance(record.get("timings"), dict) else {}
    payload = json.dumps(
        {
            "units": record.get("units") or [],
            "blocks": record.get("blocks") or None,
            "highlights": record.get("highlights") or None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    created_at = _utc_now()
    with _cursor() as cursor:
        cursor.execute(
            f"""INSERT INTO {_table()} (
                    id, kind, filename, engine_status, source_kind, doc_id,
                    issue_count, paragraph_count, convert_ms, parse_ms, correct_ms, total_ms,
                    payload, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    kind = VALUES(kind), filename = VALUES(filename),
                    engine_status = VALUES(engine_status), source_kind = VALUES(source_kind),
                    doc_id = VALUES(doc_id), issue_count = VALUES(issue_count),
                    paragraph_count = VALUES(paragraph_count), convert_ms = VALUES(convert_ms),
                    parse_ms = VALUES(parse_ms), correct_ms = VALUES(correct_ms),
                    total_ms = VALUES(total_ms), payload = VALUES(payload),
                    created_at = VALUES(created_at)""",
            (
                record_id, kind, filename, engine_status, source_kind, doc_id,
                _int_or_none(record.get("correctionCount")) or 0,
                _int_or_none(record.get("unitsWithCorrection")) or 0,
                _int_or_none(timings.get("convertMs")),
                _int_or_none(timings.get("parseMs")),
                _int_or_none(timings.get("correctMs")),
                _int_or_none(timings.get("totalMs")),
                payload, created_at,
            ),
        )
    log_info("records.saved", record_id=record_id, kind=kind, doc_id=doc_id, bytes=len(payload))
    return {"id": record_id, "createdAt": _iso(created_at)}


def list_records(limit: int = 50, offset: int = 0) -> dict:
    """记录列表（不带全文）：只回列表页要显示的那几列，结果页再按 id 取整份。"""
    _ensure()
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    with _cursor() as cursor:
        cursor.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM {_table()} "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        rows = cursor.fetchall()
        cursor.execute(f"SELECT COUNT(*) AS total FROM {_table()}")
        row = cursor.fetchone() or {}
    return {"records": [_summary(item) for item in rows], "total": int(row.get("total") or 0)}


def get_record(record_id: str) -> dict | None:
    """取一条记录的整份内容（结果页用）。"""
    _ensure()
    record_id = str(record_id or "").strip()
    if not _ID.fullmatch(record_id):
        return None
    with _cursor() as cursor:
        cursor.execute(
            f"SELECT {_SUMMARY_COLUMNS}, ignored, payload FROM {_table()} WHERE id = %s",
            (record_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row.get("payload") or "{}")
    except ValueError:
        payload = {}
    ignored = []
    if row.get("ignored"):
        try:
            ignored = sorted(int(key) for key in json.loads(row["ignored"]))
        except (TypeError, ValueError):
            ignored = []
    record = _summary(row)
    record["units"] = payload.get("units") or []
    record["blocks"] = payload.get("blocks") or None
    record["highlights"] = payload.get("highlights") or None
    record["ignored"] = ignored or None
    record["doc"] = (
        {
            "docId": row["doc_id"],
            "sourceKind": row.get("source_kind") or "pdf",
            # 相对地址：谁打开都按自己的服务地址去取，不认上传那台机器的地址
            "pdfUrl": f"/document/{row['doc_id']}",
        }
        if row.get("doc_id")
        else None
    )
    return record


def set_ignored(record_id: str, keys) -> list[int]:
    """记下「哪些是误报、不用改」：传空就是全部恢复。"""
    _ensure()
    record_id = str(record_id or "").strip()
    if not _ID.fullmatch(record_id):
        raise ValueError(f"记录 id 不合法：{record_id!r}")
    clean = sorted({
        int(key) for key in (keys or [])
        if isinstance(key, int) and not isinstance(key, bool)
    })
    with _cursor() as cursor:
        cursor.execute(
            f"UPDATE {_table()} SET ignored = %s WHERE id = %s",
            (json.dumps(clean) if clean else None, record_id),
        )
        if cursor.rowcount == 0:
            raise LookupError(record_id)
    return clean


def delete_record(record_id: str) -> dict | None:
    """删一条记录。返回它引用过的 PDF id，调用方据此把没人再用的原文也删掉。"""
    _ensure()
    record_id = str(record_id or "").strip()
    if not _ID.fullmatch(record_id):
        return None
    with _cursor() as cursor:
        cursor.execute(f"SELECT doc_id FROM {_table()} WHERE id = %s", (record_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cursor.execute(f"DELETE FROM {_table()} WHERE id = %s", (record_id,))
    log_info("records.deleted", record_id=record_id)
    return {"docId": row.get("doc_id")}


def is_document_referenced(doc_id: str) -> bool:
    """这份原文还被某条记录引用着吗？被引用就不能当过期文件清掉。"""
    if not available():
        return False
    doc_id = _clean_doc_id(doc_id)
    if not doc_id:
        return False
    try:
        _ensure()
        with _cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM {_table()} WHERE doc_id = %s LIMIT 1", (doc_id,))
            return cursor.fetchone() is not None
    except Exception as exc:
        # 库暂时连不上：宁可留着文件占点磁盘，也不能把记录要用的原文删了
        log_warning("records.reference_check_failed", doc_id=doc_id, exc=str(exc))
        return True