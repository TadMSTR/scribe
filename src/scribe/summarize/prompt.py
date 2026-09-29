"""Prompt construction from an event log.

The prompt the old pipeline used instructed the model to "mention file names, function
names, tool names, and concrete outcomes" while being shown 6.5% of the session with every
tool call stripped out. Demanding a class of fact that has been removed from the input is a
hallucination incentive, and no amount of prompt tuning fixes it.

So this prompt does the opposite: it hands over the rollups — files read, files written,
commands, tickets, failures, all derived deterministically — and instructs the model to use
only what it was given. Post-extraction the task is formatting a structured log, which is
why a small model is sufficient.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache

from ..extract.models import EventLog
from .contamination import RETRY_REMINDER
from .schema import contract_spec, field_caps, json_schema


def _caps_clause(caps: dict[str, int] | None = None) -> str:
    """The declared-limit sentence, built from this call's caps.

    Fields are grouped by the cap they share, so the sentence stays readable as the caps
    diverge, and it cannot disagree with what `json_schema` emits or what `parse` enforces.

    Takes `caps` rather than reading the globals for the same reason `json_schema` does: since
    `caps_for` derives per session, a prose clause built from the constants would state 100
    while the schema declared 156. That is #872's failure — the model is told a limit, complies
    with it, and writes a shorter digest than the log supports, every run, invisibly, with
    nothing rejecting it to make the drift visible.
    """
    groups: dict[int, list[str]] = {}
    for name, cap in (caps or field_caps()).items():
        groups.setdefault(cap, []).append(name)
    parts = []
    for cap, names in sorted(groups.items()):
        subject = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        parts.append(f"{cap} entries for {subject}")
    return "; ".join(parts)


_SYSTEM_TEMPLATE = (
    "You summarize one software engineering session from a structured event log.\n"
    "\n"
    "Respond with a single JSON object and nothing else. Use ONLY facts present in the "
    "event log. Every file path, command, tool name and ticket number you mention must "
    "appear verbatim in the log — a downstream check verifies this and rejects the summary "
    "if it does not hold. If you do not know something, omit it; never guess a path, a "
    "number or a date.\n"
    "\n"
    "Write in the third person about what happened. Do not reproduce markdown headings, "
    "step lists or <placeholder> tokens from the source. If a skill or template was loaded, "
    "say only that it was loaded and for what purpose, never its contents.\n"
    "\n"
    "Fields:\n"
    "  asked       - one sentence: what the user wanted.\n"
    "  done        - what was actually carried out. At least one entry.\n"
    "  found       - findings, measurements and diagnoses, with their numbers.\n"
    "  decisions   - choices made and why, including things deliberately not done.\n"
    "  open_items  - what remains, is blocked, or needs a person.\n"
    "  artifacts   - files, branches, PRs and services created or changed.\n"
    "  tickets     - ticket references that appear in the log.\n"
    "\n"
    # State the limit. The schema declares it as `maxItems`, but a field description that says
    # "the ticket references that appear in the log" and a cap the model is never told is a
    # contract the model cannot satisfy — it complies, and is rejected for complying.
    "Each list field has a limit, declared as maxItems in the schema below: {caps_clause}. "
    "Do not exceed them. If the log holds more entries than a field allows, list "
    "the most significant ones up to the limit and stop: a response over the limit is rejected "
    "outright and the whole summary is lost, so a field you have had to shorten is always "
    "better than one that runs over.\n"
    "\n"
    "A limit is a ceiling, not a target. Most sessions need far fewer entries than a field "
    "allows; write as many as the log actually supports and no more.\n"
)


def build_system_prompt(caps: dict[str, int] | None = None) -> str:
    """The system half, with `caps` stated in its prose. `None` means the globals."""
    return _SYSTEM_TEMPLATE.format(caps_clause=_caps_clause(caps))


#: The default-caps rendering. Still a module constant because it is what a caller with no
#: log gets, but the live path goes through `build_system_prompt(caps_for(log))`.
SYSTEM = build_system_prompt()


@cache
def prompt_sha256() -> str:
    """The identity of what the model is asked to do, for attributing a digest after the fact.

    Covers the system template, the user-prompt framing (rendered over an empty log, so the
    wrapper and the embedded schema are hashed and no session content is), the retry reminder,
    and `schema.contract_spec` -- the cap RULES, not any one session's caps.

    A hash rather than a declared `PROMPT_VERSION` constant, deliberately. A constant is only
    as good as the discipline of bumping it, and a missed bump is invisible: the run record
    would attribute two different prompts to one version and the trend would call the change
    model drift. A hash cannot be forgotten. The cost is that it also moves on a whitespace
    edit, which is the right side to err on -- a spurious new identity is visible, a missing
    one is not.
    """
    material = {
        "system_template": _SYSTEM_TEMPLATE,
        "user_framing": build_user_prompt(EventLog(session_id="", transcript_path="")),
        "retry_reminder": RETRY_REMINDER,
        "contract": contract_spec(),
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_user_prompt(log: EventLog, caps: dict[str, int] | None = None) -> str:
    """Render the event log into the user half of the prompt.

    `caps` is threaded into the embedded schema rather than recomputed here, so the JSON the
    model is shown, the prose limit in `SYSTEM`, and what `parse` enforces are one decision
    made once in `summarize_log`.
    """
    doc = log.content_dict()
    return (
        "Event log for one session.\n\n"
        f"```json\n{json.dumps(doc, ensure_ascii=False, indent=None)}\n```\n\n"
        "Return one JSON object matching this schema:\n\n"
        f"```json\n{json.dumps(json_schema(caps), ensure_ascii=False)}\n```\n"
    )


def grounding_corpus(log: EventLog) -> str:
    """Every string a digest is allowed to draw a concrete fact from.

    Used by the contamination overlap check and, in Phase 5, by the groundedness gate. It is
    built from the same document the model was shown, so the two cannot drift apart — a
    corpus assembled independently would eventually disagree with the prompt and start
    failing true claims.
    """
    return log.grounding_text()
