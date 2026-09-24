"""Tests for the corpus survey — the instrument vikunja#876 was measured with.

An instrument gets tested more carefully than a gate, not less. This build exists partly
because two throwaway measurements of the same corpus disagreed 16-fold on the bucket that
decides the fix, and neither could be re-run. The properties asserted here are the ones whose
failure would produce a confident, reproducible, *wrong* number:

  * it finds the blocks at all (a flat glob over an agent-partitioned corpus reports a clean
    corpus, which is the most dangerous way for this to be wrong);
  * it grades each block against its own log and not its file's first log (vikunja#852);
  * it agrees with the gate it is measuring, and says so when it does not.
"""

from __future__ import annotations

import json

import pytest

from scribe.eventlog import write_eventlog
from scribe.extract.models import EventLog, Turn
from scribe.qc import (
    CLAIM_CODE,
    CLAIM_PATH,
    CLAIM_TICKET,
    CLAIM_TOOL,
    Report,
)
from scribe.qc_survey import (
    ABSENT,
    BUCKETS,
    COINCIDENTAL_NUMBER,
    COMPOSED,
    ID_CONFLATION,
    PUNCTUATION_WRAPPED,
    RULE_CURRENT,
    RULE_LITERAL,
    SINGLE_TOKEN,
    SUFFIX,
    TOKEN_MISSING,
    TOKEN_SPREAD,
    classify_path_claim,
    classify_span_claim,
    classify_ticket_claim,
    cross_session_probe,
    home_forms,
    iter_blocks,
    longest_present_suffix,
    rejected_claims,
    rejected_paths,
    span_trim_probe,
    survey,
)
from scribe.qc_survey_cli import main as survey_main
from scribe.writeback import anchor, terminator


def _log(session_id: str, *fragments: str) -> EventLog:
    return EventLog(
        session_id=session_id,
        transcript_path=f"/var/tmp/projects/x/{session_id}.jsonl",
        turns=[
            Turn(turn_uuid=f"u-{session_id}", index=i, user_text=t) for i, t in enumerate(fragments)
        ],
    )


def _block(log: EventLog, body: str) -> str:
    uuid = f"u-{log.session_id}"
    return (
        anchor(log.session_id, uuid, log.transcript_path) + f"\n{body}\n" + terminator(uuid) + "\n"
    )


@pytest.fixture
def corpus(tmp_path):
    """Two sessions, one digest file, one agent subdirectory. The live shape in miniature."""
    digests = tmp_path / "digests" / "developer"
    digests.mkdir(parents=True)
    events = tmp_path / "eventlogs"
    events.mkdir()

    one = _log(
        "aaaaaaaa",
        "cloned into /home/user/repos/personal/alpha",
        "the test is tests/unit/test_one.py",
    )
    two = _log(
        "bbbbbbbb",
        "cloned into /home/user/repos/personal/beta",
        "the test is tests/unit/test_two.py",
    )
    for log in (one, two):
        write_eventlog(events, log)

    (digests.parent / "developer" / "2026-09-01.md").write_text(
        _block(one, "- touched /home/user/repos/personal/alpha/tests/unit/test_one.py")
        + _block(two, "- touched /home/user/repos/personal/beta/tests/unit/test_two.py"),
        encoding="utf-8",
    )
    return tmp_path / "digests", events


def test_blocks_are_found_below_an_agent_subdirectory(corpus) -> None:
    """The live corpus is `digests/<agent>/<date>.md`. A flat `glob("*.md")` finds nothing and
    reports a clean corpus — a zero that looks like a pass."""
    assert len(list(iter_blocks(corpus[0]))) == 2


def test_each_block_is_graded_against_its_own_log(corpus) -> None:
    """vikunja#852. Both blocks make a true claim about their own session, and each claim is a
    fabrication relative to the other. Grading the file against one log reports the second
    block as ungrounded; grading per block reports nothing."""
    result = survey(*corpus)
    assert result.blocks == 2
    assert result.findings_path == 0, [v.claim for v in result.verdicts]


def test_a_block_whose_claim_belongs_to_the_other_session_is_caught(corpus) -> None:
    """The matched pair — the per-block grading must still be a grading."""
    digest = next((corpus[0] / "developer").glob("*.md"))
    digest.write_text(
        digest.read_text().replace("/beta/tests/unit/test_two.py", "/gamma/tests/unit/test_x.py"),
        encoding="utf-8",
    )
    result = survey(*corpus)
    assert result.findings_path == 1
    assert result.buckets[ABSENT] == 1


