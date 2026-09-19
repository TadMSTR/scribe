"""Tests for the QC gates.

Every test here has a matched pair: a case the gate must fail and a case it must pass. A gate
asserted only on its failure case is indistinguishable from one that always fails, and the
gate this replaces — `memory-compact-qc.sh` — failed the opposite way, passing everything
because it graded a summary against another summary rather than against the source.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from scribe.eventlog import log_from_dict
from scribe.extract import extract
from scribe.extract.models import EventLog, Turn
from scribe.qc import (
    _ID_CONTEXT,
    _PATH_RE,
    CLAIM_CODE,
    CLAIM_COMMAND,
    CLAIM_IDENTIFIER,
    DEFAULT_COVERAGE_FLOOR,
    Report,
    _claims,
    _ticket_detail,
    _tokens_co_occur,
    check_digest,
    check_freshness,
    check_groundedness,
    classify_span,
    composition_split,
    grounding_terms,
    path_grounded,
    path_literal,
    span_grounded,
    strip_scribe_markers,
)
from scribe.qc_cli import main as qc_main
from scribe.writeback import anchor, terminator

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


# --- vikunja#874: a sentence-ending period is not part of the path -------------------------


def _log_with_a_path_only_in_the_prose(path: str) -> EventLog:
    """An event log whose only mention of `path` is in what the user said.

    This is the live shape, and the distinction is what makes the test non-vacuous. A path in
    `rollup.files_read` is excused by `_in_category`'s bidirectional containment -- the
    terminator-bearing claim CONTAINS the real path, so it grades grounded even with the
    defect present. The defect only bites on the second route, verbatim presence in the
    corpus, where `.../request.md.` is simply absent. The 2026-09-16 case was exactly that:
    an audit-request path Ted named in conversation, never opened as a file.
    """
    return EventLog(
        session_id="s",
        transcript_path="/var/tmp/projects/one/sess.jsonl",
        turns=[Turn(turn_uuid="u1", index=0, user_text=f"file the audit request to {path}")],
    )


def test_a_true_path_claim_at_the_end_of_a_sentence_is_grounded() -> None:
    """The live 2026-09-16 case. `_PATH_RE`'s body class held a literal `.`, so a path that
    ended a sentence was extracted WITH the terminator -- a string that appears in no event
    log -- and a true claim was recorded as ungrounded. Two thirds of real groundedness
    findings were this one artifact."""
    path = "~/.claude/comms/artifacts/audit-requests/scribe/request.md"
    report = check_digest(
        f"**Asked:** x\n\n**Done:**\n- Filed the audit request to {path}.\n",
        _log_with_a_path_only_in_the_prose(path),
        floor=0.0,
    )
    assert not any(f.check == "groundedness" for f in report.findings), [
        f.detail for f in report.findings
    ]


def test_that_true_claim_really_was_failing_before(monkeypatch) -> None:
    """The pair that proves the test above is not vacuous.

    Re-grade the SAME digest against the SAME log with the pre-fix pattern restored. If this
    does not fail, the test above is passing for some other reason and proves nothing about
    vikunja#874.
    """
    import scribe.qc as qc

    path = "~/.claude/comms/artifacts/audit-requests/scribe/request.md"
    monkeypatch.setattr(qc, "_PATH_RE", re.compile(r"(?:(?<=\s)|^)(?:~|\.{0,2})/[\w./~-]{3,}"))
    report = check_digest(
        f"**Asked:** x\n\n**Done:**\n- Filed the audit request to {path}.\n",
        _log_with_a_path_only_in_the_prose(path),
        floor=0.0,
    )
    assert any("request.md." in f.detail for f in report.findings), [
        f.detail for f in report.findings
    ]


def test_an_invented_path_at_the_end_of_a_sentence_still_fails(log) -> None:
    """The true positive the fix must keep. #848 was tuned to ~4/5 passing WITH the real
    defect still caught, and #868 was the same shape -- a guard tuned until everything passes
    is not a guard. Trimming the terminator must not also excuse the claim."""
    report = check_digest(
        "**Asked:** x\n\n**Done:**\n- Also edited /etc/nginx/nginx.conf.\n", log, floor=0.0
    )
    assert not report.ok
    assert any("nginx.conf" in f.detail for f in report.findings)
    # The terminator is gone from the claim as reported, not merely tolerated in the compare.
    assert not any("nginx.conf.'" in f.detail for f in report.findings), [
        f.detail for f in report.findings
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The defect itself, in both prefix forms.
        ("read /repo/src/mod.py.", "/repo/src/mod.py"),
        ("read ~/notes/thing.md.", "~/notes/thing.md"),
        # An ellipsis is three terminators, not a path component.
        ("read /repo/a/b.md... and", "/repo/a/b.md"),
        # ...and the cases a bare rstrip(".") would break. A trailing dot is legitimate
        # exactly when it is a complete final segment.
        ("walked /repo/src/.. back", "/repo/src/.."),
        ("the tree at ../../.. root", "../../.."),
        ("a dir /repo/src/. here", "/repo/src/."),
        # Prefixes were never at risk -- they match before the body.
        ("read ./scripts/run.sh first", "./scripts/run.sh"),
        ("read ../relative/thing.py here", "../relative/thing.py"),
        # A dotted extension ends in a word character, and a trailing slash or `~` is real.
        ("a dir /repo/src/ ends", "/repo/src/"),
        ("backup /repo/foo.py~ kept", "/repo/foo.py~"),
    ],
)
def test_the_path_pattern_trims_a_terminator_and_only_a_terminator(text, expected) -> None:
    assert _PATH_RE.findall(text) == [expected]


def test_the_path_pattern_keeps_its_three_character_floor() -> None:
    """The `{2,}` body plus one constrained final character is the SAME floor as the old
    `{3,}`, not a loosening. A two-character path was never a claim worth grading."""
    assert _PATH_RE.findall("under /ab floor") == []
    assert _PATH_RE.findall("at /a/b floor") == ["/a/b"]


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


# --- vikunja#848: the classifier must separate a digest's vocabulary from its commands -----
#
# The regression set is SYNTHETIC, not the five live sessions the plan names. Those
# transcripts are ~12 MB of Ted's real work, are 0600 on purpose, and are the unredacted
# source of the 146 secret-shaped strings the extractor scrubbed on that run -- committing
# them would move all of that into git history and onto every CI runner that clones the repo.
# `transcript-qc-regression.jsonl` instead reproduces each structural shape the live run
# exposed: a bare id that is not an identifier, a `#N` the extractor's ref-collection drops,
# and the status/model/config vocabulary that was being reported as fabricated commands.
# The real-corpus replay stays reproducible outside the repo -- see docs/qc-regression.md.

QC_REGRESSION = FIXTURES / "transcript-qc-regression.jsonl"


@pytest.fixture
def qc_log():
    return extract(QC_REGRESSION)


def test_the_vocabulary_of_a_good_digest_is_not_reported_as_hallucination(qc_log) -> None:
    """70 of 73 findings on the first live run were this: statuses, model names, config keys
    and field names, each demanded to appear in `rollup.commands`."""
    report = check_digest((FIXTURES / "digest-qc-composed.md").read_text(), qc_log)
    assert report.findings == [], [f.detail for f in report.findings]
    assert report.ok is True


def test_a_vikunja_id_rendered_as_an_identifier_is_still_caught(qc_log) -> None:
    """The one true positive, and the reason the gate must not simply be loosened.

    `#417` is a real ticket -- just not this one. Vikunja's id 417 is identifier #398, so the
    reference reads as ordinary and points somewhere else entirely.
    """
    report = check_digest((FIXTURES / "digest-qc-id-conflation.md").read_text(), qc_log)
    assert report.ok is False
    assert len(report.findings) == 1, [f.detail for f in report.findings]
    detail = report.findings[0].detail
    assert "#417" in detail
    assert "internal id" in detail


def test_the_two_regression_digests_differ_only_in_the_conflated_reference() -> None:
    """The pair is a differential: one character apart, opposite verdicts.

    Without this, the two fixtures could drift until they differed in several ways and the
    verdict could no longer be attributed to the conflation alone -- the pair would still
    look like it was testing something.
    """
    good = (FIXTURES / "digest-qc-composed.md").read_text().splitlines()
    bad = (FIXTURES / "digest-qc-id-conflation.md").read_text().splitlines()
    assert len(good) == len(bad)
    differing = [(a, b) for a, b in zip(good, bad, strict=True) if a != b]
    assert len(differing) == 1
    assert differing[0][0].endswith("#398")
    assert differing[0][1].endswith("#417")


def test_token_overlap_does_not_ground_an_invention(log) -> None:
    """The widest grounding route must still reject a fabricated command.

    Note what this does and does not prove: the fixture log does not contain these words at
    all, so it only shows the route is reachable and rejects. The structural property -- that
    scattered words are not enough -- is
    `test_a_fabrication_is_not_grounded_by_its_words_appearing_separately` below.
    """
    report = check_digest("**Done:**\n- Ran `systemctl restart nginx`\n", log)
    assert any("systemctl restart nginx" in f.detail for f in report.findings)


def _scattered_log() -> EventLog:
    """A log containing every word of `systemctl restart nginx`, never together."""
    log = EventLog(session_id="s", transcript_path="t")
    first = Turn(turn_uuid="a", index=0)
    first.user_text = "Can we restart the deployment please"
    first.assistant_text = ["I checked the nginx config and it looks fine."]
    second = Turn(turn_uuid="b", index=1)
    second.assistant_text = ["No systemctl issues found on the host."]
    log.turns = [first, second]
    return log


def test_a_fabrication_is_not_grounded_by_its_words_appearing_separately() -> None:
    """The scribe-shadow-fixes-2026-09 audit finding, as a test.

    The first version of `_tokens_co_occur` asked only that each token appear *somewhere* in
    the ~180 KB corpus, so a fabricated command graded clean whenever its individual words
    turned up in unrelated sentences -- routine in any session touching several topics. The
    commit that introduced it claimed this exact command was "still rejected", which held only
    against a fixture where the words are absent. That made the guarantee a property of the
    test data rather than of the code, which is what this test exists to prevent.
    """
    report = Report()
    check_groundedness("Ran `systemctl restart nginx` to fix it.", _scattered_log(), report)
    assert [f.detail for f in report.findings] != []
    assert any("systemctl restart nginx" in f.detail for f in report.findings)


def test_a_genuine_compression_survives_the_proximity_requirement() -> None:
    """The other direction: tokens that really do appear together must still ground.

    Without this, the fix could be tightened until nothing passes -- which is the original
    defect wearing the opposite sign.
    """
    log = EventLog(session_id="s", transcript_path="t")
    turn = Turn(turn_uuid="a", index=0)
    turn.assistant_text = [
        'Called subprocess.Popen(["claude", "-p"], env=child_env) from the worker.'
    ]
    log.turns = [turn]
    report = Report()
    check_groundedness('Ran `popen(["claude","-p"], env=child_env)`.', log, report)
    assert [f.detail for f in report.findings] == []


def test_the_proximity_window_is_what_separates_the_two() -> None:
    """Pin the mechanism, not just the two outcomes.

    Both cases above would pass with an unbounded window; only the fabricated one changes
    behaviour with it. Asserting that directly means a future change to the window cannot
    quietly restore the defect while both outcome tests still read as green.
    """
    corpus = _scattered_log().grounding_text().lower()
    assert _tokens_co_occur("systemctl restart nginx", corpus, window=10**9) is True
    assert _tokens_co_occur("systemctl restart nginx", corpus) is False


def test_a_ticket_claim_is_not_excused_by_a_longer_number(qc_log) -> None:
    """Tickets are matched exactly. Containment would let `#65` ride on the log's `#655`."""
    report = check_digest("**Tickets:**\n- Closed #65 today\n", qc_log)
    assert any("#65" in f.detail for f in report.findings)


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        ("set -euo pipefail", "command"),
        ("git -C /repo checkout -b feat/thing", "command"),
        ("systemctl restart nginx", "command"),
        ("cat a.txt | grep b", "command"),
        ('popen(["claude","-p"], env=child_env)', "code"),
        ("max_retries=2", "code"),
        ("parked", "identifier"),
        ("mistral-small-latest", "identifier"),
        ("credentials: source: env", "identifier"),
        ("approved → in-progress → completed", "identifier"),
        ("src/tools/queue.py:43", "identifier"),
        ("", "identifier"),
    ],
)
def test_spans_are_classified_by_shape_not_by_backticks(span, expected) -> None:
    assert classify_span(span) == expected


