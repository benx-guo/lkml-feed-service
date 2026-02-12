"""Tests for lkml_feed_api._nntp — minimal NNTP client."""

import io
import socket
from unittest.mock import MagicMock, patch

import pytest

from lkml_feed_api._nntp import NNTP, ArticleInfo, NNTPError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(server_bytes: bytes) -> NNTP:
    """Create an NNTP instance with a fake socket that yields *server_bytes*.

    The first line of *server_bytes* must be a 2xx greeting.
    """
    fake_file = io.BytesIO(server_bytes)
    fake_sock = MagicMock(spec=socket.socket)
    fake_sock.makefile.return_value = fake_file

    with patch("lkml_feed_api._nntp.socket.create_connection", return_value=fake_sock):
        conn = NNTP("fake", 119)

    # Replace internals so subsequent commands read from the same stream
    conn._sock = fake_sock
    conn._file = fake_file
    return conn


# ---------------------------------------------------------------------------
# Connection / greeting
# ---------------------------------------------------------------------------


class TestConnect:
    def test_successful_greeting(self):
        conn = _make_conn(b"200 welcome\r\n")
        assert conn is not None

    def test_refused_greeting(self):
        with pytest.raises(NNTPError, match="Connection refused"):
            _make_conn(b"502 access denied\r\n")

    def test_empty_greeting(self):
        with pytest.raises(NNTPError, match="Connection closed"):
            _make_conn(b"")


# ---------------------------------------------------------------------------
# DATE
# ---------------------------------------------------------------------------


class TestDate:
    def test_date_ok(self):
        conn = _make_conn(b"200 welcome\r\n111 20260212120000\r\n")
        resp = conn.date()
        assert resp.startswith("111")
        assert "20260212" in resp

    def test_date_error(self):
        conn = _make_conn(b"200 welcome\r\n500 command not recognized\r\n")
        with pytest.raises(NNTPError):
            conn.date()


# ---------------------------------------------------------------------------
# QUIT
# ---------------------------------------------------------------------------


class TestQuit:
    def test_quit_ok(self):
        conn = _make_conn(b"200 welcome\r\n205 bye\r\n")
        resp = conn.quit()
        assert "205" in resp

    def test_quit_on_broken_conn(self):
        """quit() should not raise even if the connection is already dead."""
        conn = _make_conn(b"200 welcome\r\n")
        # file is exhausted, so sendall / readline will fail
        conn._sock.sendall.side_effect = OSError("broken pipe")
        resp = conn.quit()
        assert resp == ""


# ---------------------------------------------------------------------------
# GROUP
# ---------------------------------------------------------------------------


class TestGroup:
    def test_group_ok(self):
        conn = _make_conn(
            b"200 welcome\r\n"
            b"211 1234 100 50000 org.kernel.vger.linux-doc\r\n"
        )
        resp, count, first, last, name = conn.group("org.kernel.vger.linux-doc")
        assert resp.startswith("211")
        assert count == 1234
        assert first == 100
        assert last == 50000
        assert name == "org.kernel.vger.linux-doc"

    def test_group_no_such_group(self):
        conn = _make_conn(b"200 welcome\r\n411 no such group\r\n")
        with pytest.raises(NNTPError, match="411"):
            conn.group("nonexistent")

    def test_group_name_fallback(self):
        """If response has no group name field, use the requested name."""
        conn = _make_conn(b"200 welcome\r\n211 10 1 10\r\n")
        _, _, _, _, name = conn.group("my.group")
        assert name == "my.group"


# ---------------------------------------------------------------------------
# OVER
# ---------------------------------------------------------------------------

_OVER_RESPONSE = (
    b"200 welcome\r\n"
    b"224 Overview information follows\r\n"
    b"100\t[PATCH] fix bug\tAlice <a@b.com>\tSun, 09 Feb 2026 10:00:00 +0000"
    b"\t<msg-id-1@test>\t<ref@test>\t5000\t80\r\n"
    b"101\tRe: [PATCH] fix bug\tBob <b@b.com>\tSun, 09 Feb 2026 11:00:00 +0000"
    b"\t<msg-id-2@test>\t<msg-id-1@test>\t3000\t40\r\n"
    b".\r\n"
)


