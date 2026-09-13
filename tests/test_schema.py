"""Tests for digest schema validation."""

from __future__ import annotations

import json

import pytest

from scribe.summarize.schema import (
    MAX_ITEMS,
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
