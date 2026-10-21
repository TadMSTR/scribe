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

from . import __version__, corrections, runrecord
from .childenv import BASE_ENV, allowlisted_env
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
from .manifest import append, record_for_turn
from .qc import DEFAULT_COVERAGE_FLOOR, Report, check_digest, id_conflation_edits
from .state import STATUS_FAILED, SessionRow, Store
from .summarize.prompt import prompt_sha256
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
    #: Why this session's run record was not written, or "" if it was (or none was due).
    #: **Not in `errors`.** The digest is on disk either way and the record observes it; a
    #: failed observation is counted in `run_record_errors`, per invariant 13, and must not
    #: turn a good sweep into a failed one.
    run_record_error: str = ""
    #: Why this session's corrections-ledger record was not written, or "" if it was (or none
    #: was due -- only a block `rewrite_id_conflations` changed gets one). Counted in
    #: `correction_errors`, never fatal, not in `errors`: the same contract as the run record.
    correction_error: str = ""
    #: `#N` refs rewritten to `id N` after grading -- see `qc.rewrite_id_conflations`. Counted
    #: so a falling `id-conflation` rate after a prompt change can be told apart from the
    #: rewrite silently not running: the first moves this toward 0 too, the second leaves it
    #: at 0 while the bucket stays full.
    ticket_refs_rewritten: int = 0
    #: The provider was down (`Outcome.provider_unavailable`), so the session was left exactly
    #: as it was: no block written, no state touched, no retry budget spent. The next sweep
    #: re-offers it because its offset never moved (#1188 (id 1271) item 4). An outage is not
    #: a property of the session, and `MAX_PROVISIONAL_ATTEMPTS` exists for failures that are.
    deferred: bool = False
    #: Never handed to the provider: an earlier session in this sweep was deferred, and the
    #: sweep stopped calling a provider it had just found down. Untouched, like `deferred`.
    not_attempted: bool = False
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
            "run_record_error": self.run_record_error,
            "correction_error": self.correction_error,
            "ticket_refs_rewritten": self.ticket_refs_rewritten,
            "deferred": self.deferred,
            "not_attempted": self.not_attempted,
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


#: What every hook sees, whatever it is configured with. The same set `claude -p` gets; it is
#: defined once, in `childenv`, so the two children cannot drift apart.
HOOK_BASE_ENV = BASE_ENV


def hook_env(extra: Sequence[str] = ()) -> dict[str, str]:
    """The hook's environment: `HOOK_BASE_ENV` plus the names the operator listed. Nothing else.

    **Not the sweep's own environment.** A sweep runs with the summarizer's credential in it
    (`MISTRAL_API_KEY` on forge), and an indexer has no use for that key. Inheriting by
    default would hand it to whatever binary `on_digest_written` names -- SC-06 in the fleet's
    pattern base, where the recurring form is exactly a new subprocess added without `env=`.
    An indexer that genuinely needs a secret of its own names it in `on_digest_written_env`,
    by exact name, so the grant is visible in the config that makes it.
    """
    return allowlisted_env((*HOOK_BASE_ENV, *extra))


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
    try:
        errf = tempfile.TemporaryFile()  # noqa: SIM115 -- entered by `with errf:` below
    except OSError as exc:
        # The digest is already on disk; a full /tmp must cost the hook, not the sweep.
        return f"could not start: no temp file for stderr ({exc})"
    with errf:
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


@dataclass
class AfterWrite:
    """What `after_write` did to one file. Every failure here is counted, never raised."""

    frontmatter_error: str = ""
    #: turn uuid -> its `index.jsonl` record, for every block whose line was appended.
    manifest_records: dict[str, dict] = field(default_factory=dict)
    manifest_errors: dict[str, str] = field(default_factory=dict)
    #: Ledger records appended, and turn uuid -> why one was not.
    corrections_appended: int = 0
    correction_errors: dict[str, str] = field(default_factory=dict)
    #: "" when no hook is configured, else "ok" or "failed".
    hook: str = ""
    hook_error: str = ""


