"""Tests for the QC gates.

Every test here has a matched pair: a case the gate must fail and a case it must pass. A gate
asserted only on its failure case is indistinguishable from one that always fails, and the
gate this replaces — `memory-compact-qc.sh` — failed the opposite way, passing everything
because it graded a summary against another summary rather than against the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scribe.extract import extract
from scribe.qc import DEFAULT_COVERAGE_FLOOR, check_digest, check_freshness, grounding_terms
from scribe.qc_cli import main as qc_main

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def log():
    return extract(FIXTURES / "transcript-structural.jsonl")


GOOD = (
    "**Asked:** First real question about vikunja#843\n\n"
    "**Done:**\n"
    "- Created a branch with `git -C /repo checkout -b feat/thing 9f8e7d6`\n"
    "- Attempted to write /repo/src/mod.py, which failed with EACCES\n"
    "- Read ticket detail via mcp__scoped-mcp__vikunja-mcp_task_get\n\n"
    "**Tickets:**\n- #843\n- #926\n"
)


def test_a_grounded_digest_passes(log) -> None:
    report = check_digest(GOOD, log)
    assert report.ok, [f.detail for f in report.findings]


def test_an_invented_path_is_caught(log) -> None:
    report = check_digest(GOOD + "- Also edited /etc/nginx/nginx.conf\n", log)
    assert not report.ok
    assert any("nginx.conf" in f.detail for f in report.findings)


def test_an_invented_command_is_caught(log) -> None:
    report = check_digest(GOOD + "- Ran `systemctl restart nginx`\n", log)
    assert any("systemctl" in f.detail for f in report.findings)


def test_an_invented_ticket_is_caught(log) -> None:
    report = check_digest(GOOD + "- Closed #4242\n", log)
    assert any("#4242" in f.detail for f in report.findings)


def test_an_invented_tool_is_caught(log) -> None:
    report = check_digest(GOOD + "- The WebFetch call returned 502\n", log)
    assert any("webfetch" in f.detail.lower() for f in report.findings)


@pytest.mark.parametrize(
    "text",
    [
        "**Asked:** x\n\n**Done:**\n- Read the logs and wrote a note\n",
        "**Asked:** x\n\n**Done:**\n- Task complete, nothing to edit\n",
        "**Asked:** x\n\n**Done:**\n- Will write up the findings\n",
    ],
)
def test_ordinary_english_is_not_mistaken_for_a_tool_claim(log, text: str) -> None:
    """`Read`, `Write`, `Edit` and `Task` are tool names AND ordinary words. Flagging the
    prose would fail true claims, and a gate that cries wolf gets switched off."""
    report = check_digest(text, log, floor=0.0)
    assert not any(f.check == "groundedness" for f in report.findings), [
        f.detail for f in report.findings
    ]


def test_a_distinctive_tool_name_is_still_checked_bare(log) -> None:
    """The carve-out is scoped to genuinely ambiguous words, not to tool names generally."""
    report = check_digest("**Asked:** x\n\n- used ToolSearch\n", log, floor=0.0)
    assert any("toolsearch" in f.detail.lower() for f in report.findings)


def test_a_partial_path_is_accepted(log) -> None:
    """A digest may name `src/mod.py` where the log holds the absolute path. Requiring
    equality would fail a true claim."""
    report = check_digest("**Asked:** x\n\n**Done:**\n- edited /repo/src/mod.py\n", log, floor=0.0)
    assert not any(f.check == "groundedness" for f in report.findings)


def test_the_event_coverage_floor_fails_a_digest_that_ignores_the_session(log) -> None:
    """Without a floor, an extractor regression that silently drops events reads as a clean
    pass: the digest stays perfectly grounded in a log that has lost most of its content."""
    report = check_digest("**Asked:** something\n\n**Done:**\n- worked on it\n", log)
    assert not report.ok
    assert any(f.check == "event_coverage" for f in report.findings)
    assert report.coverage < DEFAULT_COVERAGE_FLOOR


def test_a_digest_covering_the_events_passes_the_floor(log) -> None:
    report = check_digest(GOOD, log)
    assert report.coverage == 1.0
    assert report.events_total == 3


def test_the_floor_is_configurable(log) -> None:
    thin = "**Asked:** something\n\n**Done:**\n- worked on it\n"
    assert not check_digest(thin, log, floor=0.5).ok
    assert check_digest(thin, log, floor=0.0).ok


def test_an_empty_digest_fails(log) -> None:
    report = check_digest("   ", log)
    assert not report.ok
    assert any("empty" in f.detail for f in report.findings)


def test_a_session_with_no_events_does_not_fail_the_floor() -> None:
    """A quiet session is a real outcome, not a QC failure."""
    empty = extract(FIXTURES / "transcript-empty.jsonl")
    assert check_digest("**Asked:** nothing happened\n", empty).ok


def test_grounding_terms_are_drawn_from_the_event_log(log) -> None:
    terms = grounding_terms(log)
    assert "/repo/src/mod.py" in terms["paths"]
    assert "#843" in terms["tickets"]
    assert "mcp__scoped-mcp__vikunja-mcp_task_get" in terms["tools"]


# --- freshness ---------------------------------------------------------------------


def test_a_missing_digest_is_a_coverage_failure(tmp_path, log) -> None:
    from scribe.qc import Report

    report = Report()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}")
    check_freshness(tmp_path / "absent.md", transcript, report)
    assert any(f.check == "coverage" for f in report.findings)


def test_a_digest_older_than_its_transcript_is_a_failure(tmp_path) -> None:
    import os

    from scribe.qc import Report

    digest, transcript = tmp_path / "d.md", tmp_path / "t.jsonl"
    digest.write_text("old")
    transcript.write_text("{}")
    os.utime(digest, (1_000_000, 1_000_000))
    os.utime(transcript, (2_000_000, 2_000_000))
    report = Report()
    check_freshness(digest, transcript, report)
    assert any("older than the transcript" in f.detail for f in report.findings)


def test_a_fresh_digest_passes(tmp_path) -> None:
    import os

    from scribe.qc import Report

    digest, transcript = tmp_path / "d.md", tmp_path / "t.jsonl"
    transcript.write_text("{}")
    digest.write_text("new")
    os.utime(transcript, (1_000_000, 1_000_000))
    os.utime(digest, (2_000_000, 2_000_000))
    report = Report()
    check_freshness(digest, transcript, report)
    assert report.ok


# --- the CLI, which is what the build plan invokes ----------------------------------


def test_the_plan_gate_exits_non_zero_on_the_known_bad_digest(capsys) -> None:
    """The build plan's own verification step:

        python -m scribe.qc --digest tests/fixtures/hallucinated-digest.json \\
          --events tests/fixtures/events.json
        echo $?     # expect non-zero
    """
    code = qc_main(
        [
            "--digest",
            str(FIXTURES / "hallucinated-digest.json"),
            "--events",
            str(FIXTURES / "events.json"),
        ]
    )
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_the_plan_gate_exits_zero_on_a_grounded_digest(capsys) -> None:
    """The other half. A gate that fails everything proves as little as one that passes
    everything, and only this pair distinguishes them."""
    code = qc_main(
        [
            "--digest",
            str(FIXTURES / "digest-good.md"),
            "--events",
            str(FIXTURES / "events.json"),
        ]
    )
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_the_cli_emits_json_on_request(capsys) -> None:
    qc_main(
        [
            "--digest",
            str(FIXTURES / "hallucinated-digest.json"),
            "--events",
            str(FIXTURES / "events.json"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["findings"]


def test_the_cli_exits_2_on_a_missing_file(tmp_path, capsys) -> None:
    """Distinguishable from a QC failure: 2 means "could not look", 1 means "looked and it
    was bad"."""
    code = qc_main(["--digest", str(tmp_path / "no.md"), "--events", str(FIXTURES / "events.json")])
    assert code == 2
    assert "not a file" in capsys.readouterr().err


def test_the_cli_exits_2_on_malformed_events(tmp_path, capsys) -> None:
    bad = tmp_path / "events.json"
    bad.write_text("{not json")
    code = qc_main(["--digest", str(FIXTURES / "digest-good.md"), "--events", str(bad)])
    assert code == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_python_m_scribe_qc_actually_runs_the_gate() -> None:
    """`python -m scribe.qc` is the command the plan specifies.

    Without a `__main__` guard in qc.py it imports the module, runs nothing and exits 0 — a
    gate that cannot fail, which is exactly the defect this gate exists to catch. That
    happened during the build, so it is asserted through a real subprocess rather than by
    calling main() directly.
    """
    import subprocess
    import sys

    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "scribe.qc",
            "--digest",
            str(FIXTURES / "hallucinated-digest.json"),
            "--events",
            str(FIXTURES / "events.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(Path(__file__).parent.parent),
    )
    assert proc.returncode == 1, f"gate did not fail: {proc.stdout} {proc.stderr}"
    assert "FAIL" in proc.stdout
