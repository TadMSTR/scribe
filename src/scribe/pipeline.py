"""The end-to-end pipeline: discover, extract, summarize, write back, check.

Phase 6 runs this **in shadow**. `memsearch-summarize` stays in production, scribe writes to
its own output root, and nothing in the live path changes. Cutover is a separate decision.

`dry_run` performs every step except the model call, which makes the extractor and the gates
exercisable against the real corpus at zero cost and with nothing leaving the machine. It is
the mode to reach for when the question is "is the event log right", which is most of the
time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .discovery import scan
from .extract import extract
from .extract.models import EventLog
from .extract.redact import Redactor
from .qc import DEFAULT_COVERAGE_FLOOR, Report, check_digest
from .state import STATUS_FAILED, SessionRow, Store
from .summarize.providers import Provider, build
from .summarize.runner import Outcome, summarize_log
from .telemetry import SPAN_SUMMARIZE, record_spend, set_span_attributes, span
from .writeback import append_block, daily_path


@dataclass
class SessionResult:
    """What happened to one session, for the run report and the shadow comparison."""

    transcript_path: str
    session_id: str = ""
    agent: str = ""
    turns: int = 0
    events: int = 0
    extracted_chars: int = 0
    token_estimate: int = 0
    secrets_redacted: int = 0
    degradation_level: int = 0
    summarized: bool = False
    written: bool = False
    #: `written` says a block reached the file; it does NOT say the block is a digest.
    #: `render_failure` and `render_suppressed` both produce a block, and both are appended, so
    #: a run that lost every summary reports the same `written` as one that lost none. These two
    #: are what make the totals readable: `written == summarized + suppressed + placeholder`.
    suppressed: bool = False
    placeholder: bool = False
    dry_run: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    qc_ok: bool | None = None
    qc_coverage: float = 0.0
    qc_findings: list[str] = field(default_factory=list)
    #: Secrets caught by the last-line re-scrub of the rendered digest. Should always be
    #: zero -- see `process_session`. Any non-zero value is an extraction-layer miss.
    post_render_redactions: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "transcript_path": self.transcript_path,
            "session_id": self.session_id,
            "agent": self.agent,
            "turns": self.turns,
            "events": self.events,
            "extracted_chars": self.extracted_chars,
            "token_estimate": self.token_estimate,
            "secrets_redacted": self.secrets_redacted,
            "degradation_level": self.degradation_level,
            "summarized": self.summarized,
            "written": self.written,
            "suppressed": self.suppressed,
            "placeholder": self.placeholder,
            "dry_run": self.dry_run,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "qc_ok": self.qc_ok,
            "qc_coverage": round(self.qc_coverage, 4),
            "qc_findings": self.qc_findings,
            "post_render_redactions": self.post_render_redactions,
            "errors": self.errors,
        }


def _last_turn_uuid(log: EventLog) -> str:
    return log.turns[-1].turn_uuid if log.turns else ""


def _parse_ts(value: str) -> datetime | None:
    """Parse one extractor timestamp, or return None if it is absent or malformed."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def session_when(log: EventLog, fallback: datetime) -> datetime:
    """The datetime a digest is filed under -- the session's own, never the wall clock.

    This one value decides the daily filename, the `## Session HH:MM` heading and the
    `### HH:MM` heading inside the block, so sourcing it from the session fixes all three
    at once (vikunja#847).

    **`started_at` first, not `ended_at`.** The plan argued for `ended_at` because it matches
    the incumbent's stop-hook semantics, but that does not transfer: the incumbent appends per
    *turn*, so a session spanning midnight has its turns split across two daily files by
    construction, while scribe writes one block for the whole session. Neither choice
    reproduces it, so the tiebreak is which one loses less. A session running 14:00 -> 01:00
    has ten hours of work on the first day and one on the second; anchoring on the end files
    all eleven under the second. Sessions end just past midnight far more often than they
    start just before it, so the start is the less lossy anchor.

    Timestamps are normalised to UTC because that is what the extractor records and what the
    previous `datetime.now(UTC)` produced -- mixing a local-time date into a corpus of UTC
    ones would be worse than either alone.

    The clock stays as the last resort, for a transcript whose timestamps are missing or
    unparseable. Filing such a session under today beats not filing it at all.
    """
    for candidate in (log.started_at, log.ended_at):
        parsed = _parse_ts(candidate)
        if parsed is not None:
            return parsed
    return fallback


