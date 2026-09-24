"""Tests for the durable session store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scribe.eventlog import session_eventlog
from scribe.state import (
    _SCHEMA,
    SCHEMA_VERSION,
    STATUS_ACTIVE,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_SUMMARIZED,
    SchemaTooNewError,
    SessionRow,
    Store,
)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "nested" / "state.sqlite3")


def test_schema_is_versioned_and_the_parent_dir_is_created(store: Store) -> None:
    assert store.schema_version == SCHEMA_VERSION
    assert store.path.parent.is_dir()


def test_reopening_an_existing_store_is_safe(tmp_path) -> None:
    p = tmp_path / "s.sqlite3"
    Store(p).upsert_observed("/t.jsonl", size_bytes=10, mtime_ns=1)
    again = Store(p)
    assert again.get("/t.jsonl") is not None


def test_upsert_records_an_observation(store: Store) -> None:
    row = store.upsert_observed("/t.jsonl", size_bytes=100, mtime_ns=5, agent="research")
    assert row.size_bytes == 100
    assert row.agent == "research"
    assert row.status == STATUS_ACTIVE


def test_upsert_never_regresses_progress(store: Store) -> None:
    """An observation says what is on disk, not what has been processed.

    Conflating the two would make a rescan silently re-summarize finished work.
    """
    store.upsert_observed("/t.jsonl", size_bytes=100, mtime_ns=5)
    store.mark_summarized("/t.jsonl", offset=100, last_turn_uuid="u9", turn_uuids=["u9"])
    store.upsert_observed("/t.jsonl", size_bytes=140, mtime_ns=9)
    row = store.get("/t.jsonl")
    assert row is not None
    assert row.last_offset == 100
    assert row.last_turn_uuid == "u9"


def test_upsert_does_not_clear_known_metadata_with_blanks(store: Store) -> None:
    store.upsert_observed("/t.jsonl", size_bytes=1, mtime_ns=1, agent="writer", session_id="s1")
    store.upsert_observed("/t.jsonl", size_bytes=2, mtime_ns=2)
    row = store.get("/t.jsonl")
    assert row is not None
    assert (row.agent, row.session_id) == ("writer", "s1")


def test_has_unread_bytes_detects_a_resumed_session(store: Store) -> None:
    """Completion is inferred from an idle timer, so it can be wrong: Claude Code appends
    to an existing transcript when a session resumes. Growth past the read offset is what
    makes that recoverable."""
    store.upsert_observed("/t.jsonl", size_bytes=100, mtime_ns=1)
    store.mark_summarized("/t.jsonl", offset=100, last_turn_uuid="u1", turn_uuids=["u1"])
    assert store.get("/t.jsonl").has_unread_bytes is False
    store.upsert_observed("/t.jsonl", size_bytes=250, mtime_ns=2)
    row = store.get("/t.jsonl")
    assert row.status == STATUS_SUMMARIZED, "status alone still says done"
    assert row.has_unread_bytes is True, "but the bytes say otherwise, and they win"


def test_offset_never_goes_backwards(store: Store) -> None:
    store.upsert_observed("/t.jsonl", size_bytes=300, mtime_ns=1)
    store.mark_summarized("/t.jsonl", offset=300, last_turn_uuid="u2", turn_uuids=["u2"])
    store.mark_summarized("/t.jsonl", offset=50, last_turn_uuid="u3", turn_uuids=["u3"])
    assert store.get("/t.jsonl").last_offset == 300


def test_turn_uuid_is_the_idempotency_key(store: Store) -> None:
    """vikunja#844: keying on (session_id, HH:MM) collided 62 times across 3,539 blocks.

    Writing the same turn twice must be a no-op enforced by the schema, not by a check the
    caller has to remember to perform.
    """
    store.upsert_observed("/t.jsonl", size_bytes=10, mtime_ns=1)
    first = store.mark_summarized(
        "/t.jsonl", offset=10, last_turn_uuid="u1", turn_uuids=["u1", "u2"]
    )
    second = store.mark_summarized(
        "/t.jsonl", offset=10, last_turn_uuid="u1", turn_uuids=["u1", "u2"]
    )
    assert first == 2
    assert second == 0


def test_two_turns_in_the_same_minute_do_not_collide(store: Store) -> None:
    """The exact shape of #844, expressed as uuids rather than timestamps."""
    store.upsert_observed("/t.jsonl", size_bytes=10, mtime_ns=1)
    n = store.mark_summarized(
        "/t.jsonl",
        offset=10,
        last_turn_uuid="b",
        turn_uuids=["9f2c-same-minute-a", "9f2c-same-minute-b"],
    )
    assert n == 2


