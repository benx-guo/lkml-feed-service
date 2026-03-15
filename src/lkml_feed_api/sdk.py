"""LKML Feed SDK — 对外公共接口。

用法::

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
            break  # 已追平，等会儿再来
"""

from typing import List, Optional

from .feed import NNTPFetcher
from .models import FetchResult, MailEntry

_DEFAULT_STATE_FILE = ".lkml_feed_state.json"


class LKMLFeedClient:
    """lore.kernel.org 邮件列表 NNTP 客户端。

    初始化时指定要监听的子系统和关键词，之后每次 ``get_latest()``
    拉取新消息并返回关键词匹配的结果。

    Args:
        subsystems: 要监听的子系统列表。
        keywords:   关键词列表（不区分大小写，匹配 subject，OR 逻辑）。
                    匹配的条目才会拉取正文。不传则返回所有新消息。
        state_file: 状态文件路径。默认 ``.lkml_feed_state.json``，
                    传 ``None`` 禁用持久化。
    """

    def __init__(
        self,
        subsystems: List[str],
        *,
        keywords: Optional[List[str]] = None,
        state_file: Optional[str] = _DEFAULT_STATE_FILE,
    ) -> None:
        self._subsystems = subsystems
        self._keywords = [k.lower() for k in keywords] if keywords else None
        self._fetcher = NNTPFetcher(state_file=state_file)

    def get_latest(self) -> FetchResult:
        """拉取新消息，返回关键词匹配的条目及是否已追平。"""
        match_fn = None
        if self._keywords:
            match_fn = lambda e: _match_any(e, self._keywords)  # noqa: E731

        return self._fetcher.fetch_latest(self._subsystems, match_fn=match_fn)

    def rewind(self, n: int) -> None:
        """将所有子系统的游标回拨 *n* 篇。"""
        for subsystem in self._subsystems:
            self._fetcher.rewind(f"org.kernel.vger.{subsystem}", n)

    def reset(self) -> None:
        """重置游标状态，下次拉取从当前最新开始。"""
        self._fetcher.reset_cursors()

    def close(self) -> None:
        """关闭 NNTP 连接并保存状态。"""
        self._fetcher.close()


def _match_any(entry: MailEntry, keywords: List[str]) -> bool:
    """subject 包含任意一个关键词即返回 True。"""
    subject = entry.subject.lower()
    return any(kw in subject for kw in keywords)
