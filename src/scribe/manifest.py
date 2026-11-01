"""`index.jsonl` — a machine-readable manifest of the digest corpus, for push indexers.

A glob-based indexer (qmd on forge) needs nothing from scribe beyond "index `output_dir`".
An indexer behind an API — a vector DB, Meilisearch, OpenSearch — cannot glob; it has to be
*told* what changed, and before this it would have had to parse the HTML anchor comments to
learn a block's identity, which no third-party indexer does. This file is that telling, in a
form any consumer can tail.

**One line per block written, append-only. Record identity is `(path, turn_uuid)`, and the
same `path` recurs.** Digests are not write-once, and `writeback.append_block` has three
outcomes, two of which produce a line:

  * turn already **final** — nothing written, **no line**;
  * turn present as **provisional** — replaced in place, **same `turn_uuid`**, new `sha256`:
    a second line for the same `(path, turn_uuid)`;
  * turn absent — appended with a new `turn_uuid`: a line for the same `path`, new key.

Since 0.18.0 there is a fourth outcome: a block for a session that already has blocks in the
file **supersedes** them (vikunja#1347) -- unless it is a `placeholder` or `suppressed`
stand-in and the session has a final block, in which case it is appended beside it. The
superseding block takes the first one's place and the others are removed, so their keys stop
existing on disk. Each removed key gets a **tombstone** line: `provisional` is `"superseded"`,
`sha256` is `""`, and an extra field `superseded_by` names the turn that replaced it. A
consumer filtering on `provisional == ""` skips tombstones without knowing about them; one that
tracks records drops the key. `read` drops it, so `check` does not report the removed block as
stale.

So a consumer needs two rules, and they are not the same rule:

  * **record identity is `(path, turn_uuid)`; the last line for a key wins;**
  * **the indexable document is the file** — on any line naming a `path`, re-ingest that
    whole file rather than trying to assemble it from block records.

Fields, in order:

    path           digest file, relative to output_dir, POSIX separators
    sha256         of THIS BLOCK -- anchor line through terminator line, UTF-8. Not the file:
                   a file hash changes whenever a later block is appended, so every earlier
                   line would go stale and `--check` could never pass.
    agent          the digest's directory -- the partition it was actually filed under
    date           the daily file's stem, YYYY-MM-DD
    session_id     from the block's anchor
    turn_uuid      from the block's anchor
    provisional    "" for a real digest, else "placeholder" or "suppressed"; a consumer that
                   wants only real digests filters on provisional == ""
    eventlog_path  relative to eventlog_dir, or "" if the event log is absent
    written_at     ISO-8601 UTC when the line was appended; null for a rebuilt line.
                   INFORMATIONAL: nothing on disk records it, so it is never verified

**Derived state, never authoritative.** Every field but `written_at` is re-derivable from the
digests themselves, which is what `rebuild` and `check` do. A manifest that could drift in
silence would be worse than none, because consumers trust it.

**A sibling of `output_dir` by default, never a child**, and `config.load` refuses a
`manifest_path` inside either the digest or the event-log tree — see `Config.manifest_file`.
Written `0600`: it carries `session_id` values, which `paths.py` documents as
credential-shaped when paired with `claude -p --resume`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .eventlog import session_eventlog
from .paths import secure_append, secure_create, secure_dir, secure_file
from .writeback import paired_blocks

#: Every field `rebuild` can reproduce, and therefore the set `check` compares. `written_at`
#: is deliberately absent: comparing it would report a difference on every line forever.
DERIVED_FIELDS = (
    "path",
    "sha256",
    "agent",
    "date",
    "session_id",
    "turn_uuid",
    "provisional",
    "eventlog_path",
)
FIELDS = (*DERIVED_FIELDS, "written_at")
#: `provisional` on a tombstone line. Not a value any block on disk can carry.
SUPERSEDED = "superseded"

Key = tuple[str, str]


def _read(path: Path) -> str:
    """Same decoding as `writeback._read`, so a hash here matches a hash taken at write time."""
    return path.read_text(encoding="utf-8", errors="replace")


def _record(cfg: Config, md: Path, text: str, anchor: re.Match[str], close: re.Match[str]) -> dict:
    """The manifest record for one block. The ONLY place a record is derived.

    Shared by the append path and the rebuild path, deliberately: if the two computed any
    field differently, `check` would report drift on correct data -- the failure that makes
    an operator stop reading a check.
    """
    root = Path(cfg.output_dir).expanduser()
    rel = md.relative_to(root)
    session_id, turn_uuid, transcript = anchor.group(1), anchor.group(2), anchor.group(3)
    evroot = Path(cfg.eventlog_dir).expanduser()
    ev = session_eventlog(evroot, session_id, transcript)
    block = text[anchor.start() : close.end()]
    return {
        "path": rel.as_posix(),
        "sha256": hashlib.sha256(block.encode("utf-8")).hexdigest(),
        "agent": rel.parent.as_posix() if rel.parent != Path(".") else "",
        "date": md.stem,
        "session_id": session_id,
        "turn_uuid": turn_uuid,
        "provisional": anchor.group(4) or "",
        "eventlog_path": ev.relative_to(evroot).as_posix() if ev.is_file() else "",
        "written_at": None,
    }


def _line(record: dict) -> str:
    return json.dumps({k: record[k] for k in FIELDS}, ensure_ascii=False) + "\n"


def _ensure_parent(path: Path) -> None:
    """Create the manifest's directory owner-only -- but only if it does not exist yet.

    Not `secure_dir` on an existing directory. That tightens its argument, which is right for
    a directory scribe owns and wrong here: the default parent is `output_dir`'s parent, and
    a `manifest_path` can put it anywhere, including `~`. The file itself is always `0600`.
    """
    if not path.parent.exists():
        secure_dir(path.parent)


def record_for_turn(cfg: Config, md: Path, turn_uuid: str) -> dict:
    """The record for `turn_uuid`'s block in `md`, as it is on disk now.

    Raises `LookupError` when the file holds no complete block for that turn, and `OSError`
    when it cannot be read. Takes the FIRST complete block for the uuid, as
    `writeback.replace_block` does, so both agree on which block is meant.
    """
    text = _read(md)
    for a, c in paired_blocks(text):
        if a.group(2) == turn_uuid:
            return _record(cfg, md, text, a, c)
    raise LookupError(f"no complete block for turn {turn_uuid} in {md}")


def append(cfg: Config, record: dict, *, now: datetime | None = None) -> Path:
    """Append one line, stamped `written_at`. One write call, then fsync."""
    path = cfg.manifest_file()
    _ensure_parent(path)
    stamped = dict(record, written_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"))
    with secure_append(path) as fh:
        fh.write(_line(stamped))
        fh.flush()
        os.fsync(fh.fileno())
    secure_file(path)
    return path


def tombstone(cfg: Config, md: Path, session_id: str, turn_uuid: str, superseded_by: str) -> dict:
    """The line recording that `turn_uuid`'s block in `md` was removed by a superseding block.

    Not derived from disk, because there is nothing left on disk to derive it from; `rebuild`
    never produces one, and `read` drops the key it names, so `check` compares like with like.
    """
    rel = md.relative_to(Path(cfg.output_dir).expanduser())
    return {
        "path": rel.as_posix(),
        "sha256": "",
        "agent": rel.parent.as_posix() if rel.parent != Path(".") else "",
        "date": md.stem,
        "session_id": session_id,
        "turn_uuid": turn_uuid,
        "provisional": SUPERSEDED,
        "eventlog_path": "",
        "written_at": None,
        "superseded_by": superseded_by,
    }


def append_tombstone(cfg: Config, record: dict, *, now: datetime | None = None) -> Path:
    """Append one tombstone line. Same write discipline as `append`."""
    path = cfg.manifest_file()
    _ensure_parent(path)
    stamped = dict(record, written_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"))
    line = json.dumps(
        {**{k: stamped[k] for k in FIELDS}, "superseded_by": stamped["superseded_by"]}
    )
    with secure_append(path) as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    secure_file(path)
    return path


def append_turn(cfg: Config, md: Path, turn_uuid: str, *, now: datetime | None = None) -> dict:
    """Derive the record for a block just written, and append it. Returns the record."""
    record = record_for_turn(cfg, md, turn_uuid)
    append(cfg, record, now=now)
    return record


def rebuild(cfg: Config) -> list[dict]:
    """Every complete block in the corpus, as records, **in memory**. Nothing is written.

    One record per `(path, turn_uuid)`, taking the first block for a uuid as
    `record_for_turn` does. A torn block (anchor with no terminator) is skipped, as
    `append_block` skips it: its extent is unknown and the next sweep rewrites it.
    """
    root = Path(cfg.output_dir).expanduser()
    out: list[dict] = []
    seen: set[Key] = set()
    for md in sorted(root.rglob("*.md")):
        if not md.is_file():
            continue
        text = _read(md)
        for a, c in paired_blocks(text):
            rec = _record(cfg, md, text, a, c)
            key = (rec["path"], rec["turn_uuid"])
            if key not in seen:
                seen.add(key)
                out.append(rec)
    return out


def write(cfg: Config, records: list[dict]) -> Path:
    """Replace the manifest wholesale with `records`, via a 0600 temp file and a rename.

    Not `writeback.atomic_replace`, only because that calls `secure_dir` on the parent -- see
    `_ensure_parent`. The temp file is created 0600 by the kernel (FW-03), fsynced, renamed.
    """
    path = cfg.manifest_file()
    _ensure_parent(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with secure_create(tmp) as fh:
            fh.write("".join(_line(r) for r in records))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        # A half-written temp file is a second copy of every session id, left lying around.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


@dataclass
class Current:
    """The manifest on disk, collapsed to its current records: last line per key wins."""

    records: dict[Key, dict] = field(default_factory=dict)
    #: `(line number, reason)` for every line that is not a well-formed record.
    malformed: list[tuple[int, str]] = field(default_factory=list)
    lines: int = 0
    #: Tombstone lines read (superseded blocks). Their keys are not in `records`.
    tombstones: int = 0


def read(path: Path) -> Current:
    """Parse the manifest. Raises `FileNotFoundError` if it does not exist."""
    cur = Current()
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh, start=1):
            cur.lines = n
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError as exc:
                cur.malformed.append((n, f"not JSON ({exc.msg})"))
                continue
            if not isinstance(rec, dict):
                cur.malformed.append((n, "not a JSON object"))
                continue
            missing = [k for k in DERIVED_FIELDS if not isinstance(rec.get(k), str)]
            if missing:
                cur.malformed.append((n, f"missing or non-string field(s): {', '.join(missing)}"))
                continue
            key = (rec["path"], rec["turn_uuid"])
            if rec.get("superseded_by"):
                # A tombstone: the block is gone. Last line wins, as for any key, so a block
                # later written again under the same key comes back.
                cur.records.pop(key, None)
                cur.tombstones += 1
                continue
            cur.records[key] = rec
    return cur


@dataclass
class Drift:
    """What `check` found. Clean only when every list is empty."""

    #: On disk as a block, absent from the manifest.
    missing: list[Key] = field(default_factory=list)
    #: In the manifest, with no block on disk -- a deleted digest, or a hand edit.
    stale: list[Key] = field(default_factory=list)
    #: Present in both, differing on these fields.
    changed: list[tuple[Key, list[str]]] = field(default_factory=list)
    malformed: list[tuple[int, str]] = field(default_factory=list)
    #: True when there was no manifest file at all.
    absent: bool = False
    expected: int = 0
    lines: int = 0
    #: Sessions with more than one FINAL block anywhere in the corpus, each with its blocks.
    #: Not drift -- the manifest can match the disk exactly and still list them -- but a
    #: breach of the corpus contract "one block per session" (systems/memory-pipeline.md,
    #: contract 1), which nothing checked before vikunja#1347. Provisional blocks do not
    #: count: a stand-in is replaced by the next write for its session.
    multi_block: list[tuple[str, list[Key]]] = field(default_factory=list)
    #: `provisional:pending` blocks on disk. Informational, not a failure.
    pending_blocks: list[Key] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """The manifest matches the corpus. Says nothing about `multi_block`."""
        return not (self.missing or self.stale or self.changed or self.malformed or self.absent)

    @property
    def one_block_per_session(self) -> bool:
        return not self.multi_block


def multi_block_sessions(records: list[dict]) -> list[tuple[str, list[Key]]]:
    """Sessions holding more than one final block, from `rebuild`'s records, sorted by id."""
    by_session: dict[str, list[Key]] = {}
    for r in records:
        if r["provisional"] == "" and r["session_id"]:
            by_session.setdefault(r["session_id"], []).append((r["path"], r["turn_uuid"]))
    return sorted((sid, keys) for sid, keys in by_session.items() if len(keys) > 1)


def check(cfg: Config) -> Drift:
    """Rebuild into memory and compare against the manifest on disk, on `DERIVED_FIELDS`.

    Also reports `multi_block`, which is a property of the corpus rather than of the
    manifest, so it is computed even when the manifest file is absent.
    """
    records = rebuild(cfg)
    expected = {(r["path"], r["turn_uuid"]): r for r in records}
    drift = Drift(
        expected=len(expected),
        multi_block=multi_block_sessions(records),
        pending_blocks=sorted(
            (r["path"], r["turn_uuid"]) for r in records if r["provisional"] == "pending"
        ),
    )
    try:
        cur = read(cfg.manifest_file())
    except FileNotFoundError:
        drift.absent = True
        drift.missing = sorted(expected)
        return drift
    drift.lines, drift.malformed = cur.lines, cur.malformed
    drift.missing = sorted(k for k in expected if k not in cur.records)
    drift.stale = sorted(k for k in cur.records if k not in expected)
    for key in sorted(k for k in expected if k in cur.records):
        diff = [f for f in DERIVED_FIELDS if expected[key][f] != cur.records[key][f]]
        if diff:
            drift.changed.append((key, diff))
    return drift
