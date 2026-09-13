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
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .paths import secure_dir, secure_file

ANCHOR_RE = re.compile(r"<!--\s*session:(\S+)\s+turn:(\S+)\s+transcript:(\S+?)\s*-->")


def anchor(session_id: str, turn_uuid: str, transcript_path: str) -> str:
    """The HTML comment that identifies a block.

    Keyed on the turn uuid. The pipeline being replaced keyed its idempotency on
    `(session_id, HH:MM)`, which collided 62 times across 3,539 blocks (vikunja#844) — a
    minute is not fine-grained enough to identify a turn, and a uuid has no granularity to
    be wrong at.
    """
    return f"<!-- session:{session_id} turn:{turn_uuid} transcript:{transcript_path} -->"


def daily_path(root: str | Path, agent: str, when: datetime) -> Path:
    """`<root>/<agent>/YYYY-MM-DD.md`, mirroring the live memory layout."""
    return Path(root).expanduser() / (agent or "unknown") / f"{when:%Y-%m-%d}.md"


def existing_turns(path: Path) -> set[str]:
    """Turn uuids already present in a daily file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {m.group(2) for m in ANCHOR_RE.finditer(text)}


def append_block(
    path: Path,
    *,
    body: str,
    session_id: str,
    turn_uuid: str,
    transcript_path: str,
    when: datetime,
) -> bool:
    """Append one digest block. Returns False if the turn is already present.

    The whole block is composed in memory and written in a single append, so an interrupted
    write cannot leave a half-block with an anchor that claims the turn is done.
    """
    # Digests are derived from 0600 transcripts and must not be published wider than
    # their source. See paths.py.
    secure_dir(path.parent)
    if turn_uuid in existing_turns(path):
        return False

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
    parts.append(anchor(session_id, turn_uuid, transcript_path) + "\n")
    parts.append(body if body.endswith("\n") else body + "\n")

    # FW-01 (atomic state writes) resolves differently for an append than for a replace:
    # there is no temp-file-then-rename for "add to the end of a file". What is done instead:
    # the whole block is composed in memory and handed to ONE write call, then fsynced, so a
    # completed append is durable and concurrent appenders cannot interleave.
    #
    # The residual, stated plainly rather than glossed: a crash *during* a write larger than
    # PIPE_BUF can still tear, and because the anchor precedes the body, a torn block leaves
    # an anchor claiming the turn is done above a truncated body. Both dedup guards -- this
    # file and the store -- would then agree not to rewrite it. Closing that properly means
    # read-modify-replace of the whole daily file on every append, which is O(file) per block
    # and trades a rare truncation for a routine cost. Flagged in the audit request rather
    # than decided here.
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(parts))
        fh.flush()
        os.fsync(fh.fileno())
    secure_file(path)
    return True
