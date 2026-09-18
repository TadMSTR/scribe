"""Tests for per-session cap derivation (vikunja#901).

The design rests on one property — **the cap declared to the model is the cap the validator
enforces** — and on one safety argument: the derived cap is `max(derived, global)`, so it can
only ever move up, and no session can be rejected that is not rejected today.

**A note on what cannot be proved by replaying the corpus.** The build plan asked for every
written digest block to be replayed under the new caps with zero rejections. That check passes,
and it passes *vacuously*: every written block was accepted under the old global, so it is
below the floor by construction, and the floor is the old global. Measured 2026-09-18, the
largest written block in the corpus is `done` 58 (floor 200), `tickets` 87 (floor 100),
`artifacts` 53 (floor 100) — the headroom is never consulted. That is AGENTS.md invariant 11
one more time: the written corpus is right-censored by the cap, so it cannot be used to
validate a change to the cap.

So the tests below assert the *construction* instead, which is what actually carries the
safety claim, plus the one genuinely uncensored observation the corpus does hold: session
`6017c8ce`, whose 103-item `tickets` response was rejected against a 104-ticket rollup and
whose placeholder is still on disk. `test_the_live_rejection_*` pair is two-sided on purpose —
the "after" assertion means nothing unless the "before" one fails first.
"""

from __future__ import annotations

import pytest

from scribe.extract.models import EventLog, Rollup, Stats
from scribe.summarize.prompt import build_system_prompt
from scribe.summarize.render import render_digest
from scribe.summarize.schema import (
    _LIST_FIELDS,
    MAX_DERIVED_MULTIPLE,
    MAX_ITEMS,
    SchemaError,
    caps_for,
    field_caps,
    json_schema,
    parse,
)

DERIVED = sorted(f.name for f in _LIST_FIELDS if f.derive is not None)
PROSE = sorted(f.name for f in _LIST_FIELDS if f.derive is None)


def _log(*, tool_events: int = 0, tickets: int = 0, files: int = 0) -> EventLog:
    return EventLog(
        session_id="s1",
        transcript_path="/p/t.jsonl",
        rollup=Rollup(
            tickets=[f"#{i}" for i in range(tickets)],
            files_written=[f"/p/f{i}.py" for i in range(files)],
        ),
        stats=Stats(tool_events=tool_events),
    )


#: The shape of session `6017c8ce` — the one whose digest was actually discarded. Reproduced
#: as counts rather than by reading the operator's event log: the numbers are the whole
#: content of the test case, and a test that needs a file outside the repo is a test that
#: stops running the day that file ages out.
LIVE_REJECTION = _log(tool_events=672, tickets=104, files=68)
#: What the model returned for it, in length. 103 faithful ticket references against a rollup
#: of 104 — the model was right and the digest was thrown away for it.
LIVE_RESPONSE = {"asked": "x", "done": ["a"], "tickets": [f"#{i}" for i in range(103)]}


def test_the_live_rejection_still_raises_under_the_old_globals() -> None:
    """The gate control. If this ever stops failing, the test below proves nothing.

    `parse` with no caps is exactly the production path before this build.
    """
    with pytest.raises(SchemaError) as exc:
        parse(LIVE_RESPONSE)
    assert not exc.value.retryable
    assert "tickets" in str(exc.value)


def test_the_live_rejection_parses_under_its_own_derived_caps() -> None:
    digest = parse(LIVE_RESPONSE, caps_for(LIVE_REJECTION))
    assert len(digest.tickets) == 103
    assert digest.truncated == {}, "a derived cap must not silently shorten instead"


def test_the_derived_cap_sits_above_the_count_not_at_it() -> None:
    """The load-bearing constraint, and the one that fails in the direction that looks right.

    A cap set equal to the rollup count would pass every "no rejection" test and then make the
    model shed: measured on `aa6634a7` (58 tickets in the rollup), a declared cap of 40
    produced 27/35/28 across three identical runs while 100 produced 53 every time. So assert
    a margin, not merely sufficiency.
    """
    caps = caps_for(LIVE_REJECTION)
    assert caps["tickets"] > len(LIVE_REJECTION.rollup.tickets)
    assert caps["tickets"] >= len(LIVE_REJECTION.rollup.tickets) * 1.25


@pytest.mark.parametrize("name", DERIVED)
def test_a_derived_cap_never_falls_below_the_global(name: str) -> None:
    """The safety property the whole build rests on: the cap only ever moves up.

    This is why no session can be lost that is not lost today, and it holds for an empty log,
    a tiny one and a huge one alike.
    """
    globals_ = field_caps()
    for log in (_log(), _log(tool_events=1, tickets=1, files=1), LIVE_REJECTION):
        assert caps_for(log)[name] >= globals_[name]


def test_a_small_session_is_not_squeezed() -> None:
    """A log with two tickets must not get a cap of two or three.

    Without the floor, the derivation would be strictly worse than the global it replaced for
    the overwhelming majority of sessions — 99% of the corpus sits below every floor.
    """
    caps = caps_for(_log(tool_events=4, tickets=2, files=1))
    assert caps == field_caps()


