"""`python -m scribe.qc` — run the deterministic gates and exit non-zero on a failure.

The exit code is the contract. A gate that reports its findings and exits 0 is a report, not
a gate, and CI treats it as a pass.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import MISSING
from pathlib import Path

from .extract.models import EventLog, Rollup, Stats, ToolEvent, Turn
from .qc import DEFAULT_COVERAGE_FLOOR, check_digest


def _rebuild(cls, data: dict, **overrides):
    """Reconstruct one dataclass from its own `to_dict` output, field by field.

    Driven by `__dataclass_fields__` rather than a hand-written list. The hand-written
    version drifted: it rebuilt the rollup and the event skeleton but dropped `user_text`,
    `assistant_text` and `result_digest`, so this CLI graded the *same* digest against the
    *same* log more strictly than the pipeline did -- the model's own turns and every tool
    result were missing from the corpus, and true claims drawn from them read as ungrounded.
    Since the CLI is what CI runs, that is the path that would have reported the phantom
    findings. Deriving the field list from the dataclass means adding a field to the schema
    cannot silently reintroduce it.
    """
    kwargs = {}
    for name, spec in cls.__dataclass_fields__.items():
        if name in overrides:
            kwargs[name] = overrides[name]
            continue
        want = spec.type if isinstance(spec.type, str) else ""
        required = spec.default is MISSING and spec.default_factory is MISSING
        if name not in data:
            # A field the dataclass has no default for must still be supplied, or an event log
            # missing `session_id` or a turn missing `turn_uuid` raises TypeError instead of
            # being graded. `to_dict` omits empty values, so this is an ordinary round trip,
            # not a malformed-input case.
            if required:
                kwargs[name] = 0 if want.startswith("int") else ""
            continue
        value = data[name]
        if want.startswith("list["):
            kwargs[name] = [str(x) for x in value] if isinstance(value, list) else []
        elif want.startswith("int"):
            kwargs[name] = int(value)
        elif want.startswith("str"):
            kwargs[name] = str(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def _log_from_dict(data: dict) -> EventLog:
    """Rebuild an `EventLog` from a serialized event log.

    Everything `EventLog.grounding_text()` reads must be reconstructed, because that text is
    the ground truth the groundedness gate checks against. See `_rebuild`.
    """
    turns = []
    for t in data.get("turns") or []:
        turn: Turn = _rebuild(Turn, t, rollup=_rebuild(Rollup, t.get("rollup") or {}), events=[])
        for e in t.get("events") or []:
            turn.events.append(_rebuild(ToolEvent, e))
        turns.append(turn)
    return _rebuild(
        EventLog,
        data,
        turns=turns,
        rollup=_rebuild(Rollup, data.get("rollup") or {}),
        stats=Stats(),
    )


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
        log = _log_from_dict(events)
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
