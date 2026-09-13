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
