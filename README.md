# lkml-feed-service

REST API 服务，通过 NNTP 从 [lore.kernel.org](https://lore.kernel.org/) 增量拉取内核邮件列表消息。

## 启动

```bash
uv sync
uv run lkml-feed-api
```

## API

### `GET /`

健康检查。

```json
{"code": 200, "message": "pong"}
```

### `GET /latest`

返回自上次请求以来的新消息。每次最多处理 100 条消息，积压较多时剩余部分会在后续请求中返回。

```json
{
  "code": 200,
  "message": "",
  "data": {
    "entries": [
      {
        "subject": "[PATCH v2] docs/zh_CN: Add index.rst translation",
        "author": "Alice",
        "email": "alice@example.com",
        "url": "https://lore.kernel.org/linux-doc/msg-id@domain/",
        "message_id": "msg-id@domain",
        "in_reply_to": null,
        "received_at": "2026-02-10T12:00:00+00:00",
        "summary": "...",
        "subsystem": "linux-doc"
      }
    ]
  }
}
```

### `POST /rewind?n=5000`

回拨 n 条消息，之后 `/latest` 会重新返回这些消息。

```bash
curl -X POST "http://localhost:8000/rewind?n=5000"
```

```json
{"code": 200, "message": "rewound 5000 messages"}
```

### `POST /reset`

重置状态，跳过所有历史，之后 `/latest` 从最新位置开始。

```bash
curl -X POST http://localhost:8000/reset
```

```json
{"code": 200, "message": "reset to latest"}
```

## 配置

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `LKML_SUBSYSTEMS` | 子系统列表，逗号分隔 | `linux-doc` |
| `LKML_KEYWORDS` | 关键词列表，逗号分隔（不区分大小写，匹配 subject，OR 逻辑） | `zh_cn` |
| `LKML_BODY_CONCURRENCY` | 正文拉取并发数，设为 `1` 串行，`>1` 多连接并行 | `1` |

## 许可证

[MIT](LICENSE)