def test_the_instrument_agrees_with_the_gate(corpus) -> None:
    """`gate_disagreements` is the drift alarm. Under the shipped rule the survey's own path
    verdicts must match the findings `check_groundedness` produced, claim for claim."""
    assert survey(*corpus, rule=RULE_CURRENT).gate_disagreements == 0


def test_the_literal_rule_is_strictly_stricter(corpus) -> None:
    """The "before" number has to be reproducible from the same commit as the "after" one, or
    the comparison needs a checkout and nobody re-checks it."""
    before = survey(*corpus, rule=RULE_LITERAL)
    after = survey(*corpus, rule=RULE_CURRENT)
    assert before.findings_path > after.findings_path
    assert before.blocks == after.blocks


def test_a_missing_event_log_is_counted_not_skipped_silently(corpus) -> None:
    """A block with no log is not a passing block. Counting it as one would hide exactly the
    loss scribe's recovery work exists to surface."""
    for path in (corpus[1]).glob("*.json"):
        path.unlink()
    result = survey(*corpus)
    assert result.blocks == 0 and result.blocks_no_log == 2


def test_rejected_claims_survive_a_quote_and_are_kept_apart_by_kind() -> None:
    """A claim carrying a quote must come back exactly, and must not cross routes.

    This used to guard a `repr` parse: the claim was recovered by stripping a shared prefix off
    `detail` and `literal_eval`-ing the rest, and a naive strip would corrupt any claim holding
    a quote and silently misfile it into `absent`. `Finding.claim` removed the parse, so the
    corruption it guarded is now unreachable by construction.

    The property it was really protecting is not: recovery must be exact, and it must separate
    the routes. So it is asserted against the replacement mechanism rather than retired -- and
    with the detail deliberately set to a *different* claim, which the old parse would have
    believed and this one cannot even see.
    """
    report = Report()
    weird = "/tmp/it's/a/path.md"
    report.add(
        "groundedness",
        "path not in the event log: '/decoy/not/the/claim.md'",
        kind=CLAIM_PATH,
        claim=weird,
    )
    report.add(
        "groundedness", "tool not in the event log: 'WebFetch'", kind=CLAIM_TOOL, claim="webfetch"
    )
    report.add(
        "groundedness",
        "ticket '#417' is not in the event log, which contains '417' only as an internal id",
        kind=CLAIM_TICKET,
        claim="#417",
    )
    report.add("event_coverage", "digest references 1/90 events", kind="", claim="")

    assert rejected_paths(report) == [weird]
    assert rejected_claims(report, CLAIM_TOOL) == ["webfetch"]
    # The mid-sentence claim no prefix constant could have recovered -- the case that decided
    # the design. 30 of 62 live ticket findings take this shape.
    assert rejected_claims(report, CLAIM_TICKET) == ["#417"]
    assert rejected_claims(report, CLAIM_CODE) == []


def test_buckets_are_disjoint_and_ordered() -> None:
    """A claim that is both a composition and a suffix match is counted once, as the stronger
    of the two. Without a fixed order the shares depend on evaluation order and two runs of the
    same classifier disagree — which is how this build started."""
    corpus_text = "/home/user/repos/personal/alpha and tests/unit/test_one.py"
    bucket, _, split = classify_path_claim(
        "/home/user/repos/personal/alpha/tests/unit/test_one.py",
        set(),
        corpus_text,
        home="/home/user",
    )
    assert bucket == COMPOSED and split[0] == "/home/user/repos/personal/alpha"
    assert (
        classify_path_claim("/nowhere/at/all.md", set(), corpus_text, home="/home/user")[0]
        == ABSENT
    )
    assert BUCKETS.index(COMPOSED) < BUCKETS.index(SUFFIX) < BUCKETS.index(ABSENT)


def test_longest_present_suffix_counts_from_the_deep_end() -> None:
    """The floor is chosen against this number, so it has to be the *most* of the claim the
    corpus accounts for, not the first run that happens to match."""
    corpus_text = "unit/test_one.py appears, and so does tests/unit/test_one.py"
    assert longest_present_suffix("/home/user/alpha/tests/unit/test_one.py", corpus_text) == 3
    assert longest_present_suffix("/home/user/alpha/nothing.py", corpus_text) == 0