def test_unprocessed_turns_filters_and_preserves_order(store: Store) -> None:
    store.upsert_observed("/t.jsonl", size_bytes=10, mtime_ns=1)
    store.mark_summarized("/t.jsonl", offset=10, last_turn_uuid="u2", turn_uuids=["u2"])
    assert store.unprocessed_turns("/t.jsonl", ["u1", "u2", "u3"]) == ["u1", "u3"]


def test_unprocessed_turns_on_empty_input(store: Store) -> None:
    assert store.unprocessed_turns("/t.jsonl", []) == []


def test_status_and_attempts(store: Store) -> None:
    store.upsert_observed("/t.jsonl", size_bytes=1, mtime_ns=1)
    store.set_status("/t.jsonl", STATUS_COMPLETE)
    assert store.get("/t.jsonl").status == STATUS_COMPLETE
    assert store.record_attempt("/t.jsonl", error="429") == 1
    assert store.record_attempt("/t.jsonl", error="429") == 2
    assert store.get("/t.jsonl").last_error == "429"


def test_marking_summarized_clears_a_previous_error(store: Store) -> None:
    store.upsert_observed("/t.jsonl", size_bytes=1, mtime_ns=1)
    store.set_status("/t.jsonl", STATUS_FAILED, error="boom")
    store.mark_summarized("/t.jsonl", offset=1, last_turn_uuid="u1", turn_uuids=["u1"])
    row = store.get("/t.jsonl")
    assert row.status == STATUS_SUMMARIZED
    assert row.last_error == ""


def test_list_by_status(store: Store) -> None:
    store.upsert_observed("/a.jsonl", size_bytes=1, mtime_ns=1)
    store.upsert_observed("/b.jsonl", size_bytes=1, mtime_ns=1)
    store.set_status("/b.jsonl", STATUS_COMPLETE)
    assert [r.transcript_path for r in store.list_by_status(STATUS_ACTIVE)] == ["/a.jsonl"]
    assert [r.transcript_path for r in store.list_by_status(STATUS_COMPLETE)] == ["/b.jsonl"]


def test_record_attempt_on_unknown_path_returns_zero(store: Store) -> None:
    assert store.record_attempt("/nope.jsonl") == 0


def test_get_unknown_path_returns_none(store: Store) -> None:
    assert store.get("/nope.jsonl") is None


def test_forget_removes_session_and_its_turns(store: Store) -> None:
    store.upsert_observed("/t.jsonl", size_bytes=1, mtime_ns=1)
    store.mark_summarized("/t.jsonl", offset=1, last_turn_uuid="u1", turn_uuids=["u1"])
    store.forget("/t.jsonl")
    assert store.get("/t.jsonl") is None
    assert store.unprocessed_turns("/t.jsonl", ["u1"]) == ["u1"]


def test_session_row_unread_property_is_pure() -> None:
    assert SessionRow("/x", size_bytes=10, last_offset=3).has_unread_bytes is True
    assert SessionRow("/x", size_bytes=3, last_offset=3).has_unread_bytes is False


# --- schema 2: the session_id backfill ------------------------------------------------------


def _legacy_db(tmp_path, rows: list[tuple[str, str]]) -> Path:
    """A store at schema 1, holding rows as they were actually written before #872.

    Built with raw SQL rather than by calling `Store`, because `Store.__init__` is the thing
    under test -- constructing the fixture through it would migrate the rows before the
    assertion ever ran.
    """
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO sessions (transcript_path, session_id, status, first_seen, updated_at) "
        "VALUES (?, ?, 'summarized', '2026-08-01', '2026-08-01')",
        rows,
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return path


