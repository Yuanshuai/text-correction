import os
import tempfile
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    """加载必需的环境变量，缺失或为空时直接报错，避免静默使用默认值。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"缺少必需的环境变量 {name}，请在启动时通过 -e {name}=xxx 传入"
        )
    return value


def _reject_non_ascii(name: str, value: str) -> None:
    """凭据里出现非 ASCII 字符，说明填的是文档里的占位符。

    这些值最终进 HTTP 请求头，而请求头只允许 ASCII：非 ASCII 会在发请求时抛
    "'ascii' codec can't encode characters in position ..."，每片文本都失败、
    接口却照常返回 200 和空结果。所以在启动时就拦下来。
    """
    bad = [(i, ch) for i, ch in enumerate(value) if ord(ch) > 127]
    if not bad:
        return
    snippet = "".join(ch for _, ch in bad[:8])
    raise RuntimeError(
        f"环境变量 {name} 含有非 ASCII 字符：第 {bad[0][0] + 1} 个字符起是「{snippet}」。"
        f"HTTP 请求头只能是 ASCII，请确认没有把文档里的占位符（如 sk-你的token）当成真实凭据传入。"
    )


def _int_env(name: str, default: int) -> int:
    """读取整数型环境变量；未设置时用默认值，写错时报错而不是静默忽略。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"环境变量 {name} 必须是整数，实际为 {raw!r}") from None


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
OPENAI_LOG_LEVEL = "WARNING"
LOG_TEXT_PREVIEW = 120

# 模型网关统一按 OpenAI 兼容接口接：base_url + 模型名 + Bearer token 就够了
OPEN_AI_MODEL = _require_env("OPEN_AI_MODEL")
OPEN_BASE_URL = _require_env("OPEN_BASE_URL")
OPEN_AI_TOKEN = _require_env("OPEN_AI_TOKEN")

# 凭据会直接进 HTTP 头，非 ASCII 一律在启动时报错（多见于把文档占位符当真实值传入）
for _env_name, _env_value in (
    ("OPEN_AI_TOKEN", OPEN_AI_TOKEN),
    ("PARSE2JSON_TOKEN", os.environ.get("PARSE2JSON_TOKEN") or ""),
):
    if _env_value:
        _reject_non_ascii(_env_name, _env_value)

LLM_DISABLE_THINKING = True

# 一份 000.txt（交给模型读的那个文件）最多多少字：更长的文本在句末切分成多份，每份各跑
# 一遍阅读工具、再按偏移合回原文；不按字数拒绝请求（份数多则耗时长）。
MAX_TEXT_LENGTH = _int_env("MAX_TEXT_LENGTH", 3000)
MAX_CONCURRENCY = _int_env("MAX_CONCURRENCY", 10)

# PDF 解析开几个进程：耗时的活是表格识别（逐页扫矢量线条），页与页互不相干，
# 按页并行能把 75 页合同的解析从 2.0 秒压到 0.5 秒。1 表示不并行。不填时按核数取、
# 最多 8（容器里 gunicorn 每个 worker 各有一份进程池，别开太大把核占满）。
_pdf_parse_workers = _int_env("PDF_PARSE_WORKERS", 0)
PDF_PARSE_WORKERS = _pdf_parse_workers if _pdf_parse_workers > 0 else min(8, os.cpu_count() or 4)

# ── 外部文档解析（DocParser：POST /async/parse2json，异步提交 + 轮询查结果）──

# 配了地址就默认启用，而且是**唯一**那条解析路：原文交给外部那套版面模型 + OCR 解析
# （扫描件、没有框线的表格比本地 PyMuPDF 强）。出岔子——连不上、服务报错、等超时——直接报错，
# 不退回本地解析：两条路给的结果不一样，悄悄换一条，用户看到的纠错效果就跟着变，出了问题
# 还查不出原因。想跑本地那套就把地址清掉、或者传 PARSE2JSON_ENABLED=0。
PARSE2JSON_BASE_URL = (os.environ.get("PARSE2JSON_BASE_URL") or "").strip().rstrip("/")
PARSE2JSON_TOKEN = (os.environ.get("PARSE2JSON_TOKEN") or "").strip()
PARSE2JSON_ENABLED = (
    os.environ.get("PARSE2JSON_ENABLED") or ("1" if PARSE2JSON_BASE_URL else "0")
).strip().lower() in ("1", "true", "yes", "on")

