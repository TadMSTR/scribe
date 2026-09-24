"""`python -m scribe index` — regenerate the digest manifest, or check it for drift.

`--rebuild` walks the digest corpus and rewrites `index.jsonl` from the blocks on disk. It is
how the digests written before the manifest existed become visible to a push indexer, and how
a manifest that fell behind (a failed append, a hand edit) is repaired.

`--check` does the same rebuild in memory and compares. That is what makes the manifest
derived state rather than authoritative state: it can always be re-derived and diffed, so it
cannot drift in silence. `written_at` is excluded from the comparison — nothing on disk
records when a line was appended, so comparing it would fail every line forever.

A `--rebuild` racing a live sweep can drop the line that sweep appends between the rebuild's
walk and its rename. Run it when no sweep is running (forge's cron holds `flock -n`), and
`--check` afterwards; a dropped line shows up there as missing.

Exit codes: 0 clean (or rebuilt), 1 drift found, 2 config error, 3 the corpus or manifest
could not be read or written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, load
from .manifest import Drift, check, rebuild, write

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_CONFIG = 2
EXIT_IO = 3


def _drift_dict(d: Drift) -> dict:
    return {
        "clean": d.clean,
        "absent": d.absent,
        "expected": d.expected,
        "lines": d.lines,
        "missing": [list(k) for k in d.missing],
        "stale": [list(k) for k in d.stale],
        "changed": [{"key": list(k), "fields": f} for k, f in d.changed],
        "malformed": [{"line": n, "reason": r} for n, r in d.malformed],
    }


def _print_drift(d: Drift, path: Path) -> None:
    print(f"manifest: {path}")
    print(f"  {d.expected} block(s) on disk, {d.lines} line(s) in the manifest")
    if d.absent:
        print("  !! no manifest file -- every block is missing from it")
    for path_, turn in d.missing:
        print(f"  missing  {path_} turn:{turn}  (block on disk, no manifest line)")
    for path_, turn in d.stale:
        print(f"  stale    {path_} turn:{turn}  (manifest line, no block on disk)")
    for (path_, turn), fields in d.changed:
        print(f"  changed  {path_} turn:{turn}  ({', '.join(fields)})")
    for n, reason in d.malformed:
        print(f"  malformed line {n}: {reason}")
    if d.clean:
        print("  clean")
    else:
        print("  run `scribe index --rebuild` to regenerate it from the digests")
    print("  (written_at is informational and is not verified)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scribe index",
        description="Regenerate the digest manifest (index.jsonl), or check it for drift.",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--rebuild",
        action="store_true",
        help="rewrite the manifest from the digests on disk (run when no sweep is running)",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="rebuild in memory and compare; exit 1 on any difference",
    )
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    path = cfg.manifest_file()
    try:
        if args.rebuild:
            records = rebuild(cfg)
            write(cfg, records)
            if args.json:
                json.dump({"rebuilt": len(records), "manifest": str(path)}, sys.stdout)
                sys.stdout.write("\n")
            else:
                print(f"rebuilt {path}: {len(records)} record(s)")
            return EXIT_OK
        drift = check(cfg)
    except OSError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return EXIT_IO

    if args.json:
        json.dump({"manifest": str(path), **_drift_dict(drift)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_drift(drift, path)
    return EXIT_OK if drift.clean else EXIT_DRIFT


if __name__ == "__main__":
    sys.exit(main())