def _ids(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(path)
    try:
        return {
            r[0]: r[1] for r in conn.execute("SELECT transcript_path, session_id FROM sessions")
        }
    finally:
        conn.close()


def test_opening_a_legacy_store_backfills_the_empty_ids(tmp_path) -> None:
    """The repair. A 6%-populated column is worse than an empty one: a query keyed on it
    returns rows and looks like it works while omitting the other 94%."""
    db = _legacy_db(
        tmp_path, [("/p/-home-user--x/sess-aaa.jsonl", ""), ("/p/-y/sess-bbb.jsonl", "")]
    )
    store = Store(db)
    assert _ids(db) == {
        "/p/-home-user--x/sess-aaa.jsonl": "sess-aaa",
        "/p/-y/sess-bbb.jsonl": "sess-bbb",
    }
    assert store.schema_version == 2


def test_the_backfill_never_overwrites_a_real_extracted_id(tmp_path) -> None:
    """A value that came from an actual extraction beats one derived from a filename. The
    two agree on every row measured, but where they ever disagreed the extractor is right."""
    db = _legacy_db(tmp_path, [("/p/-x/sess-aaa.jsonl", "id-from-the-transcript")])
    Store(db)
    assert _ids(db) == {"/p/-x/sess-aaa.jsonl": "id-from-the-transcript"}


@pytest.mark.parametrize(
    "path",
    ["/p/-x/..jsonl", "/p/-x/-leading-dash.jsonl", "/p/-x/has space.jsonl", "/p/-x/.jsonl"],
)
def test_a_stem_that_is_not_a_bare_identifier_is_left_empty(tmp_path, path: str) -> None:
    """Skipped rather than cleaned up. A scrubbed name can collide with a real session's, and
    a wrong id is worse than no id -- it would resolve to another session's event log."""
    db = _legacy_db(tmp_path, [(path, "")])
    Store(db)
    assert _ids(db) == {path: ""}


def test_the_backfill_is_idempotent_and_does_not_rerun(tmp_path) -> None:
    """It is gated on `user_version`, so reopening the store must not touch the rows again --
    including a row an operator has deliberately cleared."""
    db = _legacy_db(tmp_path, [("/p/-x/sess-aaa.jsonl", "")])
    Store(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE sessions SET session_id = '' WHERE transcript_path = '/p/-x/sess-aaa.jsonl'"
    )
    conn.commit()
    conn.close()
    Store(db)
    assert _ids(db) == {"/p/-x/sess-aaa.jsonl": ""}


def test_a_fresh_store_is_created_at_the_current_schema(tmp_path) -> None:
    assert Store(tmp_path / "fresh.sqlite3").schema_version == SCHEMA_VERSION


def test_the_backfill_matches_what_the_event_log_lookup_expects(tmp_path) -> None:
    """The backfilled id is not decorative -- it is what `session_eventlog` resolves a row to.
    If the two rules ever disagreed, a replay would look for the wrong file."""
    tpath = "/p/-home-user--claude-projects-developer/sess-ccc.jsonl"
    db = _legacy_db(tmp_path, [(tpath, "")])
    Store(db)
    backfilled = _ids(db)[tpath]
    assert session_eventlog(tmp_path, backfilled, tpath) == session_eventlog(tmp_path, "", tpath)


def test_an_older_binary_does_not_lower_the_schema_marker(tmp_path, monkeypatch) -> None:
    """The hazard runs in the direction a naive test does not.

    Opening an OLD database with a NEW binary passes whether or not the stamp is guarded —
    the marker moves up either way, which is correct behaviour and proves nothing. What broke
    (vikunja#877) is the reverse: a schema-N database opened by a schema-(N-1) build, which
    stamped `user_version` unconditionally and quietly relabelled the DB downwards. Observed
    live, when the still-deployed v0.2.0 cron reset a schema-2 marker on its next hourly run.

    So: write the marker one AHEAD of what the binary claims to understand, and assert the
    binary leaves it alone.
    """
    db = tmp_path / "state.sqlite3"
    Store(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # The older binary, simulated where it actually lives: the constant the running code
    # compares against. It must not come away having rewritten the marker.
    monkeypatch.setattr("scribe.state.SCHEMA_VERSION", SCHEMA_VERSION - 1)
    with pytest.raises(SchemaTooNewError):
        Store(db)

    with sqlite3.connect(db) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


def test_a_database_from_a_newer_scribe_is_refused_rather_than_used(tmp_path) -> None:
    """An older binary meeting a newer database is the case most likely to lose data, and it
    was completely silent. Refusing is the point — a `Store` that opened it and carried on
    would write rows against a schema it does not know."""
    db = tmp_path / "state.sqlite3"
    Store(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(SchemaTooNewError, match=f"schema {SCHEMA_VERSION + 1}"):
        Store(db)


def test_the_marker_still_advances_on_an_older_database(tmp_path) -> None:
    """The true positive kept beside the guard. "Only ever advance" must not quietly become
    "never write", which would leave every migration gate reading 0 forever."""
    db = tmp_path / "state.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version = 0")

    assert Store(db).schema_version == SCHEMA_VERSION
