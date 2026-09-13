"""Tests for the ported contamination guard.

Ported from `memsearch-summarize.py`; vikunja#248 asks for this logic to be unified rather
than triplicated, so this copy is intended to be the shared one. The tests are weighted
toward the false-positive side: a guard that fires on ordinary prose suppresses real
summaries, which is a worse outcome than the leak it was built to stop.
"""

from __future__ import annotations

import pytest

from scribe.summarize.contamination import build_fallback_note, detect_contamination


@pytest.mark.parametrize(
    "summary,reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("- did <specific step> today", "placeholder_token"),
        ("- ran <the command>", "placeholder_token"),
        ("### Phase 1 — Extractor", "template_signature"),
        ("## Configuration", "template_signature"),
        ("Base directory for this skill: /x", "template_signature"),
        ("- wrote N docs", "residue_token"),
        ("- reviewed XX tickets", "residue_token"),
        ("- progress N/M complete", "residue_token"),
        ("- dated YYYY-MM-DD", "residue_token"),
        ("- filed on 2026-07-XX", "residue_token"),
    ],
)
def test_contamination_signals(summary: str, reason: str) -> None:
    assert detect_contamination(summary, raw="") == reason


@pytest.mark.parametrize(
    "summary",
    [
        "- Merged PR #16 and tagged v0.5.1",
        "- Chose option A over plan B after measuring",
        "- Coverage is N/A for the vendored module",
        "- The image is 500M and the cache is 1.2G",
        "- Ran 3/5 of the checks, dated 2026-09-13",
        "- Fixed the parser and added 12 tests",
    ],
)
def test_ordinary_prose_is_not_flagged(summary: str) -> None:
    """Over-firing suppresses real summaries. `N/A`, `500M`, `option A` and real dates are
    all explicitly carved out."""
    assert detect_contamination(summary, raw="") is None


def test_verbatim_overlap_is_detected() -> None:
    raw = "\n".join(
        [
            "This line is long enough to count as non-trivial content",
            "And so is this second line of substantial source material",
            "And a third line that is also comfortably over the threshold",
        ]
    )
    assert detect_contamination(raw, raw) == "verbatim_overlap"


def test_two_overlapping_lines_are_not_enough() -> None:
    """The threshold is three consecutive lines. Two is a coincidence worth tolerating."""
    raw = "\n".join(
        [
            "This line is long enough to count as non-trivial content",
            "And so is this second line of substantial source material",
            "A third line that differs entirely from anything in the summary",
        ]
    )
    summary = "\n".join([*raw.splitlines()[:2], "a genuinely original closing observation here"])
    assert detect_contamination(summary, raw) is None


def test_short_lines_are_ignored_when_comparing() -> None:
    raw = "- a\n- b\n- c\n- d\n"
    assert detect_contamination(raw, raw) is None


def test_fallback_note_quotes_the_first_user_line() -> None:
    raw = "[User]: please check the release workflow\n[Claude Code]: ok"
    assert "please check the release workflow" in build_fallback_note(raw)
    assert "contamination guard" in build_fallback_note(raw)


def test_fallback_note_drops_a_contaminated_first_line() -> None:
    """The 'safe' fallback could otherwise re-leak up to 200 characters of exactly the
    disallowed content — a MEDIUM audit finding on 2026-07-21."""
    raw = "[User]: Base directory for this skill: /home/x/skills/thing"
    note = build_fallback_note(raw)
    assert "Base directory" not in note
    assert "User turn:" not in note
    assert "contamination guard" in note


def test_fallback_note_drops_a_placeholder_first_line() -> None:
    note = build_fallback_note("[User]: run <specific step> now")
    assert "<specific step>" not in note


def test_fallback_note_with_no_user_line_is_just_the_marker() -> None:
    note = build_fallback_note("[Claude Code]: hello")
    assert note.startswith("- Summary suppressed")


def test_fallback_note_truncates_a_long_user_line() -> None:
    note = build_fallback_note("[User]: " + "x" * 500)
    assert len(note) < 400


# --- F-05: the fallback re-check runs three of the four detectors -------------------


@pytest.mark.parametrize(
    "line,detector",
    [
        ("run <specific step> now", "placeholder"),
        ("Base directory for this skill: /x", "signature"),
        ("wrote N docs today", "residue (added by F-05)"),
        ("reviewed XX tickets", "residue (added by F-05)"),
        ("dated YYYY-MM-DD", "residue (added by F-05)"),
    ],
)
def test_a_contaminated_first_line_is_dropped_from_the_fallback(line: str, detector: str) -> None:
    """The 'safe' fallback must not re-leak the content the guard exists to suppress.

    Upstream ran only the placeholder and signature detectors here. `_RESIDUE_RES` was added
    for scribe (F-05, 2026-09-13 audit) because upstream's omission carried no stated reason
    and a residue token is the same class of template artefact as the other two.
    """
    note = build_fallback_note(f"[User]: {line}")
    assert "User turn:" not in note, f"{detector} did not suppress the quoted line"
    assert "contamination guard" in note


@pytest.mark.parametrize(
    "line",
    [
        "please check the release workflow",
        "merged PR #16 and tagged v0.5.1",
        "coverage is N/A for the vendored module",
        "the image is 500M",
    ],
)
def test_a_clean_first_line_still_survives_the_re_check(line: str) -> None:
    """The matched half. A re-check that dropped everything would pass the test above while
    making the fallback useless."""
    note = build_fallback_note(f"[User]: {line}")
    assert "User turn:" in note
    assert line[:20] in note
