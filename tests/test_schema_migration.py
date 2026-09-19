"""The `stateful` requirement: an N-1 database must migrate, keep its rows, and refuse N+1.

**Nothing checked any of this before.** `repo-conform.py` does not carry a requirement for it
— it is not even in `--list-unchecked` — so the repo declared `stateful: true` and reported a
clean sweep while the migration path was exercised only by tests that create a fresh database
and immediately open it.

The three assertions are chosen against three different silent failures:

  1. **The marker advances.** Cheap, and the one a naive test already covers.
  2. **The rows survive, and the migration actually did its work.** A migration only ever run
     forward on an EMPTY database has not been tested. This is the assertion with substance
     here, because schema 2's DDL is byte-identical to schema 1's — the only thing migrating
     is `_backfill_session_ids`, so a test that checks the marker and the table shape would
     pass against a build where the backfill had been deleted outright.
  3. **A newer database is refused.** vikunja#877: `Store.__init__` stamped `user_version`
     unconditionally, so a still-deployed older binary silently relabelled a newer DB
     downwards on its next hourly cron run.

`tests/test_state.py` already covers (3) at the `Store` level by monkeypatching
`SCHEMA_VERSION`. This file covers it the other way round — stamping the DATABASE ahead and
driving the real CLI — because that is the shape the incident actually had, and it exercises
`recover_cli`'s exit-code mapping rather than the exception alone.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scribe.recover_cli import EXIT_STATE_INCOMPATIBLE
from scribe.recover_cli import main as recover_main
from scribe.state import SCHEMA_VERSION, SchemaTooNewError, Store

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from regenerate_schema_v1 import (
    FIXTURE,
    SCHEMA_V1_TAG,
    derive,
    tag_is_reachable,
)

#: Legacy rows as schema 1 left them: `session_id` empty, because nothing supplied it after
#: extraction until vikunja#872. The stems are real-shaped session ids.
LEGACY_ROWS = [
    ("/home/ted/.claude/projects/dev/3f2a9c81-4b7e-4d1a-9c22-8e5f0a1b2c3d.jsonl", "summarized"),
    ("/home/ted/.claude/projects/ops/7c4e1d92-5a8f-4e2b-8d33-9f6a1b2c3d4e.jsonl", "summarized"),
    ("/home/ted/.claude/projects/dev/not a bare id.jsonl", "complete"),
]


def _schema_v1_db(path: Path) -> None:
    """Create a schema-1 database and populate it, exactly as v0.2.0 would have left it."""
    with sqlite3.connect(path) as conn:
        conn.executescript(FIXTURE.read_text())
        conn.executemany(
            "INSERT INTO sessions (transcript_path, session_id, agent, size_bytes, mtime_ns,"
            " last_offset, last_turn_uuid, status, attempts, last_error, first_seen,"
            " updated_at) VALUES (?, '', 'dev', 4096, 1700000000, 2048, 'turn-abc', ?, 0, '',"
            " '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
            LEGACY_ROWS,
        )
        conn.execute(
            "INSERT INTO processed_turns (turn_uuid, transcript_path, written_at)"
            " VALUES ('turn-abc', ?, '2026-08-01T00:00:00+00:00')",
            (LEGACY_ROWS[0][0],),
        )


# --------------------------------------------------------------------------- provenance


def test_the_schema_1_fixture_still_matches_the_tag_it_claims_to_come_from() -> None:
    """The fixture is a copy of code from a tag. A copy can drift from its source silently.

    This runs in CI because ci.yml checks out with `fetch-depth: 0`. If that ever changes,
    SCRIBE_REQUIRE_SCHEMA_PROVENANCE turns the skip below into a failure — without it, a
    checkout change would switch this check off and nothing would say so.
    """
    if not tag_is_reachable():
        if os.environ.get("SCRIBE_REQUIRE_SCHEMA_PROVENANCE"):
            pytest.fail(
                f"tag {SCHEMA_V1_TAG} is unreachable but SCRIBE_REQUIRE_SCHEMA_PROVENANCE is "
                "set. CI must check out with fetch-depth: 0 for this check to run."
            )
        pytest.skip(f"tag {SCHEMA_V1_TAG} unreachable (shallow clone)")

    assert FIXTURE.read_text() == derive(), (
        f"{FIXTURE.name} no longer matches src/scribe/state.py at {SCHEMA_V1_TAG}. "
        "Regenerate with: python tests/fixtures/regenerate_schema_v1.py"
    )


def test_the_fixture_really_is_schema_1(tmp_path) -> None:
    """A fixture stamping the CURRENT version would make every migration test below a no-op
    that passes."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)
    with sqlite3.connect(db) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1
    assert SCHEMA_VERSION > 1, "this whole file assumes there is something to migrate to"


# --------------------------------------------------------------------------- forward


def test_opening_a_schema_1_database_advances_the_marker(tmp_path) -> None:
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)
    assert Store(db).schema_version == SCHEMA_VERSION


