"""The run record: one line per session a live sweep finished, never raising, never on a dry run.

What these pin, in the order the plan's verification lists them: a live run writes exactly one
record with every field; a dry run writes none; a failed write is counted and not raised; the
backfill is idempotent and attributes nothing it cannot observe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scribe import __version__, runrecord
from scribe.config import Config, ProviderConfig, StageConfig
from scribe.extract.models import EventLog
from scribe.extract.redact import REDACTED
from scribe.pipeline import output_root, run_once, summarize_run
from scribe.qc import Report
from scribe.state import Store
from scribe.summarize import prompt, schema
from scribe.summarize.providers import Completion, Provider

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = json.dumps(
    {
        "asked": "First real question about vikunja#843",
        "done": ["Created a branch with `git -C /repo checkout -b feat/thing 9f8e7d6`"],
        "tickets": ["#843"],
    }
)
#: `tickets` is a bounded field, so 500 of them against a one-ticket rollup is a non-retryable
#: schema violation: the session becomes a placeholder (same shape as test_pipeline's).
OVER_CAP = json.dumps(
    {"asked": "q", "done": ["Created a branch"], "tickets": [f"#{i}" for i in range(500)]}
)

#: Every key a live record carries. Pinned as a set so a field dropped by a refactor fails
#: here rather than as a hole in next month's trend.
LIVE_FIELDS = {
    "record_version",
    "ts",
    "source",
    "graded_by",
    "scribe_version",
    "prompt_sha256",
    "provider",
    "model_requested",
    "model_resolved",
    "agent",
    "session_id",
    "turn_uuid",
    "transcript_path",
    "digest",
    "session_at",
    "full_read",
    "replayed",
    "turns",
    "events",
    "extracted_chars",
    "token_estimate",
    "input_tokens",
    "output_tokens",
    "degradation_level",
    "written",
    "suppressed",
    "placeholder",
    "discarded",
    "truncated_items",
    "truncated_fields",
    "secrets_redacted",
    "post_render_redactions",
    "errors",
    "qc_ok",
    "qc_coverage",
    "events_referenced",
    "events_total",
    "findings_by_check",
    "findings_by_kind",
    "findings_by_bucket",
    "claims",
    "lag_s",
}


class Stub(Provider):
    name = "stub"
    model = "requested-model"

    def __init__(self, text: str = GOOD, resolved: str = "resolved-model-2026") -> None:
        self.text = text
        self.resolved = resolved

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        return Completion(
            text=self.text,
            input_tokens=50,
            output_tokens=10,
            model=self.resolved or self.model,
            provider="stub",
            model_resolved=self.resolved,
        )


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-research"
    d.mkdir(parents=True)
    target = d / "sess.jsonl"
    target.write_bytes((FIXTURES / "transcript-structural.jsonl").read_bytes())
    old = 1_000_000.0 - 3600
    os.utime(target, (old, old))
    cfg = Config(
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
        qc_run_record=str(tmp_path / "runs.jsonl"),
        providers={"stub": ProviderConfig(name="stub", type="openai-compatible", model="m")},
        stages={"session": StageConfig(provider="stub", model="m")},
    )
    return cfg, Store(cfg.state_path)


def _records(cfg: Config) -> list[dict]:
    return runrecord.read(cfg.qc_run_record).records


def test_a_live_run_writes_exactly_one_record_per_session_with_every_field(env) -> None:
    cfg, store = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    (rec,) = _records(cfg)

    assert set(rec) == LIVE_FIELDS
    assert rec["source"] == "live"
    assert rec["scribe_version"] == rec["graded_by"] == __version__
    assert rec["prompt_sha256"] == prompt.prompt_sha256()
    assert rec["model_requested"] == "requested-model"
    assert rec["model_resolved"] == "resolved-model-2026"
    assert rec["session_id"] == r.session_id and rec["agent"] == r.agent
    assert rec["qc_ok"] is r.qc_ok and rec["qc_coverage"] == round(r.qc_coverage, 4)
    assert rec["written"] is True and rec["placeholder"] is False
    assert rec["digest"] == str(next(output_root(cfg).rglob("*.md")).relative_to(output_root(cfg)))
    assert isinstance(rec["lag_s"], float)


def test_the_record_file_is_owner_only(env) -> None:
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert Path(cfg.qc_run_record).stat().st_mode & 0o777 == 0o600


def test_a_second_sweep_with_nothing_new_appends_nothing(env) -> None:
    """One line per session FINISHED -- a sweep that re-offers nothing records nothing."""
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert len(_records(cfg)) == 1


def test_a_dry_run_writes_no_record(env) -> None:
    cfg, store = env
    run_once(cfg, store, dry_run=True, now=1_000_000.0)
    assert not Path(cfg.qc_run_record).exists()


def test_an_empty_run_record_path_turns_the_record_off(env) -> None:
    cfg, store = env
    cfg.qc_run_record = ""
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert r.written and not r.run_record_error
    assert not list(Path(cfg.state_path).parent.glob("runs.jsonl"))


def test_a_failed_write_is_counted_not_raised_and_is_not_an_error(env, tmp_path) -> None:
    """Invariant 13: own field, own total. The digest is on disk; only its record is missing."""
    cfg, store = env
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    cfg.qc_run_record = str(blocker / "runs.jsonl")

    results = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    (r,) = results
    assert r.written is True
    assert r.run_record_error
    assert not any("run record" in e for e in r.errors)
    totals = summarize_run(results)
    assert totals["run_record_errors"] == 1
    assert totals["errors"] == 0


def test_a_classifier_crash_is_counted_and_the_digest_still_lands(env, monkeypatch) -> None:
    cfg, store = env

    def boom(*a, **k):
        raise RuntimeError("classifier broke")

    monkeypatch.setattr(runrecord, "qc_fields", boom)
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert r.written is True
    assert "classifier broke" in r.run_record_error
    assert not Path(cfg.qc_run_record).exists()


def test_a_placeholder_is_recorded_and_marked(env) -> None:
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(OVER_CAP))
    (rec,) = _records(cfg)
    assert rec["placeholder"] is True
    assert rec["qc_ok"] is not None, "graded, as the sweep grades it -- excluded at report time"


def test_model_resolved_is_null_when_the_provider_named_none(env) -> None:
    """Record what the API returned. Never the requested name standing in for it."""
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(resolved=""))
    (rec,) = _records(cfg)
    assert rec["model_resolved"] is None
    assert rec["model_requested"] == "requested-model"


def test_the_live_json_report_carries_the_record_error_field(env) -> None:
    cfg, store = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert r.to_dict()["run_record_error"] == ""


# --- qc_fields ------------------------------------------------------------------------


def _log() -> EventLog:
    return EventLog(session_id="s1", transcript_path="/t/s1.jsonl")


def test_findings_are_counted_by_check_kind_and_bucket() -> None:
    report = Report()
    report.add("groundedness", "d", kind="path", claim="/nowhere/at/all.py")
    report.add("groundedness", "d", kind="ticket", claim="#999")
    report.add("event_coverage", "d")
    qc = runrecord.qc_fields(report, "body", _log())
    assert qc["qc_ok"] is False
    assert qc["findings_by_check"] == {"groundedness": 2, "event_coverage": 1}
    assert qc["findings_by_kind"] == {"path": 1, "ticket": 1}
    assert qc["findings_by_bucket"] == {"path": {"absent": 1}, "ticket": {"absent": 1}}
    assert runrecord.absent_count(qc) == 2


def test_claims_are_capped_and_pass_through_the_redactor() -> None:
    report = Report()
    report.add("groundedness", "d", kind="identifier", claim="ghp_FAKE1234567890abcdefgh")
    for n in range(40):
        report.add("groundedness", "d", kind="identifier", claim=f"made_up_name_{n}")
    qc = runrecord.qc_fields(report, "body", _log())
    assert len(qc["claims"]) == runrecord.MAX_CLAIMS
    assert qc["findings_by_kind"] == {"identifier": 41}, "the counts are complete"
    assert "ghp_FAKE" not in json.dumps(qc)
    assert REDACTED in qc["claims"][0]["claim"]


# --- prompt identity ------------------------------------------------------------------


@pytest.fixture
def fresh_hash():
    prompt.prompt_sha256.cache_clear()
    yield
    prompt.prompt_sha256.cache_clear()


def test_the_prompt_hash_is_stable(fresh_hash) -> None:
    first = prompt.prompt_sha256()
    prompt.prompt_sha256.cache_clear()
    assert prompt.prompt_sha256() == first
    assert len(first) == 64


def test_a_system_prompt_change_is_a_new_prompt_identity(fresh_hash, monkeypatch) -> None:
    before = prompt.prompt_sha256()
    prompt.prompt_sha256.cache_clear()
    monkeypatch.setattr(prompt, "_SYSTEM_TEMPLATE", prompt._SYSTEM_TEMPLATE + " Be brief.")
    assert prompt.prompt_sha256() != before


def test_a_cap_rule_change_is_a_new_prompt_identity(fresh_hash, monkeypatch) -> None:
    """Handoff question 2: the caps shape output, so a cap change must not read as drift."""
    before = prompt.prompt_sha256()
    prompt.prompt_sha256.cache_clear()
    monkeypatch.setattr(schema, "MAX_DERIVED_MULTIPLE", schema.MAX_DERIVED_MULTIPLE + 1)
    assert prompt.prompt_sha256() != before


# --- read -----------------------------------------------------------------------------


def test_a_torn_line_is_counted_not_fatal(tmp_path) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n{"torn": \n[1, 2]\n')
    loaded = runrecord.read(path)
    assert loaded.records == [{"a": 1}, {"b": 2}]
    assert (loaded.lines, loaded.bad_lines) == (4, 2)


def test_a_missing_record_reads_as_empty(tmp_path) -> None:
    loaded = runrecord.read(tmp_path / "absent.jsonl")
    assert (loaded.records, loaded.lines) == ([], 0)


def test_append_never_raises(tmp_path) -> None:
    blocker = tmp_path / "f"
    blocker.write_text("x")
    assert runrecord.append(blocker / "runs.jsonl", {"a": 1})
    assert runrecord.append(tmp_path / "ok.jsonl", {"bad": object()})


# --- backfill -------------------------------------------------------------------------


def test_backfill_grades_every_block_once_and_is_idempotent(env) -> None:
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    record = Path(cfg.state_path).parent / "baseline.jsonl"

    first = runrecord.backfill(record, cfg.output_dir, cfg.eventlog_dir)
    assert (first.blocks, first.appended, first.skipped) == (1, 1, 0)
    second = runrecord.backfill(record, cfg.output_dir, cfg.eventlog_dir)
    assert (second.blocks, second.appended, second.skipped) == (1, 0, 1)
    assert len(runrecord.read(record).records) == 1


def test_backfill_skips_a_block_a_live_sweep_already_recorded(env) -> None:
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    stats = runrecord.backfill(cfg.qc_run_record, cfg.output_dir, cfg.eventlog_dir)
    assert (stats.appended, stats.skipped) == (0, 1)
    assert len(_records(cfg)) == 1


def test_backfill_attributes_nothing_it_cannot_observe(env) -> None:
    cfg, store = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    record = Path(cfg.state_path).parent / "baseline.jsonl"
    runrecord.backfill(record, cfg.output_dir, cfg.eventlog_dir)
    (rec,) = runrecord.read(record).records
    assert rec["source"] == "backfill"
    for key in ("scribe_version", "prompt_sha256", "model_resolved", "model_requested"):
        assert rec[key] is None, key
    assert rec["graded_by"] == __version__
    # Graded the same way the live path graded it.
    assert rec["qc_ok"] is r.qc_ok
    assert rec["qc_coverage"] == round(r.qc_coverage, 4)


def test_backfill_counts_a_block_whose_event_log_is_gone(env) -> None:
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    for f in Path(cfg.eventlog_dir).iterdir():
        f.unlink()
    stats = runrecord.backfill(
        Path(cfg.state_path).parent / "b.jsonl", cfg.output_dir, cfg.eventlog_dir
    )
    assert (stats.blocks, stats.no_log, stats.appended) == (1, 1, 0)
