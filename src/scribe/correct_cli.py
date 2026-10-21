"""`scribe correct` — apply the id-conflation rewrite to digests written before it existed.

0.13.0 rewrites a `#N` the QC gate proves is a tracker's internal id into `id N`, after grading
and before the block reaches disk (`qc.id_conflation_edits`). Only NEW digests get that. Every
block written before it still carries the wrong ref, and those blocks still reach every agent's
session-start injection and the indexer. This applies the same rewrite to them.

**Dry run by default.** It reads the corpus and the event logs, and writes nothing: no digest,
no `index.jsonl` line, no ledger record, no backup.

**What it changes, and what it never does.** For every complete, final block with a loadable
event log, it re-grades the block with the current `check_digest` and applies exactly the edit
the live path would. Nothing else:

  * no free-form or hand edits -- there is no option that takes a replacement;
  * provisional blocks are skipped (the next sweep replaces them anyway);
  * torn blocks are skipped, as `manifest.rebuild` skips them: their extent is unknown;
  * a block whose event log is missing or unloadable is skipped and COUNTED, never guessed.
    The gate needs the log to know a number is an id; without it, the ref stays as written;
  * a file that is not valid UTF-8 is skipped and counted (audit F-03). Decoding it with
    `errors="replace"` and writing it back would turn every bad byte, in blocks this never
    meant to touch, into U+FFFD -- an edit the ledger could not describe.

**Bytes in, bytes out.** A file is read as bytes and decoded strictly, with no newline
translation, and that exact text is what is spliced and written back. A `\r` a model put in a
digest survives. `Path.read_text` translates newlines, so comparing its result with the bytes
before the write made every CR-bearing file look changed on disk, forever (audit F-01).

**`--apply` needs the sweep's lock.** scribe has no lock of its own: the cron line's `flock -n
-E 75 <lock>` is the only mutual exclusion. A `correct --apply` racing the sweep would
read-modify-replace a daily file while `append_block` appends to it, and the replace would
drop the appended block. So `--apply` takes an exclusive `flock(2)` on `--lock PATH` -- name the
cron's lock file, since `flock(1)` takes the same kind of lock and the two exclude each other --
and exits `75` if it is held. Without `--lock`, `--apply` refuses unless `--no-lock` is passed,
for a corpus no sweep writes to.

**Per file, in this order:** copy the file to `<backup_root>/correct-<UTC>/<relative path>`
(0600, directories 0700); splice every changed block's body in one read-modify-replace via
`writeback.atomic_replace`; then `pipeline.after_write`, the sequence the sweep runs -- a
manifest line per changed block (invariant 17: without it `scribe index --check` goes red on
correct data), a `source: correct` ledger record per changed block, and the hook once. A file
whose backup cannot be written is not touched, and the run stops there. So does any failure
after that, in the replace or in `after_write`; the report still prints what was done before it
and where the backup is (audit F-02).

**Idempotent.** `id 1258` is not a ticket claim, so a second `--apply` finds nothing, writes no
ledger line, makes no backup directory and exits 0.

**What this does not change.** `runs.jsonl` keeps measuring the model's original output -- that
is the point of grading before rewriting. `qc-survey` re-grades what is on disk, so it will
report fewer id conflations after an apply. That is this command, not the prompt improving.

## Exit codes

===  ========================================================================
  0  Nothing to correct, or `--apply` corrected everything it found.
  1  Corrections are pending: a dry run found some, or `--apply` left a file alone
     because it changed on disk after the scan. The same contract as `recover`'s dry
     run, so a monitor can alert on it.
  2  Usage or config error, including `--apply` with neither `--lock` nor
     `--no-lock`, and a lock file that cannot be opened.
  4  The tool FAILED (as `recover`'s `EXIT_INTERNAL`): a bug, an environment failure,
     or a backup or replace that could not be written. Files corrected before the
     failure stay corrected and recorded; the report says how far it got.
 75  The lock is held -- a sweep is running. Nothing was written. Re-run later.
===  ========================================================================

The failure path prints a traceback to stderr. Nothing wires this command to an alert today; if
something ever does, re-read the SECURITY note in `recover_cli.main` first, because `correct`
parses digest text and that note's premise does not hold here.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import corrections
from .config import Config, ConfigError, load
from .eventlog import EventLogError, load_eventlog, session_eventlog
from .manifest import record_for_turn
from .paths import DIR_MODE, FILE_MODE, secure_dir
from .pipeline import AfterWrite, after_write
from .qc import check_digest, id_conflation_edits
from .writeback import ANCHOR_RE, atomic_replace, paired_blocks

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 4
#: The cron's `flock -n -E 75`: a skipped run, which a scheduler wrapper reports as ok.
EXIT_LOCKED = 75
#: Sample edits the human report prints. `--json` carries all of them.
SAMPLE_EDITS = 20


class CorrectError(RuntimeError):
    """A step that must stop the run: today, only a backup that could not be written."""


@dataclass
class Change:
    """One block the rewrite changes."""

    path: Path
    digest: str
    session_id: str
    turn_uuid: str
    #: The block body's span in the file as scanned: anchor end to terminator start.
    span: tuple[int, int]
    body: str
    #: 1-based line of the block's anchor in the file, for the human report.
    anchor_line: int
    sha256_before: str
    #: `{"line", "before", "after"}`, `line` within the block (anchor = 1).
    edits: list[dict] = field(default_factory=list)


@dataclass
class Scan:
    blocks: int = 0
    no_eventlog: int = 0
    bad_eventlog: int = 0
    provisional: int = 0
    torn: int = 0
    #: Files skipped whole because they are not valid UTF-8.
    undecodable: int = 0
    changes: list[Change] = field(default_factory=list)
    #: path -> the exact bytes the changes were computed against.
    raws: dict[Path, bytes] = field(default_factory=dict)

    @property
    def refs(self) -> int:
        return sum(len(c.edits) for c in self.changes)

    @property
    def files(self) -> list[Path]:
        return sorted({c.path for c in self.changes})


def scan(cfg: Config) -> Scan:
    """Every change the rewrite would make, computed from disk. Pure: writes nothing."""
    root = Path(cfg.output_dir).expanduser()
    evroot = Path(cfg.eventlog_dir).expanduser()
    out = Scan()
    for md in sorted(root.rglob("*.md")):
        if not md.is_file():
            continue
        raw = md.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            out.undecodable += 1
            continue
        pairs = paired_blocks(text)
        out.torn += len(list(ANCHOR_RE.finditer(text))) - len(pairs)
        for a, c in pairs:
            out.blocks += 1
            if a.group(4):
                out.provisional += 1
                continue
            session_id, turn_uuid, transcript = a.group(1), a.group(2), a.group(3)
            ev = session_eventlog(evroot, session_id, transcript)
            if not ev.is_file():
                out.no_eventlog += 1
                continue
            try:
                log = load_eventlog(ev)
            except EventLogError:
                out.bad_eventlog += 1
                continue
            body = text[a.end() : c.start()]
            # `first_line=1`: the body starts with the newline that ends the anchor line, so
            # its first character is already on the block's line 1.
            new_body, edits = id_conflation_edits(body, check_digest(body, log), first_line=1)
            if not edits:
                continue
            # The hash `index.jsonl` holds for this block, derived by the manifest's own code, so
            # `sha256_before` and `sha256_after` are the same kind of hash however the file
            # spells its newlines.
            rec = record_for_turn(cfg, md, turn_uuid)
            out.changes.append(
                Change(
                    path=md,
                    digest=rec["path"],
                    session_id=session_id,
                    turn_uuid=turn_uuid,
                    span=(a.end(), c.start()),
                    body=new_body,
                    anchor_line=text.count("\n", 0, a.start()) + 1,
                    sha256_before=rec["sha256"],
                    edits=edits,
                )
            )
            out.raws[md] = raw
    return out


def take_lock(path: Path) -> int | None:
    """An exclusive, non-blocking `flock(2)` on `path`: the fd, or None if it is held.

    The same lock `flock(1)` takes, so this and the cron's `flock -n` exclude each other.
    Released when the process exits; the fd is never closed early. `OSError` (the file cannot
    be opened) propagates: it is a bad `--lock`, not a held one.
    """
    fd = os.open(path.expanduser(), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _backup_run_dir(cfg: Config, now: datetime) -> Path:
    """A fresh `correct-<UTC>` directory under the backup root, created 0700, never reused."""
    root = secure_dir(cfg.backup_root())
    stem = f"correct-{now:%Y%m%dT%H%M%SZ}"
    for n in range(100):
        run = root / (stem if n == 0 else f"{stem}-{n}")
        try:
            os.mkdir(run, DIR_MODE)
        except FileExistsError:
            continue
        run.chmod(DIR_MODE)
        return run
    raise CorrectError(f"no free backup directory under {root} for {stem}")


def _backup(dest: Path, data: bytes) -> None:
    """Write `data` -- the file's original bytes -- to `dest`, 0600 from creation, fsynced."""
    secure_dir(dest.parent)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