def test_the_probe_prices_every_floor_it_is_given(corpus) -> None:
    """The floors are derived from this, so it must actually vary with them rather than
    returning one number under different labels."""
    probe = cross_session_probe(*corpus, floors=((2, 1), (2, 3)), per_block=5)
    assert [f["min_tail"] for f in probe["floors"]] == [1, 3]
    assert probe["shipped"]["window"] > 0


def test_the_survey_cli_never_writes_to_the_corpus(corpus, tmp_path, capsys) -> None:
    """This build grades; it does not summarize. A QC change that mutates the corpus is the one
    way it could do damage, and 185 live digest files went in."""
    digests, events = corpus
    before = {p: p.read_bytes() for p in digests.rglob("*.md")}
    out = tmp_path / "result.json"
    assert survey_main(["--digests", str(digests), "--events", str(events), "--out", str(out)]) == 0
    assert {p: p.read_bytes() for p in digests.rglob("*.md")} == before
    assert json.loads(out.read_text())["blocks"] == 2


def test_the_survey_cli_rejects_an_unknown_bucket(corpus) -> None:
    digests, events = corpus
    assert (
        survey_main(["--digests", str(digests), "--events", str(events), "--bucket", "nonsense"])
        == 2
    )


def test_the_probe_prints_the_shipped_configuration(corpus, capsys) -> None:
    """The floors in `qc` cite this output. If the shipped row stopped printing, the docstring
    would be quoting a table nobody could regenerate."""
    digests, events = corpus
    assert survey_main(["--digests", str(digests), "--events", str(events), "--probe"]) == 0
    out = capsys.readouterr().out
    assert "shipped (prefix 2, tail 2, window 120)" in out
    assert "foreign claims tested:" in out


def test_the_summary_reports_every_bucket(corpus, capsys) -> None:
    """A bucket that silently stopped printing would look like a bucket that emptied."""
    digests, events = corpus
    assert survey_main(["--digests", str(digests), "--events", str(events)]) == 0
    out = capsys.readouterr().out
    for bucket in BUCKETS:
        assert bucket in out
    assert "gate disagreements: 0" in out


def test_the_bucket_listing_prints_only_that_bucket(corpus, capsys) -> None:
    digest = next((corpus[0] / "developer").glob("*.md"))
    digest.write_text(
        digest.read_text().replace("/beta/tests/unit/test_two.py", "/gamma/tests/unit/test_x.py"),
        encoding="utf-8",
    )
    digests, events = corpus
    assert (
        survey_main(["--digests", str(digests), "--events", str(events), "--bucket", ABSENT]) == 0
    )
    out = capsys.readouterr().out
    assert "/gamma/tests/unit/test_x.py" in out
    assert out.count("\n") == 1


