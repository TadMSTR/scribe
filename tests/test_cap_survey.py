"""Tests for `scribe cap-survey` — the tool that keeps the cap comments checkable.

A comment asserting a measurement that the committed tool cannot reproduce is worse than no
comment, because it reads as evidence. These tests pin the survey to the *input* side and to
the arithmetic in `schema.caps_for`, so the two cannot drift.
"""

from __future__ import annotations

import json

from scribe.cap_survey import DENOMINATORS, load_logs, ratios, survey
from scribe.cap_survey_cli import main as survey_main
from scribe.extract.models import EventLog, Rollup, Stats
from scribe.summarize.schema import _LIST_FIELDS, caps_for, field_caps


def _doc(*, tool_events=0, tickets=0, files=0, commands=0) -> dict:
    return {
        "rollup": {
            "tickets": [f"#{i}" for i in range(tickets)],
            "files_written": [f"/p/f{i}.py" for i in range(files)],
            "commands": [f"cmd{i}" for i in range(commands)],
        },
        "stats": {"tool_events": tool_events},
    }


def _write(root, docs) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i, doc in enumerate(docs):
        (root / f"s{i}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_the_survey_covers_every_derived_field() -> None:
    """Non-vacuity guard: a survey that measured two of the three fields would still print a
    plausible table. Read the field list out of the contract, not out of the survey."""
    assert set(DENOMINATORS) == {f.name for f in _LIST_FIELDS if f.derive is not None}


def test_it_measures_the_input_ceiling(tmp_path) -> None:
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=10, tickets=2), _doc(tool_events=672, tickets=104)])
    rows = {r.field: r for r in survey(load_logs(root))}
    assert rows["tickets"].maximum == 104
    assert rows["done"].maximum == 672


def test_the_derived_maximum_matches_caps_for(tmp_path) -> None:
    """The survey reports what `caps_for` would actually produce. Two implementations of one
    rule is how the comment and the code start disagreeing."""
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=672, tickets=104, files=68)])
    log = EventLog(
        session_id="s",
        transcript_path="/p/t.jsonl",
        rollup=Rollup(
            tickets=[f"#{i}" for i in range(104)],
            files_written=[f"/p/f{i}.py" for i in range(68)],
        ),
        stats=Stats(tool_events=672),
    )
    caps = caps_for(log)
    for row in survey(load_logs(root)):
        assert row.max_cap == caps[row.field], row.field


def test_a_small_corpus_never_rises_above_the_floor(tmp_path) -> None:
    """The floor is what keeps ordinary sessions behaving as they did. If a corpus of small
    logs reported `above_floor`, the floor would not be doing its job."""
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=20, tickets=3, files=2) for _ in range(5)])
    for row in survey(load_logs(root)):
        assert row.above_floor == 0
        assert row.max_cap == field_caps()[row.field]


def test_a_malformed_log_is_skipped_not_fatal(tmp_path) -> None:
    """One bad file must not make the other 469 unmeasurable."""
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=5)])
    (root / "broken.json").write_text("{not json", encoding="utf-8")
    (root / "list.json").write_text("[]", encoding="utf-8")
    assert len(load_logs(root)) == 1


def test_commands_is_reported_as_a_share_so_it_stays_visibly_unsuitable(tmp_path) -> None:
    """`rollup.commands` is kept in the output precisely because it is NOT used: it was named
    as `done`'s ceiling for three builds. A share of 0.1 is the evidence that a denominator
    covering a tenth of a session's activity cannot bound what the session did."""
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=100, commands=10)])
    assert ratios(load_logs(root))["rollup.commands"]["median"] == 0.1


def test_ratios_ignores_a_log_with_no_tool_events(tmp_path) -> None:
    """Zero denominators are excluded rather than counted as zero — averaging them in would
    understate every share and make `commands` look better than it is."""
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=0, commands=5), _doc(tool_events=100, commands=50)])
    assert ratios(load_logs(root))["rollup.commands"] == {"median": 0.5, "p99": 0.5, "max": 0.5}


def test_the_cli_prints_a_table(tmp_path, capsys) -> None:
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=672, tickets=104, files=68)])
    assert survey_main(["--eventlogs", str(root)]) == 0
    out = capsys.readouterr().out
    assert "1 event log(s)" in out
    assert "stats.tool_events" in out
    assert "rollup.commands" in out


def test_the_cli_emits_json(tmp_path, capsys) -> None:
    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=672, tickets=104)])
    assert survey_main(["--eventlogs", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["logs"] == 1
    assert {f["field"] for f in payload["fields"]} == set(DENOMINATORS)


def test_an_empty_directory_is_an_error_not_an_empty_table(tmp_path, capsys) -> None:
    """A survey printing zeroes over no data reads exactly like a corpus that has not moved."""
    root = tmp_path / "eventlogs"
    root.mkdir()
    assert survey_main(["--eventlogs", str(root)]) == 1
    assert "no event logs" in capsys.readouterr().err


def test_the_dispatcher_exposes_the_subcommand(tmp_path, capsys) -> None:
    from scribe.__main__ import main as dispatch

    root = tmp_path / "eventlogs"
    _write(root, [_doc(tool_events=5)])
    assert dispatch(["cap-survey", "--eventlogs", str(root)]) == 0
    assert "event log(s)" in capsys.readouterr().out
