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
from collections.abc import Iterator
from dataclasses import MISSING
from pathlib import Path

from .extract.models import EventLog, Rollup, Stats, ToolEvent, Turn
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


def session_eventlog(root: str | Path, session_id: str, transcript_path: str) -> Path:
    """Where the event log for one *state row* lives.

    `eventlog_path` keys on the session id, which is the right key and is exactly what a
    legacy state row does not have -- `scan` upserts a row from a filesystem stat, and the id
    lives inside the transcript. The stem of the transcript path is that same id by
    construction, so it is the fallback rather than a guess.

    Factored out because two callers need the rule and they must not drift: the replay path in
    `pipeline._load_session` and the discovery of orphaned sessions in `discovery.orphaned`.
    One resolving a row to a different file than the other would mean a session that discovery
    offers and the pipeline then cannot load.
    """
    return eventlog_path(root, session_id or Path(transcript_path).stem, transcript_path)


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


def _string_leaves(node: object) -> Iterator[str]:
    """Every string leaf in a decoded event log, depth-first."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _string_leaves(value)
    elif isinstance(node, list):
        for item in node:
            yield from _string_leaves(item)


def contains_value(path: str | Path, value: str) -> bool | None:
    r"""Whether `value` appears in any string leaf of the event log at `path`.

    Returns None when the question cannot be answered — no path, missing file, unreadable,
    or not valid JSON. **None is a third answer, not a False.** The caller states a cause
    based on this, and "the log says no" and "there is no log" support very different
    claims; collapsing them is how vikunja#856 got its confident wrong message in the first
    place.

    **Field-level, walking leaves — never a regex over the serialized file.** That
    distinction is not a style preference, it is the documented trap:
    `Redactor`'s `envvar` rule ends in the negated class `[^\s\"',\]\}]+`, and in the
    serialized form a `"` has become `\"`, so the match runs straight through what was a
    quote boundary and reports "values" of 155-668 characters. Measured across two real
    event logs: field-level scrubbing fired **0** times, whole-file scrubbing fired **7**,
    and all seven were spurious. Anyone auditing `eventlogs/` will reach for `grep` or a
    file scanner first; this is the note saying why that answer is wrong.

    Substring containment rather than equality, because the value was matched inside a larger
    leaf — a shell command line, a tool argument blob — and the leaf is what was stored.
    """
    if not path:
        return None
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return any(value in leaf for leaf in _string_leaves(payload))


def read_eventlog(root: str | Path, session_id: str) -> Path | None:
    """Resolve a session id to its event log, or None when there is none.

    The lookup half of the drill-down. Returns the path rather than the parsed log so a
    caller can hand it straight to `scribe qc --events`, which is the common reason to
    want it.
    """
    path = eventlog_path(root, session_id)
    return path if path.is_file() else None


class EventLogError(ValueError):
    """An event log that cannot be read back. Raised by `load_eventlog` only."""


def _rebuild(cls, data: dict, **overrides):
    """Reconstruct one dataclass from its own `to_dict` output, field by field.

    Driven by `__dataclass_fields__` rather than a hand-written list. The hand-written
    version drifted: it rebuilt the rollup and the event skeleton but dropped `user_text`,
    `assistant_text` and `result_digest`, so this CLI graded the *same* digest against the
    *same* log more strictly than the pipeline did -- the model's own turns and every tool
    result were missing from the corpus, and true claims drawn from them read as ungrounded.
    Since the CLI is what CI runs, that is the path that would have reported the phantom
    findings. Deriving the field list from the dataclass means adding a field to the schema
    cannot silently reintroduce it.
    """
    kwargs = {}
    for name, spec in cls.__dataclass_fields__.items():
        if name in overrides:
            kwargs[name] = overrides[name]
            continue
        want = spec.type if isinstance(spec.type, str) else ""
        required = spec.default is MISSING and spec.default_factory is MISSING
        if name not in data:
            # A field the dataclass has no default for must still be supplied, or an event log
            # missing `session_id` or a turn missing `turn_uuid` raises TypeError instead of
            # being graded. `to_dict` omits empty values, so this is an ordinary round trip,
            # not a malformed-input case.
            if required:
                kwargs[name] = 0 if want.startswith("int") else ""
            continue
        value = data[name]
        if want.startswith("list["):
            kwargs[name] = [str(x) for x in value] if isinstance(value, list) else []
        elif want.startswith("int"):
            kwargs[name] = int(value)
        elif want.startswith("str"):
            kwargs[name] = str(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def log_from_dict(data: dict) -> EventLog:
    """Rebuild an `EventLog` from one already-decoded serialized log.

    The in-memory half of `load_eventlog`, split out because `scribe qc --events` reads and
    decodes the file itself in order to give its own message for malformed JSON.

    Everything `EventLog.grounding_text()` reads must be reconstructed, because that text is
    the ground truth the groundedness gate checks against. See `_rebuild`.

    **`stats` is reconstructed, not discarded.** It used to be dropped -- `qc` grades from
    `turns` and never reads it, so for the gate it was dead weight. A REPLAY is the other
    caller, and it needs `raw_file_bytes` to record the offset and the turn and event counts
    to report what it did. Dropping them there would produce a digest that looks entirely
    normal over a session the state DB then believes it has read nothing of.
    """
    turns = []
    for t in data.get("turns") or []:
        turn: Turn = _rebuild(Turn, t, rollup=_rebuild(Rollup, t.get("rollup") or {}), events=[])
        for e in t.get("events") or []:
            turn.events.append(_rebuild(ToolEvent, e))
        turns.append(turn)
    return _rebuild(
        EventLog,
        data,
        turns=turns,
        rollup=_rebuild(Rollup, data.get("rollup") or {}),
        stats=_rebuild(Stats, data.get("stats") or {}),
    )


def load_eventlog(path: str | Path) -> EventLog:
    """Read one persisted event log back into an `EventLog`. The inverse of `write_eventlog`.

    This module's own docstring has promised since it was written that the persisted log
    "lets a replay skip re-extraction", and until now there was no reader to do it with:
    `write_eventlog` wrote, `read_eventlog` returned a *path*, and `pipeline.process_session`
    reconstructed by calling `extract(transcript_path)`. So a deleted transcript was terminal
    even though the evidence survived -- and transcripts age out at 30 days while
    `eventlogs/` has no cleanup policy at all (vikunja#873, #778).

    A dataclass round trip, not a re-derivation. The serialized form is `EventLog.to_dict()`
    verbatim and carries every field on the dataclass, so this reads what was written rather
    than inferring it.

    **Raises rather than returning None**, unlike `contains_value` and `read_eventlog`. Those
    two answer a question where "cannot tell" is a real and useful third answer. This one is
    an input to summarization: a caller that silently received an empty log would write a
    digest that looks normal and says nothing, which is the failure this whole component
    exists to make impossible.
    """
    p = Path(path).expanduser()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise EventLogError(f"cannot read event log {p}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise EventLogError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EventLogError(f"{p} is not an event log: top level is {type(payload).__name__}")
    try:
        return log_from_dict(payload)
    except (AttributeError, TypeError, ValueError) as exc:
        raise EventLogError(f"{p} is not an event log: {exc}") from exc
