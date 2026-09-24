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
import re
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


def repo_snapshot(dest: Path) -> int:
    """Copy the files git would commit into `dest`, and return how many were copied.

    Tracked files plus untracked-but-not-ignored ones (`--exclude-standard`), taken from the
    WORKING TREE so uncommitted edits are scanned too. What is left out is what `.gitignore`
    excludes -- `.venv/`, caches, local data -- which is content no commit carries and which
    the CI gate never sees, because CI scans a clean checkout.

    Scanning `REPO_ROOT` itself with `--no-git` walked all of it (vikunja#899). With the
    distro gitleaks on forge that reported 202 findings in `.venv` and failed the gate for a
    reason that was not the repository; with the pinned version it happened to be 0. A gate
    whose verdict depends on what a developer has pip-installed is measuring the developer.

    A path deleted in the working tree but still in the index is skipped: there is nothing
    to scan.
    """
    # S603/S607: `git` by name, like `gitleaks` above; no element of the argv is external.
    listing = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    copied = 0
    for rel in sorted(set(filter(None, listing.split("\0")))):
        src = REPO_ROOT / rel
        if not src.is_file() or src.is_symlink():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copied += 1
    return copied


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
    # Loud, and repeated beside the verdict at the end. This used to be one `NOTE` line above
    # seven `ok`s, and it is the single fact that decides whether the run means anything:
    # forge's distro binary prints no version at all and fails this gate on content the
    # pinned one allowlists. A caveat that is easy to read past is not a caveat.
    skew = installed != GITLEAKS_PINNED
    if skew:
        print(_skew_banner(installed))

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

    # The repository itself, with its allowlisted synthetic fixtures, must be clean. Scanned
    # as the set of files a commit could carry, not as a directory -- see `repo_snapshot`.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        try:
            copied = repo_snapshot(d)
        except (OSError, subprocess.CalledProcessError) as exc:
            # Not skippable, per the module docstring: a repository check that could not run
            # must not read as one that passed.
            check(False, f"could not list the repository's files ({exc})")
        else:
            check(copied > 0, f"repository snapshot is non-empty ({copied} files)")
            code, rules = scan(d)
            check(code == 0, f"this repository is clean (rules fired: {sorted(rules) or 'none'})")

    suffix = " -- NOT against the gitleaks version CI pins" if skew else ""
    if FAILURES:
        print(f"\n{len(FAILURES)} gate check(s) failed{suffix}")
        if skew:
            print(_skew_banner(installed))
        return 1
    print(f"\nall gate checks passed{suffix}")
    if skew:
        print(_skew_banner(installed))
    return 0


def _skew_banner(installed: str) -> str:
    # The Debian build answers `gitleaks version` with "version is set by build process",
    # which reads as nonsense spliced into a sentence. Say what it means instead.
    what = installed if re.fullmatch(r"v?\d+(\.\d+)*", installed) else "an unversioned build"
    rule = "!" * 78
    return (
        f"{rule}\n"
        f"!! gitleaks on PATH is {what}; CI pins {GITLEAKS_PINNED}.\n"
        "!! Rulesets differ between versions: this result says NOTHING about CI, in either\n"
        "!! direction. Fetch the pinned binary (see AGENTS.md, Testing) and re-run.\n"
        f"{rule}"
    )


if __name__ == "__main__":
    sys.exit(main())
