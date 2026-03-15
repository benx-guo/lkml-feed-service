"""Minimal NNTP client replacing the deprecated nntplib (removed in Python 3.13)."""

import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class NNTPError(Exception):
    """Base NNTP error."""


@dataclass
class ArticleInfo:
    """Mimics the subset of nntplib.ArticleInfo used by feed.py."""

    lines: List[bytes] = field(default_factory=list)


# Standard OVER field names (RFC 3977 §8.4), in order after article number.
_OVER_FIELDS = (
    "subject",
    "from",
    "date",
    "message-id",
    "references",
    ":bytes",
    ":lines",
)


class NNTP:
    """Minimal NNTP client implementing only the commands we need.

    Supports: DATE, QUIT, GROUP, OVER, BODY.
    """

    def __init__(self, host: str, port: int = 119, timeout: float = 30) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._file = self._sock.makefile("rb")
        # Read server greeting
        resp = self._readline()
        if not resp.startswith("2"):
            raise NNTPError(f"Connection refused: {resp}")

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _sendline(self, cmd: str) -> None:
        self._sock.sendall(f"{cmd}\r\n".encode())

    def _readline(self) -> str:
        raw = self._file.readline()
        if not raw:
            raise NNTPError("Connection closed unexpectedly")
        return raw.decode("utf-8", errors="replace").strip()

    def _read_multiline(self) -> List[bytes]:
        """Read a dot-terminated multi-line response block."""
        lines: List[bytes] = []
        while True:
            raw = self._file.readline()
            if not raw:
                raise NNTPError("Connection closed during multiline response")
            # Strip trailing CRLF / LF
            if raw.endswith(b"\r\n"):
                raw = raw[:-2]
            elif raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw == b".":
                break
            # Dot-unstuffing (RFC 3977 §3.1.1)
            if raw.startswith(b".."):
                raw = raw[1:]
            lines.append(raw)
        return lines

    # ------------------------------------------------------------------
    # NNTP commands
    # ------------------------------------------------------------------

    def date(self) -> str:
        """DATE — used as a keep-alive / health check."""
        self._sendline("DATE")
        resp = self._readline()
        if not resp.startswith("111"):
            raise NNTPError(resp)
        return resp

    def quit(self) -> str:
        """QUIT — close the connection gracefully."""
        try:
            self._sendline("QUIT")
            resp = self._readline()
        except (OSError, NNTPError):
            resp = ""
        finally:
            self._file.close()
            self._sock.close()
        return resp

    def group(self, name: str) -> Tuple[str, int, int, int, str]:
        """GROUP — select a newsgroup.

        Returns ``(resp, count, first, last, group_name)``.
        """
        self._sendline(f"GROUP {name}")
        resp = self._readline()
        if not resp.startswith("211"):
            raise NNTPError(resp)
        # 211 count first last groupname
        parts = resp.split()
        count = int(parts[1])
        first = int(parts[2])
        last = int(parts[3])
        group_name = parts[4] if len(parts) > 4 else name
        return resp, count, first, last, group_name

    def over(
        self, message_spec: Tuple[int, int]
    ) -> Tuple[str, List[Tuple[int, Dict[str, str]]]]:
        """OVER — retrieve overview data for a range of articles.

        *message_spec* is ``(start, end)``.
        Returns ``(resp, [(art_num, overview_dict), ...])``.
        """
        start, end = message_spec
        self._sendline(f"OVER {start}-{end}")
        resp = self._readline()
        if not resp.startswith("224"):
            raise NNTPError(resp)
        raw_lines = self._read_multiline()

        result: List[Tuple[int, Dict[str, str]]] = []
        for raw in raw_lines:
            text = raw.decode("utf-8", errors="replace")
            fields = text.split("\t")
            try:
                art_num = int(fields[0])
            except (ValueError, IndexError):
                continue
            overview: Dict[str, str] = {}
            for i, key in enumerate(_OVER_FIELDS):
                overview[key] = fields[i + 1] if i + 1 < len(fields) else ""
            result.append((art_num, overview))
        return resp, result

    def body(self, article_num: int) -> Tuple[str, ArticleInfo]:
        """BODY — retrieve the body of an article.

        Returns ``(resp, ArticleInfo)`` where ``ArticleInfo.lines`` is a list
        of raw byte-strings (one per line).
        """
        self._sendline(f"BODY {article_num}")
        resp = self._readline()
        if not resp.startswith("222"):
            raise NNTPError(resp)
        lines = self._read_multiline()
        return resp, ArticleInfo(lines=lines)

    def body_many(
        self, article_nums: List[int]
    ) -> List[Tuple[int, Optional[ArticleInfo]]]:
        """BODY pipelining — send all commands, then read all responses.

        Returns ``[(article_num, ArticleInfo or None), ...]``.
        """
        # Send all BODY commands without waiting
        for num in article_nums:
            self._sendline(f"BODY {num}")

        # Read all responses in order
        results: List[Tuple[int, Optional[ArticleInfo]]] = []
        for num in article_nums:
            resp = self._readline()
            if resp.startswith("222"):
                lines = self._read_multiline()
                results.append((num, ArticleInfo(lines=lines)))
            else:
                results.append((num, None))
        return results