def test_the_cli_and_the_pipeline_grade_a_digest_identically(qc_log, tmp_path) -> None:
    """`log_from_dict` used to drop `user_text` and `assistant_text`, so the same digest and
    the same log graded stricter through `python -m scribe.qc` than through the pipeline.

    Two paths disagreeing about whether a digest is grounded is its own false signal -- the
    CLI is what CI runs, so it is the one that would have reported the phantom findings.
    """
    events = tmp_path / "events.json"
    events.write_text(json.dumps(qc_log.to_dict()), encoding="utf-8")
    rebuilt = log_from_dict(json.loads(events.read_text()))
    assert rebuilt.grounding_text() == qc_log.grounding_text()

    digest = tmp_path / "d.md"
    digest.write_text((FIXTURES / "digest-qc-composed.md").read_text(), encoding="utf-8")
    assert qc_main(["--digest", str(digest), "--events", str(events)]) == 0


def test_an_event_log_missing_required_fields_is_graded_not_crashed(tmp_path) -> None:
    """`to_dict` omits empty values, so a round trip can legitimately lack `session_id` or a
    turn's `turn_uuid`. Rebuilding from `__dataclass_fields__` must supply those rather than
    raise -- the dataclasses have no defaults for them."""
    events = tmp_path / "e.json"
    events.write_text(json.dumps({"turns": [{"events": [{"seq": 1, "tool": "Bash"}]}]}))
    digest = tmp_path / "d.md"
    digest.write_text("**Asked:** anything\n")
    assert qc_main(["--digest", str(digest), "--events", str(events)]) in (0, 1)


