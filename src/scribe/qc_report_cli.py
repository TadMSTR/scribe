"""`python -m scribe qc-report` — the QC trend over the run record, with an exit-code contract.

Exit codes. Each is distinct, and no success path returns 1, so a scheduler can route 1 to an
alert without being paged by a report that merely ran (#875, #890):

  0  no threshold breached (or none set: report-only)
  1  a regression threshold was breached
  2  config or usage error
  3  insufficient data: fewer than --min-sessions graded sessions, so NO verdict. Not a pass.
  4  tool failure: the record is unreadable, or too many lines are corrupt. Says nothing
     about quality.

`--backfill` grades every digest block already on disk and appends it to the record as
`source: backfill`, then exits (0, or 4 if the record could not be read or written). It is
idempotent, keyed on each block's anchor. The report reads `live` records unless told
otherwise: a backfill is a baseline, and pooling it into the live trend would dilute both.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import runrecord
from .config import QC_THRESHOLD_KEYS, ConfigError, check_threshold, load
from .qc_report import (
    EXIT_INSUFFICIENT,
    EXIT_OK,
    EXIT_REGRESSION,
    EXIT_TOOL_FAILURE,
    EXIT_USAGE,
    GROUP_FIELDS,
    SOURCE_ALL,
    build,
    parse_ts,
)

#: A crash mid-append can tear the last line, so a small number of bad lines is survivable.
#: More than this share of the file (and more than one line) is a record the report cannot
#: vouch for, and it says so with exit 4 rather than reporting on what is left.
MAX_BAD_FRACTION = 0.01

EPILOG = """\
exit codes:
  0  no threshold breached (or none set: report-only)
  1  a regression threshold was breached
  2  config or usage error
  3  insufficient data (fewer than --min-sessions graded sessions): no verdict, NOT a pass
  4  tool failure (unreadable record, too many corrupt lines): says nothing about quality

statistics:
  pass_rate          per session, every check including the event-coverage floor
  groundedness rate  per block, groundedness only (what qc-survey calls failure_rate)
  These are different numbers. Do not compare one with the other.
