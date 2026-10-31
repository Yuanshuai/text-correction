# 文本纠错服务 API 文档

> 本文档描述 `correction/api.py` 当前对外接口，与 `docs/openapi.yaml` 配套使用，可直接导入 Apifox。
>
> **Apifox 导入路径**：项目设置 → 导入数据 → OpenAPI/Swagger → 选择 `docs/openapi.yaml`

---

## 概述

| 项目 | 说明 |
|------|------|
| 服务名称 | Text Correction MVP |
| 版本 | 0.5.0 |
| 后端端口 | `32546`（只在 compose 网络里，不对外发布） |
| 对外端口 | 整套部署只有前端一个口：`FRONTEND_PORT`，生产为 `18083` |
| 协议 | HTTP / JSON，文件上传使用 `multipart/form-data` |
| 兼容格式 | 同时返回 `results`（NestJS 约定）与 `code/data/messages`（旧版 Flask 约定） |

### 部署地址

| 环境 | 外部可访问地址 |
|------|----------------|
| 生产 | `http://203.0.113.10:18083` |
| 本地 | `http://127.0.0.1:18080` |

前端 nginx 把 `/api/*` 转给后端（剥掉 `/api` 前缀）、`/openapi/*` 原样转过去。所以浏览器和外部系统都只认
上面这一个地址，后端端口不用对外。

**外部系统只需要第 6 节那两个接口**（`POST /openapi/contract-audit/web-correct` 和 `-DocParser`）；其余都是本站前端在用的，
列在下面只为内部联调方便。

### 能力范围

- 中文错别字、同音字、近音字（平翘舌不分、前后鼻音不分）检测与纠正
- 改动一律是字级的：一次只改一个字符，返回的 `position` / `original_char` / `correct_char` 都是单字
- 每处改动都要能机械归到一类错别字（同音字 / 近音字 / 字母数字 / 同字连写 / 多打的字 / 漏写的字）才落地，润色类改动一律不落地
- 合同/表单文本的空格保留：只去除首尾空白，中间空格不修改、不臆造填写
- 返回字符级坐标 `position` / `wrong_ids` / `errors`，供前端高亮
- PDF 文档批量提取与纠错
- DOCX 合同模板字段审查

### 不支持

- 润色、改写、风格调整
- 自动填写合同填空位的具体数值/文字

---

## 接口总览

| 方法 | 路径 | 用途 | 谁在用 |
|------|------|------|--------|
| POST | `/openapi/contract-audit/web-correct` | **对外接口**：文本 / 文件 / 文件链接纠错，返回结果页链接 | 外部系统 |
| POST | `/openapi/contract-audit/web-correct-DocParser` | **对外接口（DocParser）**：只收 `fileUrl`，配了解析服务时链接原样转交 | 外部系统 / 本站前端 |
| GET | `/health` | 轻量存活检查（不连模型、不消耗 token） | 容器健康检查 / 运维 |
| GET | `/health/correction` | 纠错链路深度自检（真跑一次纠错，花 token） | 运维手动 |
| GET | `/config` | 前端能力开关：配没配外部解析服务 | 本站前端 |
| POST | `/correction_text` | 单条 / 多行文本纠错 | 本站 |
| POST | `/correction` | PDF / Word / Excel 文件批量纠错 | 本站 |
| POST | `/review_contract` | DOCX 合同字段审查 | 本站 |
| GET | `/document/{doc_id}` | 取结果页要展示的原文 PDF | 本站结果页 |
| GET | `/records` | 纠错记录列表 | 本站记录页 |
| POST | `/records` | 存一条纠错记录 | 本站结果页 |
| GET | `/records/{record_id}` | 取一条记录的整份内容 | 本站结果页 |
| PUT | `/records/{record_id}/ignored` | 标记 / 恢复误报 | 本站结果页 |
| DELETE | `/records/{record_id}` | 删一条记录 | 本站记录页 |

---

## 环境配置

| 环境 | Base URL |
|------|----------|
| 本地 | `http://127.0.0.1:32546` |
| Docker | `http://{host}:32546` |

### 公共请求头

| Header | 必填 | 说明 |
|--------|------|------|
| `Content-Type` | 是 | JSON 接口为 `application/json`；文件接口为 `multipart/form-data` |
| `X-Request-ID` | 否 | 链路追踪 ID，响应头回传 |

### 公共响应头

| Header | 说明 |
|--------|------|
| `X-Request-ID` | 与请求对应，便于日志排查 |

### 启动参数（环境变量）

服务通过环境变量或 `.env` 文件加载配置，Docker 部署时使用 `-e` 传入。必需变量缺失会直接报错，不会静默使用默认值。

模型网关统一走 OpenAI 兼容接口 + Bearer token：

