"""Turn one Claude Code JSONL transcript into a structured event log.

No LLM, no network, no writes to the transcript. `~/.claude/projects/*/*.jsonl` is the only
durable record of what actually happened and Claude Code's unset `cleanupPeriodDays` already
caps it at 30 days (vikunja#778), so this module opens transcripts read-only and never
writes back to that tree under any code path.

What the pipeline this replaces did, and why it is wrong: `parse-transcript.sh :: format_turn()`
keeps `[User]:` text and assistant `text` blocks and skips `tool_use`, `tool_result` and
`thinking`. Measured on the session named in the build plan's Verification section, that is
33,995 of 523,546 characters — **6.5% of the session reaches the summarizer**, which is then
instructed to "mention file names, function names, tool names, and concrete outcomes". The
fix is not to stop skipping tool blocks: their raw bodies are ~$26/mo against a $30 allowance
and they are the surface behind vikunja#638/#700/#842. It is to compress them here, into
names, bounded digests, exit status and touched paths.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .models import (
    KIND_AGENT,
    KIND_BASH,
    KIND_FILE_READ,
    KIND_FILE_WRITE,
    KIND_MCP,
    KIND_OTHER,
    KIND_SEARCH,
    KIND_WEB,
    EventLog,
    ToolEvent,
    Turn,
)
from .redact import Redactor

# Ported verbatim from parse-transcript.sh, not reinvented. These are a forge divergence
# from upstream memsearch and they are why template placeholders stopped leaking into
# memory files: Claude Code writes a skill load as a "user" entry whose content is the
# entire skill markdown, and the two known shapes that arrive WITHOUT isMeta set are the
# slash-command wrapper and the skill body. Dropping the guard would reintroduce a fixed
# bug, so it travels with the isMeta check rather than being treated as redundant.
_INJECTED_PREFIXES = (
    "Base directory for this skill:",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)

# Ticket and PR references are separated on purpose. Both are written with a `#NNN`, and
# the corpus is full of both: `vikunja#843` is a Vikunja ticket, while `TadMSTR/dockhand-mcp#6`,
# `component-registry#1` and `PR #3` are pull requests. Folding them into one list looked
# tidy until it was measured — the top of the combined frequency table was #1 through #12,
# every one of them a PR number, drowning the Vikunja refs the summary actually cares about.
#
# A repo-qualified `<name>#N` or an explicit `PR #N` is a pull request. Everything else,
# including a bare `#843`, is a Vikunja ticket, because that is the fleet default recorded
# in CLAUDE.md and the form build plans use in prose.
_VIKUNJA_URL_RE = re.compile(r"vikunja\.helmforge\.me/tasks/(\d+)")
_PR_QUALIFIED_RE = re.compile(r"\b((?:[\w.-]+/)?[a-z][\w.-]*[a-z0-9])#(\d{1,6})\b", re.I)
_PR_WORD_RE = re.compile(r"\b(?:PRs?|pull(?:\s+request)?s?|issues?)\s+\[?#(\d{1,6})\b", re.I)
# `(#7)` trailing a commit subject is GitHub's squash-merge convention, and `[#7](url)`
# is the markdown link form. Both were showing up as Vikunja tickets.
_PR_PAREN_RE = re.compile(r"\(#(\d{1,6})\)")
_PR_LINK_RE = re.compile(r"\[#(\d{1,6})\]\(")
_TICKET_RE = re.compile(r"(?<![/\w\d])#([1-9]\d{0,5})\b")

_URL_RE = re.compile(r"https?://[^\s'\"<>)\]}]+")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_GIT_CMD_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?([a-z][a-z-]*)")

#: Tools whose call reads a file, mapped to the input key naming it. Anything not listed
#: falls through to the generic MCP/other handling rather than being guessed at — a wrong
#: target is worse than an absent one, because the Phase 5 groundedness gate treats the
#: rollup as ground truth.
_READ_TOOLS = {"Read": "file_path", "NotebookRead": "notebook_path"}
_WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_SEARCH_TOOLS = {"Glob": "pattern", "Grep": "pattern", "ToolSearch": "query"}
_WEB_TOOLS = {"WebFetch": "url", "WebSearch": "query"}
_AGENT_TOOLS = {"Task": "description", "Agent": "description", "Skill": "skill"}

#: Keys an MCP tool's arguments commonly use for the thing being acted on, in preference
#: order. MCP tool schemas are not uniform, so this is a best-effort projection; when none
#: match, the target is left empty and the tool name still carries the signal.
_MCP_TARGET_KEYS = (
    "command",
    "repo",
    "repo_path",
    "path",
    "file",
    "file_path",
    "task_id",
    "room_name",
    "query",
    "url",
    "branch",
    "pr_number",
    "project_id",
    "name",
)


def _is_injected(text: str) -> bool:
    s = (text or "").lstrip()
    return any(s.startswith(p) for p in _INJECTED_PREFIXES)


def _agent_from_project_dir(project_dir: str) -> str:
    """Recover the agent name from Claude Code's flattened project directory name.

    Claude Code encodes the project path by replacing separators, so
    `-home-ted--claude-projects-research` is `~/.claude/projects/research`. Returns ""
    rather than a guess when the shape is unfamiliar.

    **This encoding is lossy and cannot be inverted from the string alone.** A separator and
    a hyphen inside a name both become `-`, so `-...-projects-doc-health` is equally
    `projects/doc-health` and `projects/doc/health`. The tail is resolved against the
    directory those names live in: the longest candidate that exists wins, and the first
    segment is the fallback when nothing does. Splitting unconditionally at the first hyphen
    -- which is what this did -- silently renamed three of forge's ten agents (`doc-health`,
    `helm-build`, `memory-sync`), and the rename landed on the *write* side, so their digests
    went to a directory no reader would look in.

    Prefer `agent_for_path` over calling this directly; it consults the session's real `cwd`
    first, which is not ambiguous at all.
    """
    if not project_dir:
        return ""
    marker = "-claude-projects-"
    idx = project_dir.find(marker)
    if idx == -1:
        return ""
    tail = project_dir[idx + len(marker) :]
    if not tail:
        return ""
    segments = tail.split("-")
    projects = Path("~/.claude/projects").expanduser()
    for stop in range(len(segments), 1, -1):
        candidate = "-".join(segments[:stop])
        if (projects / candidate).is_dir():
            return candidate
    return segments[0]


def _agent_from_cwd(cwd: str) -> str:
    """Recover the agent name from a session's working directory. Exact, not a heuristic.

    A project directory is `~/.claude/projects/<agent>`, and a path keeps its separators, so
    there is nothing here to disambiguate. Returns "" for anything else -- outside that tree
    there is no agent, and guessing one would attribute a session to it.
    """
    if not cwd:
        return ""
    p = Path(cwd).expanduser()
    if p.parent.name == "projects" and p.parent.parent.name == ".claude":
        return p.name
    return ""


def agent_for_path(path: str) -> str:
    """The agent owning `path`, whichever of the two forms it arrives in.

    **Both forms are real and they are read by different halves of this system.** A hook sees
    `$CLAUDE_PROJECT_DIR`, the session's working directory. The extractor sees Claude Code's
    flattened *transcript* directory, and a transcript's own records carry the working
    directory too. Resolution has to be identical on both sides or the writer and the reader
    disagree about where an agent's digests live -- which does not fail loudly, it just
    injects nothing, for exactly the agents whose names contain a hyphen.

    **The flattened form is checked first, and that ordering is a form discriminator rather
    than a ranking.** A flattened transcript directory really does live at
    `~/.claude/projects/-home-ted--claude-projects-sysadmin`, so it satisfies the working-
    directory shape exactly -- and answering `-home-ted--claude-projects-sysadmin` would be
    both wrong and well-formed. The `-claude-projects-` marker is the only thing that says
    which form a string is in, so it decides, and `_agent_from_cwd` handles what is left.
    """
    if not path:
        return ""
    flattened = _agent_from_project_dir(Path(path).name)
    return flattened or _agent_from_cwd(path)


def _classify(tool: str, args: dict) -> tuple[str, str]:
    """Return `(kind, target)` for a tool call: what it did and what it did it to."""
    if tool == "Bash":
        return KIND_BASH, str(args.get("command", ""))
    for table, kind in (
        (_READ_TOOLS, KIND_FILE_READ),
        (_WRITE_TOOLS, KIND_FILE_WRITE),
        (_SEARCH_TOOLS, KIND_SEARCH),
        (_WEB_TOOLS, KIND_WEB),
        (_AGENT_TOOLS, KIND_AGENT),
    ):
        if tool in table:
            return kind, str(args.get(table[tool], "") or "")
    if tool.startswith("mcp__"):
        for key in _MCP_TARGET_KEYS:
            if key in args and args[key] not in (None, "", [], {}):
                return KIND_MCP, str(args[key])
        return KIND_MCP, ""
    return KIND_OTHER, ""


def _args_summary(args: dict, skip_key: str | None) -> str:
    """A compact `k=v` rendering of the arguments not already shown as the target.

    Values are stringified and left to `Redactor.digest` to bound, because a per-value cap
    applied here would truncate before scrubbing — see the ordering rule in redact.py.
    """
    parts = []
    for k, v in args.items():
        if k == skip_key or v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{k}={v}")
    return " ".join(parts)


def _result_text(block: dict, tool_use_result) -> str:
    """Flatten a tool_result body to text.

    `content` is a string 167 times out of 176 in the reference session, but also appears as
    a list of `text` / `tool_reference` blocks. `toolUseResult` carries the richer form for
    Bash (stdout/stderr), which is what makes a non-zero exit visible at all.
    """
    content = block.get("content")
    chunks: list[str] = []
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(str(part.get("text", "")))
    if isinstance(tool_use_result, dict):
        for key in ("stdout", "stderr"):
            val = tool_use_result.get(key)
            if val:
                chunks.append(f"[{key}] {val}")
    return "\n".join(c for c in chunks if c)


def _collect_refs(text: str) -> tuple[list[str], list[str]]:
    """Return `(vikunja_tickets, pull_requests)` found in `text`.

    Order matters: the repo-qualified PR form is consumed first and its span blanked, so a
    trailing bare-`#NNN` sweep cannot re-harvest `dockhand-mcp#6` as ticket `#6`.
    """
    tickets: list[str] = []
    prs: list[str] = []
    blob = text or ""

    for m in _VIKUNJA_URL_RE.finditer(blob):
        ref = f"#{m.group(1)}"
        if ref not in tickets:
            tickets.append(ref)

    spans: list[tuple[int, int]] = []
    for m in _PR_QUALIFIED_RE.finditer(blob):
        name, num = m.group(1), m.group(2)
        if name.lower().endswith("vikunja"):
            ref = f"#{num}"
            if ref not in tickets:
                tickets.append(ref)
        else:
            ref = f"{name}#{num}"
            if ref not in prs:
                prs.append(ref)
        spans.append(m.span())
    for pat in (_PR_WORD_RE, _PR_PAREN_RE, _PR_LINK_RE):
        for m in pat.finditer(blob):
            ref = f"#{m.group(1)}"
            if ref not in prs:
                prs.append(ref)
            spans.append(m.span())

    masked = list(blob)
    for a, b in spans:
        for i in range(a, b):
            masked[i] = " "
    for m in _TICKET_RE.finditer("".join(masked)):
        ref = f"#{m.group(1)}"
        if ref not in tickets:
            tickets.append(ref)
    return tickets, prs


def _collect_git_refs(command: str) -> list[str]:
    """Pull git refs out of a git command line.

    Scoped to git commands on purpose. A bare 7-40 hex scan over all text also matches
    image digests and content hashes, and a rollup that claims `18cfe3ef` is a git ref
    would hand the Phase 5 groundedness gate a fact that is true of the transcript but
    false about the world.
    """
    out: list[str] = []
    if not command or "git" not in command:
        return out
    for m in _GIT_CMD_RE.finditer(command):
        sub = m.group(1)
        if sub not in out:
            out.append(sub)
        tail = command[m.end() : m.end() + 200]
        for sha in _SHA_RE.findall(tail):
            if sha not in out:
                out.append(sha)
    return out


def _push(seq: list[str], value: str, limit: int = 200) -> None:
    if value and value not in seq and len(seq) < limit:
        seq.append(value)


def extract(
    path: str | os.PathLike[str],
    *,
    max_session_chars: int = 200_000,
    target_chars: int = 1200,
    result_chars: int = 600,
    args_chars: int = 400,
    text_chars: int = 8000,
) -> EventLog:
    """Extract one transcript into an `EventLog`.

    Opens `path` read-only and never writes to it.
    """
    p = Path(path).expanduser()
    red = Redactor()
    log = EventLog(session_id="", transcript_path=str(p), project_dir=p.parent.name)
    log.agent = agent_for_path(log.project_dir)
    st = log.stats
    try:
        st.raw_file_bytes = p.stat().st_size
    except OSError:
        st.raw_file_bytes = 0

    turns: list[Turn] = []
    current: Turn | None = None
    pending: dict[str, ToolEvent] = {}
    seq = 0

    def _ensure_turn(uuid: str, ts: str) -> Turn:
        """Open a turn for assistant activity that precedes any real user message.

        A resumed session, or one whose opening user record was meta, can begin with
        assistant output. Attaching it to a synthetic turn keeps the event count honest
        instead of silently discarding the work.
        """
        nonlocal current
        if current is None:
            current = Turn(turn_uuid=uuid or "orphan", index=len(turns), started_at=ts)
            turns.append(current)
        return current

    with p.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                st.records_unparsable += 1
                continue
            if not isinstance(obj, dict):
                st.records_unparsable += 1
                continue
            st.records += 1

            if not log.session_id and obj.get("sessionId"):
                log.session_id = str(obj["sessionId"])
            for attr, key in (("cwd", "cwd"), ("git_branch", "gitBranch"), ("version", "version")):
                if not getattr(log, attr) and obj.get(key):
                    setattr(log, attr, str(obj[key]))

            ts = str(obj.get("timestamp", "") or "")
            if ts:
                if not log.started_at:
                    log.started_at = ts
                log.ended_at = ts

            rtype = obj.get("type")
            if rtype not in ("user", "assistant"):
                # Claude Code writes TWO records at a compact boundary, and this filter is
                # where the first one goes: `type: system, subtype: compact_boundary`. Its
                # payload is a fixed string ("Conversation compacted") plus a
                # `compactMetadata` dict — a marker, not conversation — so dropping it loses
                # no session content and the drop is deliberate, not incidental.
                #
                # It is the only record that marks WHERE the boundary fell. If the compact
                # summary is ever captured as a first-class event-log field (deferred in
                # vikunja#893), this is the record that supplies the position, and this
                # branch is what has to change. The second record is handled below.
                #
                # Deliberately NOT covered by a test. One was written and removed 2026-09-18:
                # it could not be made to fail. Widening this filter to admit `type: system`
                # still leaks nothing, because the marker carries its payload at top-level
                # `content` while the reader below takes `message.content`, so it falls out
                # at the empty-text check either way. A test that cannot go red is not a
                # guard, and keeping one here would have implied cover that does not exist.
                continue

            if obj.get("isMeta"):
                st.skipped_meta += 1
                continue

            # Third member of the _INJECTED_PREFIXES / isMeta family, and here for the same
            # reason: Claude Code writes machine-generated text as a record that is
            # structurally indistinguishable from a person speaking. At a compact boundary it
            # emits `type: user, isCompactSummary: true` whose content is the model's own
            # recap of the conversation so far.
            #
            # Measured 2026-09-18 (vikunja#893) on session 6017c8ce: 18,859 characters of
            # recap entering the event log as user text, in the worst-degraded session in the
            # corpus (degradation_level 4, budget_exceeded). It competed for the byte budget
            # against 672 real tool events, and invariant 6 forbids the ladder from dropping
            # events — so the recap won and real evidence was thinned to make room for it.
            #
            # This opens its own Turn if left alone; excluding it lowers `turns` by one on
            # any transcript carrying a boundary. That is the guard working, not a regression.
            if obj.get("isCompactSummary"):
                st.skipped_compact += 1
                continue

            content = (obj.get("message") or {}).get("content")

            if rtype == "user":
                # A user record carrying tool_result blocks is the harness returning output,
                # not a person speaking. It continues the current turn rather than opening
                # one — treating it as a turn boundary is what would fragment a session into
                # one "turn" per tool call.
                blocks = content if isinstance(content, list) else []
                results = [
                    b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if results:
                    for block in results:
                        st.tool_results += 1
                        ev = pending.pop(str(block.get("tool_use_id", "")), None)
                        if ev is None:
                            st.orphan_results += 1
                            continue
                        is_err = block.get("is_error")
                        body = _result_text(block, obj.get("toolUseResult"))
                        ev.result_chars = len(body)
                        st.raw_content_chars += len(body)
                        ev.result_digest = red.digest(body, result_chars)
                        ev.ok = not is_err if is_err is not None else True
                        if is_err:
                            ev.error = red.digest(body, 300)
                            st.failures += 1
                    continue

                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(
                        str(b.get("text", ""))
                        for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if not text.strip():
                    continue
                if _is_injected(text):
                    st.skipped_injected += 1
                    continue

                st.raw_content_chars += len(text)
                current = Turn(
                    turn_uuid=str(obj.get("uuid", "") or f"turn-{len(turns)}"),
                    index=len(turns),
                    started_at=ts,
                    ended_at=ts,
                    user_text=red.digest(text, text_chars),
                )
                turns.append(current)
                continue

            # assistant
            if not isinstance(content, list):
                continue
            turn = _ensure_turn(str(obj.get("uuid", "")), ts)
            if ts:
                turn.ended_at = ts
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking":
                    st.thinking_blocks += 1
                    turn.thinking_blocks += 1
                elif btype == "text":
                    text = str(block.get("text", "")).strip()
                    if text:
                        st.raw_content_chars += len(text)
                        turn.assistant_text.append(red.digest(text, text_chars))
                elif btype == "tool_use":
                    st.tool_events += 1
                    tool = str(block.get("name", "") or "unknown")
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    st.raw_content_chars += len(
                        json.dumps(block, ensure_ascii=False, separators=(",", ":"))
                    )
                    kind, target = _classify(tool, args)
                    skip = None
                    if target:
                        for table in (
                            _READ_TOOLS,
                            _WRITE_TOOLS,
                            _SEARCH_TOOLS,
                            _WEB_TOOLS,
                            _AGENT_TOOLS,
                        ):
                            if tool in table:
                                skip = table[tool]
                                break
                        if tool == "Bash":
                            skip = "command"
                    ev = ToolEvent(
                        seq=seq,
                        tool=tool,
                        kind=kind,
                        target=red.digest(target, target_chars),
                        args_digest=red.digest(_args_summary(args, skip), args_chars),
                    )
                    seq += 1
                    turn.events.append(ev)
                    pending[str(block.get("id", ""))] = ev

    log.turns = turns
    # Re-resolved now that the records have been read. `cwd` is exact where the flattened
    # directory name is ambiguous, and it is only available after parsing -- so the
    # assignment above is a provisional one that this supersedes whenever it can.
    log.agent = _agent_from_cwd(log.cwd) or log.agent
    _build_rollups(log)
    _finalise(log, red, max_session_chars)
    return log


def _build_rollups(log: EventLog) -> None:
    """Derive per-turn rollups, then union them into the session rollup."""
    for turn in log.turns:
        r = turn.rollup
        text_pool = [turn.user_text, *turn.assistant_text]
        for ev in turn.events:
            if ev.kind == KIND_BASH:
                _push(r.commands, ev.target.split("\n")[0][:300])
                for ref in _collect_git_refs(ev.target):
                    _push(r.git_refs, ref)
            elif ev.kind == KIND_FILE_READ:
                _push(r.files_read, ev.target)
            elif ev.kind == KIND_FILE_WRITE:
                _push(r.files_written, ev.target)
            elif ev.kind == KIND_AGENT:
                _push(r.agents, ev.target[:120])
            elif ev.kind == KIND_WEB:
                _push(r.urls, ev.target)
            if ev.tool.startswith("mcp__"):
                _push(r.mcp_tools, ev.tool)
            if ev.ok is False:
                _push(r.failures, f"{ev.tool}: {ev.error[:200] or 'failed'}")
            text_pool.append(ev.target)
            text_pool.append(ev.result_digest)
        blob = "\n".join(t for t in text_pool if t)
        tickets, prs = _collect_refs(blob)
        for ticket in tickets:
            _push(r.tickets, ticket)
        for pr in prs:
            _push(r.prs, pr)
        for url in _URL_RE.findall(blob):
            _push(r.urls, url.rstrip(".,);"))
        log.rollup.merge(r)


def _finalise(log: EventLog, red: Redactor, max_session_chars: int) -> None:
    """Fill in stats, then degrade the document if it exceeds the byte budget.

    The ladder below is the build plan's priority order — results before arguments, and
    never user or assistant text — with one correction found by measuring it. The plan's
    order alone cannot reach a 120,000-char budget on a busy session: after dropping every
    result and argument digest, the reference session is still ~181,000 chars, so a literal
    reading kept degrading into the tool targets. Discarding the target is discarding the
    file path and the command line, which is the signal this whole component exists to
    deliver. So targets are *tightened*, never dropped, and the floor of the ladder is a
    still-useful event: tool name, kind, exit status and a short target.

    When even that floor exceeds the budget, `budget_exceeded` is set and the document is
    returned oversized rather than mutilated further. One session in the 429-file corpus has
    a text-only floor of 143,410 chars, and since text is never droppable, no ladder can
    bring it under 120,000. Silently shipping a stripped event log for that session would
    be indistinguishable from a session that genuinely did nothing.
    """
    st = log.stats
    st.turns = len(log.turns)
    st.secrets_redacted = red.count
    st.redaction_fires = dict(red.by_kind)

    def size() -> int:
        # Content only. See EventLog.content_dict: including `stats` would make the size
        # measurement depend on the size measurement.
        return len(json.dumps(log.content_dict(), ensure_ascii=False))

    st.extracted_chars = size()
    if max_session_chars <= 0:
        st.token_estimate = (st.extracted_chars + 3) // 4
        if st.raw_content_chars:
            st.compression_ratio = round(st.extracted_chars / st.raw_content_chars, 4)
        return

    def _clear(attr: str) -> None:
        for turn in log.turns:
            for ev in turn.events:
                if getattr(ev, attr):
                    setattr(ev, attr, "")
                    st.fields_cleared += 1

    def _tighten(limit: int) -> None:
        for turn in log.turns:
            for ev in turn.events:
                if len(ev.target) > limit:
                    ev.target = ev.target[:limit] + "…"
                    st.fields_cleared += 1

    ladder = (
        (1, lambda: _clear("result_digest")),
        (2, lambda: _clear("args_digest")),
        (3, lambda: _tighten(200)),
        (4, lambda: _tighten(80)),
    )
    for level, step in ladder:
        if st.extracted_chars <= max_session_chars:
            break
        step()
        st.degradation_level = level
        st.extracted_chars = size()

    st.budget_exceeded = st.extracted_chars > max_session_chars
    st.token_estimate = (st.extracted_chars + 3) // 4
    if st.raw_content_chars:
        st.compression_ratio = round(st.extracted_chars / st.raw_content_chars, 4)
