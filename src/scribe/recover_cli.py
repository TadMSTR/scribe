"""`scribe recover` — reopen sessions whose digest was never really written.

Two jobs, and the first is the one that is easy to get wrong.

**Stamp the legacy blocks.** 30 sessions were lost before provisional anchors existed
(vikunja#872, #868), so their blocks are indistinguishable from real digests to
`append_block` — that is precisely why the loss was permanent. They are identifiable only by
their *body*: `render.PLACEHOLDER_MARKER` or `contamination.SUPPRESSION_MARKER`. This stamps
`provisional:` into those anchors so the ordinary write path can replace them.

**Reset the state.** A provisional session's retry budget is spent, and a summarized one will
never be offered at all. `Store.reset_for_retry` clears the turns, the offset and the counter,
so the next `scribe run` re-summarizes it.

Both are idempotent. Running twice stamps nothing new and resets rows that are already
resettable, which is the property that makes it safe to run before you have decided whether
you meant it.

**The join key is `transcript_path`, not `session_id`.** `scan` upserts from a filesystem
stat, and the id lives inside the transcript, so the column was empty on every legacy row.
Matching the 30 affected sessions by `session_id` returned 0 of 30; by `transcript_path`,
30 of 30. `transcript_path` remains the join key — the backfill that populated `session_id`
for the legacy rows does not change that, because the two are equivalent only where the
basename convention holds.

## Exit codes — this is an INTERFACE

A scheduled detector is wired to these (part 5 of the memory-consolidation-2026-09 programme,
vikunja#875), so they are a contract rather than an implementation detail.

===  ============================================================================
  0  Nothing is lost. A dry run found no provisional block on disk, **or** an
     `--apply` run completed without error.
  1  Unrepaired loss, and it is recoverable. Provisional blocks are on disk and
     every affected session can be re-summarized.
  2  `ConfigError`. Pre-existing, unchanged.
  3  Unrepaired loss that re-running will NOT fix -- at least one affected session
     has neither a transcript nor a usable event log. Needs a human.
===  ============================================================================

Two properties are load-bearing and easy to break:

**The DRY RUN is the detector; `--apply` is the repair.** `--apply` returns 0 whenever it
completes, even if it leaves unrecoverable blocks behind. A cron that repairs and then reports
failure pages every single time it works -- that is the vikunja#398 shape, where a QC script's
success path was indistinguishable from a dead pipeline. Poll with a dry run.

**`1` clears when the loss is repaired, not when `--apply` runs.** `scan_corpus` counts
stamped blocks too, deliberately: a stamped block is still a stand-in, and the loss is only
actually gone once `scribe run --live` has replaced it with a digest. So the signal stays red
across `--apply` until the summarizer has done its half. That is the honest reading of "is
anything lost", and it is why the two verbs return different things for the same corpus.

`3` outranks `1`, because the response differs: `1` is "run the repair", `3` is "this one is
not coming back by itself".
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ConfigError, load
from .eventlog import EventLogError, load_eventlog, session_eventlog
from .state import Store
from .summarize.contamination import SUPPRESSION_MARKER
from .summarize.render import PLACEHOLDER_MARKER
from .writeback import (
    ANCHOR_RE,
    PROVISIONAL_PLACEHOLDER,
    PROVISIONAL_SUPPRESSED,
    atomic_replace,
    paired_blocks,
)

#: See the module docstring -- these are consumed by a scheduled detector, not just by a human
#: reading a terminal, so they are named rather than written as literals at the return sites.
EXIT_OK = 0
EXIT_RECOVERABLE = 1
EXIT_CONFIG = 2
EXIT_UNRECOVERABLE = 3


@dataclass
class Found:
    """One block on disk that holds a stand-in rather than a digest."""

    path: Path
    turn_uuid: str
    transcript_path: str
    kind: str
    #: True when the anchor already says so. False means a block from before the marker
    #: existed, which is every one of the 22 this was written for.
    stamped: bool
    #: Byte span of **this** anchor in the file. Carried so `stamp` can rewrite the exact
    #: occurrence `paired_blocks` identified, rather than re-finding it by uuid.
    span: tuple[int, int]


def scan_corpus(output_dir: Path) -> list[Found]:
    """Every provisional block in the digest corpus, stamped or not.

    Blocks are recognised by body, not by anchor, because the whole point is to find the ones
    whose anchor does not say. A block without a terminator is skipped: it was never written
    whole, its extent is unknown, and `append_block` already treats it as absent.
    """
    found: list[Found] = []
    for md in sorted(Path(output_dir).expanduser().rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m, close in paired_blocks(text):
            body = text[m.end() : close.start()]
            if PLACEHOLDER_MARKER in body:
                kind = PROVISIONAL_PLACEHOLDER
            elif SUPPRESSION_MARKER in body:
                kind = PROVISIONAL_SUPPRESSED
            else:
                continue
            found.append(
                Found(
                    path=md,
                    turn_uuid=m.group(2),
                    transcript_path=m.group(3),
                    kind=kind,
                    stamped=bool(m.group(4)),
                    span=(m.start(), m.end()),
                )
            )
    return found


def stamp(path: Path, blocks: list[Found]) -> int:
    """Write `provisional:` into the anchors of `blocks`, all of which are in `path`.

    **Splices the exact byte spans `scan_corpus` recorded**, rather than re-finding the
    anchors by uuid. The difference is not stylistic. The previous version ran
    `ANCHOR_RE.sub` over the whole file and decided per match on `wanted.get(turn)`, which
    rewrites *every* anchor-shaped match carrying that uuid — including one quoted inside
    another block's body, which this corpus has already been shown to produce for the
    terminator half of the syntax. `scan_corpus` knows which occurrence is the real block; a
    uuid lookup throws that knowledge away and then guesses. (MEDIUM, scribe-digest-loss
    audit 2026-09-16.)

    Spans are applied in reverse order so that each splice cannot shift the offsets of the
    ones not yet applied. One read-modify-replace per file for the same reason.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    pending = sorted((b for b in blocks if not b.stamped), key=lambda b: b.span, reverse=True)
    if not pending:
        return 0

    updated = text
    for b in pending:
        start, end = b.span
        m = ANCHOR_RE.match(text, start, end)
        if m is None or m.group(2) != b.turn_uuid:
            # The file changed under us between scan and stamp. Skipping is correct: a stale
            # span is the one case where writing would land the attribute somewhere arbitrary.
            continue
        session, turn, transcript, _existing = m.groups()
        replacement = (
            f"<!-- session:{session} turn:{turn} transcript:{transcript} provisional:{b.kind} -->"
        )
        updated = updated[:start] + replacement + updated[end:]

    if updated == text:
        return 0
    atomic_replace(path, updated)
    return len(pending)


