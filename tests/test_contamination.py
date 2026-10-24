"""Tests for the ported contamination guard.

Ported from `memsearch-summarize.py`; vikunja#248 asks for this logic to be unified rather
than triplicated, so this copy is intended to be the shared one. The tests are weighted
toward the false-positive side: a guard that fires on ordinary prose suppresses real
summaries, which is a worse outcome than the leak it was built to stop.
"""

from __future__ import annotations

import pytest

from scribe.summarize.contamination import (
    RETRY_REMINDER,
    build_fallback_note,
    detect_contamination,
)


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
        ("Closed N tickets today", "residue_token"),
        ("N files changed in the PR", "residue_token"),
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


# --- vikunja#1216: the `#N (id M)` notation is not residue ----------------------------------

#: Real lines from the 2026-10-22/23 sessions that were suppressed on all nine attempts, and
#: from the convention docs. Every one of them is about the citation form rather than a
#: template echoing a count, and each was a `residue_token` before 0.16.0.
NOTATION_LINES = [
    pytest.param("bare #N is a warning", id="C1-hash-prefix"),
    pytest.param("a swapped pair #N (id M) where M is the identifier", id="C2-pair-then-bare"),
    pytest.param("id M resolves", id="C3-id-prefix"),
    pytest.param("`#M` as written, N as an id", id="C4-bare-after-notation"),
    pytest.param("still accepted as `n/N` with N above 256", id="C5-code-span-then-bare"),
    pytest.param("cite tickets as #N (id M) everywhere", id="C6-plain-pair"),
    pytest.param("- cite as `#N (id M)`, and `N/M` pairs are refused", id="code-span"),
    # Accepted (audit F-04): a `#`-prefixed template form is the same token as the notation.
    pytest.param("Merged PR #N into main", id="F-04-accepted-hash-form"),
    # A word-labelled `#N` still anchors when `(id` follows it (R2-01); here the id is a real
    # number, so nothing else on the line could.
    pytest.param("a swapped pair #N (id 525) where N is the id", id="R2-01-hash-then-id"),
    # Excused by the code span alone: nothing earlier on the line anchors it.
    pytest.param("`N/M` pairs are refused", id="code-span-only"),
]


@pytest.mark.parametrize("line", NOTATION_LINES)
def test_the_ticket_notation_is_not_residue(line: str) -> None:
    assert detect_contamination(line, raw="") is None


@pytest.mark.parametrize(
    "line",
    [
        # Option (ii) excuses a bare letter only AFTER a notation use on its line, so a count
        # letter that comes first still fires, even on a line that then writes `#N`.
        "the N in #N refers to identifier",
        "- Closed N tickets: #N, #N",
        # `XX` and the date templates are never excused, notation or not.
        "#N (id M) filed on 2026-07-XX",
        "id M reviewed XX tickets",
        "`#N` dated YYYY-MM-DD",
        # A notation use on ANOTHER line excuses nothing.
        "cite as #N (id M)\n- wrote N docs",
        # Audit F-02: inline code that only CONTAINS a standalone letter is not notation, so
        # it anchors nothing; and an `N/M` pair after a real anchor still fires.
        "ran `M-x compile` then wrote N docs",
        "ran `--max N` and closed N tickets",
        "see #N, progress N/M complete",
        # Audit F-04: the notation writes `id` in lowercase, so `ID M` is a count letter.
        "ID M tickets were closed",
        # Audit R2-01: a word-labelled `#N` is excused itself but anchors nothing after it.
        "Merged PR #N, then closed N tickets",
        "Step #N: wrote N docs",
    ],
)
def test_the_notation_exemption_is_bounded(line: str) -> None:
    """The positive half of option (ii): the exemption is per line and positional, so it
    cannot be used to smuggle a count letter past the detector."""
    assert detect_contamination(line, raw="") == "residue_token"


def test_a_count_letter_split_across_lines_still_fires() -> None:
    """The detector still scans the whole digest, so `\\s+` in the lookahead keeps crossing a
    newline exactly as before 0.16.0."""
    assert detect_contamination("- wrote N\ndocs", raw="") == "residue_token"


def test_the_fallback_keeps_a_first_line_that_cites_the_notation() -> None:
    """F1. The detector and the re-check share `_has_residue` (F-05), so a user line that
    quotes the convention survives the fallback like it survives the detector."""
    note = build_fallback_note("[User]: cite as #N (id M)")
    assert "User turn: cite as #N (id M)" in note
    # R2-01: the same narrowing reaches the fallback through the shared helper.
    assert "User turn:" not in build_fallback_note("[User]: Merged PR #N, then closed N tickets")
    # C6's line never fired, so it cannot tell the shared helper from the old regexes. This
    # one could: it was dropped from every fallback note before 0.16.0.
    assert "User turn: bare #N is a warning" in build_fallback_note("[User]: bare #N is a warning")


def test_the_retry_reminder_no_longer_forbids_the_notation() -> None:
    """The old reminder banned N and M outright, so attempts 2 and 3 of a session about the
    notation failed exactly as attempt 1 did."""
    assert "`#N (id M)`" in RETRY_REMINDER
    assert "count letters like N, M, or XX" in RETRY_REMINDER


def test_a_long_line_of_excused_hits_is_scanned_in_linear_time() -> None:
    """IV-47 (0.16.0 pre-audit). The first cut re-scanned the whole line for every hit, so
    100 KB of excused hits took 17 s; the guard runs on model output. Generous bound: the
    quadratic version needs minutes here, the linear one well under a second."""
    import time

    for line in ("#N a " * 40_000, "`N a` " * 40_000, "id M x " * 40_000):
        t = time.perf_counter()
        assert detect_contamination(line, raw="") is None
        assert time.perf_counter() - t < 2.0


def test_an_unclosed_backtick_does_not_make_the_anchor_scan_quadratic() -> None:
    """Audit F-01. The code-span anchor used to be a regex alternative that, after one
    unclosed backtick, tried every later `N` as a pivot and scanned to the end of the line
    each time: 100 KB took 3.3 s. The audit's own line: the first hit is unexcused, but
    deciding that computes the line's facts, and that was the quadratic part. No `#N` up
    front, or the regex's leftmost alternative matches at once and the case is never run."""
    import time

    line = "`" + "N x " * 25_000
    t = time.perf_counter()
    assert detect_contamination(line, raw="") == "residue_token"
    assert time.perf_counter() - t < 2.0
