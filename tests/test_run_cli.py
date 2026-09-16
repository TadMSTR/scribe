"""Tests for `python -m scribe run` and the top-level dispatcher.

The most important property is the default: dry run. Phase 6 is a shadow run, so the mode
that spends money and sends data off the machine has to be asked for by name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scribe.__main__ import main as dispatch
from scribe.run_cli import main as run_main

FIXTURES = Path(__file__).parent / "fixtures"

CONFIG = """
[extract]
quiet_period_minutes = 15

[discovery]
project_globs = ["{globs}"]
output_dir    = "{out}"
state_path    = "{state}"

[summarize.session]
provider = "mistral"
model    = "mistral-small-latest"

[providers.mistral]
type     = "openai-compatible"
base_url = "https://api.example.test/v1"
api_key  = {{ env = "SCRIBE_ABSENT_KEY" }}
"""


@pytest.fixture
def workspace(tmp_path):
    projects = tmp_path / "projects" / "-home-ted--claude-projects-research"
    projects.mkdir(parents=True)
    target = projects / "sess.jsonl"
    target.write_bytes((FIXTURES / "transcript-structural.jsonl").read_bytes())
    old = 1.0
    os.utime(target, (old, old))  # long idle, so the quiet period has passed
    cfg = tmp_path / "scribe.toml"
    cfg.write_text(
        CONFIG.format(
            globs=str(tmp_path / "projects" / "*") + "/",
            out=str(tmp_path / "out"),
            state=str(tmp_path / "state.sqlite3"),
        ),
        encoding="utf-8",
    )
    return cfg, tmp_path


def test_the_default_is_a_dry_run(workspace, capsys) -> None:
    cfg, tmp = workspace
    assert run_main(["--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("dry run:")
    assert "1 session" in out
    assert not (tmp / "out").exists(), "a dry run must write nothing"


def test_a_dry_run_needs_no_credential(workspace, monkeypatch) -> None:
    """The provider is built only when it is needed, so a dry run never reads its key."""
    cfg, _tmp = workspace
    monkeypatch.delenv("SCRIBE_ABSENT_KEY", raising=False)
    assert run_main(["--config", str(cfg)]) == 0


def test_json_output_carries_totals_and_sessions(workspace, capsys) -> None:
    cfg, _tmp = workspace
    run_main(["--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["sessions"] == 1
    assert payload["totals"]["events_total"] == 3
    assert payload["sessions"][0]["dry_run"] is True


def test_limit_is_honoured(workspace, capsys) -> None:
    cfg, tmp = workspace
    src = tmp / "projects" / "-home-ted--claude-projects-research" / "sess.jsonl"
    for name in ("b.jsonl", "c.jsonl"):
        sibling = src.parent / name
        sibling.write_bytes(src.read_bytes())
        os.utime(sibling, (1.0, 1.0))
    run_main(["--config", str(cfg), "--limit", "2", "--json"])
    assert json.loads(capsys.readouterr().out)["totals"]["sessions"] == 2


def test_a_missing_config_exits_2(tmp_path, capsys) -> None:
    assert run_main(["--config", str(tmp_path / "absent.toml")]) == 2
    assert "config not found" in capsys.readouterr().err


def test_a_malformed_config_exits_2(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("= = not toml")
    assert run_main(["--config", str(bad)]) == 2


def test_a_live_run_with_an_undeclared_stage_exits_2(tmp_path, capsys) -> None:
    """A config error must be distinguishable from a QC failure."""
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[discovery]\nproject_globs = ["{tmp_path}/none/*/"]\n'
        f'state_path = "{tmp_path}/s.sqlite3"\n'
    )
    # No sessions, so the provider is never built and the sweep is a no-op success.
    assert run_main(["--config", str(cfg), "--live"]) == 0


def test_state_can_be_overridden(workspace, tmp_path) -> None:
    cfg, _tmp = workspace
    alt = tmp_path / "alt.sqlite3"
    run_main(["--config", str(cfg), "--state", str(alt)])
    assert alt.exists()


def test_errors_are_reported_per_session_in_the_human_output(
    workspace, capsys, monkeypatch
) -> None:
    """A run report that hides which session failed is the blind spot this replaces."""
    cfg, _tmp = workspace
    from scribe import pipeline

    real = pipeline.run_once

    def with_error(*a, **k):
        results = real(*a, **k)
        for r in results:
            r.errors.append("synthetic failure")
        return results

    monkeypatch.setattr("scribe.run_cli.run_once", with_error)
    run_main(["--config", str(cfg)])
    out = capsys.readouterr().out
    assert "1 error(s)" in out
    assert "sess.jsonl: synthetic failure" in out


def test_qc_totals_are_reported_when_sessions_were_graded(workspace, capsys, monkeypatch) -> None:
    cfg, _tmp = workspace
    from scribe import pipeline

    real = pipeline.run_once

    def graded(*a, **k):
        results = real(*a, **k)
        for r in results:
            r.qc_ok, r.qc_coverage = True, 0.75
        return results

    monkeypatch.setattr("scribe.run_cli.run_once", graded)
    run_main(["--config", str(cfg)])
    out = capsys.readouterr().out
    assert "QC 1/1 passed" in out
    assert "75.0%" in out


def test_a_config_error_raised_during_the_sweep_exits_2(workspace, capsys, monkeypatch) -> None:
    """Distinguishable from a QC failure: 2 means the run could not be set up."""
    from scribe.config import ConfigError

    cfg, _tmp = workspace

    def boom(*a, **k):
        raise ConfigError("stage 'session' names provider 'gone', which is not declared")

    monkeypatch.setattr("scribe.run_cli.run_once", boom)
    assert run_main(["--config", str(cfg), "--live"]) == 2
    assert "not declared" in capsys.readouterr().err


# --- dispatcher --------------------------------------------------------------------


def test_dispatch_routes_extract(capsys) -> None:
    assert dispatch(["extract", str(FIXTURES / "transcript-structural.jsonl"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1


def test_dispatch_routes_qc(capsys) -> None:
    code = dispatch(
        [
            "qc",
            "--digest",
            str(FIXTURES / "hallucinated-digest.json"),
            "--events",
            str(FIXTURES / "events.json"),
        ]
    )
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_dispatch_routes_run(workspace, capsys) -> None:
    cfg, _tmp = workspace
    assert dispatch(["run", "--config", str(cfg)]) == 0
    assert "dry run:" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["nonsense"]])
def test_dispatch_rejects_an_unknown_subcommand(argv, capsys) -> None:
    assert dispatch(argv) == 2
    assert "usage:" in capsys.readouterr().err


# --- vikunja#849: a lost summary has to be loud on the surface that reports a run ----------


def _with_placeholder(monkeypatch, **flags):
    from scribe import pipeline

    real = pipeline.run_once

    def patched(*a, **k):
        results = real(*a, **k)
        for r in results:
            for name, value in flags.items():
                setattr(r, name, value)
        return results

    monkeypatch.setattr("scribe.run_cli.run_once", patched)


def test_a_placeholder_is_reported_loudly(workspace, capsys, monkeypatch) -> None:
    """A run that did not write a summary must not read like a clean one.

    The counter existing in the JSON is not the same as an operator seeing it: the backfill
    is driven from the printed output, and `written: 5 / summarized: 4` is what made 13 lost
    sessions look like a successful run.

    The wording changed with #872: a placeholder is no longer a loss, because the session is
    retryable and the block will be replaced. It is still shouted about — it is a digest that
    does not exist yet — but the word LOST now belongs to the `discarded` line, which is the
    case where a digest really was destroyed.
    """
    cfg, _tmp = workspace
    _with_placeholder(monkeypatch, placeholder=True, written=True)
    run_main(["--config", str(cfg)])
    out = capsys.readouterr().out
    assert "1 placeholder(s) written" in out
    assert "!!" in out
    assert "scribe recover" in out


def test_a_discarded_digest_is_the_one_reported_as_lost(workspace, capsys, monkeypatch) -> None:
    """The genuinely unrecoverable case, and the one that was silent for 30 sessions.

    `summarized` and `written: False` together mean the model produced a digest that never
    reached disk. The replace path should make this unreachable; the line exists so that is
    observable rather than assumed.
    """
    cfg, _tmp = workspace
    _with_placeholder(monkeypatch, placeholder=False, written=False, discarded=True)
    run_main(["--config", str(cfg)])
    out = capsys.readouterr().out
    assert "NOT WRITTEN" in out
    assert "LOST" in out


def test_a_clean_run_says_nothing_about_placeholders(workspace, capsys, monkeypatch) -> None:
    """Pins the line to the count. A banner printed unconditionally is noise, and noise is
    how the next real one gets skipped over."""
    cfg, _tmp = workspace
    _with_placeholder(monkeypatch, placeholder=False)
    run_main(["--config", str(cfg)])
    out = capsys.readouterr().out
    assert "placeholder" not in out
    assert "LOST" not in out


def test_the_placeholder_count_reaches_the_json_totals(workspace, capsys, monkeypatch) -> None:
    cfg, _tmp = workspace
    _with_placeholder(monkeypatch, placeholder=True, written=True)
    run_main(["--config", str(cfg), "--json"])
    totals = json.loads(capsys.readouterr().out)["totals"]
    assert totals["placeholders"] == 1
    assert totals["written"] == 1
