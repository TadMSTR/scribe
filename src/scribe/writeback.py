"""Write session digests to disk.

**Scribe writes to its own output directory, not to the live memory files.** Phase 6 is a
shadow run: `memsearch-summarize` stays in production and cutover is a separate decision. The
layout mirrors the live one exactly — `<root>/<agent>/YYYY-MM-DD.md` with the same anchor
comment — so a side-by-side comparison is a diff rather than a translation, and so cutover is
a change of root rather than a change of format.

Two properties matter more than the format:

**Every block ends with a newline.** A missing one glued ~1,030 headings together in the live
corpus (vikunja#439). Enforced at the point of emission and asserted on every path.

**Appending the same turn twice is a no-op.** The anchor carries the turn uuid, and the store
already refuses a duplicate; this module refuses one too, by reading the file. Two guards
because they fail in different circumstances — the store can be reset, and the file can be
edited by hand.

**A provisional block is not a duplicate.** That dedup guard was, for 30 sessions, the thing
that made the loss permanent (vikunja#872, #868). A failed summarization writes a marked
placeholder, which carries the turn uuid like any other block. The session then retried on a
later sweep — `scan` gates on unread bytes, not on status, so it really did retry — the model
really did produce a digest, and `append_block` threw it away, because the uuid was already
present. The state was then marked summarized and the session was done. The digest was
generated, paid for, and discarded, and nothing counted it.

So a block records whether its body is *final* or *provisional*, in the anchor. A final block
is still refused. A provisional one is **replaced in place** by the first real digest that
arrives for the same turn.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .paths import secure_create, secure_dir, secure_file

ANCHOR_RE = re.compile(
    r"<!--\s*session:(\S+)\s+turn:(\S+)\s+transcript:(\S+?)"
    r"(?:\s+provisional:(\S+))?\s*-->"
)

#: A block whose body is a marked placeholder — `render_failure` output. The summary is gone.
PROVISIONAL_PLACEHOLDER = "placeholder"
#: A block whose body is the contamination guard's suppression note — `render_suppressed`.
PROVISIONAL_SUPPRESSED = "suppressed"
#: Written after the body. Its presence is what proves the block was written whole --
#: see `existing_turns`.
TERMINATOR_RE = re.compile(r"<!--\s*/scribe\s+turn:(\S+)\s*-->")


def terminator(turn_uuid: str) -> str:
    """Closing marker for a block, written last.

    This is what makes a torn write *detectable*. Without it, a crash mid-append leaves an
    anchor claiming the turn is done above a truncated body, and both dedup guards then agree
    never to retry it — the session is lost silently and permanently. Making the tear
    detectable is far cheaper than making the write atomic, which would mean
    read-modify-replace of the whole daily file on every append.
    """
    return f"<!-- /scribe turn:{turn_uuid} -->"


def anchor(session_id: str, turn_uuid: str, transcript_path: str, provisional: str = "") -> str:
    """The HTML comment that identifies a block.

    Keyed on the turn uuid. The pipeline being replaced keyed its idempotency on
    `(session_id, HH:MM)`, which collided 62 times across 3,539 blocks (vikunja#844) — a
    minute is not fine-grained enough to identify a turn, and a uuid has no granularity to
    be wrong at.

    `provisional` names the kind of stand-in a block holds, and is **omitted entirely** for a
    real digest. Omitted rather than written as `provisional:final` so the 444 blocks already
    on disk read correctly under the new regex without being rewritten: no attribute has
    always meant, and still means, a finished digest.
    """
    tail = f" provisional:{provisional}" if provisional else ""
    return f"<!-- session:{session_id} turn:{turn_uuid} transcript:{transcript_path}{tail} -->"


def daily_path(root: str | Path, agent: str, when: datetime) -> Path:
    """`<root>/<agent>/YYYY-MM-DD.md`, mirroring the live memory layout."""
    return Path(root).expanduser() / (agent or "unknown") / f"{when:%Y-%m-%d}.md"


def _complete_blocks(text: str) -> dict[str, str]:
    """Turn uuid -> its `provisional` attribute (`""` for a final digest), for whole blocks.

    A turn counts only when its opening anchor AND its closing terminator are both present.
    An anchor alone means a torn write, and reporting that as "already done" is precisely how
    a session would be lost forever: `append_block` would skip it, and the store would too.
    Omitting it makes the next sweep rewrite it.
    """
    closed = {m.group(1) for m in TERMINATOR_RE.finditer(text)}
    return {
        m.group(2): (m.group(4) or "") for m in ANCHOR_RE.finditer(text) if m.group(2) in closed
    }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def existing_turns(path: Path) -> set[str]:
    """Turn uuids whose block is complete **and final** — a real digest, never to be rewritten.

    A provisional block is deliberately absent from this set. It occupies the uuid on disk but
    it is not an answer, and treating it as one is what made 30 sessions unrecoverable.
    """
    return {uuid for uuid, kind in _complete_blocks(_read(path)).items() if not kind}


def provisional_turns(path: Path) -> dict[str, str]:
    """Turn uuids whose complete block is a stand-in, mapped to which kind it is."""
    return {uuid: kind for uuid, kind in _complete_blocks(_read(path)).items() if kind}


def _block(
    *, body: str, session_id: str, turn_uuid: str, transcript_path: str, provisional: str
) -> str:
    """The anchor / body / terminator triple, without any surrounding separation."""
    return (
        anchor(session_id, turn_uuid, transcript_path, provisional)
        + "\n"
        + (body if body.endswith("\n") else body + "\n")
        + terminator(turn_uuid)
        + "\n"
    )


def atomic_replace(path: Path, text: str) -> None:
    """Replace `path`'s whole contents, via a temp file and `os.replace`.

    Used by every path that rewrites a daily file rather than appending to one — the digest
    replace here, and `recover`'s anchor stamping. Shared rather than written twice because
    the two easy mistakes are both invisible in the result:

      * **No fsync.** `os.replace` is atomic with respect to other readers, not with respect
        to a power loss: without the flush the rename can land before the bytes do.
      * **A temp file at the process umask.** `open(path, "w")` creates 0644, and **rename
        preserves the source's permissions**, so a 0644 temp file silently downgrades an
        already-0600 daily file. A `chmod` after the rename closes the window but does not
        remove it. `secure_create` applies the mode at `O_CREAT`, so there is none.
        (FW-03 in the fleet's pattern knowledge base.)
    """
    secure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    with secure_create(tmp) as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def replace_block(
    path: Path,
    *,
    body: str,
    session_id: str,
    turn_uuid: str,
    transcript_path: str,
    provisional: str = "",
) -> bool:
    """Swap a provisional block's body for a real one, in place. Returns False if absent.

    Read-modify-replace of the whole daily file, which `append_block` deliberately avoids
    paying on every write. It is the right price *here*: this runs only when a stand-in is
    being upgraded, which is rare by construction and is the alternative to losing the digest
    outright. The write goes via a temp file and `os.replace`, so a crash leaves either the
    old file or the new one and never a half-rewritten corpus — the append path cannot do
    that, which is why it settles for making a tear detectable instead.

    The `## Session HH:MM` heading above the anchor is left alone: the replacement is the same
    session at the same time, so the heading is still correct, and not touching it keeps the
    rewrite to exactly the bytes that are wrong.
    """
    text = _read(path)
    if turn_uuid not in provisional_turns(path):
        return False

    start = end = -1
    for m in ANCHOR_RE.finditer(text):
        if m.group(2) != turn_uuid:
            continue
        close = TERMINATOR_RE.search(text, m.end())
        if close is None:
            return False
        start, end = m.start(), close.end()
        break
    if start < 0:
        return False

    replacement = _block(
        body=body,
        session_id=session_id,
        turn_uuid=turn_uuid,
        transcript_path=transcript_path,
        provisional=provisional,
    ).rstrip("\n")
    updated = text[:start] + replacement + text[end:]

    atomic_replace(path, updated)
    return True


def append_block(
    path: Path,
    *,
    body: str,
    session_id: str,
    turn_uuid: str,
    transcript_path: str,
    when: datetime,
    provisional: str = "",
) -> bool:
    """Append one digest block, or replace a provisional one. False if already final.

    The whole block is composed in memory and written in a single append, so an interrupted
    write cannot leave a half-block with an anchor that claims the turn is done.

    A turn already on disk as a **provisional** block is replaced rather than skipped — that
    is the write-side half of making a lost digest recoverable. Note the ordering: the final
    check comes first, so a real digest is never overwritten by anything, including by a later
    placeholder for the same turn.
    """
    # Digests are derived from 0600 transcripts and must not be published wider than
    # their source. See paths.py.
    secure_dir(path.parent)
    if turn_uuid in existing_turns(path):
        return False
    if turn_uuid in provisional_turns(path):
        return replace_block(
            path,
            body=body,
            session_id=session_id,
            turn_uuid=turn_uuid,
            transcript_path=transcript_path,
            provisional=provisional,
        )

    parts: list[str] = []
    if path.exists() and path.stat().st_size:
        # Separate from whatever precedes it. Reading the tail rather than assuming it ended
        # cleanly: the corpus this mirrors contains ~1,030 blocks that did not.
        tail = path.read_text(encoding="utf-8", errors="replace")[-2:]
        if not tail.endswith("\n"):
            parts.append("\n")
        if not tail.endswith("\n\n"):
            parts.append("\n")
    else:
        parts.append(f"# {when:%Y-%m-%d}\n\n")

    parts.append(f"## Session {when:%H:%M}\n\n")
    parts.append(
        _block(
            body=body,
            session_id=session_id,
            turn_uuid=turn_uuid,
            transcript_path=transcript_path,
            provisional=provisional,
        )
    )

    # FW-01 (atomic state writes) resolves differently for an append than for a replace:
    # there is no temp-file-then-rename for "add to the end of a file". The whole block is
    # composed in memory and handed to ONE write call, then fsynced, so a completed append is
    # durable and concurrent appenders cannot interleave.
    #
    # A crash mid-write can still tear. What changed after the scribe-2026-09 audit (F-04) is
    # that a tear is now DETECTABLE rather than silent: the block ends with a terminator, and
    # `existing_turns` counts a turn only when anchor and terminator are both present. The
    # audit framed the choice as "accept the risk, or read-modify-replace the whole daily file
    # per append". Its own analysis pointed at the better option -- the harm was never the
    # tear itself but that neither guard could SEE it, so the session was lost permanently.
    # Detection costs one line per block; atomicity would cost O(file) on every append.
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(parts))
        fh.flush()
        os.fsync(fh.fileno())
    secure_file(path)
    return True