def test_every_pre_existing_row_survives_the_migration(tmp_path) -> None:
    """A migration only ever run forward on an empty database has not been tested."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)

    store = Store(db)

    for path, status in LEGACY_ROWS:
        row = store.get(path)
        assert row is not None, f"{path} was lost by the migration"
        assert row.status == status
        assert row.agent == "dev"
        assert row.size_bytes == 4096
        assert row.last_offset == 2048
        assert row.last_turn_uuid == "turn-abc"
        assert row.first_seen == "2026-08-01T00:00:00+00:00"

    # The join table too. Losing it would silently re-summarize every turn it recorded.
    assert store.unprocessed_turns(LEGACY_ROWS[0][0], ["turn-abc"]) == []


def test_the_migration_actually_backfilled_the_session_ids(tmp_path) -> None:
    """The assertion with substance.

    Schema 2's DDL is byte-identical to schema 1's — `_backfill_session_ids` is the entire
    migration. So checking the marker and the table shape would pass against a build with the
    backfill deleted. This is what distinguishes "the version number moved" from "the data
    was migrated".
    """
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)

    store = Store(db)

    assert store.get(LEGACY_ROWS[0][0]).session_id == "3f2a9c81-4b7e-4d1a-9c22-8e5f0a1b2c3d"
    assert store.get(LEGACY_ROWS[1][0]).session_id == "7c4e1d92-5a8f-4e2b-8d33-9f6a1b2c3d4e"


def test_a_stem_that_is_not_a_bare_id_is_left_empty_rather_than_cleaned_up(tmp_path) -> None:
    """The conservative half of the backfill, and the discriminating one: a backfill that
    wrote every stem unconditionally would pass the test above. A scrubbed name can collide
    with a real session's, which is the one outcome worse than an empty field."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)

    assert Store(db).get(LEGACY_ROWS[2][0]).session_id == ""


def test_migrating_twice_is_a_no_op(tmp_path) -> None:
    """The hourly cron opens this database every hour forever. If a second open re-ran the
    migration, the first migration that TRANSFORMS a value rather than filling a blank would
    corrupt on its second application."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)

    first = Store(db)
    before = {p: first.get(p).session_id for p, _ in LEGACY_ROWS}

    second = Store(db)
    assert second.schema_version == SCHEMA_VERSION
    assert {p: second.get(p).session_id for p, _ in LEGACY_ROWS} == before


def test_a_value_already_present_is_not_overwritten_by_the_backfill(tmp_path) -> None:
    """A value that came from an actual extraction must always win over one derived from a
    filename."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)
    real = "an-id-that-came-from-the-transcript"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE sessions SET session_id = ? WHERE transcript_path = ?",
            (real, LEGACY_ROWS[0][0]),
        )

    assert Store(db).get(LEGACY_ROWS[0][0]).session_id == real


# --------------------------------------------------------------------------- refusal


def test_a_database_stamped_ahead_is_refused(tmp_path) -> None:
    """vikunja#877's shape, driven from the DATABASE side rather than by patching the
    binary's constant — that is how the incident actually presented."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)
    Store(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(SchemaTooNewError):
        Store(db)

    # And it did not relabel it on the way out.
    with sqlite3.connect(db) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION + 1


def test_the_cli_maps_that_refusal_to_exit_5(tmp_path) -> None:
    """`scribe-recover-check.sh` pages on these codes. 5 means "upgrade the binary", which is
    a different operator action from 4's "this is a bug" — reported as 4 it would look like
    something to chase."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)
    Store(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    cfg = tmp_path / "scribe.toml"
    cfg.write_text(
        f'[paths]\ndigest_dir = "{tmp_path / "digests"}"\n'
        f'eventlog_dir = "{tmp_path / "eventlogs"}"\n'
        f'transcript_dir = "{tmp_path / "transcripts"}"\n'
        f'state_db = "{db}"\n'
    )
    (tmp_path / "digests").mkdir()
    (tmp_path / "eventlogs").mkdir()
    (tmp_path / "transcripts").mkdir()

    assert recover_main(["--config", str(cfg), "--state", str(db)]) == EXIT_STATE_INCOMPATIBLE


def test_a_schema_1_database_reaches_an_ordinary_code_instead(tmp_path) -> None:
    """The control beside the refusal. A `recover` that returned 5 unconditionally would
    satisfy the test above and be completely broken."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)

    cfg = tmp_path / "scribe.toml"
    cfg.write_text(
        f'[paths]\ndigest_dir = "{tmp_path / "digests"}"\n'
        f'eventlog_dir = "{tmp_path / "eventlogs"}"\n'
        f'transcript_dir = "{tmp_path / "transcripts"}"\n'
        f'state_db = "{db}"\n'
    )
    (tmp_path / "digests").mkdir()
    (tmp_path / "eventlogs").mkdir()
    (tmp_path / "transcripts").mkdir()

    assert recover_main(["--config", str(cfg), "--state", str(db)]) != EXIT_STATE_INCOMPATIBLE


def test_subprocess_exit_code_is_the_one_the_shell_sees(tmp_path) -> None:
    """`recover_main` returning 5 and `python -m scribe recover` exiting 5 are different
    claims. The pager reads the second."""
    db = tmp_path / "state.sqlite3"
    _schema_v1_db(db)
    Store(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    cfg = tmp_path / "scribe.toml"
    cfg.write_text(
        f'[paths]\ndigest_dir = "{tmp_path / "digests"}"\n'
        f'eventlog_dir = "{tmp_path / "eventlogs"}"\n'
        f'transcript_dir = "{tmp_path / "transcripts"}"\n'
        f'state_db = "{db}"\n'
    )
    (tmp_path / "digests").mkdir()
    (tmp_path / "eventlogs").mkdir()
    (tmp_path / "transcripts").mkdir()

    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent.parent / "src"))
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "scribe", "recover", "--config", str(cfg), "--state", str(db)],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).parent.parent,
    )
    assert proc.returncode == EXIT_STATE_INCOMPATIBLE, proc.stderr
