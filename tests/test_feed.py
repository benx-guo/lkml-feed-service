"""Tests for lkml_feed_api.feed — NNTPFetcher with mocked NNTP connections."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from lkml_feed_api._nntp import NNTP, ArticleInfo, NNTPError
from lkml_feed_api.feed import NNTPFetcher, _MAX_ARTICLES_PER_FETCH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_nntp(**kwargs) -> MagicMock:
    """Create a MagicMock pretending to be an NNTP connection."""
    conn = MagicMock(spec=NNTP)
    conn.date.return_value = "111 20260306120000"
    conn.group.return_value = ("211 100 1 500 grp", 100, 1, 500, "grp")
    conn.over.return_value = ("224 ok", [])
    conn.body.return_value = ("222 ok", ArticleInfo(lines=[b"body text"]))
    conn.body_many.side_effect = lambda nums: [
        (n, ArticleInfo(lines=[b"body text"])) for n in nums
    ]
    conn.quit.return_value = "205 bye"
    for k, v in kwargs.items():
        setattr(conn, k, v)
    return conn


def _make_fetcher(tmp_path, cursors=None) -> NNTPFetcher:
    """Create an NNTPFetcher with a temp state file."""
    state_file = str(tmp_path / "state.json")
    if cursors:
        Path(state_file).write_text(json.dumps(cursors))
    return NNTPFetcher(state_file=state_file)


def _overview(art_num, subject="test subject", from_="Alice <a@b.com>",
              date="Sun, 01 Jan 2026 00:00:00 +0000",
              message_id="<mid@test>", references=""):
    return (art_num, {
        "subject": subject,
        "from": from_,
        "date": date,
        "message-id": message_id,
        "references": references,
        ":bytes": "1000",
        ":lines": "20",
    })


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


class TestConnect:
    @patch("lkml_feed_api.feed.NNTP")
    def test_connect_creates_new_connection(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        MockNNTP.return_value = mock_conn
        fetcher = _make_fetcher(tmp_path)
        conn = fetcher._connect()
        assert conn is mock_conn
        MockNNTP.assert_called_once()

    @patch("lkml_feed_api.feed.NNTP")
    def test_connect_reuses_healthy_connection(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        MockNNTP.return_value = mock_conn
        fetcher = _make_fetcher(tmp_path)
        fetcher._connect()
        fetcher._connect()
        # Only one NNTP() call — connection reused
        MockNNTP.assert_called_once()

    @patch("lkml_feed_api.feed.NNTP")
    def test_connect_reconnects_on_dead_connection(self, MockNNTP, tmp_path):
        dead_conn = _mock_nntp()
        dead_conn.date.side_effect = OSError("broken")
        new_conn = _mock_nntp()
        MockNNTP.side_effect = [dead_conn, new_conn]

        fetcher = _make_fetcher(tmp_path)
        fetcher._connect()  # gets dead_conn
        dead_conn.date.side_effect = OSError("broken")
        conn = fetcher._connect()  # dead_conn.date() fails, reconnects
        assert conn is new_conn

    @patch("lkml_feed_api.feed.NNTP")
    def test_connect_reconnects_on_value_error(self, MockNNTP, tmp_path):
        """ValueError from corrupted socket buffer should trigger reconnect."""
        bad_conn = _mock_nntp()
        bad_conn.date.side_effect = ValueError("PyMemoryView buf NULL")
        good_conn = _mock_nntp()
        MockNNTP.side_effect = [bad_conn, good_conn]

        fetcher = _make_fetcher(tmp_path)
        fetcher._connect()
        bad_conn.date.side_effect = ValueError("PyMemoryView buf NULL")
        conn = fetcher._connect()
        assert conn is good_conn

    @patch("lkml_feed_api.feed.time.sleep")
    @patch("lkml_feed_api.feed.NNTP")
    def test_connect_retries_on_failure(self, MockNNTP, mock_sleep, tmp_path):
        MockNNTP.side_effect = [OSError("timeout"), OSError("timeout"), _mock_nntp()]
        fetcher = _make_fetcher(tmp_path)
        fetcher._conn = None
        conn = fetcher._connect()
        assert conn is not None
        assert MockNNTP.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("lkml_feed_api.feed.time.sleep")
    @patch("lkml_feed_api.feed.NNTP")
    def test_connect_raises_after_max_retries(self, MockNNTP, mock_sleep, tmp_path):
        MockNNTP.side_effect = OSError("always fails")
        fetcher = _make_fetcher(tmp_path)
        fetcher._conn = None
        with pytest.raises(OSError, match="always fails"):
            fetcher._connect()

    @patch("lkml_feed_api.feed.NNTP")
    def test_close_conn_tolerates_quit_failure(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.quit.side_effect = OSError("already closed")
        MockNNTP.return_value = mock_conn
        fetcher = _make_fetcher(tmp_path)
        fetcher._connect()
        fetcher._close_conn()
        assert fetcher._conn is None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestState:
    def test_load_and_save_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"grp": 42}))

        fetcher = NNTPFetcher(state_file=str(state_file))
        assert fetcher._cursors == {"grp": 42}

        fetcher._cursors["grp"] = 100
        fetcher._save_state()
        assert json.loads(state_file.read_text()) == {"grp": 100}

    def test_load_ignores_invalid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not json")
        fetcher = NNTPFetcher(state_file=str(state_file))
        assert fetcher._cursors == {}

    def test_no_state_file(self):
        fetcher = NNTPFetcher(state_file=None)
        assert fetcher._cursors == {}
        fetcher._save_state()  # should not raise


# ---------------------------------------------------------------------------
# Rewind
# ---------------------------------------------------------------------------


class TestRewind:
    @patch("lkml_feed_api.feed.NNTP")
    def test_rewind_existing_cursor(self, MockNNTP, tmp_path):
        fetcher = _make_fetcher(tmp_path, cursors={"grp": 500})
        fetcher.rewind("grp", 100)
        assert fetcher._cursors["grp"] == 400

    @patch("lkml_feed_api.feed.NNTP")
    def test_rewind_clamps_to_zero(self, MockNNTP, tmp_path):
        fetcher = _make_fetcher(tmp_path, cursors={"grp": 50})
        fetcher.rewind("grp", 100)
        assert fetcher._cursors["grp"] == 0

    @patch("lkml_feed_api.feed.NNTP")
    def test_rewind_unknown_group_queries_server(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 grp", 100, 1, 500, "grp")
        MockNNTP.return_value = mock_conn
        fetcher = _make_fetcher(tmp_path)
        fetcher.rewind("grp", 100)
        assert fetcher._cursors["grp"] == 400

    @patch("lkml_feed_api.feed.NNTP")
    def test_rewind_persists_state(self, MockNNTP, tmp_path):
        fetcher = _make_fetcher(tmp_path, cursors={"grp": 500})
        fetcher.rewind("grp", 50)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["grp"] == 450


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_all_cursors(self, tmp_path):
        fetcher = _make_fetcher(tmp_path, cursors={"a": 1, "b": 2})
        fetcher.reset_cursors()
        assert fetcher._cursors == {}
        state = json.loads((tmp_path / "state.json").read_text())
        assert state == {}


# ---------------------------------------------------------------------------
# Parse overview
# ---------------------------------------------------------------------------


class TestParseOverview:
    def test_basic_parse(self):
        overview = {
            "subject": "[PATCH] docs/zh_CN: fix typo",
            "from": "Alice Wang <alice@example.com>",
            "date": "Sun, 01 Jan 2026 12:00:00 +0000",
            "message-id": "<abc123@example.com>",
            "references": "<parent@example.com>",
            ":bytes": "5000",
            ":lines": "80",
        }
        entry = NNTPFetcher._parse_overview(overview, "linux-doc")
        assert entry.subject == "[PATCH] docs/zh_CN: fix typo"
        assert entry.author == "Alice Wang"
        assert entry.email == "alice@example.com"
        assert entry.message_id == "abc123@example.com"
        assert entry.in_reply_to == "parent@example.com"
        assert entry.subsystem == "linux-doc"
        assert "linux-doc" in entry.url
        assert "abc123@example.com" in entry.url

    def test_missing_name_uses_from_header(self):
        overview = {
            "subject": "test",
            "from": "just-an-address",
            "date": "",
            "message-id": "",
            "references": "",
        }
        entry = NNTPFetcher._parse_overview(overview, "linux-doc")
        assert entry.author == "just-an-address"

    def test_empty_message_id_no_url(self):
        overview = {
            "subject": "test",
            "from": "",
            "date": "",
            "message-id": "",
            "references": "",
        }
        entry = NNTPFetcher._parse_overview(overview, "linux-doc")
        assert entry.url is None
        assert entry.message_id is None

    def test_invalid_date_uses_utc_now(self):
        overview = {
            "subject": "test",
            "from": "",
            "date": "not-a-date",
            "message-id": "",
            "references": "",
        }
        entry = NNTPFetcher._parse_overview(overview, "linux-doc")
        assert entry.received_at is not None  # falls back to utcnow


# ---------------------------------------------------------------------------
# Fetch subsystem
# ---------------------------------------------------------------------------


class TestFetchSubsystem:
    @patch("lkml_feed_api.feed.NNTP")
    def test_first_run_initializes_cursor(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [_overview(401)])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path)
        entries, caught_up = fetcher._fetch_subsystem("linux-doc")

        # Cursor initialized to last - MAX = 500 - 100 = 400
        group_name = "org.kernel.vger.linux-doc"
        assert group_name in fetcher._cursors

    @patch("lkml_feed_api.feed.NNTP")
    def test_caught_up_returns_empty(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 500})
        entries, caught_up = fetcher._fetch_subsystem("linux-doc")
        assert entries == []
        assert caught_up is True

    @patch("lkml_feed_api.feed.NNTP")
    def test_fetches_new_articles(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [
            _overview(451, subject="Article 1"),
            _overview(452, subject="Article 2"),
        ])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 450})
        entries, caught_up = fetcher._fetch_subsystem("linux-doc")
        assert len(entries) == 2
        assert entries[0].subject == "Article 1"

    @patch("lkml_feed_api.feed.NNTP")
    def test_match_fn_filters_entries(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [
            _overview(451, subject="[PATCH] docs/zh_CN: fix"),
            _overview(452, subject="[PATCH] fix driver bug"),
            _overview(453, subject="docs/zh_CN: update translation"),
        ])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 450})
        match_fn = lambda e: "zh_cn" in e.subject.lower()
        entries, _ = fetcher._fetch_subsystem("linux-doc", match_fn=match_fn)
        assert len(entries) == 2
        assert all("zh_CN" in e.subject for e in entries)

    @patch("lkml_feed_api.feed.NNTP")
    def test_cursor_advances_after_fetch(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [_overview(451)])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 450})
        fetcher._fetch_subsystem("linux-doc")
        assert fetcher._cursors["org.kernel.vger.linux-doc"] == 500

    @patch("lkml_feed_api.feed.NNTP")
    def test_caps_at_max_articles(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 2000 1 2000 g", 2000, 1, 2000, "g")
        mock_conn.over.return_value = ("224 ok", [])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 0})
        _, caught_up = fetcher._fetch_subsystem("linux-doc")
        assert caught_up is False
        assert fetcher._cursors["org.kernel.vger.linux-doc"] == _MAX_ARTICLES_PER_FETCH

    @patch("lkml_feed_api.feed.NNTP")
    def test_body_failure_still_returns_entry(self, MockNNTP, tmp_path):
        """If body_many returns None for an article, entry still returned with empty summary."""
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [_overview(451)])
        mock_conn.body_many.side_effect = lambda nums: [(n, None) for n in nums]
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 450})
        entries, _ = fetcher._fetch_subsystem("linux-doc")
        assert len(entries) == 1
        assert entries[0].summary == ""

    @patch("lkml_feed_api.feed.NNTP")
    def test_body_value_error_handled(self, MockNNTP, tmp_path):
        """Pipeline error should not crash; entries returned with empty summary."""
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [_overview(451)])
        mock_conn.body_many.side_effect = ValueError("PyMemoryView buf NULL")
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 450})
        entries, _ = fetcher._fetch_subsystem("linux-doc")
        assert len(entries) == 1

    @patch("lkml_feed_api.feed.time.sleep")
    @patch("lkml_feed_api.feed.NNTP")
    def test_group_failure_returns_empty(self, MockNNTP, mock_sleep, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.side_effect = NNTPError("server error")
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path)
        entries, caught_up = fetcher._fetch_subsystem("linux-doc")
        assert entries == []
        assert caught_up is True

    @patch("lkml_feed_api.feed.time.sleep")
    @patch("lkml_feed_api.feed.NNTP")
    def test_over_failure_returns_empty(self, MockNNTP, mock_sleep, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.side_effect = NNTPError("over failed")
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 450})
        entries, caught_up = fetcher._fetch_subsystem("linux-doc")
        assert entries == []
        assert caught_up is True


# ---------------------------------------------------------------------------
# fetch_latest (multi-subsystem)
# ---------------------------------------------------------------------------


class TestFetchLatest:
    @patch("lkml_feed_api.feed.NNTP")
    def test_multiple_subsystems(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [_overview(451)])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={
            "org.kernel.vger.linux-doc": 450,
            "org.kernel.vger.linux-kernel": 450,
        })
        result = fetcher.fetch_latest(["linux-doc", "linux-kernel"])
        assert len(result.entries) == 2

    @patch("lkml_feed_api.feed.NNTP")
    def test_caught_up_flag(self, MockNNTP, tmp_path):
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 500})
        result = fetcher.fetch_latest(["linux-doc"])
        assert result.is_caught_up is True


# ---------------------------------------------------------------------------
# Thread safety — conn_lock
# ---------------------------------------------------------------------------


class TestThreadSafety:
    @patch("lkml_feed_api.feed.NNTP")
    def test_concurrent_fetch_serialized_by_lock(self, MockNNTP, tmp_path):
        """Concurrent requests should be serialized, not interleaved."""
        call_order = []

        def slow_group(name):
            call_order.append(("group_start", threading.current_thread().name))
            time.sleep(0.05)
            call_order.append(("group_end", threading.current_thread().name))
            return ("211 100 1 500 g", 100, 1, 500, "g")

        mock_conn = _mock_nntp()
        mock_conn.group.side_effect = slow_group
        mock_conn.over.return_value = ("224 ok", [])
        MockNNTP.return_value = mock_conn

        fetcher = _make_fetcher(tmp_path, cursors={"org.kernel.vger.linux-doc": 500})

        threads = []
        for i in range(3):
            t = threading.Thread(
                target=fetcher.fetch_latest,
                args=(["linux-doc"],),
                name=f"worker-{i}",
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # With the lock, group calls should not interleave:
        # each group_start should be immediately followed by its group_end
        group_events = [(ev, name) for ev, name in call_order if ev.startswith("group")]
        for i in range(0, len(group_events), 2):
            if i + 1 < len(group_events):
                assert group_events[i][0] == "group_start"
                assert group_events[i + 1][0] == "group_end"
                assert group_events[i][1] == group_events[i + 1][1]

    @patch("lkml_feed_api.feed.NNTP")
    def test_concurrent_rewind_and_fetch(self, MockNNTP, tmp_path):
        """Rewind and fetch should not corrupt cursor state."""
        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [])
        MockNNTP.return_value = mock_conn

        group_name = "org.kernel.vger.linux-doc"
        fetcher = _make_fetcher(tmp_path, cursors={group_name: 500})

        errors = []

        def do_rewind():
            try:
                fetcher.rewind(group_name, 100)
            except Exception as e:
                errors.append(e)

        def do_fetch():
            try:
                fetcher.fetch_latest(["linux-doc"])
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=do_rewind),
            threading.Thread(target=do_fetch),
            threading.Thread(target=do_rewind),
            threading.Thread(target=do_fetch),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"
        # Cursor should be a valid integer
        assert isinstance(fetcher._cursors[group_name], int)


# ---------------------------------------------------------------------------
# SDK (LKMLFeedClient)
# ---------------------------------------------------------------------------


class TestSDK:
    @patch("lkml_feed_api.feed.NNTP")
    def test_keyword_matching_case_insensitive(self, MockNNTP, tmp_path):
        from lkml_feed_api.sdk import LKMLFeedClient

        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [
            _overview(451, subject="[PATCH] docs/zh_CN: fix typo"),
            _overview(452, subject="[PATCH] fix driver crash"),
        ])
        MockNNTP.return_value = mock_conn

        client = LKMLFeedClient(
            ["linux-doc"],
            keywords=["zh_cn"],
            state_file=str(tmp_path / "state.json"),
        )
        # Set cursor so it fetches
        client._fetcher._cursors["org.kernel.vger.linux-doc"] = 450
        result = client.get_latest()
        assert len(result.entries) == 1
        assert "zh_CN" in result.entries[0].subject

    @patch("lkml_feed_api.feed.NNTP")
    def test_no_keywords_returns_all(self, MockNNTP, tmp_path):
        from lkml_feed_api.sdk import LKMLFeedClient

        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        mock_conn.over.return_value = ("224 ok", [
            _overview(451, subject="Article A"),
            _overview(452, subject="Article B"),
        ])
        MockNNTP.return_value = mock_conn

        client = LKMLFeedClient(
            ["linux-doc"],
            keywords=None,
            state_file=str(tmp_path / "state.json"),
        )
        client._fetcher._cursors["org.kernel.vger.linux-doc"] = 450
        result = client.get_latest()
        assert len(result.entries) == 2

    @patch("lkml_feed_api.feed.NNTP")
    def test_rewind_adjusts_all_subsystems(self, MockNNTP, tmp_path):
        from lkml_feed_api.sdk import LKMLFeedClient

        mock_conn = _mock_nntp()
        mock_conn.group.return_value = ("211 100 1 500 g", 100, 1, 500, "g")
        MockNNTP.return_value = mock_conn

        client = LKMLFeedClient(
            ["linux-doc", "linux-kernel"],
            state_file=str(tmp_path / "state.json"),
        )
        client._fetcher._cursors["org.kernel.vger.linux-doc"] = 500
        client._fetcher._cursors["org.kernel.vger.linux-kernel"] = 300
        client.rewind(100)
        assert client._fetcher._cursors["org.kernel.vger.linux-doc"] == 400
        assert client._fetcher._cursors["org.kernel.vger.linux-kernel"] == 200


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


class TestAPI:
    """Test API route functions directly (avoids TestClient version issues)."""

    @patch("lkml_feed_api.app.client")
    def test_ping(self, mock_client):
        from lkml_feed_api.app import ping
        resp = ping()
        assert resp.code == 200
        assert resp.message == "pong"

    @patch("lkml_feed_api.app.client")
    def test_latest(self, mock_client):
        from lkml_feed_api.app import latest
        from lkml_feed_api.models import FetchResult, MailEntry

        mock_client.get_latest.return_value = FetchResult(entries=[
            MailEntry(
                subject="test", author="Alice", received_at="2026-01-01T00:00:00",
                summary="body", subsystem="linux-doc",
            )
        ], is_caught_up=True)

        resp = latest()
        assert resp.code == 200
        assert len(resp.data["entries"]) == 1
        assert resp.data["entries"][0]["subject"] == "test"

    @patch("lkml_feed_api.app.client")
    def test_rewind(self, mock_client):
        from lkml_feed_api.app import rewind
        from lkml_feed_api.models import FetchResult

        mock_client.get_latest.return_value = FetchResult(entries=[], is_caught_up=True)

        resp = rewind(n=100)
        assert resp.code == 200
        mock_client.rewind.assert_called_once_with(100)
        assert "rewound" in resp.message


    @patch("lkml_feed_api.app.client")
    def test_reset(self, mock_client):
        from lkml_feed_api.app import reset
        resp = reset()
        assert resp.code == 200
        mock_client.reset.assert_called_once()