def after_write(cfg: Config, path: Path, blocks: Sequence[tuple[str, dict | None]]) -> AfterWrite:
    """Everything that follows blocks reaching disk in one file. None of it can unwrite them.

    `blocks` is `(turn_uuid, correction)` per block just written, where `correction` is the
    `corrections.record` keyword arguments for a block scribe edited after the model wrote it
    -- everything but `digest` and `sha256_after`, which are only known here -- or None.

    **The one sequence for every writer.** The sweep calls it with one block; `scribe
    correct` with every block it changed in the file. Shared rather than copied, because two
    writers that drift in what follows a write is exactly the failure invariant 17 describes:
    a block on disk with no `index.jsonl` line, and a `--check` red on correct data.

    Order is load-bearing:

      1. **Frontmatter**, once per file, because it rewrites the file and a hook must be
         handed the finished file. It does not touch blocks, so no hash below moves.
      2. **The manifest line**, per block, so `scribe index --check` stays clean.
      3. **The ledger record**, per corrected block, after the manifest because its
         `sha256_after` is that line's `sha256` -- the same hash, derived once.
      4. **The hook**, once per file, last: it is the notification and the manifest is the
         payload it points at, so a consumer woken by it must find its line already there.
    """
    done = AfterWrite()
    if cfg.emit_frontmatter:
        try:
            apply_frontmatter(path)
        except OSError as exc:
            done.frontmatter_error = str(exc)
    ledger = cfg.corrections_file()
    for turn_uuid, correction in blocks:
        rec: dict | None = None
        try:
            rec = record_for_turn(cfg, path, turn_uuid)
            append(cfg, rec)
            done.manifest_records[turn_uuid] = rec
        except (OSError, LookupError, ValueError) as exc:
            done.manifest_errors[turn_uuid] = str(exc)
        if correction is None or ledger is None:
            continue
        try:
            entry = corrections.record(
                **correction,
                digest=rec["path"] if rec else _relative(cfg, path),
                sha256_after=rec["sha256"] if rec else None,
            )
        except (TypeError, ValueError) as exc:
            done.correction_errors[turn_uuid] = f"{type(exc).__name__}: {exc}"
            continue
        err = corrections.append(ledger, entry)
        if err:
            done.correction_errors[turn_uuid] = err
        else:
            done.corrections_appended += 1
    if cfg.on_digest_written:
        done.hook_error = run_hook(
            cfg.on_digest_written,
            path,
            cfg.on_digest_written_timeout_seconds,
            env=hook_env(cfg.on_digest_written_env),
        )
        done.hook = "failed" if done.hook_error else "ok"
    return done