def process_session(
    row: SessionRow,
    cfg: Config,
    store: Store,
    *,
    provider: Provider | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
    spend_log: str | None = None,
) -> SessionResult:
    """Run one session all the way through. Never raises for an expected failure.

    An unexpected exception is caught and recorded against the session rather than allowed
    to abort the sweep: one malformed transcript must not stop the other fourteen.
    """
    result = SessionResult(transcript_path=row.transcript_path, agent=row.agent, dry_run=dry_run)
    try:
        log = extract(row.transcript_path, max_session_chars=cfg.max_session_chars)
    except OSError as exc:
        result.errors.append(f"extract: {exc}")
        store.set_status(row.transcript_path, STATUS_FAILED, error=str(exc))
        return result

    st = log.stats
    result.session_id = log.session_id
    result.agent = log.agent or row.agent
    result.turns, result.events = st.turns, st.tool_events
    result.extracted_chars, result.token_estimate = st.extracted_chars, st.token_estimate
    result.secrets_redacted = st.secrets_redacted
    result.degradation_level = st.degradation_level
    # Derived here rather than from the clock, and necessarily after extraction: the session's
    # own timestamps are the only thing that makes a backfill file 429 digests under 429 dates
    # instead of collapsing them all into the day the backfill ran (vikunja#847).
    when = session_when(log, now or datetime.now(UTC))

    if not log.turns:
        # A transcript with no real user turn is not a failure; there is simply nothing to
        # summarize. Marking it summarized stops it being re-offered on every scan.
        store.mark_summarized(
            row.transcript_path, offset=st.raw_file_bytes, last_turn_uuid="", turn_uuids=[]
        )
        return result

    turn_uuids = [t.turn_uuid for t in log.turns]
    pending = store.unprocessed_turns(row.transcript_path, turn_uuids)
    if not pending:
        store.mark_summarized(
            row.transcript_path,
            offset=st.raw_file_bytes,
            last_turn_uuid=_last_turn_uuid(log),
            turn_uuids=[],
        )
        return result

    if dry_run or provider is None:
        return result

    with span(
        SPAN_SUMMARIZE, session_id=log.session_id, agent=result.agent, events=st.tool_events
    ) as sp:
        outcome: Outcome = summarize_log(log, provider, heading=f"{when:%H:%M}")
        set_span_attributes(
            sp,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            ok=outcome.ok,
            suppressed=outcome.suppressed,
        )

    result.input_tokens, result.output_tokens = outcome.input_tokens, outcome.output_tokens
    result.summarized = outcome.ok
    result.suppressed = outcome.suppressed
    # The third state, and the one that was invisible: neither a digest nor a deliberate
    # suppression note, but `render_failure`'s marked placeholder. The session's summary is
    # gone. Recorded here so the run totals can say so out loud -- inferring it from
    # `written - summarized` is how 13 lost sessions would have read as a clean backfill.
    result.placeholder = not outcome.ok and not outcome.suppressed
    result.errors.extend(outcome.errors)
    if outcome.input_tokens or outcome.output_tokens:
        record_spend(
            model=outcome.model,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            session_id=log.session_id,
            provider=outcome.provider,
            log_path=spend_log,
        )

    # Defence in depth on the way to disk. The audit observed that nothing re-redacts the
    # summarizer's RENDERED output, so the whole redaction model rested on extraction-time
    # completeness -- and the two Medium findings in that layer showed the assumption was not
    # free. This re-scrub is deliberately NOT a substitute for fixing extraction: the outbound
    # call happens earlier, so it protects the on-disk digest only.
    #
    # It is also a DETECTOR. The digest is derived from an already-scrubbed event log, so a
    # non-zero count here can only mean extraction missed something. That is worth surfacing
    # loudly rather than quietly cleaning up.
    guard = Redactor()
    markdown = guard.scrub(outcome.markdown)
    if guard.count:
        result.post_render_redactions = guard.count
        result.errors.append(
            f"post-render redaction fired {guard.count}x — a secret survived extraction "
            f"and was caught only at write time; investigate redact.py"
        )

    # The QC gate runs on whatever was produced, including a placeholder: a run report that
    # silently omits the sessions that failed is the same shape of blind spot this component
    # was built to remove.
    report: Report = check_digest(markdown, log, floor=coverage_floor)
    result.qc_ok = report.ok
    result.qc_coverage = report.coverage
    result.qc_findings = [f"{f.check}: {f.detail}" for f in report.findings]

    path = daily_path(cfg.output_dir, result.agent, when)
    result.written = append_block(
        path,
        body=markdown,
        session_id=log.session_id,
        turn_uuid=_last_turn_uuid(log),
        transcript_path=row.transcript_path,
        when=when,
    )
    if outcome.ok or outcome.suppressed:
        store.mark_summarized(
            row.transcript_path,
            offset=st.raw_file_bytes,
            last_turn_uuid=_last_turn_uuid(log),
            turn_uuids=pending,
        )
    else:
        attempts = store.record_attempt(row.transcript_path, error=outcome.reason)
        store.set_status(row.transcript_path, STATUS_FAILED, error=outcome.reason)
        result.errors.append(f"attempt {attempts}: {outcome.reason}")
    return result