@dataclass
class Applied:
    files: int = 0
    blocks: int = 0
    refs: int = 0
    backup_dir: str = ""
    corrections_appended: int = 0
    #: Files that changed on disk between the scan and the write, and were left alone.
    skipped_changed: list[str] = field(default_factory=list)
    manifest_errors: list[str] = field(default_factory=list)
    correction_errors: list[str] = field(default_factory=list)
    frontmatter_errors: list[str] = field(default_factory=list)
    hook_ok: int = 0
    hook_failed: list[str] = field(default_factory=list)
    #: Why the run stopped early, or "".
    aborted: str = ""


def apply(cfg: Config, found: Scan, *, now: datetime | None = None) -> Applied:
    """Write `found`'s changes. Call only while holding the sweep's lock (or with no sweep)."""
    done = Applied()
    run_dir: Path | None = None
    root = Path(cfg.output_dir).expanduser()
    by_file: dict[Path, list[Change]] = {}
    for ch in found.changes:
        by_file.setdefault(ch.path, []).append(ch)
    for path in sorted(by_file):
        changes = by_file[path]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            done.aborted = f"re-read of {path.relative_to(root)} failed: {exc}"
            break
        if raw != found.raws[path]:
            # Not under a held lock, or something other than scribe wrote it. The spans are
            # stale and splicing them would land the edit somewhere arbitrary.
            done.skipped_changed.append(str(path.relative_to(root)))
            continue
        original = raw.decode("utf-8")
        text = original
        # Reverse order, so each splice cannot shift the spans not yet applied.
        for ch in sorted(changes, key=lambda c: c.span, reverse=True):
            start, end = ch.span
            text = text[:start] + ch.body + text[end:]
        # The rename proves the write was whole, not that the content is right (FW-13). A
        # splice must leave every block where it was: same turns, same order, same count.
        before_turns = [a.group(2) for a, _c in paired_blocks(original)]
        if [a.group(2) for a, _c in paired_blocks(text)] != before_turns:
            done.aborted = f"{path.relative_to(root)}: the splice moved a block, file not touched"
            break
        try:
            if run_dir is None:
                run_dir = _backup_run_dir(cfg, now or datetime.now(UTC))
                done.backup_dir = str(run_dir)
            _backup(run_dir / path.relative_to(root), raw)
        except (OSError, CorrectError) as exc:
            done.aborted = f"backup of {path.relative_to(root)} failed, file not touched: {exc}"
            break
        rel = str(path.relative_to(root))
        try:
            atomic_replace(path, text)
        except OSError as exc:
            # Earlier files are corrected and recorded, and the backup exists: the report has
            # to survive this, so it is recorded here rather than raised (audit F-02).
            with contextlib.suppress(OSError):
                path.with_name(path.name + ".tmp").unlink(missing_ok=True)
            done.aborted = f"replace of {rel} failed, file not touched: {type(exc).__name__}: {exc}"
            break
        done.files += 1
        done.blocks += len(changes)
        done.refs += sum(len(c.edits) for c in changes)
        try:
            written = _after_write(cfg, path, changes)
        except Exception as exc:  # `after_write` counts its own failures; this is a bug's net
            done.aborted = f"{rel} was corrected, then its post-write steps failed: {exc!r}"
            break
        done.corrections_appended += written.corrections_appended
        done.manifest_errors += [f"{rel} turn:{t}: {e}" for t, e in written.manifest_errors.items()]
        done.correction_errors += [
            f"{rel} turn:{t}: {e}" for t, e in written.correction_errors.items()
        ]
        if written.frontmatter_error:
            done.frontmatter_errors.append(f"{rel}: {written.frontmatter_error}")
        if written.hook == "ok":
            done.hook_ok += 1
        elif written.hook == "failed":
            done.hook_failed.append(f"{rel}: {written.hook_error}")
    return done


