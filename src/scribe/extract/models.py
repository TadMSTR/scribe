"""The event-log schema — the contract every downstream stage consumes.

Phase 1 deliberately has no third-party dependency and no network access, so these are
stdlib dataclasses rather than pydantic models. The summarizer (Phase 3) validates the
*model's* output against a pydantic schema; that is a different contract with a different
threat model, and conflating the two would drag an HTTP-capable dependency into the one
module that is supposed to be provably offline.

`SCHEMA_VERSION` is written into every document. Downstream code should refuse a version it
does not recognise rather than guessing — a silently-tolerated schema change is how a field
rename becomes a summary that stops mentioning files.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

#: 2 (scribe 0.18.0) added `entrypoint`, `subagents`, `compactions`, `background_pending`,
#: `Turn.queued_input` and `Stats.queued_commands`. A version-1 log has none of them, and
#: `eventlog.log_from_dict` reads the three list fields back as None -- "not recorded" --
#: never as an empty list, which would claim the session had no subagents.
SCHEMA_VERSION = 2

#: Coarse buckets for what a tool call *did*, so a prompt can group by activity without
#: carrying a hardcoded list of tool names that goes stale every time one is added.
KIND_BASH = "bash"
KIND_FILE_READ = "file_read"
KIND_FILE_WRITE = "file_write"
KIND_SEARCH = "search"
KIND_MCP = "mcp"
KIND_AGENT = "agent"
KIND_WEB = "web"
KIND_OTHER = "other"


@dataclass
class ToolEvent:
    """One tool_use paired with its tool_result.

    A tool_use with no matching tool_result is still emitted, with `ok=None` — an
    interrupted or still-running call is a real thing that happened and dropping it would
    make the event count disagree with the transcript.
    """

    seq: int
    tool: str
    kind: str
    target: str = ""
    args_digest: str = ""
    ok: bool | None = None
    error: str = ""
    result_digest: str = ""
    result_chars: int = 0
    #: A task-queue call's own `status` argument, read from the structured arguments. The
    #: headless rollup used to search `args_digest` for `status=`, which a value earlier in the
    #: same call could fake (audit F-06). Empty for every other tool.
    task_status: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", None) or k == "ok"}


@dataclass
class Rollup:
    """Facts derived deterministically so the summarizer never has to infer them.

    Every field here is something the old pipeline asked the model to produce from an input
    that did not contain it. Deriving them in code is the whole point: a file path in the
    digest can now be checked against this list, which is what makes the Phase 5
    groundedness gate possible at all.
    """

    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    #: Vikunja tickets. Kept separate from `prs` because a rollup that lists
    #: `dockhand-mcp#6` under "tickets" is telling the summarizer something false, and
    #: the Phase 5 groundedness gate treats this list as ground truth.
    tickets: list[str] = field(default_factory=list)
    prs: list[str] = field(default_factory=list)
    git_refs: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}

    def merge(self, other: Rollup) -> None:
        """Union `other` into self, preserving first-seen order and dropping duplicates."""
        for name in self.__dataclass_fields__:
            mine: list[str] = getattr(self, name)
            seen = set(mine)
            for item in getattr(other, name):
                if item not in seen:
                    seen.add(item)
                    mine.append(item)


@dataclass
class Turn:
    """A user message and everything the assistant did in response to it.

    Keyed on `turn_uuid`, the user record's own uuid. This is the idempotency key for the
    whole pipeline. The pipeline it replaces keyed on `(session_id, HH:MM)`, which collides
    whenever two turns start in the same minute — 62 times across 3,539 blocks (vikunja#844).
    """

    turn_uuid: str
    index: int
    started_at: str = ""
    ended_at: str = ""
    user_text: str = ""
    assistant_text: list[str] = field(default_factory=list)
    events: list[ToolEvent] = field(default_factory=list)
    rollup: Rollup = field(default_factory=Rollup)
    thinking_blocks: int = 0
    #: Input the harness delivered while this turn was running: a `queued_command` attachment,
    #: which is how a background task's `<task-notification>` arrives when the session is
    #: mid-turn. Not `user_text`: nobody typed it, and it did not start this turn. Before 0.18.0
    #: the parser dropped these records, and 2 of 4 subagent results in one session never
    #: reached the event log.
    queued_input: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {
            "turn_uuid": self.turn_uuid,
            "index": self.index,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "user_text": self.user_text,
            "queued_input": self.queued_input,
            "assistant_text": self.assistant_text,
            "events": [e.to_dict() for e in self.events],
            "rollup": self.rollup.to_dict(),
        }
        if self.thinking_blocks:
            out["thinking_blocks"] = self.thinking_blocks
        return {k: v for k, v in out.items() if v not in ("", [], {})}


@dataclass
class Stats:
    """Measurements, including the ones the build plan's verification asserts against.

    `secrets_redacted` is reported as a count rather than applied silently, because the
    redaction control needs a *positive* signal. "No secret appears in the output" is also
    satisfied by an extractor that emitted nothing, so absence alone proves nothing about
    whether the filter ran.
    """

    records: int = 0
    records_unparsable: int = 0
    turns: int = 0
    tool_events: int = 0
    tool_results: int = 0
    thinking_blocks: int = 0
    skipped_meta: int = 0
    skipped_injected: int = 0
    #: Compact-boundary records excluded from user text (vikunja#893). Counted rather
    #: than dropped silently for the same reason as `secrets_redacted`: a zero here on
    #: a transcript that HAS a compact boundary means the guard did not fire, which is
    #: not distinguishable from "no boundary present" unless the count is reported.
    skipped_compact: int = 0
    #: `queued_command` attachments read into `Turn.queued_input`. Every other attachment type
    #: is still dropped; this count is what makes the one kept type measurable.
    queued_commands: int = 0
    orphan_results: int = 0
    failures: int = 0
    #: Sum of characters across the four content categories the old hook triaged, so the
    #: 6.5%-reaching-the-LLM measurement in vikunja#843 can be reproduced from the output
    #: rather than taken on faith.
    #:
    #: **NOT an input-only invariant, despite the name.** It counts content that reached
    #: this accumulator, so every guard that `continue`s before it — `isMeta`,
    #: `_INJECTED_PREFIXES`, `isCompactSummary` — lowers it. Excluding one compact recap
    #: moved it by 18,859 on session 6017c8ce. Flagged INFO by the 2026-09-18 audit of
    #: scribe-truth-pass-2026-09, because the build plan had asserted it "counts input,
    #: not output" and would therefore hold flat across the new guard.
    #:
    #: Do not use it to detect a filter dropping more than it should: it falls whenever a
    #: guard legitimately fires, so that test reads as a violation exactly when the code
    #: is working. Compare `tool_events` and `secrets_redacted` instead — both held at 672
    #: and 80 across that change, which is what showed the guard was correctly scoped.
    raw_content_chars: int = 0
    raw_file_bytes: int = 0
    extracted_chars: int = 0
    compression_ratio: float = 0.0
    token_estimate: int = 0
    secrets_redacted: int = 0
    redaction_fires: dict[str, int] = field(default_factory=dict)
    degradation_level: int = 0
    #: Count of digest fields cleared or tightened by the byte-budget ladder. Not a
    #: count of dropped events — the ladder never removes an event, because an event
    #: log that has forgotten a tool ran cannot ground a claim about it.
    fields_cleared: int = 0
    #: True when the ladder bottomed out and the document is STILL over budget. The
    #: caller must decide what to do; this module will not silently ship a stripped
    #: log, because that is indistinguishable from a session that did nothing.
    budget_exceeded: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Subagent:
    """One Agent (or legacy Task) tool call: which subagent ran, and where its transcript is.

    `agent_id` comes from the tool result's structured `toolUseResult.agentId`, falling back
    to the `agentId: ...` line in the result text. `status` is the tool result's own status for
    a foreground call (`completed`), and for a background one the `<status>` of its
    task-notification, or `running` when none arrived. The subagent's transcript is never read
    here: `log_path` is where Claude Code keeps it, and `log_exists` whether it was there when
    this log was extracted -- None when it could not be looked at (audit F-05), so "could not
    stat" is never recorded as "absent".
    """

    turn_index: int
    seq: int
    subagent_type: str = ""
    description: str = ""
    background: bool = False
    agent_id: str = ""
    status: str = ""
    log_path: str = ""
    log_exists: bool | None = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Compaction:
    """One `compact_boundary` record: when the session compacted, and from what to what."""

    ts: str = ""
    turn_index: int = 0
    trigger: str = ""
    pre_tokens: int = 0
    post_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EventLog:
    """The complete extraction of one transcript. This is the contract."""

    session_id: str
    transcript_path: str
    project_dir: str = ""
    agent: str = ""
    cwd: str = ""
    git_branch: str = ""
    version: str = ""
    #: `entrypoint` from the transcript's records: `sdk-cli` for `claude -p`, `sdk-ts` for an
    #: SDK host, `cli` for the terminal. What the headless rendering mode keys on.
    entrypoint: str = ""
    started_at: str = ""
    ended_at: str = ""
    turns: list[Turn] = field(default_factory=list)
    rollup: Rollup = field(default_factory=Rollup)
    stats: Stats = field(default_factory=Stats)
    #: None means NOT RECORDED (a version-1 log), and that is a different fact from `[]`.
    subagents: list[Subagent] | None = field(default_factory=list)
    compactions: list[Compaction] | None = field(default_factory=list)
    #: Background tasks (a background Agent call or a `run_in_background` Bash) launched in the
    #: final turn with no task-notification yet: the session is waiting on work it started, so
    #: its last message is not a closing statement. None means not recorded.
    background_pending: list[str] | None = field(default_factory=list)

    def grounding_text(self) -> str:
        """Every string a digest is allowed to draw a concrete fact from, as one blob.

        This is the *same document the model was shown*, serialized. That identity is the
        point: a corpus assembled independently of the prompt would eventually disagree with
        it and start failing true claims. `summarize.prompt.grounding_corpus` delegates here
        rather than building its own, and `qc` reads it directly so that checking a digest
        never requires importing the provider stack.
        """
        return json.dumps(self.content_dict(), ensure_ascii=False)

    def content_dict(self) -> dict:
        """The document minus `stats` — what the byte budget governs.

        `stats` reports the document's own size, so measuring a dict that contains it is
        self-referential: writing the number changes the number. It is also not part of what
        a summarizer is sent. Budgeting on content resolves both at once, and keeps
        `extracted_chars` a figure a caller can reproduce.
        """
        d = self.to_dict()
        d.pop("stats", None)
        return d

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "project_dir": self.project_dir,
            "agent": self.agent,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "version": self.version,
            "entrypoint": self.entrypoint,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": [t.to_dict() for t in self.turns],
            "rollup": self.rollup.to_dict(),
            "subagents": _maybe([s.to_dict() for s in self.subagents or []], self.subagents),
            "compactions": _maybe([c.to_dict() for c in self.compactions or []], self.compactions),
            "background_pending": self.background_pending,
            "stats": self.stats.to_dict(),
        }


def _maybe(rendered: list, source: list | None) -> list | None:
    """`rendered`, or None when the source field was not recorded at all."""
    return None if source is None else rendered
