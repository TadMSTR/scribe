"""`python -m scribe cap-survey` — re-measure the input distribution behind the schema caps.

Run this before changing `MAX_DONE_ITEMS`, `MAX_ROLLUP_ITEMS` or any `headroom` in
`schema._LIST_FIELDS`. The numbers in those comments are dated; this is how the date gets
moved without the measurement becoming folklore.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cap_survey import load_logs, ratios, survey
from .config import ConfigError, load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe cap-survey",
        description="Measure the event-log input distribution the schema caps derive from.",
    )
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--eventlogs", type=Path, default=None, help="override the event-log directory")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = args.eventlogs
    if root is None:
        try:
            root = Path(load(args.config).eventlog_dir)
        except ConfigError as exc:
            print(f"scribe: {exc}", file=sys.stderr)
            return 2

    logs = load_logs(root)
    if not logs:
        print(f"scribe: no event logs under {root}", file=sys.stderr)
        return 1

    rows = survey(logs)
    shares = ratios(logs)

    if args.json:
        json.dump(
            {"logs": len(logs), "fields": [r.to_dict() for r in rows], "shares": shares},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    print(f"{len(logs)} event log(s) under {root}\n")
    print(
        f"{'field':10} {'denominator':34} {'med':>5} {'p99':>5} {'max':>5} "
        f"{'floor':>6} {'cap max':>8} {'raised':>7}"
    )
    for r in rows:
        print(
            f"{r.field:10} {r.denominator:34} {r.median:5} {r.p99:5} {r.maximum:5} "
            f"{r.floor:6} {r.max_cap:8} {r.above_floor:4}/{r.n}"
        )
    print("\nCandidate denominators as a share of stats.tool_events:")
    for label, s in shares.items():
        print(f"  {label:34} median {s['median']:.3f}  p99 {s['p99']:.3f}  max {s['max']:.3f}")
    print(
        "\n`rollup.commands` is shown to keep its unsuitability visible, not because anything\n"
        "uses it: `schema.py` named it as `done`'s ceiling for three builds while 45% of\n"
        "written blocks exceeded it. A denominator that small a share of the work is not one."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
