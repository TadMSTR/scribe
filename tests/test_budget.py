"""Tests for the byte-budget degradation ladder.

The ordering is a value judgement encoded in code, so it is asserted directly rather than
inferred from a final size: a ladder that reaches the budget by discarding the wrong thing
still reaches the budget.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from scribe.extract import extract
from scribe.extract.models import EventLog, Rollup, Stats, ToolEvent, Turn
from scribe.extract.parser import _finalise
from scribe.extract.redact import Redactor

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import budget_profile

#: The default `extract()` budget, which the guard below is about.
DEFAULT_BUDGET = 200_000


def _log(n_events: int = 40, filler: int = 500) -> EventLog:
    turn = Turn(turn_uuid="u1", index=0, user_text="U" * filler)
    turn.assistant_text = ["A" * filler]
    for i in range(n_events):
        turn.events.append(
            ToolEvent(
                seq=i,
                tool="Bash",
                kind="bash",
                target="T" * filler,
                args_digest="G" * filler,
                result_digest="R" * filler,
                ok=True,
            )
        )
    return EventLog(
        session_id="s",
        transcript_path="/tmp/x.jsonl",
        turns=[turn],
        rollup=Rollup(),
        stats=Stats(),
    )


def _at_budget(budget: int) -> EventLog:
    log = _log()
    _finalise(log, Redactor(), budget)
    return log


def test_no_degradation_when_under_budget() -> None:
    log = _at_budget(10_000_000)
    assert log.stats.degradation_level == 0
    assert log.stats.fields_cleared == 0
    assert all(e.result_digest for e in log.turns[0].events)


def test_level_1_drops_result_digests_first() -> None:
    """Results are the largest and lowest-value-per-byte category — 261 KB of the 523 KB
    reference session — so they go before anything else."""
    log = _at_budget(62_000)
    assert log.stats.degradation_level == 1
    events = log.turns[0].events
    assert all(not e.result_digest for e in events)
    assert all(e.args_digest for e in events), "args must survive level 1"
    assert all(e.target for e in events), "targets must survive level 1"


def test_level_2_drops_args_but_keeps_targets() -> None:
    log = _at_budget(42_000)
    assert log.stats.degradation_level == 2
    events = log.turns[0].events
    assert all(not e.args_digest for e in events)
    assert all(e.target for e in events)


def test_targets_are_tightened_never_dropped() -> None:
    """The correction the measurement forced.

    A literal reading of "drop arguments before tool names" kept degrading into the target,
    and for a Bash event the target IS the command line — the single most valuable fact in
    the log. At the very bottom of the ladder a target is 80 characters, never absent.
    """
    log = _at_budget(1_000)
    assert log.stats.degradation_level == 4
    for e in log.turns[0].events:
        assert e.target, "target must never be emptied"
        assert len(e.target) <= 81  # 80 + the elision marker


def test_tool_identity_always_survives() -> None:
    log = _at_budget(1)
    for e in log.turns[0].events:
        assert e.tool == "Bash"
        assert e.kind == "bash"
        assert e.ok is True


def test_user_and_assistant_text_are_never_dropped() -> None:
    """The one invariant the plan states absolutely."""
    for budget in (1, 1_000, 42_000, 62_000):
        log = _at_budget(budget)
        assert log.turns[0].user_text == "U" * 500
        assert log.turns[0].assistant_text == ["A" * 500]


def test_no_event_is_ever_removed() -> None:
    for budget in (1, 5_000, 50_000):
        log = _at_budget(budget)
        assert len(log.turns[0].events) == 40


def test_budget_exceeded_is_reported_not_hidden() -> None:
    """One session in the 429-file corpus has a text-only floor of 143,410 chars. Since text
    is never droppable, no ladder brings it under 120,000 — and silently shipping a stripped
    log for it would be indistinguishable from a session that did nothing."""
    log = _log(n_events=0, filler=200_000)
    _finalise(log, Redactor(), 1_000)
    assert log.stats.budget_exceeded is True
    assert log.stats.extracted_chars > 1_000


def test_budget_zero_disables_degradation() -> None:
    log = _at_budget(0)
    assert log.stats.degradation_level == 0
    assert log.stats.budget_exceeded is False
    assert all(e.result_digest for e in log.turns[0].events)


@pytest.mark.parametrize("budget", [1, 1_000, 42_000, 62_000, 10_000_000])
def test_result_fits_the_declared_size(budget: int) -> None:
    log = _at_budget(budget)
    assert log.stats.extracted_chars == len(json.dumps(log.content_dict(), ensure_ascii=False))
    assert log.stats.token_estimate == (log.stats.extracted_chars + 3) // 4


@pytest.fixture
def reference(tmp_path) -> Path:
    """A synthetic transcript with the reference session's size profile -- see
    `tests/fixtures/budget_profile.py`. Runs everywhere, CI included, and never expires."""
    return budget_profile.synthesize(budget_profile.load(), tmp_path / "reference.jsonl")


def test_the_reference_profile_degrades_at_most_one_level_at_default_budget(reference) -> None:
    """Regression guard on the default (#966 (id 1049)). Measured across 429 real transcripts:
    74% need no degradation at all and the busiest reaches level 4. The reference session sits
    at 1, and a change that pushes it further has changed the extractor's size profile.

    Through `extract()`, not `_finalise`: the ladder's input is the transcript, and the size of
    what the parser makes of it is half of what this guards."""
    log = extract(reference)
    assert extract(reference).stats.secrets_redacted == 0, "filler must not trip the redactor"
    assert log.stats.degradation_level <= 1
    assert log.stats.budget_exceeded is False


def test_the_reference_profile_is_load_bearing(reference) -> None:
    """A fixture sitting at level 0 with room to spare would pass the guard above whatever the
    ladder did. This one needs the first rung, and has little headroom past it: at 70% of the
    default budget, as at the real session, it falls further."""
    assert extract(reference, max_session_chars=0).stats.extracted_chars > DEFAULT_BUDGET
    assert extract(reference).stats.degradation_level == 1
    assert (
        extract(reference, max_session_chars=int(DEFAULT_BUDGET * 0.7)).stats.degradation_level >= 2
    )


def test_the_profile_still_matches_what_it_was_derived_from(reference) -> None:
    """The synthetic transcript must serialize to the reference's size, or it profiles nothing.
    Within 2%: filler keys and the rollups the profile does not carry (urls, tickets) differ."""
    expected = budget_profile.load()["undegraded_chars"]
    got = extract(reference, max_session_chars=0).stats.extracted_chars
    assert abs(got - expected) / expected < 0.02, (got, expected)


#: Set where the reference transcript is expected to exist, so its absence FAILS instead of
#: skipping. Same idea as `SCRIBE_REQUIRE_SCHEMA_PROVENANCE`; never set in CI, which cannot have it.
REQUIRE_REFERENCE_ENV = "SCRIBE_REQUIRE_BUDGET_REFERENCE"


def test_real_session_degrades_at_most_one_level_at_default_budget(real_home) -> None:
    """The same guard on the real transcript, kept as a second check while it exists. The
    synthetic one above is the guard of record; this one says whether the profile still
    describes the session it was taken from.

    Claude Code deletes a transcript `cleanupPeriodDays` after its last write (default 30), so
    this skips once the file ages out -- on forge, around 2026-11-12. Delete it then.

    The path is built from the real home rather than written out, so the host that holds the
    reference transcript still runs this and no username is committed to reach it. Claude Code
    names a project directory after its absolute path with `/` flattened to `-`."""
    flattened = str(real_home).replace("/", "-") + "--claude-projects-research"
    path = (
        real_home
        / ".claude"
        / "projects"
        / flattened
        / "2dcbe653-0a16-4675-8004-34f628047a15.jsonl"
    )
    if not path.exists():
        if os.environ.get(REQUIRE_REFERENCE_ENV):
            pytest.fail(f"reference transcript missing but {REQUIRE_REFERENCE_ENV} is set: {path}")
        pytest.skip("reference transcript not present on this host")
    log = extract(path)
    assert log.stats.degradation_level <= 1
    assert log.stats.budget_exceeded is False
