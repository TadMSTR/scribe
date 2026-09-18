"""Tests for digest schema validation."""

from __future__ import annotations

import json

import pytest

from scribe.summarize.schema import (
    _LIST_FIELDS,
    MAX_DONE_ITEMS,
    MAX_ITEMS,
    MAX_ROLLUP_ITEMS,
    SCHEMA_VERSION,
    Digest,
    SchemaError,
    json_schema,
    parse,
)

#: Which fields reject an overflow and which shorten it. Derived from the contract itself so
#: that moving a field between the two classes updates every test that depends on the split
#: rather than leaving one asserting the old behaviour.
BOUNDED = sorted(f.name for f in _LIST_FIELDS if f.bounded)
UNBOUNDED = sorted(f.name for f in _LIST_FIELDS if not f.bounded)

VALID = {
    "asked": "Check the release workflow",
    "done": ["Ran the workflow list", "Read the logs"],
    "found": ["All three jobs passed"],
    "decisions": ["Deferred the arm64 check"],
    "open_items": ["Default branch still needs setting"],
    "artifacts": ["/repo/CHANGELOG.md"],
    "tickets": ["#843"],
}


def test_parses_a_valid_object() -> None:
    d = parse(VALID)
    assert d.asked == "Check the release workflow"
    assert d.done == ["Ran the workflow list", "Read the logs"]
    assert d.tickets == ["#843"]


def test_parses_a_json_string() -> None:
    assert parse(json.dumps(VALID)).asked == VALID["asked"]


def test_parses_a_fenced_json_block() -> None:
    """Models emit ```json fences often enough that failing on one would spend retries on
    presentation rather than on content."""
    assert parse(f"```json\n{json.dumps(VALID)}\n```").asked == VALID["asked"]


def test_parses_a_bare_fence() -> None:
    assert parse(f"```\n{json.dumps(VALID)}\n```").asked == VALID["asked"]


def test_optional_fields_may_be_absent() -> None:
    d = parse({"asked": "a", "done": ["b"]})
    assert d.found == [] and d.decisions == [] and d.tickets == []


def test_a_string_where_a_list_belongs_is_accepted() -> None:
    """Unambiguous, common, and carries the intended meaning — rejecting it would burn a
    retry on nothing."""
    assert parse({"asked": "a", "done": "just one thing"}).done == ["just one thing"]


def test_numbers_in_a_list_are_coerced() -> None:
    assert parse({"asked": "a", "done": ["x", 42]}).done == ["x", "42"]


def test_blank_entries_are_dropped() -> None:
    assert parse({"asked": "a", "done": ["x", "  ", ""]}).done == ["x"]


@pytest.mark.parametrize(
    "payload,match",
    [
        ({}, "'asked' is required"),
        ({"asked": "  "}, "'asked' is required"),
        ({"asked": 5, "done": ["x"]}, "'asked' is required"),
        ({"asked": "a"}, "'done' is required"),
        ({"asked": "a", "done": []}, "'done' is required"),
        ({"asked": "a", "done": [{"nested": 1}]}, "expected string"),
        ({"asked": "a", "done": 7}, "must be a list of strings"),
    ],
)
def test_violations_raise(payload: dict, match: str) -> None:
    with pytest.raises(SchemaError, match=match):
        parse(payload)


def test_non_json_raises() -> None:
    with pytest.raises(SchemaError, match="not valid JSON"):
        parse("I'm afraid I can't do that")


def test_empty_response_raises() -> None:
    with pytest.raises(SchemaError, match="empty response"):
        parse("   ")


def test_a_json_array_is_rejected() -> None:
    with pytest.raises(SchemaError, match="must be a JSON object"):
        parse("[1, 2, 3]")


