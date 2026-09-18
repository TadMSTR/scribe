"""`python -m scribe.qc_survey` — measure the groundedness gate against the live corpus.

Read-only by construction: it opens digests and event logs, and writes only to stdout or to a
path given explicitly with `--out`. **It never rewrites a digest.** This build grades; a QC
change that mutates the corpus is the one way it could do damage.

Exit 0 whatever it finds. This reports on a gate, it is not one — exiting non-zero on a high
failure rate would make the instrument that diagnoses a crying-wolf gate into a second one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_EVENTLOG_DIR, DEFAULT_OUTPUT_DIR
from .qc_survey import BUCKETS, RULE_CURRENT, RULES, cross_session_probe, survey


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe.qc_survey",
        description="Grade every digest block against its own event log and classify "
        "every path claim the groundedness gate rejects.",
    )
    ap.add_argument("--digests", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    ap.add_argument("--events", type=Path, default=Path(DEFAULT_EVENTLOG_DIR))
    ap.add_argument("--home", default="", help="override $HOME for the expansion test")
    ap.add_argument("--limit", type=int, default=0, help="stop after N blocks (0 = all)")
    ap.add_argument("--bucket", default="", help="print only the claims in one bucket")
    ap.add_argument(
        "--rule",
        default=RULE_CURRENT,
        choices=sorted(RULES),
        help="which grounding rule to grade with",
    )
    ap.add_argument(
        "--probe", action="store_true", help="derive the segment floors instead of grading"
    )
    ap.add_argument("--out", type=Path, help="write the full JSON result here")
    ap.add_argument("--json", action="store_true", help="print the full JSON result")
    args = ap.parse_args(argv)

    if args.bucket and args.bucket not in BUCKETS:
        print(f"scribe.qc_survey: unknown bucket {args.bucket!r}", file=sys.stderr)
        return 2

    if args.probe:
        probe = cross_session_probe(args.digests, args.events)
        if args.out:
            args.out.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
        print(
            f"foreign claims tested: {probe['foreign_claims']}    "
            f"own failing claims: {probe['own_failing_claims']}"
        )
        print(f"  {'prefix':>6} {'tail':>4}  {'false-ground':>14}  {'true-ground':>13}")
        for f in probe["floors"]:
            print(
                f"  {f['min_prefix']:>6} {f['min_tail']:>4}  "
                f"{f['false_ground']:>6} {f['false_ground_rate']:>7.1%}  "
                f"{f['true_ground']:>5} {f['true_ground_rate']:>7.1%}"
            )
        sh = probe["shipped"]
        print(
            f"  shipped (prefix {sh['min_prefix']}, tail {sh['min_tail']}, "
            f"window {sh['window']}): "
            f"false {sh['false_ground']} ({sh['false_ground_rate']:.2%})  "
            f"true {sh['true_ground']} ({sh['true_ground_rate']:.1%})"
        )
        return 0

    result = survey(args.digests, args.events, home=args.home, limit=args.limit, rule=args.rule)
    payload = result.to_dict()

    if args.out:
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.bucket:
        for v in result.verdicts:
            if v.bucket == args.bucket:
                print(f"{v.digest}  {v.session_id[:8]}  {v.suffix_segments}  {v.claim}")
        return 0

    print(f"rule: {result.rule}    gate disagreements: {result.gate_disagreements}")
    print(
        f"blocks graded: {result.blocks}    "
        f"with >=1 finding: {result.blocks_failing} ({result.failure_rate:.1%})    "
        f"no event log: {result.blocks_no_log}"
    )
    print(f"findings: {result.findings_total} total, {result.findings_path} path claims")
    for bucket in BUCKETS:
        n = result.buckets.get(bucket, 0)
        share = n / result.findings_path if result.findings_path else 0.0
        print(f"  {bucket:<16} {n:>5}  {share:6.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
