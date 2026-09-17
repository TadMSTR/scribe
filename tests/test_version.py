"""The package's declared version must match the one it is packaged as.

They drifted: `pyproject.toml` went to 0.2.0 at release and `__init__.py` stayed at 0.1.0, so
`scribe.__version__` under-reported by a full version for the whole of v0.2.0's life. That is
not cosmetic -- it is what a human or an agent reads to answer "what is running on the host",
and it produced a wrong answer during this build's own pre-audit: the deployed venv was
reported as v0.1.0 in an audit request when it was actually v0.2.0.

**Read `pyproject.toml`, not `importlib.metadata`.** The metadata is whatever was installed,
which for an editable or stale install can lag the source tree -- so a parity check against it
can pass while the two files in the repo still disagree. The file on disk is the declaration.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import scribe

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_package_version_matches_pyproject() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert scribe.__version__ == declared, (
        f"__init__.py says {scribe.__version__}, pyproject.toml says {declared} -- "
        "a release bumped one and not the other"
    )