def _after_write(cfg: Config, path: Path, changes: list[Change]) -> AfterWrite:
    """`pipeline.after_write` for every changed block in one file, as `source: correct`."""
    return after_write(
        cfg,
        path,
        [
            (
                ch.turn_uuid,
                {
                    "source": corrections.SOURCE_CORRECT,
                    "session_id": ch.session_id,
                    "turn_uuid": ch.turn_uuid,
                    "edits": ch.edits,
                    "sha256_before": ch.sha256_before,
                },
            )
            for ch in changes
        ],
    )


def _edits(found: Scan) -> list[dict]:
    return [
        {
            "digest": ch.digest,
            "turn_uuid": ch.turn_uuid,
            "line": e["line"],
            "file_line": ch.anchor_line + e["line"] - 1,
            "before": e["before"],
            "after": e["after"],
        }
        for ch in found.changes
        for e in ch.edits
    ]


def _report(found: Scan) -> dict:
    return {
        "blocks_scanned": found.blocks,
        "blocks_to_change": len(found.changes),
        "refs": found.refs,
        "files": len(found.files),
        "skipped": {
            "no_eventlog": found.no_eventlog,
            "bad_eventlog": found.bad_eventlog,
            "provisional": found.provisional,
            "torn": found.torn,
            "undecodable_files": found.undecodable,
        },
    }


