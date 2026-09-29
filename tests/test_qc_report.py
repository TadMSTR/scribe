"""`qc-report`: the statistics, and the exit-code contract a scheduler will alert on.

Every code 0-4 is reached here from the CLI, not from `evaluate` alone: the contract is what
the process returns, and #875 was a success path that returned 1 while every unit test passed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scribe.__main__ import main as scribe_main
from scribe.qc_report import (
    EXIT_INSUFFICIENT,
    EXIT_OK,
    EXIT_REGRESSION,
    EXIT_TOOL_FAILURE,
    EXIT_USAGE,
    stats,
)
from scribe.qc_report_cli import main

END = datetime(2026, 10, 1, tzinfo=UTC)
UNTIL = END.isoformat()


def rec(
    *,
    days_ago: float = 1,
    ok: bool = True,
    coverage: float = 0.5,
    absent: int = 0,
    source: str = "live",
    placeholder: bool = False,
    agent: str = "developer",
    model: str | None = "m-1",
    **extra,
) -> dict:
    ts = (END - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    checks = {} if ok else {"groundedness": max(absent, 1)}
    return {
        "ts": ts,
        "source": source,
        "agent": agent,
        "scribe_version": "0.12.0",
        "prompt_sha256": "abc",
        "model_resolved": model,
        "placeholder": placeholder,
        "qc_ok": ok,
        "qc_coverage": coverage,
        "findings_by_check": checks,
        "findings_by_kind": {"path": absent} if absent else {},
        "findings_by_bucket": {"path": {"absent": absent}} if absent else {},
        "full_read": False,
        "truncated_items": 0,
        "lag_s": 60.0,
        **extra,
    }


def write(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def run(path: Path, *args: str) -> int:
    return main(["--run-record", str(path), "--until", UNTIL, "--json", *args])


@pytest.fixture
def record(tmp_path) -> Path:
    return tmp_path / "runs.jsonl"


# --- the exit-code contract -----------------------------------------------------------


def test_exit_0_with_enough_sessions_and_no_thresholds(record, capsys) -> None:
    write(record, [rec(ok=False) for _ in range(25)])
    assert run(record) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["current"]["pass_rate"] == 0.0, "a terrible rate with no threshold is report-only"


def test_exit_1_when_an_absolute_floor_is_breached(record) -> None:
    write(record, [rec(ok=i < 10) for i in range(25)])
    assert run(record, "--min-pass-rate", "0.5") == EXIT_REGRESSION
    assert run(record, "--min-pass-rate", "0.3") == EXIT_OK


def test_exit_1_on_absent_findings_per_session(record) -> None:
    write(record, [rec(ok=False, absent=2) for _ in range(25)])
    assert run(record, "--max-absent-per-session", "1") == EXIT_REGRESSION


def test_exit_2_on_usage_and_config_errors(record, tmp_path) -> None:
    write(record, [rec() for _ in range(25)])
    assert run(record, "--days", "0") == EXIT_USAGE
    assert run(record, "--min-pass-rate", "1.5") == EXIT_USAGE
    bad = tmp_path / "bad.toml"
    bad.write_text("[qc]\nmin_pass_rat = 0.5\n")
    assert run(record, "--config", str(bad)) == EXIT_USAGE
    assert main(["--run-record", "", "--json"]) == EXIT_USAGE
    assert main(["--run-record", str(record), "--until", "yesterday"]) == EXIT_USAGE
    with pytest.raises(SystemExit) as exc:
        run(record, "--by", "colour")
    assert exc.value.code == EXIT_USAGE


def test_exit_3_below_min_sessions_even_when_the_rate_is_terrible(record) -> None:
    """1 and 3 are distinct: too little data is no verdict, not a regression and not a pass."""
    write(record, [rec(ok=False) for _ in range(5)])
    assert run(record, "--min-pass-rate", "0.9") == EXIT_INSUFFICIENT
    assert run(record) == EXIT_INSUFFICIENT


def test_exit_3_on_a_missing_record(tmp_path) -> None:
    """A fresh install has no record yet. That is no data, not a broken tool."""
    assert run(tmp_path / "never-written.jsonl") == EXIT_INSUFFICIENT


def test_exit_4_on_an_unreadable_record(tmp_path) -> None:
    assert run(tmp_path) == EXIT_TOOL_FAILURE  # a directory: IsADirectoryError


def test_exit_4_on_too_many_corrupt_lines(record) -> None:
    good = "".join(json.dumps(rec()) + "\n" for _ in range(25))
    record.write_text(good + "{torn\n" * 5)
    assert run(record) == EXIT_TOOL_FAILURE


def test_one_torn_line_is_tolerated(record) -> None:
    good = "".join(json.dumps(rec()) + "\n" for _ in range(25))
    record.write_text(good + '{"torn": \n')
    assert run(record) == EXIT_OK


def test_an_unexpected_crash_is_a_tool_failure_not_a_verdict(record, monkeypatch) -> None:
    write(record, [rec() for _ in range(25)])

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr("scribe.qc_report_cli.build", boom)
    assert run(record) == EXIT_TOOL_FAILURE


def test_the_subcommand_is_dispatched(record) -> None:
    write(record, [rec() for _ in range(25)])
    assert scribe_main(["qc-report", "--run-record", str(record), "--until", UNTIL]) == 0


# --- relative thresholds --------------------------------------------------------------


def test_a_drop_against_the_previous_window_is_a_regression(record) -> None:
    before = [rec(days_ago=10, ok=True) for _ in range(25)]
    after = [rec(days_ago=1, ok=i < 15) for i in range(25)]
    write(record, before + after)
    assert run(record, "--max-pass-rate-drop", "0.2") == EXIT_REGRESSION
    assert run(record, "--max-pass-rate-drop", "0.5") == EXIT_OK


def test_a_thin_previous_window_leaves_the_relative_check_unanswered(record, capsys) -> None:
    before = [rec(days_ago=10) for _ in range(3)]
    after = [rec(days_ago=1, ok=False) for _ in range(25)]
    write(record, before + after)
    assert run(record, "--max-pass-rate-drop", "0.1") == EXIT_INSUFFICIENT
    assert "previous window" in json.loads(capsys.readouterr().out)["unchecked"][0]


def test_an_absolute_breach_still_reports_when_the_previous_window_is_thin(record) -> None:
    write(record, [rec(days_ago=1, ok=False) for _ in range(25)])
    args = ("--max-pass-rate-drop", "0.1", "--min-pass-rate", "0.5")
    assert run(record, *args) == EXIT_REGRESSION


def test_thresholds_come_from_the_toml_and_flags_override_them(record, tmp_path) -> None:
    write(record, [rec(ok=i < 10) for i in range(25)])
    cfg = tmp_path / "scribe.toml"
    cfg.write_text("[qc]\nmin_pass_rate = 0.5\nmin_sessions = 5\n")
    assert run(record, "--config", str(cfg)) == EXIT_REGRESSION
    assert run(record, "--config", str(cfg), "--min-pass-rate", "0.3") == EXIT_OK


# --- what the report says -------------------------------------------------------------


def test_backfill_is_not_pooled_with_live_by_default(record, capsys) -> None:
    write(record, [rec(ok=False, source="backfill") for _ in range(30)] + [rec()] * 25)
    run(record)
    live = json.loads(capsys.readouterr().out)["current"]
    assert (live["graded"], live["pass_rate"]) == (25, 1.0)
    run(record, "--source", "all")
    assert json.loads(capsys.readouterr().out)["current"]["graded"] == 55


def test_placeholders_are_excluded_from_the_pass_rate() -> None:
    s = stats([rec(), rec(ok=False, placeholder=True)])
    assert (s["sessions"], s["graded"], s["placeholders"], s["pass_rate"]) == (2, 1, 1, 1.0)


def test_the_two_statistics_are_reported_separately() -> None:
    """Pass rate is per session over every check; groundedness is per block, one check."""
    floor_only = rec(ok=False, findings_by_check={"event_coverage": 1})
    s = stats([rec(), floor_only, rec(ok=False, absent=1)])
    assert s["pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["groundedness"] == {"blocks_failing": 1, "failure_rate": pytest.approx(1 / 3, 1e-3)}
    assert s["coverage_floor_failed"] == 1


def test_findings_are_broken_out_by_kind_and_bucket() -> None:
    a = rec(
        ok=False,
        findings_by_check={"groundedness": 3},
        findings_by_kind={"path": 2, "ticket": 1},
        findings_by_bucket={"path": {"suffix": 1, "absent": 1}, "ticket": {"id-conflation": 1}},
    )
    f = stats([a, rec()])["findings"]
    assert f["by_bucket"] == {"path": {"absent": 1, "suffix": 1}, "ticket": {"id-conflation": 1}}
    assert (f["absent_total"], f["absent_per_session"]) == (1, 0.5)


def test_truncation_is_split_by_read_type_with_denominators() -> None:
    rows = [
        rec(full_read=True, truncated_items=3),
        rec(full_read=True),
        rec(full_read=False),
        rec(full_read=None),
    ]
    tr = stats(rows)["truncation"]
    assert tr["full_read"] == {"sessions": 2, "truncated": 1, "rate": 0.5}
    assert tr["incremental"] == {"sessions": 1, "truncated": 0, "rate": 0.0}
    assert tr["unknown"] == 1


def test_grouping_and_new_model_values(record, capsys) -> None:
    rows = [rec(days_ago=10, model="m-1") for _ in range(3)]
    rows += [rec(agent="research", model="m-2") for _ in range(3)] + [rec() for _ in range(3)]
    write(record, rows)
    run(record, "--by", "agent", "--compare", "--min-sessions", "1")
    out = json.loads(capsys.readouterr().out)
    assert [g["key"] for g in out["groups"]] == [{"agent": "developer"}, {"agent": "research"}]
    assert out["new_models"] == ["m-2"]


def test_the_text_report_labels_both_statistics(record, capsys) -> None:
    write(record, [rec(post_render_redactions=1)] + [rec() for _ in range(24)])
    assert main(["--run-record", str(record), "--until", UNTIL]) == EXIT_OK
    out = capsys.readouterr().out
    assert "per session, all checks" in out and "per block" in out
    assert "!! 1 post-render redaction" in out
    assert "report-only" in out


# --- backfill through the CLI ---------------------------------------------------------


def test_backfill_flag_on_an_empty_corpus(record, tmp_path, capsys) -> None:
    cfg = tmp_path / "scribe.toml"
    cfg.write_text(
        f'[discovery]\noutput_dir = "{tmp_path / "d"}"\neventlog_dir = "{tmp_path / "e"}"\n'
    )
    assert run(record, "--config", str(cfg), "--backfill") == EXIT_OK
    assert json.loads(capsys.readouterr().out)["blocks"] == 0


def test_the_text_report_prints_groups_breaches_and_unchecked(record, capsys) -> None:
    rows = [rec(days_ago=10) for _ in range(3)]
    rows += [rec(ok=False, model="m-2", discarded=True) for _ in range(25)]
    write(record, rows)
    code = main(
        [
            "--run-record",
            str(record),
            "--until",
            UNTIL,
            "--by",
            "prompt",
            "--min-pass-rate",
            "0.5",
            "--max-pass-rate-drop",
            "0.1",
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_REGRESSION
    assert "previous:" in out and "[prompt=abc]" in out
    assert "new model value(s) this window: m-2" in out
    assert "!! min_pass_rate" in out and "unchecked:" in out
    assert "produced but not written" in out
    assert "verdict: REGRESSION (exit 1)" in out


def test_backfill_text_output_and_write_failure(record, monkeypatch, capsys) -> None:
    from scribe import runrecord

    failed = runrecord.Backfill(blocks=2, write_errors=2, first_error="OSError: full")
    monkeypatch.setattr(runrecord, "backfill", lambda *a, **k: failed)
    assert main(["--run-record", str(record), "--backfill"]) == EXIT_TOOL_FAILURE
    assert "!! 2 record(s) not written: OSError: full" in capsys.readouterr().out

    def unreadable(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(runrecord, "backfill", unreadable)
    assert main(["--run-record", str(record), "--backfill"]) == EXIT_TOOL_FAILURE
