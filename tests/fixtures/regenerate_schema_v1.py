"""Regenerate `state-schema-v1.sql` from the tag that actually shipped schema 1.

Run from the repo root, with the v0.2.0 tag reachable:

    python tests/fixtures/regenerate_schema_v1.py

This is the single definition of how the fixture is derived. `tests/test_schema_migration.py`
imports `derive()` from here rather than reimplementing the extraction, so the guard and the
generator cannot drift into disagreeing about what the fixture should contain — which would
make the guard pass against a fixture it built itself.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

#: The last release whose `SCHEMA_VERSION` was 1. v0.3.0 raised it to 2.
SCHEMA_V1_TAG = "v0.2.0"

FIXTURE = Path(__file__).with_name("state-schema-v1.sql")

_HEADER = """\
-- Schema 1, as scribe {tag} actually created it.
--
-- GENERATED, NOT HAND-WRITTEN. The body below is the `_SCHEMA` literal from
-- `src/scribe/state.py` at tag {tag} ({sha}), the last release whose
-- SCHEMA_VERSION was 1, plus the `user_version` stamp that version's `Store.__init__`
-- applied. Extracted with `ast.literal_eval`, so it is the string the interpreter saw
-- rather than a transcription of it.
--
-- WHY A COMMITTED FILE RATHER THAN `git show` AT TEST TIME. CI checks out with
-- fetch-depth 0 so the tag IS reachable there, and tests/test_schema_migration.py
-- re-derives this file and asserts it still matches. But a test that can only run with
-- full history is a test that silently stops running the moment a checkout changes, so
-- the fixture is the primary artefact and the git check is the guard on it.
--
-- Regenerate: python tests/fixtures/regenerate_schema_v1.py
"""


def tag_is_reachable(tag: str = SCHEMA_V1_TAG) -> bool:
    """True when `tag` can be resolved here. False under a shallow clone."""
    return (
        subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", f"{tag}^{{commit}}"],  # noqa: S607
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def derive(tag: str = SCHEMA_V1_TAG) -> str:
    """Build the fixture's contents from `tag`. Raises if the tag is unreachable."""
    sha = subprocess.run(  # noqa: S603
        ["git", "rev-parse", f"{tag}^{{commit}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    source = subprocess.run(  # noqa: S603
        ["git", "show", f"{tag}:src/scribe/state.py"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    values: dict[str, object] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("_SCHEMA", "SCHEMA_VERSION"):
                values[name] = ast.literal_eval(node.value)

    version = values["SCHEMA_VERSION"]
    if version != 1:
        raise SystemExit(f"{tag} has SCHEMA_VERSION {version}, expected 1 — wrong tag?")

    header = _HEADER.format(tag=tag, sha=sha[:12])
    schema = str(values["_SCHEMA"]).strip()
    return f"{header}\n{schema}\n\nPRAGMA user_version = {version};\n"


if __name__ == "__main__":
    if not tag_is_reachable():
        raise SystemExit(
            f"tag {SCHEMA_V1_TAG} is not reachable — fetch tags first (git fetch --tags)"
        )
    FIXTURE.write_text(derive())
    print(f"wrote {FIXTURE}", file=sys.stderr)
