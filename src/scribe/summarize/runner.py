"""Orchestration: event log in, rendered markdown out.

The retry policy distinguishes three outcomes, and conflating any two of them is how this
component would fail quietly:

  * **Retryable transport failure** — rate limits, 5xx, timeouts. Backed off and retried.
  * **Schema violation** — the response did not fit the contract. Retried, because a model
    that produced malformed JSON once often does not the next time, and because rendering it
    anyway is how malformed output reaches a memory file.
  * **Contamination** — the response copied template text. Retried *with a reminder appended*,
    then falls back to a deterministic suppression note rather than to nothing.

On permanent failure a marked placeholder is written. It carries the transcript path so the
block is replayable, and **not the transcript** — leaving raw content on disk for a failed
summary is vikunja#386.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..extract.models import EventLog
from .contamination import RETRY_REMINDER, build_fallback_note, detect_contamination
from .prompt import SYSTEM, build_user_prompt, grounding_corpus
from .providers import Completion, Provider, ProviderError
from .render import render_digest, render_failure, render_suppressed
from .schema import Digest, SchemaError, parse

#: A 429 backs off by 3x the base interval, matching the semantics of the daemon this
#: replaces so operational behaviour does not change under the cutover.
RATE_LIMIT_MULTIPLIER = 3


@dataclass
class Outcome:
    """What happened, in enough detail for the caller to record spend and decide status."""

    markdown: str
    digest: Digest | None = None
    ok: bool = False
    suppressed: bool = False
    reason: str = ""
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    errors: list[str] = field(default_factory=list)


def summarize_log(
    log: EventLog,
    provider: Provider,
    *,
    heading: str = "",
    max_attempts: int = 3,
    base_backoff: float = 2.0,
    timeout: float = 120.0,
    sleep=time.sleep,
) -> Outcome:
    """Summarize one event log, retrying per the policy above.

    `sleep` is injected so backoff is exercised in tests without spending the wall-clock —
    a test that genuinely sleeps gets deleted the first time someone is in a hurry.
    """
    user = build_user_prompt(log)
    corpus = grounding_corpus(log)
    outcome = Outcome(markdown="")
    reminder = ""

    for attempt in range(1, max_attempts + 1):
        outcome.attempts = attempt
        try:
            completion: Completion = provider.complete(SYSTEM + reminder, user, timeout=timeout)
        except ProviderError as exc:
            outcome.errors.append(str(exc))
            if not exc.retryable or attempt == max_attempts:
                outcome.reason = str(exc)
                break
            delay = base_backoff * attempt
            if exc.status == 429:
                delay *= RATE_LIMIT_MULTIPLIER
            sleep(delay)
            continue

        outcome.input_tokens += completion.input_tokens
        outcome.output_tokens += completion.output_tokens
        outcome.model = completion.model
        outcome.provider = completion.provider

        try:
            digest = parse(completion.text)
        except SchemaError as exc:
            # Treated exactly like an API error, per the plan. A response that does not fit
            # the contract is not a summary.
            outcome.errors.append(f"schema: {exc}")
            if attempt == max_attempts:
                outcome.reason = f"schema violation after {attempt} attempts: {exc}"
                break
            sleep(base_backoff * attempt)
            continue

        rendered = render_digest(digest, heading=heading)
        contaminated = detect_contamination(rendered, corpus)
        if contaminated:
            outcome.errors.append(f"contamination: {contaminated}")
            if attempt < max_attempts:
                reminder = RETRY_REMINDER
                sleep(base_backoff * attempt)
                continue
            # Fall back to the deterministic note rather than to nothing: a suppressed
            # summary still records that the session happened and why it is not here.
            outcome.markdown = render_suppressed(note=build_fallback_note(corpus), heading=heading)
            outcome.suppressed = True
            outcome.reason = f"contamination: {contaminated}"
            return outcome

        outcome.markdown = rendered
        outcome.digest = digest
        outcome.ok = True
        return outcome

    outcome.markdown = render_failure(
        transcript_path=log.transcript_path,
        reason=outcome.reason or "unknown failure",
        attempts=outcome.attempts,
        heading=heading,
    )
    return outcome
