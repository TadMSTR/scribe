"""Tests for digest write-back."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.writeback import (
    UNATTRIBUTED,
    anchor,
    append_block,
    daily_path,
    existing_turns,
    provisional_turns,
    replace_block,
)

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
    assert m.groups() == ("s", "t", "/p.jsonl", None)


def test_a_provisional_anchor_is_parseable_and_a_final_one_carries_no_attribute() -> None:
    """The attribute is optional in the regex, and absent from a final block's anchor.

    That absence is load-bearing rather than cosmetic: 444 blocks were on disk before this
    existed, all of them real digests, and they have to keep reading as final without being
    rewritten. "No attribute" has always meant finished, so it still does.
    """
    from scribe.writeback import ANCHOR_RE

    assert " provisional:" not in anchor("s", "t", "/p.jsonl")
    m = ANCHOR_RE.search(anchor("s", "t", "/p.jsonl", "placeholder"))
    assert m is not None
    assert m.groups() == ("s", "t", "/p.jsonl", "placeholder")


def test_a_pre_existing_anchor_still_reads_as_final(tmp_path) -> None:
    """A block written before provisional markers existed, byte for byte from the corpus."""
    path = tmp_path / "d" / "2026-08-19.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# 2026-08-19\n\n## Session 09:14\n\n"
        "<!-- session:a278a0f2 turn:a6c70569 transcript:/h/t.jsonl -->\n"
        "**Asked:** Something real\n"
        "<!-- /scribe turn:a6c70569 -->\n"
    )
    assert existing_turns(path) == {"a6c70569"}
    assert provisional_turns(path) == {}


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


# --- #872/#868: a provisional block is replaceable, a final one is not ----------------


def _append_provisional(
    root: Path, kind="placeholder", turn="u1", body="**Summary unavailable**\n"
):
    path = daily_path(root, "research", WHEN)
    append_block(
        path,
        body=body,
        session_id="s1",
        turn_uuid=turn,
        transcript_path="/h/t.jsonl",
        when=WHEN,
        provisional=kind,
    )
    return path


@pytest.mark.parametrize("kind", ["placeholder", "suppressed"])
def test_a_real_digest_replaces_a_provisional_block(tmp_path, kind: str) -> None:
    """The write-side half of the fix, and the exact 30-session failure.

    Before this, the second call returned False: the uuid was on disk, so the digest that had
    just been generated and paid for was discarded, and the state was then marked summarized.
    """
    path = _append_provisional(tmp_path, kind=kind)
    assert provisional_turns(path) == {"u1": kind}

    assert append_block(
        path,
        body="- Ran the real work\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
    )
    text = path.read_text()
    assert "- Ran the real work" in text
    assert "Summary unavailable" not in text
    assert existing_turns(path) == {"u1"}
    assert provisional_turns(path) == {}


def test_replacing_leaves_exactly_one_block_and_one_heading(tmp_path) -> None:
    """A replace must not become an append. Duplicate blocks under one turn uuid is the
    class of bug the anchor exists to prevent (vikunja#844), and a rewrite is the one path
    that could reintroduce it."""
    path = _append_provisional(tmp_path)
    append_block(
        path,
        body="- Ran the real work\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
    )
    text = path.read_text()
    assert text.count("<!-- session:") == 1
    assert text.count("<!-- /scribe turn:u1 -->") == 1
    assert text.count("## Session ") == 1
    assert text.count("# 2026-09-13") == 1
    assert text.endswith("\n")


def test_replacing_preserves_the_blocks_around_it(tmp_path) -> None:
    """The neighbours are the reason this reads the file rather than truncating to the
    anchor: a daily file holds every session for that agent that day."""
    path = daily_path(tmp_path, "research", WHEN)
    _append(tmp_path, turn="before", body="- earlier session\n")
    _append_provisional(tmp_path, turn="middle")
    _append(tmp_path, turn="after", body="- later session\n")

    assert append_block(
        path,
        body="- recovered\n",
        session_id="s1",
        turn_uuid="middle",
        transcript_path="/h/t.jsonl",
        when=WHEN,
    )
    text = path.read_text()
    assert "- earlier session" in text
    assert "- later session" in text
    assert "- recovered" in text
    assert existing_turns(path) == {"before", "middle", "after"}
    assert text.index("earlier") < text.index("recovered") < text.index("later")


def test_a_final_block_is_never_overwritten(tmp_path) -> None:
    """The check order matters. A real digest must survive a later placeholder for the same
    turn — otherwise a transient provider failure on a re-run could destroy a good digest,
    which would be a strictly worse bug than the one being fixed."""
    path, _ = _append(tmp_path, body="- the real digest\n")
    assert not append_block(
        path,
        body="**Summary unavailable**\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
        provisional="placeholder",
    )
    assert "- the real digest" in path.read_text()
    assert "Summary unavailable" not in path.read_text()


def test_a_placeholder_can_be_replaced_by_another_placeholder(tmp_path) -> None:
    """A retry that fails again refreshes the stand-in rather than stacking a second one."""
    path = _append_provisional(tmp_path, body="**Summary unavailable** attempt 1\n")
    assert append_block(
        path,
        body="**Summary unavailable** attempt 2\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
        provisional="placeholder",
    )
    text = path.read_text()
    assert text.count("<!-- session:") == 1
    assert "attempt 2" in text and "attempt 1" not in text
    assert provisional_turns(path) == {"u1": "placeholder"}


def test_replace_block_refuses_a_turn_that_is_not_provisional(tmp_path) -> None:
    path, _ = _append(tmp_path)
    assert not replace_block(
        path, body="x\n", session_id="s1", turn_uuid="u1", transcript_path="/h/t.jsonl"
    )
    assert not replace_block(
        path, body="x\n", session_id="s1", turn_uuid="nope", transcript_path="/h/t.jsonl"
    )


def test_a_torn_provisional_block_is_not_replaced(tmp_path) -> None:
    """No terminator means the block was never written whole, so its extent is unknown.

    Rewriting from an anchor to a guessed end would corrupt whatever followed. Reporting it
    as neither final nor provisional makes the next sweep append a fresh block instead, which
    is the same answer `existing_turns` has always given for a tear.
    """
    path = daily_path(tmp_path, "research", WHEN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 2026-09-13\n\n## Session 14:52\n\n"
        "<!-- session:s1 turn:u1 transcript:/h/t.jsonl provisional:placeholder -->\n"
        "**Summary unavail"
    )
    assert provisional_turns(path) == {}
    assert existing_turns(path) == set()
    assert not replace_block(
        path, body="x\n", session_id="s1", turn_uuid="u1", transcript_path="/h/t.jsonl"
    )


def test_the_replaced_file_keeps_owner_only_permissions(tmp_path) -> None:
    """Digests are derived from 0600 transcripts. The temp-file-and-rename path is a second
    place that creates a file, and a fresh temp file gets 0644 from the umask."""
    path = _append_provisional(tmp_path)
    append_block(
        path,
        body="- recovered\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
    )
    assert path.stat().st_mode & 0o077 == 0
    assert not list(path.parent.glob("*.tmp"))


def test_the_temp_file_is_owner_only_before_the_rename(tmp_path, monkeypatch) -> None:
    """FW-03: rename preserves the *source's* permissions.

    `test_the_replaced_file_keeps_owner_only_permissions` passes either way, because the
    `chmod` afterwards closes the window — so it cannot see this. Inspect the temp file at
    the moment of the rename instead, which is the only point the window is observable.

    A 0644 temp file does not merely expose itself briefly: renaming it over an already-0600
    daily file silently downgrades the destination.
    """
    import os as _os

    seen: list[int] = []
    real = _os.replace

    def spy(src, dst, *a, **k):
        seen.append(Path(src).stat().st_mode & 0o777)
        return real(src, dst, *a, **k)

    monkeypatch.setattr("scribe.writeback.os.replace", spy)
    path = _append_provisional(tmp_path)
    append_block(
        path,
        body="- recovered\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
    )
    assert seen, "the replace path did not go through os.replace"
    assert all(mode & 0o077 == 0 for mode in seen), f"temp file was {seen!r} at rename"


# --- a model can emit the block structure it is describing ----------------------------


#: Taken verbatim from a digest in the live corpus (developer/2026-09-15.md). The session was
#: about the QC gate's treatment of the terminator, so the digest quotes the terminator.
_MODEL_EMITTED_TERMINATOR = (
    "- QC gate defect: terminator <!-- /scribe turn:... --> was read as a /scribe path claim\n"
)


def test_a_terminator_inside_a_body_does_not_end_the_block(tmp_path) -> None:
    """The extent of a block is the span between an anchor and *its own* terminator.

    Taking the first terminator after the anchor would end the block inside its own body, so
    a replace would cut there and orphan the rest — including the real terminator, which
    would then close the *next* block.
    """
    path = daily_path(tmp_path, "research", WHEN)
    append_block(
        path,
        body="- before\n" + _MODEL_EMITTED_TERMINATOR + "- after\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
        provisional="placeholder",
    )
    assert provisional_turns(path) == {"u1": "placeholder"}

    assert append_block(
        path,
        body="- recovered\n",
        session_id="s1",
        turn_uuid="u1",
        transcript_path="/h/t.jsonl",
        when=WHEN,
    )
    text = path.read_text()
    assert "- recovered" in text
    assert "- before" not in text and "- after" not in text
    assert "/scribe turn:..." not in text
    assert text.count("<!-- /scribe turn:u1 -->") == 1
    assert existing_turns(path) == {"u1"}


def test_a_body_terminator_for_a_real_uuid_does_not_close_a_torn_block(tmp_path) -> None:
    """A global "uuids seen as a terminator" set would mark a torn block complete.

    That is the exact failure `terminator()` exists to make detectable: an anchor above a
    truncated body, with both dedup guards agreeing never to retry it.
    """
    path = daily_path(tmp_path, "research", WHEN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 2026-09-13\n\n## Session 14:52\n\n"
        "<!-- session:s1 turn:torn transcript:/h/t.jsonl -->\n"
        "**Asked:** truncated mid-w"
        # the next block's body happens to quote the torn block's terminator
        "\n\n## Session 15:10\n\n"
        "<!-- session:s2 turn:later transcript:/h/u.jsonl -->\n"
        "- discussed <!-- /scribe turn:torn -->\n"
        "<!-- /scribe turn:later -->\n"
    )
    assert existing_turns(path) == {"later"}
    assert "torn" not in existing_turns(path)


def test_a_torn_block_does_not_borrow_the_next_blocks_terminator(tmp_path) -> None:
    """Scanning past the next anchor would close a torn block with a stranger's terminator."""
    path = daily_path(tmp_path, "research", WHEN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 2026-09-13\n\n"
        "<!-- session:s1 turn:torn transcript:/h/t.jsonl -->\n"
        "**Asked:** truncated mid-w\n"
        "<!-- session:s2 turn:whole transcript:/h/u.jsonl -->\n"
        "**Asked:** fine\n"
        "<!-- /scribe turn:whole -->\n"
    )
    assert existing_turns(path) == {"whole"}


def test_a_quoted_anchor_does_not_hide_the_block_containing_it(tmp_path) -> None:
    """A complete, FINAL digest whose body quotes an anchor must stay visible.

    Regression from `56ab8ce`, found by the 2026-09-16 audit's adjacent finding. That commit
    bounded the terminator search at the next anchor to stop a torn block borrowing a
    stranger's terminator — correct — but an anchor quoted inside a body then became that
    bound, putting the containing block's own terminator out of range. The block read as
    torn, dropped out of `existing_turns` AND `provisional_turns`, and the next sweep would
    have appended a duplicate over a perfectly good digest.

    The pre-`56ab8ce` code got this case right, so the fix has to hold both: a quoted
    terminator must not close a block, and a quoted anchor must not open one.
    """
    path = daily_path(tmp_path, "research", WHEN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 2026-09-13\n\n## Session 14:52\n\n"
        "<!-- session:s1 turn:real transcript:/h/x.jsonl -->\n"
        "**Asked:** How the anchor format works\n"
        "- The anchor is <!-- session:s2 turn:other transcript:/h/y.jsonl --> on its own line\n"
        "<!-- /scribe turn:real -->\n"
    )
    assert existing_turns(path) == {"real"}
    assert provisional_turns(path) == {}


def test_structure_is_recognised_only_alone_on_a_line(tmp_path) -> None:
    """The discriminator, stated directly.

    Every block scribe writes puts its anchor and terminator on their own lines, and the live
    corpus bears that out exactly: 444 of 444 anchors and 444 of 445 terminators are alone on
    theirs — the one exception being the quoted terminator that started all of this.
    """
    from scribe.writeback import ANCHOR_RE, TERMINATOR_RE

    assert ANCHOR_RE.search("<!-- session:s turn:t transcript:/p.jsonl -->")
    assert TERMINATOR_RE.search("<!-- /scribe turn:t -->")
    assert not ANCHOR_RE.search("see <!-- session:s turn:t transcript:/p.jsonl -->")
    assert not ANCHOR_RE.search("<!-- session:s turn:t transcript:/p.jsonl --> see")
    assert not TERMINATOR_RE.search("- terminator <!-- /scribe turn:t --> was misread")


def test_the_patterns_never_span_a_newline() -> None:
    """`\\s` matches a newline; `[ \\t]` does not.

    With `(?m)^...$` anchoring, a `\\s`-based separator could start on one line and finish on
    the next, which would defeat the line anchoring it is paired with.
    """
    from scribe.writeback import ANCHOR_RE, TERMINATOR_RE

    assert not ANCHOR_RE.search("<!-- session:s turn:t transcript:/p.jsonl\n-->")
    assert not TERMINATOR_RE.search("<!-- /scribe\nturn:t -->")


# --- the write path had no guard on `agent`, while the read path always had one ------------


@pytest.mark.parametrize("agent", ["..", ".", "", "a/b", "../../etc", "/abs"])
def test_an_unusable_agent_name_cannot_escape_the_digest_root(tmp_path, agent: str) -> None:
    """`agent` reaches `daily_path` from a transcript's own top-level `cwd` field, via
    `_agent_from_cwd`, which returns `Path(cwd).name`. A `cwd` of `~/.claude/projects/..`
    therefore yields `".."` and this used to write one level ABOVE the digest root.

    Medium, memory-consolidation-2026-09-p4 audit. The read side (`journal.agent_dir`) had
    `valid_agent` plus containment from the start; the write side did not.
    """
    got = daily_path(tmp_path, agent, WHEN)
    assert tmp_path.resolve() in got.resolve().parents
    assert got.parent.name == UNATTRIBUTED


@pytest.mark.parametrize("agent", ["developer", "doc-health", "memory-sync", "a.b", "A9_-"])
def test_a_legitimate_agent_name_still_names_its_own_directory(tmp_path, agent: str) -> None:
    """The true positive the guard must not eat. `doc-health` and `memory-sync` are the exact
    hyphenated names `agent_for_path` exists to resolve correctly -- a guard that routed them
    to `unknown` would recreate the bug that motivated it."""
    assert daily_path(tmp_path, agent, WHEN).parent.name == agent


def test_the_rejected_digest_is_still_written_rather_than_discarded(tmp_path) -> None:
    """`daily_path` falls back where `journal.agent_dir` raises, deliberately.

    Refusing to READ yields an empty injection -- indistinguishable from a quiet day. Refusing
    to WRITE would discard a summary already paid for, which is the loss this whole component
    exists to prevent. So the digest survives, in the bucket an empty agent always used.
    """
    path = daily_path(tmp_path, "..", WHEN)
    assert append_block(
        path,
        body="**Asked:** something\n",
        session_id="s",
        turn_uuid="u1",
        transcript_path="/t.jsonl",
        when=WHEN,
    )
    assert path.is_file()
    assert "**Asked:** something" in path.read_text(encoding="utf-8")


def test_the_write_and_read_sides_agree_on_what_an_agent_name_is(tmp_path) -> None:
    """One definition in `paths.py`, two consumers. They held different copies of this rule
    once -- one of them empty -- which is exactly how the write path stayed unguarded while
    the read path was correct."""
    from scribe.journal import agent_dir

    for name in ("developer", "doc-health", "a.b"):
        assert agent_dir(tmp_path, name).name == daily_path(tmp_path, name, WHEN).parent.name
    for bad in ("..", ".", "a/b"):
        with pytest.raises(ValueError):
            agent_dir(tmp_path, bad)
        assert daily_path(tmp_path, bad, WHEN).parent.name == UNATTRIBUTED
