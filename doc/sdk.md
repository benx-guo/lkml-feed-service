# SDK 使用指南

## 快速开始

每次调用 `get_latest()` 返回一批新消息（最多 1000 条）。当 `is_caught_up` 为 `False` 时，说明还有更多未处理的消息，应继续调用直到 `True`，此时已拉取到全部增量。

```python
import time
from lkml_feed_api import LKMLFeedClient

client = LKMLFeedClient(
    subsystems=["linux-doc"],
    keywords=["zh_cn"],
)

while True:
    result = client.get_latest()
    for e in result.entries:
        print(e.subject, e.author)
    if result.is_caught_up:
        time.sleep(60)  # 已追平，等待新消息
```

## API

### `LKMLFeedClient(subsystems, *, keywords, state_file)`

创建客户端，长期持有，连接自动复用。

| 参数 | 类型 | 说明 |
|------|------|------|
| `subsystems` | `List[str]` | 子系统列表，如 `["linux-doc"]` |
| `keywords` | `Optional[List[str]]` | 关键词列表，不区分大小写，匹配 subject（OR 逻辑）。不传则返回所有消息 |
| `state_file` | `Optional[str]` | 状态持久化文件路径，默认 `.lkml_feed_state.json`，传 `None` 禁用 |

### `get_latest() -> FetchResult`

拉取一批新消息。

返回 `FetchResult`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `entries` | `List[MailEntry]` | 本批命中的条目 |
| `is_caught_up` | `bool` | `True` 已追平最新，`False` 还有更多 |

### `rewind(n: int)`

回拨 n 条消息。下次 `get_latest()` 会重新返回这些消息。

### `reset()`

重置状态，下次 `get_latest()` 从当前最新位置开始。

### `close()`

关闭连接并保存状态。

## `MailEntry` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `subject` | `str` | 邮件主题 |
| `author` | `str` | 作者姓名 |
| `email` | `Optional[str]` | 作者邮箱 |
| `url` | `Optional[str]` | lore.kernel.org 链接 |
| `message_id` | `Optional[str]` | Message-ID |
| `in_reply_to` | `Optional[str]` | 父消息 Message-ID |
| `received_at` | `str` | 接收时间（ISO 8601） |
| `summary` | `str` | 邮件正文（仅 subject 命中关键词时拉取，否则为空） |
| `subsystem` | `str` | 所属子系统 |

## 与 API 层的区别

API 层（`/latest`）和 SDK 层行为一致：每次请求拉取一个批次，返回 `is_caught_up` 标志。调用方可据此判断是否继续拉取。

## 工作原理

- NNTP 连接 `nntp.lore.kernel.org:119`，group 为 `org.kernel.vger.{subsystem}`
- OVER 命令批量获取元数据（subject / from / date / message-id / references），每批最多 1000 篇
- 基于 article number 做增量游标，持久化到 JSON 文件，重启不丢状态
- 按 subject 关键词过滤，只对命中的条目拉取 BODY
- 断线自动重连（3 次重试，指数退避）