@pytest.mark.parametrize("name", PROSE)
def test_the_prose_fields_do_not_move(name: str) -> None:
    """`found`, `decisions` and `open_items` have no countable input, so nothing derives them.

    A regression here would be worse than the bug being fixed: these fields truncate rather
    than raise, and wiring them to a rollup would convert a shortened digest back into a lost
    session (vikunja#884).
    """
    assert caps_for(LIVE_REJECTION)[name] == MAX_ITEMS


def test_no_log_means_the_globals() -> None:
    """`caps_for(None)` is the fallback, and `parse(raw)` with no caps must equal it."""
    assert caps_for(None) == field_caps()


@pytest.mark.parametrize("name", DERIVED)
def test_the_declared_cap_is_the_cap_parse_enforces_for_a_derived_log(name: str) -> None:
    """The property the design rests on, asserted for DERIVED caps rather than the defaults.

    Checking the schema against itself would pass with `parse` reading the globals and
    `json_schema` reading the derived dict — which is #849 exactly, a response rejected
    against a limit it was never shown. So drive `parse` with one more item than the schema
    declares and require it to object.
    """
    caps = caps_for(LIVE_REJECTION)
    declared = json_schema(caps)["properties"][name]["maxItems"]
    assert declared == caps[name]

    over = {"asked": "x", "done": ["a"], name: [f"item {i}" for i in range(declared + 1)]}
    if name == "done":
        over["done"] = [f"item {i}" for i in range(declared + 1)]
    with pytest.raises(SchemaError):
        parse(over, caps)


def test_the_system_prompt_states_the_derived_limit_not_the_global() -> None:
    """The third copy of the contract, and the one that drifts silently.

    `done` moved 40 -> 200 in #872 while a hand-written prompt would still have said 40. The
    same hazard returns the moment the cap varies per session: the model is told 100, complies,
    and writes a shorter digest than the log supports with nothing rejecting it to show the
    drift. Nothing in the JSON schema catches that, because the model obeyed the prose.
    """
    caps = caps_for(LIVE_REJECTION)
    rendered = build_system_prompt(caps)
    assert str(caps["tickets"]) in rendered
    assert "100 entries for" not in rendered


def test_the_truncation_note_quotes_the_cap_that_actually_fired() -> None:
    """The note exists so a reader can see how much was lost and against what.

    Rendered from the constants it would say "at the 40-item cap" under a session whose cap
    was 156 — a statement about the code that is false about the run.
    """
    caps = dict(field_caps(), found=7)
    digest = parse(
        {"asked": "x", "done": ["a"], "found": [f"f{i}" for i in range(9)]},
        caps,
    )
    assert digest.truncated == {"found": 2}
    assert "7-item cap" in render_digest(digest, caps=caps)


@pytest.mark.parametrize("name", DERIVED)
def test_a_derived_cap_is_clamped_to_a_multiple_of_its_floor(name: str) -> None:
    """The 2026-09-18 audit's one Low finding: the denominators come from a file on disk.

    Anyone able to write under the event-log directory could inflate `stats.tool_events` and
    hand themselves an unbounded `done` cap — removing the `bounded` invention guard for that
    session rather than merely widening it. The clamp bounds the damage at 10x.

    The denominators here are deliberately absurd (far past anything the corpus contains), so
    this test exercises the clamp rather than the floor. `test_the_clamp_does_not_fire_on_a
    _realistic_session` is the other half.
    """
    floor = field_caps()[name]
    huge = _log(tool_events=1_000_000, tickets=1_000_000, files=1_000_000)
    assert caps_for(huge)[name] == floor * MAX_DERIVED_MULTIPLE


def test_the_clamp_never_drops_a_cap_below_its_floor() -> None:
    """A ceiling that could undercut the floor would reintroduce the bug being fixed — a
    session rejected for a count the log plainly supports. `min` is applied to a value already
    `max`ed against the floor, and the multiple is >= 1, so this holds; asserted because the
    two bounds are written on different lines and could drift apart."""
    for log in (_log(), _log(tool_events=1, tickets=1, files=1), LIVE_REJECTION):
        for name, cap in caps_for(log).items():
            assert cap >= field_caps()[name]


def test_the_clamp_does_not_fire_on_a_realistic_session() -> None:
    """Non-vacuity, and the reason 10 was chosen rather than something tighter.

    `6017c8ce` is the largest session in the corpus — 672 tool events, 104 tickets, 68
    artifacts. If the clamp bound *it*, the constant would be doing measurement's job, and a
    future busy session would be silently shortened by a number nobody derived.
    """
    caps = caps_for(LIVE_REJECTION)
    for name in DERIVED:
        ceiling = field_caps()[name] * MAX_DERIVED_MULTIPLE
        assert caps[name] < ceiling, f"{name} is at the clamp; the constant is too tight"
