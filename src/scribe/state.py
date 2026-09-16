"""Durable per-session state, in SQLite.

Replaces the spool-JSON-per-turn model. Two things it has to get right:

**The idempotency key is the turn uuid.** The pipeline being replaced keyed on
`(session_id, HH:MM)`, which collides whenever two turns start inside the same minute — 62
times across 3,539 blocks (vikunja#844). A uuid does not have a granularity to be wrong at.

**A "complete" session can come back.** Claude Code appends to an existing transcript when a
session is resumed, so completion is a judgement from an idle timer, not a fact. State
therefore records how far the file was read (`last_offset`) rather than only that it was
handled, and a file that has grown past its recorded offset is live again regardless of what
its status says.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .paths import secure_dir, secure_sqlite

SCHEMA_VERSION = 1

STATUS_ACTIVE = "active"
STATUS_COMPLETE = "complete"
STATUS_SUMMARIZED = "summarized"
STATUS_FAILED = "failed"

#: Written, but not with a digest. The block on disk is `render_failure`'s marked placeholder
#: or the contamination guard's suppression note, and the real summary does not exist yet.
#:
#: This status is the state-side half of making a loss recoverable (vikunja#872, #868). What
#: it replaces is worse than "terminal": a suppressed session went straight to `summarized`,
#: and a placeholder set `failed` but left `last_offset` untouched, so it was re-offered on
#: *every* sweep for the rest of its life — one session reached 12 attempts, each a ~50k-token
#: call, and could never have succeeded because `append_block` would have refused the result.
#: Neither "done" nor "retry forever" was right. `provisional` means retry, a bounded number
#: of times, and say so in the totals meanwhile.
STATUS_PROVISIONAL = "provisional"

#: How many sweeps may retry a provisional session before it is left alone.
#:
#: Three, because the failures that produce a provisional block are overwhelmingly determined
#: by the input rather than by the sample — a cap violation sees the same log every time, and
#: the runner has already spent its own three in-call attempts on a contamination rejection.
#: An unbounded retry is not free: it is a full summarization call per sweep, forever, on a
#: session that by construction keeps failing.
#:
#: Exhausting the budget is not a terminal verdict on the session, only on the current code.
#: `scribe recover` resets the counter, which is how a session becomes retryable again after
#: the thing that was failing has been fixed — the case this whole build exists for.
MAX_PROVISIONAL_ATTEMPTS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    transcript_path TEXT PRIMARY KEY,
    -- Written when a session is summarized, not when it is observed: `scan` upserts a row
    -- from a filesystem stat, and the session id is inside the transcript. It was empty on
    -- all 441 rows until vikunja#872, because nothing ever supplied it after extraction --
    -- an always-empty column that reads like a usable key. `transcript_path` is the primary
    -- key and the join key; this is for correlating a row with a digest anchor.
    session_id      TEXT,
    agent           TEXT,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    mtime_ns        INTEGER NOT NULL DEFAULT 0,
    last_offset     INTEGER NOT NULL DEFAULT 0,
    last_turn_uuid  TEXT,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status);

-- One row per turn that has made it into a written digest. The PRIMARY KEY is the
-- idempotency guarantee: writing the same turn twice is a no-op, not a duplicate block.
CREATE TABLE IF NOT EXISTS processed_turns (
    turn_uuid       TEXT PRIMARY KEY,
    transcript_path TEXT NOT NULL,
    written_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS processed_turns_path ON processed_turns(transcript_path);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class SessionRow:
    transcript_path: str
    session_id: str = ""
    agent: str = ""
    size_bytes: int = 0
    mtime_ns: int = 0
    last_offset: int = 0
    last_turn_uuid: str = ""
    status: str = STATUS_ACTIVE
    attempts: int = 0
    last_error: str = ""
    first_seen: str = ""
    updated_at: str = ""

    @property
    def has_unread_bytes(self) -> bool:
        """True when the file has grown past what was last read.

        This is what makes a resumed session visible. A status of `summarized` says only
        that the bytes read *at the time* were summarized.
        """
        return self.size_bytes > self.last_offset

    @property
    def is_retryable_provisional(self) -> bool:
        """True when the block on disk is a stand-in and the retry budget is not spent.

        Deliberately independent of `has_unread_bytes`: a provisional session has had its
        offset advanced precisely so it stops being re-offered by the byte check, and this is
        what offers it instead — a bounded number of times rather than every sweep.
        """
        return self.status == STATUS_PROVISIONAL and self.attempts < MAX_PROVISIONAL_ATTEMPTS


class Store:
    """SQLite-backed session state. Safe to open concurrently; writes are short."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        # Owner-only, both layers. The store holds session_id values, which are functionally
        # credentials when paired with `claude -p --resume`; sqlite3 would otherwise create
        # the file 0644 from the process umask. See paths.py.
        secure_dir(self.path.parent)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        secure_sqlite(self.path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL so a long read cannot block the writer. Set per-connection because it is a
        # database-level setting that only needs to succeed once, and a read-only volume
        # would otherwise make opening the store fail rather than degrade.
        with closing(conn):
            # pragma may fail on an exotic filesystem; WAL is an optimisation, not a
            # correctness requirement, so a failure here must not stop the store opening.
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode = WAL")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @property
    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def upsert_observed(
        self,
        transcript_path: str,
        *,
        size_bytes: int,
        mtime_ns: int,
        session_id: str = "",
        agent: str = "",
    ) -> SessionRow:
        """Record that a transcript exists with the given size and mtime.

        Never regresses `last_offset` or clears `last_turn_uuid` — an observation says what
        is on disk, not what has been processed. Conflating the two is how a rescan would
        silently re-summarize a session that was already done.
        """
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (transcript_path, session_id, agent, size_bytes,
                                      mtime_ns, status, first_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transcript_path) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    mtime_ns   = excluded.mtime_ns,
                    session_id = COALESCE(NULLIF(excluded.session_id, ''), sessions.session_id),
                    agent      = COALESCE(NULLIF(excluded.agent, ''), sessions.agent),
                    updated_at = excluded.updated_at
                """,
                (transcript_path, session_id, agent, size_bytes, mtime_ns, STATUS_ACTIVE, now, now),
            )
        row = self.get(transcript_path)
        assert row is not None
        return row

    def get(self, transcript_path: str) -> SessionRow | None:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM sessions WHERE transcript_path = ?", (transcript_path,)
            ).fetchone()
        return _row(r) if r else None

    def list_by_status(self, status: str) -> list[SessionRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at", (status,)
            ).fetchall()
        return [_row(r) for r in rows]

    def set_status(self, transcript_path: str, status: str, *, error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, last_error = ?, updated_at = ? "
                "WHERE transcript_path = ?",
                (status, error, _now(), transcript_path),
            )

    def record_attempt(self, transcript_path: str, *, error: str = "") -> int:
        """Increment the attempt counter and return the new value."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET attempts = attempts + 1, last_error = ?, updated_at = ? "
                "WHERE transcript_path = ?",
                (error, _now(), transcript_path),
            )
            r = conn.execute(
                "SELECT attempts FROM sessions WHERE transcript_path = ?", (transcript_path,)
            ).fetchone()
        return int(r["attempts"]) if r else 0

    def mark_summarized(
        self,
        transcript_path: str,
        *,
        offset: int,
        last_turn_uuid: str,
        turn_uuids: list[str],
        session_id: str = "",
    ) -> int:
        """Advance the read offset and record which turns were written.

        Returns the number of turns newly recorded. A turn already present is skipped rather
        than replaced — that is the idempotency guarantee, and it is enforced by the primary
        key rather than by a check the caller has to remember.
        """
        now = _now()
        with self._connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO processed_turns (turn_uuid, transcript_path, written_at) "
                "VALUES (?, ?, ?)",
                [(u, transcript_path, now) for u in turn_uuids],
            )
            inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.execute(
                "UPDATE sessions SET last_offset = MAX(last_offset, ?), last_turn_uuid = ?, "
                "status = ?, last_error = '', "
                "session_id = COALESCE(NULLIF(?, ''), session_id), updated_at = ? "
                "WHERE transcript_path = ?",
                (offset, last_turn_uuid, STATUS_SUMMARIZED, session_id, now, transcript_path),
            )
        return inserted

    def mark_provisional(
        self,
        transcript_path: str,
        *,
        offset: int,
        last_turn_uuid: str,
        session_id: str = "",
        error: str = "",
    ) -> int:
        """Record that a stand-in was written, and spend one of the retry budget.

        The two halves are chosen against opposite failure modes:

          * `last_offset` **is** advanced, so the byte check stops re-offering the session on
            every sweep. That is what stops the 12-attempt token burn.
          * `processed_turns` is **not** written, so the turn is still pending and the next
            sweep genuinely re-summarizes it rather than short-circuiting on "nothing new".

        Returns the new attempt count so the caller can report it.
        """
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_offset = MAX(last_offset, ?), last_turn_uuid = ?, "
                "status = ?, attempts = attempts + 1, last_error = ?, "
                "session_id = COALESCE(NULLIF(?, ''), session_id), updated_at = ? "
                "WHERE transcript_path = ?",
                (
                    offset,
                    last_turn_uuid,
                    STATUS_PROVISIONAL,
                    error,
                    session_id,
                    now,
                    transcript_path,
                ),
            )
            r = conn.execute(
                "SELECT attempts FROM sessions WHERE transcript_path = ?", (transcript_path,)
            ).fetchone()
        return int(r["attempts"]) if r else 0

    def reset_for_retry(self, transcript_path: str) -> bool:
        """Make a session summarizable again: clear its turns, offset and attempt budget.

        The deliberate counterpart to `mark_provisional`, and the only thing that reopens a
        session whose budget is spent. Used by `scribe recover` once the defect that was
        failing has been fixed — without it, "retry a bounded number of times" would mean a
        session lost to a bug stays lost after the bug is gone.

        Returns False for a transcript the store has never seen.
        """
        with self._connect() as conn:
            r = conn.execute(
                "SELECT 1 FROM sessions WHERE transcript_path = ?", (transcript_path,)
            ).fetchone()
            if r is None:
                return False
            conn.execute(
                "DELETE FROM processed_turns WHERE transcript_path = ?", (transcript_path,)
            )
            conn.execute(
                "UPDATE sessions SET status = ?, last_offset = 0, attempts = 0, "
                "last_error = '', updated_at = ? WHERE transcript_path = ?",
                (STATUS_COMPLETE, _now(), transcript_path),
            )
        return True

    def unprocessed_turns(self, transcript_path: str, turn_uuids: list[str]) -> list[str]:
        """Filter `turn_uuids` down to those not already written, preserving order."""
        if not turn_uuids:
            return []
        with self._connect() as conn:
            # S608: `placeholders` is a run of `?` marks derived only from the LENGTH of
            # the input; every value is still bound as a parameter. There is no way to
            # express a variable-length IN clause otherwise.
            placeholders = ",".join("?" * len(turn_uuids))
            query = (
                "SELECT turn_uuid FROM processed_turns "  # noqa: S608
                f"WHERE turn_uuid IN ({placeholders})"
            )
            seen = {r["turn_uuid"] for r in conn.execute(query, turn_uuids).fetchall()}
        return [u for u in turn_uuids if u not in seen]

    def forget(self, transcript_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM processed_turns WHERE transcript_path = ?", (transcript_path,)
            )
            conn.execute("DELETE FROM sessions WHERE transcript_path = ?", (transcript_path,))


def _row(r: sqlite3.Row) -> SessionRow:
    return SessionRow(
        transcript_path=r["transcript_path"],
        session_id=r["session_id"] or "",
        agent=r["agent"] or "",
        size_bytes=r["size_bytes"],
        mtime_ns=r["mtime_ns"],
        last_offset=r["last_offset"],
        last_turn_uuid=r["last_turn_uuid"] or "",
        status=r["status"],
        attempts=r["attempts"],
        last_error=r["last_error"] or "",
        first_seen=r["first_seen"],
        updated_at=r["updated_at"],
    )