@pytest.mark.parametrize(
    "payload", ['{"turns": "notalist"}', '{"turns": [{"index": "abc"}]}', "null", "[]"]
)
def test_a_malformed_event_log_is_not_reported_as_a_failed_gate(payload, tmp_path) -> None:
    """Exit 1 is the FAIL verdict and an unhandled traceback also exits 1, so a broken input
    file used to be indistinguishable from a digest that failed the gate. CI reads the exit
    code, not the traceback."""
    events = tmp_path / "e.json"
    events.write_text(payload)
    digest = tmp_path / "d.md"
    digest.write_text("**Asked:** anything\n")
    assert qc_main(["--digest", str(digest), "--events", str(events)]) == 2


def test_co_occurrence_is_a_span_not_a_distance_from_an_anchor() -> None:
    """Three tokens where the outer two are further apart than the window, but each is within
    it of the middle one.

    An anchored implementation answers this differently depending on which token it anchors
    on -- and since the anchor came from iterating a set, the verdict moved with the
    interpreter's hash seed. Measuring the enclosing span instead is symmetric, so there is
    one right answer: the claim spans more than the window, so it is not grounded.
    """
    corpus = "alpha" + ("." * 120) + "beta" + ("." * 120) + "gamma"
    assert _tokens_co_occur("alpha beta", corpus, window=130) is True
    assert _tokens_co_occur("beta gamma", corpus, window=130) is True
    # alpha..gamma spans ~250 characters, so the three together must not ground at 130.
    assert _tokens_co_occur("alpha beta gamma", corpus, window=130) is False
    assert _tokens_co_occur("alpha beta gamma", corpus, window=260) is True


def test_the_verdict_does_not_depend_on_token_order() -> None:
    """The same claim written in any order must grade identically.

    This is the property the hash-seed bug violated. Asserting it directly means a future
    reintroduction of an anchored implementation fails here rather than intermittently in CI.
    """
    corpus = "alpha" + ("." * 120) + "beta" + ("." * 120) + "gamma"
    orders = ["alpha beta gamma", "gamma beta alpha", "beta gamma alpha", "beta alpha gamma"]
    verdicts = {_tokens_co_occur(o, corpus, window=130) for o in orders}
    assert len(verdicts) == 1


# ---------------------------------------------------------------------------
# Scribe's own block markers are not model claims (vikunja#852).
# ---------------------------------------------------------------------------


@pytest.fixture
def bare_log() -> EventLog:
    """A minimal event log whose corpus contains no path resembling a scribe marker.

    The `log` and `qc_log` fixtures both extract real transcripts **from this repository**,
    so their grounding corpora contain `/home/ted/repos/personal/scribe/...` — and the
    groundedness check tests `claim not in corpus` by substring, which means `/scribe` is
    "grounded" against either of them. A marker test built on those passes whether or not
    the stripping exists. This fixture is what makes these assertions able to fail.
    """
    return EventLog(
        session_id="s-1",
        transcript_path="/var/tmp/projects/one/sess.jsonl",
        turns=[Turn(turn_uuid="t-1", index=1, user_text="Please do the thing.")],
    )


def test_the_marker_fixture_can_actually_see_an_ungrounded_path(bare_log) -> None:
    """Guards the guard: if `/scribe` were grounded here, the next two tests prove nothing."""
    report = Report()
    check_groundedness("- Touched /scribe/nowhere.py\n", bare_log, report)
    assert any("/scribe" in f.detail for f in report.findings)


def test_the_terminator_is_not_read_as_a_file_path(bare_log) -> None:
    """`<!-- /scribe turn:... -->` must not become a `/scribe` path claim.

    This is the defect that made `scribe qc --digest <a file scribe wrote>` structurally
    incapable of passing: the terminator is on every block, `/scribe` is in no event log,
    so every digest file carried a guaranteed groundedness finding. It stayed invisible
    because the pipeline grades the rendered block *before* the markers are attached, and
    only the CLI — the path a human re-checking a digest actually takes — ever sees them.
    """
    report = Report()
    check_groundedness(f"- Did a thing.\n{terminator('abc-123')}\n", bare_log, report)
    assert not [f for f in report.findings if "/scribe" in f.detail]


def test_both_markers_are_removed_before_anything_reads_the_text() -> None:
    """Asserted on `strip_scribe_markers` directly, because the anchor cannot be caught
    through `check_groundedness` today.

    An anchor's transcript path is preceded by `transcript:` rather than whitespace, and
    `_PATH_RE` requires whitespace or start-of-string — so the anchor happens to produce no
    claim regardless of whether it is stripped. A groundedness-level test of it would pass
    with the anchor half of the strip deleted, which is a test that proves nothing.

    The anchor is still stripped, deliberately: the only reason it is currently harmless is
    one lookbehind in an unrelated regex, and the next widening of `_PATH_RE` would make it
    a finding on every digest. This pins that, at the level where it is real.
    """
    text = (
        anchor("s-1", "t-1", "/nowhere/near/the/event/log.jsonl")
        + "\n- Did a thing.\n"
        + terminator("t-1")
    )
    stripped = strip_scribe_markers(text)

    assert "/nowhere/near/the/event/log.jsonl" not in stripped
    assert "/scribe" not in stripped
    assert "- Did a thing." in stripped, "the body itself must survive untouched"


