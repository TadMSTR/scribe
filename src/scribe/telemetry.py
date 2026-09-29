"""Spend metering and OTel spans.

**Spend records are written in the exact shape `memsearch-spend.sh` already parses.** That
parser finds the first `{` on a line and reads `timestamp`, `model`, `input_tokens`,
`output_tokens` and `event_kind`/`event`, so a record is a plain JSON line and needs no
change to the meter.

Worth recording precisely, because the build plan gets the cause wrong: the plan says the
meter "currently meters only compact". It does not — `SUMMARIZE_LOG` is already in its stream
list. The real defect, measured 2026-09-13, is that `memsearch-summarize` writes its token
records to `~/.pm2/logs/memsearch-summarize-out.log` while the meter reads
`~/logs/memsearch-summarize.log`, which is 0 bytes. Reported spend was therefore about half
the real figure. Filed as vikunja#846. Scribe writes to its own dedicated log rather than to
a PM2 stdout stream, so it is not subject to that rotation and the path is explicit.

**OTel is optional and off by default.** The import is inside the function: a telemetry
dependency that must be installed for the extractor to run would defeat the point of keeping
it dependency-free, and an import error at module scope would take the service down for a
feature nobody enabled.

**`span()` alone emits nothing.** `trace.get_tracer()` returns a *no-op* tracer until an SDK
`TracerProvider` has been installed, so a process that never calls `setup_tracing()` produces
spans that go nowhere -- with no error, and with every call site looking correct. That is the
shape of vikunja#320 (searxng-mcp: "init succeeds, export is silently lost"), and it is the
reason `tests/test_telemetry.py` asserts against a real exporter rather than against `span()`
having been called. A test that builds its own provider would pass while production stayed
dark; the in-memory test therefore drives `setup_tracing()` itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .paths import secure_append, secure_dir, secure_file

#: Span names match the ones the existing SigNoz dashboards query, so those keep working
#: across the shadow run and the cutover.
SPAN_SUMMARIZE = "memsearch.summarize"
SPAN_REJECTED = "memsearch.summarize_rejected"
SPAN_EXTRACT = "memsearch.summarize_extract"

DEFAULT_SPEND_LOG = "~/logs/scribe-tokens.log"


def record_spend(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    event: str = "summarize",
    session_id: str = "",
    provider: str = "",
    log_path: str | os.PathLike[str] | None = None,
) -> None:
    """Append one spend record.

    Never raises. A metering failure must not take down a summarization run — the whole
    point of the meter is to notice an outage, and a meter that can cause one is worse than
    no meter.
    """
    path = Path(os.path.expanduser(str(log_path or DEFAULT_SPEND_LOG)))
    record = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "model": model,
        "provider": provider,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "event_kind": event,
    }
    try:
        secure_dir(path.parent)
        # Created 0600 at `O_CREAT`, not chmod'd after the first write (FW-03). `secure_file`
        # still runs for a log an older build created at the umask's mode.
        with secure_append(path) as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        secure_file(path)
    except OSError:
        return


#: Set once `setup_tracing()` has installed a provider, so `shutdown_tracing()` knows whether
#: there is anything to flush and a second `setup_tracing()` call is a no-op rather than a
#: second exporter attached to the same process.
_provider: object | None = None


def tracing_enabled() -> bool:
    """Whether a provider has been installed by `setup_tracing()`."""
    return _provider is not None


def setup_tracing(*, service_name: str = "scribe", exporter: object | None = None) -> bool:
    """Install an OTel `TracerProvider` so `span()` actually exports. Returns whether it did.

    **This is the step whose absence is invisible.** Without it `span()` still runs, still
    yields an object, and still sets attributes -- onto a no-op tracer that discards
    everything. Installing the packages and setting the endpoint is not sufficient; something
    has to build the provider, and that something is this.

    Three outcomes, and the middle one is the one vikunja#336 spent two months in:

      * **No endpoint** -- returns False silently. Telemetry is off by default and a batch
        CLI should not comment on a feature nobody asked for.
      * **Endpoint set, packages missing** -- returns False *and prints to stderr*. The
        operator has stated an intent that the environment cannot satisfy, and #336's own
        recommendation was to stop letting that degrade to silence. It is a warning rather
        than a fatal error because scribe is a batch job over a corpus: dying over an
        observability extra would trade a complete run for a complete outage.
      * **Both present** -- builds the provider, returns True.

    `exporter` is injectable so a test can drive this exact path with an in-memory exporter.
    A test that constructs its own `TracerProvider` instead would prove only that the OTel
    SDK works, which was never in doubt -- what needs proving is that *scribe's* bootstrap
    reaches an exporter, and that the two new call sites are inside it.
    """
    global _provider
    if _provider is not None:
        return True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint and exporter is None:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        print(
            "scribe: OTEL_EXPORTER_OTLP_ENDPOINT is set but the OpenTelemetry packages are "
            "not installed — no spans will be emitted. Install with: "
            "pip install 'scribe[telemetry]'",
            file=sys.stderr,
        )
        return False
    if exporter is None:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError:
            print(
                "scribe: OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP gRPC exporter is "
                "not installed — no spans will be emitted. Install with: "
                "pip install 'scribe[telemetry]'",
                file=sys.stderr,
            )
            return False
        # No path suffix. The gRPC exporter takes the endpoint verbatim, unlike the HTTP one
        # which wants `/v1/traces` appended — and forge's endpoint is the gRPC port (4317).
        exporter = OTLPSpanExporter(endpoint=endpoint)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))  # type: ignore[arg-type]
    trace.set_tracer_provider(provider)
    _provider = provider
    return True


def shutdown_tracing() -> None:
    """Flush and tear down the provider. Safe to call when tracing was never set up.

    `BatchSpanProcessor` exports on a timer, so a batch CLI that finishes its sweep and exits
    can drop the whole batch — including, by construction, the spans from the end of the run.
    Shutting down forces the flush.
    """
    global _provider
    provider = _provider
    _provider = None
    if provider is None:
        return
    with contextlib.suppress(Exception):
        provider.shutdown()  # type: ignore[attr-defined]


@contextlib.contextmanager
def span(name: str, **attributes) -> Iterator[object]:
    """Start an OTel span if OTel is configured, otherwise do nothing.

    Yields either a real span or None, so callers guard their `set_attribute` calls. Enabled
    by `OTEL_EXPORTER_OTLP_ENDPOINT`, matching the convention used by the other forge MCP
    servers.

    **A span from here only goes anywhere if `setup_tracing()` ran.** The endpoint check
    below decides whether to build a span at all; the provider decides whether that span is
    exported or discarded. `tracing_enabled()` is consulted too so an injected exporter works
    without the environment variable, which is what makes the emission test possible.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") and not tracing_enabled():
        yield None
        return
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    tracer = trace.get_tracer("scribe")
    with tracer.start_as_current_span(name) as sp:
        for key, value in attributes.items():
            if value is not None:
                sp.set_attribute(key, value)
        yield sp


def set_span_attributes(sp: object, **attributes) -> None:
    """Set attributes on a span that may be None."""
    if sp is None:
        return
    for key, value in attributes.items():
        if value is not None:
            sp.set_attribute(key, value)  # type: ignore[attr-defined]