"""


def _flag(key: str) -> str:
    return "--" + key.replace("_", "-")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m scribe qc-report",
        description="Report QC results from the run record as a trend, by kind and bucket.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--run-record", default=None, help="override [qc] run_record")
    ap.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    ap.add_argument("--until", default=None, help="window end, ISO 8601 (default: now)")
    ap.add_argument(
        "--compare", action="store_true", help="also report the previous window of equal length"
    )
    ap.add_argument(
        "--by",
        action="append",
        default=[],
        choices=sorted(GROUP_FIELDS),
        help="break down by this attribute; repeatable",
    )
    ap.add_argument(
        "--source",
        default=runrecord.SOURCE_LIVE,
        choices=(*runrecord.SOURCES, SOURCE_ALL),
        help="which records to read (default: live)",
    )
    ap.add_argument(
        "--min-sessions",
        type=int,
        default=None,
        help="graded sessions needed for a verdict (default: [qc] min_sessions, else 20)",
    )
    for key in QC_THRESHOLD_KEYS:
        ap.add_argument(_flag(key), type=float, default=None, dest=key)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="grade every digest block on disk into the record (source: backfill), then exit",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception as exc:  # the contract: a crash is a tool failure, never a verdict
        print(f"scribe qc-report: tool failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILURE


def _main(argv: list[str] | None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load(args.config)
        thresholds = dict(cfg.qc_thresholds)
        for key in QC_THRESHOLD_KEYS:
            value = getattr(args, key)
            if value is not None:
                thresholds[key] = check_threshold(key, value, where="--")
    except ConfigError as exc:
        print(f"scribe qc-report: {exc}", file=sys.stderr)
        return EXIT_USAGE
    record_path = cfg.qc_run_record if args.run_record is None else args.run_record
    if not record_path:
        print("scribe qc-report: [qc] run_record is empty (disabled)", file=sys.stderr)
        return EXIT_USAGE
    if args.days < 1:
        print("scribe qc-report: --days must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    min_sessions = cfg.qc_min_sessions if args.min_sessions is None else args.min_sessions
    if min_sessions < 1:
        print("scribe qc-report: --min-sessions must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    end = datetime.now(UTC) if args.until is None else parse_ts(args.until)
    if end is None:
        print(f"scribe qc-report: --until {args.until!r} is not an ISO 8601 time", file=sys.stderr)
        return EXIT_USAGE

    if args.backfill:
        return _backfill(record_path, cfg.output_dir, cfg.eventlog_dir, as_json=args.json)

    try:
        loaded = runrecord.read(record_path)
    except OSError as exc:
        print(f"scribe qc-report: cannot read {record_path}: {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILURE
    if loaded.bad_lines > 1 and loaded.bad_lines > loaded.lines * MAX_BAD_FRACTION:
        print(
            f"scribe qc-report: {loaded.bad_lines} of {loaded.lines} lines in {record_path} "
            f"are not records; refusing to report on the remainder",
            file=sys.stderr,
        )
        return EXIT_TOOL_FAILURE

    report, verdict = build(
        loaded.records,
        days=args.days,
        end=end,
        source=args.source,
        by=args.by,
        compare=args.compare,
        thresholds=thresholds,
        min_sessions=min_sessions,
    )
    report["record"] = {"path": record_path, "lines": loaded.lines, "bad_lines": loaded.bad_lines}
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print(report, verdict.code)
    return verdict.code


def _backfill(record_path: str, output_dir: str, eventlog_dir: str, *, as_json: bool) -> int:
    try:
        result = runrecord.backfill(record_path, output_dir, eventlog_dir)
    except OSError as exc:
        print(f"scribe qc-report: cannot read {record_path}: {exc}", file=sys.stderr)
        return EXIT_TOOL_FAILURE
    if as_json:
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(
            f"backfill: {result.blocks} block(s) on disk, {result.appended} appended, "
            f"{result.skipped} already recorded, {result.no_log} without an event log"
        )
        if result.write_errors:
            print(f"  !! {result.write_errors} record(s) not written: {result.first_error}")
    return EXIT_TOOL_FAILURE if result.write_errors else EXIT_OK


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None, fmt: str = ".2f") -> str:
    return "n/a" if value is None else format(value, fmt)


def _secs(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}s"


def _section(label: str, s: dict) -> list[str]:
    g, f = s["groundedness"], s["findings"]
    tr = s["truncation"]
    lines = [
        f"{label}: {s['sessions']} session(s), {s['graded']} graded, "
        f"{s['placeholders']} placeholder(s) excluded",
        f"  pass rate (per session, all checks)   {_pct(s['pass_rate'])}  "
        f"({s['passed']}/{s['graded']})",
        f"  coverage mean / p10                  {_pct(s['coverage_mean'])} / "
        f"{_pct(s['coverage_p10'])}; below floor {s['coverage_floor_failed']} "
        f"({_pct(s['coverage_floor_failed_share'])})",
        f"  groundedness (per block)             {g['blocks_failing']} failing "
        f"({_pct(g['failure_rate'])})",
        f"  findings                             {f['total']} "
        f"({_num(f['per_session'])} per session); absent {f['absent_total']} "
        f"({_num(f['absent_per_session'])} per session)",
    ]
    for kind, buckets in f["by_bucket"].items():
        parts = ", ".join(f"{b} {n}" for b, n in buckets.items())
        lines.append(f"    {kind:<11} {f['by_kind'].get(kind, 0):>5}   {parts}")
    lines.append(
        f"  truncation   full read {tr['full_read']['truncated']}/{tr['full_read']['sessions']}"
        f", incremental {tr['incremental']['truncated']}/{tr['incremental']['sessions']}"
        + (f", unknown {tr['unknown']}" if tr["unknown"] else "")
    )
    lines.append(
        f"  tokens in/out {s['input_tokens']}/{s['output_tokens']}   "
        f"lag p50/p95 {_secs(s['lag_s_p50'])}/{_secs(s['lag_s_p95'])}   "
        f"suppressed {s['suppressed']}"
    )
    models = ", ".join(f"{m} ({n})" for m, n in s["models_resolved"].items()) or "none recorded"
    lines.append(f"  models resolved {models}")
    if s["discarded"]:
        lines.append(f"  !! {s['discarded']} digest(s) produced but not written")
    if s["post_render_redactions"]:
        lines.append(
            f"  !! {s['post_render_redactions']} post-render redaction(s): a secret-shaped "
            f"string reached a rendered digest"
        )
    return lines


def _print(report: dict, code: int) -> None:
    w = report["window"]
    print(f"qc-report: source {report['source']}, {w['days']} day(s) to {w['end']}")
    for line in _section("current", report["current"]):
        print(line)
    if report["previous"] is not None:
        for line in _section("previous", report["previous"]):
            print(line)
    for grp in report["groups"]:
        key = ", ".join(
            f"{k}={(str(v)[:12] if k == 'prompt' and v else v)}" for k, v in grp["key"].items()
        )
        c = grp["current"]
        print(
            f"  [{key}] {c['graded']} graded, pass {_pct(c['pass_rate'])}, "
            f"absent/session {_num(c['findings']['absent_per_session'])}"
        )
    if report["new_models"]:
        print(f"  new model value(s) this window: {', '.join(report['new_models'])}")
    for b in report["breaches"]:
        print(f"  !! {b}")
    for u in report["unchecked"]:
        print(f"  unchecked: {u}")
    verdicts = {
        EXIT_OK: "ok" if report["thresholds"] else "ok (report-only: no thresholds set)",
        EXIT_REGRESSION: "REGRESSION",
        EXIT_INSUFFICIENT: "insufficient data -- no verdict",
    }
    print(f"verdict: {verdicts.get(code, code)} (exit {code})")


if __name__ == "__main__":
    sys.exit(main())
