import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterable, Mapping
from urllib.parse import quote

from .models import (
    JobStatus,
    ListingJob,
    SeenState,
    TERMINAL_JOB_STATUSES,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    search_key TEXT PRIMARY KEY,
    initialized INTEGER NOT NULL DEFAULT 0 CHECK (initialized IN (0, 1)),
    created_at REAL NOT NULL,
    initialized_at REAL
);

CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('baseline', 'queued', 'claimed', 'notified', 'rejected', 'failed')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    claimed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS listings_queue_idx
    ON listings (status, created_at, listing_id);
"""


def _validate_search_key(search_key_value):
    if not isinstance(search_key_value, str) or not search_key_value:
        raise ValueError("search key must be a non-empty string")
    return search_key_value


def _validated_payload(payload):
    if not isinstance(payload, Mapping):
        raise TypeError("listing payload must be a mapping")

    listing_id = payload.get("id")
    if not isinstance(listing_id, str) or not listing_id:
        raise ValueError("listing payload id must be a non-empty string")

    # Store a plain dictionary so callers cannot mutate a job after enqueueing.
    normalized = dict(payload)
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError("listing payload must be JSON serializable") from error
    return listing_id, encoded


class StateStore:
    """SQLite-backed search state and durable notification queue.

    Each process should create its own ``StateStore`` for the same database
    path.  SQLite transactions make baseline initialization, enqueueing, and
    claiming safe when the poller and worker are active at the same time.

    ``recover_claimed_jobs`` is intentionally explicit.  Calling it from a
    poller's constructor would reset a job that an active worker currently
    owns, so the worker should call it once during its startup sequence.
    """

    def __init__(self, path, *, read_only=False):
        self.path = os.fsdecode(os.fspath(path))
        self.read_only = read_only
        if read_only and self.path == ":memory:":
            raise ValueError("read-only mode requires a database file")

        connection_path = self.path
        connection_options = {}
        if read_only:
            # SQLite's URI mode=ro opens an existing database without creating
            # a missing file. Quoting the path also handles spaces and URI
            # punctuation in temporary or deployment directories.
            connection_path = (
                "file:"
                + quote(os.path.abspath(self.path), safe="/")
                + "?mode=ro"
            )
            connection_options["uri"] = True

        self.connection = sqlite3.connect(
            connection_path,
            timeout=30,
            isolation_level=None,
            **connection_options,
        )
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 30000")
            if self.path != ":memory:":
                self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.executescript(_SCHEMA)

    def close(self):
        """Close this process's database connection."""

        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.close()

    def _begin(self):
        if self.connection is None:
            raise RuntimeError("state store is closed")
        if self.read_only:
            raise sqlite3.OperationalError("state store is read-only")
        self.connection.execute("BEGIN IMMEDIATE")

    def _rollback(self):
        if self.connection is not None and self.connection.in_transaction:
            self.connection.rollback()

    @staticmethod
    def _job_from_row(row):
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                f"stored listing payload is malformed: {row['listing_id']}"
            ) from error
        return ListingJob(
            listing_id=row["listing_id"],
            payload=payload,
            status=JobStatus(row["status"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            claimed_at=row["claimed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def is_search_initialized(self, search_key_value):
        """Return whether a search has completed its first-poll baseline."""

        _validate_search_key(search_key_value)
        row = self.connection.execute(
            "SELECT initialized FROM searches WHERE search_key = ?",
            (search_key_value,),
        ).fetchone()
        return bool(row and row["initialized"])

    def initialize_search(self, search_key_value, listings: Iterable[Mapping]):
        """Atomically baseline ``listings`` and mark a search initialized.

        The return value is the number of new globally tracked listings, or
        ``None`` when another poll already initialized this search.  Baseline
        rows are terminal and are never queued for notification.  A listing
        already present globally (for example, queued from another search)
        keeps its existing state.
        """

        _validate_search_key(search_key_value)
        payloads = [_validated_payload(payload) for payload in listings]
        now = time.time()
        self._begin()
        try:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO searches
                    (search_key, initialized, created_at)
                VALUES (?, 0, ?)
                """,
                (search_key_value, now),
            )
            row = self.connection.execute(
                "SELECT initialized FROM searches WHERE search_key = ?",
                (search_key_value,),
            ).fetchone()
            if row["initialized"]:
                self.connection.rollback()
                return None

            inserted = 0
            for listing_id, encoded in payloads:
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO listings
                        (listing_id, payload, status, attempts, created_at,
                         updated_at)
                    VALUES (?, ?, 'baseline', 0, ?, ?)
                    """,
                    (listing_id, encoded, now, now),
                )
                inserted += cursor.rowcount

            self.connection.execute(
                """
                UPDATE searches
                SET initialized = 1, initialized_at = ?
                WHERE search_key = ?
                """,
                (now, search_key_value),
            )
            self.connection.commit()
            return inserted
        except BaseException:
            self._rollback()
            raise

    # This name makes the operation discoverable for callers that think in
    # terms of recording a baseline rather than initializing a search.
    record_baseline = initialize_search

    def enqueue_listing(self, payload: Mapping):
        """Atomically add a new listing to the queue.

        Returns ``True`` only when this call inserted the globally unique
        listing ID.  Existing baseline, queued, claimed, and terminal rows
        are left untouched, preventing duplicate notifications.
        """

        listing_id, encoded = _validated_payload(payload)
        now = time.time()
        self._begin()
        try:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO listings
                    (listing_id, payload, status, attempts, created_at,
                     updated_at)
                VALUES (?, ?, 'queued', 0, ?, ?)
                """,
                (listing_id, encoded, now, now),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    def claim_next(self):
        """Claim the oldest queued listing, incrementing its attempt count."""

        now = time.time()
        self._begin()
        try:
            row = self.connection.execute(
                """
                SELECT * FROM listings
                WHERE status = 'queued'
                ORDER BY created_at, listing_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None

            self.connection.execute(
                """
                UPDATE listings
                SET status = 'claimed', attempts = attempts + 1,
                    claimed_at = ?, updated_at = ?
                WHERE listing_id = ? AND status = 'queued'
                """,
                (now, now, row["listing_id"]),
            )
            claimed = self.connection.execute(
                "SELECT * FROM listings WHERE listing_id = ?",
                (row["listing_id"],),
            ).fetchone()
            self.connection.commit()
            return self._job_from_row(claimed)
        except BaseException:
            self._rollback()
            raise

    def get_listing(self, listing_id):
        """Return a listing job by ID, or ``None`` if it is unknown."""

        if not isinstance(listing_id, str) or not listing_id:
            raise ValueError("listing ID must be a non-empty string")
        row = self.connection.execute(
            "SELECT * FROM listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return self._job_from_row(row)

    def is_listing_known(self, listing_id):
        """Return whether SQLite already contains a listing ID.

        This is a read-only query and is safe to use with a read-only store.
        """

        if not isinstance(listing_id, str) or not listing_id:
            raise ValueError("listing ID must be a non-empty string")
        row = self.connection.execute(
            "SELECT 1 FROM listings WHERE listing_id = ? LIMIT 1",
            (listing_id,),
        ).fetchone()
        return row is not None

    def is_listing_terminal(self, listing_id):
        job = self.get_listing(listing_id)
        return job is not None and job.status in TERMINAL_JOB_STATUSES

    def _require_claimed(self, listing_id):
        row = self.connection.execute(
            "SELECT status FROM listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown listing: {listing_id}")
        if row["status"] != JobStatus.CLAIMED.value:
            raise ValueError(f"listing {listing_id} must be claimed")

    def _mark_terminal(self, listing_id, status):
        if not isinstance(listing_id, str) or not listing_id:
            raise ValueError("listing ID must be a non-empty string")
        now = time.time()
        self._begin()
        try:
            self._require_claimed(listing_id)
            self.connection.execute(
                """
                UPDATE listings
                SET status = ?, claimed_at = NULL, last_error = NULL,
                    updated_at = ?
                WHERE listing_id = ?
                """,
                (status.value, now, listing_id),
            )
            self.connection.commit()
        except BaseException:
            self._rollback()
            raise

    def mark_notified(self, listing_id):
        """Mark a claimed listing as successfully delivered."""

        self._mark_terminal(listing_id, JobStatus.NOTIFIED)

    def mark_rejected(self, listing_id):
        """Mark a claimed listing as rejected by the LLM."""

        self._mark_terminal(listing_id, JobStatus.REJECTED)

    def fail_or_retry(self, listing_id, error, max_retries=3):
        """Record a failed attempt and requeue or permanently fail the job.

        ``max_retries`` counts retries after the first claim.  Therefore its
        default of three permits four total attempts, matching the worker
        policy agreed for OpenRouter failures.
        """

        if not isinstance(max_retries, int) or isinstance(max_retries, bool):
            raise TypeError("max_retries must be an integer")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if error is not None and not isinstance(error, str):
            error = str(error)
        if not isinstance(listing_id, str) or not listing_id:
            raise ValueError("listing ID must be a non-empty string")

        now = time.time()
        self._begin()
        try:
            row = self.connection.execute(
                "SELECT status, attempts FROM listings WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown listing: {listing_id}")
            if row["status"] != JobStatus.CLAIMED.value:
                raise ValueError(f"listing {listing_id} must be claimed")

            status = (
                JobStatus.FAILED
                if row["attempts"] > max_retries
                else JobStatus.QUEUED
            )
            self.connection.execute(
                """
                UPDATE listings
                SET status = ?, claimed_at = NULL, last_error = ?,
                    updated_at = ?
                WHERE listing_id = ?
                """,
                (status.value, error, now, listing_id),
            )
            self.connection.commit()
            return status
        except BaseException:
            self._rollback()
            raise

    def recover_claimed_jobs(self):
        """Requeue all claims left by a worker that stopped or crashed."""

        now = time.time()
        self._begin()
        try:
            cursor = self.connection.execute(
                """
                UPDATE listings
                SET status = 'queued', claimed_at = NULL, updated_at = ?
                WHERE status = 'claimed'
                """,
                (now,),
            )
            self.connection.commit()
            return cursor.rowcount
        except BaseException:
            self._rollback()
            raise

    def pending_count(self):
        """Return queued and claimed jobs that still need worker handling."""

        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM listings
            WHERE status IN ('queued', 'claimed')
            """
        ).fetchone()
        return row["count"]

    def all_terminal(self):
        """Return true when no listing remains queued or claimed."""

        return self.pending_count() == 0

    def failure_boundary(self):
        """Return the number of permanently failed listings currently stored."""

        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM listings WHERE status = 'failed'"
        ).fetchone()
        return row["count"]

    def once_successful(self, failure_boundary=0):
        """Return whether once-mode drained without new failures.

        Baseline, notified, and rejected listings are successful terminal
        outcomes. Queued and claimed listings are still in progress, while a
        failed listing makes the once-mode result unsuccessful even though it
        is terminal for queue draining. ``failure_boundary`` is the count of
        failed listings captured before the once-mode invocation, allowing
        historical failures to remain recorded without making every later
        invocation fail.
        """

        if not isinstance(failure_boundary, int) or isinstance(
            failure_boundary, bool
        ):
            raise TypeError("failure boundary must be an integer")
        if failure_boundary < 0:
            raise ValueError("failure boundary must not be negative")

        row = self.connection.execute(
            """
            SELECT
                SUM(status IN ('queued', 'claimed')) AS pending,
                SUM(status = 'failed') AS failed
            FROM listings
            """
        ).fetchone()
        return row["pending"] in (None, 0) and row["failed"] <= failure_boundary


def search_key(search_url):
    return hashlib.sha256(search_url.encode("utf-8")).hexdigest()


def load_seen(path):
    if not os.path.exists(path):
        return SeenState()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("seen state has an unsupported format")

    listing_ids = data.get("listing_ids")
    initialized_searches = data.get("initialized_searches")
    if not isinstance(listing_ids, list) or not isinstance(
        initialized_searches, list
    ):
        raise ValueError("seen state is malformed")
    if any(not isinstance(value, str) for value in listing_ids):
        raise ValueError("seen state listing IDs must be strings")
    if any(not isinstance(value, str) for value in initialized_searches):
        raise ValueError("seen state search keys must be strings")

    return SeenState(set(listing_ids), set(initialized_searches))


def save_seen(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    fd, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{basename}.",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "listing_ids": sorted(state.listing_ids),
                    "initialized_searches": sorted(
                        state.initialized_searches
                    ),
                },
                f,
                indent=2,
            )
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise
