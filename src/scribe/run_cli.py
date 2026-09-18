"""`python -m scribe run` — one sweep of the pipeline.

Defaults to `--dry-run`. Phase 6 is a shadow run and the live path must not change, so the
mode that costs money and sends data off the machine is the one you have to ask for by name.
`--once` without `--dry-run` is the shadow run proper.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, load
from .pipeline import run_once, summarize_run
from .state import Store
from .telemetry import setup_tracing, shutdown_tracing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe run",
        description="Discover finished sessions, extract them, and optionally summarize.",
    )
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument(
        "--live",
        action="store_true",
        help="actually call the summarization provider (default: dry run, no network)",
    )
    ap.add_argument("--limit", type=int, default=0, help="process at most N sessions")
    ap.add_argument("--state", type=Path, default=None, help="override the state database")
    ap.add_argument("--spend-log", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return 2

    store = Store(args.state or cfg.state_path)
    # `run` is the only subcommand that reaches a span call site, so it is the only one that
    # needs a provider. Without this the three span names exist, the call sites execute, and
    # nothing is ever exported — see `telemetry.setup_tracing`.
    setup_tracing()
    try:
        results = run_once(
            cfg, store, dry_run=not args.live, limit=args.limit, spend_log=args.spend_log
        )
    except ConfigError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return 2
    finally:
        # In `finally`, because `BatchSpanProcessor` exports on a timer and a sweep that
        # raises would otherwise drop every span it had already produced — including the ones
        # describing the failure, which are the ones worth having.
        shutdown_tracing()

    totals = summarize_run(results)
    if args.json:
        json.dump(
            {"totals": totals, "sessions": [r.to_dict() for r in results]},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        mode = "live" if args.live else "dry run"
        print(
            f"{mode}: {totals['sessions']} session(s), {totals['events_total']} events, "
            f"{totals['secrets_redacted']} secrets redacted"
        )
        if totals["qc_graded"]:
            print(
                f"  QC {totals['qc_passed']}/{totals['qc_graded']} passed, "
                f"mean coverage {totals['mean_coverage']:.1%}"
            )
        # Loud, and above the error list: a placeholder is a session whose summary is gone,
        # and the reason this was invisible for a month is that it only ever showed up as a
        # one-off difference between two numbers nobody was subtracting.
        if totals["placeholders"]:
            print(
                f"  !! {totals['placeholders']} placeholder(s) written -- "
                f"{totals['placeholders']} session summar"
                f"{'y' if totals['placeholders'] == 1 else 'ies'} not yet written"
                f" (retryable: `scribe recover --status`)"
            )
        if totals["suppressed"]:
            print(f"  {totals['suppressed']} suppressed (contamination fallback)")
        # Quiet, and deliberately so: a truncated digest is written, so this is degradation
        # and not loss, and it must not read like the `!!` lines below it. But it has to be
        # HERE. `summarize_run` has aggregated `truncated`/`truncated_items` correctly since
        # #884 and this branch never printed either, so the only way to see the counter was
        # `--json`, which the cron does not pass -- `grep -c truncated ~/.pm2/logs/
        # scribe-out.log` returned 0 for the whole period. vikunja#887 asked for the number
        # to be allowed to accumulate and then looked at; it was accumulating into nothing.
        # AGENTS.md invariant 13, one level out: a counter nobody can see is not a counter.
        if totals["truncated"]:
            breakdown = ", ".join(
                f"{dropped} from {name}" for name, dropped in totals["truncated_fields"].items()
            )
            run_types = (
                f"{totals['truncated_full_read']} on a full read"
                if totals["truncated_full_read"]
                else "none on a full read"
            )
            print(
                f"  {totals['truncated']} digest(s) truncated, "
                f"{totals['truncated_items']} item(s) dropped ({breakdown}); {run_types}"
            )
        # Louder than a placeholder, because a placeholder is a session waiting and this is a
        # session whose digest was generated, paid for and thrown away. It happened 30 times
        # and reported as a clean `summarized` every time. It should now be unreachable --
        # printing it is how we find out if it is not.
        if totals["discarded"]:
            print(
                f"  !! {totals['discarded']} digest(s) produced but NOT WRITTEN -- "
                f"{totals['discarded']} session summar"
                f"{'y' if totals['discarded'] == 1 else 'ies'} LOST; "
                f"this should be unreachable, report it"
            )
        if totals["errors"]:
            print(f"  {totals['errors']} error(s)")
            for r in results:
                for err in r.errors:
                    print(f"    {Path(r.transcript_path).name}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