| 变量 | 必填 | 说明 |
|------|------|------|
| `OPEN_AI_MODEL` | 是 | 模型名，如 `qwen3.5-27b` |
| `OPEN_BASE_URL` | 是 | 网关地址，如 `https://llm.example.com/v1` |
| `OPEN_AI_TOKEN` | 是 | Bearer token |

---

#### 公共运行参数

| 变量 | 默认 | 是否生效 | 说明 |
|------|------|----------|------|
| `LOG_LEVEL` | `INFO` | 生效 | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `MAX_TEXT_LENGTH` | `3000` | 生效 | 一份 `000.txt`（交给模型读的那个文件）最多多少字；更长的文本在句末切分后分片并发纠错，不拒绝请求 |
| `MAX_CONCURRENCY` | `10` | 生效 | 并发处理 LLM 任务的最大数量 |
| `LLM_TIMEOUT` | `60` | 生效 | 单次 LLM 请求超时秒数 |
| `LLM_TLS_VERIFY` | `false` | 生效 | 是否校验 LLM 网关 HTTPS 证书；默认关闭，需要校验时设为 `true` |
| `ENABLE_WORD_CORRECTION` | `false` | 生效 | `true` 时 `/correction` 支持 `.docx` / `.doc` |
| `ENABLE_TEST_MODE` | `false` | 生效 | `true` 时 `/correction_text` 返回内置假纠错结果，不调用 LLM |
| `TOOL_MAX_TURNS` | `20` | 生效 | 一份 `000.txt` 最多来回几轮：读一段、改一处、再读下一段都算一轮；模型调用 `finish`、或连着两轮既不读也不改就提前结束。漏读的区间、以及引文没抄准/不唯一这类"块本身没写对"的打回没清时，`finish` 会先被打回一次 |
| `PUBLIC_BASE_URL` | 空 | 生效 | 对外接口回的「结果页链接」挂在哪个地址上，例如 `http://10.0.0.10:18081`；留空按这次请求的地址推 |

---

#### 启动示例

**本地启动**

```bash
cp .env.example .env
# 编辑 .env，填入 OPEN_AI_MODEL / OPEN_BASE_URL 及对应模式鉴权参数
python index.py
```

**Docker 启动**

```bash
docker run -d --name text-correction -p 32546:32546 -e OPEN_AI_MODEL=qwen3.5-27b -e OPEN_BASE_URL=https://llm.example.com/v1 -e OPEN_AI_TOKEN=sk-your-token ghcr.io/yuanshuai/text-correction:latest
```

排除，Docker 部署时请使用 `-e` 显式传入环境变量。

---

## 通用响应说明

除 `/health`、`/health/correction` 外，成功响应的业务码均为 `code=0`、`messages="任务完成"`，并同时返回：

| 字段 | 类型 | 说明 |
|------|------|------|
| `results` | object / array | 顶层结果；文本接口为对象，批量接口为数组 |
| `code` | int | `0` 表示成功 |
| `data.results` | object / array | `results` 的双写副本，兼容旧客户端 |
| `messages` | string | 提示信息 |
| `engine_status` | string | `ok` 或 `degraded` |

错误响应使用 FastAPI 默认结构：`{"detail": "错误说明"}`。

---

## 1. 健康检查

```
GET /health
```

轻量存活检查：只回答「进程还在、配置齐了」，不连模型、不消耗 token。容器健康检查每 30 秒打一次这里，所以必须是纯本地判断。

**成功响应 `200`**

```json
{
  "status": "ok",
  "model": "qwen3.5-27b",
  "base_url": "https://llm.example.com/v1",
  "max_text_length": 3000,
  "uptime_seconds": 3821
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 固定 `ok`；进程起不来时请求本身就失败 |
| `model` | string | 当前模型 |
| `base_url` | string | LLM 网关地址 |
| `max_text_length` | int | 一份 `000.txt` 的字数上限（不是请求拒绝阈值） |
| `uptime_seconds` | int | 进程已运行秒数 |

> 模型通不通、会不会调工具，看下一节的 `/health/correction`：那条会真跑一次纠错，会花 token，只在需要时手动调。

### 能力开关

```
GET /config
```

前端一进页面就问它一句：配没配外部解析服务（DocParser）。配了就把「文本纠错」「文档纠错」
这两条手工入口收起来，页面只留「纠错记录」和「链接纠错」——那种部署下纠错主要是外部系统走
API 调进来的，再摆手工入口只会让人绕着走。没配、或者拿不到（后端老版本、网络不通）就照旧三个页签。

**成功响应 `200`**

```json
{ "parse2json": true }
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `parse2json` | bool | `PARSE2JSON_BASE_URL` 有值、也没被 `PARSE2JSON_ENABLED=0` 关掉时为 `true` |

---

## 2. 纠错链路深度自检

```
GET /health/correction
```