def test_stripping_the_markers_does_not_silence_a_real_hallucination(bare_log) -> None:
    """The pair. Removing scribe's markers must not remove anything else.

    Without this, `strip_scribe_markers` returning `""` would pass the two tests above —
    and a gate handed an empty document passes everything.
    """
    report = Report()
    check_groundedness(
        f"- Edited /etc/nginx/nginx.conf\n{terminator('abc-123')}\n", bare_log, report
    )
    assert any("nginx" in f.detail for f in report.findings)


def test_a_marker_shaped_span_inside_prose_is_still_graded(bare_log) -> None:
    """Only the two known markers are stripped, not every HTML comment.

    A model that writes `<!-- /etc/shadow -->` into a bullet is still making a claim, and a
    blanket comment strip would be a hole opened for no reason.
    """
    report = Report()
    check_groundedness("- Note: <!-- /etc/shadow was read -->\n", bare_log, report)
    assert any("/etc/shadow" in f.detail for f in report.findings)


def test_the_truncation_note_is_stripped_before_anything_reads_it() -> None:
    """Asserted on `strip_scribe_markers` directly, and for the same reason as the anchor:
    a groundedness-level test of it would prove nothing today.

    **The note currently yields no claim even unstripped.** It carries no path, no backticked
    span, no `#ticket` and no tool name, so `_claims` returns an empty set either way —
    measured, not assumed. A `check_groundedness` test would therefore pass with this strip
    deleted, which is precisely the vacuous shape the anchor test above refuses.

    The strip is kept regardless, as defence in depth rather than a live fix. The note is
    scribe's own prose (vikunja#884), it is a category error to grade it as a model claim, and
    the only thing making it harmless right now is its wording. Adding a field name in
    backticks — the obvious next edit — would turn it into an identifier claim grounded in no
    event log, on every truncated digest.
    """
    note = "_Truncated: 4 further items dropped at the 40-item cap._"
    assert note not in strip_scribe_markers(f"- A real bullet\n{note}\n- Another bullet")
    assert "A real bullet" in strip_scribe_markers(f"- A real bullet\n{note}\n")


def test_stripping_the_note_does_not_eat_an_ordinary_italic_line() -> None:
    """The pattern is anchored to the marker prefix, not to underscores in general. A digest
    bullet may legitimately contain emphasis, and a greedy pattern would silence real claims
    — the hole `strip_scribe_markers` explicitly declines to open for HTML comments."""
    text = "- Read _config.py_ and /repo/src/mod.py\n"
    assert strip_scribe_markers(text) == text


# --- vikunja#876: a true claim that COMPOSES is not a hallucination ------------------------


def _log_naming(*fragments: str) -> EventLog:
    """An event log whose prose names each fragment, and nothing else.

    Prose rather than `rollup.files_read` for the same reason as
    `_log_with_a_path_only_in_the_prose`: a path in the rollup is already excused by
    `_in_category`'s bidirectional containment, so a test built on one would pass with the
    defect still present. The corpus route is where this defect lives.
    """
    return EventLog(
        session_id="s",
        transcript_path="/var/tmp/projects/one/sess.jsonl",
        turns=[
            Turn(turn_uuid=f"u{i}", index=i, user_text=text) for i, text in enumerate(fragments)
        ],
    )


def _grounded(claim: str, log: EventLog) -> bool:
    report = Report()
    check_groundedness(f"**Done:**\n- touched {claim}\n", log, report)
    return not any(f.check == "groundedness" for f in report.findings)


def test_a_path_that_composes_two_things_the_log_named_is_grounded() -> None:
    """The defect. The log names a directory and, separately, a relative path beneath it; the
    digest writes the two joined. That is a true claim about what the model was shown, and it
    was 770 of 875 path findings on the live corpus."""
    log = _log_naming(
        "cloned into /home/ted/repos/personal/webhook-doorman",
        "the failing test is in tests/unit/test_router.py",
    )
    assert _grounded("/home/ted/repos/personal/webhook-doorman/tests/unit/test_router.py", log)


def test_a_path_the_log_never_named_is_still_ungrounded() -> None:
    """The matched pair. Neither half of this appears, so no amount of composition reaches it
    -- and it must not, or the gate passes everything."""
    log = _log_naming(
        "cloned into /home/ted/repos/personal/webhook-doorman",
        "the failing test is in tests/unit/test_router.py",
    )
    assert not _grounded("/etc/nginx/sites-enabled/doorman.conf", log)


def test_the_root_is_not_a_directory_prefix() -> None:
    """`MIN_PREFIX_SEGMENTS`. At a floor of one, `/home` plus everything after it composes,
    which would ground every absolute path on the machine against any log that says `/home`."""
    log = _log_naming("under /home somewhere", "ted/repos/personal/scribe/src/scribe/qc.py")
    assert not _grounded("/home/ted/repos/personal/scribe/src/scribe/qc.py", log)


