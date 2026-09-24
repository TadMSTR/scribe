"""Every command scribe recommends to an operator must be a command that runs.

vikunja#903 was one line of advice naming `scribe recover --status`, a flag that has never
existed, printed at the exact moment a summary had been lost. It survived because nothing
tied the message to `recover`'s parser: `tests/test_run_cli.py` asserted the line was
printed, and that the string `"scribe recover"` appeared in it, which a broken flag appended
to a correct prefix satisfies perfectly.

Preflight's sweep found two more in `run_cli.py`'s own module docstring — `--dry-run` and
`--once`, neither of which exists — so the single fix was never the deliverable. This is:
the coupling itself, checked mechanically, so the next rewording cannot quietly reintroduce
it. Same shape as AGENTS.md's rule that a prompt must not state a cap the validator does not
enforce; here the parser is the validator and the advice string is the claim.

The source text is scanned raw, comments included. A comment telling the next reader to run
`scribe recover --status` is wrong in exactly the way this exists to catch.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from scribe.__main__ import main as dispatch

SRC = Path(__file__).resolve().parent.parent / "src" / "scribe"

#: Every subcommand `python -m scribe` dispatches to, per `__main__.USAGE`.
SUBCOMMANDS = (
    "cap-survey",
    "deps-drift",
    "extract",
    "events",
    "index",
    "journal",
    "qc",
    "qc-survey",
    "recover",
    "run",
)

#: The module that owns each subcommand's parser. A bare `--flag` written inside one of
#: these is read as advice about that subcommand — which is what makes the two defects in
#: `run_cli.py`'s docstring reachable, since neither named a subcommand at all.
OWNERS = {
    "cap_survey_cli.py": "cap-survey",
    "deps_drift_cli.py": "deps-drift",
    "events_cli.py": "events",
    "index_cli.py": "index",
    "journal_cli.py": "journal",
    "qc_cli.py": "qc",
    "qc_survey_cli.py": "qc-survey",
    "recover_cli.py": "recover",
    "run_cli.py": "run",
}

#: Backticked `--tokens` that are deliberately not advice about this module's own parser.
#: Deliberately an EXPLICIT list rather than a looser regex: a pattern slack enough to have
#: no false positives is also slack enough to miss the next real defect. Empty today — the
#: sweep runs clean across all 23 advice strings in `src/scribe/` — and if it ever needs
#: more entries than the check catches defects, that is the signal to scope the check down
#: rather than to keep feeding it.
ALLOWED_BARE: dict[str, set[str]] = {}

_COMMAND = re.compile(r"`(scribe\s+[^`]+)`")
_BARE_FLAG = re.compile(r"`(--[A-Za-z][\w-]*)`")


def _flags_of(subcommand: str) -> set[str]:
    """The flags `subcommand`'s parser actually accepts, read from its own `--help`.

    Taken from argparse rather than from a hand-kept list, because a hand-kept list is the
    same class of thing as the advice string and would drift alongside it.
    """
    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        dispatch([subcommand, "--help"])
    out = buf.getvalue()
    assert out, f"`scribe {subcommand} --help` printed nothing"
    return set(re.findall(r"--[A-Za-z][\w-]*", out))


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_subcommand_inventory_matches_the_dispatcher() -> None:
    """Pins SUBCOMMANDS to `__main__`, so a new subcommand cannot silently go unchecked.

    Without this the sweep below degrades quietly: a subcommand nobody added here is simply
    never swept, and the check reports green over exactly the code it stopped covering.
    """
    from scribe.__main__ import USAGE

    declared = set(re.search(r"\{([^}]+)\}", USAGE).group(1).split("|"))
    assert declared == set(SUBCOMMANDS)


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_every_recommended_command_is_runnable(path: Path) -> None:
    """`scribe <sub> --flag` written anywhere in the source must name a real flag."""
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for quoted in _COMMAND.findall(line):
            tokens = quoted.split()[1:]
            subcommand = tokens[0]
            where = f"{path.name}:{lineno}"
            assert subcommand in SUBCOMMANDS, (
                f"{where} recommends `{quoted}`, but `{subcommand}` is not a subcommand"
            )
            real = _flags_of(subcommand)
            for flag in (t for t in tokens[1:] if t.startswith("--")):
                assert flag in real, (
                    f"{where} recommends `{quoted}`, but `{flag}` is not a flag of "
                    f"`scribe {subcommand}` -- it accepts {sorted(real)}"
                )


@pytest.mark.parametrize("filename,subcommand", sorted(OWNERS.items()))
def test_a_cli_module_names_only_its_own_real_flags(filename: str, subcommand: str) -> None:
    """A bare `--flag` inside a CLI module is advice about that module's own parser.

    This is the check that catches #903's two siblings: `run_cli.py`'s docstring promised
    `--dry-run` and `--once` without ever naming a subcommand, so nothing tying a *command*
    to a parser could have seen them.
    """
    path = SRC / filename
    real = _flags_of(subcommand)
    allowed = ALLOWED_BARE.get(filename, set())
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for flag in _BARE_FLAG.findall(line):
            if flag in allowed:
                continue
            assert flag in real, (
                f"{filename}:{lineno} names `{flag}`, which is not a flag of "
                f"`scribe {subcommand}` -- it accepts {sorted(real)}"
            )
