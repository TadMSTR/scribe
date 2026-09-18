"""Render a validated `Digest` to markdown.

**Rendering is deterministic code, not model output.** The model produces structured fields;
the shape of the memory file is decided here. That is what makes formatting regressions
impossible to blame on a model, and it is what lets the QC gate compare a digest's claims
against the event log field by field.

Every rendered block **ends with a newline**. A missing one glued ~1,030 headings together
in the memory corpus (vikunja#439), and the fix belongs at the point of emission rather than
in whatever concatenates the blocks.
"""

from __future__ import annotations

import re

from .schema import Digest, field_caps

_SECTIONS: tuple[tuple[str, str], ...] = (
    ("done", "Done"),
    ("found", "Found"),
    ("decisions", "Decisions"),
    ("open_items", "Open"),
    ("artifacts", "Artifacts"),
    ("tickets", "Tickets"),
)


def _bullet(text: str) -> str:
    """One bullet, with interior newlines flattened.

    A multi-line value would otherwise emit lines that are not bullets, which breaks both
    the markdown and any downstream parse that assumes one item per line.
    """
    return "- " + " ".join(text.split())


#: Prefix of the line that records a truncated field, and the pattern that matches the whole
#: line. Both are constants for the same reason `PLACEHOLDER_MARKER` is: `qc` has to recognise
#: this line to *exclude* it, and a reworded banner that silently stops being recognised would
#: reintroduce the finding it exists to avoid.
TRUNCATION_MARKER = "_Truncated:"
TRUNCATION_RE = re.compile(rf"^{re.escape(TRUNCATION_MARKER)}[^\n]*_$", re.M)


def _truncation_note(dropped: int, cap: int) -> str:
    """The line that says items were dropped.

    **Deliberately not a bullet.** The QC gate reads bullets as the model's claims and grades
    each against the event log; a bullet asserting "4 further items were dropped" is a claim
    scribe made, about scribe, which appears in no event log and would fail groundedness on
    every truncated digest. That is the vikunja#848 shape — a gate that flags its own output.
    `qc.strip_scribe_markers` removes this line for the same reason it removes the anchor.

    It stays human-visible regardless, because the whole argument for truncating over
    rejecting is that a shortened digest beats no digest — and that is only honest if the
    reader can see it was shortened.
    """
    items = "item" if dropped == 1 else "items"
    return f"{TRUNCATION_MARKER} {dropped} further {items} dropped at the {cap}-item cap._"


def render_digest(digest: Digest, *, heading: str = "", caps: dict[str, int] | None = None) -> str:
    """Render one session digest. Always terminated with a newline.

    `caps` is threaded in rather than read from the constants because the truncation note
    quotes the cap that fired, and since `caps_for` derives per session the constant is no
    longer that number. A note reading "dropped at the 40-item cap" under a session whose cap
    was 156 is a statement about the code that is false about the run — and the note exists
    precisely so a reader can see how much was lost and against what.
    """
    caps = caps or field_caps()
    lines: list[str] = []
    if heading:
        lines.append(f"### {heading}")
        lines.append("")
    lines.append(f"**Asked:** {' '.join(digest.asked.split())}")
    for attr, label in _SECTIONS:
        values = getattr(digest, attr)
        if not values:
            continue
        lines.append("")
        lines.append(f"**{label}:**")
        lines.extend(_bullet(v) for v in values)
        dropped = digest.truncated.get(attr, 0)
        if dropped:
            lines.append(_truncation_note(dropped, caps[attr]))
    return "\n".join(lines) + "\n"


#: The line that identifies a placeholder block on disk.
#:
#: A constant because `scribe recover` has to find these blocks in a corpus written before
#: provisional anchors existed, and matching on a prose literal copied into another module is
#: how a reworded banner silently stops being recoverable. `test_render.py` pins the renderer
#: to it, so the two cannot drift.
PLACEHOLDER_MARKER = "**Summary unavailable — this block is a placeholder, not a summary.**"


def render_failure(*, transcript_path: str, reason: str, attempts: int, heading: str = "") -> str:
    """Render a marked placeholder for a session that could not be summarized.

    Two requirements, both from past incidents:

      * It is **clearly marked as a failure**, so a reader never mistakes a gap for a quiet
        session and the block can be found and replayed.
      * It carries the **transcript path only** — never the transcript. Leaving raw content
        on disk for a failed summary is vikunja#386, a standing secret-exposure path. The
        path is enough to replay from, and the transcript is already on disk anyway.
    """
    lines = []
    if heading:
        lines.append(f"### {heading}")
        lines.append("")
    lines.append(PLACEHOLDER_MARKER)
    lines.append("")
    lines.append(f"- Reason: {' '.join(reason.split())}")
    lines.append(f"- Attempts: {attempts}")
    lines.append(f"- Replay from: `{transcript_path}`")
    return "\n".join(lines) + "\n"


def render_suppressed(*, note: str, heading: str = "") -> str:
    """Render the contamination-guard fallback. Also newline-terminated."""
    lines = []
    if heading:
        lines.append(f"### {heading}")
        lines.append("")
    lines.append(note.rstrip("\n"))
    return "\n".join(lines) + "\n"