手动触发的深度自检：用内置约 50 字文本跑一次完整纠错链路，会消耗 token，建议只在部署后或排查时调。只要接口正常返回，`status` 固定为 `ok`；模型是否纠出预期错字不作为健康判定条件，因为 LLM 输出具有不稳定性。

**成功响应 `200`**

```json
{
  "status": "ok",
  "elapsed_ms": 12427,
  "test_text": "甲方应按合同约定向乙方支付货款，逾期未支付的，应承但违约责任，并赔偿乙方因此产生的全部损失。",
  "error_count": 1,
  "matched_count": 1,
  "expected_count": 1,
  "checks": [
    {
      "original": "承但",
      "expected": "承担",
      "matched": true
    }
  ],
  "errors": [
    {
      "position": 25,
      "original_char": "但",
      "correct_char": "担",
      "tag": "replace"
    }
  ],
  "correct_text": "甲方应按合同约定向乙方支付货款，逾期未支付的，应承担违约责任，并赔偿乙方因此产生的全部损失。"
}
```

**异常响应 `200`**

```json
{
  "status": "error",
  "elapsed_ms": 1520,
  "error": "任务因未知原因失败"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `ok` 正常返回；`error` 内部异常 |
| `elapsed_ms` | int | 完整纠错链路耗时 |
| `test_text` | string | 内置测试文本 |
| `error_count` | int | 本次检出的错误数 |
| `matched_count` | int | 命中预期纠错的数量，仅观测 |
| `expected_count` | int | 内置预期纠错数量 |
| `checks` | array | 预期纠错匹配明细，仅观测 |
| `errors` | array | 错误明细，结构与 `CorrectionError` 一致 |
| `correct_text` | string | 纠错后文本 |

---

## 3. 单条文本纠错

```
POST /correction_text
Content-Type: application/json
```

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 待纠错文本；长度不限，超过 `MAX_TEXT_LENGTH` 会在句末切分后分片并发纠错 |

**请求示例**

```json
{
  "content": "甲方应按合同约定向乙方支付货款，逾期未支付的，应承但违约责任。"
}
```

**成功响应 `200`**

```json
{
  "results": {
    "original_text": "甲方应按合同约定向乙方支付货款，逾期未支付的，应承但违约责任。",
    "correct_text": "甲方应按合同约定向乙方支付货款，逾期未支付的，应承担违约责任。",
    "wrong_ids": [25],
    "errors": [
      {
        "position": 25,
        "original_char": "但",
        "correct_char": "担",
        "tag": "replace"
      }
    ]
  },
  "code": 0,
  "data": {
    "results": {
      "original_text": "甲方应按合同约定向乙方支付货款，逾期未支付的，应承但违约责任。",
      "correct_text": "甲方应按合同约定向乙方支付货款，逾期未支付的，应承担违约责任。",
      "wrong_ids": [25],
      "errors": [
        {
          "position": 25,
          "original_char": "但",
          "correct_char": "担",
          "tag": "replace"
        }
      ]
    }
  },
  "messages": "任务完成",
  "engine_status": "ok"
}
```

**无错响应 `200`**

```json
{
  "results": {
    "original_text": "该项目已通过验收，各项指标符合设计要求。",
    "correct_text": "该项目已通过验收，各项指标符合设计要求。",
    "wrong_ids": [],
    "errors": []
  },
  "code": 0,
  "data": {
    "results": {
      "original_text": "该项目已通过验收，各项指标符合设计要求。",
      "correct_text": "该项目已通过验收，各项指标符合设计要求。",
      "wrong_ids": [],
      "errors": []
    }
  },
  "messages": "任务完成",
  "engine_status": "ok"
}
```

**其他说明**

- 若 `content` 中出现字面量 `\\n` 且没有真实换行，接口会先将其转换为真实换行。
- 整段文本就是模型眼里的 `000.txt`：模型用 `read_text` / `find_text` 自己读，改动写成 apply_patch 形态的小补丁（```@` 引文定位 + 改前 + 改后），位置由服务端在原文里搜出来。
- 单行超过 `MAX_TEXT_LENGTH` 时按句末切成多片（切不动就按上限硬切），分片并发纠错后按下标偏移合回整段，不拒绝请求；返回的 `position` 始终相对整段 `original_text`。
- `original_text` 仅去除首尾空白，中间空格保持原样。

---

## 4. 文件批量纠错

```
POST /correction
Content-Type: multipart/form-data
```

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 是 | 待纠错的文件，支持的类型见下表 |
| `limit` | query int | 否 | 限制处理段落数，`0` 或不传表示不限制 |
| `debug` | query bool | 否 | `true` 时返回纠错调试数据（每一轮的调用数与落地数、被引文校验或错误类型门拦下的改动、覆盖率与未读区间） |

**支持的文件类型**

判定**只看文件后缀**（不嗅探内容）。Office 那些会先交给转换服务（Gotenberg / LibreOffice）转成 PDF，
再跟 PDF 走完全同一条解析、纠错、展示链路，所以结果页看到的版式跟直接打开原文件一样。

| 类型 | 后缀 | 走哪条路 |
|------|------|----------|
| PDF | `.pdf` | 原生解析，带每个字的坐标，改动能框在原字上 |
| Word | `.doc` `.docx` `.docm` `.rtf` `.odt` `.wps` | 先转 PDF |
| Excel | `.xls` `.xlsx` `.xlsm` `.ods` `.et` | 先转 PDF |
| PowerPoint | `.ppt` `.pptx` `.odp` `.dps` | 先转 PDF |
| 纯文本 | `.txt` `.csv` | 也走转换，转成 PDF 后再纠 |

文件大小上限 **100 MB**（前端 nginx 的 `client_max_body_size`）。

三种不成功的情形，调用方要注意区分：

| 情形 | 表现 |
|------|------|
| 图片（`.png` / `.jpg` 等）、扫描件 PDF（没有文字层） | **HTTP 200，但 `results` 为空**——解析不出任何文字，没有东西可纠。跟「文档没错字」在响应里长得一样，判断依据只有 `results` 为空 |
| 后缀不在上表里、又不是能打开的文件 | HTTP `500`，`{"detail": "文档纠错任务失败"}` |
| 转换服务不可用，且不是 `.docx` / `.doc` | HTTP `400`，`{"detail": "文档转换服务不可用：这种格式需要先转成 PDF 才能纠错，请上传 PDF 或联系管理员检查转换服务"}` |
| 配了外部解析服务，而它连不上 / 报错 / 等超时 | HTTP `500`，`{"detail": "外部解析服务解析失败：..."}`——**不会退回本地解析** |

> 转换服务不可用、但文件是 `.docx` / `.doc`、且设了 `ENABLE_WORD_CORRECTION=true` 时，会退回「直接解析
> Word 文字」那条老路：没有坐标，结果页只能把文字重排出来，版式跟原文件不一样。
>
> **配了外部解析服务（`PARSE2JSON_BASE_URL`）就没有这些退路**：那套是唯一的解析器，连不上、服务报错、
> 等超时一律回 `500`；Word/Excel 也必须先转成 PDF（它只收 PDF），转换服务不可用同样回 `500`。
> 想跑本地那套，把 `PARSE2JSON_BASE_URL` 清掉或传 `PARSE2JSON_ENABLED=0`——两条路各跑各的，不混着来。

**成功响应 `200`**

`data.results` / `results` 为数组，只返回有修改的段落或表格单元格。

```json
{
  "results": [
    {
      "original_text": "4.2资料的提供和消除",
      "correct_text": "4.2资料的提供和使用",
      "wrong_ids": [9, 10],
      "errors": [
        {
          "position": 9,
          "original_char": "消",
          "correct_char": "使",
          "tag": "replace"
        },
        {
          "position": 10,
          "original_char": "除",
          "correct_char": "用",
          "tag": "replace"
        }
      ]
    }
  ],
  "code": 0,
  "data": {
    "results": [
      {
        "original_text": "4.2资料的提供和消除",
        "correct_text": "4.2资料的提供和使用",
        "wrong_ids": [9, 10],
        "errors": [
          {
            "position": 9,
            "original_char": "消",
            "correct_char": "使",
            "tag": "replace"
          },
          {
            "position": 10,
            "original_char": "除",
            "correct_char": "用",
            "tag": "replace"
          }
        ]
      }
    ]
  },
  "messages": "任务完成",
  "engine_status": "ok"
}
```

**失败响应**

- 文档解析失败或未知异常：HTTP `500`，响应体 `{"detail": "文档纠错任务失败"}`。
- `debug=true` 时，响应增加 `debug` 字段：每一片一条记录，含 `unit_offset`（该片在拼接文本里的起始偏移）、`chars` / `covered` / `unread`（字数、已读字数、未读区间）、`finished`（是否走到 `finish`）与 `turns`（每一轮的调用数 / 落地数 / 回灌的拦下原因）。同一份记录会写入 `debug_logs/tool_debug_current.jsonl`。

---

## 5. 合同字段审查

```
POST /review_contract
Content-Type: multipart/form-data
```

对 DOCX/DOC 合同模板中的占位符字段逐项审查。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 是 | DOCX 或 DOC 合同模板文件 |

**成功响应 `200`**

```json
{
  "results": [
    {
      "approved": true,
      "reason": "未发现文字错误",
      "severity": "info",
      "field_index": 0,
      "field_name": "项目名称",
      "field_type": "text",
      "paragraph_index": 149
    }
  ],
  "code": 0,
  "data": {
    "results": [
      {
        "approved": true,
        "reason": "未发现文字错误",
        "severity": "info",
        "field_index": 0,
        "field_name": "项目名称",
        "field_type": "text",
        "paragraph_index": 149
      }
    ]
  },
  "messages": "任务完成",
  "engine_status": "ok"
}
```

**响应字段**

| 字段 | 类型 | 说明 |
|------|------|------|
| `approved` | bool | `true` 通过；`false` 有风险需关注 |
| `reason` | string | 审查理由 |
| `severity` | string | `info` / `warning` / `critical` |
| `field_index` | int | 字段序号，从 0 开始 |
| `field_name` | string | 占位符字段名 |
| `field_type` | string | `text` / `select` / `number` / `percent` / `date` / `amount` |
| `paragraph_index` | int | 所在段落下标；非段落字段为 `-1` |
| `table_location` | string / null | 表格位置，例如 `表格1行2列3`；仅表格字段返回 |

**失败响应**

- 文件解析失败或未知异常：HTTP `500`，响应体 `{"detail": "合同审查任务失败"}`。

---

## 6. 对外接口：文本 / 文件纠错（返回结果页链接）

```
POST /openapi/contract-audit/web-correct[?full=true]          ← 原有接口，行为保持不变
POST /openapi/contract-audit/web-correct-DocParser[?full=true]       ← 配了解析服务时用这个
Content-Type: multipart/form-data        （或 application/x-www-form-urlencoded）
```

给外部系统（合同审查那边的网页）用的单入口：文本走 `content`、文件走 `file`、文件链接走 `fileUrl`，三个都给时以 `file` 为先、其次是 `fileUrl`。

两个路径的返回完全一样；原接口收 `content` / `file` / `fileUrl` 三选一，**DocParser 接口只收 `fileUrl`**。区别是走不走外部解析服务：原接口一律走本地那套（链接也由我们下载）；DocParser 接口在配了外部解析服务时把链接原样转交（见下面「`fileUrl` 说明」）。

> `content` / `file` / `fileUrl` 都是**表单字段**。发 JSON 请求体、或者把 `content` 拼在 URL 查询串里都不作数，
> 会回 `400 {"detail": "content（文本）、file（文件）和 fileUrl（文件链接）至少要给一个"}`。
> 两种表单都能用：`multipart/form-data` 传文本、文件、链接都行；`application/x-www-form-urlencoded`
> 只能传 `content`（文本）和 `fileUrl`，要传文件必须用 `multipart/form-data`。
> 用 Apifox 时把 Body 切成 `form-data`（不是 JSON），key 填 `content`，类型选 text。
> DocParser 接口只认 `fileUrl`，没给会回 `400 {"detail": "fileUrl（文件链接）必须给一个"}`。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| `content` | 表单 | string | 三选一 | 待纠错的文本 |
| `file` | 表单 | file | 三选一 | 待纠错的文件（PDF / Word / Excel / PPT / txt）；支持的类型见第 4 节那张表 |
| `fileUrl` | 表单 | string | 三选一 | 待纠错文件的 http/https 链接；原接口由我们下载后走和 `file` 一样的链路，DocParser 接口配了解析服务时原样转交 |
| `full` | 查询串 | boolean | 否 | 文件纠错时额外回全文分段 `blocks` 与页面高亮框 `highlights`；默认 `false` |

**`fileUrl` 说明**：原接口里链接一律由我们自己下载（超时 120 秒、上限 50 MB），下完跟直接上传
`file` 完全一样，Word 等 Office 格式先转 PDF；链接取不回来、指向网页或空内容会回 `400`。
原接口的文本、文件、链接三路都走本地解析，跟有没有配外部解析服务无关。

**DocParser 接口的差别**：`POST /openapi/contract-audit/web-correct-DocParser` **只收 `fileUrl`**（文本纠错、上传文件走原接口），
返回跟原接口一样。配了外部解析服务时链接**原样转交**——我们不下载、不转格式，它自己去取、自己判格式，
报什么错比如「这个链接不是 PDF」就原样回给调用方（HTTP `400` + 它那个 `errorCode`）。没配解析服务时，
DocParser 接口跟原接口一样由我们下载、走本地那套。

**跟 `/correction`、`/correction_text` 的区别**：两路都多回一个 `url`（能直接打开的结果页链接）
和 `record_id`（这条纠错记录的 id），并把结果存进纠错记录——链接可以转发，换台机器也打得开。

**成功响应 `200`（走文本）**

```json
{
  "type": "text",
  "correction_count": 1,
  "record_id": "20261016-064530-969495",
  "url": "http://127.0.0.1:18080/#/result/20261016-064530-969495",
  "results": {
    "original_text": "这是一个纠错温本",
    "correct_text": "这是一个纠错文本",
    "wrong_ids": [6],
    "errors": [
      { "position": 6, "original_char": "温", "correct_char": "文", "tag": "replace" }
    ]
  },
  "code": 0,
  "data": { "results": { "...": "跟顶层 results 同一份" } },
  "messages": "任务完成",
  "engine_status": "ok"
}
```

**成功响应 `200`（走文件）**

带 `?full=true` 时比走文本多这几段：`blocks`（按正文顺序的全文分段）、`highlights`（改动贴在
页面哪儿）、`doc_id` / `pdf_url`（结果页铺出来的那份原文，Word 也先转成 PDF）、`source_kind`、
`timings`（转换 / 解析 / 纠错各花了多久）。不带 `?full=true` 时这几段不回，响应跟 `/correction` 一样只有 `results`。

**响应字段说明**

`type` / `correction_count` / `record_id` / `url` / `code` / `messages` / `engine_status` 这几项两路都有。
`/correction` 的响应少 `type` / `correction_count` / `record_id` / `url` 四项，其余字段完全相同。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 本次走的是哪条：`file`（文件）/ `text`（文本） |
| `correction_count` | int | 本次纠了多少**处**。注意「处」不等于「字」，见本节最后那条 |
| `record_id` | string \| null | 这条纠错记录的 id，用 `GET /records/{record_id}` 取整份；记录库连不上时 `null` |
| `url` | string \| null | 能直接打开的结果页链接；记录库连不上时 `null` |
| `code` | int | `0` 表示成功 |
| `results` | array \| object | `file` 时是数组，逐段 / 逐格的差异（只含有改动的段，没错字的不出现）；`text` 时是单个对象 |
| `data.results` | 同 `results` | `results` 的双写副本，兼容老客户端，内容完全一样 |
| `messages` | string | 提示信息，正常是「任务完成」 |
| `engine_status` | string | `ok` / `degraded`（模型网关抖动、但结果照常返回时为 `degraded`） |
| `blocks` | array | 仅 `?full=true`：按正文顺序的全文分段（段落和表格单元格交错），结果页拿它铺全文 |
| `highlights` | array | 仅 `?full=true`：改动贴在页面哪儿；**处数就是它的长度** |
| `doc_id` | string | 仅 `?full=true`：原文 PDF 的 id（32 位十六进制），配 `/document/{doc_id}` 取 |
| `pdf_url` | string | 仅 `?full=true`：就是 `/document/{doc_id}`，相对地址 |
| `source_kind` | string | 仅 `?full=true`：`pdf`（上传的本来就是 PDF）/ `office`（Office 转出来的 PDF） |
| `timings` | object | 仅 `?full=true`：`convert_ms`（Office 转 PDF）/ `parse_ms`（解析）/ `correct_ms`（纠错）/ `total_ms`（总），单位毫秒 |

`results` 数组每一项：

| 字段 | 类型 | 说明 |
|------|------|------|
| `original_text` | string | 这一段 / 这一格的原文（只去首尾空白，中间空格原样保留） |
| `correct_text` | string | 纠错后的文本 |
| `wrong_ids` | int[] | 有改动的字符下标，去重升序；就是 `errors` 里 `position` 去重排序的结果 |
| `errors` | object[] | 逐字改动明细，见下表 |

`errors` 每一项：

| 字段 | 类型 | 说明 |
|------|------|------|
| `position` | int | 在 `original_text` 里的下标，从 `0` 开始；`insert` 表示插在这个下标的字符**之前** |
| `original_char` | string | 改前的字；`insert` 时是空字符串，`replace` / `delete` 时是原文里真实存在的那几个字 |
| `correct_char` | string | 改成什么；`delete` 时是空字符串 |
| `tag` | string | `replace`（改）/ `insert`（增）/ `delete`（删） |

`blocks` 每一项（`?full=true`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `p` 普通段落 / `row` 表格单元格 |
| `text` | string | 该段 / 该格的原文 |
| `errors` | object[] | 该段 / 该格的改动，形状同上 |
| `wrong_ids` | int[] | 该段 / 该格有改动的字符下标 |
| `table` | int | 只有表格格有：第几张表，从 `0` 起（跨表唯一） |
| `row` `col` | int | 只有表格格有：行、列，从 `0` 起 |
| `colspan` `rowspan` | int | 只有表格格有：合并了几列 / 几行，不合并是 `1`；被竖合并盖住的续格不在这里出现 |

`highlights` 每一项（`?full=true`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `index` | int | 处号，从 `0` 起，跟结果页清单的顺序一一对应 |
| `page` | int | 在第几页，从 `0` 起 |
| `tag` | string | `replace` / `insert` / `delete` |
| `rects` | array | 框在页面上的位置，每个框是 `[x0, y0, x1, y1]`（PDF 页面坐标系，单位 1/72 英寸，原点左上、y 向下）；一个改动跨行时会是多个框 |
| `original` | string | 改动前的内容；`insert` 时是空字符串 |
| `corrected` | string | 改动后的内容；`delete` 时是空字符串 |

> **`correction_count` 数的是「处」，`errors` 列的是「字」，两个数不一样是正常的。**
> 相邻的同类改动会合成一处：连着改几个字算一处、连着删几个字算一处、同一位置补几个字算一处。
> 拿一份真实响应举例：`errors` 有 14 条，其中 `34`（新→星）和 `35`（奇→期）是相邻的两个字，
> 合成一处，所以 `correction_count` 是 **13**，`highlights` 也是 13 个框。
> 要「一共改了几处」用 `correction_count`；要「逐字定位、自己渲染」用 `wrong_ids` + `errors`。

**失败响应**

- `content` / `file` / `fileUrl` 都没给：HTTP `400`，响应体 `{"detail": "content（文本）、file（文件）和 fileUrl（文件链接）至少要给一个"}`
- 文档解析失败或未知异常：HTTP `500`

> `url` / `record_id` 在纠错记录库（MySQL）连不上时回 `null`——纠错本身照常返回，不因为存不上记录就失败。
> 链接地址优先取 `PUBLIC_BASE_URL`；留空按这次请求的地址推（外部系统调的是前端那个端口，
> nginx 把 Host 原样透传过来，推出来的就是用户能打开的那个地址）。

---

## 7. 结果页原文

结果页就是拿这个地址当 PDF 阅读器的数据源：浏览器按需取字节、一页页画出来，改动的高亮框再按坐标贴上去。

```
GET /document/{doc_id}
```

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| `doc_id` | 路径 | string | 32 位十六进制文档 id，从 `/correction?full=true` 或 `GET /records/{id}` 的 `doc.docId` 里拿 |

**响应**

| HTTP | 场景 | 响应 |
|------|------|------|
| 200 | 正常 | PDF 字节流，`Content-Type: application/pdf`，`Content-Disposition: inline` |
| 206 | 带 `Range` 头 | 分段字节流（PDF.js 靠它渐进加载，几十页的文档不用一次下完） |
| 307 | 配了对象存储对外地址 | 重定向到带签名的临时地址，浏览器直连对象存储，后端一个字节都不搬 |
| 404 | 不存在或已过期 | `{"detail": "文档不存在或已过期"}` |

原文按 id 寻址、内容永不改变，所以响应带 `Cache-Control: private, max-age=86400`，浏览器可以放心缓存。

---

## 8. 纠错记录

纠错记录统一存进 MySQL：结果页按 id 取整份，链接可以转发、换台机器也打得开。
记录本身进库，原文 PDF 不进库（库里只存 32 位 `docId`，结果页按它去 `/document/{docId}` 取）。

> **没配 `MYSQL_DSN` 时这一组整体关闭**，一律回 `503 {"detail": "纠错记录存储暂时不可用"}`；
> 前端会自动退回「记录只存浏览器本地」的老做法，纠错功能一点不受影响。

### 8.1 记录列表

```
GET /records?limit=50&offset=0
```

| 参数 | 位置 | 类型 | 默认 | 说明 |
|------|------|------|------|------|
| `limit` | 查询串 | int | 50 | 一页多少条，1~200 |
| `offset` | 查询串 | int | 0 | 从第几条开始 |

按时间倒序，**不带全文**——只回列表页要显示的那几列，结果页再按 id 取整份：

```json
{
  "records": [
    {
      "id": "20261016-093605-8eedbf",
      "kind": "file",
      "filename": "货物零星采购合同（2025年版）.docx",
      "engineStatus": "ok",
      "correctionCount": 12,
      "unitsWithCorrection": 7,
      "createdAt": "2026-10-16T09:36:05Z",
      "timings": { "convertMs": 3800, "parseMs": 668, "correctMs": 41000, "totalMs": 45500 },
      "hasDoc": true
    }
  ],
  "total": 31,
  "storage": "mysql"
}
```

| 字段 | 说明 |
|------|------|
| `correctionCount` | 纠了多少处（跟结果页页头同一个口径） |
| `unitsWithCorrection` | 涉及多少段 / 单元格 |
| `timings` | 转换 / 解析 / 纠错 / 总耗时（毫秒），没有的段不回 |
| `hasDoc` | 这条记录有没有可展示的原文，有就能取 `/document/{docId}` |

### 8.2 存一条记录

```
POST /records
Content-Type: application/json
```

前端纠错完成后调这个，拿到 `id` 再跳结果页 `#/result/<id>`。响应 `{"id": "...", "createdAt": "...", "storage": "mysql"}`。

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 否 | 不传服务端生成（`20261016-120000-ab12cd`）；传了就沿用，把浏览器里的老记录导进来 |
| `kind` | string | 否 | `file`（默认）/ `text` |
| `filename` | string | 否 | 展示用文件名 |
| `engineStatus` | string | 否 | `ok` / `degraded` |
| `correctionCount` | int | 否 | 纠了多少处 |
| `unitsWithCorrection` | int | 否 | 涉及多少段 / 单元格 |
| `timings` | object | 否 | `convertMs` / `parseMs` / `correctMs` / `totalMs` |
| `units` | array | 否 | 逐段、逐格的纠错结果（结果页正文用） |
| `blocks` | array | 否 | 按正文顺序的全文分段 |
| `highlights` | array | 否 | 改动贴在页面哪儿 |
| `doc` | object | 否 | `{docId, sourceKind, pdfUrl}`，`docId` 是 32 位 hex |

