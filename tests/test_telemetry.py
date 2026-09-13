"""Tests for spend metering and optional OTel spans.

The spend record's shape is asserted against the keys `memsearch-spend.sh` actually reads,
because that is the whole point of the format: `timestamp`, `model`, `input_tokens`,
`output_tokens`, and `event_kind` or `event`.
"""

from __future__ import annotations

import json

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
