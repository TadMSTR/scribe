"""Structural tests for the extractor.

These assert the things the old pipeline got wrong: that tool traffic survives extraction
at all, that a turn is a turn and not a tool call, and that the meta/injected guards ported
from `parse-transcript.sh` still hold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scribe.extract import extract
from scribe.extract.models import (
    KIND_BASH,
    KIND_FILE_WRITE,
    KIND_MCP,
    SCHEMA_VERSION,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def structural():
    return extract(FIXTURES / "transcript-structural.jsonl")


@pytest.fixture
def bearer():
    return extract(FIXTURES / "transcript-with-bearer.jsonl")


def test_tool_traffic_survives_extraction(bearer) -> None:
    """The defect this component exists to fix.

    `parse-transcript.sh :: format_turn()` skips tool_use and tool_result, so the pipeline
    being replaced captures ZERO tool events from any transcript. Any number greater than
    zero here is the whole thesis, so the assertion is deliberately about the count rather
    than about a particular tool.
    """
    assert bearer.stats.tool_events == 2
    assert bearer.stats.tool_results == 2
    tools = [e.tool for t in bearer.turns for e in t.events]
    assert tools == ["Bash", "Read"]


def test_meta_records_are_dropped(structural) -> None:
    """isMeta guard, ported from the hook. A skill load is the entire skill markdown."""
    assert structural.stats.skipped_meta == 1
    blob = json.dumps(structural.to_dict())
    assert "ENTIRE SKILL BODY" not in blob


def test_injected_prefixes_are_dropped(structural) -> None:
    """The belt-and-braces guard for harness turns that arrive WITHOUT isMeta set."""
    assert structural.stats.skipped_injected == 1
    assert "<command-name>" not in json.dumps(structural.to_dict())


def test_turns_are_keyed_on_user_message_uuid(structural) -> None:
    """Idempotency key. The pipeline being replaced keyed on (session_id, HH:MM), which
    collided 62 times across 3,539 blocks (vikunja#844)."""
    assert [t.turn_uuid for t in structural.turns] == ["s1", "s2"]
    assert len({t.turn_uuid for t in structural.turns}) == len(structural.turns)


def test_tool_results_do_not_open_a_new_turn(structural) -> None:
    """A user record carrying tool_result blocks is the harness returning output, not a
    person speaking. Treating it as a boundary fragments a session into one turn per call."""
    assert structural.stats.turns == 2
    assert len(structural.turns[0].events) == 3


def test_results_pair_with_their_call_by_id(structural) -> None:
    by_tool = {e.tool: e for e in structural.turns[0].events}
    assert by_tool["Bash"].ok is True
    assert by_tool["Write"].ok is False
    assert "EACCES" in by_tool["Write"].error


def test_orphan_result_is_counted_not_silently_dropped(structural) -> None:
    assert structural.stats.orphan_results == 1


def test_malformed_lines_are_counted_not_fatal(structural) -> None:
    assert structural.stats.records_unparsable == 1
    assert structural.stats.turns == 2


def test_kinds_are_classified(structural) -> None:
    kinds = {e.tool: e.kind for e in structural.turns[0].events}
    assert kinds["Bash"] == KIND_BASH
    assert kinds["Write"] == KIND_FILE_WRITE
    assert kinds["mcp__scoped-mcp__vikunja-mcp_task_get"] == KIND_MCP


def test_rollups_are_derived(structural) -> None:
    """Every field here is something the old prompt asked the model to produce from an
    input that did not contain it."""
    r = structural.rollup
    assert "/repo/src/mod.py" in r.files_written
    assert any("git -C /repo checkout" in c for c in r.commands)
    assert "mcp__scoped-mcp__vikunja-mcp_task_get" in r.mcp_tools
    assert "checkout" in r.git_refs and "9f8e7d6" in r.git_refs
    assert "#843" in r.tickets
    assert any("Write" in f for f in r.failures)


def test_ticket_refs_come_from_both_shapes(structural) -> None:
    """A bare #843 in prose and a vikunja task URL are the same ticket."""
    assert "#843" in structural.turns[0].rollup.tickets
    assert "#926" in structural.turns[1].rollup.tickets


def test_git_refs_exclude_non_git_hashes() -> None:
    """A bare 7-40 hex scan also matches image digests. A rollup claiming `18cfe3ef` is a
    git ref hands the groundedness gate a fact that is false about the world."""
    from scribe.extract.parser import _collect_git_refs

    assert _collect_git_refs("docker pull postgres@sha256:18cfe3ef0011aabb") == []
    assert "9f8e7d6" in _collect_git_refs("git show 9f8e7d6")


def test_session_metadata_is_captured(bearer) -> None:
    assert bearer.session_id == "sess-bearer"
    assert bearer.cwd == "/home/user/proj"
    assert bearer.git_branch == "main"
    assert bearer.started_at and bearer.ended_at


def test_agent_is_recovered_from_project_dir() -> None:
    from scribe.extract.parser import _agent_from_project_dir

    assert _agent_from_project_dir("-home-ted--claude-projects-research") == "research"
    assert _agent_from_project_dir("-home-ted--claude-projects-developer") == "developer"
    assert _agent_from_project_dir("something-else") == ""


def test_thinking_blocks_are_counted_not_included(bearer) -> None:
    """Thinking is the one category worth counting but not carrying: it is large, it is not
    a record of what happened, and it is the least grounded text in the transcript."""
    assert bearer.stats.thinking_blocks == 1
    assert "I should curl the endpoint" not in json.dumps(bearer.to_dict())


def test_empty_transcript_yields_an_empty_log() -> None:
    log = extract(FIXTURES / "transcript-empty.jsonl")
    assert log.stats.records == 0
    assert log.turns == []
    assert log.to_dict()["schema_version"] == SCHEMA_VERSION


def test_document_is_json_serialisable(structural) -> None:
    json.dumps(structural.to_dict(), ensure_ascii=False)


def test_extract_never_writes_to_the_transcript(tmp_path) -> None:
    """Transcripts are the only durable record of what happened and Claude Code already
    caps them at 30 days (vikunja#778). Read-only is a hard invariant, so it is asserted
    on both mtime and content rather than trusted to code review."""
    src = (FIXTURES / "transcript-structural.jsonl").read_bytes()
    target = tmp_path / "t.jsonl"
    target.write_bytes(src)
    before_mtime = target.stat().st_mtime_ns
    extract(target)
    assert target.read_bytes() == src
    assert target.stat().st_mtime_ns == before_mtime
