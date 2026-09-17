"""Feed the SessionStart journal injection from scribe's digests.

**This is the only memory push surface that demonstrably reaches a CloudCLI agent session.**
Measured live 2026-09-15: a hook emitting `hookSpecificOutput.additionalContext` is surfaced;
the sibling `UserPromptSubmit` hook emits a bare `systemMessage` and CloudCLI drops it for
agent sessions. So the payload shape below is not a convention to tidy later -- it is the
whole reason this module exists, and `additionalContext` is the field that carries content.

The incumbent producer is the memsearch plugin: its Stop hook appends a raw transcript block
to `<project>/.memsearch/memory/YYYY-MM-DD.md` and the `memsearch-summarize` service rewrites
it in place. Both are being retired. Nothing about that retirement would have failed loudly
-- `stop.sh`'s own comment says its raw block "is only transient because memsearch-summarize
replaces it" -- so the journal would have filled with raw transcript rather than emptied.

**The format was never in question, and that was established by measurement.** The incumbent
consumer's parser was run verbatim over a real scribe digest: 177 lines of clean output,
unmodified. `preview` below is a port of that parser, and `tests/test_journal.py` holds it to
byte-for-byte agreement with the original (kept at `tests/reference/recent_memory_preview.awk`).
Porting rather than shelling out to awk buys a testable consumer: the property worth
asserting is "the digests still parse", and a test that only checked scribe wrote a file to a
path would pass unchanged on the day the content stopped parsing.

One incumbent quirk is deliberately NOT reproduced. Its context assembly is built in bash
double quotes -- `context="# Recent Memory\n\n"` -- where `\n` is two literal characters, not
a newline, and `jq -Rs` then faithfully encodes the backslash. Live injected context on forge
carries visible `\n` between the heading and the first file. `build_context` writes real
newlines.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .extract.parser import agent_for_path
from .paths import contained, valid_agent

#: Lines kept per file. The incumbent's `tail -n 40`, and the reason a long day's digest
#: cannot crowd out the previous day's.
DEFAULT_MAX_LINES = 40
#: Files read, most recent first. Two, as the incumbent did.
DEFAULT_FILE_COUNT = 2

HOOK_EVENT = "SessionStart"
CONTEXT_HEADING = "# Recent Memory"

#: `[[:space:]]` minus `\n`, which cannot occur inside a record. Spelled out rather than
#: written `\s`, which in Python also matches unicode separators the POSIX class does not.
_SP = r"[ \t\v\f\r]"
_SECTION_RE = re.compile(rf"^##{_SP}")
_SUBHEAD_RE = re.compile(rf"^#{{3,4}}{_SP}")
_BULLET_RE = re.compile(rf"^-{_SP}")

#: `find -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md'`, as the incumbent globbed.
_JOURNAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def preview(text: str, max_lines: int = DEFAULT_MAX_LINES) -> str:
    """Reduce a digest to the lines the SessionStart consumer keeps.

    Keeps `##` sections, `###`/`####` subheads and `- ` bullets; emits a section only if it
    carried at least one bullet; returns the last `max_lines` lines of the result.

    **A section with no bullets is dropped, and that is load-bearing rather than cosmetic.**
    The journals this replaces accumulated heading-only stubs from sessions that produced no
    content, and a stub that survived here would occupy one of the two injection slots while
    saying nothing.
    """
    if max_lines <= 0:
        raise ValueError("max_lines must be positive")

    out: list[str] = []
    section: list[str] = []
    has_body = False

    def flush() -> None:
        nonlocal section, has_body
        if section and has_body:
            out.extend(section)
        section = []
        has_body = False

    for line in text.split("\n"):
        if _SECTION_RE.match(line):
            # A new `##` closes the previous section, which is why a trailing section is
            # only emitted by the flush after the loop.
            flush()
            section.append(line)
        elif _SUBHEAD_RE.match(line):
            section.append(line)
        elif _BULLET_RE.match(line):
            section.append(line)
            has_body = True
    flush()

    return "\n".join(out[-max_lines:])


def recent_journals(
    directory: str | os.PathLike[str], limit: int = DEFAULT_FILE_COUNT
) -> list[Path]:
    """The `limit` most recent `YYYY-MM-DD.md` files in `directory`, newest first.

    Sorting by name is sorting by date for this filename shape, and it is what the incumbent
    did (`sort -r`), so a file whose mtime was touched by a copy or a restore cannot reorder
    the journal. Compare `birthtime`, which `cp -p` resets.

    **Symlinks are not followed**, matching `find -type f` without `-L`. That is a guard and
    not a detail: everything reachable from here is read and injected into a session, so a
    symlink dropped into the digest directory would be a read primitive pointed at whatever
    the operator's account can open.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    try:
        entries = list(os.scandir(Path(directory).expanduser()))
    except OSError:
        # Absent before scribe's first run, which is an ordinary state and not an error.
        return []
    root = Path(directory).expanduser()
    names = sorted(
        (e.name for e in entries if _JOURNAL_RE.match(e.name) and e.is_file(follow_symlinks=False)),
        reverse=True,
    )
    return [root / n for n in names[:limit]]


