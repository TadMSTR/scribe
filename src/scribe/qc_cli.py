"""`python -m scribe.qc` — run the deterministic gates and exit non-zero on a failure.

The exit code is the contract. A gate that reports its findings and exits 0 is a report, not
a gate, and CI treats it as a pass.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from .eventlog import log_from_dict
from .qc import DEFAULT_COVERAGE_FLOOR, check_digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe.qc",
        description="Check a digest against the event log it was derived from.",
    )
    ap.add_argument("--digest", type=Path, required=True, help="digest markdown, or a JSON file")
    ap.add_argument("--events", type=Path, required=True, help="event log JSON")
    ap.add_argument("--floor", type=float, default=DEFAULT_COVERAGE_FLOOR)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    for path in (args.digest, args.events):
        if not path.is_file():
            print(f"scribe.qc: not a file: {path}", file=sys.stderr)
            return 2

    try:
        events = json.loads(args.events.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"scribe.qc: {args.events} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    text = args.digest.read_text(encoding="utf-8", errors="replace")
    if args.digest.suffix == ".json":
        # A .json digest is flattened to text for the claim scan. If it is not valid JSON,
        # scan it as-is rather than failing: the gate's job is to judge content, and it can
        # still do that on a malformed file.
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(text), ensure_ascii=False)

    try:
        log = log_from_dict(events)
    except (AttributeError, TypeError, ValueError) as exc:
        # Exit 1 is the gate's FAIL verdict. An unhandled traceback also exits 1, so a
        # malformed event log was indistinguishable from a digest that failed the gate --
        # and CI reads the exit code, not the traceback.
        print(f"scribe.qc: {args.events} is not an event log: {exc}", file=sys.stderr)
        return 2

    report = check_digest(text, log, floor=args.floor)
    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        verdict = "PASS" if report.ok else "FAIL"
        print(
            f"{verdict}  coverage {report.events_referenced}/{report.events_total} "
            f"({report.coverage:.1%})"
        )
        for f in report.findings:
            print(f"  {f.check}: {f.detail}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
