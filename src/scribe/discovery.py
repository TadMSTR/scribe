"""Find sessions on the filesystem and decide which are finished.

**v1 discovers from the filesystem, not from a registry.** `agent-postgres` has the right
schema for one and is not one: 45 rows across 2 agents, every row `status='active'` because
nothing ever closes them, and interactive CLI sessions — which produce the transcripts being
summarized — are never registered at all. Wiring it in would mean depending on a table that
does not know about most of the work.

**Completion is inferred, not signalled.** The Stop hook fires per *turn*, so there is no
session-end event to wait for. A transcript is treated as complete once it has been idle
longer than the quiet period. That is a judgement and it can be wrong in one direction:
Claude Code appends to an existing transcript when a session resumes, so a session declared
complete can come back. `SessionRow.has_unread_bytes` is what makes that recoverable — see
`state.py`.
"""

from __future__ import annotations

import glob
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .eventlog import session_eventlog
from .state import STATUS_ACTIVE, STATUS_COMPLETE, STATUS_PROVISIONAL, SessionRow, Store


@dataclass(frozen=True)
class Discovered:
    """A transcript on disk, with the facts needed to judge whether it is finished."""

    path: Path
    size_bytes: int
    mtime_ns: int
    idle_seconds: float

    @property
    def agent(self) -> str:
        from .extract.parser import agent_for_path

        return agent_for_path(self.path.parent.name)


def iter_transcripts(
    project_globs: tuple[str, ...],
    exclude: tuple[str, ...] = (),
    *,
    now: float | None = None,
) -> Iterator[Discovered]:
    """Yield every `*.jsonl` transcript under `project_globs`.

    `now` is injectable so idle time can be tested without sleeping. Defaulting it to
    `time.time()` at call time rather than at import is deliberate: a module-level default
    would freeze the clock for the life of the process.
    """
    clock = time.time() if now is None else now
    seen: set[Path] = set()
    for pattern in project_globs:
        base = os.path.expanduser(pattern)
        for hit in glob.glob(os.path.join(base, "*.jsonl")):
            p = Path(hit)
            if p in seen:
                continue
            if any(part in exclude for part in p.parts):
                continue
            try:
                st = p.stat()
            except OSError:
                # Vanished between glob and stat, or unreadable. Skipping is correct: it is
                # not a session we can process, and there is nothing to record about it.
                continue
            if not p.is_file():
                continue
            seen.add(p)
            yield Discovered(
                path=p,
                size_bytes=st.st_size,
                mtime_ns=st.st_mtime_ns,
                idle_seconds=max(0.0, clock - st.st_mtime),
            )


def scan(cfg: Config, store: Store, *, now: float | None = None) -> list[SessionRow]:
    """Observe every transcript, update state, and return those ready to summarize.

    A session is ready when it has been idle past the quiet period **and** either has bytes
    that have not been read yet, or holds a stand-in block with retry budget left.

    The unread-bytes half is what stops a completed session from being re-summarized on every
    scan for the rest of its life — without it, "idle for 15 minutes" is permanently true of
    every transcript that will never be touched again, which is most of them.

    The provisional half is what makes a lost digest recoverable (vikunja#872, #868). Note
    that readiness has never consulted `status`, and still does not: a `failed` session was
    re-offered by the byte check alone, which is why one reached 12 summarization calls. The
    new branch is not "also retry failures" — it is a *bounded* offer for the sessions whose
    block on disk is admittedly not an answer. See `SessionRow.is_retryable_provisional`.
    """
    quiet_seconds = cfg.quiet_period_minutes * 60
    ready: list[SessionRow] = []
    for found in iter_transcripts(cfg.project_globs, cfg.exclude, now=now):
        row = store.upsert_observed(
            str(found.path),
            size_bytes=found.size_bytes,
            mtime_ns=found.mtime_ns,
            agent=found.agent,
        )
        if found.idle_seconds < quiet_seconds:
            if row.status != STATUS_ACTIVE:
                store.set_status(str(found.path), STATUS_ACTIVE)
            continue
        if not (row.has_unread_bytes or row.is_retryable_provisional):
            continue
        if row.status != STATUS_COMPLETE:
            store.set_status(str(found.path), STATUS_COMPLETE)
            row.status = STATUS_COMPLETE
        ready.append(row)
    return ready


def orphaned(cfg: Config, store: Store) -> list[SessionRow]:
    """Sessions whose transcript is gone but whose event log survives (vikunja#873).

    `scan` cannot reach these: it discovers from the filesystem, and the file it discovers by
    no longer exists. Transcripts age out at 30 days (`cleanupPeriodDays` is unset, #778)
    while `eventlogs/` has no cleanup policy at all, so this is the set that grows.

    **Replay is opt-in, and the gate is the status.** Only rows a deliberate
    `scribe recover --apply` reopened are offered -- `reset_for_retry` is what moves a row out
    of `summarized`, and it is only called for a session whose block on disk is admittedly a
    stand-in. That matters more than it looks: 18 event logs currently have no transcript and
    **16 of them already hold real digests**. A rule phrased as "replay every orphaned log"
    would re-summarize those sixteen and overwrite good digests with a replay -- the one way
    this feature can destroy something. Requiring the reopen means an untouched session stays
    untouched no matter how long its transcript has been gone.
    """
    ready: list[SessionRow] = []
    for status in (STATUS_COMPLETE, STATUS_PROVISIONAL):
        for row in store.list_by_status(status):
            if Path(row.transcript_path).expanduser().exists():
                continue
            if not (row.has_unread_bytes or row.is_retryable_provisional):
                continue
            if session_eventlog(cfg.eventlog_dir, row.session_id, row.transcript_path).is_file():
                ready.append(row)
    return ready
