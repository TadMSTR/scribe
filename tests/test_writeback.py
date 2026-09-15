"""Tests for digest write-back."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.writeback import anchor, append_block, daily_path, existing_turns

WHEN = datetime(2026, 9, 13, 14, 52, tzinfo=UTC)


def _append(root: Path, *, turn="u1", body="- did a thing\n", when=WHEN, agent="research"):
    path = daily_path(root, agent, when)
    return path, append_block(
        path,
        body=body,
        session_id="s1",
        turn_uuid=turn,
        transcript_path="/p/t.jsonl",
        when=when,
    )


def test_layout_mirrors_the_live_memory_tree(tmp_path) -> None:
    """A change of root at cutover, not a change of format."""
    p = daily_path(tmp_path, "research", WHEN)
    assert p == tmp_path / "research" / "2026-09-13.md"


def test_missing_agent_falls_back_rather_than_writing_to_the_root(tmp_path) -> None:
    assert daily_path(tmp_path, "", WHEN).parent.name == "unknown"


def test_first_write_creates_the_file_with_a_date_heading(tmp_path) -> None:
    path, written = _append(tmp_path)
    assert written is True
    text = path.read_text()
    assert text.startswith("# 2026-09-13\n")
    assert "## Session 14:52" in text
    assert "- did a thing" in text


def test_every_write_ends_with_a_newline(tmp_path) -> None:
    """vikunja#439: ~1,030 blocks were glued together by a missing one."""
    path, _ = _append(tmp_path, body="- no trailing newline")
    assert path.read_text().endswith("\n")


def test_a_body_that_already_ends_with_a_newline_is_not_doubled(tmp_path) -> None:
    """Retargeted when the F-04 terminator landed: the body is no longer the last thing in
    the file, so the assertion moved to the body's own boundary rather than the file's."""
    path, _ = _append(tmp_path, body="- ends cleanly\n")
    text = path.read_text()
    assert "- ends cleanly\n<!-- /scribe" in text
    assert "\n\n<!-- /scribe" not in text
    assert not text.endswith("\n\n")


def test_the_anchor_carries_session_turn_and_transcript(tmp_path) -> None:
    path, _ = _append(tmp_path)
    text = path.read_text()
    assert "session:s1" in text
    assert "turn:u1" in text
    assert "transcript:/p/t.jsonl" in text


def test_appending_the_same_turn_twice_is_a_no_op(tmp_path) -> None:
    """The idempotency key is the turn uuid, checked against the file itself. The store has
    the same guard; two are kept because they fail in different circumstances — the store can
    be reset, and the file can be edited by hand."""
    path, first = _append(tmp_path, turn="u1")
    _path, second = _append(tmp_path, turn="u1")
    assert (first, second) == (True, False)
    # One anchor and one terminator, not two blocks. Counting the bare string `turn:u1`
    # would now be 2 for a single correct block, so count the anchors specifically.
    assert path.read_text().count("<!-- session:") == 1
    assert path.read_text().count("<!-- /scribe turn:u1 -->") == 1


def test_a_different_turn_appends(tmp_path) -> None:
    path, _ = _append(tmp_path, turn="u1")
    _path, second = _append(tmp_path, turn="u2", body="- second thing\n")
    assert second is True
    text = path.read_text()
    assert "turn:u1" in text and "turn:u2" in text
    assert text.count("## Session") == 2


def test_blocks_are_separated_by_a_blank_line(tmp_path) -> None:
    path, _ = _append(tmp_path, turn="u1")
    _append(tmp_path, turn="u2")
    assert "\n\n## Session" in path.read_text()


def test_a_file_left_without_a_trailing_newline_is_repaired_on_append(tmp_path) -> None:
    """The corpus this mirrors contains blocks that did not end cleanly. Appending after one
    must not glue the new heading onto the old text."""
    path = daily_path(tmp_path, "research", WHEN)
    path.parent.mkdir(parents=True)
    path.write_text("# 2026-09-13\n\n## Session 09:00\n- older block with no newline")
    _append(tmp_path, turn="u9")
    text = path.read_text()
    assert "no newline\n\n## Session 14:52" in text


