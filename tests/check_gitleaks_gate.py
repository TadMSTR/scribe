#!/usr/bin/env python3
"""Assert the secret-scanning gate actually fires. Behaviour, not configuration.

A scanner that has silently stopped matching reports "no leaks found", and so does a
genuinely clean repository: the same green, from opposite causes. Every assertion below
plants a secret and requires the gate to FIND it. The clean-tree check comes last and is
only meaningful because the planted cases established the gate can fire at all.

THIS REPOSITORY HAS A SPECIFIC HAZARD. Its test suite must contain credential-shaped
strings, because the component under test is a redactor. `.gitleaks.toml` therefore
allowlists three named test paths. An allowlist is a hole in the gate, so:

  * probes are planted in a TEMPORARY DIRECTORY, never under `tests/`, so the allowlist
    cannot be what makes them pass;
  * one probe asserts the allowlist is NOT over-broad, by planting a real-shaped secret
    with no synthetic marker into a file named like an allowlisted one and requiring it to
    still be caught.

NOT NAMED test_*.py, deliberately: pytest would collect it, execute the scans at import
time and sys.exit() mid-collection, taking down the suite. Run it directly:
`python tests/check_gitleaks_gate.py`.

NOT SKIPPABLE. If gitleaks is missing this fails rather than skipping. A gate check that
passes when it could not run reports the same thing as one that verified something.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The version CI installs. A local run against a different binary tests a different
#: ruleset — reported rather than enforced, because refusing to run is worse than
#: running with a caveat printed.
GITLEAKS_PINNED = "8.28.0"

FAILURES: list[str] = []
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / ".gitleaks.toml"

# Assembled at runtime so this file does not itself contain a contiguous credential-shaped
# literal — otherwise the prober trips the very scanner it is testing.
_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5"
# NOT the AWS documented example key (AKIAIOSFODNN7EXAMPLE). gitleaks 8.28.0 allowlists
# that value by design, so a probe built on it reports the gate broken on a gate that
# works. Older gitleaks did catch it, which is how the difference stayed hidden: this
# script passed against the distro binary on the dev host and failed in CI, which
# installs a pinned version. Hence GITLEAKS_PINNED below.
_AWS = "AKIA" + "3ZQR7TXWVB2NLKPD"


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def scan(directory: Path) -> tuple[int, set[str]]:
    """Return `(exit_code, rule_ids)` for a gitleaks scan of `directory`.

    The report is written to a separate temporary directory, never into `directory`. Writing
    it inside the scanned tree left a `_gitleaks_report.json` in the repository root on the
    run that scans the repository — a findings file, containing the very matches being
    hunted, deposited into the tree the next scan will read.
    """
    # S603/S607: invoking `gitleaks` by name is the point of this script — it verifies the
    # binary CI installed on PATH, and hardcoding an absolute path would test a different
    # thing than the workflow runs. No component of the argv is caller-supplied.
    reportdir = tempfile.mkdtemp(prefix="gitleaks-report-")
    report = Path(reportdir) / "report.json"
    proc = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gitleaks",
            "detect",
            "--no-git",
            "--source",
            str(directory),
            "--config",
            str(CONFIG),
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--redact",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    rules: set[str] = set()
    if report.exists():
        # A malformed or unreadable report means "no rule IDs recovered", not a crash. The
        # exit code is the authoritative signal and is returned either way.
        with contextlib.suppress(ValueError, OSError):
            rules = {f.get("RuleID", "") for f in json.loads(report.read_text() or "[]")}
    shutil.rmtree(reportdir, ignore_errors=True)
    return proc.returncode, rules


def main() -> int:
    if shutil.which("gitleaks") is None:
        print("FAIL gitleaks is not installed — the gate cannot be verified")
        return 1
    if not CONFIG.exists():
        print(f"FAIL missing {CONFIG}")
        return 1

    installed = subprocess.run(
        ["gitleaks", "version"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    print(f"gitleaks gate checks (installed: {installed or 'unknown'}, CI pins {GITLEAKS_PINNED})")
    if installed != GITLEAKS_PINNED:
        print(
            f"  NOTE  local gitleaks is {installed or 'unknown'}, not {GITLEAKS_PINNED}. "
            "Rulesets differ between versions — a pass here is not a pass in CI."
        )

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "leak.py").write_text(f'TOKEN = "{_GH}"\n', encoding="utf-8")
        code, rules = scan(d)
        check(code != 0, "planted GitHub token in a normal path is caught")
        check(bool(rules), f"a rule fired (got: {sorted(rules) or 'none'})")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "aws.env").write_text(f"AWS_ACCESS_KEY_ID={_AWS}\n", encoding="utf-8")
        code, rules = scan(d)
        # Asserts the SPECIFIC rule, not just a non-zero exit. Exit-code-only would stay
        # green if some other rule happened to match the surrounding text, which is exactly
        # how a rule that has stopped working goes unnoticed.
        check(
            "aws-access-token" in rules, f"planted AWS key fires aws-access-token ({sorted(rules)})"
        )
        check(code != 0, "planted AWS key is caught")

    # The allowlist must excuse SYNTHETIC literals, not the whole file. A real-shaped
    # secret in an allowlisted filename must still be caught, or the allowlist is a hole
    # rather than an exemption.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        tests = d / "tests"
        tests.mkdir()
        # A key-shaped identifier is required for `generic-api-key` to fire at all.
        # An earlier version of this probe wrote `REAL = "..."`, which no rule matches, so
        # it reported the allowlist as a hole in a config that was fine — a probe that
        # fails for its own reasons is as useless as one that cannot fail.
        (tests / "test_redact.py").write_text(f'TOKEN = "{_GH}"\n', encoding="utf-8")
        code, _ = scan(d)
        check(code != 0, "allowlist does not excuse an unmarked secret in a named path")

    # Only now is a clean result meaningful.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ok.py").write_text('GREETING = "hello"\nPORT = 8499\n', encoding="utf-8")
        code, _ = scan(d)
        check(code == 0, "clean tree reports no leaks")

    # The repository itself, with its allowlisted synthetic fixtures, must be clean.
    code, rules = scan(REPO_ROOT)
    check(code == 0, f"this repository is clean (rules fired: {sorted(rules) or 'none'})")

    if FAILURES:
        print(f"\n{len(FAILURES)} gate check(s) failed")
        return 1
    print("\nall gate checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