# 解析服务是自己按 URL 去下载原文的，我们本机这份文件它拿不到，所以得给它一个够得着的地址：
# 填一个带 {doc_id} 的模板，比如 http://10.0.0.10:18081/api/document/{doc_id}
# （前端那个端口，/api 由 nginx 转到后端）。配了 S3_PUBLIC_ENDPOINT 时优先用对象存储的
# 签名地址，这个模板可以不填；两样都没有就等于外部解析没配全，纠错会直接报错。
PARSE2JSON_FILE_URL = (os.environ.get("PARSE2JSON_FILE_URL") or "").strip()
if PARSE2JSON_FILE_URL and "{doc_id}" not in PARSE2JSON_FILE_URL:
    raise RuntimeError(
        "PARSE2JSON_FILE_URL 里要用 {doc_id} 占位，例如 "
        "http://10.0.0.10:18081/api/document/{doc_id}"
    )

PARSE2JSON_TIMEOUT = _int_env("PARSE2JSON_TIMEOUT", 60)             # 单次提交/轮询请求的超时秒数
# 从提交到拿到结果最多等多少**分钟**：大文档动辄几十分钟（服务端排队 + 版面识别 + OCR），
# 默认给足一小时。等超了这次纠错就报错，不把请求无限挂着。
# 老名字 PARSE2JSON_MAX_WAIT（单位是秒）还认，按分钟向上取整折算，免得已经在跑的部署改了名就不生效。
PARSE2JSON_MAX_WAIT_MIN = _int_env("PARSE2JSON_MAX_WAIT_MIN", 0)
if not PARSE2JSON_MAX_WAIT_MIN:
    _legacy_max_wait = _int_env("PARSE2JSON_MAX_WAIT", 0)
    PARSE2JSON_MAX_WAIT_MIN = -(-_legacy_max_wait // 60) if _legacy_max_wait > 0 else 60
PARSE2JSON_MAX_WAIT_MIN = max(1, PARSE2JSON_MAX_WAIT_MIN)
PARSE2JSON_MAX_WAIT = PARSE2JSON_MAX_WAIT_MIN * 60                  # 内部一律按秒算
PARSE2JSON_POLL_INTERVAL = _int_env("PARSE2JSON_POLL_INTERVAL", 1)  # 多久查一次结果
PARSE2JSON_MAX_PAGES = _int_env("PARSE2JSON_MAX_PAGES", 0)          # 0 = 用服务端默认（1000 页）
PARSE2JSON_OCR = (os.environ.get("PARSE2JSON_OCR") or "").strip().lower() in ("1", "true", "yes", "on")

# ── 结果页：Office 转 PDF + PDF 托管 ──

# Word/Excel 转 PDF 的外部服务（Gotenberg 那套 API：POST /forms/libreoffice/convert）。
# 结果页要「跟直接打开原文件一个样」，只有 PDF 做得到，所以 Office 文件先转 PDF 再走
# 同一条链路。不配这个地址就跳过转换，Word 退回原来的纯文本渲染。
CONVERT_SERVER = os.environ.get("CONVERT_SERVER", "").strip().rstrip("/")
CONVERT_TIMEOUT = _int_env("CONVERT_TIMEOUT", 120)

# 结果页要用的 PDF 落在这里，浏览器按 id 取走。只有没配对象存储时才用得上：
# 配了 S3_* 之后长期那份在对象存储里，本机只在临时目录放一份解析用的、请求结束就删。
# 所以也不用定时任务：每次写入时顺手清掉超过 TTL 小时的老文件。
DOC_CACHE_DIR = os.environ.get("DOC_CACHE_DIR", "").strip() or os.path.join(
    tempfile.gettempdir(), "correction-docs"
)
DOC_TTL_HOURS = _int_env("DOC_TTL_HOURS", 24)
LLM_TIMEOUT = _int_env("LLM_TIMEOUT", 60)
LLM_TLS_VERIFY = os.environ.get("LLM_TLS_VERIFY", "false").lower() in ("1", "true", "yes")

# ── 纠错记录的存储（MySQL）──

# 记录进 MySQL，原文 PDF 仍留在磁盘上（库里只存文档 id）。两种写法都行：
#   MYSQL_DSN=mysql://用户:密码@主机:3306/库名?charset=utf8mb4
# 或者分开传 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE。
# 不配就是「记录只存浏览器本地」的老做法：记录接口回 503，前端自动退回本地存储，
# 纠错本身一点不受影响。
def _mysql_dsn() -> str:
    raw = (os.environ.get("MYSQL_DSN") or "").strip()
    if raw:
        return raw
    host = (os.environ.get("MYSQL_HOST") or "").strip()
    if not host:
        return ""
    user = (os.environ.get("MYSQL_USER") or "").strip()
    password = os.environ.get("MYSQL_PASSWORD") or ""
    database = (os.environ.get("MYSQL_DATABASE") or "").strip()
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    path = f"/{database}" if database else ""
    return f"mysql://{credentials}{host}:{_int_env('MYSQL_PORT', 3306)}{path}"


MYSQL_DSN = _mysql_dsn()
# 表名：跟别的服务共用一个库时改一下就能错开
MYSQL_RECORD_TABLE = (os.environ.get("MYSQL_RECORD_TABLE") or "").strip() or "correction_record"
# 每个 worker 在池子里留几条连接：记录都是小读写，两条够用
MYSQL_POOL_SIZE = _int_env("MYSQL_POOL_SIZE", 2)
MYSQL_CONNECT_TIMEOUT = _int_env("MYSQL_CONNECT_TIMEOUT", 5)
MYSQL_TIMEOUT = _int_env("MYSQL_TIMEOUT", 10)

# ── 原文文件的对象存储（MinIO / S3 兼容）──

# 结果页要展示的那份 PDF 存这里，记录里只留对象 key（S3_PREFIX + 文档 id）。不配就还是存
# 本机磁盘：单副本 + 短保留期能用，一扩副本或者容器重建就会丢文件——库里的记录还在、
# 链接点开却没有原文。
#
# S3_ENDPOINT 是后端去连的地址（http://主机:端口，也可以只写主机、端口用 S3_PORT 传）；
# S3_PUBLIC_ENDPOINT 是「浏览器也能访问到」的地址：配了就让浏览器直连对象存储
# （/document/<id> 回一个带签名的临时地址，后端一个字节都不用搬），不配就由后端转发。
S3_ENDPOINT = (os.environ.get("S3_ENDPOINT") or "").strip()
S3_PORT = _int_env("S3_PORT", 0)
# 空 = 按地址里的协议判断（https 就是加密）
S3_USE_SSL = (os.environ.get("S3_USE_SSL") or "").strip()
S3_ACCESS_KEY = (os.environ.get("S3_ACCESS_KEY") or "").strip()
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY") or ""
S3_BUCKET = (os.environ.get("S3_BUCKET") or "").strip()
# 对象名前缀：跟别的服务共用一个桶时把目录错开
S3_PREFIX = (os.environ.get("S3_PREFIX") or "correction/").strip()
S3_PUBLIC_ENDPOINT = (os.environ.get("S3_PUBLIC_ENDPOINT") or "").strip()
# 直连模式下签名地址的有效期（秒）：够打开一份文档就行
S3_PRESIGN_SECONDS = _int_env("S3_PRESIGN_SECONDS", 3600)
S3_TIMEOUT = _int_env("S3_TIMEOUT", 30)

# ── 阅读工具纠错参数 ──

# 一份 000.txt 最多来回几轮：读一段、改一处、再读下一段都算一轮。0 表示不设上限——
# 循环本来就有两个正常出口（模型调 finish 且覆盖率 100%、连着两轮既不读也不改），
# 轮数是按片算的，片长由 MAX_TEXT_LENGTH 卡住，和整份文档多长无关。正数时作为熔断，
# 防模型反复写不合规改动块时无限调 LLM。
TOOL_MAX_TURNS = _int_env("TOOL_MAX_TURNS", 0)

ENABLE_WORD_CORRECTION = os.environ.get("ENABLE_WORD_CORRECTION", "false").lower() in ("1", "true", "yes")
ENABLE_TEST_MODE = os.environ.get("ENABLE_TEST_MODE", "false").lower() in ("1", "true", "yes")

# ── 对外接口（/openapi/*）──

# 对外接口回的「结果页链接」挂在哪个地址上，例如 http://10.0.0.10:18081。
# 留空就按这次请求的地址推：外部系统调的是前端那个端口，nginx 把 Host 原样透传过来，
# 推出来的就是用户能打开的那个地址；要是直接打后端端口，推出来的会是后端自己的地址，
# 那种部署就把这里钉死。
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")

BASE_URL = OPEN_BASE_URL
MODEL_NAME = OPEN_AI_MODEL
API_KEY = OPEN_AI_TOKEN


if BASE_URL.endswith("/"):
    BASE_URL = BASE_URL.rstrip("/")
