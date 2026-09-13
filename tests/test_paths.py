"""Permissions on everything scribe creates.

NE-05 in the fleet's security-patterns knowledge base, recurrence 13 — the single most
repeated finding across forge audits. This build's specific version: Claude Code transcripts
are mode 0600, and scribe emits derived content. At the default umask that content lands
0644, which takes owner-only material and publishes it to the seven `agent-*` accounts on
this host, none of which are in group `ted`. The world-read bit is exactly the bit that
grants them access.
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.paths import DIR_MODE, FILE_MODE, secure_dir, secure_file, secure_sqlite
from scribe.state import Store
from scribe.telemetry import record_spend
from scribe.writeback import append_block, daily_path

WHEN = datetime(2026, 9, 13, 14, 52, tzinfo=UTC)


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def assert_owner_only(p: Path) -> None:
    assert not mode(p) & 0o077, f"{p} is {oct(mode(p))}, accessible beyond the owner"


def test_secure_dir_creates_owner_only(tmp_path) -> None:
    d = secure_dir(tmp_path / "x")
    assert mode(d) == DIR_MODE


def test_secure_dir_tightens_every_directory_it_creates(tmp_path) -> None:
    """Chmod'ing only the leaf leaves a 0755 ancestor above a 0700 directory, which defeats
    the point: the traversal bit on the parent is what a reader needs."""
    secure_dir(tmp_path / "a" / "b" / "c")
    for part in ("a", "a/b", "a/b/c"):
        assert_owner_only(tmp_path / part)


def test_secure_dir_does_not_touch_directories_it_did_not_create(tmp_path) -> None:
    """Walking up and tightening whatever is found would eventually reach ~ or /home."""
    pre = tmp_path / "preexisting"
    pre.mkdir(mode=0o755)
    pre.chmod(0o755)
    secure_dir(pre / "child")
    assert mode(pre) == 0o755, "an existing ancestor must be left alone"
    assert_owner_only(pre / "child")


def test_secure_dir_tightens_an_existing_target(tmp_path) -> None:
    """The target itself is ours even when it already exists — it is where we write."""
    d = tmp_path / "loose"
    d.mkdir(mode=0o777)
    d.chmod(0o777)
    secure_dir(d)
    assert mode(d) == DIR_MODE


def test_secure_file_tightens(tmp_path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    f.chmod(0o644)
    secure_file(f)
    assert mode(f) == FILE_MODE


def test_secure_file_on_a_missing_path_does_not_raise(tmp_path) -> None:
    """A permissions failure must not take down a summarization run."""
    secure_file(tmp_path / "absent")


def test_secure_sqlite_covers_the_wal_sidecars(tmp_path) -> None:
    """`-wal` and `-shm` are created by SQLite under the process umask, not by us."""
    db = tmp_path / "s.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        p.write_text("x")
        p.chmod(0o644)
    secure_sqlite(db)
    for suffix in ("", "-wal", "-shm"):
        assert_owner_only(db.with_name(db.name + suffix))


# --- the three real write paths -----------------------------------------------------


def test_the_state_store_is_owner_only(tmp_path) -> None:
    """It holds session_id values, which are functionally credentials paired with
    `claude -p --resume`. sqlite3 inherits the umask and creates 0644 by default."""
    store = Store(tmp_path / "deep" / "state.sqlite3")
    store.upsert_observed("/t.jsonl", size_bytes=1, mtime_ns=1, session_id="sess-abc")
    store.mark_summarized("/t.jsonl", offset=1, last_turn_uuid="u", turn_uuids=["u"])
    assert_owner_only(store.path)
    assert_owner_only(store.path.parent)


def test_the_spend_log_is_owner_only(tmp_path) -> None:
    log = tmp_path / "deep" / "tokens.log"
    record_spend(model="m", input_tokens=1, output_tokens=1, log_path=log)
    assert_owner_only(log)
    assert_owner_only(log.parent)


def test_digests_are_owner_only(tmp_path) -> None:
    """Derived content must not be published wider than the 0600 transcripts it came from."""
    path = daily_path(tmp_path / "out", "research", WHEN)
    append_block(
        path,
        body="- x\n",
        session_id="s",
        turn_uuid="u",
        transcript_path="/t.jsonl",
        when=WHEN,
    )
    assert_owner_only(path)
    assert_owner_only(path.parent)
    assert_owner_only(tmp_path / "out")


def test_no_path_the_pipeline_creates_is_readable_beyond_the_owner(tmp_path) -> None:
    """The sweep. A per-file assertion only covers the files someone remembered to list;
    this walks everything that actually appeared on disk."""
    root = tmp_path / "run"
    store = Store(root / "state" / "s.sqlite3")
    store.upsert_observed("/t.jsonl", size_bytes=1, mtime_ns=1)
    record_spend(model="m", input_tokens=1, output_tokens=1, log_path=root / "logs" / "t.log")
    path = daily_path(root / "out", "writer", WHEN)
    append_block(
        path,
        body="- x\n",
        session_id="s",
        turn_uuid="u",
        transcript_path="/t.jsonl",
        when=WHEN,
    )
    created = [*root.rglob("*"), root]
    assert len(created) >= 6, "guard against a vacuous pass on an empty tree"
    for p in created:
        assert_owner_only(p)


@pytest.mark.parametrize("umask_value", [0o000, 0o022])
def test_the_result_does_not_depend_on_the_process_umask(tmp_path, umask_value: int) -> None:
    """A permissive umask is the condition under which this defect appears at all, so the
    guarantee has to hold under one rather than merely under the developer's shell."""
    import os

    old = os.umask(umask_value)
    try:
        store = Store(tmp_path / f"u{umask_value}" / "s.sqlite3")
        store.upsert_observed("/t.jsonl", size_bytes=1, mtime_ns=1)
        assert_owner_only(store.path)
        assert_owner_only(store.path.parent)
    finally:
        os.umask(old)
