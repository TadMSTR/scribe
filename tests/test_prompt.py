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
import re

import pytest

from scribe.extract.models import EventLog, Rollup, Stats, Turn
from scribe.summarize.prompt import SYSTEM, build_system_prompt, build_user_prompt
from scribe.summarize.schema import caps_for, json_schema

DECLARED_CAPS = {p["maxItems"] for p in json_schema()["properties"].values() if "maxItems" in p}

#: The caps a real session now gets, since `caps_for` derives them per log. Shaped like
#: `6017c8ce`, whose digest was discarded at 103 tickets against a 104-ticket rollup.
DERIVED = caps_for(
    EventLog(
        session_id="s1",
        transcript_path="/p/t.jsonl",
        rollup=Rollup(
            tickets=[f"#{i}" for i in range(104)],
            files_written=[f"/p/f{i}.py" for i in range(68)],
        ),
        stats=Stats(tool_events=672),
    )
)

#: Every drift test below runs against the globals AND against a derived set. `None` is the
#: no-log fallback and still the path ~20 bare test calls take; the derived set is the live
#: one. Parameterised rather than rewritten on purpose — these assertions caught #849 and
#: #872 in their existing form, and replacing them would retire that coverage to add a case.
CAP_SETS = [None, DERIVED]


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
    per-field split is the substance of the fix — so assert the split exists first.

    Three since #872, not two: `done` moved off the free-text cap onto its own.
    """
    assert len(DECLARED_CAPS) == 3


@pytest.mark.parametrize("caps", CAP_SETS)
def test_the_system_prompt_states_every_declared_limit(caps) -> None:
    """A limit the model is never given is a limit it cannot respect.

    This catches drift in one direction: a cap changed in `schema.py` while the prompt keeps
    the old number. It does not verify that each field is paired with the right cap in the
    prose — `test_declared_maxitems_is_the_cap_parse_enforces` covers the pairing on the
    enforcement side, and the schema itself travels with the user prompt.
    """
    rendered = build_system_prompt(caps)
    declared = {p["maxItems"] for p in json_schema(caps)["properties"].values() if "maxItems" in p}
    for cap in declared:
        assert str(cap) in rendered, f"the system prompt never mentions the {cap}-item limit"


def test_the_system_prompt_says_what_happens_if_the_limit_is_exceeded() -> None:
    """Stating a number is not stating a contract. The model has to be told that going over
    loses the whole summary, otherwise "list the ticket references in the log" still reads as
    the stronger instruction."""
    assert "rejected" in SYSTEM


@pytest.mark.parametrize("caps", CAP_SETS)
def test_the_user_prompt_carries_the_schema_with_its_caps(caps) -> None:
    """The schema travels with every call, so the field/cap pairing reaches the model as data
    rather than as prose it has to parse."""
    rendered = build_user_prompt(_log(), caps)
    schema = json_schema(caps)
    assert json.dumps(schema, ensure_ascii=False) in rendered
    assert schema["properties"]["tickets"]["maxItems"] != schema["properties"]["done"]["maxItems"]


@pytest.mark.parametrize("caps", CAP_SETS)
def test_the_prompt_pairs_each_field_with_its_own_cap(caps) -> None:
    """Not just "the numbers appear somewhere" — the right number against the right field.

    The caps clause used to be a hand-written sentence naming four fields and one number.
    That is how #872 could have shipped silently: `done` moves from 40 to 200 in `schema.py`
    and the prose still says 40, so the model is told a limit, complies with it, and the
    validator — which now permits 200 — is not the thing that rejects it. The model just
    writes a shorter digest than the log supports, every run, invisibly.

    This reads the pairing back out of `SYSTEM`'s own text and checks it against
    `json_schema()`, so the two halves are compared with each other rather than each with
    itself. It fails on a clause that goes stale *and* on one that is quietly dropped.
    """
    rendered = build_system_prompt(caps)
    clause = re.search(r"declared as maxItems in the schema below: (.+?)\. Do not", rendered)
    assert clause, "the system prompt no longer states the caps at all"

    stated: dict[str, int] = {}
    for cap, subject in re.findall(
        r"(\d+) entries for ([a-z_, ]+?(?: and [a-z_]+)?)(?=;|$)", clause.group(1)
    ):
        for name in re.split(r",\s*|\s+and\s+", subject.strip()):
            stated[name] = int(cap)

    declared = {
        name: prop["maxItems"]
        for name, prop in json_schema(caps)["properties"].items()
        if "maxItems" in prop
    }
    assert stated == declared