def test_existing_turns_reads_every_anchor(tmp_path) -> None:
    _append(tmp_path, turn="u1")
    _append(tmp_path, turn="u2")
    assert existing_turns(daily_path(tmp_path, "research", WHEN)) == {"u1", "u2"}


def test_existing_turns_on_a_missing_file_is_empty(tmp_path) -> None:
    assert existing_turns(tmp_path / "nope.md") == set()


def test_anchor_format_is_parseable_by_its_own_regex() -> None:
    from scribe.writeback import ANCHOR_RE

    m = ANCHOR_RE.search(anchor("s", "t", "/p.jsonl"))
    assert m is not None
    assert m.groups() == ("s", "t", "/p.jsonl")


@pytest.mark.parametrize("agent", ["research", "developer", "writer"])
def test_agents_get_separate_files(tmp_path, agent: str) -> None:
    path, _ = _append(tmp_path, agent=agent)
    assert path.parent.name == agent
    assert path.exists()


# --- F-04: torn writes must be detectable ------------------------------------------


def test_a_complete_block_carries_a_terminator(tmp_path) -> None:
    path, _ = _append(tmp_path, turn="u1")
    assert "<!-- /scribe turn:u1 -->" in path.read_text()


def test_a_torn_block_is_not_reported_as_done(tmp_path) -> None:
    """The whole point of F-04. A crash mid-append leaves an anchor above a truncated body;
    without the terminator both dedup guards call that turn done and the session is lost
    permanently."""
    path, _ = _append(tmp_path, turn="u1", body="- first\n")
    _append(tmp_path, turn="u2", body="- second block that will be torn\n")
    full = path.read_text()
    path.write_text(full[: full.index("- second block") + 12])  # cut mid-body
    turns = existing_turns(path)
    assert "u1" in turns, "the intact block must still count"
    assert "u2" not in turns, "the torn block must not count as done"


def test_a_torn_block_is_rewritten_on_the_next_sweep(tmp_path) -> None:
    path, _ = _append(tmp_path, turn="u1")
    _append(tmp_path, turn="u2", body="- will be torn\n")
    full = path.read_text()
    path.write_text(full[: full.index("- will be torn") + 6])
    assert _append(tmp_path, turn="u2", body="- retried\n")[1] is True
    assert "u2" in existing_turns(path)


def test_an_anchor_without_a_terminator_never_counts(tmp_path) -> None:
    """Hand-written or half-migrated content must not be trusted either."""
    path = daily_path(tmp_path, "research", WHEN)
    path.parent.mkdir(parents=True)
    path.write_text(
        "# 2026-09-13\n\n## Session 09:00\n"
        "<!-- session:s turn:orphan transcript:/t.jsonl -->\n- body with no terminator\n"
    )
    assert existing_turns(path) == set()


def test_a_terminator_without_an_anchor_never_counts(tmp_path) -> None:
    path = daily_path(tmp_path, "research", WHEN)
    path.parent.mkdir(parents=True)
    path.write_text("<!-- /scribe turn:ghost -->\n")
    assert existing_turns(path) == set()


def test_a_digest_carries_no_expires_frontmatter(tmp_path) -> None:
    """Nothing scribe writes may claim an expiry date.

    `memory-expire.py` sweeps `~/.claude/memory/` and deletes notes whose `expires:`
    frontmatter has passed. Digests live outside that tree, so it cannot reach them today
    and this costs one test — but "today" is doing real work in that sentence. The digest
    root is one config change away from being somewhere that sweeper looks, and a
    machine-generated corpus that quietly deletes itself is not a failure anyone would
    attribute to a frontmatter key added years earlier.
    """
    path = tmp_path / "research" / "2026-09-15.md"
    append_block(
        path,
        body="**Asked:** A question\n\n**Done:**\n- Did a thing\n",
        session_id="s-1",
        turn_uuid="t-1",
        transcript_path="/var/tmp/sess.jsonl",
        when=datetime(2026, 9, 15, 14, 52, tzinfo=UTC),
    )
    text = path.read_text(encoding="utf-8")

    assert "expires" not in text.lower()
    # And no YAML frontmatter block at all -- `expires` is only reachable inside one, so the
    # absence of the container is the durable form of the claim.
    assert not text.lstrip().startswith("---")
