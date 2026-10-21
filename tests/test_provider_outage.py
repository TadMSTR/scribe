"""A provider outage leaves sessions pending, stops the sweep early, and exits loudly.

#1188 (id 1271) item 4. Before this, a Mistral outage turned each pending session into a
placeholder and spent one of its `MAX_PROVISIONAL_ATTEMPTS`; with hourly sweeps, 2-3 hours of
outage retired every session until a manual `scribe recover --apply`. And `scribe run` exited 0
throughout, so the cron's Healthchecks ping stayed green while every session made its own 3
calls against a provider that was down.

Backoff is real `time.sleep` in production. Here `summarize_log` is wrapped with an injected
no-op `sleep` so the retry path runs at full fidelity without spending wall-clock.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

import pytest

from scribe import pipeline
from scribe.config import Config, ProviderConfig, StageConfig
from scribe.pipeline import output_root, run_once, summarize_run
from scribe.run_cli import EXIT_PROVIDER_UNAVAILABLE
from scribe.run_cli import main as run_main
from scribe.state import MAX_PROVISIONAL_ATTEMPTS, STATUS_PROVISIONAL, Store
from scribe.summarize.providers import Completion, Provider, ProviderError
from scribe.summarize.runner import summarize_log

FIXTURES = Path(__file__).parent / "fixtures"
#: Three transcripts with distinct session ids and turn uuids, so a healthy sweep writes three
#: distinct blocks rather than refusing two as duplicates.
TRANSCRIPTS = ("structural", "past-dated", "compact-boundary")
GOOD = json.dumps({"asked": "A question", "done": ["Did a thing"]})


def outage() -> ProviderError:
    return ProviderError("stub: server error 503", retryable=True, status=503, unavailable=True)


class Stub(Provider):
    """Plays `script` in order, then answers GOOD. Counts every call it receives."""

    name = "stub"
    model = "m"

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.calls = 0

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        self.calls += 1
        item = self.script.pop(0) if self.script else GOOD
        if isinstance(item, Exception):
            raise item
        return Completion(text=item, input_tokens=1, output_tokens=1, provider="stub")


class Down(Stub):
    """Every call fails as an outage."""

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        self.calls += 1
        raise outage()


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(
        pipeline, "summarize_log", functools.partial(summarize_log, sleep=lambda _s: None)
    )


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-research"
    d.mkdir(parents=True)
    paths = []
    for name in TRANSCRIPTS:
        target = d / f"{name}.jsonl"
        target.write_bytes((FIXTURES / f"transcript-{name}.jsonl").read_bytes())
        old = 1_000_000.0 - 3600
        os.utime(target, (old, old))
        paths.append(target)
    cfg = Config(
        quiet_period_minutes=15,
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
        qc_run_record="",
        providers={"stub": ProviderConfig(name="stub", type="openai-compatible", model="m")},
        stages={"session": StageConfig(provider="stub", model="m")},
    )
    return cfg, Store(cfg.state_path), paths


def _sweep(cfg, store, provider):
    return run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: provider)


def test_an_outage_is_confirmed_by_a_second_session_then_skips_the_rest(env) -> None:
    cfg, store, _paths = env
    down = Down()
    results = _sweep(cfg, store, down)

    assert len(results) == 3
    totals = summarize_run(results)
    assert totals["deferred"] == 2
    assert totals["not_attempted"] == 1
    # The breaker: two sessions' 3 attempts each -- the suspect and its confirming probe --
    # and nothing after them.
    assert down.calls == 6
    # No block of any kind, placeholder included.
    assert not list(output_root(cfg).rglob("*.md"))
    assert totals["placeholders"] == 0
    assert totals["written"] == 0


def test_an_outage_touches_no_session_state(env) -> None:
    cfg, store, paths = env
    _sweep(cfg, store, Down())
    for p in paths:
        row = store.get(str(p))
        assert row.attempts == 0
        assert row.last_offset == 0
        assert row.status != STATUS_PROVISIONAL
        assert row.has_unread_bytes


def test_the_next_healthy_sweep_summarizes_everything_with_no_budget_spent(env) -> None:
    cfg, store, paths = env
    _sweep(cfg, store, Down())
    healthy = Stub()
    results = _sweep(cfg, store, healthy)

    assert len(results) == 3
    assert all(r.summarized and r.written for r in results)
    assert healthy.calls == 3
    for p in paths:
        row = store.get(str(p))
        assert row.status == "summarized"
        assert row.attempts == 0


def test_an_outage_mid_sweep_keeps_the_sessions_already_done(env) -> None:
    """The breaker trips on the sessions that found the provider down, not before them."""
    cfg, store, _paths = env
    results = _sweep(cfg, store, Stub(GOOD, *[outage()] * 6))
    assert [r.summarized for r in results] == [True, False, False]
    assert [r.deferred for r in results] == [False, True, True]
    assert len(list(output_root(cfg).rglob("*.md"))) == 1


def _order(cfg, store) -> list[str]:
    """The session ids in the order a sweep offers them. A dry run observes and processes
    nothing (invariant 8), so asking does not change what the live sweep sees."""
    return [r.session_id for r in run_once(cfg, store, dry_run=True, now=1_000_000.0)]


class Poison(Stub):
    """Up for everyone except one session, whose request always fails as if it were down --
    the shape a session-specific fault takes once it is misread as an outage (audit F-01)."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        if self.session_id in user:
            self.calls += 1
            raise outage()
        return super().complete(system, user, timeout=timeout)


