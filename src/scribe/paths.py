"""Filesystem permissions for everything scribe creates.

**Derived content inherits the confidentiality of its source.** Claude Code transcripts are
mode `0600`; scribe reads them and writes digests, session state and spend records. Emitting
those at the default `0644` would take owner-only content and make it world-readable — a
downgrade introduced by this component, not inherited from anywhere.

That is not abstract on forge. Seven `agent-*` local accounts exist, **none of them in group
`ted`**, so the world-read bit is precisely the bit that grants them access. And the session
state store holds `session_id` values, which are functionally credentials when paired with
`claude -p --resume` — the same shape as the matrix-dispatcher `sessions.db` finding.

Two layers, because they fail differently:

  * **The directory is `0700`.** This is the durable guard: it protects anything created
    inside regardless of that file's own mode, which matters for files scribe does not create
    directly — SQLite's `-wal` and `-shm` sidecars are the concrete case.
  * **The file is `0600`.** Belt and braces, and the thing that survives someone loosening
    the directory later.

`sqlite3` inherits the process umask and creates `0644` by default, so the chmod after
connect is required rather than decorative.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_dir(path: str | os.PathLike[str]) -> Path:
    """Create `path` and any missing parents owner-only.

    Tightens **every directory it creates**, not just the leaf. `mkdir(parents=True)` applies
    the process umask to the intermediates, so chmod'ing only the leaf leaves a `0755`
    ancestor above a `0700` directory — which defeats the point, since the traversal bit on
    the parent is what a reader needs.

    Directories that already existed are left alone. Walking up and tightening whatever is
    found would eventually reach `~` or `/home` and lock the operator out of their own
    machine; only what this call brings into being is ours to set.
    """
    p = Path(path).expanduser()
    created: list[Path] = []
    probe = p
    while not probe.exists():
        created.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    p.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    # mkdir's `mode` is masked by the umask, and is ignored outright for a directory that
    # already existed, so neither case can be trusted without an explicit chmod.
    for made in created:
        with contextlib.suppress(OSError):
            made.chmod(DIR_MODE)
    if not created:
        with contextlib.suppress(OSError):
            p.chmod(DIR_MODE)
    return p


def secure_file(path: str | os.PathLike[str]) -> Path:
    """Tighten `path` to owner-only if it exists.

    Never raises. A permissions failure must not take down a summarization run — but it must
    also not pass silently as success, which is why callers create the parent via
    `secure_dir` first: that guard holds even when this one cannot.
    """
    p = Path(path).expanduser()
    with contextlib.suppress(OSError):
        p.chmod(FILE_MODE)
    return p


def secure_sqlite(db_path: str | os.PathLike[str]) -> None:
    """Tighten a SQLite database and its WAL sidecars.

    The `-wal` and `-shm` files are created by SQLite, not by us, so they get the process
    umask. They are tightened here when present, and the `0700` parent directory covers the
    window before they exist.
    """
    p = Path(db_path).expanduser()
    for candidate in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
        if candidate.exists():
            secure_file(candidate)