同一个 `id` 再存一次是**覆盖**。

### 8.3 取一条记录

```
GET /records/{record_id}
```

响应 `{"record": {...}, "storage": "mysql"}`：在 8.1 那几列基础上多回 `units` / `blocks` / `highlights` /
`ignored` / `doc`。记录不存在回 `404 {"detail": "这条纠错记录不存在"}`。

`doc.pdfUrl` 是**相对地址**（`/document/<docId>`），谁打开就按自己的服务地址去取，不认上传那台机器的地址。

### 8.4 标记误报

```
PUT /records/{record_id}/ignored
Content-Type: application/json

{ "ignored": [3, 7] }
```

按**处号**标记哪些是误报、不用改；传空数组 `[]` 就是全部恢复。响应 `{"ignored": [3, 7]}`，恢复光了回 `{"ignored": null}`。

### 8.5 删一条记录

```
DELETE /records/{record_id}
```

响应 `{"deleted": true}`，记录不存在回 `404`。它引用的原文要是没有别的记录再用，顺手把对象存储里那份和本机缓存那份一起清掉。

---
## 数据模型

### CorrectionResult

| 字段 | 类型 | 说明 |
|------|------|------|
| `original_text` | string | 规范化后的原文，仅去除首尾空白 |
| `correct_text` | string | 纠错后文本；无错误时与原文相同 |
| `wrong_ids` | int[] | 错误字符位置索引，去重升序 |
| `errors` | CorrectionError[] | 单字粒度错误明细 |