def _after_write(
    cfg: Config,
    result: SessionResult,
    path: Path,
    turn_uuid: str,
    correction: dict | None = None,
) -> None:
    """`after_write` for the sweep's one block, folded into its `SessionResult`."""
    done = after_write(cfg, path, [(turn_uuid, correction)])
    if done.frontmatter_error:
        result.errors.append(f"frontmatter: {done.frontmatter_error}")
    result.manifest_appended = turn_uuid in done.manifest_records
    result.manifest_error = done.manifest_errors.get(turn_uuid, "")
    result.correction_error = done.correction_errors.get(turn_uuid, "")
    result.hook, result.hook_error = done.hook, done.hook_error


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
    allow_defer: bool = True,
) -> SessionResult:
    """Run one session all the way through. Never raises for an expected failure.

    `allow_defer=False` takes the provisional path even when the provider looked down: the
    re-run `run_once` makes for a deferral the next session showed was not an outage.

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
    except (OSError, ValueError) as exc:  # ValueError: an unencodable string (audit F-03)
        result.errors.append(f"eventlog: {exc}")

    # What the post-grading rewrite changed, for the corrections ledger. Empty when it is off
    # or changed nothing, which is what keeps a ledger line off every uncorrected block.
    edits: list[dict] = []
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

        if outcome.provider_unavailable and allow_defer:
            # Return BEFORE the block and before any store write. No placeholder: it would
            # occupy the turn on disk for a session that is merely late. No `mark_provisional`:
            # that spends one of `MAX_PROVISIONAL_ATTEMPTS` and advances the offset, so a few
            # hours of outage would retire every pending session until a manual `recover`.
            # Leaving the row alone is what makes the next sweep re-offer it for free.
            set_span_attributes(sp, deferred=True)
            result.deferred = True
            result.placeholder = False
            result.errors.append(
                f"provider unavailable, session left pending for the next sweep: {outcome.reason}"
            )
            return result

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

        # Classified here, once, for both the span and the run record: kinds and buckets are
        # the unit of the trend, and `absent` is the only bucket that says the MODEL invented
        # something rather than the gate being strict about how it was written.
        try:
            qc = runrecord.qc_fields(report, markdown, log)
        except Exception as exc:  # the record observes the sweep; it must never abort it
            qc = None
            result.run_record_error = f"classify: {type(exc).__name__}: {exc}"

        # Strictly AFTER both gradings above, and that order is the point: `report` and `qc`
        # measure the model's own output, and the `id-conflation` count is how the prompt's
        # `#N (id M)` rule gets evaluated. Rewriting first would grade scribe's correction and
        # read zero whatever the model did. Only what reaches disk is corrected.
        if cfg.qc_rewrite_id_conflations:
            # `first_line=2`: the body follows the anchor, which is the block's line 1.
            markdown, edits = id_conflation_edits(markdown, report, first_line=2)
            result.ticket_refs_rewritten = len(edits)
        # On the summarize span rather than a new one: the dashboards query these names, and
        # a QC time series belongs on the span that produced the digest it grades.
        set_span_attributes(
            sp,
            **{
                "scribe.qc.ok": report.ok,
                "scribe.qc.coverage": round(report.coverage, 4),
                "scribe.qc.findings": len(report.findings),
                "scribe.qc.findings.absent": runrecord.absent_count(qc) if qc else None,
                "scribe.qc.ticket_refs_rewritten": result.ticket_refs_rewritten,
            },
        )

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
        correction = None
        if edits:
            # `sha256_before` is null on `live`: the unrewritten block never reached disk.
            correction = {
                "source": corrections.SOURCE_LIVE,
                "session_id": log.session_id,
                "turn_uuid": _last_turn_uuid(log),
                "edits": edits,
                "sha256_before": None,
            }
        _after_write(cfg, result, path, _last_turn_uuid(log), correction)
    if cfg.qc_run_record and qc is not None:
        result.run_record_error = runrecord.append(
            cfg.qc_run_record,
            _run_record(result, row, log, outcome, provider, qc, digest=_relative(cfg, path)),
        )

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


def _relative(cfg: Config, path: Path) -> str:
    """The digest's path under the output root, as `qc_survey.iter_blocks` names it."""
    try:
        return str(path.expanduser().relative_to(output_root(cfg)))
    except ValueError:
        return str(path)


