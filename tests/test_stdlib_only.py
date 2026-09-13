"""`scribe.extract` must import nothing outside the standard library.

This is enforced here rather than by keeping `httpx` out of `[project.dependencies]`. A
packaging convention is a statement of intent that nothing checks; an AST walk over the
module's own imports is a guarantee. The invariant matters because `scribe.extract` is the
component that reads raw transcripts — the least trustworthy input in the system — and the
one whose "no network" claim the whole design leans on.
"""

from __future__ import annotations

import ast
import pathlib
import sys

EXTRACT = pathlib.Path(__file__).parent.parent / "src" / "scribe" / "extract"


def _module_files() -> list[pathlib.Path]:
    files = sorted(EXTRACT.glob("*.py"))
    assert files, "found no modules to check — the guard would pass vacuously"
    return files


def _toplevel_imports(path: pathlib.Path) -> set[str]:
    """Every distinct top-level package imported by `path`, relative imports excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: within scribe itself
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_extract_imports_only_the_standard_library() -> None:
    stdlib = sys.stdlib_module_names
    offenders: dict[str, set[str]] = {}
    for path in _module_files():
        external = {n for n in _toplevel_imports(path) if n not in stdlib}
        if external:
            offenders[path.name] = external
    assert not offenders, f"non-stdlib imports in scribe.extract: {offenders}"


def test_the_guard_can_actually_fail() -> None:
    """A check that cannot fail reports the same thing as one that verified something."""
    src = "import httpx\nfrom scribe.extract import parser\n"
    tree = ast.parse(src)
    names = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert "httpx" in names
    assert "httpx" not in sys.stdlib_module_names


def test_extract_does_not_import_the_summarize_package() -> None:
    """The dependency runs one way. If extraction ever needed the summarizer, the offline
    guarantee would be a matter of import order rather than of structure."""
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("summarize"):
                raise AssertionError(f"{path.name} imports the summarizer")


def test_httpx_is_not_imported_by_importing_extract() -> None:
    """Belt and braces on the AST walk: an import hidden inside a function would evade it.

    Run in a subprocess because the test session itself has already imported httpx.
    """
    import subprocess

    code = "import sys; import scribe.extract; sys.exit(1 if 'httpx' in sys.modules else 0)"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"importing scribe.extract pulled in httpx: {proc.stderr}"