def run_once(
    cfg: Config,
    store: Store,
    *,
    dry_run: bool = False,
    limit: int = 0,
    now: float | None = None,
    provider_factory: Callable[[], Provider] | None = None,
    spend_log: str | None = None,
) -> list[SessionResult]:
    """One sweep: discover ready sessions and process each.

    The provider is constructed once per sweep and only when it is actually needed, so a
    dry run never touches provider config at all — including its credential.
    """
    ready = scan(cfg, store, now=now)
    if limit:
        ready = ready[:limit]
    provider: Provider | None = None
    if not dry_run and ready:
        provider = (
            provider_factory
            or (lambda: build(cfg.provider_for("session"), cfg.stages["session"].model))
        )()
    return [
        process_session(row, cfg, store, provider=provider, dry_run=dry_run, spend_log=spend_log)
        for row in ready
    ]


def summarize_run(results: list[SessionResult]) -> dict:
    """Aggregate a sweep into the figures the shadow comparison reports."""
    graded = [r for r in results if r.qc_ok is not None]
    return {
        "sessions": len(results),
        "summarized": sum(1 for r in results if r.summarized),
        "written": sum(1 for r in results if r.written),
        "suppressed": sum(1 for r in results if r.suppressed),
        #: Non-zero means summaries were LOST, not merely degraded. Treat it as loud.
        "placeholders": sum(1 for r in results if r.placeholder),
        "events_total": sum(r.events for r in results),
        "secrets_redacted": sum(r.secrets_redacted for r in results),
        "post_render_redactions": sum(r.post_render_redactions for r in results),
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
        "degraded": sum(1 for r in results if r.degradation_level),
        "qc_graded": len(graded),
        "qc_passed": sum(1 for r in graded if r.qc_ok),
        "groundedness_rate": (
            round(sum(1 for r in graded if r.qc_ok) / len(graded), 4) if graded else None
        ),
        "mean_coverage": (
            round(sum(r.qc_coverage for r in graded) / len(graded), 4) if graded else None
        ),
        "errors": sum(len(r.errors) for r in results),
    }


def output_root(cfg: Config) -> Path:
    return Path(cfg.output_dir).expanduser()