def _run_record(
    result: SessionResult,
    row: SessionRow,
    log: EventLog,
    outcome: Outcome,
    provider: Provider,
    qc: dict,
    *,
    digest: str,
) -> dict:
    """One live record. Every value is one the pipeline already holds -- nothing is re-derived.

    `lag_s` runs from the transcript's last write as discovery observed it (`row.mtime_ns`) to
    now, which is within a second of the digest reaching disk. For a replayed session that is
    the transcript's last write before it aged out, which is still the true lag.
    """
    lag = round(datetime.now(UTC).timestamp() - row.mtime_ns / 1e9, 1) if row.mtime_ns else None
    return {
        "record_version": runrecord.RECORD_VERSION,
        "ts": runrecord._now(),
        "source": runrecord.SOURCE_LIVE,
        "graded_by": __version__,
        "scribe_version": __version__,
        "prompt_sha256": prompt_sha256(),
        "provider": outcome.provider or provider.name,
        "model_requested": getattr(provider, "model", "") or None,
        # What the response said, or null. Never back-filled from the request: see
        # `Completion.model_resolved`.
        "model_resolved": outcome.model_resolved or None,
        "agent": result.agent,
        "session_id": result.session_id,
        "turn_uuid": _last_turn_uuid(log),
        "transcript_path": result.transcript_path,
        "digest": digest,
        "session_at": log.started_at or log.ended_at or None,
        "full_read": result.full_read,
        "replayed": result.replayed,
        "turns": result.turns,
        "events": result.events,
        "extracted_chars": result.extracted_chars,
        "token_estimate": result.token_estimate,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "degradation_level": result.degradation_level,
        "written": result.written,
        "suppressed": result.suppressed,
        "placeholder": result.placeholder,
        "discarded": result.discarded,
        "truncated_items": result.truncated_items,
        "truncated_fields": result.truncated_fields,
        "secrets_redacted": result.secrets_redacted,
        "post_render_redactions": result.post_render_redactions,
        "errors": len(result.errors),
        **qc,
        "ticket_refs_rewritten": result.ticket_refs_rewritten,
        "lag_s": lag,
    }


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

    def run(row: SessionRow, *, allow_defer: bool = True) -> SessionResult:
        return process_session(
            row,
            cfg,
            store,
            provider=provider,
            dry_run=dry_run,
            spend_log=spend_log,
            allow_defer=allow_defer,
        )

    # The circuit breaker. Once the provider is down, every later session would make its own
    # 3 calls to reach the same verdict, so they are left untouched and re-offered next sweep.
    # It covers an endpoint that refuses or drops the connection, or answers 429/5xx. It does
    # NOT cover one that accepts the connection and never answers: a read timeout is retryable
    # but not `unavailable` (see `providers.py`), so each such session spends its 3 x timeout
    # and then provisional budget (audit R2-03).
    #
    # **It trips on TWO deferrals in a row, not one** (audit F-01). One session deferring
    # cannot tell an outage from a fault in that session's own request, and on one deferral a
    # single poison session would stop every session behind it, sweep after sweep, while the
    # run blamed the provider. So the next session is the probe: if it also finds the provider
    # down, the outage is confirmed and both stay pending. If the provider answers it, the
    # first deferral was not an outage, and that session is re-run with `allow_defer=False` --
    # onto the provisional path, where `MAX_PROVISIONAL_ATTEMPTS` bounds it.
    #
    # **Evidence counts from either side of the suspect** (audit R2-01). A probe that only
    # looked forward left a suspect with nothing after it -- the last or only ready session,
    # and every orphan, which `orphaned()` appends last -- deferred on every sweep forever. So
    # `provider_seen_up` remembers whether the provider answered ANY session this sweep, and a
    # suspect still pending when the loop ends is re-run if it did. The cost, accepted: an
    # outage that starts just before the last session spends one provisional attempt on it.
    #
    # SECURITY[accepted]: two adjacent suspects trip the breaker.
    # Audit: 2026-10-21/scribe-qc-integrity-2026-10 R2-01.
    # **Residual, accepted (Ted, 2026-10-21):** two suspects in a row with no answered session
    # before them still trip the breaker. Telling that from an outage would need a per-session
    # deferral count in the state DB. It takes two sessions whose own requests each make the
    # endpoint refuse, drop or 5xx, which `unavailable`'s narrow definition makes speculative.
    results: list[SessionResult] = []
    tripped = False
    provider_seen_up = False
    suspect: tuple[int, SessionRow] | None = None
    for row in ready:
        if tripped:
            results.append(
                SessionResult(
                    transcript_path=row.transcript_path,
                    agent=row.agent,
                    not_attempted=True,
                    full_read=row.last_offset == 0,
                )
            )
            continue
        result = run(row)
        results.append(result)
        if result.deferred:
            if suspect is None:
                suspect = (len(results) - 1, row)
            else:
                tripped = True
        elif _provider_answered(result):
            provider_seen_up = True
            if suspect is not None:
                index, suspect_row = suspect
                results[index] = run(suspect_row, allow_defer=False)
                suspect = None
    if suspect is not None and provider_seen_up and not tripped:
        index, suspect_row = suspect
        results[index] = run(suspect_row, allow_defer=False)
    return results


def _provider_answered(result: SessionResult) -> bool:
    """Whether this session reached the provider and got an answer, good or bad.

    A session with nothing to summarize, or one whose extraction failed, never called the
    provider, so it says nothing about whether the provider is up and cannot clear a suspect.
    """
    return result.summarized or result.suppressed or result.placeholder


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
        "run_record_errors": sum(1 for r in results if r.run_record_error),
        "correction_errors": sum(1 for r in results if r.correction_error),
        #: `#N` refs corrected to `id N` across the sweep. Not an outcome and not loss: every
        #: digest it counts was written, with the wrong ref fixed.
        "ticket_refs_rewritten": sum(r.ticket_refs_rewritten for r in results),
        #: The provider was down: the session that found it so, and the ones the sweep then
        #: did not hand to it. Both left pending with no budget spent. Non-zero drives
        #: `run_cli.EXIT_PROVIDER_UNAVAILABLE`.
        "deferred": sum(1 for r in results if r.deferred),
        "not_attempted": sum(1 for r in results if r.not_attempted),
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
