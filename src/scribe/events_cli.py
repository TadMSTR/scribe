"""`python -m scribe events <session-id>` — resolve a digest back to its evidence.

This is the third tier made reachable. A digest block carries its session id in the anchor
comment `writeback.anchor` emits, so going from a line in a digest to the event log behind it
is a **lookup**, not a search:

    grep -o 'session:[^ ]*' <digest.md>      # the id is already there
    python -m scribe events <that-id> --path # where its evidence lives
    python -m scribe qc --digest <digest.md> --events "$(!!)"

`--path` exists so the answer can be fed straight to `scribe qc --events` without a caller
parsing anything out of the output.

Exit codes are the contract, as everywhere else here: 0 found, 1 no event log for that
session, 2 a usage or config problem. "No event log" is distinct from "could not look" —
collapsing them is how a missing drill-down tier would read as a broken command.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import ConfigError, load
from .eventlog import read_eventlog


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe events",
        description="Print the persisted event log for one session.",
    )
    ap.add_argument("session_id", help="session id, as carried in a digest's anchor comment")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument(
        "--eventlog-dir",
        default=None,
        help="override the configured event log root",
    )
    ap.add_argument(
        "--path",
        action="store_true",
        help="print only the path, for piping into `scribe qc --events`",
    )
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return 2

    root = args.eventlog_dir or cfg.eventlog_dir
    path = read_eventlog(root, args.session_id)
    if path is None:
        print(
            f"scribe: no event log for session {args.session_id!r} under {Path(root).expanduser()}",
            file=sys.stderr,
        )
        return 1

    if args.path:
        print(path)
        return 0

    # Streamed rather than read into memory: an event log runs to ~275,000 characters at the
    # top of the measured corpus, and the common use of this verb is piping it into `jq`.
    with path.open("r", encoding="utf-8") as fh:
        shutil.copyfileobj(fh, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