def test_item_cap_is_enforced_on_a_bounded_field_and_truncates_an_unbounded_one() -> None:
    """The cap still bites both ways — what differs is what overflowing COSTS.

    `done` is bounded by the session's own commands, so more entries than the cap means the
    model invented some and the response is refused. `found` is free prose with no input-side
    ceiling, so the surplus is dropped and the session survives (vikunja#884). Keeping both
    halves in one test is deliberate: a change that made everything truncate would silently
    delete the guard, and this is where that shows up.
    """
    with pytest.raises(SchemaError, match="more than this session's"):
        parse({"asked": "a", "done": [f"item {i}" for i in range(MAX_DONE_ITEMS + 1)]})

    d = parse({"asked": "a", "done": ["d"], "found": [f"f{i}" for i in range(MAX_ITEMS + 1)]})
    assert len(d.found) == MAX_ITEMS
    assert d.truncated == {"found": 1}


def test_long_items_are_truncated_not_rejected() -> None:
    d = parse({"asked": "a", "done": ["x" * 5000]})
    assert len(d.done[0]) == 2000


def test_to_dict_carries_the_schema_version() -> None:
    assert Digest(asked="a", done=["b"]).to_dict()["schema_version"] == SCHEMA_VERSION


def test_json_schema_declares_the_required_fields() -> None:
    s = json_schema()
    assert s["required"] == ["asked", "done"]
    assert s["additionalProperties"] is False
    assert set(s["properties"]) == {
        "asked",
        "done",
        "found",
        "decisions",
        "open_items",
        "artifacts",
        "tickets",
    }


# --- vikunja#849: the cap is a declared contract, per field ------------------------------

_CAPPED = sorted(n for n, p in json_schema()["properties"].items() if "maxItems" in p)


def test_every_list_field_declares_a_maxitems() -> None:
    """Non-vacuity guard for the parametrised test below.

    If `json_schema` stopped emitting `maxItems`, `_CAPPED` would be empty and the
    parametrised test would collect zero cases and report green while checking nothing.
    """
    assert _CAPPED == [
        "artifacts",
        "decisions",
        "done",
        "found",
        "open_items",
        "tickets",
    ]


@pytest.mark.parametrize("name", _CAPPED)
def test_declared_maxitems_is_the_cap_parse_enforces(name: str) -> None:
    """The limit the model is shown and the limit it is judged against are one number.

    The cap is read out of `json_schema()` rather than from the constants, so this fails on
    the general form of the original defect: a schema that declares one limit while the
    validator enforces another. Before #849 the schema declared nothing at all.
    """
    cap = json_schema()["properties"][name]["maxItems"]
    at_cap = {"asked": "a", "done": ["d"], name: [f"i{i}" for i in range(cap)]}
    assert len(getattr(parse(at_cap), name)) == cap

    # One item over is where the declared number and the enforced number would diverge. Both
    # classes are checked against the SAME cap read from the schema — only the consequence of
    # passing it differs, so a drifted `maxItems` still fails here whichever class it is in.
    over = dict(at_cap, **{name: [f"i{i}" for i in range(cap + 1)]})
    if name in BOUNDED:
        with pytest.raises(SchemaError, match="more than this session's"):
            parse(over)
    else:
        assert len(getattr(parse(over), name)) == cap
        assert parse(over).truncated == {name: 1}


def test_a_rollup_backed_field_takes_the_real_over_cap_session() -> None:
    """Session aa6634a7 holds 58 tickets in its rollup; the model listed 53 and was rejected.

    Not a synthetic near-miss at cap+1 — the interesting case is the one where the *correct*
    output is large, which is the case the old blanket cap destroyed.
    """
    d = parse({"asked": "a", "done": ["x"], "tickets": [f"#{800 + i}" for i in range(58)]})
    assert len(d.tickets) == 58


