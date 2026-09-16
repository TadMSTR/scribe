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

from .schema import Digest

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


def render_digest(digest: Digest, *, heading: str = "") -> str:
    """Render one session digest. Always terminated with a newline."""
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
