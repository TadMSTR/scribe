"""Tests for deterministic rendering.

Every rendered block must end with a newline. The missing one glued ~1,030 headings together
in the memory corpus (vikunja#439), so it is asserted on every render path rather than on
the one that happened to be remembered.
"""

from __future__ import annotations

import pytest

from scribe.summarize.render import render_digest, render_failure, render_suppressed
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
