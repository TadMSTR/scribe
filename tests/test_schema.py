"""Tests for digest schema validation."""

from __future__ import annotations

import json

import pytest

from scribe.summarize.schema import (
    MAX_ITEMS,
    MAX_ROLLUP_ITEMS,
    SCHEMA_VERSION,
    Digest,
    SchemaError,
    json_schema,
    parse,
)

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


def test_item_cap_is_enforced() -> None:
    with pytest.raises(SchemaError, match="more than the"):
        parse({"asked": "a", "done": [f"item {i}" for i in range(MAX_ITEMS + 1)]})


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

    over = dict(at_cap, **{name: [f"i{i}" for i in range(cap + 1)]})
    with pytest.raises(SchemaError, match="more than the"):
        parse(over)


def test_a_rollup_backed_field_takes_the_real_over_cap_session() -> None:
    """Session aa6634a7 holds 58 tickets in its rollup; the model listed 53 and was rejected.

    Not a synthetic near-miss at cap+1 — the interesting case is the one where the *correct*
    output is large, which is the case the old blanket cap destroyed.
    """
    d = parse({"asked": "a", "done": ["x"], "tickets": [f"#{800 + i}" for i in range(58)]})
    assert len(d.tickets) == 58


def test_the_free_text_cap_is_genuinely_lower_than_the_rollup_cap() -> None:
    """One constant cannot guard both classes, which is the whole argument of #849.

    `tickets` is bounded by the log's own rollup; `done` is unbounded model prose. A refactor
    that collapses the two back into a single constant fails here whichever value it picks.
    """
    assert MAX_ITEMS < MAX_ROLLUP_ITEMS
    size = MAX_ROLLUP_ITEMS
    assert len(parse({"asked": "a", "done": ["x"], "tickets": ["#1"] * size}).tickets) == size
    with pytest.raises(SchemaError, match="more than the"):
        parse({"asked": "a", "done": [f"d{i}" for i in range(size)]})


def test_a_cap_violation_is_not_retryable() -> None:
    """It is a property of the input: every attempt sees the same log and overruns the same
    way. Retrying bought two more ~50k-token calls for an identical rejection."""
    with pytest.raises(SchemaError) as exc:
        parse({"asked": "a", "done": [f"d{i}" for i in range(MAX_ITEMS + 1)]})
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
