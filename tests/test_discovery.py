"""Tests for filesystem session discovery and quiet-period completion.

Time is injected rather than slept, so the quiet-period logic is tested at its boundary
instead of approximately.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scribe.config import Config
from scribe.discovery import iter_transcripts, orphaned, scan
from scribe.eventlog import write_eventlog
from scribe.extract.models import EventLog
from scribe.state import STATUS_ACTIVE, STATUS_COMPLETE, Store

QUIET = 15  # minutes
QUIET_S = QUIET * 60


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    (projects / "-home-ted--claude-projects-research").mkdir(parents=True)
    (projects / "-home-ted--claude-projects-writer").mkdir(parents=True)
    cfg = Config(
        quiet_period_minutes=QUIET,
        project_globs=(str(projects / "*") + "/",),
        exclude=(".memsearch",),
    )
    return cfg, Store(tmp_path / "s.sqlite3"), projects


def _touch(path, *, content: str = "{}\n", age_seconds: float = 0.0, now: float = 1_000_000.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    stamp = now - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_finds_transcripts_across_project_dirs(env) -> None:
    cfg, _store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / "a.jsonl")
    _touch(projects / "-home-ted--claude-projects-writer" / "b.jsonl")
    found = sorted(d.path.name for d in iter_transcripts(cfg.project_globs, now=1_000_000.0))
    assert found == ["a.jsonl", "b.jsonl"]


def test_agent_is_recovered_from_the_project_dir(env) -> None:
    cfg, _store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / "a.jsonl")
    (found,) = list(iter_transcripts(cfg.project_globs, now=1_000_000.0))
    assert found.agent == "research"


def test_non_jsonl_files_are_ignored(env) -> None:
    cfg, _store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / "notes.md")
    assert list(iter_transcripts(cfg.project_globs, now=1_000_000.0)) == []


def test_excluded_path_components_are_skipped(env) -> None:
    cfg, _store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / ".memsearch" / "c.jsonl")
    cfg.project_globs = (str(projects / "*" / "*") + "/",)
    assert list(iter_transcripts(cfg.project_globs, cfg.exclude, now=1_000_000.0)) == []


def test_idle_seconds_is_measured_from_mtime(env) -> None:
    cfg, _store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / "a.jsonl", age_seconds=300)
    (found,) = list(iter_transcripts(cfg.project_globs, now=1_000_000.0))
    assert 299 <= found.idle_seconds <= 301


def test_a_file_newer_than_now_reports_zero_idle_not_negative(env) -> None:
    """Clock skew, or a copy that reset the timestamp, must not produce a negative age."""
    cfg, _store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / "a.jsonl", age_seconds=-500)
    (found,) = list(iter_transcripts(cfg.project_globs, now=1_000_000.0))
    assert found.idle_seconds == 0.0


def test_active_session_is_not_ready(env) -> None:
    cfg, store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / "a.jsonl", age_seconds=QUIET_S - 60)
    assert scan(cfg, store, now=1_000_000.0) == []
    assert store.get(str(projects / "-home-ted--claude-projects-research" / "a.jsonl")).status == (
        STATUS_ACTIVE
    )


def test_idle_session_becomes_ready(env) -> None:
    cfg, store, projects = env
    p = _touch(
        projects / "-home-ted--claude-projects-research" / "a.jsonl", age_seconds=QUIET_S + 60
    )
    ready = scan(cfg, store, now=1_000_000.0)
    assert [r.transcript_path for r in ready] == [str(p)]
    assert store.get(str(p)).status == STATUS_COMPLETE


def test_the_quiet_period_boundary(env) -> None:
    cfg, store, projects = env
    p = projects / "-home-ted--claude-projects-research" / "a.jsonl"
    _touch(p, age_seconds=QUIET_S - 1)
    assert scan(cfg, store, now=1_000_000.0) == []
    _touch(p, age_seconds=QUIET_S)
    assert len(scan(cfg, store, now=1_000_000.0)) == 1


def test_a_summarized_session_is_not_offered_again(env) -> None:
    """Without the unread-bytes condition, "idle for 15 minutes" is permanently true of
    every transcript that will never be touched again — which is most of them."""
    cfg, store, projects = env
    p = _touch(
        projects / "-home-ted--claude-projects-research" / "a.jsonl", age_seconds=QUIET_S + 60
    )
    (row,) = scan(cfg, store, now=1_000_000.0)
    store.mark_summarized(str(p), offset=row.size_bytes, last_turn_uuid="u1", turn_uuids=["u1"])
    assert scan(cfg, store, now=1_000_000.0) == []
    assert scan(cfg, store, now=1_000_000.0) == []


def test_a_resumed_session_is_offered_again(env) -> None:
    """The case the idle heuristic gets wrong, and how it recovers."""
    cfg, store, projects = env
    p = _touch(
        projects / "-home-ted--claude-projects-research" / "a.jsonl", age_seconds=QUIET_S + 60
    )
    (row,) = scan(cfg, store, now=1_000_000.0)
    store.mark_summarized(str(p), offset=row.size_bytes, last_turn_uuid="u1", turn_uuids=["u1"])
    assert scan(cfg, store, now=1_000_000.0) == []

    _touch(p, content='{}\n{"resumed": true}\n', age_seconds=QUIET_S + 60)
    ready = scan(cfg, store, now=1_000_000.0)
    assert [r.transcript_path for r in ready] == [str(p)]


def test_a_session_that_becomes_active_again_reverts_status(env) -> None:
    cfg, store, projects = env
    p = _touch(
        projects / "-home-ted--claude-projects-research" / "a.jsonl", age_seconds=QUIET_S + 60
    )
    scan(cfg, store, now=1_000_000.0)
    assert store.get(str(p)).status == STATUS_COMPLETE
    _touch(p, content='{}\n{"more": 1}\n', age_seconds=0)
    assert scan(cfg, store, now=1_000_000.0) == []
    assert store.get(str(p)).status == STATUS_ACTIVE


def test_a_vanished_file_is_skipped_not_fatal(env, monkeypatch) -> None:
    cfg, _store, projects = env
    _touch(projects / "-home-ted--claude-projects-research" / "a.jsonl")
    real_stat = os.stat

    def boom(path, *a, **k):
        if str(path).endswith("a.jsonl"):
            raise FileNotFoundError(2, "gone")
        return real_stat(path, *a, **k)

    monkeypatch.setattr("pathlib.Path.stat", lambda self, **k: boom(self))
    assert list(iter_transcripts(cfg.project_globs, now=1_000_000.0)) == []


def test_scan_records_the_agent(env) -> None:
    cfg, store, projects = env
    p = _touch(projects / "-home-ted--claude-projects-writer" / "a.jsonl", age_seconds=QUIET_S + 60)
    scan(cfg, store, now=1_000_000.0)
    assert store.get(str(p)).agent == "writer"


def test_empty_project_tree_yields_nothing(env) -> None:
    cfg, store, _projects = env
    assert scan(cfg, store, now=1_000_000.0) == []


# --- vikunja#873: the second source, and the thing it must never touch ---------------------


@pytest.fixture
def orphan_env(tmp_path):
    """A session whose transcript is gone and whose event log survives."""
    cfg = Config(
        quiet_period_minutes=QUIET,
        project_globs=(str(tmp_path / "projects" / "*") + "/",),
        eventlog_dir=str(tmp_path / "eventlogs"),
    )
    store = Store(tmp_path / "s.sqlite3")
    tpath = str(tmp_path / "projects" / "-home-ted--claude-projects-research" / "sess-a.jsonl")
    store.upsert_observed(tpath, size_bytes=4096, mtime_ns=1, agent="research", session_id="sess-a")
    write_eventlog(cfg.eventlog_dir, EventLog(session_id="sess-a", transcript_path=tpath))
    return cfg, store, tpath


def test_a_reopened_session_with_no_transcript_is_offered(orphan_env) -> None:
    """`scan` cannot reach these -- it discovers from the filesystem and the file is gone."""
    cfg, store, tpath = orphan_env
    store.reset_for_retry(tpath)
    assert [r.transcript_path for r in orphaned(cfg, store)] == [tpath]
    assert scan(cfg, store, now=1_000_000.0) == [], "scan cannot see a file that is not there"


def test_a_summarized_orphan_is_never_offered(orphan_env) -> None:
    """The guard that stops this feature destroying something.

    18 event logs on forge currently have no transcript and **16 of them already hold real
    digests**. A rule phrased as "replay every orphaned log" would re-summarize those sixteen
    and overwrite good digests with a replay. Replay is opt-in: only a deliberate
    `scribe recover --apply` moves a row out of `summarized`, and it only does that for a
    session whose block on disk is admittedly a stand-in.
    """
    cfg, store, tpath = orphan_env
    store.mark_summarized(tpath, offset=4096, last_turn_uuid="u1", turn_uuids=["u1"])
    assert orphaned(cfg, store) == []


def test_an_orphan_with_no_event_log_is_not_offered(tmp_path) -> None:
    """Nothing to replay from. Offering it would burn a sweep rediscovering that."""
    cfg = Config(eventlog_dir=str(tmp_path / "eventlogs"))
    store = Store(tmp_path / "s.sqlite3")
    tpath = str(tmp_path / "gone.jsonl")
    store.upsert_observed(tpath, size_bytes=4096, mtime_ns=1, agent="research")
    store.reset_for_retry(tpath)
    assert orphaned(cfg, store) == []


def test_a_session_whose_transcript_still_exists_is_left_to_scan(orphan_env, tmp_path) -> None:
    """The two sources must not both yield the same row, or it is processed twice in a sweep."""
    cfg, store, tpath = orphan_env
    _touch(Path(tpath), age_seconds=QUIET_S + 1)
    store.reset_for_retry(tpath)
    assert orphaned(cfg, store) == []
    assert [r.transcript_path for r in scan(cfg, store, now=1_000_000.0)] == [tpath]
