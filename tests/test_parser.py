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


@pytest.fixture
def compact():
    return extract(FIXTURES / "transcript-compact-boundary.jsonl")


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


def test_a_task_url_is_a_ticket_on_any_vikunja_host_and_no_other() -> None:
    """The URL form is matched by shape, not by one deployment's hostname -- and the shape is
    what stops it reading every `/tasks/N` on the web as a ticket."""
    from scribe.extract.parser import _collect_refs

    assert _collect_refs("see https://vikunja.example.com/tasks/926")[0] == ["#926"]
    assert _collect_refs("see https://vikunja.tracker.example/tasks/17")[0] == ["#17"]
    assert _collect_refs("see https://tracker.example/tasks/926")[0] == []
    assert _collect_refs("see https://notvikunja.example.com/tasks/926")[0] == []
    # `vikunja` as a later label, or after a hyphen, is not the first label. A `\b` anchor
    # matched all three of these.
    assert _collect_refs("see https://try.vikunja.io/tasks/5")[0] == []
    assert _collect_refs("see https://my.vikunja.example/tasks/7")[0] == []
    assert _collect_refs("see https://x-vikunja.example/tasks/3")[0] == []
    assert _collect_refs("(vikunja.example.com/tasks/12)")[0] == ["#12"]


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

    assert _agent_from_project_dir("-home-user--claude-projects-research") == "research"
    assert _agent_from_project_dir("-home-user--claude-projects-developer") == "developer"
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


# --------------------------------------------------------------------------------------
# Agent attribution
# --------------------------------------------------------------------------------------


def _transcript(dir_name: str, *, cwd: str, tmp_path: Path) -> Path:
    """One minimal transcript inside a project directory named `dir_name`."""
    d = tmp_path / dir_name
    d.mkdir(parents=True)
    p = d / "0e269db6-a164-4723-82b5-e120e8518746.jsonl"
    p.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "0e269db6-a164-4723-82b5-e120e8518746",
                "timestamp": "2026-09-15T14:02:00Z",
                "cwd": cwd,
                "message": {"role": "user", "content": "do the thing"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return p


def test_a_hyphenated_agent_is_attributed_from_cwd(tmp_path) -> None:
    """Claude Code's flattened directory name cannot be inverted: a separator and a hyphen
    inside a name both become `-`. The session's own `cwd` keeps its separators and is
    therefore exact -- and the parser was already reading it, just not using it for this.

    Without the re-resolution this agent is recorded as `doc`, and since the digest path is
    built from it, the digests land in a directory the SessionStart hook never looks in.
    """
    p = _transcript(
        "-home-user--claude-projects-doc-health",
        cwd="/home/user/.claude/projects/doc-health",
        tmp_path=tmp_path,
    )
    assert extract(p).agent == "doc-health"


def test_attribution_falls_back_to_the_directory_name_without_a_cwd(tmp_path) -> None:
    """A transcript carrying no `cwd` record still has to be attributed to something."""
    d = tmp_path / "-home-user--claude-projects-research"
    d.mkdir(parents=True)
    p = d / "s.jsonl"
    p.write_text(
        json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "x"}})
        + "\n",
        encoding="utf-8",
    )
    log = extract(p)
    assert log.cwd == ""
    assert log.agent == "research"


def test_a_cwd_outside_the_projects_tree_does_not_override_attribution(tmp_path) -> None:
    """A session started in a repo checkout has a `cwd` that names no agent. Letting it win
    would replace a correct name with an empty one."""
    p = _transcript(
        "-home-user--claude-projects-research",
        cwd="/home/user/repos/personal/scribe",
        tmp_path=tmp_path,
    )
    assert extract(p).agent == "research"


def test_compact_summary_is_not_extracted_as_user_text(compact) -> None:
    """vikunja#893. Claude Code writes the post-compaction recap as `type: user` with
    `isCompactSummary: true`, so it is structurally a person speaking and the parser
    believed it.

    Measured on session 6017c8ce before the guard: 18,859 characters of machine recap in
    the event log as user text, in the worst-degraded session in the corpus. The ladder
    may not drop events (invariant 6), so the recap displaced real evidence rather than
    being displaced by it.
    """
    assert compact.stats.skipped_compact == 1
    blob = json.dumps(compact.to_dict(), ensure_ascii=False)
    assert "This session is being continued" not in blob
    assert "MACHINE RECAP FILLER" not in blob


def test_compact_guard_keeps_the_turns_on_either_side(compact) -> None:
    """The positive half, and the reason the absence assertion above means anything.

    A transcript that extracted to nothing at all would satisfy every `not in` check in
    this file. So assert what SURVIVED, and assert its order: the guard must remove one
    record from the middle of a session without disturbing what surrounds it.
    """
    texts = [t.user_text for t in compact.turns if t.user_text]
    assert texts == [
        "REAL TURN BEFORE THE BOUNDARY about vikunja#893",
        "REAL TURN AFTER THE BOUNDARY about vikunja#896",
    ]
    # The tool event attached to the pre-boundary turn survives too — the guard is scoped
    # to user text and must not take tool traffic with it.
    assert compact.stats.tool_events == 1
    assert compact.stats.tool_results == 1
    assert [e.tool for t in compact.turns for e in t.events] == ["Bash"]


def test_compact_summary_does_not_open_a_turn(compact) -> None:
    """Answers the handoff's question 2 as an assertion rather than a note.

    The record was opening its own Turn, so excluding it lowers `turns` by exactly one on
    any transcript carrying a boundary (52 -> 51 on session 6017c8ce). Pinning the count
    here means a future change that reinstates the turn fails loudly instead of quietly
    restoring the defect.
    """
    assert compact.stats.turns == 2
