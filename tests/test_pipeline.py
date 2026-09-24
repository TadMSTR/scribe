"""End-to-end pipeline tests.

The property these are mostly about: a dry run must be genuinely inert. Phase 6 is a shadow
run and the live path must not change, so "dry" has to mean no provider call, no credential
read, and no write — not merely "no crash".
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.config import Config, ProviderConfig, StageConfig
from scribe.eventlog import write_eventlog
from scribe.extract.models import EventLog
from scribe.pipeline import (
    _CAUSE_DETAIL,
    CAUSE_EXTRACTION,
    CAUSE_MODEL,
    CAUSE_UNKNOWN,
    classify_post_render,
    output_root,
    process_session,
    run_once,
    session_when,
    summarize_run,
)
from scribe.state import (
    MAX_PROVISIONAL_ATTEMPTS,
    STATUS_FAILED,
    STATUS_PROVISIONAL,
    STATUS_SUMMARIZED,
    Store,
)
from scribe.summarize.providers import Completion, Provider, ProviderError

FIXTURES = Path(__file__).parent / "fixtures"
WHEN = datetime(2026, 9, 13, 14, 52, tzinfo=UTC)
GOOD = json.dumps(
    {
        "asked": "First real question about vikunja#843",
        "done": ["Created a branch with `git -C /repo checkout -b feat/thing 9f8e7d6`"],
        "tickets": ["#843"],
    }
)


class Stub(Provider):
    name = "stub"

    def __init__(self, *script) -> None:
        self.script = list(script) or [GOOD]
        self.calls = 0

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        self.calls += 1
        item = self.script.pop(0) if self.script else GOOD
        if isinstance(item, Exception):
            raise item
        return Completion(
            text=item, input_tokens=50, output_tokens=10, model="stub-1", provider="stub"
        )


class Exploding(Provider):
    """Fails the test loudly if a dry run ever reaches a provider."""

    name = "exploding"

    def complete(self, system, user, *, timeout=120.0):
        raise AssertionError("a dry run must not call the provider")


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    d = projects / "-home-ted--claude-projects-research"
    d.mkdir(parents=True)
    target = d / "sess.jsonl"
    target.write_bytes((FIXTURES / "transcript-structural.jsonl").read_bytes())
    old = 1_000_000.0 - 3600
    os.utime(target, (old, old))
    cfg = Config(
        quiet_period_minutes=15,
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
        providers={
            "stub": ProviderConfig(
                name="stub", type="openai-compatible", base_url="http://x/v1", model="m"
            )
        },
        stages={"session": StageConfig(provider="stub", model="m")},
    )
    return cfg, Store(cfg.state_path), target


def test_a_dry_run_finds_the_session_but_calls_nothing(env) -> None:
    cfg, store, _t = env
    results = run_once(
        cfg, store, dry_run=True, now=1_000_000.0, provider_factory=lambda: Exploding()
    )
    assert len(results) == 1
    assert results[0].events == 3
    assert results[0].dry_run is True
    assert results[0].summarized is False


def test_a_dry_run_writes_nothing(env) -> None:
    """Inertness covers the event log too.

    It would have been convenient to persist it here — "is the event log right" is the
    question a dry run is for. But a dry run is documented to write nothing, and that is
    what makes it safe to sweep the real corpus with; `scribe extract --json` answers the
    same question without spending the guarantee.
    """
    cfg, store, _t = env
    run_once(cfg, store, dry_run=True, now=1_000_000.0)
    assert not output_root(cfg).exists()
    assert not Path(cfg.eventlog_dir).exists()


def test_a_dry_run_does_not_advance_the_session_state(env) -> None:
    """It must remain available for the live run that follows."""
    cfg, store, target = env
    run_once(cfg, store, dry_run=True, now=1_000_000.0)
    assert store.get(str(target)).status != STATUS_SUMMARIZED
    assert len(run_once(cfg, store, dry_run=True, now=1_000_000.0)) == 1


def test_a_live_run_summarizes_and_writes(env) -> None:
    cfg, store, target = env
    stub = Stub()
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: stub)
    (r,) = results
    assert stub.calls == 1
    assert r.summarized is True
    assert r.written is True
    written = list(output_root(cfg).rglob("*.md"))
    assert len(written) == 1
    assert written[0].read_text().endswith("\n")
    assert store.get(str(target)).status == STATUS_SUMMARIZED


def test_output_goes_to_scribes_own_root_not_the_live_memory_tree(env) -> None:
    """Phase 6 changes nothing in the live path."""
    cfg, store, _t = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    (written,) = list(output_root(cfg).rglob("*.md"))
    assert output_root(cfg) in written.parents
    assert ".memsearch" not in str(written)
    assert written.parent.name == "research"


def test_a_second_live_run_does_not_duplicate_the_session(env) -> None:
    cfg, store, _t = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    second = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert second == []
    (written,) = list(output_root(cfg).rglob("*.md"))
    assert written.read_text().count("## Session") == 1


def test_spend_is_recorded_for_a_live_run(env, tmp_path) -> None:
    cfg, store, _t = env
    log = tmp_path / "tokens.log"
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(), spend_log=str(log))
    (rec,) = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    assert rec["input_tokens"] == 50
    assert rec["event_kind"] == "summarize"


def test_no_spend_is_recorded_for_a_dry_run(env, tmp_path) -> None:
    cfg, store, _t = env
    log = tmp_path / "tokens.log"
    run_once(cfg, store, dry_run=True, now=1_000_000.0, spend_log=str(log))
    assert not log.exists()


def test_qc_runs_on_the_produced_digest(env) -> None:
    cfg, store, _t = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.qc_ok is True
    assert r.qc_coverage > 0


def test_a_hallucinating_model_is_caught_by_qc_and_still_reported(env) -> None:
    """A run report that silently omits failures is the same blind spot this replaces."""
    cfg, store, _t = env
    bad = json.dumps({"asked": "x", "done": ["Edited /etc/nginx/nginx.conf"]})
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(bad))
    assert r.summarized is True
    assert r.qc_ok is False
    assert any("nginx" in f for f in r.qc_findings)


def test_a_permanent_provider_failure_writes_a_marked_placeholder(env) -> None:
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    assert r.summarized is False
    (written,) = list(output_root(cfg).rglob("*.md"))
    assert "placeholder, not a summary" in written.read_text()
    row = store.get(str(target))
    assert row.status == STATUS_PROVISIONAL
    # Not `failed`: the status now says what is actually on disk. A placeholder is a block
    # that exists and is not an answer, and the old `failed` left `last_offset` behind, so
    # the byte check re-offered the session on every sweep forever -- 12 calls on one real
    # session, none of which could have landed.
    assert row.last_offset > 0
    assert row.attempts == 1


def test_a_failed_session_is_retried_on_the_next_sweep(env) -> None:
    """End to end: placeholder, then a real digest that *replaces* it.

    This is the whole of #872/#868 in one test. Before the fix the second sweep really did
    run and really did produce this digest -- and `append_block` refused it, because the
    placeholder already held the turn uuid.
    """
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    again = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert len(again) == 1
    assert again[0].summarized is True
    assert again[0].written is True
    assert again[0].discarded is False

    (written,) = list(output_root(cfg).rglob("*.md"))
    text = written.read_text()
    assert "placeholder, not a summary" not in text
    assert "provisional:" not in text
    assert text.count("<!-- session:") == 1
    assert store.get(str(target)).status == "summarized"


def test_the_retry_budget_is_bounded(env) -> None:
    """A session that keeps failing stops costing a summarization call per sweep.

    The failure this guards is measured, not hypothetical: one production session reached
    **12 attempts**, each a ~50k-token call, because a placeholder left `last_offset` behind
    and the byte check re-offered it indefinitely.
    """
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    for _ in range(MAX_PROVISIONAL_ATTEMPTS):
        assert len(run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))) == 1
    assert store.get(str(target)).attempts == MAX_PROVISIONAL_ATTEMPTS
    assert run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err)) == []


def test_an_exhausted_session_is_reopened_by_a_reset(env) -> None:
    """Spending the budget is a verdict on the current code, not on the session.

    Without this, "retry a bounded number of times" would mean a session lost to a bug stays
    lost after the bug is fixed — which is exactly the 30-session position this build is in.
    """
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    for _ in range(MAX_PROVISIONAL_ATTEMPTS):
        run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    assert run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub()) == []

    assert store.reset_for_retry(str(target)) is True
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.summarized is True and r.written is True


def test_a_suppressed_session_is_provisional_not_summarized(env) -> None:
    """#868's half. A suppression note went straight to `summarized` — terminal, on a session
    that was never summarized — so a re-run skipped it and the digest never existed."""
    cfg, store, target = env
    scaffold = json.dumps({"asked": "x", "done": ["- <specific step>"]})
    # Three, because the runner retries a contamination rejection with a reminder and the
    # stub falls back to a good response once its script runs out — two would test the
    # reminder rescuing the run, which is a different (and also correct) behaviour.
    (r,) = run_once(
        cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(scaffold, scaffold, scaffold)
    )
    assert r.suppressed is True
    assert r.summarized is False
    assert store.get(str(target)).status == STATUS_PROVISIONAL

    (again,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert again.summarized is True
    (written,) = list(output_root(cfg).rglob("*.md"))
    assert "Summary suppressed" not in written.read_text()


def test_the_session_id_column_is_populated(env) -> None:
    """It was empty on all 441 production rows, because `scan` upserts from a filesystem stat
    and the id lives inside the transcript. An always-empty column reads like a usable key:
    matching the 30 affected sessions by `session_id` returned 0 of 30."""
    cfg, store, target = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert store.get(str(target)).session_id


def test_the_session_id_column_is_populated_on_the_provisional_path_too(env) -> None:
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    assert store.get(str(target)).session_id


def test_an_unreadable_transcript_is_recorded_not_fatal(env, monkeypatch) -> None:
    """One malformed transcript must not stop the other fourteen."""
    cfg, store, target = env
    row = store.upsert_observed(str(target), size_bytes=10, mtime_ns=1, agent="research")
    monkeypatch.setattr(
        "scribe.pipeline.extract",
        lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")),
    )
    r = process_session(row, cfg, store, provider=Stub(), now=WHEN)
    assert "permission denied" in r.errors[0]
    assert store.get(str(target)).status == STATUS_FAILED


def test_a_transcript_with_no_real_turns_is_settled_not_retried(env, tmp_path) -> None:
    """A quiet session is a real outcome. Leaving it unmarked would re-offer it forever."""
    cfg, store, _t = env
    empty_dir = Path(cfg.project_globs[0].rstrip("/*")) / "-home-ted--claude-projects-writer"
    empty_dir.mkdir(parents=True, exist_ok=True)
    blank = empty_dir / "blank.jsonl"
    blank.write_text("")
    os.utime(blank, (1_000_000.0 - 3600, 1_000_000.0 - 3600))
    row = store.upsert_observed(str(blank), size_bytes=0, mtime_ns=1, agent="writer")
    r = process_session(row, cfg, store, provider=Stub(), now=WHEN)
    assert r.turns == 0
    assert store.get(str(blank)).status == STATUS_SUMMARIZED


def test_limit_caps_the_sweep(env, tmp_path) -> None:
    cfg, store, target = env
    sibling = target.parent / "sess2.jsonl"
    sibling.write_bytes(target.read_bytes())
    os.utime(sibling, (1_000_000.0 - 3600, 1_000_000.0 - 3600))
    assert len(run_once(cfg, store, dry_run=True, now=1_000_000.0, limit=1)) == 1


def test_summarize_run_aggregates(env) -> None:
    cfg, store, _t = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    totals = summarize_run(results)
    assert totals["sessions"] == 1
    assert totals["summarized"] == 1
    assert totals["groundedness_rate"] == 1.0
    assert totals["events_total"] == 3


def test_summarize_run_on_an_empty_sweep() -> None:
    totals = summarize_run([])
    assert totals["sessions"] == 0
    assert totals["groundedness_rate"] is None


def test_process_session_refuses_the_provider_even_when_handed_one(env) -> None:
    """Isolates the guard inside process_session.

    The sweep-level test cannot see this one: `run_once` does not BUILD a provider for a dry
    run, so `provider` is None there and the early return fires for the wrong reason. Both
    guards were mutation-tested and both survived, each masked by the other — defence in
    depth that no test could distinguish from a single guard. This passes a live provider
    explicitly so only the `dry_run` check can stop it.
    """
    cfg, store, target = env
    row = store.upsert_observed(str(target), size_bytes=100, mtime_ns=1, agent="research")
    result = process_session(row, cfg, store, provider=Exploding(), dry_run=True, now=WHEN)
    assert result.dry_run is True
    assert result.summarized is False
    assert not output_root(cfg).exists()


def test_run_once_does_not_even_construct_a_provider_for_a_dry_run(env) -> None:
    """Isolates the guard in run_once.

    Constructing the provider is not free of consequence: it validates config and, for a
    provider type that needed one, would be the first place a credential could be demanded.
    A dry run must not reach it at all.
    """
    cfg, store, _t = env
    built: list[str] = []

    def factory():
        built.append("constructed")
        return Stub()

    run_once(cfg, store, dry_run=True, now=1_000_000.0, provider_factory=factory)
    assert built == [], "a dry run constructed a provider"


def test_run_once_does_construct_one_for_a_live_run(env) -> None:
    """The matched half — otherwise the assertion above is satisfied by a factory that is
    never called under any circumstances."""
    cfg, store, _t = env
    built: list[str] = []

    def factory():
        built.append("constructed")
        return Stub()

    run_once(cfg, store, dry_run=False, now=1_000_000.0, provider_factory=factory)
    assert built == ["constructed"]


# --- post-render redaction guard (scribe-2026-09 audit, structural observation) ------


def test_a_secret_in_the_model_output_is_scrubbed_before_it_reaches_disk(env) -> None:
    """Defence in depth. The audit observed that nothing re-redacted the summarizer's
    RENDERED output, so the whole redaction model rested on extraction-time completeness —
    and two Medium findings in that layer showed the assumption was not free.
    """
    cfg, store, _t = env
    leaky = json.dumps(
        {"asked": "x", "done": ["used GITHUB_TOKEN=ghp_FAKE1234567890abcdefgh to push"]}
    )
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(leaky))
    (written,) = list(output_root(cfg).rglob("*.md"))
    text = written.read_text()
    assert "ghp_FAKE" not in text
    assert "«redacted»" in text


def test_the_post_render_guard_is_a_detector_not_a_silent_cleanup(env) -> None:
    """A hit is surfaced loudly. It is the *cause* the message must not overstate.

    Retargeted for vikunja#856. This test previously asserted the message said the secret
    "survived extraction" — but the fixture below is the model-output case, not the
    extraction-miss one: the token is in the STUB PROVIDER'S RESPONSE, never in the
    transcript, so the event log cannot contain it. The old assertion passed while naming
    the wrong file, which is precisely the defect #856 describes.
    """
    cfg, store, _t = env
    leaky = json.dumps({"asked": "x", "done": ["token is ghp_FAKE1234567890abcdefgh"]})
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(leaky))
    assert r.post_render_redactions >= 1
    assert any("caught at write time" in e for e in r.errors)


def test_a_value_the_model_invented_is_not_blamed_on_extraction(env) -> None:
    """The model-output branch: the value is absent from the event log, so redact.py is
    explicitly NOT named. This is the case that was misdiagnosed for the whole shadow run."""
    cfg, store, _t = env
    leaky = json.dumps({"asked": "x", "done": ["token is ghp_FAKE1234567890abcdefgh"]})
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(leaky))
    assert r.post_render_cause == CAUSE_MODEL
    assert r.eventlog_path, "the branch is only meaningful when a log was actually consulted"
    (msg,) = [e for e in r.errors if "post-render" in e]
    assert "redact.py is not the file to look at" in msg
    assert "survived extraction" not in msg


def test_a_value_present_in_the_event_log_does_name_redact_py(env) -> None:
    """The extraction-miss branch, constructed as the mirror of the one above.

    Both cases are built, per the plan: a single case would prove only that one branch
    exists. The difference is entirely in what the event log contains — the provider
    response is identical — which is exactly the discrimination #856 asked for.
    """
    cfg, store, _t = env
    secret = "ghp_FAKE1234567890abcdefgh"
    leaky = json.dumps({"asked": "x", "done": [f"token is {secret}"]})
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(leaky))
    # Non-vacuity, asserted rather than assumed: BEFORE the plant this same call must return
    # the other cause. Without this line the test would pass even if `classify_post_render`
    # ignored its arguments and always returned CAUSE_EXTRACTION.
    assert classify_post_render(r.eventlog_path, [secret]) == CAUSE_MODEL

    # Plant the value in the persisted log, which is what "extraction missed it" means: the
    # value reached the event log, so the model was shown it.
    path = pathlib.Path(r.eventlog_path)
    payload = json.loads(path.read_text())
    payload["planted_leaf"] = f"the value was here all along: {secret}"
    path.write_text(json.dumps(payload))

    assert classify_post_render(r.eventlog_path, [secret]) == CAUSE_EXTRACTION
    assert _CAUSE_DETAIL[CAUSE_EXTRACTION].endswith("investigate redact.py")


def test_the_captured_plaintext_never_reaches_any_reported_sink(env) -> None:
    """`capture=True` holds the plaintext of what was matched. Prove it stays held.

    `result.errors` is an unredacted sink — it reaches the JSON report and the CLI, and a
    previous audit on this repo found exactly that. So the guarantee is asserted against the
    *whole reported surface*, not just against the one field the fix touched: if a future
    change pipes `guard.captured` into a message, this goes red.
    """
    cfg, store, _t = env
    secret = "ghp_FAKE1234567890abcdefgh"
    leaky = json.dumps({"asked": "x", "done": [f"token is {secret}"]})
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(leaky))

    assert r.post_render_redactions >= 1, "the fixture must actually trip the detector"
    reported = json.dumps(r.to_dict())
    assert secret not in reported
    assert "ghp_" not in reported
    # And the digest that was written is clean too — the re-scrub is still doing its job.
    (written,) = list(output_root(cfg).rglob("*.md"))
    assert secret not in written.read_text()


def test_an_absent_event_log_yields_no_cause_at_all(env) -> None:
    """The third answer. Before #852 there was no log, and the code asserted a cause anyway;
    with no evidence the honest output is "undetermined", not a guess."""
    assert classify_post_render("", ["ghp_FAKE1234567890abcdefgh"]) == CAUSE_UNKNOWN
    assert classify_post_render("/nonexistent/eventlog.json", ["x"]) == CAUSE_UNKNOWN


def test_the_guard_stays_quiet_on_a_clean_digest(env) -> None:
    """It must discriminate — otherwise the detector half is worthless."""
    cfg, store, _t = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.post_render_redactions == 0
    assert r.post_render_cause == ""
    assert not any("post-render" in e for e in r.errors)


def test_post_render_redactions_are_aggregated_in_the_run_totals(env) -> None:
    cfg, store, _t = env
    leaky = json.dumps({"asked": "x", "done": ["ghp_FAKE1234567890abcdefgh"]})
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(leaky))
    assert summarize_run(results)["post_render_redactions"] >= 1


def test_qc_grades_the_scrubbed_text_not_the_raw_model_output(env) -> None:
    """The gate must judge what actually lands on disk, or its verdict describes a document
    nobody has."""
    cfg, store, _t = env
    leaky = json.dumps({"asked": "x", "done": ["ghp_FAKE1234567890abcdefgh"]})
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(leaky))
    (written,) = list(output_root(cfg).rglob("*.md"))
    assert r.qc_ok is not None
    assert "ghp_FAKE" not in written.read_text()


# --- vikunja#847: the digest's date is the session's, not the run's ------------------------

PAST_START = datetime(2026, 8, 17, 23, 40, tzinfo=UTC)


@pytest.fixture
def past_env(env):
    """The same environment, with a transcript whose session ran on 2026-08-17.

    The fixture deliberately spans midnight -- 23:40 on the 17th through 00:20 on the 18th --
    so it separates all three candidate anchors: the run clock (`WHEN`, 2026-09-13),
    `ended_at` (the 18th) and `started_at` (the 17th). A fixture sitting wholly inside one
    past day would prove only that the clock is unused, and would pass just as happily with
    the wrong one of the two session timestamps. That is the same shape of hole that let 367
    green tests miss this: every other fixture here is dated today.
    """
    cfg, store, target = env
    past = target.parent / "past.jsonl"
    past.write_bytes((FIXTURES / "transcript-past-dated.jsonl").read_bytes())
    os.utime(past, (1_000_000.0 - 3600, 1_000_000.0 - 3600))
    row = store.upsert_observed(
        str(past), size_bytes=past.stat().st_size, mtime_ns=1, agent="research"
    )
    return cfg, store, row


def test_a_digest_is_filed_under_the_session_date_not_the_run_date(past_env) -> None:
    """The backfill defect: 429 sessions would otherwise collapse into one file."""
    cfg, store, row = past_env
    r = process_session(row, cfg, store, provider=Stub(), now=WHEN)
    assert r.written is True
    assert (output_root(cfg) / "research" / "2026-08-17.md").exists()
    assert not (output_root(cfg) / "research" / f"{WHEN:%Y-%m-%d}.md").exists()


def test_a_midnight_spanning_session_is_filed_under_the_day_it_began(past_env) -> None:
    """`started_at`, not `ended_at`. See `session_when` for why the start loses less."""
    cfg, store, row = past_env
    process_session(row, cfg, store, provider=Stub(), now=WHEN)
    # Asserting the absence of the 18th alone would pass under the pre-fix wall-clock
    # behaviour too, which writes to neither day. The positive half is what makes this
    # test specific to the start-vs-end choice.
    assert (output_root(cfg) / "research" / "2026-08-17.md").exists()
    assert not (output_root(cfg) / "research" / "2026-08-18.md").exists()


def test_both_headings_come_from_the_session_not_the_clock(past_env) -> None:
    """The filename alone is not enough -- `writeback` derives two headings from `when` too."""
    cfg, store, row = past_env
    process_session(row, cfg, store, provider=Stub(), now=WHEN)
    text = (output_root(cfg) / "research" / "2026-08-17.md").read_text()
    assert "# 2026-08-17" in text
    assert "## Session 23:40" in text
    assert "### 23:40" in text
    assert f"{WHEN:%H:%M}" not in text


def test_session_when_prefers_the_start_then_the_end_then_the_clock() -> None:
    log = EventLog(session_id="s", transcript_path="t")
    assert session_when(log, WHEN) == WHEN

    log.ended_at = "2026-08-18T00:20:30Z"
    assert session_when(log, WHEN) == datetime(2026, 8, 18, 0, 20, 30, tzinfo=UTC)

    log.started_at = "2026-08-17T23:40:00Z"
    assert session_when(log, WHEN) == PAST_START


def test_an_unparseable_session_timestamp_falls_back_rather_than_raising() -> None:
    """A malformed timestamp must not abort the sweep -- one bad transcript, not fourteen."""
    log = EventLog(session_id="s", transcript_path="t", started_at="not-a-date", ended_at="")
    assert session_when(log, WHEN) == WHEN


def test_a_naive_session_timestamp_is_read_as_utc() -> None:
    """Comparing a naive datetime against an aware one raises; the fallback must not be reached
    by accident, and the result must be formattable."""
    log = EventLog(session_id="s", transcript_path="t", started_at="2026-08-17T23:40:00")
    assert session_when(log, WHEN) == PAST_START


def test_an_offset_session_timestamp_is_normalised_to_utc() -> None:
    """00:40 on the 18th at +01:00 is 23:40 on the 17th in UTC -- and the corpus is UTC."""
    log = EventLog(session_id="s", transcript_path="t", started_at="2026-08-18T00:40:00+01:00")
    assert session_when(log, WHEN) == PAST_START


# --- vikunja#849: a placeholder must be visible in the totals ------------------------------

OVER_CAP = json.dumps(
    {
        "asked": "First real question about vikunja#843",
        "done": ["Created a branch"],
        "tickets": [f"#{i}" for i in range(500)],
    }
)


def test_a_placeholder_is_counted_as_a_placeholder_and_not_as_a_summary(env) -> None:
    """The blind spot this closes.

    `written` counts blocks that reached the file, and `render_failure`'s placeholder is a
    block, so a run that lost every summary reported the same `written` as one that lost
    none. The only tell was `written` and `summarized` differing by one — a subtraction
    nobody was performing. 13 of 431 sessions would have gone this way in the backfill.
    """
    cfg, store, _t = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(OVER_CAP))
    (r,) = results

    assert r.summarized is False
    assert r.written is True, "the placeholder block genuinely did reach the file"
    assert r.placeholder is True
    assert r.suppressed is False

    totals = summarize_run(results)
    assert totals["placeholders"] == 1
    assert totals["summarized"] == 0
    assert totals["written"] == 1, "written keeps its meaning: a block was appended"


def test_a_successful_run_reports_no_placeholder(env) -> None:
    """Pins the counter to the outcome rather than to the code path being reached at all.

    Without this, a counter hardcoded to 1 would pass the test above.
    """
    cfg, store, _t = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    (r,) = results
    assert r.summarized is True and r.placeholder is False
    totals = summarize_run(results)
    assert totals["placeholders"] == 0
    assert totals["written"] == 1


def test_the_three_written_outcomes_account_for_every_written_block(env) -> None:
    """`written == summarized + suppressed + placeholders`, asserted rather than assumed.

    This is the property that makes the totals readable: any future outcome that writes a
    block without landing in one of the three buckets re-opens exactly the gap #849 found.
    """
    cfg, store, _t = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(OVER_CAP))
    t = summarize_run(results)
    assert t["written"] == t["summarized"] + t["suppressed"] + t["placeholders"]


def test_the_placeholder_flag_is_carried_into_the_per_session_report(env) -> None:
    """The run report is JSON before it is a printed line; a field missing from `to_dict` is
    invisible to anything consuming the shadow comparison."""
    cfg, store, _t = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(OVER_CAP))
    d = r.to_dict()
    assert d["placeholder"] is True
    assert d["suppressed"] is False


# ---------------------------------------------------------------------------
# The persisted event log — the drill-down tier (vikunja#852).
# ---------------------------------------------------------------------------


class Watching(Provider):
    """Records the state of the event log root **at the moment the model is called**.

    This is what makes the ordering testable rather than inferable. Asserting after the run
    that the file exists cannot distinguish "written before the call" from "written after
    it", and those two differ in exactly the case the file exists for.
    """

    name = "watching"

    def __init__(self, root: Path, *, raise_with=None) -> None:
        self.root = Path(root)
        self.raise_with = raise_with
        self.seen: list[str] = []

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        self.seen = sorted(p.name for p in self.root.glob("*.json")) if self.root.exists() else []
        if self.raise_with:
            raise self.raise_with
        return Completion(
            text=GOOD, input_tokens=50, output_tokens=10, model="stub-1", provider="stub"
        )


def test_a_live_run_persists_one_event_log_per_session(env) -> None:
    cfg, store, _t = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    written = list(Path(cfg.eventlog_dir).glob("*.json"))
    assert len(written) == 1
    assert r.eventlog_path == str(written[0])


def test_the_event_log_exists_before_the_model_is_called(env) -> None:
    """The ordering constraint, observed from inside the call rather than after it."""
    cfg, store, _t = env
    watcher = Watching(cfg.eventlog_dir)
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: watcher)
    assert len(watcher.seen) == 1, "the event log must be on disk before the provider runs"


def test_a_failed_model_call_still_leaves_the_event_log(env) -> None:
    """The case it exists for: the summary is gone, the evidence is not.

    Written after the call instead, this file would be absent exactly when the digest is a
    placeholder — which is the one time nobody can reconstruct what the run saw.
    """
    cfg, store, _t = env
    err = ProviderError("bad request", retryable=False)
    (r,) = run_once(
        cfg,
        store,
        now=1_000_000.0,
        provider_factory=lambda: Watching(cfg.eventlog_dir, raise_with=err),
    )
    assert r.placeholder is True, "precondition: this run lost its summary"
    assert r.eventlog_path and Path(r.eventlog_path).is_file()


def test_the_persisted_log_can_re_check_its_own_digest(env) -> None:
    """The point of keeping it: `scribe qc` on a digest written in an earlier run.

    Runs the QC *CLI* over the two files as they sit on disk, because that is the path a
    human or CI actually takes, and it is the path whose event-log loader drifted once
    already.
    """
    from scribe.qc_cli import main as qc_main

    cfg, store, _t = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    (digest,) = list(output_root(cfg).rglob("*.md"))

    assert qc_main(["--digest", str(digest), "--events", r.eventlog_path]) == 0


def test_a_hallucinated_digest_still_fails_the_gate_against_the_persisted_log(env) -> None:
    """Pins the test above to the verdict rather than to the gate merely running.

    A `check_digest` that returned PASS unconditionally would satisfy the happy path.
    """
    from scribe.qc_cli import main as qc_main

    cfg, store, _t = env
    bad = json.dumps({"asked": "x", "done": ["Edited /etc/nginx/nginx.conf"]})
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(bad))
    (digest,) = list(output_root(cfg).rglob("*.md"))

    assert qc_main(["--digest", str(digest), "--events", r.eventlog_path]) == 1


def test_the_event_log_root_is_not_inside_the_digest_root(env) -> None:
    """Indexing follows the digest root. A nested event log would be swept up with it."""
    cfg, store, _t = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert output_root(cfg) not in Path(cfg.eventlog_dir).parents
    assert list(output_root(cfg).rglob("*.json")) == []


def test_an_unwritable_event_log_root_does_not_lose_the_digest(env, tmp_path) -> None:
    """A digest without its drill-down tier beats no digest.

    The failure is recorded rather than swallowed — a silently absent event log is how the
    tier would come to be missing for a subset of sessions without anyone noticing.
    """
    cfg, store, _t = env
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    cfg.eventlog_dir = str(blocked / "eventlogs")

    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())

    assert r.written is True and r.summarized is True
    assert r.eventlog_path == ""
    assert any("eventlog" in e for e in r.errors)


def test_the_run_totals_count_the_event_logs(env) -> None:
    cfg, store, _t = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert summarize_run(results)["eventlogs_written"] == 1


def test_a_dry_run_reports_no_event_logs(env) -> None:
    """Pins the counter to the outcome; a constant 1 would pass the test above."""
    cfg, store, _t = env
    results = run_once(cfg, store, dry_run=True, now=1_000_000.0)
    assert summarize_run(results)["eventlogs_written"] == 0


def test_the_event_log_path_is_carried_into_the_per_session_report(env) -> None:
    """The run report is JSON before it is a printed line."""
    cfg, store, _t = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.to_dict()["eventlog_path"] == r.eventlog_path


# --- vikunja#873: a session survives its transcript ----------------------------------------


def _lose_the_session_then_the_transcript(cfg, store, target) -> None:
    """Reproduce the state #873 is actually about, in the order it really happens.

    A provider failure leaves a marked placeholder on disk and the row `provisional` -- the
    event log is persisted BEFORE the model call precisely so it survives this. The transcript
    then ages out at 30 days. What is left is a digest that is not an answer, and an event log
    that is the only remaining evidence of the session.
    """
    err = ProviderError("bad request", retryable=False)
    (first,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    assert first.placeholder is True, "precondition: the session was lost"
    assert first.eventlog_path, "precondition: its event log was persisted"
    assert store.get(str(target)).status == STATUS_PROVISIONAL
    target.unlink()


def test_a_session_whose_transcript_is_gone_still_produces_a_real_digest(env) -> None:
    """The end-to-end this whole phase exists for.

    Transcripts age out at 30 days (`cleanupPeriodDays` unset, vikunja#778) and `eventlogs/`
    has no cleanup policy, so the log outlives its source. Before `load_eventlog`,
    `process_session` reconstructed by calling `extract(transcript_path)` and a deleted
    transcript was terminal.
    """
    cfg, store, target = env
    _lose_the_session_then_the_transcript(cfg, store, target)

    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())

    assert len(results) == 1, "the reopened session was not offered"
    result = results[0]
    assert result.replayed is True
    assert result.summarized is True
    assert result.placeholder is False
    assert result.written is True
    assert result.events == 3, "a replay must carry the same events the extract did"
    digest = next(output_root(cfg).rglob("*.md"))
    assert "First real question" in digest.read_text(encoding="utf-8")


def test_a_replay_is_reported_as_one_rather_than_passing_for_an_extraction(env) -> None:
    """A replayed digest is as good as its log and no better. An operator reading a run report
    should be able to see which sessions were reconstructed rather than read."""
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    (first,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    assert first.replayed is False
    assert first.to_dict()["replayed"] is False

    target.unlink()
    (second,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert second.to_dict()["replayed"] is True


def test_a_transcript_that_exists_is_always_preferred_over_the_log(env) -> None:
    """The log is a fallback, not a cache. A session that resumed has grown since its log was
    written, and re-extracting is what picks that up -- measured live, the one session of 429
    that differed was exactly this case."""
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    (first,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    # A log that would be obviously wrong if it were read in preference to the transcript.
    stale = EventLog(session_id=first.session_id, transcript_path=str(target), agent="research")
    write_eventlog(cfg.eventlog_dir, stale)

    (second,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert second.replayed is False
    assert second.events == 3, "the transcript on disk was not what got read"


def test_an_unreadable_transcript_is_a_failure_not_a_silent_replay(env) -> None:
    """`OSError` is NOT "transcript absent". Replaying an older log on a permissions fault
    would hide a real problem behind a stale-but-plausible digest, and the session would be
    recorded as summarized."""
    cfg, store, target = env
    err = ProviderError("bad request", retryable=False)
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    target.chmod(0o000)
    try:
        row = store.get(str(target))
        result = process_session(row, cfg, store, provider=Stub())
    finally:
        target.chmod(0o600)
    assert result.replayed is False
    assert result.summarized is False
    assert any("extract:" in e for e in result.errors)
    assert store.get(str(target)).status == STATUS_FAILED


# --- the traversal, end to end through a real transcript ----------------------------------


def _inject_cwd(target: Path, cwd: str) -> None:
    """Put a top-level `cwd` on the transcript's first well-formed record, leaving the rest
    byte-identical.

    Rewriting the whole file through `json.loads`/`json.dumps` would silently drop the
    deliberately malformed line the fixture carries for `records_unparsable` -- so the
    transcript under test would stop being the one the other tests use.
    """
    lines = target.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        record["cwd"] = str(pathlib.Path(cwd).expanduser())
        lines[i] = json.dumps(record)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    raise AssertionError("fixture has no parsable record to carry cwd")


def test_a_crafted_cwd_cannot_write_a_digest_outside_the_output_root(env, tmp_path) -> None:
    """The Medium from the p4 audit, driven the whole way rather than asserted at the unit.

    `log.cwd` is taken from the first top-level `cwd` key in the transcript's own records with
    no shape check, `_agent_from_cwd` returns `Path(cwd).name`, and `daily_path` used to join
    that straight onto the output root. A `cwd` of `~/.claude/projects/..` therefore put a
    digest one level above the tree, under a predictable `YYYY-MM-DD.md` name.

    Unit tests on `daily_path` alone would not have caught the introduction of this, because
    the interesting part is that `cwd` reaches it at all.
    """
    cfg, store, target = env
    _inject_cwd(target, "~/.claude/projects/..")
    old = 1_000_000.0 - 3600
    os.utime(target, (old, old))

    (result,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())

    assert result.written is True, "the digest must still be written, not discarded"
    root = output_root(cfg).resolve()
    written = list(root.rglob("*.md"))
    assert written, "no digest was written at all"
    for path in written:
        assert root in path.resolve().parents
    # And nothing landed in the parent, which is where it used to go.
    assert not list(root.parent.glob("*.md"))


def test_that_crafted_cwd_really_did_reach_the_agent_field(env) -> None:
    """Control for the test above. If the extractor stopped reading `cwd` from that record,
    the traversal test would pass while exercising nothing."""
    _cfg, _store, target = env
    _inject_cwd(target, "~/.claude/projects/..")

    from scribe.extract import extract
    from scribe.extract.parser import _agent_from_cwd

    log = extract(target)
    assert log.cwd.endswith("/projects/..")
    assert _agent_from_cwd(log.cwd) == "..", "the unsafe value no longer reaches attribution"
    assert log.agent == ".."


OVERLONG_FOUND = json.dumps(
    {
        "asked": "First real question about vikunja#843",
        "done": ["Created a branch with `git -C /repo checkout -b feat/thing 9f8e7d6`"],
        # 44, the length that actually cost session 6017c8ce its summary on 2026-09-17.
        "found": [f"A finding numbered {i}" for i in range(44)],
        "tickets": ["#843"],
    }
)


def test_an_overlong_unbounded_field_is_written_not_placeheld(env) -> None:
    """End to end, the behaviour vikunja#884 is about.

    Before this, a 44-item `found` raised a non-retryable `SchemaError`, the session went
    straight to a placeholder and the summary was gone permanently. The session must now come
    out `summarized` and `written`, with a real digest on disk — the surplus dropped, and
    nothing else about the run changed.
    """
    cfg, store, _t = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(OVERLONG_FOUND))
    r = results[0]

    assert r.summarized and r.written
    assert not r.placeholder, "a long prose field must not cost the whole session"
    assert r.truncated_items == 4
    # The breakdown, end to end. The scalar says a cap fired; only this says which one, and
    # it was collapsed here at the point of recording until vikunja#887.
    assert r.truncated_fields == {"found": 4}
    # A first sweep reads from byte 0, so this session is a full read. Asserted on the real
    # pipeline rather than on a hand-built SessionResult, because the value is read off the
    # store row and a flag that is never populated would still pass a unit test of the field.
    assert r.full_read is True

    written = "".join(p.read_text() for p in Path(cfg.output_dir).rglob("*.md"))
    assert "A finding numbered 0" in written
    assert "A finding numbered 43" not in written
    assert "4 further items dropped at the 40-item cap." in written


def test_truncation_does_not_break_the_written_identity(env) -> None:
    """`written == summarized + suppressed + placeholder` is the identity that made this whole
    family of bugs visible. Truncation is an annotation on a success, not a fourth outcome, so
    it must leave that arithmetic exactly where it was."""
    cfg, store, _t = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(OVERLONG_FOUND))
    t = summarize_run(results)

    assert t["written"] == t["summarized"] + t["suppressed"] + t["placeholders"]
    assert t["truncated"] == 1
    assert t["truncated_items"] == 4
    assert t["truncated_fields"] == {"found": 4}


def test_an_ordinary_sweep_reports_no_truncation(env) -> None:
    """A non-zero truncation count on a clean run would make the signal useless for deciding
    whether `MAX_ITEMS` is set anywhere near right."""
    cfg, store, _t = env
    t = summarize_run(run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub()))
    assert t["truncated"] == 0
    assert t["truncated_items"] == 0


# --- vikunja#902 -------------------------------------------------------------------------
#
# `test_a_dry_run_does_not_advance_the_session_state` above asserts exactly the right
# property and passes anyway, because the `env` fixture's session has PENDING TURNS: it
# takes the guard at `process_session`'s `if dry_run or provider is None` and never reaches
# either `mark_summarized` above it. The two fixtures below are the ones that do reach them.
#
# What is asserted, and what is deliberately NOT:
#
#   A dry run may OBSERVE. It must not PROCESS.
#
# `upsert_observed` (size_bytes, mtime_ns, updated_at) and `scan`'s `set_status` -> COMPLETE
# are both discovery recording what is on disk. AGENTS.md invariant 8 makes that the job, not
# a side effect, so asserting the row is unchanged -- or hashing the database -- fails on
# CORRECT code. What a dry run must not touch is what `mark_summarized` owns: the read
# offset, the last turn uuid, the `summarized` status, and the processed_turns ledger.


def _row_state(store, target):
    """The fields `mark_summarized` writes, which are the ones a dry run must not move."""
    row = store.get(str(target))
    with store._connect() as conn:
        processed = conn.execute("SELECT COUNT(*) AS n FROM processed_turns").fetchone()["n"]
    return {
        "last_offset": row.last_offset,
        "last_turn_uuid": row.last_turn_uuid,
        "status_is_summarized": row.status == STATUS_SUMMARIZED,
        "processed_turns": processed,
    }


def _env_for(tmp_path, fixture_name):
    projects = tmp_path / "projects"
    d = projects / "-home-ted--claude-projects-research"
    d.mkdir(parents=True)
    target = d / "sess.jsonl"
    target.write_bytes((FIXTURES / fixture_name).read_bytes())
    old = 1_000_000.0 - 3600
    os.utime(target, (old, old))
    cfg = Config(
        quiet_period_minutes=15,
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
        providers={
            "stub": ProviderConfig(
                name="stub", type="openai-compatible", base_url="http://x/v1", model="m"
            )
        },
        stages={"session": StageConfig(provider="stub", model="m")},
    )
    return cfg, Store(cfg.state_path), target


@pytest.fixture
def turnless_env(tmp_path):
    """A transcript with bytes but no real turn -- reaches the `if not log.turns` branch."""
    return _env_for(tmp_path, "transcript-turnless.jsonl")


def test_a_dry_run_does_not_process_a_turnless_session(turnless_env) -> None:
    """vikunja#902, branch one: `if not log.turns`.

    A transcript of nothing but injected records is the commonest shape in the corpus that
    reaches this. The session has no summary to write, so retiring it looks harmless -- but
    a dry run is documented inert, and inertness is the property people rely on when they
    point the tool at production config.
    """
    cfg, store, target = turnless_env
    run_once(cfg, store, dry_run=True, now=1_000_000.0, provider_factory=lambda: Exploding())

    after = _row_state(store, target)
    assert after["status_is_summarized"] is False, "a dry run must not retire the session"
    assert after["last_offset"] == 0, "a dry run must not advance the offset"
    assert after["last_turn_uuid"] == ""
    assert after["processed_turns"] == 0

    # Still offered on the next sweep -- that is the cost of the fix, and it is the point:
    # the live run that follows is the one entitled to do the bookkeeping.
    assert len(run_once(cfg, store, dry_run=True, now=1_000_000.0)) == 1


def test_a_dry_run_does_not_process_a_fully_processed_session(env) -> None:
    """vikunja#902, branch two: `if not pending`.

    Every turn is already in the ledger, so there is nothing pending -- and the code retires
    the session before it ever consults `dry_run`. Seeded at offset 0 so the byte check still
    offers the row; that is what a resumed session looks like.
    """
    cfg, store, target = env
    stat = target.stat()
    store.upsert_observed(str(target), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
    store.mark_summarized(str(target), offset=0, last_turn_uuid="", turn_uuids=["s1", "s2"])
    before = _row_state(store, target)
    assert before["processed_turns"] == 2, "seeding must put both turns in the ledger"

    run_once(cfg, store, dry_run=True, now=1_000_000.0, provider_factory=lambda: Exploding())

    after = _row_state(store, target)
    assert after["last_offset"] == before["last_offset"], "a dry run must not advance the offset"
    assert after["last_turn_uuid"] == before["last_turn_uuid"]
    assert after["processed_turns"] == before["processed_turns"]


def test_a_dry_run_still_records_what_it_observed(turnless_env) -> None:
    """The other side of the guard, and the reason this build does not simply freeze the DB.

    Discovery writing `size_bytes`/`mtime_ns` is an observation of what is on disk. Guarding
    it would make a dry run forget its own findings -- the opposite of what a discovery pass
    is for, and a contradiction of AGENTS.md invariant 8. A fix that makes this test fail has
    gone too far.
    """
    cfg, store, target = turnless_env
    run_once(cfg, store, dry_run=True, now=1_000_000.0, provider_factory=lambda: Exploding())
    row = store.get(str(target))
    assert row is not None, "a dry run must still record that it saw the transcript"
    assert row.size_bytes == target.stat().st_size
    assert row.mtime_ns == target.stat().st_mtime_ns


# --------------------------------------------------------------------------------------
# Manifest, hook and frontmatter on the live path (vikunja#891)
# --------------------------------------------------------------------------------------


def _manifest(cfg) -> list[dict]:
    path = cfg.manifest_file()
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def _identity_holds(results) -> bool:
    t = summarize_run(results)
    return t["written"] == t["summarized"] + t["suppressed"] + t["placeholders"]


def _second_session(target: Path) -> Path:
    """The fixture again under a new session id and new record uuids: same agent, same day."""
    other = target.with_name("sess-two.jsonl")
    text = target.read_text().replace("sess-struct", "sess-two").replace('"uuid": "', '"uuid": "b-')
    other.write_text(text)
    os.utime(other, (1_000_000.0 - 3600, 1_000_000.0 - 3600))
    return other


def test_a_live_run_records_its_block_in_the_manifest(env) -> None:
    cfg, store, _t = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.manifest_appended is True and r.manifest_error == ""
    (line,) = _manifest(cfg)
    assert line["path"] == "research/2026-09-13.md"
    assert line["session_id"] == "sess-struct"
    assert line["provisional"] == ""
    assert line["eventlog_path"] == Path(r.eventlog_path).name
    assert summarize_run([r])["manifest_appended"] == 1


def test_a_dry_run_writes_no_manifest(env) -> None:
    cfg, store, _t = env
    run_once(cfg, store, dry_run=True, now=1_000_000.0, provider_factory=lambda: Exploding())
    assert not cfg.manifest_file().exists()


def test_a_placeholder_then_its_digest_give_two_lines_for_one_key(env) -> None:
    """Verification step 3, first shape, through the real sweep: the provisional block is
    replaced in place, so the second line has the SAME key and a different hash."""
    from scribe.manifest import check

    cfg, store, _t = env
    err = ProviderError("bad request", retryable=False)
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    first, second = _manifest(cfg)
    assert (first["path"], first["turn_uuid"]) == (second["path"], second["turn_uuid"])
    assert (first["provisional"], second["provisional"]) == ("placeholder", "")
    assert first["sha256"] != second["sha256"]
    assert check(cfg).clean


def test_two_sessions_on_one_day_give_one_path_and_two_keys(env) -> None:
    """Verification step 3, second shape."""
    from scribe.manifest import check

    cfg, store, target = env
    _second_session(target)
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert len(results) == 2
    a, b = _manifest(cfg)
    assert a["path"] == b["path"]
    assert a["turn_uuid"] != b["turn_uuid"]
    assert check(cfg).clean


def test_a_manifest_that_cannot_be_written_does_not_lose_the_digest(env, tmp_path) -> None:
    cfg, store, _t = env
    blocker = tmp_path / "manifest-is-a-directory"
    blocker.mkdir()
    cfg.manifest_path = str(blocker)
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.written is True and r.summarized is True
    assert r.manifest_appended is False and r.manifest_error
    assert r.errors == []  # its own counter, not an error string (invariant 13)
    totals = summarize_run([r])
    assert totals["manifest_errors"] == 1
    assert _identity_holds([r])


def _hook(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o700)
    return script


def test_the_hook_is_handed_the_digest_after_its_manifest_line(env, tmp_path) -> None:
    """Exactly once, with the finished file, and with the payload already in place.

    The hook APPENDS what it sees, so a second invocation -- or one that ran before the
    manifest line existed -- shows up as extra or missing lines rather than being overwritten
    by a later, correct call.
    """
    cfg, store, _t = env
    seen = tmp_path / "seen"
    cfg.on_digest_written = (
        str(_hook(tmp_path, f'echo "$1" >> {seen}\ncat {cfg.manifest_file()} >> {seen}\n')),
    )
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.hook == "ok" and r.hook_error == ""
    arg, manifest_line = seen.read_text().splitlines()
    assert Path(arg) == output_root(cfg) / "research" / "2026-09-13.md"
    assert json.loads(manifest_line)["path"] == "research/2026-09-13.md"
    assert summarize_run([r])["hook_ok"] == 1


def test_a_failing_hook_is_counted_and_cannot_break_the_run(env, tmp_path) -> None:
    cfg, store, _t = env
    cfg.on_digest_written = (str(_hook(tmp_path, 'echo "indexer said no" >&2\nexit 1\n')),)
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.written is True and r.summarized is True
    assert r.hook == "failed"
    assert r.hook_error == "exit 1: indexer said no"
    assert r.errors == []
    assert summarize_run([r])["hook_failed"] == 1
    assert _identity_holds([r])


def test_a_hung_hook_is_killed_with_its_children_at_the_timeout(env, tmp_path) -> None:
    """A grandchild holding stderr would stall `subprocess.run(timeout=)` past its timeout."""
    import time

    cfg, store, _t = env
    cfg.on_digest_written = (str(_hook(tmp_path, "sleep 30 &\nsleep 30\n")),)
    cfg.on_digest_written_timeout_seconds = 0.5
    start = time.monotonic()
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert time.monotonic() - start < 10
    assert r.hook == "failed" and "timed out" in r.hook_error
    assert r.written is True
    assert _identity_holds([r])


def test_a_hook_that_does_not_exist_is_a_failure_not_a_crash(env, tmp_path) -> None:
    cfg, store, _t = env
    cfg.on_digest_written = (str(tmp_path / "no-such-indexer"),)
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.hook == "failed" and "could not start" in r.hook_error
    assert r.written is True


def test_no_hook_configured_spawns_nothing(env, monkeypatch) -> None:
    import subprocess

    cfg, store, _t = env

    def boom(*a, **k):
        raise AssertionError("no hook is configured; nothing may be spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.hook == ""
    assert summarize_run([r])["hook_ok"] == summarize_run([r])["hook_failed"] == 0


def test_the_hook_runs_without_a_shell(env, tmp_path) -> None:
    """An argv element full of shell syntax is passed through literally, never interpreted."""
    cfg, store, _t = env
    seen = tmp_path / "seen"
    canary = tmp_path / "canary"
    cfg.on_digest_written = (
        str(_hook(tmp_path, f'printf "%s\\n" "$1" > {seen}\n')),
        f"; touch {canary}",
    )
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.hook == "ok"
    assert seen.read_text() == f"; touch {canary}\n"
    assert not canary.exists()


def test_emit_frontmatter_on_the_live_path(env) -> None:
    from scribe.manifest import check

    cfg, store, _t = env
    cfg.emit_frontmatter = True
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    (written,) = list(output_root(cfg).rglob("*.md"))
    text = written.read_text()
    assert text.startswith("---\n")
    assert 'session_ids: ["sess-struct"]' in text
    assert r.errors == []
    assert check(cfg).clean


def test_frontmatter_is_off_by_default(env) -> None:
    cfg, store, _t = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    (written,) = list(output_root(cfg).rglob("*.md"))
    assert written.read_text().startswith("# 2026-09-13")


def test_the_hook_does_not_inherit_the_summarizers_credential(env, tmp_path, monkeypatch):
    """SC-06: a new subprocess without `env=` inherits every secret the sweep holds."""
    cfg, store, _t = env
    monkeypatch.setenv("MISTRAL_API_KEY", "NOTREAL-summarizer-key")
    monkeypatch.setenv("MY_INDEXER_TOKEN", "NOTREAL-indexer-token")
    seen = tmp_path / "env"
    cfg.on_digest_written = (str(_hook(tmp_path, f"env > {seen}\n")),)
    cfg.on_digest_written_env = ("MY_INDEXER_TOKEN",)
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.hook == "ok"
    names = {ln.split("=", 1)[0] for ln in seen.read_text().splitlines() if "=" in ln}
    assert "MISTRAL_API_KEY" not in names
    assert "MY_INDEXER_TOKEN" in names
    assert "PATH" in names


def test_a_hook_that_leaves_a_worker_behind_succeeds_on_time(env, tmp_path) -> None:
    """The hook's outcome is its own exit status. A background worker holding stderr must not
    turn a clean exit into a reported timeout (CodeRabbit, PR #24)."""
    import time

    cfg, store, _t = env
    cfg.on_digest_written = (str(_hook(tmp_path, "sleep 20 &\nexit 0\n")),)
    cfg.on_digest_written_timeout_seconds = 5
    start = time.monotonic()
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.hook == "ok", r.hook_error
    assert time.monotonic() - start < 4


def test_a_hook_whose_worker_escapes_the_group_cannot_stall_the_sweep(env, tmp_path) -> None:
    """`setsid` puts the worker outside the process group, so `killpg` cannot reach it. With a
    stderr pipe, the sweep then waited out the worker's whole lifetime. The timeout must bound
    the sweep regardless of what the hook leaves behind."""
    import shutil
    import time

    if shutil.which("setsid") is None:
        pytest.skip("setsid not installed")
    cfg, store, _t = env
    cfg.on_digest_written = (str(_hook(tmp_path, "setsid sleep 20 &\nsleep 20\n")),)
    cfg.on_digest_written_timeout_seconds = 1
    start = time.monotonic()
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert r.hook == "failed" and "timed out" in r.hook_error
    assert time.monotonic() - start < 6
