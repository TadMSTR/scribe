"""Filesystem permissions for everything scribe creates.

**Derived content inherits the confidentiality of its source.** Claude Code transcripts are
mode `0600`; scribe reads them and writes digests, session state and spend records. Emitting
those at the default `0644` would take owner-only content and make it world-readable — a
downgrade introduced by this component, not inherited from anywhere.

That is not abstract on forge. Seven `agent-*` local accounts exist, **none of them in the
operator's group**, so the world-read bit is precisely the bit that grants them access. And the
session state store holds `session_id` values, which are functionally credentials when paired
with `claude -p --resume` — the same shape as the matrix-dispatcher `sessions.db` finding.

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
import re
from pathlib import Path
from typing import IO

DIR_MODE = 0o700
FILE_MODE = 0o600


#: An agent name is one path component and is used as one, on BOTH sides of the corpus: the
#: journal reads `<digest_root>/<agent>` and the pipeline writes it. `.` and `..` match this
#: pattern and are excluded separately, because a character class cannot express "is not a
#: traversal".
AGENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_agent(name: str) -> bool:
    """Whether `name` is safe to use as a directory component.

    The agent name is an external identifier that reaches a path. It arrives from
    `$CLAUDE_PROJECT_DIR`, from `--agent`, or -- the route that is easiest to miss -- from a
    transcript's own top-level `cwd` field, via `extract.parser._agent_from_cwd`. `..` clears
    a naive check twice over: it is a legal directory name AND
    `Path('.../projects/..').name` really is `".."`, so every one of those three routes can
    produce it.

    **Lives here rather than in either consumer.** The read side (`journal.agent_dir`) had
    this guard and the write side (`writeback.daily_path`) did not, which is how a single-level
    traversal stayed live on the write path while the read path was correctly defended. One
    definition, so the two cannot drift apart again.
    """
    return bool(AGENT_RE.match(name)) and name not in {".", ".."}


def contained(root: str | os.PathLike[str], child: str) -> bool:
    """Whether `root / child` really resolves to somewhere under `root`.

    The guard that actually holds. A character allowlist states an intention about *names*;
    the property that matters is about *paths*, and it is asserted against the resolved path
    so it cannot be satisfied by a name that merely looks well-formed.
    """
    base = Path(root).expanduser()
    return base.resolve(strict=False) in (base / child).resolve(strict=False).parents


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


def secure_create(path: str | os.PathLike[str]) -> IO[str]:
    """Open `path` for writing, created 0600 **at creation** rather than chmod'd after.

    `open(path, "w")` inherits the process umask — typically 0022, so 0644 — and a chmod
    afterwards leaves a window in which the file is world-readable. For a temp file that is
    about to be renamed over a destination, the window is not the only problem: **rename
    preserves the source's permissions**, so a 0644 temp file silently downgrades an
    already-0600 destination. That is FW-03 in the fleet's pattern knowledge base, recurrence
    2, and it is why this exists as its own call rather than as `open()` plus `secure_file()`.

    The mode is applied by the kernel at `O_CREAT`, so there is no window at all.
    """
    p = Path(path).expanduser()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    return os.fdopen(fd, "w", encoding="utf-8")


def secure_append(path: str | os.PathLike[str]) -> IO[str]:
    """Open `path` for appending, created 0600 at creation if it does not exist yet.

    `secure_create`'s argument, for a file that is added to rather than replaced: `O_APPEND`
    instead of `O_TRUNC`, the mode still applied by the kernel at `O_CREAT`. A file that
    already exists keeps its mode, so this is also tightened after the fact by the caller's
    `secure_file` -- the pair that `writeback.append_block` already uses.
    """
    p = Path(path).expanduser()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    return os.fdopen(fd, "a", encoding="utf-8")


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
