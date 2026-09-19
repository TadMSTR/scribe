"""`python -m scribe deps-drift` — is the deployed tree the tree CI audited?

Run this on forge, pointed at a deployed venv. It answers one question that no CI gate can,
because CI cannot see forge: does `/opt/venvs/scribe` hold the dependency set that `uv.lock`
pins and `ci.yml` audits?

Today the honest answer is "not necessarily, and nothing was checking". `venv-deploy.sh`
pip-installs a wheel built from source, re-resolving from the bounded ranges in
`pyproject.toml`. See `deps_drift.py` for why that is out of scope to fix here.

Exit codes are deliberately NOT modelled on `scribe recover`'s. That tool's contract took
vikunja#875, #880 and #890 to settle and every code in it means something specific to
`scribe-recover-check.sh`; this is a report, and giving it a rich exit vocabulary would invite
someone to page on it. Drift alone never changes the exit code.

    0  the comparison ran
    2  bad invocation, or the lock or venv could not be read
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .deps_drift import compare, installed_versions, render, runtime_closure

#: Where sysadmin deploys scribe. A default rather than a required argument because the
#: overwhelmingly common invocation is the production one, and making the operator retype a
#: fixed path is how a check stops being run.
DEFAULT_VENV = "/opt/venvs/scribe"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe deps-drift",
        description="Compare a deployed venv against the dependency set uv.lock pins.",
    )
    ap.add_argument(
        "--venv",
        type=Path,
        default=Path(DEFAULT_VENV),
        help=f"deployed virtualenv to inspect (default: {DEFAULT_VENV})",
    )
    ap.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="path to uv.lock (default: the one beside this installation's pyproject.toml)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    lock = args.lock
    if lock is None:
        # Walk up from this file looking for the repo's uv.lock. An installed wheel has no
        # uv.lock anywhere near it, which is why this fails loudly with an actionable message
        # rather than silently comparing against nothing.
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "uv.lock"
            if candidate.is_file():
                lock = candidate
                break
    if lock is None or not Path(lock).is_file():
        print(
            "scribe: no uv.lock found. Pass --lock explicitly; note that an installed wheel "
            "does not carry one, so this check runs from a source checkout.",
            file=sys.stderr,
        )
        return 2

    try:
        locked = runtime_closure(lock)
        deployed = installed_versions(args.venv)
    except (OSError, KeyError, ValueError) as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return 2

    drift = compare(locked, deployed)

    if args.json:
        json.dump(
            {
                "lock": str(lock),
                "venv": str(args.venv),
                "locked_packages": len(locked),
                "deployed_packages": len(deployed),
                "clean": drift.clean,
                "mismatched": {
                    k: {"locked": v[0], "deployed": v[1]} for k, v in drift.mismatched.items()
                },
                "missing_from_deployment": drift.missing,
                "not_in_lock": drift.unlocked,
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
    else:
        print(render(drift, lock_path=str(lock), venv=str(args.venv)))

    # Always 0 when the comparison ran. See the module docstring: drift is expected by
    # construction today, and a check that goes red on the expected state gets switched off.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
