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
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .paths import secure_dir, secure_file

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
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        secure_file(path)
    except OSError:
        return


@contextlib.contextmanager
def span(name: str, **attributes) -> Iterator[object]:
    """Start an OTel span if OTel is configured, otherwise do nothing.

    Yields either a real span or None, so callers guard their `set_attribute` calls. Enabled
    by `OTEL_EXPORTER_OTLP_ENDPOINT`, matching the convention used by the other forge MCP
    servers.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
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
