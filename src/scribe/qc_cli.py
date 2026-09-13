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

from .extract.models import EventLog, Rollup, Stats, ToolEvent, Turn
from .qc import DEFAULT_COVERAGE_FLOOR, check_digest


def _log_from_dict(data: dict) -> EventLog:
    """Rebuild an `EventLog` from a serialized event log.

    Only the fields the gates read are reconstructed. Anything else would be dead weight
    that has to be kept in step with the schema for no benefit.
    """
    log = EventLog(
        session_id=str(data.get("session_id", "")),
        transcript_path=str(data.get("transcript_path", "")),
        stats=Stats(),
    )
    rollup = data.get("rollup") or {}
    known = set(Rollup.__dataclass_fields__)
    log.rollup = Rollup(
        **{k: [str(x) for x in v] for k, v in rollup.items() if k in known and isinstance(v, list)}
    )
    for t in data.get("turns") or []:
        turn = Turn(turn_uuid=str(t.get("turn_uuid", "")), index=int(t.get("index", 0)))
        for e in t.get("events") or []:
            turn.events.append(
                ToolEvent(
                    seq=int(e.get("seq", 0)),
                    tool=str(e.get("tool", "")),
                    kind=str(e.get("kind", "")),
                    target=str(e.get("target", "")),
                    ok=e.get("ok"),
                )
            )
        log.turns.append(turn)
    return log


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

    report = check_digest(text, _log_from_dict(events), floor=args.floor)
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
