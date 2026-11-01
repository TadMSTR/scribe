"""`scribe session <id>`: one read-only JSON object per session (vikunja#1347, Phase 6).

The shape is a contract (agent isolation will wrap it in a read-only scoped-mcp module), and
two properties matter most: it writes nothing, and it prints no content beyond what the
redacted event log already holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scribe.config import Config, HeadlessConfig, ProviderConfig, StageConfig
from scribe.pipeline import run_once
from scribe.session_cli import EXIT_FOUND, EXIT_UNKNOWN, EXIT_USAGE, SCHEMA, lookup, main
from scribe.state import Store
from scribe.summarize.providers import Completion, Provider
from scribe.writeback import append_block

FIXTURES = Path(__file__).parent / "fixtures"
SID = "sess-subagents"
NOW = 1_000_000.0
CLOSING = "I drafted every doc and closed every task. " * 30


class Stub(Provider):
    name = "stub"
    model = "m"

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        return Completion(
            text=json.dumps({"asked": "a", "done": ["d"]}),
            input_tokens=1,
            output_tokens=1,
            model="m",
            provider="stub",
        )


def _transcript_records() -> list[dict]:
    recs = [
        json.loads(x)
        for x in (FIXTURES / "transcript-subagents.jsonl").read_text().splitlines()
        if x.strip()
    ]
    recs = [r for r in recs if r.get("uuid") not in ("a5", "u7")]  # nothing outstanding
    for r in recs:
        if r.get("uuid") == "a6":
            r["message"]["content"][0]["text"] = CLOSING
    return recs


@pytest.fixture
def swept(tmp_path):
    """A config, and one headless session already swept into it."""
    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-writer"
    d.mkdir(parents=True)
    target = d / f"{SID}.jsonl"
    target.write_text("".join(json.dumps(r) + "\n" for r in _transcript_records()))
    os.utime(target, (NOW - 3600, NOW - 3600))
    cfg = Config(
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
        qc_run_record=str(tmp_path / "runs.jsonl"),
        providers={"stub": ProviderConfig(name="stub", type="openai-compatible")},
        stages={"session": StageConfig(provider="stub", model="m")},
        headless=HeadlessConfig(("sdk-cli",)),
        transcript_retention_days=60,
    )
    run_once(cfg, Store(cfg.state_path), now=NOW, provider_factory=Stub)
    return cfg, target


def _toml(cfg: Config, tmp_path: Path) -> Path:
    p = tmp_path / "scribe.toml"
    p.write_text(
        "[discovery]\n"
        f'project_globs = ["{cfg.project_globs[0]}"]\n'
        f'output_dir = "{cfg.output_dir}"\n'
        f'eventlog_dir = "{cfg.eventlog_dir}"\n'
        f'state_path = "{cfg.state_path}"\n'
        "transcript_retention_days = 60\n"
        f'[qc]\nrun_record = "{cfg.qc_run_record}"\n'
    )
    return p


# --- every section -------------------------------------------------------------------------


def test_the_document_joins_every_source(swept) -> None:
    cfg, target = swept
    doc = lookup(cfg, SID)
    assert doc["schema"] == SCHEMA
    assert doc["found"] is True
    assert (doc["agent"], doc["entrypoint"]) == ("writer", "sdk-cli")
    assert doc["started_at"] == "2026-11-01T00:59:00Z"

    t = doc["transcript"]
    assert t["path"] == str(target) and t["exists"] is True
    assert t["size_bytes"] == target.stat().st_size
    mtime = datetime.fromtimestamp(target.stat().st_mtime, UTC)
    assert t["expires"] == (mtime + timedelta(days=60)).date().isoformat()

    assert doc["state"]["status"] == "summarized"
    assert doc["state"]["transcript_path"] == str(target)

    ev = doc["eventlog"]
    assert ev["schema_version"] == 2
    assert [s["agent_id"] for s in ev["subagents"]] == [
        "afore0000000000001",
        "aback0000000000001",
        "aback0000000000002",
    ]
    assert [c["trigger"] for c in ev["compactions"]] == ["manual"]
    assert ev["stats"]["queued_commands"] == 1

    (block,) = doc["blocks"]
    assert block["render_mode"] == "verbatim" and block["current"] is True
    assert block["provisional"] == ""

    (run,) = doc["runs"]
    assert run["render_mode"] == "verbatim"
    assert run["closing_chars"] == len(CLOSING.strip())

    assert doc["closing_statement"]["chars"] == len(CLOSING.strip())
    assert doc["closing_statement"]["source"] == "final-turn"
    assert doc["closing_statement_reason"] is None


def test_a_session_with_no_event_log_says_so(swept) -> None:
    cfg, _ = swept
    for p in Path(cfg.eventlog_dir).glob("*.json"):
        p.unlink()
    doc = lookup(cfg, SID)
    assert doc["eventlog"] is None
    assert doc["closing_statement"] is None
    assert doc["closing_statement_reason"] == "no event log"
    assert doc["found"] is True


def test_a_version_one_log_reports_not_recorded(swept) -> None:
    cfg, _ = swept
    (path,) = Path(cfg.eventlog_dir).glob("*.json")
    raw = json.loads(path.read_text())
    for key in ("subagents", "compactions", "background_pending", "entrypoint"):
        raw.pop(key)
    raw["schema_version"] = 1
    path.write_text(json.dumps(raw))
    ev = lookup(cfg, SID)["eventlog"]
    assert ev["subagents"] == "not recorded"
    assert ev["compactions"] == "not recorded"
    assert ev["schema_version"] == 1


def test_two_blocks_are_both_listed_and_the_last_final_one_is_current(swept) -> None:
    cfg, target = swept
    (md,) = Path(cfg.output_dir).rglob("*.md")
    append_block(
        md,
        body="- a second render\n",
        session_id=SID,
        turn_uuid="later",
        transcript_path=str(target),
        when=datetime(2026, 11, 1, 0, 59, tzinfo=UTC),
    )
    blocks = lookup(cfg, SID)["blocks"]
    assert [b["turn_uuid"] for b in blocks][-1] == "later"
    assert [b["current"] for b in blocks] == [False, True]


def test_an_expired_transcript_is_not_an_error(swept, tmp_path, capsys) -> None:
    cfg, target = swept
    target.unlink()
    assert main([SID, "--config", str(_toml(cfg, tmp_path))]) == EXIT_FOUND
    doc = json.loads(capsys.readouterr().out)
    assert doc["transcript"]["exists"] is False
    assert doc["transcript"]["expires"] == "unknown"
    assert doc["eventlog"]["subagents"]


# --- the CLI -------------------------------------------------------------------------------


def test_brief_is_a_few_human_lines(swept, tmp_path, capsys) -> None:
    cfg, _ = swept
    assert main([SID, "--brief", "--config", str(_toml(cfg, tmp_path))]) == EXIT_FOUND
    out = capsys.readouterr().out
    assert out.startswith(f"session {SID}  agent writer  entrypoint sdk-cli")
    assert "subagents: 3  compactions: 1" in out
    assert len(out.splitlines()) <= 10


def test_an_unknown_session_exits_one(swept, tmp_path, capsys) -> None:
    cfg, _ = swept
    assert main(["no-such-session", "--config", str(_toml(cfg, tmp_path))]) == EXIT_UNKNOWN
    assert "no trace" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", ".hidden", "x%"])
def test_a_non_id_is_a_usage_error(bad) -> None:
    assert main([bad]) == EXIT_USAGE


# --- the two properties agent isolation relies on -----------------------------------------


def test_it_writes_nothing(swept, tmp_path) -> None:
    """Opened `mode=ro`, never through `Store`, which stamps `user_version` on open.

    `state.sqlite3-shm` is excluded, and only it: in WAL mode every reader updates that
    shared-memory index to coordinate with writers, read-only or not. It holds no database
    content. `immutable=1` would avoid it and could read a torn page while a sweep writes.
    """
    cfg, _ = swept
    db = Path(cfg.state_path)
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 1")  # an older schema `Store` would migrate

    def digest() -> dict[Path, str]:
        return {
            p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tmp_path.rglob("*")
            if p.is_file() and not p.name.endswith("-shm")
        }

    before = digest()
    lookup(cfg, SID)
    assert digest() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_no_content_beyond_the_redacted_event_log(swept) -> None:
    cfg, target = swept
    # Digest and model text in the run record, a secret in the state row's error, and a
    # transcript record the extractor drops: none of it may come out.
    with open(cfg.qc_run_record, "a") as fh:
        fh.write(
            json.dumps(
                {
                    "session_id": SID,
                    "ts": "2026-11-01T02:00:00Z",
                    "claims": [{"claim": "CLAIM-TEXT-FROM-A-DIGEST"}],
                    "contamination_excerpt": "EXCERPT-OF-MODEL-TEXT",
                }
            )
            + "\n"
        )
    with sqlite3.connect(cfg.state_path) as conn:
        conn.execute(
            "UPDATE sessions SET last_error = ?",
            ("provider said GITHUB_TOKEN=ghp_NOTREALaaaabbbbccccdddd11",),
        )
    blob = json.dumps(lookup(cfg, SID))
    assert "CLAIM-TEXT-FROM-A-DIGEST" not in blob
    assert "EXCERPT-OF-MODEL-TEXT" not in blob
    assert "ghp_NOTREAL" not in blob
    assert "SKILL LISTING NEVER EXTRACTED" in target.read_text()
    assert "SKILL LISTING NEVER EXTRACTED" not in blob


def test_an_underscore_in_the_id_is_not_a_wildcard(tmp_path) -> None:
    """`_` is a LIKE wildcard; `a_b` must not pick up the row for transcript `aXb.jsonl`."""
    db = tmp_path / "state.sqlite3"
    Store(db).upsert_observed("/p/aXb.jsonl", size_bytes=1, mtime_ns=1, agent="x")
    cfg = Config(
        state_path=str(db),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "ev"),
        project_globs=(str(tmp_path / "none") + "/",),
        qc_run_record="",
    )
    assert lookup(cfg, "a_b")["state"] is None
    assert lookup(cfg, "aXb")["state"]["transcript_path"] == "/p/aXb.jsonl"


def test_a_state_row_outside_the_project_globs_is_not_statted(tmp_path) -> None:
    db = tmp_path / "state.sqlite3"
    Store(db).upsert_observed("/etc/passwd", size_bytes=1, mtime_ns=1, agent="x")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET session_id = 'probe'")
    cfg = Config(
        state_path=str(db),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "ev"),
        project_globs=(str(tmp_path / "projects" / "*") + "/",),
        qc_run_record="",
    )
    t = lookup(cfg, "probe")["transcript"]
    assert t["exists"] is False and t["size_bytes"] is None
    assert t["outside_project_globs"] is True


def test_an_unreadable_state_db_is_an_error_not_an_unknown_session(tmp_path, capsys) -> None:
    """Baseline OV-01/OV-27: a DB that cannot be read must not read as "no trace"."""
    from scribe.session_cli import EXIT_FAILED

    db = tmp_path / "state.sqlite3"
    db.write_bytes(b"not a sqlite database at all, just bytes" * 10)
    toml = tmp_path / "scribe.toml"
    toml.write_text(
        f'[discovery]\nstate_path = "{db}"\noutput_dir = "{tmp_path / "out"}"\n'
        f'eventlog_dir = "{tmp_path / "ev"}"\nproject_globs = ["{tmp_path / "none"}/"]\n'
        '[qc]\nrun_record = ""\n'
    )
    assert main(["some-session", "--config", str(toml)]) == EXIT_FAILED
    doc = json.loads(capsys.readouterr().out)
    assert doc["errors"] and doc["errors"][0].startswith("state: ")


def test_a_crash_inside_the_lookup_keeps_the_exit_contract(swept, tmp_path, monkeypatch) -> None:
    from scribe import session_cli

    cfg, _ = swept

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(session_cli, "lookup", boom)
    assert main([SID, "--config", str(_toml(cfg, tmp_path))]) == session_cli.EXIT_FAILED


def test_a_transcript_that_cannot_be_statted_is_an_error_not_gone(swept, monkeypatch) -> None:
    """Baseline SH-15: EACCES on the transcript must not read as "expired"."""
    from pathlib import Path as _P

    cfg, target = swept
    real = _P.stat

    def denied(self, *a, **k):
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(_P, "stat", denied)
    doc = lookup(cfg, SID)
    assert doc["transcript"]["exists"] is False
    assert "transcript: PermissionError" in doc["errors"]


def test_a_found_session_with_an_unreadable_source_exits_three(swept, tmp_path, capsys) -> None:
    """Baseline OV-01 recheck: a partial answer must not exit 0."""
    from scribe.session_cli import EXIT_FAILED

    cfg, _ = swept
    (md,) = Path(cfg.output_dir).rglob("*.md")
    md.chmod(0)
    try:
        if os.access(md, os.R_OK):  # running as root: the chmod cannot deny the read
            pytest.skip("root reads a 0-mode file")
        assert main([SID, "--brief", "--config", str(_toml(cfg, tmp_path))]) == EXIT_FAILED
        assert "!! incomplete -- could not read: digest " in capsys.readouterr().out
    finally:
        md.chmod(0o600)


def test_containment_is_one_level_not_any_depth(tmp_path) -> None:
    """Baseline OV-52: `<project>/<sid>/subagents/x.jsonl` is not a transcript location."""
    from scribe.session_cli import _contained

    proj = tmp_path / "projects" / "p1"
    (proj / "sid" / "subagents").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    cfg = Config(project_globs=(str(tmp_path / "projects" / "*") + "/",))
    assert _contained(cfg, proj / "sid.jsonl") == (proj / "sid.jsonl").resolve()
    assert _contained(cfg, proj / "sid" / "subagents" / "agent-a.jsonl") is None
    # Baseline IV-21/27/28: `..` through a matching prefix must not escape.
    assert _contained(cfg, proj / ".." / ".." / "elsewhere" / "f.jsonl") is None
