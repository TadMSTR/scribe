"""Tests for truncation being *visible* (vikunja#887).

#887 asked for `truncated_items` to accumulate across runs and then be looked at. It could
not be: `summarize_run` aggregated the figure correctly and `run_cli`'s human branch never
printed it, only `--json` did, and the cron does not pass `--json`. `grep -c truncated
~/.pm2/logs/scribe-out.log` returned 0 for the whole period the counter had existed.

That is AGENTS.md invariant 13 one level out — a counter that can only be reached by a flag
nobody passes is not a counter. These tests pin the *output*, not the arithmetic, because the
arithmetic was never the broken half.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scribe.pipeline import SessionResult, summarize_run
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
def config(tmp_path):
    projects = tmp_path / "projects" / "-home-ted--claude-projects-research"
    projects.mkdir(parents=True)
    target = projects / "sess.jsonl"
    target.write_bytes((FIXTURES / "transcript-structural.jsonl").read_bytes())
    os.utime(target, (1.0, 1.0))
    cfg = tmp_path / "scribe.toml"
    cfg.write_text(
        CONFIG.format(
            globs=str(tmp_path / "projects" / "*") + "/",
            out=str(tmp_path / "out"),
            state=str(tmp_path / "state.sqlite3"),
        ),
        encoding="utf-8",
    )
    return cfg


def _truncated(**fields: int) -> SessionResult:
    return SessionResult(
        transcript_path="/p/t.jsonl",
        truncated_fields=dict(fields),
        truncated_items=sum(fields.values()),
    )


def test_totals_keep_the_field_identity() -> None:
    """ "148 items dropped" and "91 from found, 42 from decisions, 15 from open_items" are
    different facts, and only the second says which cap to look at. The dict was collapsed to
    a scalar at the point of recording, so the first live firing could not be localised."""
    totals = summarize_run([_truncated(found=91, decisions=42), _truncated(found=8)])
    assert totals["truncated_items"] == 141
    assert totals["truncated"] == 2
    assert totals["truncated_fields"] == {"found": 99, "decisions": 42}


def test_the_field_breakdown_is_ordered_by_size() -> None:
    """The reason to read it is to find the cap costing the most; alphabetical buries it."""
    totals = summarize_run([_truncated(open_items=3, found=91, decisions=42)])
    assert list(totals["truncated_fields"]) == ["found", "decisions", "open_items"]


def test_truncation_is_printed_without_json(config, capsys, monkeypatch) -> None:
    """**The actual #887 defect.** Not the arithmetic — the fact that nothing printed it.

    A test asserting `summarize_run`'s output would have passed throughout the period the
    number was invisible, which is why this drives `main()` and reads stdout.
    """
    monkeypatch.setattr(
        "scribe.run_cli.run_once",
        lambda *a, **k: [_truncated(found=91, decisions=42, open_items=15)],
    )
    assert run_main(["--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "--json" not in out
    assert "1 digest(s) truncated" in out
    assert "148 item(s) dropped" in out
    assert "91 from found" in out and "42 from decisions" in out and "15 from open_items" in out


def test_a_clean_sweep_says_nothing_about_truncation(config, capsys, monkeypatch) -> None:
    """Pins the line to the outcome. Without this, an unconditional print would pass above."""
    monkeypatch.setattr(
        "scribe.run_cli.run_once", lambda *a, **k: [SessionResult(transcript_path="/p/t.jsonl")]
    )
    assert run_main(["--config", str(config)]) == 0
    assert "truncated" not in capsys.readouterr().out


def test_truncation_is_quieter_than_a_lost_summary(config, capsys, monkeypatch) -> None:
    """A truncated digest is written; a placeholder is a summary that is gone. The `!!` prefix
    is reserved for loss, and this line must not borrow it — #884's whole argument is that
    shortening a digest beats discarding one, and a report that shouts equally at both
    undoes the distinction it was won with."""
    monkeypatch.setattr("scribe.run_cli.run_once", lambda *a, **k: [_truncated(found=91)])
    assert run_main(["--config", str(config)]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "truncated" in ln)
    assert "!!" not in line


def test_the_run_type_is_recorded_and_split_in_the_totals() -> None:
    """A full read hands the model a whole session; an incremental sweep hands it a growing
    one. Nothing distinguished them, which is the comparison #887 asked for and could not
    make. `last_offset == 0` covers both input-bounded cases — a recovery re-read AND a
    first-time read of an already-finished transcript — and costs no state-DB column."""
    full = _truncated(found=10)
    full.full_read = True
    incremental = _truncated(found=4)
    totals = summarize_run([full, incremental])
    assert totals["full_read"] == 1
    assert totals["truncated_full_read"] == 1
    assert totals["truncated"] == 2


def test_the_breakdown_reaches_the_json_report() -> None:
    """The run report is JSON before it is a printed line; a field missing from `to_dict` is
    invisible to anything consuming the shadow comparison."""
    d = _truncated(found=91).to_dict()
    assert d["truncated_fields"] == {"found": 91}
    assert d["full_read"] is False
    assert json.dumps(d)