def test_json_output_carries_the_rule_it_was_produced_under(corpus, capsys) -> None:
    """A before/after pair of JSON files is useless if neither says which rule made it."""
    digests, events = corpus
    assert (
        survey_main(
            ["--digests", str(digests), "--events", str(events), "--json", "--rule", RULE_LITERAL]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["rule"] == RULE_LITERAL


def test_home_forms_swaps_in_both_directions() -> None:
    """The survey's home bucket is a diagnostic, not the shipped rule — but it has to report
    both directions or it will attribute a tilde-written claim to the wrong bucket."""
    assert home_forms("~/repos/x.md", "/home/user") == ["/home/user/repos/x.md"]
    assert home_forms("/home/user/repos/x.md", "/home/user") == ["~/repos/x.md"]
    assert home_forms("/etc/example/x.yml", "/home/user") == []
    assert home_forms("~/repos/x.md", "") == []


def test_qc_survey_is_reachable_through_the_dispatcher(corpus, capsys) -> None:
    """A verb absent from `__main__` is a verb nobody can run — and this one is what anyone
    re-checking the figures in `qc.MIN_TAIL_SEGMENTS` has to reach for."""
    from scribe.__main__ import main as dispatch

    digests, events = corpus
    assert dispatch(["qc-survey", "--digests", str(digests), "--events", str(events)]) == 0
    assert "blocks graded: 2" in capsys.readouterr().out


def test_a_traversal_payload_in_an_anchor_cannot_escape_the_eventlog_root(corpus) -> None:
    """The session id is attacker-controllable in the only sense that matters here: it is read
    back out of a digest file, which holds model output.

    `ANCHOR_RE` captures it as `\\S+`, so `../outside/evil` is a syntactically valid anchor and
    the survey hands it straight to `eventlog_path`. `safe_stem` is what contains it — anything
    that is not a bare identifier is **replaced** with a hash rather than scrubbed, because
    scrubbing produces names that collide with a real session's.

    A planted file at the traversal destination is what makes this test discriminating. Without
    one, the id resolves to a path that does not exist either way and the assertion passes with
    the containment removed — which is what the first version of this test did.
    """
    digests, events = corpus
    outside = events.parent / "outside"
    outside.mkdir()
    planted = _log("planted", "this log is outside the event log root")
    (outside / "evil.json").write_text(
        json.dumps(planted.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    digest = next((digests / "developer").glob("*.md"))
    digest.write_text(
        digest.read_text().replace("session:aaaaaaaa", "session:../outside/evil"),
        encoding="utf-8",
    )

    result = survey(digests, events)
    assert result.blocks == 1, "the planted log outside the root was read"
    assert result.blocks_no_log == 1


# --- vikunja#888: attribution past the path route -----------------------------------------


def test_every_groundedness_finding_lands_in_a_bucket(corpus) -> None:
    """The gap this build opened with: 289 findings, 162 classified, 127 counted and unnamed.

    A tolerance cannot be chosen for a route whose failures cannot be attributed, so an
    unclassified remainder is a defect rather than a rounding detail. `findings_unattributed`
    exists to make one visible instead of absorbing it into a total.
    """
    digests, events = corpus
    (digests / "developer" / "extra.md").write_text(
        _block(
            _log("s-extra", "nothing relevant here"),
            "Closed `#4242` after reading `/nowhere/at/all.md` with `WebFetch` and "
            "`fabricated_helper()`.",
        ),
        encoding="utf-8",
    )
    write_eventlog(events, _log("s-extra", "nothing relevant here"))
    result = survey(digests, events)

    assert result.findings_unattributed == 0
    assert sum(result.kind_buckets.values()) == sum(result.kinds.values())
    assert result.gate_disagreements == 0


@pytest.mark.parametrize(
    "claim,corpus_text,expected",
    [
        ("#541", '"args_digest": "task_id=541"', ID_CONFLATION),
        ("#930", '{"target": "930"}', ID_CONFLATION),
        ("#73", "the run reported 73 pipelines", COINCIDENTAL_NUMBER),
        ("#783", "nothing of the sort here", ABSENT),
    ],
)
def test_ticket_findings_are_attributed_to_a_cause(claim, corpus_text, expected) -> None:
    """61 of 62 live ticket findings are `id-conflation` — the gate working, not over-firing."""
    assert classify_ticket_claim(claim, corpus_text) == expected


@pytest.mark.parametrize(
    "claim,corpus_text,expected",
    [
        ('"operator"', "role operator assigned", PUNCTUATION_WRAPPED),
        ("#vikunja", "posted to the vikunja room", SINGLE_TOKEN),
        ("alpha beta", "alpha here ... and much later beta", TOKEN_SPREAD),
        ("alpha missingtok", "alpha here only", TOKEN_MISSING),
        ("zzz yyy", "nothing", ABSENT),
    ],
)
def test_span_findings_are_attributed_to_a_cause(claim, corpus_text, expected) -> None:
    assert classify_span_claim(claim, corpus_text * 40, desynced=set()) == expected


def test_the_control_set_stays_scoped_to_the_path_route(corpus) -> None:
    """`ABSENT` is a bucket for every kind now. Widening `control_set` to match would silently
    redefine what three releases of "the 26 absent claims" refers to."""
    digests, events = corpus
    result = survey(digests, events)
    assert all(v.kind == CLAIM_PATH for v in result.control_set())


def test_the_span_trim_probe_prices_the_tolerance_it_declines(corpus) -> None:
    """The rejection is committed as a re-runnable measurement, not as an assertion."""
    digests, events = corpus
    probe = span_trim_probe(digests, events, min_lengths=(0, 12))
    assert [row["min_length"] for row in probe["candidates"]] == [0, 12]
    for row in probe["candidates"]:
        assert row["false_ground"] >= 0 and row["true_ground"] >= 0


def test_the_span_probe_prints_the_bar_it_is_judged_against(corpus, capsys) -> None:
    """A false-ground rate with nothing to compare it to is a number, not a decision. The
    threshold #876 accepted and the one it rejected print beside the table."""
    digests, events = corpus
    assert survey_main(["--digests", str(digests), "--events", str(events), "--probe-spans"]) == 0
    out = capsys.readouterr().out
    assert "true/false" in out
    assert "5.0 true per false" in out and "1.3 was rejected" in out
