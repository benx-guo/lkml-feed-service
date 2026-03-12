"""NNTP fetching + parsing (standalone, no DB)."""

import json
import logging
from ._nntp import NNTP, NNTPError
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .models import FetchResult, MailEntry

logger = logging.getLogger(__name__)

LORE_BASE = "https://lore.kernel.org"
NNTP_HOST = "nntp.lore.kernel.org"
NNTP_PORT = 119

# Maximum articles to fetch per subsystem in a single call
_MAX_ARTICLES_PER_FETCH = 100


class NNTPFetcher:
    def __init__(
        self,
        state_file: Optional[str] = None,
        body_concurrency: int = 1,
    ) -> None:
        self._state_file = Path(state_file) if state_file else None
        self._cursors: Dict[str, int] = {}  # group_name -> last_article_number
        self._conn: Optional[NNTP] = None
        self._conn_lock = threading.Lock()
        self._body_concurrency = max(1, body_concurrency)
        self._local = threading.local()  # thread-local connections for parallel BODY
        self._load_state()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> NNTP:
        """Establish or reuse NNTP connection with retry."""
        if self._conn is not None:
            try:
                self._conn.date()
                return self._conn
            except (NNTPError, OSError, EOFError, ValueError):
                self._close_conn()

        max_attempts = 3
        delay = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                self._conn = NNTP(NNTP_HOST, NNTP_PORT, timeout=30)
                logger.info("Connected to %s:%d", NNTP_HOST, NNTP_PORT)
                return self._conn
            except (NNTPError, OSError) as e:
                logger.warning(
                    "Attempt %d/%d connect to NNTP: %s: %s",
                    attempt,
                    max_attempts,
                    type(e).__name__,
                    e,
                )
                if attempt < max_attempts:
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise

        raise RuntimeError("Failed to connect to NNTP server")  # unreachable

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except (NNTPError, OSError, EOFError, ValueError):
                pass
            self._conn = None

    def close(self) -> None:
        """Close NNTP connection and save state."""
        with self._conn_lock:
            self._close_conn()
            self._save_state()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def reset_cursors(self) -> None:
        """Clear all cursor state."""
        self._cursors.clear()
        self._save_state()

    def rewind(self, group_name: str, n: int) -> None:
        """Rewind cursor by *n* articles."""
        with self._conn_lock:
            cursor = self._cursors.get(group_name)
            if cursor is None:
                last = self._group_with_retry(group_name)
                if last is None:
                    return
                cursor = last
                self._cursors[group_name] = cursor
            self._cursors[group_name] = max(0, cursor - n)
            self._save_state()
            logger.info(
                "Rewound %s by %d: %d -> %d",
                group_name, n, cursor, self._cursors[group_name],
            )

    def fetch_latest(
        self,
        subsystems: List[str],
        *,
        match_fn: Optional[Callable[[MailEntry], bool]] = None,
    ) -> FetchResult:
        """Fetch new messages via NNTP.

        Args:
            subsystems: Subsystem list (e.g. ["linux-doc"]).
            match_fn:   Optional filter function.

        Returns:
            FetchResult with entries and is_caught_up flag.
        """
        all_entries: List[MailEntry] = []
        all_caught_up = True
        for subsystem in subsystems:
            entries, caught_up = self._fetch_subsystem(
                subsystem, match_fn=match_fn
            )
            all_entries.extend(entries)
            if not caught_up:
                all_caught_up = False

        return FetchResult(entries=all_entries, is_caught_up=all_caught_up)

    # ------------------------------------------------------------------
    # Per-subsystem fetch
    # ------------------------------------------------------------------

    def _fetch_subsystem(
        self,
        subsystem: str,
        *,
        match_fn: Optional[Callable[[MailEntry], bool]] = None,
    ) -> Tuple[List[MailEntry], bool]:
        """Return (entries, caught_up). caught_up is True when cursor reached last."""
        group_name = f"org.kernel.vger.{subsystem}"

        with self._conn_lock:
            # GROUP command with retry
            last = self._group_with_retry(group_name)
            if last is None:
                return [], True

            cursor = self._cursors.get(group_name)
            if cursor is None:
                # First run: rewind so the first fetch returns data
                cursor = max(0, last - _MAX_ARTICLES_PER_FETCH)
                self._cursors[group_name] = cursor
                self._save_state()
                logger.info(
                    "Initialized cursor for %s at article %d (rewound from %d)",
                    group_name, cursor, last,
                )

            if cursor >= last:
                return [], True  # No new articles

            # Fetch from cursor+1 to last, capped at _MAX_ARTICLES_PER_FETCH
            start = cursor + 1
            end = min(last, cursor + _MAX_ARTICLES_PER_FETCH)
            caught_up = end >= last

            overviews = self._over_with_retry(group_name, start, end)
            if overviews is None:
                return [], True

            # Filter by match_fn, collect entries that need BODY
            matched: List[Tuple[int, MailEntry]] = []
            for art_num, overview in overviews:
                parsed = self._parse_overview(overview, subsystem)
                if match_fn is not None and not match_fn(parsed):
                    continue
                matched.append((art_num, parsed))

            # Fetch BODY (serial or parallel)
            entries: List[MailEntry] = []
            if matched:
                bodies = self._fetch_bodies(group_name, [a for a, _ in matched])
                for (_, parsed), body in zip(matched, bodies):
                    if body:
                        parsed = parsed.model_copy(update={"summary": body})
                    entries.append(parsed)

            self._cursors[group_name] = end
            self._save_state()
            logger.info(
                "Fetched %d new articles from %s (%d-%d, caught_up=%s)",
                len(entries),
                group_name,
                start,
                end,
                caught_up,
            )
            return entries, caught_up

    # ------------------------------------------------------------------
    # NNTP commands with retry
    # ------------------------------------------------------------------

    def _group_with_retry(self, group_name: str) -> Optional[int]:
        """Send GROUP command and return *last* article number, or None."""
        max_attempts = 3
        delay = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                conn = self._connect()
                _resp, _count, _first, last, _name = conn.group(group_name)
                return last
            except (NNTPError, OSError, EOFError, ValueError) as e:
                logger.warning(
                    "Attempt %d/%d GROUP %s: %s: %s",
                    attempt,
                    max_attempts,
                    group_name,
                    type(e).__name__,
                    e,
                )
                self._close_conn()
                if attempt < max_attempts:
                    time.sleep(delay)
                    delay *= 2
        logger.error(
            "Failed to access group %s after %d attempts",
            group_name,
            max_attempts,
        )
        return None

    def _over_with_retry(
        self, group_name: str, start: int, end: int
    ) -> Optional[List[Tuple[int, Dict[str, str]]]]:
        """Send OVER command and return overview list, or None."""
        max_attempts = 3
        delay = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                conn = self._connect()
                conn.group(group_name)  # must select group before OVER
                _resp, overviews = conn.over((start, end))
                return overviews
            except (NNTPError, OSError, EOFError, ValueError) as e:
                logger.warning(
                    "Attempt %d/%d OVER %d-%d on %s: %s: %s",
                    attempt,
                    max_attempts,
                    start,
                    end,
                    group_name,
                    type(e).__name__,
                    e,
                )
                self._close_conn()
                if attempt < max_attempts:
                    time.sleep(delay)
                    delay *= 2
        logger.error(
            "OVER %d-%d on %s failed after %d attempts",
            start,
            end,
            group_name,
            max_attempts,
        )
        return None

    # ------------------------------------------------------------------
    # OVER parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_overview(overview: Dict[str, str], subsystem: str) -> MailEntry:
        """Parse OVER response fields into MailEntry."""
        # From header
        from_header = overview.get("from", "")
        name, email = parseaddr(from_header)
        if not name:
            name = from_header

        # Subject
        subject = overview.get("subject", "")

        # Date
        date_str = overview.get("date", "")
        try:
            received_at = parsedate_to_datetime(date_str)
        except (ValueError, TypeError):
            received_at = datetime.now(timezone.utc)

        # Message-ID (strip angle brackets)
        raw_mid = overview.get("message-id", "")
        message_id = raw_mid.strip("<>") if raw_mid else None

        # References -> in_reply_to (last token, strip angle brackets)
        references = overview.get("references", "")
        in_reply_to = None
        if references:
            tokens = references.split()
            if tokens:
                in_reply_to = tokens[-1].strip("<>")

        # URL
        url = None
        if message_id:
            url = f"{LORE_BASE}/{subsystem}/{message_id}/"

        return MailEntry(
            subject=subject,
            author=name or "",
            email=email or None,
            url=url,
            message_id=message_id,
            in_reply_to=in_reply_to,
            received_at=received_at.isoformat(),
            summary="",  # populated by BODY if matched
            subsystem=subsystem,
        )

    # ------------------------------------------------------------------
    # BODY fetching
    # ------------------------------------------------------------------

    def _fetch_bodies(
        self, group_name: str, article_nums: List[int]
    ) -> List[Optional[str]]:
        """Fetch multiple article bodies, serial or parallel."""
        if self._body_concurrency <= 1:
            return [self._fetch_body(group_name, n) for n in article_nums]

        def _worker(art_num: int) -> Optional[str]:
            return self._fetch_body_threaded(group_name, art_num)

        with ThreadPoolExecutor(
            max_workers=self._body_concurrency
        ) as pool:
            return list(pool.map(_worker, article_nums))

    def _fetch_body(self, group_name: str, article_num: int) -> Optional[str]:
        """Fetch article body text using main connection."""
        try:
            conn = self._connect()
            conn.group(group_name)
            _resp, info = conn.body(article_num)
            lines = [
                line.decode("utf-8", errors="replace") for line in info.lines
            ]
            return "\n".join(lines)
        except (NNTPError, OSError, EOFError, ValueError) as e:
            logger.warning("Failed to fetch BODY %d: %s", article_num, e)
            self._close_conn()
            return None

    def _fetch_body_threaded(
        self, group_name: str, article_num: int
    ) -> Optional[str]:
        """Fetch article body using thread-local NNTP connection."""
        try:
            conn = self._get_thread_conn(group_name)
            _resp, info = conn.body(article_num)
            lines = [
                line.decode("utf-8", errors="replace") for line in info.lines
            ]
            return "\n".join(lines)
        except (NNTPError, OSError, EOFError, ValueError) as e:
            logger.warning("Failed to fetch BODY %d: %s", article_num, e)
            self._close_thread_conn()
            return None

    def _get_thread_conn(self, group_name: str) -> NNTP:
        """Get or create a thread-local NNTP connection."""
        conn: Optional[NNTP] = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.date()
            except (NNTPError, OSError, EOFError, ValueError):
                self._close_thread_conn()
                conn = None
        if conn is None:
            conn = NNTP(NNTP_HOST, NNTP_PORT, timeout=30)
            self._local.conn = conn
        conn.group(group_name)
        return conn

    def _close_thread_conn(self) -> None:
        conn: Optional[NNTP] = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.quit()
            except (NNTPError, OSError, EOFError, ValueError):
                pass
            self._local.conn = None

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            data: dict = json.loads(self._state_file.read_text("utf-8"))
            for group_name, cursor in data.items():
                if isinstance(cursor, int):
                    self._cursors[group_name] = cursor
                # Skip old format (list of message IDs)
            logger.info(
                "Loaded state for %d group(s) from %s",
                len(self._cursors),
                self._state_file,
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to load state file %s: %s", self._state_file, exc
            )

    def _save_state(self) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._cursors), "utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to save state file %s: %s", self._state_file, exc
            )
