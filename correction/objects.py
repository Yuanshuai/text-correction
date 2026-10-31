"""原文文件的对象存储（MinIO / 其它 S3 兼容服务）。

结果页要展示的那份 PDF 放这里，数据库里只留对象 key（S3_PREFIX + 文档 id）。放本机磁盘
在多副本和容器重建时会丢：库里的记录还在，链接点开却没有原文；对象存储谁都能取到，
也不会跟着某个容器一起消失。

没配 S3_*（见 config）时这一层整体关闭（available() 返回 False），store_pdf 照旧退回本机磁盘。
"""

from __future__ import annotations

import re
import threading
import time
from datetime import timedelta
from io import BytesIO
from urllib.parse import urlparse

import urllib3
from minio import Minio
from minio.error import S3Error

from correction.config import (
    S3_ACCESS_KEY, S3_BUCKET, S3_ENDPOINT, S3_PORT, S3_PREFIX, S3_PRESIGN_SECONDS,
    S3_PUBLIC_ENDPOINT, S3_SECRET_KEY, S3_TIMEOUT, S3_USE_SSL,
)
from correction.utils.log import log_info, log_warning

# 文档 id 是服务端生成的 32 位十六进制（见 convert.store_pdf），这里再挡一道，
# 免得有人拿 ../.. 拼出别的对象名
_DOC_ID = re.compile(r"[0-9a-f]{32}")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_CHUNK = 64 * 1024
_RETRY_SECONDS = 60

_lock = threading.Lock()
_client: Minio | None = None
_signer: Minio | None = None
_ready_until = 0.0


def available() -> bool:
    """四样配齐（地址、密钥、桶）才启用；少一样就当没配，退回本机磁盘。"""
    return bool(S3_ENDPOINT and S3_ACCESS_KEY and S3_SECRET_KEY and S3_BUCKET)


def describe() -> str:
    """日志里显示一下原文存哪儿。"""
    return S3_BUCKET if available() else "local"


def object_key(doc_id: str) -> str:
    if not _DOC_ID.fullmatch(doc_id or ""):
        raise ValueError(f"文档 id 不合法：{doc_id!r}")
    return f"{S3_PREFIX}{doc_id}.pdf"


def _host_port_secure(raw: str) -> tuple[str, int, bool]:
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if not parsed.hostname:
        raise ValueError(f"S3 地址不合法：{raw!r}")
    secure = parsed.scheme == "https"
    flag = (S3_USE_SSL or "").strip().lower()
    if flag in _TRUE:
        secure = True
    elif flag in _FALSE:
        secure = False
    port = S3_PORT or parsed.port or (443 if secure else 9000)
    return parsed.hostname, port, secure


def _build(raw: str, region: str | None = None) -> Minio:
    host, port, secure = _host_port_secure(raw)
    return Minio(
        # 新版 minio 客户端把端口并进 endpoint 了，没有单独的 port 参数
        f"{host}:{port}",
        access_key=S3_ACCESS_KEY,
        secret_key=S3_SECRET_KEY,
        secure=secure,
        # 区域已知就带上：不带的话客户端会先发一次「问桶在哪个区域」的请求
        region=region,
        # 连接超时给短一点、读超时用 S3_TIMEOUT：对象存储不通时早点失败，别把请求挂住
        http_client=urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=5, read=S3_TIMEOUT),
            retries=urllib3.Retry(total=2, backoff_factor=0.2),
        ),
    )


def _get_client() -> Minio:
    global _client
    with _lock:
        if _client is None:
            _client = _build(S3_ENDPOINT)
        return _client


