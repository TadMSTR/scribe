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
from .state import STATUS_ACTIVE, STATUS_COMPLETE, SessionRow, Store


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

    A session is ready when it has been idle past the quiet period **and** has bytes that
    have not been read yet. The second half is what stops a completed session from being
    re-summarized on every scan for the rest of its life — without it, "idle for 15 minutes"
    is permanently true of every transcript that will never be touched again, which is most
    of them.
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
        if not row.has_unread_bytes:
            continue
        if row.status != STATUS_COMPLETE:
            store.set_status(str(found.path), STATUS_COMPLETE)
            row.status = STATUS_COMPLETE
        ready.append(row)
    return ready
