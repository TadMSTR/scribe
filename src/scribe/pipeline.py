"""The end-to-end pipeline: discover, extract, summarize, write back, check.

Phase 6 runs this **in shadow**. `memsearch-summarize` stays in production, scribe writes to
its own output root, and nothing in the live path changes. Cutover is a separate decision.

`dry_run` performs every step except the model call, which makes the extractor and the gates
exercisable against the real corpus at zero cost and with nothing leaving the machine. It is
the mode to reach for when the question is "is the event log right", which is most of the
time.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .discovery import orphaned, scan
from .eventlog import (
    EventLogError,
    contains_value,
    load_eventlog,
    session_eventlog,
    write_eventlog,
)
from .extract import extract
from .extract.models import EventLog
from .extract.redact import Redactor
from .manifest import append_turn
from .qc import DEFAULT_COVERAGE_FLOOR, Report, check_digest
from .state import STATUS_FAILED, SessionRow, Store
from .summarize.providers import Provider, build
from .summarize.runner import Outcome, summarize_log
from .telemetry import SPAN_EXTRACT, SPAN_SUMMARIZE, record_spend, set_span_attributes, span
from .writeback import (
    PROVISIONAL_PLACEHOLDER,
    PROVISIONAL_SUPPRESSED,
    append_block,
    apply_frontmatter,
    daily_path,
)


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
    #: A digest was produced and then NOT written, because the turn already held a final
    #: block. This is the term that was missing from the identity above: `written` was False
    #: while `summarized` was True, 30 times, and nothing reported the difference. The replace
    #: path should make it unreachable; it is counted so that claim is checkable rather than
    #: assumed.
    discarded: bool = False
    #: Items dropped because an unbounded field overflowed its cap (vikunja#884).
    #:
    #: **Not a fourth term in the identity above.** A truncated digest is summarized and it is
    #: written, so `written == summarized + suppressed + placeholder` is untouched — this is an
    #: annotation on a success, not a new outcome. That identity is what made this whole family
    #: of bugs visible, and a state that broke it would cost more than it reported.
    #:
    #: Counted in ITEMS rather than as a flag because the number is the evidence for whether
    #: the cap is set anywhere near right. Nothing else measures the uncensored tail: the
    #: written corpus is censored by the cap, and now that overflow no longer raises, the
    #: rejection log will not record it either. If this is routinely large, `MAX_ITEMS` is
    #: wrong; if it stays at zero, the declared cap is doing the work on its own.
    truncated_items: int = 0
    #: The same overflow, per field: field name -> items dropped. `truncated_items` is this
    #: dict summed, and the sum does not replace it. "148 items dropped" and "91 from
    #: `found`, 42 from `decisions`, 15 from `open_items`" are different facts, and only the
    #: second names a cap to go and look at. `Digest.truncated` has carried the breakdown
    #: since #884; collapsing it to a scalar here, at the point of recording, is what made
    #: the first live firing (2026-09-17, 148 items) impossible to localise afterwards.
    truncated_fields: dict[str, int] = field(default_factory=dict)
    #: True when this sweep summarized the transcript from byte 0 rather than continuing one
    #: already part-read -- `reset_for_retry` cleared the offset, or the session was first
    #: seen after it had already finished.
    #:
    #: **Deliberately wider than "was this a recovery run".** A recovery re-read and a
    #: first-time read of an already-complete transcript are the same thing from the
    #: summarizer's side: both hand the model a whole session at once, where an incremental
    #: sweep hands it one that is still growing. Those are different populations and pooling
    #: them is how "truncation is up this week" stays unexplained. `last_offset == 0` names
    #: the distinction that matters and costs no state-DB column to carry, so it is the one
    #: recorded (build handoff question 3).
    full_read: bool = False
    dry_run: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    qc_ok: bool | None = None
    qc_coverage: float = 0.0
    qc_findings: list[str] = field(default_factory=list)
    #: Secrets caught by the last-line re-scrub of the rendered digest. Should always be
    #: zero -- see `process_session`. Any non-zero value is an extraction-layer miss.
    post_render_redactions: int = 0
    #: Which cause the fire supports — `extraction-miss`, `model-output` or `undetermined`.
    #: Empty when nothing fired. Reported so a sweep can be filtered by cause rather than by
    #: reading every error string (vikunja#856).
    post_render_cause: str = ""
    #: Where this session's event log was persisted, or "" if it was not. Reported so a run
    #: says out loud whether the drill-down tier exists for each session rather than leaving
    #: it to be inferred from a directory listing.
    eventlog_path: str = ""
    #: True when this session was rebuilt from its persisted event log because the transcript
    #: was gone (vikunja#873). Reported rather than silent: a replayed digest is as good as
    #: its log and no better, and an operator reading a run report should be able to see which
    #: sessions were reconstructed rather than read.
    replayed: bool = False
    #: Whether this session's block reached `index.jsonl`. False with `manifest_error` set is
    #: a manifest that is now behind the corpus -- non-fatal, since the manifest observes the
    #: run and must not take it down, and repairable with `scribe index --rebuild`.
    #:
    #: **Not in `errors`, and not a term in `written == summarized + suppressed + placeholder`.**
    #: The digest was written either way; this is about a side channel. Its own field and its
    #: own total, per invariant 13, rather than a string an operator has to grep for.
    manifest_appended: bool = False
    manifest_error: str = ""
    #: The `on_digest_written` hook: "" when none is configured or nothing was written, else
    #: "ok" or "failed". Same reasoning as the manifest -- counted, never fatal, not an error.
    hook: str = ""
    hook_error: str = ""
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
            "discarded": self.discarded,
            "truncated_items": self.truncated_items,
            "truncated_fields": self.truncated_fields,
            "full_read": self.full_read,
            "dry_run": self.dry_run,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "qc_ok": self.qc_ok,
            "qc_coverage": round(self.qc_coverage, 4),
            "qc_findings": self.qc_findings,
            "post_render_redactions": self.post_render_redactions,
            "post_render_cause": self.post_render_cause,
            "eventlog_path": self.eventlog_path,
            "replayed": self.replayed,
            "manifest_appended": self.manifest_appended,
            "manifest_error": self.manifest_error,
            "hook": self.hook,
            "hook_error": self.hook_error,
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


#: Extraction missed it: the value is in the event log, so the model was shown it. This is
#: the only case where `redact.py` is the file to look at.
CAUSE_EXTRACTION = "extraction-miss"
#: The model emitted a secret-shaped string that was never in its input. Not an extraction
#: defect; the digest is still correct on disk because the re-scrub caught it.
CAUSE_MODEL = "model-output"
#: No event log to consult, so neither cause is supported by evidence.
CAUSE_UNKNOWN = "undetermined"

_CAUSE_DETAIL: dict[str, str] = {
    CAUSE_EXTRACTION: (
        "at least one matched value IS present in this session's event log, so extraction "
        "missed it and the model was shown it — investigate redact.py"
    ),
    CAUSE_MODEL: (
        "no matched value appears in this session's event log, so the model emitted a "
        "secret-shaped string that was never in its input — this is NOT an extraction miss "
        "and redact.py is not the file to look at"
    ),
    CAUSE_UNKNOWN: (
        "this session's event log is absent or unreadable, so the cause cannot be "
        "determined — re-run with the event log present to tell the two apart"
    ),
}


def classify_post_render(eventlog_path: str, values: list[str]) -> str:
    """Which cause a post-render redaction fire supports, given the persisted event log.

    `True` wins over `False`, and `False` over `None`, because the three answers are not
    symmetric. One value found in the log proves extraction missed something regardless of
    how many others were not; a clean "not present" is only meaningful once the log has
    actually been read; and no log at all proves nothing either way.
    """
    verdicts = [contains_value(eventlog_path, value) for value in values]
    if any(v is True for v in verdicts):
        return CAUSE_EXTRACTION
    if any(v is False for v in verdicts):
        return CAUSE_MODEL
    return CAUSE_UNKNOWN


#: How much of a failing hook's stderr is kept. Its last line is usually the reason; the rest
#: is the hook's business, and an unbounded string does not belong in a run report.
HOOK_STDERR_CHARS = 200
#: How much of the stderr file is read to find that last line.
HOOK_STDERR_TAIL_BYTES = 4096


#: What every hook sees, whatever it is configured with: enough to find its binary and run in
#: the operator's locale, and nothing that authenticates anything.
HOOK_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")


def hook_env(extra: Sequence[str] = ()) -> dict[str, str]:
    """The hook's environment: `HOOK_BASE_ENV` plus the names the operator listed. Nothing else.

    **Not the sweep's own environment.** A sweep runs with the summarizer's credential in it
    (`MISTRAL_API_KEY` on forge), and an indexer has no use for that key. Inheriting by
    default would hand it to whatever binary `on_digest_written` names -- SC-06 in the fleet's
    pattern base, where the recurring form is exactly a new subprocess added without `env=`.
    An indexer that genuinely needs a secret of its own names it in `on_digest_written_env`,
    by exact name, so the grant is visible in the config that makes it.
    """
    return {k: os.environ[k] for k in (*HOOK_BASE_ENV, *extra) if k in os.environ}


def run_hook(
    argv: Sequence[str], digest: Path, timeout: float, env: dict[str, str] | None = None
) -> str:
    """Run `argv + [digest]`. Returns "" on success, else why it failed. Never raises.

    `shell=False` with an argv list -- `config._hook_argv` refuses a string at load for the
    injection reason given there. stdin is closed and stdout discarded, so a hook can neither
    wait on the sweep's terminal nor write into the cron log.

    **The outcome is the direct child's, and only the direct child is waited on.** stderr goes
    to an unlinked temp file, not a pipe. Waiting on a pipe waits for *every* process holding
    its write end, and an indexer hook that starts a background worker, or daemonises into its
    own session, hands that end on. With a pipe, a hook that exited 0 leaving a worker behind
    was reported as timed out, and one whose worker escaped the process group stalled the
    sweep for the worker's whole lifetime -- the timeout bounded nothing. Both reproduced, and
    CodeRabbit named them on PR #24. A file has no reader to block.

    **Its own process group, killed as a group on timeout**, so a hung hook takes its children
    with it. A child that has left the group (`setsid`) survives; it no longer delays the
    sweep, and killing processes outside the hook's group is not scribe's business.
    """
    with tempfile.TemporaryFile() as errf:
        try:
            proc = subprocess.Popen(  # noqa: S603 -- argv list from validated config, no shell
                # Absolute, so the appended path can never begin with `-` and be read as an
                # option by the hook -- a relative `output_dir` would pass one through as-is.
                [*argv, str(Path(digest).absolute())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errf,
                start_new_session=True,
                env=hook_env() if env is None else env,
            )
        except OSError as exc:
            return f"could not start: {exc}"
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return f"timed out after {timeout:g}s"
        if not proc.returncode:
            return ""
        # Only the tail: the last line is usually the reason, and a hook's stderr is otherwise
        # unbounded -- a worker left behind may still be appending to it.
        size = errf.seek(0, os.SEEK_END)
        errf.seek(max(0, size - HOOK_STDERR_TAIL_BYTES))
        err = errf.read(HOOK_STDERR_TAIL_BYTES).decode("utf-8", "replace")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    tail = lines[-1].strip()[:HOOK_STDERR_CHARS] if lines else ""
    return f"exit {proc.returncode}" + (f": {tail}" if tail else "")


def _after_write(cfg: Config, result: SessionResult, path: Path, turn_uuid: str) -> None:
    """Everything that follows a block reaching disk. None of it can unwrite the block.

    Order is load-bearing. Frontmatter first, because it rewrites the file and a hook must be
    handed the finished file. The manifest before the hook, because the hook is the
    notification and the manifest is the payload it points at -- a consumer woken by the hook
    must find its line already there. The manifest hashes the BLOCK, which frontmatter does
    not touch, so the two do not interact.
    """
    if cfg.emit_frontmatter:
        try:
            apply_frontmatter(path)
        except OSError as exc:
            result.errors.append(f"frontmatter: {exc}")
    try:
        append_turn(cfg, path, turn_uuid)
        result.manifest_appended = True
    except (OSError, LookupError, ValueError) as exc:
        result.manifest_error = str(exc)
    if cfg.on_digest_written:
        result.hook_error = run_hook(
            cfg.on_digest_written,
            path,
            cfg.on_digest_written_timeout_seconds,
            env=hook_env(cfg.on_digest_written_env),
        )
        result.hook = "failed" if result.hook_error else "ok"


def _load_session(row: SessionRow, cfg: Config) -> tuple[EventLog, bool]:
    """The session's event log: extracted from the transcript, or replayed from the persisted
    copy when the transcript is gone. Returns `(log, replayed)`.

    The transcript is preferred whenever it exists, and not only out of caution. It is the
    source the persisted log was derived FROM, so it can be newer -- a session that resumed
    has grown since its log was written, and re-extracting is what picks that up. Measured
    2026-09-17 across the 429 sessions holding both inputs: 428 round-tripped identically and
    the one that did not was mid-session, its persisted turns an exact prefix of the fresh
    extract. So the fallback costs nothing when the transcript is there, and is the only
    option when it is not.

    `OSError` from `extract` is NOT treated as "transcript absent". A permissions failure or
    a bad read is a different condition from a file that has aged out, and quietly replaying
    an older log in that case would hide a real fault behind a stale-but-plausible digest.
    Only a genuinely missing file takes the second path.
    """
    transcript = Path(row.transcript_path).expanduser()
    if transcript.exists():
        return extract(row.transcript_path, max_session_chars=cfg.max_session_chars), False

    log = load_eventlog(session_eventlog(cfg.eventlog_dir, row.session_id, row.transcript_path))
    return log, True


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
    result = SessionResult(
        transcript_path=row.transcript_path,
        agent=row.agent,
        dry_run=dry_run,
        # Read off the row BEFORE any store write in this function moves it.
        full_read=row.last_offset == 0,
    )
    # Wrapped at the CALL SITE rather than inside `scribe.extract`, deliberately. That package
    # is asserted stdlib-only by `tests/test_stdlib_only.py` -- it is the component that reads
    # raw transcripts, and its "no network" claim is structural rather than conventional.
    # Importing a telemetry module into it would make that invariant a property of what
    # `telemetry.py` happens to import today. Here, the span covers the same work and the
    # extractor stays clean.
    with span(SPAN_EXTRACT, transcript_path=row.transcript_path, agent=row.agent) as sp:
        try:
            log, result.replayed = _load_session(row, cfg)
        except (OSError, EventLogError) as exc:
            set_span_attributes(sp, ok=False, error=str(exc))
            result.errors.append(f"extract: {exc}")
            store.set_status(row.transcript_path, STATUS_FAILED, error=str(exc))
            return result
        set_span_attributes(
            sp,
            ok=True,
            replayed=result.replayed,
            session_id=log.session_id,
            turns=log.stats.turns,
            events=log.stats.tool_events,
            extracted_chars=log.stats.extracted_chars,
            secrets_redacted=log.stats.secrets_redacted,
            degradation_level=log.stats.degradation_level,
        )

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

    # ABOVE the two `mark_summarized` calls below, and that placement is the whole of
    # vikunja#902. It used to sit under them, so the two "nothing to summarize" branches
    # retired a session during a run documented as inert -- and the documentation is what
    # makes it safe to point a dry run at production config.
    #
    # This is a guard against PROCESSING, not against writing. A dry run has already called
    # `upsert_observed` in `scan`, recording the transcript's size and mtime, and that must
    # keep happening: AGENTS.md invariant 8 says an observation records what is on disk, not
    # what has been handled. Measured on the real corpus, discovery touches ~460 rows a sweep
    # against this branch's one. Guarding that too would make a dry run forget what it saw.
    #
    # The cost, which is real and was accepted deliberately: a turnless or fully-processed
    # session is no longer retired by a dry run, so every dry run re-offers it and the next
    # live run does the bookkeeping. Those sessions have nothing to summarize, so the work
    # is a re-extract and nothing else.
    if dry_run or provider is None:
        return result

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

    # Persist the event log HERE: past the dry-run guard, and strictly before the
    # summarization call below.
    #
    # **Before the call**, because writing it afterwards would lose it in exactly the case it
    # exists for -- a failed model call, where this is the only evidence of what the run saw,
    # and the thing that lets a replay skip re-extraction.
    #
    # **After the dry-run guard**, because a dry run is documented to write nothing, and that
    # inertness is what makes it safe to sweep the real corpus with. "Is the event log right"
    # is answerable from `scribe extract --json` without giving that up.
    #
    # A failure to write must not abort the session: a digest without its drill-down tier is
    # worth more than no digest. The error is recorded rather than swallowed.
    try:
        result.eventlog_path = str(write_eventlog(cfg.eventlog_dir, log))
    except OSError as exc:
        result.errors.append(f"eventlog: {exc}")

    with span(
        SPAN_SUMMARIZE, session_id=log.session_id, agent=result.agent, events=st.tool_events
    ) as sp:
        outcome: Outcome = summarize_log(log, provider, heading=f"{when:%H:%M}")
        if outcome.digest is not None:
            result.truncated_fields = dict(outcome.digest.truncated)
            result.truncated_items = sum(result.truncated_fields.values())
        set_span_attributes(
            sp,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            ok=outcome.ok,
            suppressed=outcome.suppressed,
            # On the span so SigNoz holds the time series without anyone parsing a log.
            # `full_read` rides along because the rate is only interpretable per population.
            truncated_items=result.truncated_items,
            full_read=result.full_read,
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
    # It is also a DETECTOR, and a fire here has TWO possible causes (vikunja#856):
    #
    #   * extraction missed it — the value was in the event log and went to the model, or
    #   * the model emitted it — a secret-shaped string that was never in its input.
    #
    # This comment used to say a fire "can only mean extraction missed something", and the
    # error told the reader to investigate `redact.py`. That was unfalsifiable before #852
    # persisted the event log, and measurement since says it was often wrong: two `--live
    # --limit 3` runs over the SAME three sessions gave `post_render_redactions` of 1 then 0,
    # which an input-side cause cannot produce. Separately, all three persisted logs scrubbed
    # field-by-field fired 0 times across 1,797 string leaves.
    #
    # So ask the log rather than asserting. `capture=True` is used ONLY here and the captured
    # plaintext never leaves this block — in particular it is never put in `result.errors`,
    # which is an unredacted sink that reaches the JSON report and the CLI.
    guard = Redactor(capture=True)
    markdown = guard.scrub(outcome.markdown)
    if guard.count:
        result.post_render_redactions = guard.count
        result.post_render_cause = classify_post_render(result.eventlog_path, guard.captured)
        # Explicit, not left to refcounting. The plaintext is provably unreachable after this
        # point either way -- `guard` is function-local and nothing retains it -- but the
        # retention window is a deliberate, bounded thing and it should look deliberate in the
        # code rather than be implicit in scope rules. Info #1, scribe-release-readiness audit.
        guard.captured.clear()
        result.errors.append(
            f"post-render redaction fired {guard.count}x — a secret was caught at write "
            f"time; {_CAUSE_DETAIL[result.post_render_cause]}"
        )

    # The QC gate runs on whatever was produced, including a placeholder: a run report that
    # silently omits the sessions that failed is the same shape of blind spot this component
    # was built to remove.
    report: Report = check_digest(markdown, log, floor=coverage_floor)
    result.qc_ok = report.ok
    result.qc_coverage = report.coverage
    result.qc_findings = [f"{f.check}: {f.detail}" for f in report.findings]

    path = daily_path(cfg.output_dir, result.agent, when)
    # What kind of body is going to disk, recorded in the block's own anchor. Empty for a real
    # digest. This is what lets a later sweep tell a stand-in from an answer and replace it --
    # before it existed, `append_block` refused the replacement and the digest was discarded.
    if outcome.ok:
        provisional = ""
    elif outcome.suppressed:
        provisional = PROVISIONAL_SUPPRESSED
    else:
        provisional = PROVISIONAL_PLACEHOLDER

    result.written = append_block(
        path,
        body=markdown,
        session_id=log.session_id,
        turn_uuid=_last_turn_uuid(log),
        transcript_path=row.transcript_path,
        when=when,
        provisional=provisional,
    )
    # A digest that was produced and NOT written. `append_block` returns False for a turn that
    # is already final, and the one thing that must never happen quietly is for that to swallow
    # a real summary -- it did, 30 times, and the run totals said `summarized` each time. The
    # replace path should make this unreachable for a stand-in; counting it is how we find out
    # if some other route reaches it. Same argument as the placeholder counter in #849:
    # inferring loss from a difference between two totals is how loss stays invisible.
    result.discarded = bool(not result.written and (outcome.ok or outcome.suppressed))
    if result.discarded:
        result.errors.append(
            "digest produced but not written: the turn already holds a final block"
        )
    if result.written:
        _after_write(cfg, result, path, _last_turn_uuid(log))

    if outcome.ok:
        store.mark_summarized(
            row.transcript_path,
            offset=st.raw_file_bytes,
            last_turn_uuid=_last_turn_uuid(log),
            turn_uuids=pending,
            session_id=log.session_id,
        )
    else:
        # Provisional, not failed and not summarized. A suppression used to be marked
        # `summarized` -- terminal on a session that was never summarized -- and a placeholder
        # used to be marked `failed` with its offset left behind, which re-offered it on every
        # sweep forever. Both are the same mistake from opposite ends: the status did not say
        # what was actually on disk. See `state.STATUS_PROVISIONAL`.
        attempts = store.mark_provisional(
            row.transcript_path,
            offset=st.raw_file_bytes,
            last_turn_uuid=_last_turn_uuid(log),
            session_id=log.session_id,
            error=outcome.reason,
        )
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
    # Two sources, because there are two ways a session can be waiting. `scan` walks the
    # transcripts; `orphaned` picks up the ones a deliberate `scribe recover` reopened whose
    # transcript has since aged out, which `scan` structurally cannot see (vikunja#873).
    ready = scan(cfg, store, now=now) + orphaned(cfg, store)
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


def _merge_truncated(results: list[SessionResult]) -> dict[str, int]:
    """Total items dropped per field across a sweep, largest first.

    Sorted by size rather than by field name because the reason to read this at all is to
    find the cap that is costing the most, and an alphabetical list buries it.
    """
    totals: dict[str, int] = {}
    for r in results:
        for name, dropped in r.truncated_fields.items():
            totals[name] = totals.get(name, 0) + dropped
    return dict(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])))


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
        "discarded": sum(1 for r in results if r.discarded),
        #: Sessions with at least one truncated field, and the total items dropped across the
        #: sweep. Both, because they answer different questions: the first is how often the
        #: declared cap fails to hold, the second is by how much.
        "truncated": sum(1 for r in results if r.truncated_items),
        "truncated_items": sum(r.truncated_items for r in results),
        #: Which fields, and by how much. The scalar above says a cap fired somewhere; this
        #: says which one, and it is the only form of the number that suggests a fix.
        "truncated_fields": _merge_truncated(results),
        #: Truncation split by run type, with the denominators to read it against. A full
        #: read hands the model a whole session and an incremental sweep hands it a growing
        #: one, so a rate pooled across the two describes neither.
        "truncated_full_read": sum(1 for r in results if r.truncated_items and r.full_read),
        "full_read": sum(1 for r in results if r.full_read),
        "events_total": sum(r.events for r in results),
        "secrets_redacted": sum(r.secrets_redacted for r in results),
        "post_render_redactions": sum(r.post_render_redactions for r in results),
        #: Sessions whose drill-down tier now exists. Counted rather than assumed: a digest
        #: written without its event log is a digest that can never be re-checked, and that
        #: is the state this build exists to end.
        "eventlogs_written": sum(1 for r in results if r.eventlog_path),
        #: Blocks recorded in `index.jsonl`, and blocks written that were NOT. The second is
        #: the one to watch: it means the manifest is behind the corpus until a rebuild.
        "manifest_appended": sum(1 for r in results if r.manifest_appended),
        "manifest_errors": sum(1 for r in results if r.manifest_error),
        "hook_ok": sum(1 for r in results if r.hook == "ok"),
        "hook_failed": sum(1 for r in results if r.hook == "failed"),
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