def _get_signer() -> Minio | None:
    """只用来在本地算签名地址的客户端：指向对外地址，签名时不再向它发请求。

    host 是算进签名里的，所以对外地址要单独签一份——拿后端连的那个内网地址签出来的链接，
    浏览器那边对不上。区域要先向内网地址问一次再带进来：不带的话客户端会自己向「对外
    地址」问桶在哪个区域，而后端不一定连得上那个地址（比如对外地址是给浏览器用的），
    一问就失败、链接就签不出来，白白退回后端转发。
    """
    global _signer
    if not S3_PUBLIC_ENDPOINT:
        return None
    with _lock:
        if _signer is not None:
            return _signer
    # 区域要在锁外问：_get_client() 自己也要拿这把锁，而 Lock 不可重入，
    # 拿着锁去调它会当场死锁
    region = None
    try:
        # minio 客户端没有公开的「查区域」入口，这里用它的内部方法；
        # 拿不到就算了（不带区域去签，退回原来的行为）
        region = _get_client()._get_region(S3_BUCKET)
    except Exception:
        region = None
    with _lock:
        if _signer is None:
            _signer = _build(S3_PUBLIC_ENDPOINT, region)
        return _signer


def init_store() -> bool:
    """确认桶在（不在就建）。连不上只记日志，不影响纠错本身。"""
    global _ready_until
    if not available():
        return False
    try:
        client = _get_client()
        if not client.bucket_exists(S3_BUCKET):
            client.make_bucket(S3_BUCKET)
            log_info("objects.bucket_created", bucket=S3_BUCKET)
    except S3Error as exc:
        # 多进程/多副本会同时启动、一起建桶，后到的这个错就是「桶已经在了」，算成功：
        # 不然每次开机都会刷几条看着像故障的日志，还会白白退避一分钟
        if exc.code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            log_warning("objects.init_failed", endpoint=S3_ENDPOINT, exc=str(exc))
            _ready_until = time.monotonic() + _RETRY_SECONDS
            return False
    except Exception as exc:
        log_warning("objects.init_failed", endpoint=S3_ENDPOINT, exc=str(exc))
        _ready_until = time.monotonic() + _RETRY_SECONDS
        return False
    _ready_until = float("inf")
    log_info("objects.ready", bucket=S3_BUCKET, prefix=S3_PREFIX, direct=bool(S3_PUBLIC_ENDPOINT))
    return True


def _ensure() -> None:
    """写之前确认桶在；失败也不拦着——真写不进去时调用方会拿到明确的报错。"""
    if _ready_until > time.monotonic():
        return
    init_store()


def put(doc_id: str, data: bytes) -> None:
    """上传一份原文。"""
    _ensure()
    _get_client().put_object(
        S3_BUCKET, object_key(doc_id), BytesIO(data), length=len(data),
        content_type="application/pdf",
    )


def exists(doc_id: str) -> bool:
    """对象在不在。连不上对象存储时按「不在」处理（让调用方退回本机那份），并记一条日志。"""
    try:
        size(doc_id)
        return True
    except S3Error:
        return False
    except Exception as exc:
        log_warning("objects.stat_failed", doc_id=doc_id, exc=str(exc))
        return False


def size(doc_id: str) -> int:
    """对象多大（转发时算 Range 要用）。没有这份对象时抛 S3Error。"""
    return int(_get_client().stat_object(S3_BUCKET, object_key(doc_id)).size)


def stream(doc_id: str, offset: int = 0, length: int = 0):
    """按块读对象（后端转发那份时用）。生成器：读完、或调用方提前断开，都会把连接还回去。"""
    response = _get_client().get_object(
        S3_BUCKET, object_key(doc_id), offset=offset, length=length,
    )
    try:
        for chunk in response.stream(_CHUNK):
            yield chunk
    finally:
        response.close()
        response.release_conn()


def presigned_url(doc_id: str) -> str | None:
    """给浏览器直连用的临时地址；没配对外地址、或签不出来就返回 None（由后端转发）。"""
    signer = _get_signer()
    if signer is None:
        return None
    try:
        return signer.presigned_get_object(
            S3_BUCKET, object_key(doc_id),
            expires=timedelta(seconds=S3_PRESIGN_SECONDS),
        )
    except Exception as exc:
        log_warning("objects.presign_failed", doc_id=doc_id, exc=str(exc))
        return None


def remove(doc_id: str) -> None:
    """删掉一份原文（记录被删时跟着删）。"""
    _get_client().remove_object(S3_BUCKET, object_key(doc_id))
