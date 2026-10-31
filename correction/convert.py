"""把 Office 文件转成 PDF：交给外部的转换服务（Gotenberg 那套 API）。

结果页要「跟直接打开原文件一个样」，只有 PDF 做得到——浏览器里把 PDF 一页页铺出来，
再把改动的字贴上高亮框。所以 Word/Excel 先转 PDF，之后跟 PDF 走同一条解析、纠错、
展示链路。这跟线上那套（audit/backend + pdf-service）是同一个做法：上传先转 PDF，
取文字时连每个字的坐标一起取，纠错只回「哪个字改成了什么」，位置全靠坐标。

转换服务地址由 CONVERT_SERVER 指定（POST {CONVERT_SERVER}/forms/libreoffice/convert，
multipart 字段名 files）。没配就返回 None，调用方自己决定退回哪条路。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from io import BytesIO
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import httpx

from correction import objects
from correction.config import (
    CONVERT_SERVER, CONVERT_TIMEOUT, DOC_CACHE_DIR, DOC_TTL_HOURS,
)
from correction.records import is_document_referenced
from correction.utils.log import log_info, log_warning

# 结果页的文档 id：就是文件名，只认 32 位十六进制，谁也别想用 ../.. 爬到别的目录
_DOC_ID = re.compile(r"[0-9a-f]{32}")

# 一趟纠错开一个这种前缀的临时目录，请求结束（成功失败都一样）整个删掉
_WORK_PREFIX = "tc-work-"

# 这种临时目录最多留这么久。正常情况请求一结束就删了，这里是给「当场删不掉」留的退路：
# 解析库碰到坏文件时会漏一个文件句柄，得等这一趟请求彻底结束、异常对象被放掉才松手。
# 期限给得宽裕，免得把正在跑的那趟请求的目录删掉。
_WORK_KEEP_SECONDS = 6 * 3600

# 能转 PDF 的后缀。这些是 LibreOffice 认得的办公格式；docx/doc 是最常用的两个。
OFFICE_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".rtf", ".odt", ".wps",
    ".xls", ".xlsx", ".xlsm", ".ods", ".et",
    ".ppt", ".pptx", ".odp", ".dps",
    ".txt", ".csv",
}


def is_office_file(filename: str) -> bool:
    """按后缀判断是不是要过转换服务的文件。"""
    _, _, ext = filename.rpartition(".")
    return bool(ext) and f".{ext.lower()}" in OFFICE_EXTENSIONS


async def convert_to_pdf(filename: str, data: bytes) -> bytes | None:
    """把一份 Office 文件转成 PDF。

    转成功返回 PDF 字节；没配转换服务、或转换失败返回 None（调用方自己决定退路）。
    转换失败只记一条日志不抛异常：解析都要走同一条路，为了一份文档转不出来就让整个
    纠错请求 500 不值得——退回纯文本渲染照样能纠错。
    """
    if not CONVERT_SERVER:
        return None
    url = f"{CONVERT_SERVER}/forms/libreoffice/convert"
    try:
        async with httpx.AsyncClient(timeout=CONVERT_TIMEOUT) as client:
            response = await client.post(url, files={"files": (filename, data)})
        response.raise_for_status()
    except Exception as exc:
        log_warning("convert.failed", filename=filename, exc=exc)
        return None
    if not response.content:
        log_warning("convert.empty", filename=filename)
        return None
    return response.content


def _warmup_docx() -> bytes:
    """最小的一个 docx（一页一句话）：只为了让转换服务把 LibreOffice 点起来。"""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>warmup</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


async def warm_convert_server() -> None:
    """启动时先让转换服务把 LibreOffice 点起来。

    LibreOffice 不是常驻的：闲一会儿再来的第一份文档要现点（实测 0.45 秒 vs 之后 0.2 秒），
    这笔开销本来会落在第一份 Word 文档的「解析」耗时里。转换服务不可用只记日志，不影响启动。
    """
    if not CONVERT_SERVER:
        return
    url = f"{CONVERT_SERVER}/forms/libreoffice/convert"
    try:
        async with httpx.AsyncClient(timeout=CONVERT_TIMEOUT) as client:
            await client.post(url, files={"files": ("warmup.docx", _warmup_docx())})
    except Exception as exc:
        log_warning("convert.warm_failed", exc=exc)


# ── 结果页要展示的 PDF：落盘 / 取回 ──

def purge_documents() -> None:
    """删掉超过 TTL 的文档。

    不做定时任务：每次存新文档时顺手清一次就够——文档只在纠错请求里产生，清理频率跟
    写入频率天然一致。

    纠错记录会长期引用原文，所以还被记录指着的那份一律不删（记录删掉之后照样按 TTL 清）。

    配了对象存储的话，本机根本不留长期那份（解析用的临时文件在系统临时目录里、请求结束就
    删），这里清的是升级前遗留下来的老文件，纯按 TTL 清就行（不用再问记录引用没引用）。
    """
    if DOC_TTL_HOURS <= 0:
        return
    deadline = time.time() - DOC_TTL_HOURS * 3600
    try:
        names = os.listdir(DOC_CACHE_DIR)
    except OSError:
        return
    for name in names:
        if not name.endswith(".pdf"):
            continue
        path = os.path.join(DOC_CACHE_DIR, name)
        try:
            if os.path.getmtime(path) >= deadline:
                continue
        except OSError:
            continue
        # 没配对象存储时，本机这份就是结果页唯一的原文：记录还指着就不能删
        # （记录在库里长期保留，原文得跟着留）
        if not objects.available() and is_document_referenced(name[:-4]):
            continue
        try:
            os.remove(path)
        except OSError:
            continue


def _purge_workdirs() -> None:
    """扫一遍系统临时目录，把早就该删掉的临时目录清掉。

    跟文档缓存一个路子：不做定时任务，下次开临时目录时顺手清一次就够。清的是
    「当场没删掉」和「进程被杀」留下的那两种，超过 _WORK_KEEP_SECONDS 才动。
    """
    deadline = time.time() - _WORK_KEEP_SECONDS
    try:
        names = os.listdir(tempfile.gettempdir())
    except OSError:
        return
    for name in names:
        if not name.startswith(_WORK_PREFIX):
            continue
        path = os.path.join(tempfile.gettempdir(), name)
        try:
            if not os.path.isdir(path) or os.path.getmtime(path) >= deadline:
                continue
        except OSError:
            continue
        discard_workdir(path)


def new_workdir() -> str:
    """给一趟纠错开一个临时目录：上传的原件、转出来的 PDF 都放里面。"""
    _purge_workdirs()
    return tempfile.mkdtemp(prefix=_WORK_PREFIX)


def discard_workdir(path: str | None) -> None:
    """删掉一趟纠错留下的临时目录。只认本服务自己开的这种名字，别的一概不碰。"""
    if not path:
        return
    if not os.path.basename(os.path.normpath(path)).startswith(_WORK_PREFIX):
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log_warning("convert.discard_failed", exc=str(exc))


def store_pdf(pdf_bytes: bytes, workdir: str | None = None) -> tuple[str, str]:
    """把结果页要展示的 PDF 存起来，返回 (文档 id, 解析时读的那份的路径)。

    配了对象存储：长期那份只放对象存储，本机只往临时目录里写一份当解析的输入，
    这一趟用完（见 api 里请求结束的清理）就删——本机不留任何「结果页还要读」的东西。
    没配对象存储：本机这份就是结果页唯一的原文，落在 DOC_CACHE_DIR 里按 TTL 清。
    """
    doc_id = uuid4().hex
    if objects.available():
        path = os.path.join(workdir or tempfile.gettempdir(), f"{_WORK_PREFIX}{doc_id}.pdf")
        with open(path, "wb") as handle:
            handle.write(pdf_bytes)
        # 传不上去就让这次纠错直接失败：原文都没存上，返回一个打不开原文的结果页没意义
        objects.put(doc_id, pdf_bytes)
        log_info("convert.uploaded", doc_id=doc_id, bucket=objects.describe(), bytes=len(pdf_bytes))
        return doc_id, path
    os.makedirs(DOC_CACHE_DIR, exist_ok=True)
    purge_documents()
    path = os.path.join(DOC_CACHE_DIR, f"{doc_id}.pdf")
    with open(path, "wb") as handle:
        handle.write(pdf_bytes)
    return doc_id, path


def document_path(doc_id: str) -> str | None:
    """按 id 找本机缓存的那份 PDF；id 不合法或文件已经清了返回 None。"""
    if not _DOC_ID.fullmatch(doc_id or ""):
        return None
    path = os.path.join(DOC_CACHE_DIR, f"{doc_id}.pdf")
    return path if os.path.isfile(path) else None


def remove_document(doc_id: str) -> None:
    """删掉一份原文：对象存储里那份，以及本机缓存里那份（有的话）。"""
    if objects.available():
        try:
            objects.remove(doc_id)
        except Exception as exc:
            log_warning("convert.remove_failed", doc_id=doc_id, where="objects", exc=str(exc))
    path = document_path(doc_id)
    if not path:
        return
    try:
        os.remove(path)
    except OSError as exc:
        log_warning("convert.remove_failed", doc_id=doc_id, where="local", exc=str(exc))
