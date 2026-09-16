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

**The join key is `transcript_path`, not `session_id`.** The state DB's `session_id` column
was empty on all 441 rows — `scan` upserts from a filesystem stat, and the id lives inside the
transcript. Matching the 30 affected sessions by `session_id` returned 0 of 30; by
`transcript_path`, 30 of 30. The column is populated going forward, which does not help for
any row written before that.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ConfigError, load
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


@dataclass
class Found:
    """One block on disk that holds a stand-in rather than a digest."""

    path: Path
    turn_uuid: str
    transcript_path: str
    kind: str
    #: True when the anchor already says so. False means a block from before the marker
    #: existed, which is every one of the 30 this was written for.
    stamped: bool


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
                )
            )
    return found


def stamp(path: Path, blocks: list[Found]) -> int:
    """Write `provisional:` into the anchors of `blocks`, all of which are in `path`.

    One read-modify-replace per file rather than per block: rewriting the same file once per
    stamped anchor would make the offsets of every later match stale.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    wanted = {b.turn_uuid: b.kind for b in blocks if not b.stamped}
    if not wanted:
        return 0

    def sub(m: re.Match[str]) -> str:
        session, turn, transcript, existing = m.groups()
        kind = wanted.get(turn, existing)
        tail = f" provisional:{kind}" if kind else ""
        return f"<!-- session:{session} turn:{turn} transcript:{transcript}{tail} -->"

    updated = ANCHOR_RE.sub(sub, text)
    if updated == text:
        return 0
    atomic_replace(path, updated)
    return len(wanted)


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
        if not Path(tpath).exists():
            # The transcript is the only input a re-run has. Without it there is nothing to
            # summarize, so reopening the row would just burn a sweep rediscovering that.
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
        return 2
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
    if not args.apply and report["blocks"]:
        print("  re-run with --apply, then `scribe run --live`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
