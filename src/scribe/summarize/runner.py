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

import re
import time
from dataclasses import dataclass, field, replace

from ..extract.models import EventLog
from ..extract.redact import Redactor
from ..telemetry import SPAN_REJECTED, span
from .contamination import (
    RETRY_REMINDER,
    Detection,
    _has_residue,
    build_fallback_note,
    explain_contamination,
    redacted_excerpt,
)
from .prompt import build_system_prompt, build_user_prompt, grounding_corpus
from .providers import Completion, Provider, ProviderError
from .render import _SECTIONS, _bullet, render_digest, render_failure, render_suppressed
from .schema import Digest, SchemaError, caps_for, parse

#: A 429 backs off by 3x the base interval, matching the semantics of the daemon this
#: replaces so operational behaviour does not change under the cutover.
RATE_LIMIT_MULTIPLIER = 3

#: Longest `token` put on the rejected span. A residue token is a few characters (`N`, `n/N`,
#: `YYYY-MM-DD`); the bound is there so the span can never become a channel for model text.
SPAN_TOKEN_CHARS = 16

#: A digit run this long or longer is masked in the span token. `\d+/[NM]` admits any
#: number of digits, so without this a 16-digit secret could reach SigNoz as a "token".
#: Four digits stay, so a `YYYY`-shaped year or a `256/N` still reads as written.
_SPAN_DIGITS_RE = re.compile(r"\d{5,}")


def _span_token(token: str) -> str:
    return _SPAN_DIGITS_RE.sub("#", token)[:SPAN_TOKEN_CHARS]


#: The only terminal signal that degrades per item rather than suppressing. It is line-based,
#: so the offending text sits in identifiable items, and it is the only detector that has
#: fired in production (42 of 42 rejections over 30 days, vikunja#1216). The others keep
#: suppressing: they mean the model reproduced the source's shape, not that it left a token.
DEGRADABLE_SIGNAL = "residue_token"


@dataclass
class Outcome:
    """What happened, in enough detail for the caller to record spend and decide status."""

    markdown: str
    digest: Digest | None = None
    ok: bool = False
    suppressed: bool = False
    #: The detector signal behind a suppression (`residue_token`, ...), "" otherwise. Its own
    #: field so the run record can carry it without parsing `reason` (vikunja#1216).
    contamination_signal: str = ""
    #: Which rule fired (`Detection.rule`), "" when no rule names it.
    contamination_rule: str = ""
    #: What the model wrote around the hit, ALREADY REDACTED (`redacted_excerpt`). Set from
    #: the terminal rejection only. Never copied into `errors`, which reaches the JSON report
    #: and the CLI unredacted, and never put on a span.
    contamination_excerpt: str = ""
    #: Items the contamination guard dropped from a written digest, in total and per field.
    #: Non-zero only for a degraded success, where `ok` is True and `contamination_signal`
    #: still says why.
    withheld_items: int = 0
    withheld_fields: dict[str, int] = field(default_factory=dict)
    reason: str = ""
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    #: `Completion.model_resolved` of the last response -- "" when the provider named none.
    model_resolved: str = ""
    #: The run ended on a `ProviderError` that says the provider is down (`unavailable`), not
    #: that this session's input is at fault. The pipeline leaves such a session pending and
    #: spends none of its retry budget; see `pipeline.process_session`.
    provider_unavailable: bool = False
    errors: list[str] = field(default_factory=list)


def _withhold_residue(digest: Digest) -> tuple[Digest, dict[str, int]] | None:
    """`digest` without its residue-carrying list items, and what was dropped per field. None
    when the digest cannot be degraded and must be suppressed as before.

    Each item is tested as it renders (`_bullet`), with the same `_has_residue` the detector
    uses, so an item is dropped for exactly what the detector objects to.

    **Majority floor.** Suppress when more than half the list items would go, or none would
    be left. A model that echoes a template leaves residue in a few items and real content
    in the rest; a digest that is mostly residue IS the echo, and trimming it until it passes
    would defeat the guard. Exactly half withheld still degrades. Half is the plan's value,
    kept because it is the point where the digest is no longer mostly the model's own
    content. The boundary is pinned on both sides in `test_runner.py`.

    `asked` is not a list and has no partial form, so residue there always suppresses.
    """
    if _has_residue(" ".join(digest.asked.split())):
        return None
    kept: dict[str, list[str]] = {}
    withheld: dict[str, int] = {}
    for attr, _label in _SECTIONS:
        values = getattr(digest, attr)
        kept[attr] = [v for v in values if not _has_residue(_bullet(v))]
        if len(kept[attr]) < len(values):
            withheld[attr] = len(values) - len(kept[attr])
    total = sum(len(getattr(digest, attr)) for attr, _label in _SECTIONS)
    dropped = sum(withheld.values())
    if not dropped or dropped * 2 > total or dropped == total:
        return None
    return replace(digest, **kept), withheld


def _record_detection(outcome: Outcome, detection: Detection) -> None:
    """Put the terminal detection on `outcome`, the excerpt redacted on the way in."""
    outcome.contamination_signal = detection.signal
    outcome.contamination_rule = detection.rule
    # Lone surrogates replaced: provider JSON can decode to one, and a strict UTF-8 write
    # would then lose the whole run record, which is the record that explains the
    # contamination (pre-audit F-2, the IV-61 shape).
    excerpt = redacted_excerpt(detection, Redactor().scrub)
    outcome.contamination_excerpt = excerpt.encode("utf-8", "replace").decode("utf-8")


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
                # Decided by how the run ENDED. Earlier attempts that failed otherwise do not
                # matter: the last word was that the provider could not be reached.
                outcome.provider_unavailable = exc.unavailable
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
        outcome.model_resolved = completion.model_resolved

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
        detection = explain_contamination(rendered, corpus)
        if detection:
            contaminated = detection.signal
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
            #
            # `rule` and `token` (0.17.0) say what matched. The excerpt does NOT go here: it is
            # model text, and SigNoz keeps spans longer than anyone reads them. `fallback`
            # still means "terminal"; whether that terminal rejection then degraded or
            # suppressed is `withheld_items` on the summarize span.
            with span(
                SPAN_REJECTED,
                session_id=log.session_id,
                agent=log.agent,
                signal=contaminated,
                attempt=attempt,
                fallback=attempt >= max_attempts,
                rule=detection.rule,
                token=_span_token(detection.token),
            ):
                pass
            outcome.errors.append(f"contamination: {contaminated}")
            if attempt < max_attempts:
                reminder = RETRY_REMINDER
                sleep(base_backoff * attempt)
                continue
            _record_detection(outcome, detection)
            # Terminal `residue_token`: drop the offending items rather than the session, then
            # re-check the reduced digest against EVERY detector, once. Anything it finds,
            # including residue that only shows once an item is gone, suppresses as before,
            # with that detection recorded. No loop and no second pruning pass.
            reduced = _withhold_residue(digest) if contaminated == DEGRADABLE_SIGNAL else None
            if reduced is not None:
                smaller, withheld = reduced
                degraded = render_digest(
                    smaller,
                    heading=heading,
                    caps=caps,
                    withheld=withheld,
                    withheld_signal=contaminated,
                )
                recheck = explain_contamination(degraded, corpus)
                if recheck is None:
                    outcome.markdown = degraded
                    outcome.digest = smaller
                    outcome.ok = True
                    outcome.withheld_fields = withheld
                    outcome.withheld_items = sum(withheld.values())
                    return outcome
                _record_detection(outcome, recheck)
                contaminated = recheck.signal
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
