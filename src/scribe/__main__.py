"""`python -m scribe` — dispatch to a subcommand."""

from __future__ import annotations

import sys

USAGE = "usage: python -m scribe {extract|events|journal|qc|run} ..."


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "extract":
        from .extract.__main__ import main as extract_main

        return extract_main(args[1:])
    if args and args[0] == "events":
        from .events_cli import main as events_main

        return events_main(args[1:])
    if args and args[0] == "journal":
        from .journal_cli import main as journal_main

        return journal_main(args[1:])
    if args and args[0] == "qc":
        from .qc_cli import main as qc_main

        return qc_main(args[1:])
    if args and args[0] == "run":
        from .run_cli import main as run_main

        return run_main(args[1:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
