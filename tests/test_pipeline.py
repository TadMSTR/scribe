"""End-to-end pipeline tests.

The property these are mostly about: a dry run must be genuinely inert. Phase 6 is a shadow
run and the live path must not change, so "dry" has to mean no provider call, no credential
read, and no write — not merely "no crash".
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.config import Config, ProviderConfig, StageConfig
from scribe.pipeline import output_root, process_session, run_once, summarize_run
from scribe.state import STATUS_FAILED, STATUS_SUMMARIZED, Store
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
    cfg, store, _t = env
    run_once(cfg, store, dry_run=True, now=1_000_000.0)
    assert not output_root(cfg).exists()


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
    assert store.get(str(target)).status == STATUS_FAILED


def test_a_failed_session_is_retried_on_the_next_sweep(env) -> None:
    cfg, store, _t = env
    err = ProviderError("bad request", retryable=False)
    run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub(err))
    again = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Stub())
    assert len(again) == 1
    assert again[0].summarized is True


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
