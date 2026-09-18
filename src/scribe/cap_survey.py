"""Measure the INPUT distribution the schema caps are derived from.

This is the tool that makes `schema.caps_for`'s constants checkable rather than archaeological.
Every number in `MAX_DONE_ITEMS`, `MAX_ROLLUP_ITEMS` and `caps_for` came from a run of this
survey, and a comment asserting a measurement the committed tool cannot reproduce is worse
than no comment at all.

**It reads persisted event logs, not transcripts.** The version that lived in a build-plan
directory re-ran `scribe.extract` over the raw transcripts, which is slow and — more
importantly — impossible for any session whose transcript has aged out at 30 days. The event
log is the durable copy, and the rollup in it is exactly what the summarizer was shown.

**It measures the input side only, and that is not a limitation to be fixed.** AGENTS.md
invariant 11: `json_schema` declares `maxItems`, so the model sheds to fit and the written
corpus is right-censored below whatever cap was in force. Re-deriving a cap from written
digests reads the cap back out of its own consequences. `ratios` below pairs output against
input deliberately — to check a *denominator*, which is a claim about proportion — and never
to choose a cap.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .summarize.schema import _LIST_FIELDS, field_caps


@dataclass
class FieldSurvey:
    """One field's input ceiling across the corpus, and what today's rule would do with it."""

    field: str
    denominator: str
    n: int
    maximum: int
    p99: int
    median: int
    #: Logs whose derived cap rises above the global floor. Small by design: the floor is set
    #: so that ordinary sessions behave exactly as they did before this build.
    above_floor: int
    floor: int
    max_cap: int

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "denominator": self.denominator,
            "logs": self.n,
            "input_max": self.maximum,
            "input_p99": self.p99,
            "input_median": self.median,
            "floor": self.floor,
            "derived_max": self.max_cap,
            "logs_above_floor": self.above_floor,
        }


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo = math.floor(k)
    hi = min(lo + 1, len(ordered) - 1)
    return int(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def _denominators(log: dict) -> dict[str, int]:
    """Each derived field's denominator, read straight off the persisted document.

    Deliberately reads the raw dict rather than going through `log_from_dict`: the survey must
    keep working on a log written by an older schema version, and a strict rebuild would
    refuse exactly the oldest logs — the ones that establish how the corpus has moved.
    """
    rollup = log.get("rollup") or {}
    stats = log.get("stats") or {}
    return {
        "stats.tool_events": int(stats.get("tool_events") or 0),
        "rollup.tickets": len(rollup.get("tickets") or []),
        "rollup.files_written|prs|git_refs": len(
            set(rollup.get("files_written") or [])
            | set(rollup.get("prs") or [])
            | set(rollup.get("git_refs") or [])
        ),
        # Carried only so the survey can keep showing that it is NOT the right denominator
        # for `done`. See `ratios`.
        "rollup.commands": len(rollup.get("commands") or []),
    }


#: What each derived field is measured against. Mirrors `_LIST_FIELDS`' `derive` callables;
#: the survey names them as strings so its output is readable without importing the schema.
DENOMINATORS: dict[str, str] = {
    "done": "stats.tool_events",
    "tickets": "rollup.tickets",
    "artifacts": "rollup.files_written|prs|git_refs",
}


def load_logs(eventlog_dir: str | Path) -> list[dict]:
    """Every persisted event log under `eventlog_dir`, skipping any that will not parse.

    A malformed log is skipped rather than raising: the survey's job is to describe the
    corpus, and one bad file must not make the other 469 unmeasurable.
    """
    out: list[dict] = []
    for path in sorted(Path(eventlog_dir).expanduser().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def survey(logs: list[dict]) -> list[FieldSurvey]:
    """The input ceiling per derived field, and where the derived cap lands."""
    caps = field_caps()
    by_name = {f.name: f for f in _LIST_FIELDS}
    results: list[FieldSurvey] = []
    for name, denominator in DENOMINATORS.items():
        f = by_name[name]
        values = [_denominators(log)[denominator] for log in logs]
        derived = [max(math.ceil(v * f.headroom), f.cap) for v in values]
        results.append(
            FieldSurvey(
                field=name,
                denominator=denominator,
                n=len(values),
                maximum=max(values, default=0),
                p99=_percentile(values, 0.99),
                median=_percentile(values, 0.50),
                above_floor=sum(1 for d in derived if d > caps[name]),
                floor=caps[name],
                max_cap=max(derived, default=caps[name]),
            )
        )
    return results


def ratios(logs: list[dict]) -> dict[str, dict[str, float]]:
    """How each candidate denominator compares to the others, per log.

    Reported as a share of `stats.tool_events`, which is the denominator `done` uses. This is
    the check that caught the original defect: `schema.py` asserted for three builds that
    `rollup.commands` bounded `done`, and on this fleet `commands` is a small and wildly
    variable fraction of the work a session did — one bash call beside 27 MCP calls is an
    ordinary session here. A denominator that is 4% of the activity on the median log is not
    a ceiling on anything.
    """
    out: dict[str, dict[str, float]] = {}
    for label in ("rollup.commands", "rollup.tickets", "rollup.files_written|prs|git_refs"):
        shares = [
            d[label] / d["stats.tool_events"]
            for d in (_denominators(log) for log in logs)
            if d["stats.tool_events"] > 0
        ]
        if not shares:
            continue
        ordered = sorted(shares)
        out[label] = {
            "median": round(ordered[len(ordered) // 2], 4),
            "p99": round(ordered[min(int(len(ordered) * 0.99), len(ordered) - 1)], 4),
            "max": round(ordered[-1], 4),
        }
    return out
