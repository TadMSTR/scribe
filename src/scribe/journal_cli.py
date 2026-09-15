"""`python -m scribe journal` — emit the SessionStart hook payload for one agent.

Written to be called by a hook, which constrains the exit codes more than a human-facing
command would. A SessionStart hook that exits non-zero has its stderr surfaced and the
session continues, so the split is:

  * **0** — a payload was written to stdout. That includes "scribe has produced no digests
    yet", which is the ordinary state before its first run and is reported in the status line
    rather than treated as a failure.
  * **2** — the command could not do its job (bad config, nonsensical arguments). Nothing is
    written to stdout, so the caller can tell a refusal from an empty result without parsing.

Never exits 1 on "nothing to say", because a hook that cried failure on every quiet morning
would be muted within a week and then silent on the morning that mattered.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import ConfigError, load
from .journal import (
    DEFAULT_FILE_COUNT,
    DEFAULT_MAX_LINES,
    agent_dir,
    agent_for_project,
    build_context,
    hook_payload,
    recent_journals,
    status_line,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scribe journal",
        description="Emit the SessionStart hook payload built from this agent's digests.",
    )
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument(
        "--digests",
        default=None,
        help="digest root (default: discovery.output_dir from config)",
    )
    ap.add_argument(
        "--project-dir",
        default=None,
        help="project directory to resolve the agent from (default: $CLAUDE_PROJECT_DIR)",
    )
    ap.add_argument(
        "--agent",
        default=None,
        help="agent name, skipping project-directory resolution",
    )
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument("--files", type=int, default=DEFAULT_FILE_COUNT)
    args = ap.parse_args(argv)

    # Rejected rather than clamped. `--max-lines 0` would slice `lines[-0:]`, which is the
    # WHOLE list — a zero that quietly means "no limit" is how a day's digest ends up
    # injected in full into every session.
    if args.max_lines <= 0:
        print("scribe: --max-lines must be positive", file=sys.stderr)
        return 2
    if args.files <= 0:
        print("scribe: --files must be positive", file=sys.stderr)
        return 2

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"scribe: {exc}", file=sys.stderr)
        return 2

    root = Path(args.digests or cfg.output_dir).expanduser()
    agent = args.agent or agent_for_project(
        args.project_dir or os.environ.get("CLAUDE_PROJECT_DIR", "")
    )

    directory = agent_dir(root, agent) if agent else root
    paths = recent_journals(directory, args.files) if agent else []
    context = build_context(paths, args.max_lines)

    json.dump(hook_payload(context, status_line(agent, directory, paths)), sys.stdout)
    sys.stdout.write("\n")
    return 0
