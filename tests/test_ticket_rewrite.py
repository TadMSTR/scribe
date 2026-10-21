"""End to end: an id-conflated `#N` is graded as the model wrote it and written as `id N`.

#1188 (id 1271) item 2a. The unit tests in `test_qc.py` pin what the rewrite changes; these pin
WHERE it sits in the pipeline, which is the half that is easy to get wrong silently. Grading
the rewritten text would make the `id-conflation` bucket read zero whatever the model did, and
the prompt change that teaches `#N (id M)` could never be evaluated.

The transcript is `transcript-qc-regression.jsonl`, whose log holds `417` only as an id
(`/tasks/417`, `"id":417`) and holds `#655` as written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scribe import runrecord
from scribe.config import Config, ProviderConfig, StageConfig
from scribe.pipeline import output_root, run_once, summarize_run
from scribe.state import Store
from scribe.summarize.providers import Completion, Provider

FIXTURES = Path(__file__).parent / "fixtures"
CONFLATED = json.dumps(
    {
        "asked": "Check the queue entry for id 417 and summarise the upstream PRs.",
        "done": ["Read `/srv/app/src/tools/queue.py`"],
        "tickets": ["#417", "#655"],
    }
)


class Stub(Provider):
    name = "stub"
    model = "m"

    def __init__(self, text: str = CONFLATED) -> None:
        self.text = text

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        return Completion(text=self.text, input_tokens=1, output_tokens=1, provider="stub")


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-research"
    d.mkdir(parents=True)
    target = d / "sess.jsonl"
    target.write_bytes((FIXTURES / "transcript-qc-regression.jsonl").read_bytes())
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


def _digest(cfg: Config) -> str:
    (path,) = list(output_root(cfg).rglob("*.md"))
    return path.read_text()


def test_the_conflated_ref_reaches_disk_as_an_id(env) -> None:
    cfg, store = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    text = _digest(cfg)
    assert "id 417" in text
    assert "#417" not in text
    assert r.ticket_refs_rewritten == 1


def test_a_grounded_ref_in_the_same_digest_is_untouched(env) -> None:
    cfg, store = env
    run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert "#655" in _digest(cfg)


def test_qc_grades_the_models_output_not_the_rewrite(env) -> None:
    """Grade first, rewrite second. The finding and the bucket say what the MODEL wrote."""
    cfg, store = env
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert r.qc_ok is False
    assert any("'#417'" in f and "internal id" in f for f in r.qc_findings)
    (rec,) = runrecord.read(cfg.qc_run_record).records
    assert rec["findings_by_bucket"]["ticket"] == {"id-conflation": 1}
    assert rec["ticket_refs_rewritten"] == 1


def test_the_rewrite_is_counted_in_the_sweep_totals(env) -> None:
    cfg, store = env
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert summarize_run(results)["ticket_refs_rewritten"] == 1


def test_with_the_key_off_nothing_is_rewritten(env) -> None:
    cfg, store = env
    cfg.qc_rewrite_id_conflations = False
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert "#417" in _digest(cfg)
    assert r.ticket_refs_rewritten == 0
    (rec,) = runrecord.read(cfg.qc_run_record).records
    assert rec["ticket_refs_rewritten"] == 0
    # The gate is unaffected by the switch: it still reports the conflation.
    assert rec["findings_by_bucket"]["ticket"] == {"id-conflation": 1}