### CorrectionError

| 字段 | 类型 | 说明 |
|------|------|------|
| `position` | int | 在 `original_text` 中的索引，从 0 开始 |
| `original_char` | string | 原文字符；`insert` 时为空字符串 |
| `correct_char` | string | 正确字符；`delete` 时为空字符串 |
| `tag` | enum | `replace` / `insert` / `delete` |

约定：

- 对外返回的 `errors` 为单字粒度；连续多字替换会拆分为多个 `CorrectionError`。
- 每条 `errors` 都必须能对上原文并且真的改到一个字：`replace` / `delete` 的 `original_char` 必须等于 `original_text[position]`，对不上的项不会出现在结果里。所以按 `errors` 依次套用得到的文本必然等于 `correct_text`。
- `insert` 的 `position` 允许等于 `original_text` 的长度，表示追加到末尾。
- 同一个 `position` 上可能有多条 `insert`（连续补字），按数组顺序首尾相接插到该位置"原来那个字符"的前面：三条 `增`/`值`/`税` 得到 `增值税`，不是 `税值增`。
- 同一个 `position` 上同时有 `insert` 和 `replace` / `delete` 时先插入、再处理那一个字符；结果里不会出现两条互相冲突的 `replace` / `delete`。

---

## 状态码说明

| HTTP | 场景 | 响应体 |
|------|------|--------|
| 200 | 成功 | 正常业务响应；`/health/correction` 内部异常时也为 200，但 `status=error` |
| 400 | 对外接口 `content` / `file` / `fileUrl` 都没给（含发 JSON、把 `content` 放查询串），或 `fileUrl` 取不回来 | `{"detail": "content（文本）、file（文件）和 fileUrl（文件链接）至少要给一个"}` |
| 404 | 记录不存在 / 文档不存在或已过期 | `{"detail": "..."}` |
| 422 | 请求体参数校验失败（如缺少 `content`） | `{"detail": "..."}` |
| 500 | 文档/合同处理失败 | `{"detail": "..."}` |
| 503 | 纠错记录存储（MySQL）没配或连不上 | `{"detail": "纠错记录存储暂时不可用"}` |

---

## 调试建议

- 请求头传入 `X-Request-ID`，响应头会回传相同 ID，用于关联日志。
- 日志事件示例：`http.start`、`request.correct_single`、`tool.done`、`tool.llm_fail`、`tool.chunk_fail`、`http.ok`。
- 设置 `LOG_LEVEL=DEBUG` 可输出更详细的文本预览。
- 测试模式设置 `ENABLE_TEST_MODE=true` 可验证接口格式，不消耗 LLM 调用。
