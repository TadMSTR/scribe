"""Orchestration: event log in, rendered markdown out.

The retry policy distinguishes three outcomes, and conflating any two of them is how this
component would fail quietly:

  * **Retryable transport failure** — rate limits, 5xx, timeouts. Backed off and retried.
  * **Schema violation** — the response did not fit the contract. Retried *when the violation
    is a bad sample* — a model that produced malformed JSON once often does not the next time,
    and rendering it anyway is how malformed output reaches a memory file. A violation that is
    a property of the input, such as a list longer than its cap, carries
    `SchemaError.retryable = False` and fails immediately rather than buying the same rejection
    twice more at ~50k input tokens a time.
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
from ..telemetry import SPAN_REJECTED, span
from .contamination import RETRY_REMINDER, build_fallback_note, detect_contamination
from .prompt import build_system_prompt, build_user_prompt, grounding_corpus
from .providers import Completion, Provider, ProviderError
from .render import render_digest, render_failure, render_suppressed
from .schema import Digest, SchemaError, caps_for, parse

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
    # Computed ONCE, here, and handed to all four consumers below. The contract the model is
    # shown (`build_system_prompt`, `build_user_prompt`), the contract it is judged against
    # (`parse`) and the cap the truncation note quotes (`render_digest`) have to be the same
    # object. Recomputing `caps_for(log)` at each call site would work today and would be one
    # refactor away from #849 — a response rejected against a limit it was never given.
    caps = caps_for(log)
    system = build_system_prompt(caps)
    user = build_user_prompt(log, caps)
    corpus = grounding_corpus(log)
    outcome = Outcome(markdown="")
    reminder = ""

    for attempt in range(1, max_attempts + 1):
        outcome.attempts = attempt
        try:
            completion: Completion = provider.complete(system + reminder, user, timeout=timeout)
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
            digest = parse(completion.text, caps)
        except SchemaError as exc:
            # Treated exactly like an API error, per the plan — including the retryable/not
            # split. A response that does not fit the contract is not a summary; a response
            # that will not fit it on the next attempt either is not worth another call.
            outcome.errors.append(f"schema: {exc}")
            if not exc.retryable:
                outcome.reason = f"schema violation (not retryable) on attempt {attempt}: {exc}"
                break
            if attempt == max_attempts:
                outcome.reason = f"schema violation after {attempt} attempts: {exc}"
                break
            sleep(base_backoff * attempt)
            continue

        rendered = render_digest(digest, heading=heading, caps=caps)
        contaminated = detect_contamination(rendered, corpus)
        if contaminated:
            # `memsearch.summarize_rejected` — emitted once per rejected attempt, matching the
            # incumbent's `_record_rejection` exactly, because the SigNoz dashboards that query
            # this span name have to keep working across the cutover. The incumbent's
            # attributes were `project`, `signal` and `attempt`; `signal` and `attempt` carry
            # over verbatim, and scribe's `session_id`/`agent` take the place of `project`,
            # which has no equivalent here. `fallback` marks the terminal rejection, the one
            # after which a suppression note is written instead of a digest — the incumbent
            # set the same flag on its second and final attempt.
            #
            # Inside the `if`, not after the loop: a session rejected on attempt 1 and accepted
            # on attempt 2 is a real contamination event that a post-loop emit would lose, and
            # "how often does the reminder rescue a run" is exactly what the dashboard is for.
            with span(
                SPAN_REJECTED,
                session_id=log.session_id,
                agent=log.agent,
                signal=contaminated,
                attempt=attempt,
                fallback=attempt >= max_attempts,
            ):
                pass
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
