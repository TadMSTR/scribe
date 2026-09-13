"""`python -m scribe` — dispatch to a subcommand."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "extract":
        from .extract.__main__ import main as extract_main

        return extract_main(args[1:])
    if args and args[0] == "qc":
        from .qc_cli import main as qc_main

        return qc_main(args[1:])
    print("usage: python -m scribe {extract|qc} ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
