# LLM 网关 Postman 请求/响应示例

> 对应网关文档 3.2 请求体字段、3.3 响应体字段。示例假设网关接口为 OpenAI 兼容的 `/chat/completions`。
## 本服务的实际请求

程序统一走 **OpenAI 兼容的基础字段**（Bearer token 鉴权），并不是下面的工具调用示例：

```json
{
  "model": "{{OPEN_AI_MODEL}}",
  "messages": [
    {
      "role": "system",
      "content": "你是文本纠错助手……只输出结构化字段"
    },
    {
      "role": "user",
      "content": "请纠错以下带下标文本：\n<indexed_text>"
    }
  ],
  "stream": false,
  "temperature": 0,
  "max_tokens": 4096
}
```

说明：

- 生产入口使用 `build_plain_json_chain`，模型以纯文本返回 JSON，再由程序手动解析。
- 因此当前生产调用 **不发送** `tools`、`tool_choice`、`stream_options`、`reasoning` 等字段。
- 本文后面的工具调用示例是网关文档的通用能力示例，仅供联调参考；如果后续改用 `with_structured_output`，才会实际发送 `tools` / `tool_choice`。

## 1. Postman 配置

| 项目 | 值 |
|------|----|
| Method | `POST` |
| URL | `{{OPEN_BASE_URL}}/chat/completions` |
| Body | `raw` -> `JSON` |

**Headers**

| Header | 测试环境 | 生产环境 |
|--------|----------|----------|
| `Content-Type` | `application/json` | `application/json` |
| `Authorization` | `Bearer {{OPEN_AI_TOKEN}}` | `Bearer {{OPEN_AI_TOKEN}}` |
| `X-Request-ID`（可选） | 链路追踪 ID | 链路追踪 ID |

环境变量示例：

- 测试：`OPEN_BASE_URL=https://llm.example.com/v1`
- 生产：`OPEN_BASE_URL=https://172.16.20.250:5030/knowledgeService/extChatApi/modelChat/v1`

---

## 2. 非流式请求/响应

### 请求体

```json
{
  "model": "qwen3.5-27b",
  "messages": [
    {
      "role": "system",
      "content": "你是一名中文文本纠错助手。"
    },
    {
      "role": "user",
      "content": "请帮我改正：今天天气很好，我们一起去公圆散步。"
    }
  ],
  "stream": false,
  "max_tokens": 512,
  "temperature": 0.2
}
```

### 响应体

```json
{
  "id": "chatcmpl-1234567890",
  "object": "chat.completion",
  "created": 1755500000,
  "model": "qwen3.5-27b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "今天天气很好，我们一起去公园散步。",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 38,
    "completion_tokens": 12,
    "total_tokens": 50
  }
}
```

说明：

- `stream=false` 时，最终正文在 `choices[0].message.content`。
- 如果调用工具，本阶段的 `content` 可能为 `null`，工具调用在 `choices[0].message.tool_calls`。

---

## 3. 流式请求/响应

### 请求体

```json
{
  "model": "qwen3.5-27b",
  "messages": [
    {
      "role": "user",
      "content": "请帮我改正：请确认合同生效日期为2026年8月18日。"
    }
  ],
  "stream": true,
  "stream_options": {
    "include_usage": true
  },
  "max_tokens": 512,
  "temperature": 0.2
}
```

### SSE 响应示例

```text
data: {"id":"chatcmpl-1234567890","object":"chat.completion.chunk","created":1755500000,"model":"qwen3.5-27b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-1234567890","object":"chat.completion.chunk","created":1755500000,"model":"qwen3.5-27b","choices":[{"index":0,"delta":{"content":"请确认"},"finish_reason":null}]}

data: {"id":"chatcmpl-1234567890","object":"chat.completion.chunk","created":1755500000,"model":"qwen3.5-27b","choices":[{"index":0,"delta":{"content":"合同生效日期为"},"finish_reason":null}]}

data: {"id":"chatcmpl-1234567890","object":"chat.completion.chunk","created":1755500000,"model":"qwen3.5-27b","choices":[{"index":0,"delta":{"content":"2026年8月18日。"},"finish_reason":null}]}

data: {"id":"chatcmpl-1234567890","object":"chat.completion.chunk","created":1755500000,"model":"qwen3.5-27b","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":21,"completion_tokens":14,"total_tokens":35}}

data: [DONE]
```

说明：

- `stream=true` 时，正文分片在 `choices[0].delta.content`，必须按顺序追加。
- 最后一个 `usage` 只有设置了 `stream_options.include_usage=true` 时才返回。
- 服务端可能返回 `reasoning_content` 或 `reasoning` 字段作为思考内容，并不是所有模型都返回。

---

## 4. 工具调用请求/响应

### 第一步：模型要求调用工具

```json
{
  "model": "qwen3.5-27b",
  "messages": [
    {
      "role": "user",
      "content": "北京今天天气怎么样？"
    }
  ],
  "stream": false,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市的实时天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {
              "type": "string",
              "description": "城市名称"
            }
          },
          "required": [
            "city"
          ]
        }
      }
    }
  ],
  "tool_choice": "auto",
  "temperature": 0.2
}
```

### 模型返回工具调用

```json
{
  "id": "chatcmpl-tool-123456",
  "object": "chat.completion",
  "created": 1755500000,
  "model": "qwen3.5-27b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_weather_001",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"city\":\"北京\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ],
  "usage": {
    "prompt_tokens": 86,
    "completion_tokens": 18,
    "total_tokens": 104
  }
}
```

### 第二步：把工具执行结果回传模型

```json
{
  "model": "qwen3.5-27b",
  "messages": [
    {
      "role": "user",
      "content": "北京今天天气怎么样？"
    },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_weather_001",
          "type": "function",
          "function": {
            "name": "get_weather",
            "arguments": "{\"city\":\"北京\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_weather_001",
      "content": "{\"city\":\"北京\",\"weather\":\"晴\",\"temperature\":\"32℃\"}"
    }
  ],
  "stream": false,
  "temperature": 0.2
}
```

### 最终回答

```json
{
  "id": "chatcmpl-tool-123457",
  "object": "chat.completion",
  "created": 1755500001,
  "model": "qwen3.5-27b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "北京今天晴，气温 32℃。",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 118,
    "completion_tokens": 15,
    "total_tokens": 133
  }
}
```

---

## 5. 含思考内容的响应示例

```json
{
  "id": "chatcmpl-reason-123456",
  "object": "chat.completion",
  "created": 1755500002,
  "model": "qwen3.5-27b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "reasoning_content": "先判断文本中是否存在错别字，再给出修正结果。",
        "content": "今天天气很好，我们一起去公园散步。",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 38,
    "completion_tokens": 12,
    "total_tokens": 50
  }
}
```

说明：

- 思考字段可能为 `reasoning_content` 或 `reasoning`，需要按实际模型返回处理。
- 该字段属于可选内容，不应影响对 `message.content` 的解析。
