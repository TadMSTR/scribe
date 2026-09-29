"""The test suite must not write to the operator's home directory (vikunja#851).

These assert against the **real** `~`, captured in `conftest` before the autouse fixture
redirects it. That is the distinction that matters: a test checking only that the tmp file
got written cannot tell isolation-working from isolation-removed, because both write the tmp
file. Naming the real path is what makes these go red when the fixture goes away.

#851 measured the consequence of not having them: 1,182 phantom `stub-1` records in
`~/logs/scribe-tokens.log`, 99.1% of the file, +20 per `pytest` run — well-formed records
lying in wait for the day the spend meter is pointed at that path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scribe.config import (
    DEFAULT_EVENTLOG_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_RUN_RECORD,
    Config,
    ProviderConfig,
    StageConfig,
)
from scribe.pipeline import run_once
from scribe.state import Store
from scribe.summarize.providers import Completion, Provider
from scribe.telemetry import DEFAULT_SPEND_LOG, record_spend

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = json.dumps({"asked": "A question", "done": ["Did a thing"], "tickets": ["#851"]})


class Stub(Provider):
    name = "stub"

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        return Completion(
            text=GOOD, input_tokens=50, output_tokens=10, model="stub-1", provider="stub"
        )


def _snapshot(path: Path) -> tuple[bool, int]:
    """Existence and size — enough to see an append, and safe when the file is absent."""
    try:
        return True, path.stat().st_size
    except OSError:
        return False, -1


def _default_config(tmp_path: Path) -> Config:
    """A `Config` with **no path overrides** — the shape of call #851 describes.

    Only the corpus location is set, because a test has to point somewhere; every output
    path is left on its default so that a missing guard shows up as a write to the real home.
    """
    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-research"
    d.mkdir(parents=True)
    target = d / "sess.jsonl"
    target.write_bytes((FIXTURES / "transcript-structural.jsonl").read_bytes())
    old = 1_000_000.0 - 3600
    os.utime(target, (old, old))
    return Config(
        project_globs=(str(projects / "*") + "/",),
        providers={"stub": ProviderConfig(name="stub", type="openai-compatible", model="m")},
        stages={"session": StageConfig(provider="stub", model="m")},
    )


def test_recording_spend_with_no_log_path_does_not_touch_the_real_log(real_spend_log) -> None:
    """The narrowest form of #851: the implicit `DEFAULT_SPEND_LOG` fallback."""
    before = _snapshot(real_spend_log)
    record_spend(model="stub-1", input_tokens=1, output_tokens=1)
    assert _snapshot(real_spend_log) == before


def test_a_live_run_on_pure_defaults_writes_nothing_under_the_real_home(
    tmp_path, real_home, real_spend_log
) -> None:
    """A full sweep — spend log, event logs, digests and state DB, all on defaults.

    A *live* run deliberately, not a dry one: the dry run writes nothing by contract, so it
    could not detect a missing guard on the two paths a live run creates.
    """
    before = _snapshot(real_spend_log)
    real_eventlogs = real_home / Path(DEFAULT_EVENTLOG_DIR).relative_to("~")
    real_digests = real_home / Path(DEFAULT_OUTPUT_DIR).relative_to("~")
    eventlogs_before, digests_before = real_eventlogs.exists(), real_digests.exists()

    cfg = _default_config(tmp_path)
    (r,) = run_once(cfg, Store(cfg.state_path), now=1_000_000.0, provider_factory=Stub)

    assert r.written and r.eventlog_path, "precondition: this run produced real output"
    assert _snapshot(real_spend_log) == before
    assert real_eventlogs.exists() == eventlogs_before
    assert real_digests.exists() == digests_before


def test_that_output_landed_in_the_redirected_home_rather_than_nowhere(
    tmp_path, isolate_home
) -> None:
    """The pair. Without it, a pipeline that silently wrote nothing would pass the above.

    "Nothing reached the real home" and "nothing was written at all" are the same
    observation from outside; this is what separates them.
    """
    cfg = _default_config(tmp_path)
    run_once(cfg, Store(cfg.state_path), now=1_000_000.0, provider_factory=Stub)

    fake_eventlogs = isolate_home / Path(DEFAULT_EVENTLOG_DIR).relative_to("~")
    fake_digests = isolate_home / Path(DEFAULT_OUTPUT_DIR).relative_to("~")
    assert list(fake_eventlogs.glob("*.json")), "the event log went somewhere else entirely"
    assert list(fake_digests.rglob("*.md")), "the digest went somewhere else entirely"


def test_the_production_defaults_really_are_under_the_home_directory() -> None:
    """The reason the guards above are not vacuous.

    If these defaults ever stopped being `~`-rooted, the tests above would pass for the
    wrong reason — nothing writes to a home path because nothing points at one any more.
    This pins the guard to the thing it guards so the two cannot drift apart.
    """
    for default in (DEFAULT_SPEND_LOG, DEFAULT_EVENTLOG_DIR, DEFAULT_OUTPUT_DIR):
        assert default.startswith("~/"), default


def test_the_fixture_actually_moves_home(isolate_home, real_home) -> None:
    """The mechanism, asserted once so a silently no-op fixture is visible."""
    assert Path(os.path.expanduser("~")) == isolate_home
    assert isolate_home != real_home


def test_a_live_run_on_defaults_writes_no_run_record_under_the_real_home(
    tmp_path, real_home, isolate_home
) -> None:
    """The run record is a production file on the #851 pattern: a default under `~`, appended
    by every live sweep. So the same pair of assertions -- the real one untouched, the
    redirected one written -- and on a LIVE run, the only kind that writes it."""
    real_record = real_home / Path(DEFAULT_RUN_RECORD).relative_to("~")
    before = _snapshot(real_record)

    cfg = _default_config(tmp_path)
    (r,) = run_once(cfg, Store(cfg.state_path), now=1_000_000.0, provider_factory=Stub)

    assert r.written and not r.run_record_error
    assert _snapshot(real_record) == before
    redirected = isolate_home / Path(DEFAULT_RUN_RECORD).relative_to("~")
    assert len(redirected.read_text().splitlines()) == 1
