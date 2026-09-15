"""Tests for `python -m scribe.extract`.

The CLI is the interface the build plan's verification steps drive and the interface Phase 5
will call, so its exit codes are part of the contract: a caller must be able to distinguish
"this session did nothing" from "I could not read the transcript".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scribe.extract.__main__ import main

FIXTURES = Path(__file__).parent / "fixtures"
BEARER = str(FIXTURES / "transcript-with-bearer.jsonl")


def test_json_output_is_parseable(capsys) -> None:
    assert main([BEARER, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == 1
    assert doc["stats"]["tool_events"] == 2


def test_json_output_ends_with_a_newline(capsys) -> None:
    """vikunja#439: a missing trailing newline glued ~1,030 headings together in the memory
    files. Scribe must not reintroduce it, and the cheapest place to hold the line is here,
    at the boundary where output is handed to something that concatenates."""
    main([BEARER, "--json"])
    assert capsys.readouterr().out.endswith("\n")


def test_text_output_ends_with_a_newline(capsys) -> None:
    main([BEARER])
    assert capsys.readouterr().out.endswith("\n")


def test_indent_pretty_prints(capsys) -> None:
    main([BEARER, "--json", "--indent", "2"])
    assert '\n  "session_id"' in capsys.readouterr().out


def test_text_output_names_tools_and_redaction(capsys) -> None:
    assert main([BEARER]) == 0
    out = capsys.readouterr().out
    assert "Bash" in out and "Read" in out
    assert "redacted" in out
    assert "sk-ant-" not in out


def test_missing_file_exits_2(capsys, tmp_path) -> None:
    assert main([str(tmp_path / "nope.jsonl")]) == 2
    assert "not a file" in capsys.readouterr().err


def test_directory_argument_exits_2(tmp_path) -> None:
    assert main([str(tmp_path)]) == 2


def test_empty_transcript_exits_0(capsys) -> None:
    """An empty session is a real outcome, not an error."""
    assert main([str(FIXTURES / "transcript-empty.jsonl"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stats"]["records"] == 0


@pytest.mark.parametrize("budget,expect_degraded", [(0, False), (500, True)])
def test_max_chars_controls_degradation(capsys, budget: int, expect_degraded: bool) -> None:
    main([BEARER, "--json", "--max-chars", str(budget)])
    stats = json.loads(capsys.readouterr().out)["stats"]
    assert (stats["degradation_level"] > 0) is expect_degraded


def test_unreadable_transcript_exits_2(capsys, tmp_path, monkeypatch) -> None:
    """A permission error must not be reported as an empty session."""
    target = tmp_path / "locked.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", boom)
    assert main([str(target)]) == 2
    assert "cannot read" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# `scribe events` — the drill-down lookup (vikunja#852).
# ---------------------------------------------------------------------------


def _write_log(root, session_id="0192f3c4-aaaa-bbbb-cccc-0123456789ab"):
    from scribe.eventlog import write_eventlog
    from scribe.extract import extract

    log = extract(Path(__file__).parent / "fixtures" / "transcript-structural.jsonl")
    log.session_id = session_id
    return log, write_eventlog(root, log)


def test_events_prints_the_log_for_a_session(tmp_path, capsys) -> None:
    from scribe.events_cli import main as events_main

    root = tmp_path / "eventlogs"
    log, _written = _write_log(root)

    assert events_main([log.session_id, "--eventlog-dir", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == log.session_id
    assert out == log.to_dict()


def test_events_path_prints_only_the_path(tmp_path, capsys) -> None:
    """The form that gets piped into `scribe qc --events`, so it must be the bare path."""
    from scribe.events_cli import main as events_main

    root = tmp_path / "eventlogs"
    log, written = _write_log(root)

    assert events_main([log.session_id, "--eventlog-dir", str(root), "--path"]) == 0
    assert capsys.readouterr().out.strip() == str(written)


def test_events_exits_one_when_there_is_no_log(tmp_path, capsys) -> None:
    """Distinct from a usage error: "no evidence kept" is an answer, not a malfunction."""
    from scribe.events_cli import main as events_main

    assert events_main(["no-such-session", "--eventlog-dir", str(tmp_path)]) == 1
    assert "no event log" in capsys.readouterr().err


def test_events_refuses_a_traversing_session_id(tmp_path, capsys) -> None:
    """A session id is untrusted input and this verb takes one straight off the command line.

    The lookup must not be able to print an arbitrary file: `safe_stem` sends anything that
    is not a bare identifier to a derived name, so the traversal resolves to a log that does
    not exist rather than to `/etc/passwd`.
    """
    from scribe.events_cli import main as events_main

    root = tmp_path / "eventlogs"
    _write_log(root)
    secret = tmp_path / "secret.json"
    secret.write_text('{"a": 1}', encoding="utf-8")

    assert events_main(["../secret", "--eventlog-dir", str(root)]) == 1
    assert '"a": 1' not in capsys.readouterr().out


def test_events_is_reachable_through_the_dispatcher(tmp_path, capsys) -> None:
    """A verb absent from `__main__` is a verb nobody can run."""
    from scribe.__main__ import main as dispatch

    root = tmp_path / "eventlogs"
    log, _w = _write_log(root)

    assert dispatch(["events", log.session_id, "--eventlog-dir", str(root), "--path"]) == 0
    assert str(root) in capsys.readouterr().out
