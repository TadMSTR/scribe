"""The run record: one JSON line per session a live sweep finished, with its QC verdict.

scribe has graded every digest since the gate shipped, and until this module it threw the
verdict away. `SessionResult.qc_ok` reached only `--json`, which the cron does not pass, and a
one-line total in a PM2 log that rotates after 14 days. Recovered from those logs on
2026-09-28: 260/554 sessions passed over two weeks, and nobody had seen the number.

So each finished session appends one line here, and `qc-report` reads the lines back as a
trend. Three properties matter more than the field list:

  * **Attributed.** `scribe_version`, `prompt_sha256` and `model_resolved` are what let a
    trend say *what* moved quality rather than only that it moved. A backfilled line cannot
    recover them and carries `null`, never a guess.
  * **Classified by kind and bucket at write time.** Most findings on the live corpus are gate
    artefacts (the `suffix` path bucket, ticket `id-conflation`), and a pooled count moves
    whenever the gate is tuned. `absent` is the hallucination signal; it is recorded as its
    own number so a gate fix cannot read as a model improvement.
  * **Never raises.** Same contract as `telemetry.record_spend`: a failed write is counted
    (`run_record_errors`) and the sweep carries on. It is not an error and does not change the
    exit code -- invariant 13, own field and own total.

Written on `--live` only. The call site sits past `process_session`'s dry-run guard, so a dry
run cannot reach it, and the test suite's `HOME` isolation keeps the default path out of the
operator's home (vikunja#851: 1,182 test records in the real spend log).

Append-only JSONL beside the digests rather than a table in the state DB: the DB is
`SCHEMA_VERSION`-gated with a migration contract, lives in `~/.local/state` (state, not
history), and a flat file is greppable by anyone who doubts the report.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .extract.models import EventLog
from .extract.redact import Redactor
from .paths import secure_dir, secure_file
from .qc import DEFAULT_COVERAGE_FLOOR, Report, check_digest
from .qc_survey import ABSENT, _load, attribute, iter_blocks
from .writeback import PROVISIONAL_PLACEHOLDER, PROVISIONAL_SUPPRESSED

#: Bumped when a field changes meaning. Adding a field does not bump it: readers take what
#: they know and ignore the rest.
RECORD_VERSION = 1
SOURCE_LIVE = "live"
#: Graded after the fact from a digest block and its persisted event log. Attribution the
#: digest does not carry is `null`, and the report never pools these with `live` unless asked.
SOURCE_BACKFILL = "backfill"
SOURCES = (SOURCE_LIVE, SOURCE_BACKFILL)
#: Ungrounded claims kept verbatim per record. The counts are complete; this is the sample a
#: person reads to see what the numbers are made of.
MAX_CLAIMS = 20


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def qc_fields(report: Report, body: str, log: EventLog, *, home: str = "") -> dict:
    """The QC half of a record. Shared by live and backfill so the two cannot grade apart.

    `claims` passes through a fresh `Redactor` -- the same guard the digest went through on
    its way to disk. A claim is a substring of the digest, so it is already scrubbed in every
    case this can see; the second pass costs nothing and means the record's safety does not
    rest on that remaining true.
    """
    attributed = attribute(report, body, log, home=home)
    by_bucket: dict[str, dict[str, int]] = {}
    for kind, bucket, _claim in attributed:
        by_bucket.setdefault(kind, {})
        by_bucket[kind][bucket] = by_bucket[kind].get(bucket, 0) + 1
    guard = Redactor()
    return {
        "qc_ok": report.ok,
        "qc_coverage": round(report.coverage, 4),
        "events_referenced": report.events_referenced,
        "events_total": report.events_total,
        "findings_by_check": dict(Counter(f.check for f in report.findings)),
        "findings_by_kind": dict(Counter(f.kind for f in report.findings if f.kind)),
        "findings_by_bucket": by_bucket,
        "claims": [
            {"check": "groundedness", "kind": kind, "bucket": bucket, "claim": guard.scrub(claim)}
            for kind, bucket, claim in attributed[:MAX_CLAIMS]
        ],
    }


def absent_count(record: dict) -> int:
    """`absent` findings across every kind -- the headline hallucination number."""
    return sum(b.get(ABSENT, 0) for b in (record.get("findings_by_bucket") or {}).values())


def append(path: str | os.PathLike[str], record: dict) -> str:
    """Append one record. Returns "" on success, else why not. Never raises.

    Owner-only, like the spend log and every other file scribe writes: a claim is text lifted
    from a digest.
    """
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n"
        p = Path(os.path.expanduser(str(path)))
        secure_dir(p.parent)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line)
        secure_file(p)
    except (OSError, TypeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


@dataclass
class Loaded:
    """What `read` found: the records, and how many lines were not records."""

    records: list[dict] = field(default_factory=list)
    lines: int = 0
    bad_lines: int = 0


def read(path: str | os.PathLike[str]) -> Loaded:
    """Every record in the file. A missing file is empty; an unreadable one raises `OSError`.

    A line that is not a JSON object is counted, not fatal. A crash mid-append leaves one torn
    last line, and refusing the whole history for it would make the report the component that
    failed. `qc-report` decides how many bad lines are too many.
    """
    out = Loaded()
    p = Path(os.path.expanduser(str(path)))
    if not p.exists():
        return out
    with p.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            out.lines += 1
            try:
                rec = json.loads(raw)
            except ValueError:
                out.bad_lines += 1
                continue
            if not isinstance(rec, dict):
                out.bad_lines += 1
                continue
            out.records.append(rec)
    return out


def _keys(records: list[dict]) -> set[tuple[str, str]]:
    return {(str(r.get("session_id", "")), str(r.get("turn_uuid", ""))) for r in records}


@dataclass
class Backfill:
    blocks: int = 0
    appended: int = 0
    #: Already in the record under this `(session_id, turn_uuid)`, from either source.
    skipped: int = 0
    no_log: int = 0
    write_errors: int = 0
    first_error: str = ""

    def to_dict(self) -> dict:
        return {
            "blocks": self.blocks,
            "appended": self.appended,
            "skipped": self.skipped,
            "no_log": self.no_log,
            "write_errors": self.write_errors,
            "first_error": self.first_error,
        }


def iter_backfill(
    digest_root: str | Path,
    eventlog_root: str | Path,
    known: set[tuple[str, str]],
    *,
    home: str = "",
    floor: float = DEFAULT_COVERAGE_FLOOR,
    stats: Backfill,
) -> Iterator[dict]:
    """A record for every block on disk not already recorded. Pure: writes nothing itself.

    Grades with `check_digest` -- groundedness AND the coverage floor, the same call the live
    path makes -- so `qc_ok` means the same thing in both sources. `qc-survey` grades
    groundedness only, which is why its 21% and a pass rate are different statistics; the
    report shows the survey's number too, from `findings_by_check`, so the two reconcile.
    """
    for block in iter_blocks(digest_root):
        stats.blocks += 1
        key = (block.session_id, block.turn_uuid)
        if key in known:
            stats.skipped += 1
            continue
        log = _load(eventlog_root, block)
        if log is None:
            stats.no_log += 1
            continue
        report = check_digest(block.body, log, floor=floor)
        known.add(key)
        yield {
            "record_version": RECORD_VERSION,
            "ts": _now(),
            "source": SOURCE_BACKFILL,
            "graded_by": __version__,
            # The digest does not say which scribe, prompt or model produced it. `null` is the
            # true answer; a guess would be attribution nobody observed.
            "scribe_version": None,
            "prompt_sha256": None,
            "provider": None,
            "model_requested": None,
            "model_resolved": None,
            "agent": log.agent or None,
            "session_id": block.session_id,
            "turn_uuid": block.turn_uuid,
            "transcript_path": block.transcript_path,
            "digest": block.digest,
            "session_at": log.started_at or log.ended_at or None,
            "full_read": None,
            "replayed": None,
            "turns": log.stats.turns,
            "events": log.stats.tool_events,
            "extracted_chars": log.stats.extracted_chars,
            "token_estimate": log.stats.token_estimate,
            "input_tokens": None,
            "output_tokens": None,
            "degradation_level": log.stats.degradation_level,
            "written": True,
            "suppressed": block.provisional == PROVISIONAL_SUPPRESSED,
            "placeholder": block.provisional == PROVISIONAL_PLACEHOLDER,
            "discarded": False,
            "truncated_items": None,
            "truncated_fields": None,
            "secrets_redacted": log.stats.secrets_redacted,
            "post_render_redactions": None,
            "errors": None,
            **qc_fields(report, block.body, log, home=home),
            "lag_s": None,
        }


def backfill(
    record_path: str | os.PathLike[str],
    digest_root: str | Path,
    eventlog_root: str | Path,
    *,
    home: str = "",
) -> Backfill:
    """Grade every digest block not yet recorded and append it. Idempotent.

    Keyed on `(session_id, turn_uuid)` -- the block's anchor -- against every record already in
    the file, live or backfill, so a re-run appends nothing and a block a live sweep already
    recorded is not counted twice. Raises `OSError` only if the existing record cannot be read;
    a failed append is counted, as it is on the live path.
    """
    stats = Backfill()
    known = _keys(read(record_path).records)
    for record in iter_backfill(digest_root, eventlog_root, known, home=home, stats=stats):
        err = append(record_path, record)
        if err:
            stats.write_errors += 1
            stats.first_error = stats.first_error or err
        else:
            stats.appended += 1
    return stats