def test_one_poison_session_cannot_starve_the_sessions_behind_it(env) -> None:
    """The deferral is a suspicion until the next session confirms it. Here the provider
    answers the next session, so the suspect is re-run onto the provisional path, where its
    retry budget bounds it, and the sessions behind it are summarized."""
    cfg, store, _paths = env
    first, *rest = _order(cfg, store)
    results = _sweep(cfg, store, Poison(first))
    by_id = {r.session_id: r for r in results}

    assert summarize_run(results)["deferred"] == 0
    assert summarize_run(results)["not_attempted"] == 0
    assert by_id[first].placeholder is True
    assert all(by_id[s].summarized for s in rest)
    poisoned = store.get(by_id[first].transcript_path)
    assert poisoned.status == STATUS_PROVISIONAL
    assert poisoned.attempts == 1


def test_a_poison_session_is_retired_by_its_budget_not_retried_forever(env) -> None:
    cfg, store, _paths = env
    first, *_rest = _order(cfg, store)
    for _ in range(MAX_PROVISIONAL_ATTEMPTS):
        _sweep(cfg, store, Poison(first))
    assert [r.session_id for r in _sweep(cfg, store, Poison(first))] == []


def test_a_lone_surrogate_in_a_transcript_does_not_stop_the_sweep(env) -> None:
    """Audit F-01/F-03, reproduced: valid JSON carrying `\\ud83d` with no low half used to
    crash `write_eventlog` on every sweep (exit 1) and, once that was caught, made httpx fail
    to encode the request -- which read as an outage."""
    cfg, store, paths = env
    target = paths[0]
    text = target.read_text().replace("First real question", "First \\ud83d question", 1)
    assert "\\ud83d" in text
    target.write_text(text)
    # Back past the quiet period: the rewrite moved the mtime to now, and a session that is
    # not idle is not offered at all -- which made the first version of this test pass vacuously.
    old = 1_000_000.0 - 3600
    os.utime(target, (old, old))
    results = _sweep(cfg, store, Stub())
    assert str(target) in [r.transcript_path for r in results]
    assert all(r.summarized for r in results), [r.errors for r in results]
    assert all(r.eventlog_path for r in results), [r.errors for r in results]


def test_a_provider_that_recovers_within_the_retries_is_not_an_outage(env) -> None:
    """Deferral is decided by how the run ENDED. A 503 then a good answer is a digest."""
    cfg, store, _paths = env
    results = _sweep(cfg, store, Stub(outage(), GOOD))
    assert summarize_run(results)["deferred"] == 0
    assert all(r.summarized for r in results)


def test_an_unparseable_envelope_still_spends_the_budget(env) -> None:
    """Retryable, but not an outage -- it can be determined by the input, so it stays on the
    provisional path. Deferring it would re-offer such a session every sweep forever."""
    cfg, store, _paths = env
    bad = ProviderError("stub: unparseable response envelope: 'choices'", retryable=True)
    results = _sweep(cfg, store, Stub(bad, bad, bad))
    first = results[0]
    assert first.deferred is False
    assert first.placeholder is True
    assert summarize_run(results)["not_attempted"] == 0
    row = store.get(first.transcript_path)
    assert row.status == STATUS_PROVISIONAL
    assert row.attempts == 1


def test_a_non_retryable_rejection_still_spends_the_budget(env) -> None:
    cfg, store, _paths = env
    rejected = ProviderError("stub: request rejected with 400", retryable=False, status=400)
    results = _sweep(cfg, store, Stub(rejected))
    first = results[0]
    assert first.deferred is False
    assert first.placeholder is True
    assert store.get(first.transcript_path).attempts == 1


def test_an_orphaned_session_stays_offered_through_an_outage(env) -> None:
    """A session whose transcript is gone is reached by `orphaned`, not `scan`. Deferral must
    leave it there: a status write would take it out of the set `orphaned` reads."""
    cfg, store, paths = env
    rejected = ProviderError("stub: request rejected with 400", retryable=False, status=400)
    # One provisional session, attempts 1, its event log persisted.
    _sweep(cfg, store, Stub(rejected))
    provisional = next(p for p in paths if store.get(str(p)).status == STATUS_PROVISIONAL)
    for p in paths:
        if p != provisional:
            store.mark_summarized(str(p), offset=10**9, last_turn_uuid="", turn_uuids=[])
    provisional.unlink()

    (deferred,) = _sweep(cfg, store, Down())
    assert deferred.transcript_path == str(provisional)
    assert deferred.deferred is True
    assert store.get(str(provisional)).attempts == 1

    (again,) = _sweep(cfg, store, Stub())
    assert again.replayed is True
    assert again.summarized is True