def _has_eventlog(cfg: Config, store: Store, transcript_path: str) -> bool:
    """Whether this session's persisted event log is on disk and actually loadable.

    Loadable, not merely present. A truncated or half-written file passes `is_file()` and
    then fails at replay time -- which would have `recover` report a session as recoverable
    and the next `scribe run` quietly fail it, splitting the diagnosis across two runs and
    two logs. The parse is cheap next to the model call it precedes.
    """
    row = store.get(transcript_path)
    session_id = row.session_id if row else ""
    path = session_eventlog(cfg.eventlog_dir, session_id, transcript_path)
    if not path.is_file():
        return False
    try:
        load_eventlog(path)
    except EventLogError:
        return False
    return True


def recover(cfg: Config, store: Store, *, apply: bool) -> dict:
    """Find every stand-in block, stamp it, and reopen its session. Returns a report."""
    found = scan_corpus(Path(cfg.output_dir))
    by_file: dict[Path, list[Found]] = {}
    for f in found:
        by_file.setdefault(f.path, []).append(f)

    stamped = 0
    reset: list[str] = []
    missing: list[str] = []
    for path, blocks in by_file.items():
        if apply:
            stamped += stamp(path, blocks)
        else:
            stamped += sum(1 for b in blocks if not b.stamped)

    for tpath in sorted({f.transcript_path for f in found}):
        if not Path(tpath).exists() and not _has_eventlog(cfg, store, tpath):
            # Neither input exists. THIS is the terminal case -- and it used to be reached by
            # a missing transcript alone, because the transcript was the only thing a re-run
            # could read. `load_eventlog` gives the replay a second source (vikunja#873), so
            # "the transcript aged out" is no longer the same condition as "nothing survives".
            missing.append(tpath)
            continue
        if apply:
            if store.reset_for_retry(tpath):
                reset.append(tpath)
            else:
                missing.append(tpath)
        elif store.get(tpath) is not None:
            reset.append(tpath)
        else:
            missing.append(tpath)

    return {
        "blocks": len(found),
        "placeholders": sum(1 for f in found if f.kind == PROVISIONAL_PLACEHOLDER),
        "suppressed": sum(1 for f in found if f.kind == PROVISIONAL_SUPPRESSED),
        "stamped": stamped,
        "reset": sorted(reset),
        "missing": sorted(missing),
        "applied": apply,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scribe recover",
        description="Reopen sessions whose digest was never written, so the next run redoes them.",
    )
    ap.add_argument("--config", type=Path, default=None, help="path to scribe.toml")
    ap.add_argument("--state", type=Path, default=None, help="override the state database")
    # Dry by default, like `scribe run`. This mutates the digest corpus and the state DB, and
    # the operator should be able to look before touching either.
    ap.add_argument(
        "--apply",
        action="store_true",
        help="stamp the blocks and reset the sessions (default: report only)",
    )
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    store = Store(args.state or cfg.state_path)
    report = recover(cfg, store, apply=args.apply)

    mode = "applied" if report["applied"] else "dry run"
    print(
        f"{mode}: {report['blocks']} provisional block(s) "
        f"({report['placeholders']} placeholder, {report['suppressed']} suppressed)"
    )
    print(f"  {report['stamped']} anchor(s) {'stamped' if args.apply else 'to stamp'}")
    print(f"  {len(report['reset'])} session(s) {'reopened' if args.apply else 'to reopen'}")
    for tpath in report["missing"]:
        print(f"  !! no transcript or state row: {tpath}")

    # `--apply` is the REPAIR, and a repair that worked exits 0. Reporting the blocks it just
    # stamped as a failure would page an operator every time the thing succeeded (vikunja#398).
    if args.apply:
        return EXIT_OK

    if not report["blocks"]:
        return EXIT_OK

    print("  re-run with --apply, then `scribe run --live`")
    # `missing` is not "worse luck this sweep", it is "no input exists" -- re-running changes
    # nothing, so it gets its own code and outranks the recoverable one. Phase 3's
    # `load_eventlog` shrinks this set rather than redefining it: a session whose transcript
    # is gone but whose event log survives stops being missing and becomes recoverable, so
    # the signal improves without the contract moving.
    if report["missing"]:
        return EXIT_UNRECOVERABLE
    return EXIT_RECOVERABLE


if __name__ == "__main__":
    sys.exit(main())
