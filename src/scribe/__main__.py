"""`python -m scribe` — dispatch to a subcommand."""

from __future__ import annotations

import sys

USAGE = (
    "usage: python -m scribe "
    "{cap-survey|deps-drift|extract|events|index|journal|qc|qc-survey|recover|run} ..."
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "cap-survey":
        from .cap_survey_cli import main as cap_survey_main

        return cap_survey_main(args[1:])
    if args and args[0] == "deps-drift":
        from .deps_drift_cli import main as deps_drift_main

        return deps_drift_main(args[1:])
    if args and args[0] == "extract":
        from .extract.__main__ import main as extract_main

        return extract_main(args[1:])
    if args and args[0] == "events":
        from .events_cli import main as events_main

        return events_main(args[1:])
    if args and args[0] == "index":
        from .index_cli import main as index_main

        return index_main(args[1:])
    if args and args[0] == "journal":
        from .journal_cli import main as journal_main

        return journal_main(args[1:])
    if args and args[0] == "qc":
        from .qc_cli import main as qc_main

        return qc_main(args[1:])
    if args and args[0] == "qc-survey":
        from .qc_survey_cli import main as qc_survey_main

        return qc_survey_main(args[1:])
    if args and args[0] == "recover":
        from .recover_cli import main as recover_main

        return recover_main(args[1:])
    if args and args[0] == "run":
        from .run_cli import main as run_main

        return run_main(args[1:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