def agent_dir(digest_root: str | os.PathLike[str], agent: str) -> Path:
    """`<digest_root>/<agent>` -- scribe's layout, and the journal's partition.

    scribe writes per agent and the journal is per project. On forge those are the same set
    under two names: a project directory is `~/.claude/projects/<agent>`. `agent_for_project`
    is where that equivalence is stated; nothing else may assume it.

    **Two guards, and the second is the one that actually holds.** `valid_agent` rejects the
    name; the containment check rejects the resulting path. A character allowlist states an
    intention about names, and the property that matters is about paths -- everything under
    the directory this returns is read and injected into a session, so a traversal here is a
    read primitive, not a tidiness problem. Containment is asserted against the resolved path
    so it cannot be satisfied by a name that merely looks well-formed.

    Raises `ValueError` rather than falling back to a default. Silently substituting
    `unknown` for a rejected name would turn a refusal into an empty injection, which is
    indistinguishable from a quiet day -- the exact ambiguity this component exists to remove.
    """
    root = Path(digest_root).expanduser()
    if not valid_agent(agent):
        raise ValueError(f"not a usable agent name: {agent!r}")
    if not contained(root, agent):
        raise ValueError(f"agent directory escapes the digest root: {root / agent}")
    return root / agent


def agent_for_project(project_dir: str) -> str:
    """Recover the agent name from a project directory, in either form it arrives in.

    Delegates to `extract.parser.agent_for_path` **so the reader and the writer resolve a
    name the same way**. They did not, briefly, and the failure was silent in the direction
    that matters: the extractor truncated `doc-health` to `doc` and wrote its digests there,
    while a hook resolving `$CLAUDE_PROJECT_DIR` looked in `doc-health` and found nothing.
    An empty injection is indistinguishable from a quiet day, which is the whole failure mode
    this component exists to remove -- so the two sides share one function rather than two
    that agree today.

    Returns "" for anything outside `~/.claude/projects`, which is correct rather than
    defensive: there is no agent there, and guessing a name would silently inject one agent's
    sessions into another's context. Names that are not a single usable path component are
    refused here too, so a rejected name never reaches a path join.
    """
    agent = agent_for_path(project_dir)
    return agent if valid_agent(agent) else ""


def build_context(paths: list[Path], max_lines: int = DEFAULT_MAX_LINES) -> str:
    """Assemble the injected block, or "" when no file yielded any content.

    A file that exists but previews to nothing contributes no heading. The empty string is
    the signal `hook_payload` uses to omit `hookSpecificOutput` entirely -- injecting a bare
    "# Recent Memory" with nothing under it would spend context to say nothing.
    """
    blocks: list[str] = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Vanished or unreadable between listing and reading. One unreadable file must
            # not cost the other its injection.
            continue
        body = preview(text, max_lines)
        if body:
            blocks.append(f"## {p.name}\n{body}")
    if not blocks:
        return ""
    return f"{CONTEXT_HEADING}\n\n" + "\n\n".join(blocks) + "\n"


def hook_payload(context: str, status: str) -> dict:
    """The JSON a SessionStart hook writes to stdout.

    `systemMessage` is always present and `hookSpecificOutput` only when there is something to
    inject. They reach different audiences: CloudCLI surfaces `additionalContext` to the agent
    and drops a bare `systemMessage` for agent sessions, so the status line is an operator
    signal in a terminal session and the context is the payload.
    """
    payload: dict = {"systemMessage": status}
    if context:
        payload["hookSpecificOutput"] = {
            "hookEventName": HOOK_EVENT,
            "additionalContext": context,
        }
    return payload


def status_line(agent: str, directory: Path, paths: list[Path]) -> str:
    """One line naming what was found, or why nothing was.

    Silence is the failure mode this whole component exists to prevent, so "no digests" is
    reported rather than merely resulting in an empty injection. The directory is named
    because the two ways this goes wrong -- scribe not running, and scribe writing somewhere
    else -- are indistinguishable without it.
    """
    if not agent:
        return "[scribe] journal: no agent resolved from the project directory — nothing injected"
    if not paths:
        return f"[scribe] journal: {agent} | no digests under {directory}"
    return f"[scribe] journal: {agent} | {len(paths)} file(s) | latest {paths[0].name}"
