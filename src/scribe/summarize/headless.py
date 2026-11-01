"""Headless sessions: the agent's own closing statement, plus a rollup, and no model call.

For a session nobody was watching -- a dispatcher's `claude -p` run -- the agent ends by
writing a report for whoever reads it later, and that report is better evidence than a
summary of it. Measured on writer session 61ec60fe (vikunja#1347): the LLM digest had six
errors the closing statement did not, including a drafter's interim state shown as final, a
drafter's caveat recorded as a decision, and PR numbers listed as tickets. Across ~110 writer
and security sessions, what the digest held that the closing statement did not was mostly
paths and tickets -- which the event log's rollup already has, with no model involved.

So the block is two parts:

  * **The closing statement**, quoted verbatim between `VERBATIM_START` and `VERBATIM_END`,
    under a line saying it is the agent's statement and not scribe's summary. Taken from the
    event log, not the transcript, so `scribe recover` can rebuild the block after the
    transcript has aged out.
  * **A rollup** generated from the event log: the same `Artifacts` and `Tickets` sections and
    caps as an LLM digest, so qmd and the journal parser see the same structure, plus PRs
    (kept apart from tickets, which the LLM digest mixed), commits, queue tasks and subagents.

Three ways out of the verbatim path, each recorded in the run record as `render_mode`:

  * the final turn's text is under `min_closing_chars`: try the previous turn (a session that
    ends on a background task's notification writes its real summary one turn earlier);
  * that is short too, or the contamination guard fires on it: the normal LLM digest
    (`verbatim-fallback-llm`, with `closing_reason` saying why);
  * the session is still mid-flight: the final turn launched background work that has not
    reported back, or holds a tool call with no result (a foreground subagent running past the
    quiet period). Its last message is not a closing statement, so a rollup-only block is
    written, anchored `provisional:pending`, and the block the session gets once it finishes
    replaces it (vikunja#1347 handoff question 1). See `pipeline` for why the final turn is
    then left unprocessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..extract.models import EventLog, Turn
from .contamination import explain_contamination
from .render import VERBATIM_END, VERBATIM_RE, VERBATIM_START, _bullet, _truncation_note
from .schema import MAX_ROLLUP_ITEMS, caps_for

#: `render_mode` values. `llm` is every session the feature does not apply to.
MODE_LLM = "llm"
MODE_VERBATIM = "verbatim"
MODE_FALLBACK = "verbatim-fallback-llm"
MODE_PENDING = "pending"
#: The modes whose block a model wrote. The QC trend is judged on these only (`qc_report`),
#: and only these say anything about whether the provider is up (`pipeline`).
LLM_MODES = frozenset({MODE_LLM, MODE_FALLBACK})

#: Where the closing statement came from.
SOURCE_FINAL = "final-turn"
SOURCE_PREVIOUS = "previous-turn"

#: The line above the quotation. Scribe's prose, so it says plainly whose words follow.
CLOSING_LABEL = (
    "_Closing statement: the agent's own final message, quoted verbatim. It is not a scribe "
    "summary._"
)
PENDING_LABEL = (
    "_Session still running: work it started has not reported back, so there is no closing "
    "statement yet. This block is replaced when the session finishes._"
)

#: A line that would be structure in the daily file rather than text: a markdown heading,
#: which would open a section for the journal preview and for qmd, or an HTML comment, which
#: could be read as a block anchor or terminator, or a setext underline (`===` / `---` alone on a
#: line, a single `-` included), which turns the line above it into a heading. Escaped with a backslash, which markdown
#: renders as the literal character, so the quotation still reads as written.
_STRUCTURAL_RE = re.compile(r"(?m)^([ \t]{0,3})(#|<!--|=+[ \t]*$|-+[ \t]*$)")

_GIT_COMMIT_MCP_SHA_RE = re.compile(r'"sha"\s*:\s*"([0-9a-f]{7,40})"')
_GIT_COMMIT_BASH_SHA_RE = re.compile(r"(?m)^\[[^\]\s]+(?: \([^)]*\))? ([0-9a-f]{7,40})\]")
_GIT_COMMIT_CMD_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_STATUS_ARG_RE = re.compile(r"(?:^|\s)status=([a-z_-]{1,32})\b")
_TASK_TOOLS = ("update_task", "submit_task")


@dataclass
class Closing:
    text: str
    turn_index: int
    source: str


@dataclass
class Choice:
    """What a headless session gets. `markdown` is empty for a fallback to the LLM digest."""

    mode: str
    markdown: str = ""
    closing: Closing | None = None
    #: Why the verbatim path was not taken, "" when it was.
    reason: str = ""
    #: Items dropped from rollup sections at their caps, per section.
    truncated: dict[str, int] = field(default_factory=dict)

    @property
    def closing_chars(self) -> int:
        return len(self.closing.text) if self.closing else 0


def _last_text(turn: Turn) -> str:
    for text in reversed(turn.assistant_text):
        if text.strip():
            return text.strip()
    return ""


def closing_statement(log: EventLog, min_chars: int) -> tuple[Closing | None, str]:
    """The closing statement, or `(None, why not)`.

    The final turn's last text first. Below `min_chars`, the previous turn's: in 1 of 12
    headless sessions that ended on a background task's notification, the final text was a
    note about the task and the real summary was one turn earlier (vikunja#1347 finding 3).
    """
    if not log.turns:
        return None, "no turns"
    final = log.turns[-1]
    text = _last_text(final)
    if text and len(text) >= min_chars:
        return Closing(text, final.index, SOURCE_FINAL), ""
    if len(log.turns) >= 2:
        prev = log.turns[-2]
        earlier = _last_text(prev)
        if earlier and len(earlier) >= min_chars:
            return Closing(earlier, prev.index, SOURCE_PREVIOUS), ""
    return None, f"closing statement under {min_chars} chars ({len(text)})"


def escape_structure(text: str) -> str:
    """`text` with every heading and HTML-comment line escaped. See `_STRUCTURAL_RE`."""
    return _STRUCTURAL_RE.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", text)


def _commits(log: EventLog) -> list[str]:
    """Commit shas the session made, with the repo where the call names one.

    From a `git_commit` MCP call's result (`"sha": ...`) or a `git commit` Bash command's
    `[branch sha]` line. Read from results, never guessed from a hex string in prose.
    """
    out: list[str] = []
    for turn in log.turns:
        for ev in turn.events:
            if ev.tool.endswith("git_commit"):
                m = _GIT_COMMIT_MCP_SHA_RE.search(ev.result_digest)
                if m:
                    out.append(f"`{m.group(1)}` {ev.target}".rstrip())
            elif ev.tool == "Bash" and _GIT_COMMIT_CMD_RE.search(ev.target):
                m = _GIT_COMMIT_BASH_SHA_RE.search(ev.result_digest)
                if m:
                    out.append(f"`{m.group(1)}`")
    return list(dict.fromkeys(out))


def _tasks(log: EventLog) -> list[str]:
    """Queue tasks the session moved, each with the last status it set, in first-seen order."""
    last: dict[str, str] = {}
    for turn in log.turns:
        for ev in turn.events:
            if not ev.tool.endswith(_TASK_TOOLS):
                continue
            ids = _UUID_RE.findall(f"{ev.target} {ev.args_digest}")
            if ev.tool.endswith("submit_task"):
                ids += _UUID_RE.findall(ev.result_digest)
            status = _STATUS_ARG_RE.search(ev.args_digest)
            for task in ids:
                if status:
                    last[task] = status.group(1)
                else:
                    last.setdefault(task, "")
    return [f"`{t}` {s}".rstrip() for t, s in last.items()]


def _agents(log: EventLog) -> list[str]:
    if log.subagents is None:
        # A version-1 log: no structured record, so the Agent call descriptions it does have.
        return list(log.rollup.agents)
    out = []
    for sub in log.subagents:
        detail = ", ".join(x for x in ("background" if sub.background else "", sub.status) if x)
        name = sub.subagent_type or "agent"
        out.append(f"{name}: {sub.description}" + (f" ({detail})" if detail else ""))
    return out


def rollup_sections(log: EventLog) -> list[tuple[str, list[str], int]]:
    """`(label, items, cap)` per section, in render order. Built from the event log alone."""
    caps = caps_for(log)
    return [
        ("Artifacts", list(log.rollup.files_written), caps["artifacts"]),
        ("Tickets", list(log.rollup.tickets), caps["tickets"]),
        ("PRs", list(log.rollup.prs), caps["tickets"]),
        ("Commits", _commits(log), MAX_ROLLUP_ITEMS),
        ("Tasks", _tasks(log), MAX_ROLLUP_ITEMS),
        ("Agents", _agents(log), MAX_ROLLUP_ITEMS),
    ]


def _render_rollup(log: EventLog) -> tuple[list[str], dict[str, int]]:
    lines: list[str] = []
    truncated: dict[str, int] = {}
    for label, items, cap in rollup_sections(log):
        if not items:
            continue
        lines.append("")
        lines.append(f"**{label}:**")
        lines.extend(_bullet(item) for item in items[:cap])
        if len(items) > cap:
            truncated[label.lower()] = len(items) - cap
            lines.append(_truncation_note(len(items) - cap, cap))
    return lines, truncated


def render_verbatim(log: EventLog, closing: Closing, *, heading: str = "") -> Choice:
    lines: list[str] = []
    if heading:
        lines += [f"### {heading}", ""]
    lines += [CLOSING_LABEL, "", VERBATIM_START, escape_structure(closing.text), VERBATIM_END]
    rollup, truncated = _render_rollup(log)
    lines += rollup
    return Choice(
        mode=MODE_VERBATIM,
        markdown="\n".join(lines) + "\n",
        closing=closing,
        truncated=truncated,
    )


def outstanding(log: EventLog) -> list[str]:
    """What the session is still waiting on: background task ids, then unanswered tool calls.

    Read from the event log, so a replay sees what the extract saw. A version-1 log has no
    `background_pending` and contributes only its unanswered calls.
    """
    waiting = list(log.background_pending or [])
    if log.turns:
        waiting += [f"{ev.tool} (no result)" for ev in log.turns[-1].events if ev.ok is None]
    return waiting


def render_pending(log: EventLog, *, heading: str = "") -> Choice:
    lines: list[str] = []
    if heading:
        lines += [f"### {heading}", ""]
    lines.append(PENDING_LABEL)
    rollup, truncated = _render_rollup(log)
    lines += rollup
    waiting = ", ".join(f"`{t}`" for t in outstanding(log))
    return Choice(
        mode=MODE_PENDING,
        markdown="\n".join(lines) + "\n",
        reason=f"still waiting on: {waiting}",
        truncated=truncated,
    )


def choose(log: EventLog, min_chars: int, *, heading: str = "") -> Choice:
    """The headless rendering for `log`, or a `MODE_FALLBACK` choice with no markdown.

    The contamination guard runs over the closing statement before it is accepted, exactly
    as it runs over a model's digest: the quotation reaches the same SessionStart injection
    and the same index. On a fire the session falls back to the LLM digest, whose own guard
    and retry handle it. The post-render redaction in `pipeline` runs over either.
    """
    if outstanding(log):
        pending = render_pending(log, heading=heading)
        rest = pending.markdown.replace(PENDING_LABEL, "")
        if explain_contamination(rest, log.grounding_text()) is not None:
            return Choice(mode=MODE_FALLBACK, reason="contamination (pending rollup)")
        return pending
    closing, why = closing_statement(log, min_chars)
    if closing is None:
        return Choice(mode=MODE_FALLBACK, reason=why)
    corpus = log.grounding_text()
    detection = explain_contamination(closing.text, corpus)
    if detection is not None:
        return Choice(
            mode=MODE_FALLBACK, closing=closing, reason=f"contamination: {detection.signal}"
        )
    choice = render_verbatim(log, closing, heading=heading)
    # The rollup too: subagent descriptions and commit targets are agent-written and reach the
    # same injection (security baseline IV-24). Run over the block minus the quotation, which
    # was checked above. 0 of 220 measured rollups fired.
    rest = VERBATIM_RE.sub("", choice.markdown).replace(CLOSING_LABEL, "")
    detection = explain_contamination(rest, corpus)
    if detection is not None:
        return Choice(
            mode=MODE_FALLBACK,
            closing=closing,
            reason=f"contamination (rollup): {detection.signal}",
        )
    return choice