def test_a_bare_filename_far_from_its_directory_does_not_compose() -> None:
    """`MIN_TAIL_SEGMENTS` plus `ADJACENCY_WINDOW`. `changelog.md` appears in nearly every log,
    so a directory pairing with any common filename anywhere in the corpus is the loosening
    that triples the false-ground rate -- and this is the exact shape a fabricated claim takes:
    two real things the session never put together."""
    log = _log_naming(
        "working in /home/ted/repos/personal/githost-mcp today",
        "x " * 200,
        "unrelated later work touched changelog.md in another repo",
    )
    assert not _grounded("/home/ted/repos/personal/githost-mcp/changelog.md", log)


def test_a_bare_filename_beside_its_directory_does_compose() -> None:
    """The matched pair, and the whole reason the window exists rather than a flat refusal:
    said together, they are one claim about one file."""
    log = _log_naming(
        "in /home/ted/repos/personal/githost-mcp I updated changelog.md for the release"
    )
    assert _grounded("/home/ted/repos/personal/githost-mcp/changelog.md", log)


def test_an_absolute_claim_grounds_against_a_home_relative_log() -> None:
    """The log writes `~/repos/...` because that is how a session's commands are written; the
    digest expands it. Neither composes on its own -- the claim's absolute prefix appears
    nowhere -- and this was the largest bucket left after composition landed."""
    log = _log_naming("filed it to ~/repos/gitea/host-forge-build-reports/thing-2026-09/audit.md")
    assert _grounded("/home/ted/repos/gitea/host-forge-build-reports/thing-2026-09/audit.md", log)


def test_re_anchoring_at_tilde_keeps_the_tail_floor() -> None:
    """The matched pair. Re-anchoring may not shrink the claim to a bare filename: if
    `~/audit.md` were a candidate, every absolute path ending in a file the log mentions
    home-relative would ground."""
    log = _log_naming("wrote ~/audit.md")
    assert not _grounded("/home/ted/repos/gitea/host-forge-build-reports/x/audit.md", log)


def test_tilde_is_read_from_the_corpus_and_never_from_the_environment(monkeypatch) -> None:
    """The verdict must not depend on who ran the gate.

    Resolving `~` against `$HOME` would grade the same digest and the same log differently on
    a different machine, and nothing in the output would say so. This asserts the property
    directly rather than trusting the implementation to keep it: same inputs, three very
    different environments, one verdict.
    """
    log = _log_naming("filed it to ~/repos/gitea/reports/thing/audit.md")
    claim = "/home/ted/repos/gitea/reports/thing/audit.md"
    verdicts = set()
    for home in ("/home/ted", "/home/someone-else", "/nonexistent"):
        monkeypatch.setenv("HOME", home)
        verdicts.add(_grounded(claim, log))
    assert verdicts == {True}


def test_the_pre_876_rule_is_still_available_and_still_strict() -> None:
    """`path_literal` is what reproduces the "before" number from this commit. If it silently
    acquired the new tolerance, the comparison this build reports would be against itself."""
    log = _log_naming(
        "cloned into /home/ted/repos/personal/webhook-doorman",
        "the failing test is in tests/unit/test_router.py",
    )
    claim = "/home/ted/repos/personal/webhook-doorman/tests/unit/test_router.py"
    corpus = log.grounding_text().lower()
    allowed = grounding_terms(log)["paths"]
    assert path_grounded(claim, allowed, corpus)
    assert not path_literal(claim, allowed, corpus)


#: The `absent` bucket's shapes -- the control set the phase 1 survey produced, transposed onto
#: a fictional host. Every one is the shape of a plausible invention: a directory the session
#: really worked in, and a filename that was never there. A timestamped backup, a provisioning
#: tree, a per-agent config, the same written `~`-first, and a dated artifact.
#:
#: **Transposed, not copied.** The measured claims name a private host's credential directory,
#: agent manifest paths and config layout, and this repository is public. The shape is what the
#: test needs; the real layout is not, and publishing it would point a reader at where that host
#: keeps its secrets for no gain. Regenerate the live set on the host that holds the corpus with
#: `python -m scribe qc-survey --bucket absent`.
CONTROL_SET = (
    "/home/alice/.credentials/service.env.bak-20260829-1934",
    "/home/alice/repos/infra-configs/appdata/observability/dashboards/site/disk-space.json",
    "/etc/example/manifests/reader-agent.yml",
    "~/repos/infra-scripts/manifests/writer-agent.yml",
    "/home/alice/artifacts/config-proposals/2026-08-18-reader-remove-thing.md",
)


