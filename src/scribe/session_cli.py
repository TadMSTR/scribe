"""`python -m scribe session <session-id>` — everything scribe holds about one session.

Before this, every investigation rebuilt the same join by hand: the transcript, the state
row, the event log, the digest blocks and the run records, each in its own file and format.
In the 30 days to 2026-11-01 about 21 agent sessions read scribe's files directly to do it,
and one review wrote four throwaway parsers (vikunja#1347). This is that join, once.

**The JSON shape is a contract.** Agent isolation will take away agents' direct access to
scribe's 0600 files, and a read-only scoped-mcp module will wrap this command instead. So the
document carries `schema` (`SCHEMA`), and a field changes meaning only with a new version.

Two rules hold for every field, and `tests/test_session_cli.py` holds them:

  1. **Read-only.** The state DB is opened `mode=ro` through sqlite's URI form -- never via
     `Store`, which stamps `user_version` on open. Nothing calls a provider. Nothing writes,
     with one exception that is SQLite's, not scribe's: in WAL mode every reader updates the
     `-shm` shared-memory index, read-only or not. It holds no database content.
  2. **No content beyond the redacted event log.** Paths, ids, counts, statuses and lengths
     are fine. Text appears only where the event log already holds it, redacted at extraction:
     the closing statement and the subagent descriptions. A run record's `claims` and
     `contamination_excerpt` are digest and model text, so `runs` carries a fixed list of
     fields that excludes them. The state row's `last_error` goes through `Redactor` first.

Exit codes: 0 found, 1 no trace of the session anywhere scribe looks, 2 a usage or config
problem, 3 the lookup failed or could not read a source (`errors` names it). A missing
transcript is not an error: it is the normal state of a session older than the retention
period, and the event log is what remains.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Config, ConfigError, load
from .discovery import iter_transcripts
from .eventlog import SAFE_STEM_RE, EventLogError, eventlog_path, load_eventlog
from .extract.redact import Redactor
from .summarize.headless import MODE_LLM, MODE_VERBATIM, closing_statement
from .summarize.render import VERBATIM_START
from .writeback import paired_blocks

#: The document's version. Bump it when a field is removed or changes meaning.
SCHEMA = "scribe.session/1"

EXIT_FOUND = 0
EXIT_UNKNOWN = 1
EXIT_USAGE = 2
#: The tool failed, or could not read a source it needed to answer "no trace". Distinct from
#: 1 so a wrapper never reads a crash as an unknown session (security baseline OV-27).
EXIT_FAILED = 3

#: The run-record fields `runs` carries. A fixed list rather than "everything minus a
#: denylist", so a field added to the record later is not exposed until someone decides to.
RUN_FIELDS = (
    "ts",
    "source",
    "scribe_version",
    "turn_uuid",
    "digest",
    "full_read",
    "replayed",
    "turns",
    "events",
    "degradation_level",
    "written",
    "suppressed",
    "placeholder",
    "pending",
    "discarded",
    "qc_ok",
    "qc_coverage",
    "render_mode",
    "closing_chars",
    "closing_source",
    "superseded_blocks",
)

STATE_FIELDS = (
    "transcript_path",
    "session_id",
    "agent",
    "status",
    "attempts",
    "last_error",
    "size_bytes",
    "last_offset",
    "last_turn_uuid",
    "first_seen",
    "updated_at",
)


def _present(path: Path, errors: list[str], label: str) -> os.stat_result | None:
    """`path`'s stat, or None when it is absent -- and an ERROR when it could not be looked at.

    `Path.is_file()` and a bare `except OSError` both read a permission failure as "not
    there". This command is meant to be wrapped for agents that cannot read scribe's files,
    so "could not look" must never read as "gone" or "no trace" (security baseline SH-15).
    """
    try:
        return path.stat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # ValueError: an embedded NUL
        errors.append(f"{label}: {type(exc).__name__}")
        return None


def _state_rows(cfg: Config, session_id: str, errors: list[str]) -> list[dict]:
    """The session's state rows, read-only. [] when the DB or the row is absent.

    A DB that is there and cannot be read is recorded in `errors` (exception type only), so it
    never reads as "no state row" -- which would make an unreadable DB look like an unknown
    session (security baseline OV-01).
    """
    db = Path(cfg.state_path).expanduser()
    if _present(db, errors, "state") is None:
        return []
    uri = f"file:{db}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        errors.append(f"state: {type(exc).__name__}")
        return []
    try:
        conn.row_factory = sqlite3.Row
        # `_` is a LIKE wildcard and SAFE_STEM_RE admits it, so it is escaped; and an exact
        # `session_id` match sorts first, so `rows[0]` is never another session's row.
        like = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? OR transcript_path LIKE ? ESCAPE '\\' "
            "ORDER BY (session_id = ?) DESC, updated_at DESC",
            (session_id, f"%/{like}.jsonl", session_id),
        ).fetchall()
    except sqlite3.Error as exc:
        errors.append(f"state: {type(exc).__name__}")
        return []
    finally:
        conn.close()
    scrub = Redactor().scrub
    out = []
    for r in rows:
        # `r.keys()`, not `r`: `in` on a `sqlite3.Row` tests its VALUES.
        columns = set(r.keys())
        row = {k: r[k] for k in STATE_FIELDS if k in columns}
        row["last_error"] = scrub(row.get("last_error") or "")
        out.append(row)
    return out


def _transcript(cfg: Config, session_id: str, rows: list[dict], errors: list[str]) -> dict:
    """Where the transcript is, whether it is still there, and when it will be deleted."""
    path = ""
    if rows:
        path = rows[0]["transcript_path"]
    else:
        for found in iter_transcripts(cfg.project_globs, cfg.exclude):
            if found.path.stem == session_id:
                path = str(found.path)
                break
    out: dict = {"path": path or None, "exists": False, "size_bytes": None, "expires": "unknown"}
    if not path:
        return out
    p = _contained(cfg, Path(path).expanduser())
    if p is None:
        # The path is the state DB's, and the DB is scribe's own -- but it is only stat'd if it
        # is where a transcript can be, so a doctored row cannot probe the filesystem through
        # this command (security baseline IV-21).
        out["outside_project_globs"] = True
        return out
    st = _present(p, errors, "transcript")
    if st is None:
        return out
    out["exists"] = True
    out["size_bytes"] = st.st_size
    mtime = datetime.fromtimestamp(st.st_mtime, UTC)
    out["last_write"] = mtime.isoformat(timespec="seconds")
    if cfg.transcript_retention_days:
        # Claude Code deletes a transcript this many days after its LAST write, so the date
        # moves whenever the session resumes.
        out["expires"] = (mtime + timedelta(days=cfg.transcript_retention_days)).date().isoformat()
    return out


def _contained(cfg: Config, path: Path) -> Path | None:
    """`path` resolved, if it is a `*.jsonl` directly inside a directory `project_globs`
    matches; else None. The caller stats the RESOLVED path, never the one it was given.

    Resolved in full first, then compared with the resolved directories `glob` finds -- whose
    `*` stops at a separator. Matching the raw path, or using `fnmatch` (whose `*` also matches
    `/`), let a doctored state row reach any `*.jsonl` on the host through `..` (security
    baseline IV-21/IV-27/IV-28/OV-52).
    """
    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError):
        return None
    if resolved.suffix != ".jsonl":
        return None
    for pattern in cfg.project_globs:
        for hit in glob.glob(os.path.expanduser(pattern).rstrip("/")):
            try:
                if Path(hit).resolve(strict=False) == resolved.parent:
                    return resolved
            except (OSError, ValueError):
                continue
    return None


def _eventlog(cfg: Config, session_id: str, errors: list[str]) -> tuple[dict | None, object | None]:
    path = eventlog_path(cfg.eventlog_dir, session_id)
    if _present(path, errors, "eventlog") is None:
        return None, None
    try:
        log = load_eventlog(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (EventLogError, OSError, ValueError) as exc:
        # In `errors` too, so the command exits 3: an unreadable log is "could not look", not
        # "no event log" (audit F-05).
        errors.append(f"eventlog: {type(exc).__name__}")
        return {"path": str(path), "error": type(exc).__name__}, None
    not_recorded = "not recorded"
    return {
        "path": str(path),
        "schema_version": raw.get("schema_version", 1),
        "agent": log.agent,
        "entrypoint": log.entrypoint or None,
        "started_at": log.started_at or None,
        "ended_at": log.ended_at or None,
        "stats": log.stats.to_dict(),
        "subagents": (
            [s.to_dict() for s in log.subagents] if log.subagents is not None else not_recorded
        ),
        "compactions": (
            [c.to_dict() for c in log.compactions] if log.compactions is not None else not_recorded
        ),
        "background_pending": (
            log.background_pending if log.background_pending is not None else not_recorded
        ),
    }, log


def _blocks(cfg: Config, session_id: str, errors: list[str]) -> list[dict]:
    """Every complete block for the session in the corpus, with which one is current."""
    root = Path(cfg.output_dir).expanduser()
    out: list[dict] = []
    if not root.is_dir():
        return out

    def unreadable(exc: OSError) -> None:
        # `Path.rglob` skips a directory it cannot list without a word, which read as "no
        # blocks" (audit F-05). `os.walk` reports it here instead.
        where = os.path.relpath(exc.filename, root) if exc.filename else "?"
        errors.append(f"digest dir {where}: {type(exc).__name__}")

    found: list[Path] = []
    for dirpath, _dirs, files in os.walk(root, onerror=unreadable):
        found += [Path(dirpath) / f for f in files if f.endswith(".md")]
    for md in sorted(found):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"digest {md.relative_to(root).as_posix()}: {type(exc).__name__}")
            continue
        if session_id not in text:
            continue
        for a, c in paired_blocks(text):
            if a.group(1) != session_id:
                continue
            body = text[a.end() : c.start()]
            provisional = a.group(4) or ""
            mode = provisional or (MODE_VERBATIM if VERBATIM_START in body else MODE_LLM)
            out.append(
                {
                    "file": md.relative_to(root).as_posix(),
                    "turn_uuid": a.group(2),
                    "provisional": provisional,
                    "render_mode": mode,
                    "chars": len(body.strip()),
                    "current": False,
                }
            )
    finals = [b for b in out if not b["provisional"]]
    current = (finals or out)[-1:] if out else []
    for b in current:
        b["current"] = True
    return out


def _runs(cfg: Config, session_id: str, errors: list[str]) -> list[dict]:
    path = Path(cfg.qc_run_record).expanduser() if cfg.qc_run_record else None
    if path is None or _present(path, errors, "runs") is None:
        return []
    out = []
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError as exc:
        errors.append(f"runs: {type(exc).__name__}")
        return out
    with fh:
        for line in fh:
            if session_id not in line:
                continue
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("session_id") == session_id:
                    out.append({k: rec.get(k) for k in RUN_FIELDS})
    return out


def lookup(cfg: Config, session_id: str) -> dict:
    """The whole document for one session. Read-only; see the module docstring."""
    errors: list[str] = []
    rows = _state_rows(cfg, session_id, errors)
    evlog, log = _eventlog(cfg, session_id, errors)
    blocks = _blocks(cfg, session_id, errors)
    runs = _runs(cfg, session_id, errors)
    transcript = _transcript(cfg, session_id, rows, errors)

    closing: dict | None = None
    closing_reason = "event log unreadable" if evlog and "error" in evlog else "no event log"
    if log is not None:
        found, why = closing_statement(log, cfg.headless.min_closing_chars)
        if found is not None:
            closing = {
                "text": found.text,
                "chars": len(found.text),
                "source": found.source,
                "turn_index": found.turn_index,
            }
            closing_reason = ""
        else:
            closing_reason = why

    row = rows[0] if rows else {}
    ev = evlog or {}
    return {
        "schema": SCHEMA,
        "session_id": session_id,
        "found": bool(rows or evlog or blocks or runs or transcript["exists"]),
        "agent": ev.get("agent") or row.get("agent") or None,
        "entrypoint": ev.get("entrypoint"),
        "started_at": ev.get("started_at"),
        "ended_at": ev.get("ended_at"),
        "transcript": transcript,
        "state": row or None,
        "eventlog": evlog,
        "blocks": blocks,
        "runs": runs,
        "closing_statement": closing,
        "closing_statement_reason": closing_reason or None,
        # What could not be read, by source and exception type. Non-empty means a section above
        # may be incomplete; `found: false` with errors is "could not look", not "no trace".
        "errors": errors,
    }


def _brief(doc: dict) -> list[str]:
    t, st, ev = doc["transcript"], doc["state"] or {}, doc["eventlog"] or {}
    current = next((b for b in doc["blocks"] if b["current"]), None)
    subs = ev.get("subagents")
    comps = ev.get("compactions")
    lines = [
        f"session {doc['session_id']}  agent {doc['agent'] or '?'}  "
        f"entrypoint {doc['entrypoint'] or '?'}",
        f"  {doc['started_at'] or '?'} -> {doc['ended_at'] or '?'}",
        f"  transcript: {'present' if t['exists'] else 'gone'}"
        + (f", expires {t['expires']}" if t["exists"] else "")
        + (f"  {t['path']}" if t["path"] else ""),
        f"  state: {st.get('status', 'no row')}" + (f", attempts {st.get('attempts', '?')}" if st else ""),
        f"  event log: {ev.get('path', 'none')}",
    ]
    if ev:
        lines.append(
            "  subagents: "
            + (str(len(subs)) if isinstance(subs, list) else str(subs))
            + "  compactions: "
            + (str(len(comps)) if isinstance(comps, list) else str(comps))
        )
    lines.append(
        f"  blocks: {len(doc['blocks'])}"
        + (f", current {current['file']} ({current['render_mode']})" if current else "")
    )
    lines.append(f"  runs: {len(doc['runs'])}")
    if doc["errors"]:
        lines.append(f"  !! incomplete -- could not read: {'; '.join(doc['errors'])}")
    closing = doc["closing_statement"]
    lines.append(
        f"  closing statement: {closing['chars']} chars from the {closing['source']}"
        if closing
        else f"  closing statement: none ({doc['closing_statement_reason']})"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scribe session",
        description="Everything scribe holds about one session, as one JSON object.",
    )
    ap.add_argument("session_id", help="session id, as carried in a digest's anchor comment")
    ap.add_argument("--config", type=Path, default=None)
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="the full document (the default)")
    out.add_argument("--brief", action="store_true", help="a few lines for a quick look")
    args = ap.parse_args(argv)

    if not SAFE_STEM_RE.match(args.session_id):
        # Used in a filename and a LIKE pattern below, so only a bare id is accepted.
        print(f"scribe: not a session id: {args.session_id!r}", file=sys.stderr)
        return EXIT_USAGE
    try:
        cfg = load(args.config)
    except (ConfigError, OSError, ValueError) as exc:
        # OSError/ValueError: an unreadable config or a bad value `load` does not convert.
        # Either would otherwise exit 1, the "no trace" code (security baseline OV-27).
        print(f"scribe: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        doc = lookup(cfg, args.session_id)
    except Exception as exc:  # the contract must hold whatever goes wrong inside the join
        print(f"scribe: session lookup failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAILED
    try:
        # Inside a guard too: a crash here (a state row missing a column, a broken pipe, an
        # encode error) used to exit 1, the "no trace" code (audit F-05).
        if args.brief:
            print("\n".join(_brief(doc)))
        else:
            json.dump(doc, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as exc:
        print(f"scribe: could not write the report ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAILED
    if doc["errors"]:
        # A partial answer is not a complete one, found or not (security baseline OV-01).
        print(f"scribe: could not look everywhere: {'; '.join(doc['errors'])}", file=sys.stderr)
        return EXIT_FAILED
    if not doc["found"]:
        print(f"scribe: no trace of session {args.session_id!r}", file=sys.stderr)
        return EXIT_UNKNOWN
    return EXIT_FOUND


if __name__ == "__main__":
    sys.exit(main())
