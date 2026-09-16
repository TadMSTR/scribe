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


# --- #868: the guard fires on position, not on the presence of a token ---------------


#: The four sentences from vikunja#868's own table, plus prose from the same fleet. Three of
#: the four were FLAGGED by the old `_PLACEHOLDER_RE.search(summary)` rule, and each flag cost
#: a real session its digest.
_PROSE_THAT_MENTIONS_PLACEHOLDERS = [
    "Wrote <programme>-p<N>-<slug> directories",
    "Discussed the <build-name> convention",
    "Renamed <agent> to the real path",
    "Set output to ~/.local/share/scribe",
    "Replaced <agent> with the resolved agent name in every manifest",
    "Documented the <type>.yaml manifest layout under ~/.claude/manifests/",
    "- Confirmed the digest anchor is <!-- session:id turn:uuid -->",
]


@pytest.mark.parametrize("summary", _PROSE_THAT_MENTIONS_PLACEHOLDERS)
def test_prose_that_merely_mentions_a_placeholder_is_not_contamination(summary: str) -> None:
    """A session *about* template syntax produces a faithful digest that names it.

    This is the whole of #868. The guard was `_PLACEHOLDER_RE.search(summary)` — any
    angle-bracketed word up to 41 characters, anywhere — and it fired 51 times across 22
    sessions in production, every one of them on a digest like these. It was also the *only*
    detector that ever fired: `residue_token`, `template_signature` and `verbatim_overlap`
    have zero fires across the whole corpus, so narrowing this one cannot regress a signal
    that was ever observed.
    """
    assert detect_contamination(summary, raw="") is None


#: Kept deliberately. #848's fix was tuned until ~4/5 of the false positives passed **with the
#: real defect still caught**; a guard tuned until everything passes is not a guard.
_GENUINE_SCAFFOLDING = [
    "<agent>",
    "- <specific step>",
    "  1. <Phase name>",
    "**Asked:** <what the user wanted>",
    "- Reason: <reason>",
    "| <field> | <value> |",
    "- Replay from: <transcript path>",
]


@pytest.mark.parametrize("summary", _GENUINE_SCAFFOLDING)
def test_a_placeholder_in_value_position_is_still_contamination(summary: str) -> None:
    """The half that proves the guard is still a guard.

    Scaffolding puts the placeholder where the value goes, so the line is a label and little
    else. Strip the placeholders and nothing readable is left — that is the signal, and it is
    the one thing the narrowing must not give up.
    """
    assert detect_contamination(summary, raw="") == "placeholder_token"


def test_a_wholly_regurgitated_template_block_is_still_suppressed() -> None:
    """The realistic failure, not a one-liner: the model emits the skill's own shape.

    Every line here is scaffolding, so the digest must be suppressed whichever line the
    detector reaches first.
    """
    block = "\n".join(
        [
            "**Asked:** <what the user wanted>",
            "",
            "**Done:**",
            "- <specific step>",
            "- <specific step>",
            "",
            "**Open:**",
            "- <what remains>",
        ]
    )
    assert detect_contamination(block, raw="") == "placeholder_token"


def test_one_scaffolded_line_condemns_an_otherwise_clean_digest() -> None:
    """Position is judged per line, and a single value-slot line is enough.

    A digest that is nine-tenths real and ends with a template line the model failed to fill
    is still a digest with a hole in it — the narrowing is about *where* a placeholder sits,
    not about how many there are.
    """
    block = "\n".join(
        [
            "**Asked:** Whether the manifest loader tolerates an unset variable",
            "",
            "**Done:**",
            "- Reproduced the failure with <build-name> unset and captured the exit code",
            "- Confirmed every other manifest still parses",
            "- Reason: <reason>",
        ]
    )
    assert detect_contamination(block, raw="") == "placeholder_token"


def test_the_fallback_recheck_keeps_the_broad_pattern_on_purpose() -> None:
    """`build_fallback_note` still rejects a quoted line on *any* placeholder token.

    This asymmetry is deliberate and a reader will want to "fix" it. The two call sites are
    answering different questions. `detect_contamination` asks "is this digest a failed
    generation?", where a false positive costs a whole session — so it needs position.
    `build_fallback_note` asks "is it safe to echo this line of the user's transcript into a
    file?", where a false positive costs one quoted line and a false negative re-leaks
    exactly the content the guard exists to suppress. Cheap to be strict, expensive to be
    wrong, so it stays strict.
    """
    quoted = "Renamed <agent> to the real path"
    assert detect_contamination(quoted, raw="") is None
    assert "User turn:" not in build_fallback_note(f"[User]: {quoted}")
