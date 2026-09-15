"""Stop the test suite writing to the operator's home directory.

Several of scribe's defaults resolve under `~`: the spend log (`telemetry.DEFAULT_SPEND_LOG`),
the event log root, the digest output root and the state database. Each is overridable, and
every existing call site that remembers to override is correct — but "every call site
remembers" is not a property a test suite can hold. vikunja#851 measured the result: 1,182
phantom `stub-1` records in the real `~/logs/scribe-tokens.log`, **99.1% of the file**, at
+20 per `pytest` run, sitting in wait for the day the spend meter is pointed at that path.

So the isolation lives in one autouse fixture instead of in N call sites.

**It redirects `HOME`, rather than patching each default.** Patching the individual constants
would cover the three defaults that exist today and silently miss the fourth somebody adds
next year; `~` is the single thing they all go through. It is also the more honest statement
of the rule being enforced, which is not "the spend log must be isolated" but *a test run
must not write outside its own tmp directory*.

`REAL_HOME` and `REAL_SPEND_LOG` are captured at import, before any fixture has run, so a
test can still name the real path in order to assert nothing touched it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Resolved at collection time — after this module is imported the fixture below makes `~`
#: point at a per-test tmp directory, so this is the only chance to see the real one.
REAL_HOME = Path(os.path.expanduser("~"))
REAL_SPEND_LOG = REAL_HOME / "logs" / "scribe-tokens.log"


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch) -> Path:
    """Point `~` at a per-test directory for the duration of one test.

    `os.path.expanduser` reads `HOME` at call time on POSIX, and every scribe default is a
    `~`-prefixed *string* expanded at use, so this reaches them all without any of them
    having to know about it.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    return home


@pytest.fixture
def real_home() -> Path:
    """The operator's actual home, for tests that must assert nothing touched it.

    Exposed as a fixture rather than imported, because `tests/` is not a package.
    """
    return REAL_HOME


@pytest.fixture
def real_spend_log() -> Path:
    """The production spend log — the specific file vikunja#851 measured 1,182 strays in."""
    return REAL_SPEND_LOG