def test_there_are_three_distinct_caps_not_one() -> None:
    """One constant cannot guard three classes of field, which is the argument of #849 and
    then again of #872.

    `tickets` is bounded by the log's own rollup. `found` is unbounded model prose. `done` is
    prose too, but prose *about* a bounded thing — the session's commands — so its ceiling is
    the input's, not zero and not the rollup's. A refactor that collapses any two of these
    back into one constant fails here whichever value it picks.
    """
    assert MAX_ITEMS < MAX_ROLLUP_ITEMS < MAX_DONE_ITEMS
    size = MAX_ROLLUP_ITEMS
    assert len(parse({"asked": "a", "done": ["x"], "tickets": ["#1"] * size}).tickets) == size
    # `found` at the same length is NOT accepted at that length -- it is cut back to its own,
    # lower cap. The three caps stay distinct; collapsing them would show up as this digest
    # keeping all `size` items.
    d = parse({"asked": "a", "done": ["x"], "found": [f"f{i}" for i in range(size)]})
    assert len(d.found) == MAX_ITEMS
    assert d.truncated == {"found": size - MAX_ITEMS}


@pytest.mark.parametrize("observed", [41, 42, 43, 44, 45, 46, 47, 49, 53, 65, 67])
def test_every_done_length_that_cost_a_session_now_parses(observed: int) -> None:
    """The uncensored failure tail from production, item for item.

    These are the exact `done` lengths that appeared in `field 'done' has N items` across 30
    distinct rejections — every one of them a whole session written as a placeholder instead
    of a digest (vikunja#872). No other field violated a cap once.

    Asserted individually rather than as "67 < the cap" so the test names what it is
    protecting. A cap moved back under any of these breaks a case with a session behind it.
    """
    d = parse({"asked": "a", "done": [f"d{i}" for i in range(observed)]})
    assert len(d.done) == observed


def test_the_done_cap_clears_the_measured_input_ceiling() -> None:
    """187 is `rollup.commands`' maximum across the 442 persisted event logs, and `commands`
    is the material `done` has to cover.

    This is the bound the cap was actually chosen against, and the reason it is not 100:
    38 sessions in that corpus carry more than 100 commands, so a cap of 100 would have
    reproduced #872 with a bigger number. The written digests cannot supply this number —
    they are right-censored at whatever the cap is.
    """
    assert MAX_DONE_ITEMS > 187
    d = parse({"asked": "a", "done": [f"d{i}" for i in range(187)]})
    assert len(d.done) == 187


def test_a_cap_violation_is_not_retryable() -> None:
    """It is a property of the input: every attempt sees the same log and overruns the same
    way. Retrying bought two more ~50k-token calls for an identical rejection."""
    with pytest.raises(SchemaError) as exc:
        parse({"asked": "a", "done": [f"d{i}" for i in range(MAX_DONE_ITEMS + 1)]})
    assert exc.value.retryable is False


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        {"asked": ""},
        {"asked": "a"},
        {"asked": "a", "done": [{"k": 1}]},
        "[1, 2, 3]",
    ],
)
def test_every_other_violation_stays_retryable(bad) -> None:
    """Sampling noise, not a property of the input — a fresh attempt often fixes it. The
    module's own argument for retrying schema violations still holds for these."""
    with pytest.raises(SchemaError) as exc:
        parse(bad)
    assert exc.value.retryable is True


# --- the message contract: reason fans out to sinks that are not all redacted ---------------

SENTINEL = "sk-live-CANARY-do-not-log-me"


@pytest.mark.parametrize(
    "bad",
    [
        f"{SENTINEL} is not json",
        f'["{SENTINEL}"]',
        {"asked": "", "done": [SENTINEL]},
        {"asked": SENTINEL},
        {"asked": SENTINEL, "done": [{"k": SENTINEL}]},
        {"asked": SENTINEL, "done": [[SENTINEL]]},
        {"asked": SENTINEL, "done": 7},
        {"asked": SENTINEL, "done": ["x"], "tickets": [SENTINEL] * (MAX_ROLLUP_ITEMS + 1)},
    ],
)
def test_no_violation_message_quotes_the_response(bad) -> None:
    """A `SchemaError` message must never carry response content.

    It becomes `Outcome.reason`, which fans out to three sinks and only one of them is
    redacted: the rendered placeholder is re-scrubbed by `Redactor` before `append_block`,
    but `store.record_attempt(error=...)` writes it to the state database verbatim and
    `SessionResult.errors` carries it into the run report JSON and the CLI verbatim. Noted by
    the scribe-schema-caps-2026-09 audit as the thing to remember if these messages are ever
    widened to include a snippet of the bad response. This is that memory, enforced.
    """
    with pytest.raises(SchemaError) as exc:
        parse(bad)
    assert SENTINEL not in str(exc.value)


