"""Quality gates that can actually fail.

The gate this replaces, `memory-compact-qc.sh`, has run weekly for months and could never
have caught the defect that motivated this whole component. It grades `compact` output for
fidelity to *its source*, and its source is the already-starved memory file. A faithful
summary of an impoverished input scores PASS. The grader was working; it was pointed at the
wrong artefact.

So every check here asserts against the **event log**, which is derived from the transcript,
never against another summary.

Three deterministic checks, no LLM:

1. **Coverage** — a session with a transcript in the window has a digest, and that digest is
   newer than the transcript's last write.
2. **Groundedness** — every file path, command, ticket and tool name the digest names appears
   in the event log. This is what makes hallucination detectable rather than a matter of
   taste.
3. **Event-coverage floor** — a digest referencing fewer than N% of the session's non-trivial
   events FAILS. Without a floor, an extractor regression that silently drops events reads as
   a clean pass: the digest stays perfectly grounded in a log that has lost most of its
   content, and checks 1 and 2 both go green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .extract.models import EventLog

#: Below this share of the session's non-trivial events, a digest is a FAIL rather than a
#: terse success. Deliberately low: the floor exists to catch an extractor that has stopped
#: extracting, not to mandate a verbose summary.
DEFAULT_COVERAGE_FLOOR = 0.10

_PATH_RE = re.compile(r"(?:(?<=\s)|^)(?:~|\.{0,2})/[\w./~-]{3,}")
_TICKET_RE = re.compile(r"(?<![/\w\d])#([1-9]\d{0,5})\b")
# Tool claims are split by ambiguity, because several tool names are also ordinary English.
# A digest saying "Read the logs" or "Task complete" is not naming a tool, and flagging it
# would fail a true claim -- a gate that cries wolf is one that gets switched off. So the
# ambiguous names count only when marked as code or prefixed, while the distinctive ones
# count wherever they appear.
_TOOL_AMBIGUOUS = ("Read", "Write", "Edit", "Task", "Skill", "Agent")
_TOOL_DISTINCT = (
    "WebFetch",
    "WebSearch",
    "ToolSearch",
    "NotebookEdit",
    "NotebookRead",
    "AskUserQuestion",
    "MultiEdit",
    "Bash",
    "Glob",
    "Grep",
)
_TOOL_RE = re.compile(
    r"\bmcp__[\w-]+\b"
    r"|`(?:" + "|".join(_TOOL_AMBIGUOUS) + r")`"
    r"|\b(?:" + "|".join(_TOOL_DISTINCT) + r")\b"
)
_CMD_RE = re.compile(r"`([^`\n]{3,200})`")


@dataclass
class Finding:
    check: str
    detail: str


@dataclass
class Report:
    """The verdict, plus every claim that could not be grounded."""

    session_id: str = ""
    transcript_path: str = ""
    findings: list[Finding] = field(default_factory=list)
    events_total: int = 0
    events_referenced: int = 0
    coverage: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, check: str, detail: str) -> None:
        self.findings.append(Finding(check, detail))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "events_total": self.events_total,
            "events_referenced": self.events_referenced,
            "coverage": round(self.coverage, 4),
            "findings": [{"check": f.check, "detail": f.detail} for f in self.findings],
        }


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def grounding_terms(log: EventLog) -> dict[str, set[str]]:
    """Everything the digest is permitted to assert, by category.

    Built from the event log rather than the transcript on purpose: the log is what the model
    was shown, so a claim grounded in the transcript but absent from the log is still a claim
    the model could not have known — and would be a bug in the extractor's budget, worth
    surfacing rather than excusing.
    """
    r = log.rollup
    paths = {p.lower() for p in (*r.files_read, *r.files_written)}
    commands = {_norm(c) for c in r.commands}
    tools: set[str] = set()
    for turn in log.turns:
        for ev in turn.events:
            tools.add(ev.tool.lower())
            if ev.kind in ("file_read", "file_write") and ev.target:
                paths.add(ev.target.lower())
    return {
        "paths": paths,
        "commands": commands,
        "tools": tools,
        "tickets": {t.lower() for t in (*r.tickets, *r.prs)},
    }


def _claims(text: str) -> dict[str, set[str]]:
    """Concrete, checkable assertions in a digest."""
    return {
        "paths": {m.group(0).strip().lower() for m in _PATH_RE.finditer(text)},
        "commands": {_norm(m.group(1)) for m in _CMD_RE.finditer(text)},
        "tools": {m.group(0).strip("`").lower() for m in _TOOL_RE.finditer(text)},
        "tickets": {f"#{m.group(1)}".lower() for m in _TICKET_RE.finditer(text)},
    }


def _grounded(claim: str, allowed: set[str]) -> bool:
    """A claim is grounded if it appears in, or is contained by, an allowed term.

    Substring containment in both directions is deliberate. A digest may legitimately name
    `src/scribe/parser.py` where the log holds the absolute path, and may quote the head of a
    long command. Requiring equality would fail true claims, and a gate that cries wolf is
    one that gets switched off.
    """
    if claim in allowed:
        return True
    return any(claim in term or term in claim for term in allowed)


def check_groundedness(digest_text: str, log: EventLog, report: Report) -> None:
    allowed = grounding_terms(log)
    for category, claims in _claims(digest_text).items():
        for claim in sorted(claims):
            if not _grounded(claim, allowed[category]):
                report.add("groundedness", f"{category[:-1]} not in the event log: {claim!r}")


def check_event_coverage(
    digest_text: str, log: EventLog, report: Report, *, floor: float = DEFAULT_COVERAGE_FLOOR
) -> None:
    """Assert the digest reflects a minimum share of the session's non-trivial events.

    "Non-trivial" excludes events with no target: a tool call the log could not describe is
    not something a digest can be blamed for omitting.
    """
    haystack = _norm(digest_text)
    events = [ev for turn in log.turns for ev in turn.events if ev.target]
    report.events_total = len(events)
    if not events:
        report.coverage = 1.0
        return
    referenced = 0
    for ev in events:
        needle = _norm(ev.target)[:60]
        if (needle and needle in haystack) or ev.tool.lower() in haystack:
            referenced += 1
    report.events_referenced = referenced
    report.coverage = referenced / len(events)
    if report.coverage < floor:
        report.add(
            "event_coverage",
            f"digest references {referenced}/{len(events)} events "
            f"({report.coverage:.1%}), below the {floor:.0%} floor",
        )


def check_freshness(digest_path: Path, transcript_path: Path, report: Report) -> None:
    """A digest must exist and be newer than the transcript's last write."""
    if not digest_path.exists():
        report.add("coverage", f"no digest for {transcript_path}")
        return
    try:
        if digest_path.stat().st_mtime_ns < transcript_path.stat().st_mtime_ns:
            report.add(
                "coverage",
                f"digest {digest_path} is older than the transcript it summarizes",
            )
    except OSError as exc:
        report.add("coverage", f"cannot compare timestamps: {exc}")


def check_digest(
    digest_text: str, log: EventLog, *, floor: float = DEFAULT_COVERAGE_FLOOR
) -> Report:
    """Run the deterministic gates over one digest. `Report.ok` is the verdict."""
    report = Report(session_id=log.session_id, transcript_path=log.transcript_path)
    if not digest_text.strip():
        report.add("coverage", "digest is empty")
        return report
    check_groundedness(digest_text, log, report)
    check_event_coverage(digest_text, log, report, floor=floor)
    return report


if __name__ == "__main__":  # pragma: no cover
    # `python -m scribe.qc` is the command the build plan specifies, and without this block
    # it imports this module, runs nothing and exits 0 -- a gate that cannot fail, which is
    # the exact defect the gate exists to catch. The CLI lives in qc_cli so that importing
    # `scribe.qc` as a library pulls in no argparse machinery.
    import sys

    from .qc_cli import main

    sys.exit(main())
