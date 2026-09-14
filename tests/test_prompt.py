"""Tests for the system prompt's half of the digest contract.

The prompt and the validator are two statements of one contract, and #849 is what happens
when they disagree: `prompt.py` asked for "ticket references that appear in the log" with no
limit stated anywhere, `json_schema()` emitted no `maxItems`, and `parse` then rejected a
response that had done exactly what it was asked. The model was punished for complying.

These tests read the limits out of `json_schema()` rather than out of the constants, so they
check the two halves against each other rather than each against itself.
"""

from __future__ import annotations

import json

from scribe.extract.models import EventLog, Rollup, Stats, Turn
from scribe.summarize.prompt import SYSTEM, build_user_prompt
from scribe.summarize.schema import json_schema

DECLARED_CAPS = {p["maxItems"] for p in json_schema()["properties"].values() if "maxItems" in p}


def _log() -> EventLog:
    turn = Turn(turn_uuid="u1", index=0, user_text="Check the release workflow")
    return EventLog(
        session_id="s1",
        transcript_path="/p/t.jsonl",
        turns=[turn],
        rollup=Rollup(),
        stats=Stats(),
    )


def test_there_is_more_than_one_declared_cap() -> None:
    """Non-vacuity guard. With a single blanket cap the test below would still pass, and the
    per-field split is the substance of the fix — so assert the split exists first."""
    assert len(DECLARED_CAPS) == 2


def test_the_system_prompt_states_every_declared_limit() -> None:
    """A limit the model is never given is a limit it cannot respect.

    This catches drift in one direction: a cap changed in `schema.py` while the prompt keeps
    the old number. It does not verify that each field is paired with the right cap in the
    prose — `test_declared_maxitems_is_the_cap_parse_enforces` covers the pairing on the
    enforcement side, and the schema itself travels with the user prompt.
    """
    for cap in DECLARED_CAPS:
        assert str(cap) in SYSTEM, f"the system prompt never mentions the {cap}-item limit"


def test_the_system_prompt_says_what_happens_if_the_limit_is_exceeded() -> None:
    """Stating a number is not stating a contract. The model has to be told that going over
    loses the whole summary, otherwise "list the ticket references in the log" still reads as
    the stronger instruction."""
    assert "rejected" in SYSTEM


def test_the_user_prompt_carries_the_schema_with_its_caps() -> None:
    """The schema travels with every call, so the field/cap pairing reaches the model as data
    rather than as prose it has to parse."""
    rendered = build_user_prompt(_log())
    schema = json_schema()
    assert json.dumps(schema, ensure_ascii=False) in rendered
    assert schema["properties"]["tickets"]["maxItems"] != schema["properties"]["done"]["maxItems"]