@pytest.mark.parametrize("claim", CONTROL_SET)
def test_the_control_set_still_fails(claim: str) -> None:
    """A gate that passes everything is not a gate (vikunja#848, #868).

    These fail against a log that names their *neighbourhood* — the parent directories and
    sibling filenames a session really touched — because that is the only version of this test
    worth running. Against an empty log they would fail with the tolerance removed entirely.
    """
    log = _log_naming(
        "worked under /home/alice/.credentials and /etc/example/manifests today",
        "read /home/alice/repos/infra-configs and ~/repos/infra-scripts",
        "also /home/alice/artifacts/config-proposals",
        "the files were service.env, editor-agent.yml, host-overview.json and index.md",
    )
    assert not _grounded(claim, log)


# --- vikunja#888 / #889 -------------------------------------------------------------------


def test_a_short_backticked_span_does_not_desync_the_ones_after_it() -> None:
    """The delimiter-pairing defect, as the corpus actually produced it.

    `_CMD_RE` used to be `` r"`([^`\n]{3,200})`" ``. A span under three characters cannot match,
    so the scan resumed at that span's CLOSING backtick and paired it with the NEXT span's
    OPENING one -- capturing the prose between them as a claim. It fails in both directions at
    once: a claim no model asserted is invented AND the real span that swallowed it is never
    graded at all, so the gate reports a hallucinated identifier while missing a true one.

    Measured over the live corpus 2026-09-19: 56 fabricated spans across 36 blocks, producing
    22 of 65 non-path groundedness findings. Every prose fragment in the residual traced here.
    """
    text = "Verified via `ps` showing `nats pub --password x` on the host."
    spans = _claims(text)
    everything = spans[CLAIM_COMMAND] | spans[CLAIM_CODE] | spans[CLAIM_IDENTIFIER]

    # The prose between the two spans is not a claim about anything.
    assert "showing" not in everything
    # And the span the defect used to swallow is graded.
    assert "nats pub --password x" in everything


@pytest.mark.parametrize(
    "text,swallowed",
    [
        ("used `uv` in dependabot.yml instead of `pip`", "in dependabot.yml instead of"),
        ("the public `id` and resolve internally to `_id`", "and resolve internally to"),
        ("redirect (`/`) to `/dashboard`", ") to"),
        ("resolve `#N` refs in `tasks_bulk_update`", "refs in"),
    ],
)
def test_no_prose_fragment_is_ever_read_as_a_claim(text: str, swallowed: str) -> None:
    """The four shapes the live corpus produced, each from a real digest."""
    spans = _claims(text)
    everything = spans[CLAIM_COMMAND] | spans[CLAIM_CODE] | spans[CLAIM_IDENTIFIER]
    assert swallowed not in everything


def test_the_scan_cap_is_on_length_so_late_evidence_still_grounds() -> None:
    """vikunja#889's cap must not make the verdict depend on WHERE the evidence sits.

    This is the test the deferred "obvious fix" would fail. An iteration cap -- bail after N
    occurrences of `prefix` -- turns a true composition into a coin flip on ordering: here the
    directory is named 500 times before the one mention that sits beside the file, so any
    iteration cap below 500 rejects a claim the uncapped rule accepts. A length cap truncates
    the same prefix of the same corpus every run, so the verdict is a function of the corpus
    and the claim alone.
    """
    prefix = "/home/ted/repos/scribe"
    # A ONE-segment tail, so `MIN_TAIL_SEGMENTS` cannot admit the claim and `_adjacent` is the
    # only route left. With two segments the floor grounds it outright and the cap is never
    # consulted -- a version of this test written that way passes on an iteration cap too.
    claim = f"{prefix}/notes.md"
    decoys = f"{prefix} mentioned alone. " * 500
    corpus = (decoys + f"cd {prefix} && cat notes.md").lower()

    assert composition_split(claim, corpus) is not None
    # And the cap really is a length: cut the corpus short of where the evidence sits and the
    # claim stops grounding, which is what proves the parameter is wired in at all.
    assert composition_split(claim, corpus, cap=100) is None


def test_the_scan_cap_keeps_the_adjacency_window_whole_past_the_cut() -> None:
    """A composition straddling the cap boundary is judged like one before it, not truncated."""
    corpus = ("x" * 4000 + " /home/ted/repos/scribe notes.md").lower()
    claim = "/home/ted/repos/scribe/notes.md"
    assert composition_split(claim, corpus, cap=4005) is not None


def test_a_ticket_written_for_an_mcp_argument_id_names_the_conflation() -> None:
    """The commonest live shape, and the one `\\bid` could not see.

    `task_id=541` has a word character before `id`, so the word boundary fails and 31 of 62
    live ticket findings fell through to the generic message. Verified against the tracker:
    id 541 is identifier #493, so `#541` names a real but unrelated ticket -- exactly what the
    message exists to say. This selects a MESSAGE and never a verdict; both branches fail.
    """
    corpus = '{"tool": "vikunja_task_get", "args_digest": "task_id=541", "ok": true}'
    assert "internal id" in _ticket_detail("#541", corpus)
    assert "internal id" in _ticket_detail("#930", '{"target": "930", "kind": "mcp"}')
    # A number that is nowhere still fails, with the plain message.
    assert "internal id" not in _ticket_detail("#4242", corpus)