def test_a_dry_run_never_defers(env) -> None:
    cfg, store, _paths = env
    results = run_once(cfg, store, dry_run=True, now=1_000_000.0, provider_factory=Down)
    assert summarize_run(results)["deferred"] == 0


# --- the exit code ---------------------------------------------------------------------

CONFIG = """
[extract]
quiet_period_minutes = 15

[discovery]
project_globs = ["{globs}"]
output_dir    = "{out}"
state_path    = "{state}"

[qc]
run_record = ""

[summarize.session]
provider = "stub"
model    = "m"

[providers.stub]
type     = "openai-compatible"
base_url = "https://api.example.test/v1"
"""


@pytest.fixture
def cli(env, tmp_path, monkeypatch):
    cfg, _store, _paths = env
    toml = tmp_path / "scribe.toml"
    toml.write_text(
        CONFIG.format(globs=cfg.project_globs[0], out=cfg.output_dir, state=cfg.state_path)
    )

    def use(provider):
        real = pipeline.run_once

        def patched(*a, **k):
            k["provider_factory"] = lambda: provider
            k["now"] = 1_000_000.0
            return real(*a, **k)

        monkeypatch.setattr("scribe.run_cli.run_once", patched)

    return toml, use


def test_scribe_run_exits_with_the_documented_code_on_an_outage(cli, capsys) -> None:
    toml, with_provider = cli
    with_provider(Down())
    assert run_main(["--config", str(toml), "--live"]) == EXIT_PROVIDER_UNAVAILABLE
    out = capsys.readouterr().out
    assert "!! provider unavailable: 2 deferred, 1 not attempted" in out


def test_the_exit_code_holds_in_json_mode(cli, capsys) -> None:
    toml, with_provider = cli
    with_provider(Down())
    assert run_main(["--config", str(toml), "--live", "--json"]) == EXIT_PROVIDER_UNAVAILABLE
    totals = json.loads(capsys.readouterr().out)["totals"]
    assert (totals["deferred"], totals["not_attempted"]) == (2, 1)


def test_a_healthy_sweep_still_exits_zero(cli) -> None:
    toml, with_provider = cli
    with_provider(Stub())
    assert run_main(["--config", str(toml), "--live"]) == 0


def test_the_exit_code_is_clear_of_every_code_already_in_use() -> None:
    """0 ok, 1 an uncaught crash, 2 usage/config, 75 the cron's `flock -E 75` skip. A collision
    with 75 would read as "previous sweep still running" and be reported as ok."""
    assert EXIT_PROVIDER_UNAVAILABLE not in (0, 1, 2, 75)


def _last(cfg, store) -> str:
    return _order(cfg, store)[-1]


def test_a_suspect_with_nothing_after_it_is_cleared_by_an_earlier_answer(env) -> None:
    """Audit R2-01, reproduced: the poison session is LAST, so no later session can probe it.
    The provider answered the two before it, which is evidence enough."""
    cfg, store, _paths = env
    last = _last(cfg, store)
    results = _sweep(cfg, store, Poison(last))
    by_id = {r.session_id: r for r in results}
    assert summarize_run(results)["deferred"] == 0
    assert by_id[last].placeholder is True
    assert store.get(by_id[last].transcript_path).attempts == 1


def test_a_lone_poison_session_is_retired_by_its_budget(env) -> None:
    """After the first sweep the healthy sessions are done, and the poison one is often the
    only ready row: nothing in the sweep can answer, so it stays deferred for that sweep --
    but the first sweep already moved it onto the provisional path."""
    cfg, store, _paths = env
    last = _last(cfg, store)
    for _ in range(MAX_PROVISIONAL_ATTEMPTS + 1):
        _sweep(cfg, store, Poison(last))
    assert _sweep(cfg, store, Poison(last)) == []


def test_a_genuine_outage_with_one_session_left_stays_deferred(env) -> None:
    """Nothing answered at all, so a single remaining deferral is not re-run: an outage must
    not spend budget just because it reached the last session."""
    cfg, store, paths = env
    _order(cfg, store)  # a dry run creates the rows; it processes nothing (invariant 8)
    for p in paths[1:]:
        store.mark_summarized(str(p), offset=10**9, last_turn_uuid="", turn_uuids=[])
    (only,) = _sweep(cfg, store, Down())
    assert only.deferred is True
    assert store.get(only.transcript_path).attempts == 0