def _print_scan(found: Scan, mode: str) -> None:
    r = _report(found)
    s = r["skipped"]
    print(
        f"{mode}: {r['blocks_scanned']} block(s) scanned, {r['blocks_to_change']} to change, "
        f"{r['refs']} ref(s), {r['files']} file(s)"
    )
    print(
        f"  skipped: {s['no_eventlog']} without an event log, {s['bad_eventlog']} with an "
        f"unloadable event log, {s['provisional']} provisional, {s['torn']} torn, "
        f"{s['undecodable_files']} file(s) not valid UTF-8"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scribe correct",
        description="Rewrite id-conflated ticket refs in digests written before the live rewrite.",
    )
    ap.add_argument("--config", type=Path, default=None, help="path to scribe.toml")
    ap.add_argument(
        "--apply", action="store_true", help="write the corrections (default: report only)"
    )
    lock = ap.add_mutually_exclusive_group()
    lock.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="with --apply: the lock file the sweep's cron holds (flock -n); exit 75 if held",
    )
    lock.add_argument(
        "--no-lock",
        action="store_true",
        help="with --apply: run without a lock, for a corpus no sweep writes to",
    )
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args(argv)

    if args.apply and args.lock is None and not args.no_lock:
        print(
            "scribe: --apply needs --lock PATH naming the sweep's lock file, or --no-lock",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not cfg.qc_rewrite_id_conflations:
        print("nothing to do: qc.rewrite_id_conflations is off in this config")
        return EXIT_OK

    if args.apply and args.lock is not None:
        try:
            fd = take_lock(args.lock)
        except OSError as exc:
            print(f"scribe: cannot open lock file {args.lock}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if fd is None:
            print(f"scribe: {args.lock} is held -- a sweep is running; nothing written")
            return EXIT_LOCKED

    try:
        # Under the lock when applying, so nothing the sweep writes can land between the scan
        # and the replace.
        found = scan(cfg)
        done = apply(cfg, found) if args.apply and found.changes else None
    except Exception as exc:  # see recover_cli.main: a crash must not read as a finding
        print(f"scribe: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL

    if args.json:
        payload = {**_report(found), "applied": args.apply, "edits": _edits(found)}
        if done is not None:
            payload["result"] = done.__dict__
        print(json.dumps(payload, indent=2))
    else:
        _print_scan(found, "applied" if args.apply else "dry run")
        if done is None:
            for e in _edits(found)[:SAMPLE_EDITS]:
                print(f"  {e['digest']}:{e['file_line']}  {e['before']} -> {e['after']}")
            if found.refs > SAMPLE_EDITS:
                print(f"  ... {found.refs - SAMPLE_EDITS} more (--json for all)")
            if found.changes:
                print("  re-run with --apply --lock <the sweep's lock file>")
        else:
            _print_applied(cfg, done)

    if done is not None:
        if done.aborted:
            return EXIT_INTERNAL
        # A file left alone is a correction still pending, not a completed apply (audit F-01).
        return EXIT_PENDING if done.skipped_changed else EXIT_OK
    return EXIT_PENDING if found.changes and not args.apply else EXIT_OK


def _print_applied(cfg: Config, done: Applied) -> None:
    print(f"  {done.blocks} block(s) corrected, {done.refs} ref(s), in {done.files} file(s)")
    if done.backup_dir:
        print(f"  backup: {done.backup_dir}")
    ledger = cfg.corrections_file()
    print(
        f"  corrections ledger: {done.corrections_appended} record(s) -> {ledger}"
        if ledger
        else "  corrections ledger: off (index.corrections_path is empty)"
    )
    for rel in done.skipped_changed:
        print(f"  !! {rel} changed on disk after the scan -- left alone, re-run")
    for err in done.manifest_errors:
        print(f"  !! manifest line not appended ({err}) -- run `scribe index --rebuild`")
    for err in done.correction_errors:
        print(f"  !! ledger record not written ({err})")
    for err in done.frontmatter_errors:
        print(f"  !! frontmatter: {err}")
    if done.hook_ok or done.hook_failed:
        print(f"  on_digest_written: {done.hook_ok} ok, {len(done.hook_failed)} failed")
        for err in done.hook_failed:
            print(f"    {err}")
    if done.aborted:
        print(f"  !! stopped: {done.aborted}")


if __name__ == "__main__":
    sys.exit(main())
