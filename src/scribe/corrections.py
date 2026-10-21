"""The corrections ledger: one JSON line per digest block scribe edited after the model wrote it.

0.13.0 started rewriting id-conflated ticket refs (`#1258` -> `id 1258`) before a block reaches
disk, and `scribe correct` applies the same rewrite to blocks written before that. Either way
the text on disk is no longer what the model produced, and the run record could say only how
many refs moved (`ticket_refs_rewritten`), not which. This file answers "why does this block
differ from the model's output?" for every block, live or retroactive.

**Kept out of the digest on purpose.** The anchor is a parsed format -- `writeback.ANCHOR_RE`
allows one optional field, and the SessionStart preview (invariant 10) and the indexer read
these files -- and agents read the body, where a correction note would be noise in every
session-start injection. A record is keyed by `(digest, turn_uuid)`, which every anchor already
carries, so the trail is one lookup away.

Fields, in order:

    record_version  1. Bumped when a field changes meaning, never when one is added.
    ts              ISO-8601 UTC, seconds
    scribe_version  the version that made the edit
    reason          why. One value today, `id-conflation`; add one only when a second
                    gate-derived correction exists
    source          `live` (the sweep, before the write) or `correct` (after the fact)
    digest          path relative to output_dir, POSIX separators, as `index.jsonl` names it
    session_id      from the block's anchor
    turn_uuid       from the block's anchor
    sha256_before   the block's hash before the edit, as `manifest._record` computes it.
                    **`null` on `live`**: the unrewritten block never existed on disk, so
                    there is no hash anybody could check it against
    sha256_after    the block's hash as written; equal to its `index.jsonl` line's `sha256`.
                    `null` only if the block could not be re-read after the write
    edits           `[{"line", "before", "after"}]`, `line` 1-based within the block (the
                    anchor is line 1)

**Tokens, never lines.** `before`/`after` are the ref tokens (`#1258`, `id 1258`). Digests are
redacted on the way to disk, but a ledger of whole lines would be a second copy of their prose,
and one more place for anything that slipped through to live.

**Never raises**, like `runrecord.append`: a failed write is counted (`correction_errors`) and
the sweep carries on. The ledger observes edits; it must never abort the write it describes.

A **sibling** of `output_dir` by default, and `config.load` refuses a path inside the digest or
event-log tree -- a JSONL file under `output_dir` is one glob away from being indexed as a
digest (invariant 17). Written 0600: it carries `session_id` values.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .paths import secure_append, secure_dir, secure_file

RECORD_VERSION = 1
REASON_ID_CONFLATION = "id-conflation"
REASONS = (REASON_ID_CONFLATION,)
SOURCE_LIVE = "live"
SOURCE_CORRECT = "correct"
SOURCES = (SOURCE_LIVE, SOURCE_CORRECT)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def record(
    *,
    source: str,
    digest: str,
    session_id: str,
    turn_uuid: str,
    edits: list[dict],
    sha256_before: str | None,
    sha256_after: str | None,
    reason: str = REASON_ID_CONFLATION,
) -> dict:
    """One ledger record. Refuses a source or reason this module does not define."""
    if source not in SOURCES:
        raise ValueError(f"unknown correction source {source!r}")
    if reason not in REASONS:
        raise ValueError(f"unknown correction reason {reason!r}")
    return {
        "record_version": RECORD_VERSION,
        "ts": _now(),
        "scribe_version": __version__,
        "reason": reason,
        "source": source,
        "digest": digest,
        "session_id": session_id,
        "turn_uuid": turn_uuid,
        "sha256_before": sha256_before,
        "sha256_after": sha256_after,
        "edits": [{"line": e["line"], "before": e["before"], "after": e["after"]} for e in edits],
    }


def append(path: str | os.PathLike[str], rec: dict) -> str:
    """Append one record. Returns "" on success, else why not. Never raises.

    One write call, then fsync, so a completed append is durable and two appenders cannot
    interleave within a line. 0600 from `O_CREAT` via `secure_append`.
    """
    try:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        p = Path(os.path.expanduser(str(path)))
        if not p.parent.exists():
            secure_dir(p.parent)
        with secure_append(p) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        secure_file(p)
    except (OSError, TypeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""
