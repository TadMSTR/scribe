"""Tests for spend metering and optional OTel spans.

The spend record's shape is asserted against the keys `memsearch-spend.sh` actually reads,
because that is the whole point of the format: `timestamp`, `model`, `input_tokens`,
`output_tokens`, and `event_kind` or `event`.
"""

from __future__ import annotations

import json
import sys

import pytest

from scribe import telemetry
from scribe.telemetry import (
    SPAN_REJECTED,
    SPAN_SUMMARIZE,
    record_spend,
    set_span_attributes,
    span,
)


def _read(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_record_carries_every_field_the_meter_reads(tmp_path) -> None:
    log = tmp_path / "tokens.log"
    record_spend(
        model="mistral-small-latest",
        input_tokens=100,
        output_tokens=20,
        session_id="s1",
        provider="mistral",
        log_path=log,
    )
    (rec,) = _read(log)
    for key in ("timestamp", "model", "input_tokens", "output_tokens", "event_kind"):
        assert key in rec, f"memsearch-spend.sh reads {key!r}"
    assert rec["input_tokens"] == 100
    assert rec["event_kind"] == "summarize"


def test_the_timestamp_parses_the_way_the_meter_expects(tmp_path) -> None:
    """The meter does `datetime.fromisoformat` after stripping a trailing Z."""
    from datetime import datetime

    log = tmp_path / "tokens.log"
    record_spend(model="m", input_tokens=1, output_tokens=1, log_path=log)
    ts = _read(log)[0]["timestamp"]
    assert ts.endswith("Z")
    assert datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_a_record_is_one_json_object_per_line(tmp_path) -> None:
    """The meter finds the first `{` on a line and parses from there."""
    log = tmp_path / "tokens.log"
    for i in range(3):
        record_spend(model="m", input_tokens=i, output_tokens=i, log_path=log)
    lines = log.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        assert line.lstrip().startswith("{")
        json.loads(line[line.find("{") :])


def test_records_append_rather_than_overwrite(tmp_path) -> None:
    log = tmp_path / "tokens.log"
    record_spend(model="a", input_tokens=1, output_tokens=1, log_path=log)
    record_spend(model="b", input_tokens=2, output_tokens=2, log_path=log)
    assert [r["model"] for r in _read(log)] == ["a", "b"]


def test_the_parent_directory_is_created(tmp_path) -> None:
    log = tmp_path / "deep" / "nested" / "tokens.log"
    record_spend(model="m", input_tokens=1, output_tokens=1, log_path=log)
    assert log.exists()


def test_a_metering_failure_never_raises(tmp_path) -> None:
    """A meter that can take down a summarization run is worse than no meter — the point of
    it is to notice an outage, not to cause one."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    record_spend(model="m", input_tokens=1, output_tokens=1, log_path=blocked / "x.log")


def test_the_event_kind_is_settable(tmp_path) -> None:
    log = tmp_path / "tokens.log"
    record_spend(model="m", input_tokens=1, output_tokens=1, event="daily", log_path=log)
    assert _read(log)[0]["event_kind"] == "daily"


def test_span_names_match_the_existing_dashboards() -> None:
    """SigNoz queries these names; keeping them means the dashboards survive the cutover."""
    assert SPAN_SUMMARIZE == "memsearch.summarize"
    assert SPAN_REJECTED == "memsearch.summarize_rejected"


def test_span_is_a_no_op_without_otel_configured(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with span(SPAN_SUMMARIZE, session_id="s") as sp:
        assert sp is None
        set_span_attributes(sp, anything="safe")


def test_span_is_a_no_op_when_otel_is_configured_but_absent(monkeypatch) -> None:
    """An import error must not take the service down for a feature nobody enabled."""
    import builtins

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    real_import = builtins.__import__

    def no_otel(name, *a, **k):
        if name.startswith("opentelemetry"):
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_otel)
    with span(SPAN_SUMMARIZE) as sp:
        assert sp is None


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict = {}

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value


def test_set_span_attributes_sets_them_on_a_real_span() -> None:
    sp = FakeSpan()
    set_span_attributes(sp, session_id="s1", tokens=42)
    assert sp.attributes == {"session_id": "s1", "tokens": 42}


def test_none_valued_attributes_are_skipped() -> None:
    """An unset attribute must be absent, not present-and-null: a dashboard filtering on it
    would otherwise match rows that have no value."""
    sp = FakeSpan()
    set_span_attributes(sp, present="x", missing=None)
    assert sp.attributes == {"present": "x"}


def test_span_yields_a_real_span_when_otel_is_available(monkeypatch) -> None:
    """Exercises the enabled path without requiring opentelemetry to be installed."""
    import contextlib
    import sys
    import types

    recorded: dict = {}
    fake_span = FakeSpan()

    @contextlib.contextmanager
    def start_as_current_span(name):
        recorded["name"] = name
        yield fake_span

    tracer = types.SimpleNamespace(start_as_current_span=start_as_current_span)
    trace_mod = types.SimpleNamespace(get_tracer=lambda _n: tracer)
    otel = types.ModuleType("opentelemetry")
    otel.trace = trace_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace_mod)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")

    with span(SPAN_SUMMARIZE, session_id="s1", skipped=None) as sp:
        assert sp is fake_span
    assert recorded["name"] == "memsearch.summarize"
    assert fake_span.attributes == {"session_id": "s1"}


# ---------------------------------------------------------------------------
# Emission — the half that vikunja#336 and #320 both got wrong
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_provider():
    """Give one test a pristine global TracerProvider, and take it back afterwards.

    OTel's `set_tracer_provider` refuses to override an already-installed provider — it logs
    and no-ops. So a test that installs one would silently hand its spans to whichever test
    got there first, and the assertions would be about the wrong exporter. Reaching into
    `trace._TRACER_PROVIDER` is the only way to make these tests order-independent.
    """
    trace = pytest.importorskip("opentelemetry.trace")
    pytest.importorskip("opentelemetry.sdk.trace")
    saved = trace._TRACER_PROVIDER
    saved_once = trace._TRACER_PROVIDER_SET_ONCE
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = trace.Once()
    telemetry._provider = None
    try:
        yield
    finally:
        telemetry.shutdown_tracing()
        trace._TRACER_PROVIDER = saved
        trace._TRACER_PROVIDER_SET_ONCE = saved_once
        telemetry._provider = None


def test_all_three_span_names_actually_reach_an_exporter(clean_provider, tmp_path) -> None:
    """The test the plan asked for, and the one #336 did not have.

    Asserting that `span()` was *called* proves nothing about whether an exporter would ever
    see it — `trace.get_tracer()` returns a no-op tracer until a provider is installed, so
    every call site can look correct while the process emits nothing. This drives
    `setup_tracing()` itself and reads what came out the other end.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    assert telemetry.setup_tracing(exporter=exporter) is True

    with telemetry.span(telemetry.SPAN_SUMMARIZE, session_id="s1", ok=True):
        pass
    with telemetry.span(telemetry.SPAN_REJECTED, signal="template", attempt=2, fallback=True):
        pass
    with telemetry.span(telemetry.SPAN_EXTRACT, turns=3, events=7):
        pass

    telemetry.shutdown_tracing()
    emitted = {s.name: s for s in exporter.get_finished_spans()}
    assert set(emitted) == {
        "memsearch.summarize",
        "memsearch.summarize_rejected",
        "memsearch.summarize_extract",
    }
    # Attributes, not just names. A dashboard that groups by `signal` breaks just as hard
    # when the attribute is missing as when the span is.
    assert emitted["memsearch.summarize_rejected"].attributes["signal"] == "template"
    assert emitted["memsearch.summarize_rejected"].attributes["attempt"] == 2
    assert emitted["memsearch.summarize_rejected"].attributes["fallback"] is True
    assert emitted["memsearch.summarize_extract"].attributes["events"] == 7
    assert emitted["memsearch.summarize"].attributes["ok"] is True


def test_a_real_run_emits_the_extract_and_rejected_spans(clean_provider, monkeypatch) -> None:
    """End to end: the two previously-dead names emit from the PIPELINE, not from a test.

    Wiring a span name into a test proves the constant is spellable. This drives the actual
    contamination path and the actual extraction call, which is what "has a call site" was
    supposed to mean.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from scribe.extract.models import EventLog
    from scribe.summarize.runner import summarize_log

    exporter = InMemorySpanExporter()
    assert telemetry.setup_tracing(exporter=exporter) is True

    # A provider whose output always trips the contamination guard, so the rejection path
    # runs for every attempt and falls back — the incumbent's attempt=1/attempt=2 shape.
    monkeypatch.setattr(
        "scribe.summarize.runner.detect_contamination", lambda rendered, corpus: "template"
    )
    log = EventLog(session_id="s9", agent="developer", transcript_path="/tmp/t.jsonl")

    class _Stub:
        def complete(self, system, user, timeout=0):
            from scribe.summarize.providers import Completion

            return Completion(
                text=json.dumps(
                    {"asked": "x", "done": ["y"], "decided": [], "next": [], "files": []}
                ),
                input_tokens=1,
                output_tokens=1,
                model="m",
                provider="p",
            )

    summarize_log(log, _Stub(), max_attempts=2, sleep=lambda _s: None)
    telemetry.shutdown_tracing()

    spans = exporter.get_finished_spans()
    rejected = [s for s in spans if s.name == "memsearch.summarize_rejected"]
    assert len(rejected) == 2, "one span per rejected attempt, matching the incumbent"
    assert [s.attributes["attempt"] for s in rejected] == [1, 2]
    assert [s.attributes["fallback"] for s in rejected] == [False, True]


def test_setup_tracing_is_off_by_default_and_silent_about_it(
    clean_provider, monkeypatch, capsys
) -> None:
    """No endpoint, no provider, no noise. Telemetry is opt-in and a batch CLI should not
    editorialise about a feature nobody turned on."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.setup_tracing() is False
    assert telemetry.tracing_enabled() is False
    assert capsys.readouterr().err == ""
    # And `span()` still yields None rather than raising, so call sites need no guard.
    with telemetry.span(telemetry.SPAN_EXTRACT) as sp:
        assert sp is None


def test_endpoint_set_but_packages_missing_is_loud(clean_provider, monkeypatch, capsys) -> None:
    """vikunja#336's actual lesson, as a test.

    nextcloud-mcp sat for two months with the endpoint set and the extra uninstalled, emitting
    nothing, because the only signal was one warning line nobody read. The operator has stated
    an intent the environment cannot satisfy; that must not degrade to silence.

    A warning rather than a hard failure is deliberate: scribe is a batch job over a corpus,
    and dying over an observability extra would trade a complete run for a complete outage.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    assert telemetry.setup_tracing() is False
    err = capsys.readouterr().err
    assert "OTEL_EXPORTER_OTLP_ENDPOINT is set" in err
    assert "scribe[telemetry]" in err


def test_the_qc_verdict_reaches_an_exporter_on_the_summarize_span(
    clean_provider, tmp_path, monkeypatch
) -> None:
    """The four `scribe.qc.*` attributes, from a real sweep, read back off a real exporter.

    On `memsearch.summarize` -- the existing name, kept for dashboard continuity -- and set
    while the span is still open. An attribute set after the span ends is dropped by the SDK
    without error, which is the same silent shape as #320.
    """
    import os
    from pathlib import Path

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from scribe.config import Config, ProviderConfig, StageConfig
    from scribe.pipeline import run_once
    from scribe.state import Store
    from scribe.summarize.providers import Completion, Provider

    exporter = InMemorySpanExporter()
    assert telemetry.setup_tracing(exporter=exporter) is True

    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-research"
    d.mkdir(parents=True)
    target = d / "sess.jsonl"
    fixtures = Path(__file__).parent / "fixtures"
    target.write_bytes((fixtures / "transcript-structural.jsonl").read_bytes())
    os.utime(target, (1_000_000.0 - 3600, 1_000_000.0 - 3600))
    cfg = Config(
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "ev"),
        state_path=str(tmp_path / "s.sqlite3"),
        qc_run_record=str(tmp_path / "runs.jsonl"),
        providers={"stub": ProviderConfig(name="stub", type="openai-compatible", model="m")},
        stages={"session": StageConfig(provider="stub", model="m")},
    )

    class _Stub(Provider):
        name = "stub"

        def complete(self, system, user, *, timeout=120.0) -> Completion:
            # One path the log never mentions: a guaranteed `absent` finding.
            body = {"asked": "q", "done": ["Edited /nowhere/invented/zzqx_ghost.py"]}
            return Completion(text=json.dumps(body), model="m", provider="stub")

    (r,) = run_once(cfg, Store(cfg.state_path), now=1_000_000.0, provider_factory=_Stub)
    telemetry.shutdown_tracing()

    (sp,) = [s for s in exporter.get_finished_spans() if s.name == "memsearch.summarize"]
    attrs = sp.attributes
    assert attrs["scribe.qc.ok"] is False and r.qc_ok is False
    assert attrs["scribe.qc.coverage"] == round(r.qc_coverage, 4)
    assert attrs["scribe.qc.findings"] == len(r.qc_findings) >= 1
    assert attrs["scribe.qc.findings.absent"] >= 1
