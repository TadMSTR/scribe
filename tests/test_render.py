"""Tests for deterministic rendering.

Every rendered block must end with a newline. The missing one glued ~1,030 headings together
in the memory corpus (vikunja#439), so it is asserted on every render path rather than on
the one that happened to be remembered.
"""

from __future__ import annotations

import pytest

from scribe.summarize.render import (
    TRUNCATION_MARKER,
    render_digest,
    render_failure,
    render_suppressed,
)
from scribe.summarize.schema import Digest

FULL = Digest(
    asked="Check the release workflow",
    done=["Listed runs", "Read logs"],
    found=["All three jobs passed"],
    decisions=["Deferred arm64"],
    open_items=["Set the default branch"],
    artifacts=["/repo/CHANGELOG.md"],
    tickets=["#843"],
)


@pytest.mark.parametrize(
    "text",
    [
        render_digest(FULL),
        render_digest(FULL, heading="14:52"),
        render_digest(Digest(asked="a", done=["b"])),
        render_failure(transcript_path="/t.jsonl", reason="429", attempts=3),
        render_failure(transcript_path="/t.jsonl", reason="429", attempts=3, heading="14:52"),
        render_suppressed(note="- suppressed"),
        render_suppressed(note="- suppressed", heading="14:52"),
    ],
)
def test_every_render_path_ends_with_exactly_one_newline(text: str) -> None:
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_all_sections_render() -> None:
    out = render_digest(FULL)
    for label in ("Done", "Found", "Decisions", "Open", "Artifacts", "Tickets"):
        assert f"**{label}:**" in out
    assert "**Asked:** Check the release workflow" in out


def test_empty_sections_are_omitted() -> None:
    out = render_digest(Digest(asked="a", done=["b"]))
    assert "**Done:**" in out
    for label in ("Found", "Decisions", "Open", "Artifacts", "Tickets"):
        assert f"**{label}:**" not in out


def test_heading_is_rendered_when_given() -> None:
    assert render_digest(FULL, heading="14:52").startswith("### 14:52\n")
    assert not render_digest(FULL).startswith("###")


def test_multiline_values_are_flattened_to_one_bullet() -> None:
    """A multi-line value would emit lines that are not bullets, breaking the markdown and
    any downstream parse that assumes one item per line."""
    out = render_digest(Digest(asked="a", done=["line one\nline two\n\nline three"]))
    bullets = [x for x in out.splitlines() if x.startswith("- ")]
    assert bullets == ["- line one line two line three"]


def test_failure_placeholder_is_unmistakably_a_failure() -> None:
    out = render_failure(transcript_path="/p/t.jsonl", reason="rate limited", attempts=3)
    assert "placeholder, not a summary" in out
    assert "rate limited" in out
    assert "Attempts: 3" in out


def test_failure_placeholder_carries_the_path_and_not_the_transcript() -> None:
    """vikunja#386: leaving raw content on disk for a failed summary is a standing
    secret-exposure path. The path is enough to replay from."""
    out = render_failure(
        transcript_path="/p/t.jsonl",
        reason="boom",
        attempts=1,
    )
    assert "/p/t.jsonl" in out
    assert len(out) < 400, "a placeholder this long is carrying content it should not"


def test_rendering_is_deterministic() -> None:
    assert render_digest(FULL) == render_digest(FULL)


def test_a_truncated_field_says_so_in_the_block() -> None:
    """The loss has to be visible to whoever reads the memory file.

    The entire argument for truncating rather than rejecting is that a shortened digest beats
    no digest — which is only honest if the shortening is stated. A silent truncation would be
    the worst of the three options: #849 and #872 were both caught because their failure was
    loud.
    """
    d = Digest(asked="a", done=["x"], found=["f"] * 40, truncated={"found": 4})
    out = render_digest(d)
    assert TRUNCATION_MARKER in out
    assert "4 further items dropped at the 40-item cap." in out


def test_the_truncation_note_is_not_a_bullet() -> None:
    """A bullet would be graded as a claim.

    `qc` reads bullets as the model's assertions and checks each against the event log. A
    bullet saying "4 further items dropped" is scribe's own prose about scribe, appears in no
    event log, and would attach a guaranteed groundedness finding to precisely the digests
    this change exists to stop losing.
    """
    d = Digest(asked="a", done=["x"], found=["f"] * 40, truncated={"found": 4})
    note = [ln for ln in render_digest(d).splitlines() if TRUNCATION_MARKER in ln]
    assert len(note) == 1
    assert not note[0].lstrip().startswith("-")


def test_the_note_lands_under_the_field_that_was_truncated() -> None:
    """Attribution matters when two fields overflow — one note at the end could not say
    which field lost what."""
    d = Digest(
        asked="a",
        done=["x"],
        found=["f"] * 40,
        decisions=["d"] * 40,
        truncated={"decisions": 2},
    )
    lines = render_digest(d).splitlines()
    assert lines[lines.index("**Decisions:**") + 41].startswith(TRUNCATION_MARKER)
    assert not any(
        ln.startswith(TRUNCATION_MARKER)
        for ln in lines[lines.index("**Found:**") : lines.index("**Decisions:**")]
    )


def test_an_untruncated_digest_carries_no_note() -> None:
    assert TRUNCATION_MARKER not in render_digest(Digest(asked="a", done=["x"], found=["f", "g"]))