class TestOver:
    def test_over_ok(self):
        conn = _make_conn(_OVER_RESPONSE)
        resp, overviews = conn.over((100, 101))
        assert resp.startswith("224")
        assert len(overviews) == 2

        art_num, ov = overviews[0]
        assert art_num == 100
        assert ov["subject"] == "[PATCH] fix bug"
        assert "Alice" in ov["from"]
        assert ov["message-id"] == "<msg-id-1@test>"
        assert ov["references"] == "<ref@test>"

        art_num2, ov2 = overviews[1]
        assert art_num2 == 101
        assert "Re:" in ov2["subject"]

    def test_over_error(self):
        conn = _make_conn(b"200 welcome\r\n423 no articles in range\r\n")
        with pytest.raises(NNTPError, match="423"):
            conn.over((999, 999))

    def test_over_malformed_line_skipped(self):
        """Lines with non-integer article numbers should be silently skipped."""
        conn = _make_conn(
            b"200 welcome\r\n"
            b"224 Overview\r\n"
            b"notanumber\tbad line\r\n"
            b"100\tgood subject\tFrom\tDate\t<mid>\t\t0\t0\r\n"
            b".\r\n"
        )
        _, overviews = conn.over((100, 100))
        assert len(overviews) == 1
        assert overviews[0][0] == 100

    def test_over_missing_fields_default_empty(self):
        """Fields beyond what the server provides should default to ''."""
        conn = _make_conn(
            b"200 welcome\r\n"
            b"224 Overview\r\n"
            b"100\tsubject only\r\n"
            b".\r\n"
        )
        _, overviews = conn.over((100, 100))
        assert len(overviews) == 1
        ov = overviews[0][1]
        assert ov["subject"] == "subject only"
        assert ov["from"] == ""
        assert ov["references"] == ""


# ---------------------------------------------------------------------------
# BODY
# ---------------------------------------------------------------------------


class TestBody:
    def test_body_ok(self):
        conn = _make_conn(
            b"200 welcome\r\n"
            b"222 100 body follows\r\n"
            b"Hello world\r\n"
            b"Second line\r\n"
            b".\r\n"
        )
        resp, info = conn.body(100)
        assert resp.startswith("222")
        assert isinstance(info, ArticleInfo)
        assert len(info.lines) == 2
        assert info.lines[0] == b"Hello world"
        assert info.lines[1] == b"Second line"

    def test_body_error(self):
        conn = _make_conn(b"200 welcome\r\n423 no such article\r\n")
        with pytest.raises(NNTPError, match="423"):
            conn.body(999)

    def test_body_dot_unstuffing(self):
        """Lines starting with '..' should have the leading dot removed."""
        conn = _make_conn(
            b"200 welcome\r\n"
            b"222 body\r\n"
            b"..This line starts with a dot\r\n"
            b"Normal line\r\n"
            b"...\r\n"
            b".\r\n"
        )
        _, info = conn.body(100)
        assert info.lines[0] == b".This line starts with a dot"
        assert info.lines[1] == b"Normal line"
        assert info.lines[2] == b".."

    def test_body_empty(self):
        conn = _make_conn(
            b"200 welcome\r\n"
            b"222 body\r\n"
            b".\r\n"
        )
        _, info = conn.body(100)
        assert info.lines == []


# ---------------------------------------------------------------------------
# Multiline edge cases
# ---------------------------------------------------------------------------


class TestMultiline:
    def test_connection_closed_during_multiline(self):
        conn = _make_conn(
            b"200 welcome\r\n"
            b"222 body\r\n"
            b"incomplete, no dot terminator\r\n"
            # stream ends without ".\r\n"
        )
        with pytest.raises(NNTPError, match="Connection closed"):
            conn.body(100)

    def test_lf_only_line_endings(self):
        """Server sending LF-only (no CR) should still work."""
        conn = _make_conn(
            b"200 welcome\n"
            b"222 body\n"
            b"line one\n"
            b"line two\n"
            b".\n"
        )
        _, info = conn.body(100)
        assert len(info.lines) == 2
        assert info.lines[0] == b"line one"