def test_span_routes_do_not_admit_a_claim_on_trimmed_punctuation() -> None:
    """The one tolerance the residual suggested, priced and declined.

    `qc_survey.span_trim_probe` puts it at 1.2 true claims per false one at its very tightest,
    against the 5.0 that bought `ADJACENCY_WINDOW` and the 1.3 #876 rejected as too weak. If a
    future change admits it, this goes red and the probe is the argument to re-run.
    """
    allowed = {"commands": set(), "paths": set(), "tools": set(), "tickets": set()}
    corpus = "the helper _is_loopback returns true for 127.0.0.1"
    assert not span_grounded("_is_loopback()", CLAIM_CODE, allowed, corpus)


CONTROL_CLAIMS = (
    "/home/ted/.claude/comms/artifacts/audit-requests/never-ran/request.md",
    "~/repos/personal/imaginary-tool/src/imaginary/main.py",
    "/opt/appdata/nonexistent-service/config.yml",
    "/var/log/fabricated/output.log",
)


@pytest.mark.parametrize("claim", CONTROL_CLAIMS)
def test_a_claim_absent_from_its_log_fails_every_route(claim: str) -> None:
    """The control set, in miniature. **Lowering the failure rate is not the goal on its own.**

    The live corpus carries 26 path claims whose basename appears nowhere in their own event
    log (`qc-survey --bucket absent`, still exactly 26 after this build). They are what says a
    tolerance went too far, and they cannot be pinned in-repo by name because the corpus is not
    in the repo -- so the property is pinned instead, against a log that plausibly *could* have
    held them.

    vikunja#848 and #868 are both cases where a gate was "fixed" into uselessness, and a build
    that only showed the rate falling would not have noticed. Every route has to reject these:
    the derived category set, verbatim corpus presence, composition, and `~` re-anchoring.
    """
    corpus = (
        "read /home/ted/repos/personal/scribe/src/scribe/qc.py and "
        "~/repos/personal/scribe/tests/test_qc.py; wrote /home/ted/.claude/comms/artifacts/"
        "build-plans/real-build-2026-09/plan.md; ran pytest -q in /home/ted/repos/personal/scribe"
    ).lower()
    allowed = {
        "/home/ted/repos/personal/scribe/src/scribe/qc.py",
        "~/repos/personal/scribe/tests/test_qc.py",
    }
    assert not path_grounded(claim, allowed, corpus)


def test_the_control_claims_are_rejected_for_absence_and_not_by_a_broken_predicate() -> None:
    """The other half: a predicate that rejects everything would pass the test above.

    A control set is only evidence if the same corpus grounds a true claim -- otherwise
    `path_grounded` could be `return False` and every assertion still hold. This is the
    positive half that makes the negative one mean something.
    """
    corpus = (
        "read /home/ted/repos/personal/scribe/src/scribe/qc.py and "
        "~/repos/personal/scribe/tests/test_qc.py; wrote /home/ted/.claude/comms/artifacts/"
        "build-plans/real-build-2026-09/plan.md; ran pytest -q in /home/ted/repos/personal/scribe"
    ).lower()
    allowed = {"/home/ted/repos/personal/scribe/src/scribe/qc.py"}
    assert path_grounded("/home/ted/repos/personal/scribe/src/scribe/qc.py", allowed, corpus)
    assert path_grounded(
        "/home/ted/.claude/comms/artifacts/build-plans/real-build-2026-09/plan.md",
        allowed,
        corpus,
    )


def test_the_id_context_pattern_does_not_backtrack_on_a_long_word_run() -> None:
    """`_ID_CONTEXT` must stay linear in corpus length.

    Its first draft opened with `\\w*_id`, which the engine re-tries at every start position:
    0.39s at 16k characters, 13.8s at 100k, and **232 seconds** measured against this corpus's
    longest real event log at 414,187. The live corpus hid it completely -- event logs are JSON
    and runs of word characters are short -- so one session logging a base64 blob or a minified
    file would have hung the gate with nothing in any measurement to predict it.

    The bound is deliberately loose. The fixed pattern does this in ~4ms; anything that trips a
    two-second ceiling has reintroduced backtracking, not merely run on a slow machine.
    """
    corpus = "a" * 414_187
    started = time.perf_counter()
    re.search(_ID_CONTEXT + r"4242\b", corpus)
    assert time.perf_counter() - started < 2.0

    # And the same for the shapes it actually has to match, at full corpus length.
    for filler in ("_" * 414_187, '{"args_digest":"task_id=541"} ' * 13_000):
        started = time.perf_counter()
        re.search(_ID_CONTEXT + r"4242\b", filler)
        assert time.perf_counter() - started < 2.0
