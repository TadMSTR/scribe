"""Persist the extracted event log — the drill-down tier between a digest and the raw JSONL.

Three tiers were assumed and only two existed:

    digest (searchable)  ->  event log (drill-down)  ->  raw JSONL (Backrest)

`pipeline.py` built the middle one in memory, handed it to the summarizer and the QC gate,
and dropped it. Going from a digest line to its evidence therefore meant re-parsing a 2.5 MB
transcript out of a Backrest restore, and a digest could never be re-checked after the fact,
because `scribe qc` needs the event log and nothing kept it.

**The serialized form is `EventLog.to_dict()` verbatim** — byte-for-byte the JSON that
`scribe extract --json` already emits and `scribe qc --events` already consumes. This is
deliberately not a new format: a file written here is a valid input to the gate the moment it
lands, which is the entire reason for keeping it.

**Written before the summarization call.** If the model call fails, this is the only evidence
of what that run saw, and it lets a replay skip re-extraction. Written afterwards it would be
missing in precisely the case it exists for.

**Never indexed.** These are evidence reached *from* a digest, not search targets — 39 MB of
tool arguments would swamp the digest signal in every semantic query. They live in their own
root, a *sibling* of `digests/` and never inside it, so the `session-digests` qmd collection
excludes them by construction rather than by a pattern somebody has to keep correct.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path

from .extract.models import EventLog
from .paths import secure_create, secure_dir

#: A session id usable as a filename verbatim. Deliberately narrower than "no separators":
#: no dots, so a stem can never carry its own extension, and a leading alphanumeric, so
#: neither `.` nor `..` nor a hidden file can be produced.
SAFE_STEM_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

SUFFIX = ".json"


def safe_stem(session_id: str, transcript_path: str = "") -> str:
    """The filename stem for one session: its id where that is safe, else a derived one.

    The session id is read out of the transcript — it is whatever `sessionId` happened to
    say, which makes it untrusted input, and interpolating untrusted input into a path is
    the traversal bug. Anything that is not plainly a bare identifier is therefore
    **replaced rather than sanitised**: scrubbing produces a name that can collide with a
    real session's, and a collision here silently overwrites another session's evidence.

    Falling back rather than refusing is the deliberate half. A transcript with no usable
    id is exactly the malformed case whose evidence is worth most, and deriving the stem
    from the transcript path keeps it stable across runs, so a re-run updates that
    session's file instead of accumulating a new one each sweep.
    """
    sid = (session_id or "").strip()
    if SAFE_STEM_RE.match(sid):
        return sid
    seed = transcript_path or sid
    return "unidentified-" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]


def eventlog_path(root: str | Path, session_id: str, transcript_path: str = "") -> Path:
    """Where one session's event log lives: `<root>/<session-id>.json`.

    Flat and keyed on the session id alone. The digest block's anchor comment carries the
    session id and nothing else, so a flat layout makes the drill-down a *lookup*; date
    partitioning would force a glob over the whole corpus to answer "where is this one".
    At ~475 sessions a year a flat directory is not a problem worth solving.
    """
    return Path(root).expanduser() / (safe_stem(session_id, transcript_path) + SUFFIX)


def write_eventlog(root: str | Path, log: EventLog) -> Path:
    """Write one event log owner-only, replacing any previous one, and return its path.

    Written whole via a temp file and a rename, unlike `writeback.append_block`. That
    module cannot do this — there is no atomic "add to the end of a file", so it settles
    for making a tear *detectable*. Here the unit of writing is one session's complete
    document, so a re-run always has a correct whole to write and there is no reason to
    leave a torn one readable.

    The temp file is created 0600 **by the kernel at O_CREAT**, not chmod'd afterwards
    (`secure_create`). Two reasons, and the second is the one that bites: a chmod-after leaves
    a window where the file is world-readable, and `rename` preserves the *source's*
    permissions — so a 0644 temp file silently downgrades an already-0600 destination.
    Owner-only throughout, because this is derived from a 0600 transcript and is the least
    redacted thing scribe keeps. See `paths.py`.
    """
    path = eventlog_path(root, log.session_id, log.transcript_path)
    secure_dir(path.parent)
    payload = json.dumps(log.to_dict(), ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with secure_create(tmp) as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


def read_eventlog(root: str | Path, session_id: str) -> Path | None:
    """Resolve a session id to its event log, or None when there is none.

    The lookup half of the drill-down. Returns the path rather than the parsed log so a
    caller can hand it straight to `scribe qc --events`, which is the common reason to
    want it.
    """
    path = eventlog_path(root, session_id)
    return path if path.is_file() else None
