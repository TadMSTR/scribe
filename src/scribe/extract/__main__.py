"""CLI: `python -m scribe.extract <transcript.jsonl> [--json]`.

Prints the event log as JSON, or a human-readable digest. Exits non-zero on an unreadable
transcript so a caller can tell "nothing happened" from "could not look".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import EventLog
from .parser import extract


def _render_text(log: EventLog) -> str:
    st = log.stats
    out = [
        f"session   {log.session_id or '(unknown)'}",
        f"agent     {log.agent or '(unknown)'}",
        f"window    {log.started_at or '?'} -> {log.ended_at or '?'}",
        f"turns     {st.turns}   events {st.tool_events}   failures {st.failures}",
        f"size      {st.extracted_chars:,} chars (~{st.token_estimate:,} tok) "
        f"from {st.raw_content_chars:,} raw  ratio {st.compression_ratio}",
        f"redacted  {st.secrets_redacted} secret-shaped values {st.redaction_fires or ''}",
        f"budget    level {st.degradation_level}" + ("  OVER BUDGET" if st.budget_exceeded else ""),
        "",
    ]
    for turn in log.turns:
        head = (turn.user_text or "(no user text)").splitlines()[0][:100]
        out.append(f"── turn {turn.index} [{turn.turn_uuid[:8]}] {head}")
        for ev in turn.events:
            mark = " " if ev.ok is not False else "!"
            tgt = ev.target.splitlines()[0][:80] if ev.target else ""
            out.append(f"   {mark} {ev.tool:<44} {tgt}")
        r = turn.rollup.to_dict()
        if r:
            for key, vals in r.items():
                out.append(f"     · {key}: {', '.join(str(v)[:60] for v in vals[:6])}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe.extract",
        description="Extract a structured event log from a Claude Code JSONL transcript.",
    )
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--json", action="store_true", help="emit the event log as JSON")
    ap.add_argument("--indent", type=int, default=None, help="pretty-print JSON")
    ap.add_argument(
        "--max-chars",
        type=int,
        default=200_000,
        help="byte budget before degradation; 0 disables (default: 200000)",
    )
    args = ap.parse_args(argv)

    if not args.transcript.is_file():
        print(f"scribe: not a file: {args.transcript}", file=sys.stderr)
        return 2
    try:
        log = extract(args.transcript, max_session_chars=args.max_chars)
    except OSError as exc:
        print(f"scribe: cannot read {args.transcript}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(log.to_dict(), sys.stdout, ensure_ascii=False, indent=args.indent)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_render_text(log) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