def test_the_44_item_found_that_cost_a_session_now_parses() -> None:
    """The real rejection, at its real length.

    Session `6017c8ce` was discarded on 2026-09-17 with
    `field 'found' has 44 items, more than this session's 40 cap` — one over-long prose
    field taking a whole session's summary with it (vikunja#884). 44 rather than a
    synthetic `cap + 1`
    because the point of the number is that it was observed: nothing in the written corpus
    reaches 40, so a test tuned to the corpus would have been satisfied at 33 and proved
    nothing.
    """
    d = parse({"asked": "a", "done": ["d"], "found": [f"f{i}" for i in range(44)]})
    assert len(d.found) == MAX_ITEMS
    assert d.truncated == {"found": 4}


@pytest.mark.parametrize("name", UNBOUNDED)
def test_every_unbounded_field_truncates_not_just_the_one_that_failed(name: str) -> None:
    """`found` is the field that happened to fail first, not the only one exposed.

    `decisions` and `open_items` share its shape exactly — free prose against `MAX_ITEMS`,
    with nothing on the input side to bound them — and their written maxima (22 and 21) are
    just as right-censored as `found`'s 33 was. Fixing only the field with a corpse attached
    is how this bug reached its third occurrence.
    """
    d = parse({"asked": "a", "done": ["d"], name: [f"i{i}" for i in range(MAX_ITEMS + 7)]})
    assert len(getattr(d, name)) == MAX_ITEMS
    assert d.truncated == {name: 7}


def test_truncation_does_not_swallow_a_genuinely_malformed_response() -> None:
    """The half that proves the guard still exists.

    Truncation is for a response that is too LONG. A response that is WRONG — not JSON, not an
    object, missing `asked`, a non-string inside a list, or a bounded field past its ceiling —
    must still be refused, or "we stopped discarding digests" would just mean the validator
    was deleted. #848 and #868 were both gates that passed everything.
    """
    for bad in ("", "not json at all", "[1, 2, 3]", '{"done": ["d"]}'):
        with pytest.raises(SchemaError):
            parse(bad)
    with pytest.raises(SchemaError, match="expected string"):
        parse({"asked": "a", "done": ["d"], "found": ["ok", {"nested": "object"}]})
    with pytest.raises(SchemaError, match="must be a list"):
        parse({"asked": "a", "done": ["d"], "found": 17})


def test_a_clean_digest_records_no_truncation() -> None:
    """The overwhelmingly common case. A non-empty `truncated` on an ordinary digest would
    put a note on every block in the corpus."""
    assert parse(VALID).truncated == {}


def test_which_fields_are_unbounded_is_pinned_not_merely_derived() -> None:
    """The classification itself, stated once as a literal.

    Every other test here reads `BOUNDED`/`UNBOUNDED` off `_LIST_FIELDS`, which keeps them
    consistent but means none of them can notice a field CHANGING class — they would simply
    re-derive and pass. Worse, `test_every_unbounded_field_truncates...` is parametrized over
    `UNBOUNDED`, so emptying it collapses that test to zero cases and reports a skip rather
    than a failure. Measured, by flipping every field to bounded: 5 tests fail and that one
    silently skips.

    So the split is written out here. Moving a field between the classes is a real decision
    about whether its ceiling is knowable from the event log, and it should have to be made
    twice.
    """
    assert UNBOUNDED == ["decisions", "found", "open_items"]
    assert BOUNDED == ["artifacts", "done", "tickets"]
