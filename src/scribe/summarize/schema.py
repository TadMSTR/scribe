"""The digest schema — named fields, validated before anything is rendered.

The pipeline this replaces asked for "2-6 bullets" over an unstructured blob. Two failures
follow from that ask, and both are structural rather than model-specific:

  * With no output schema the model anchors on the easy text. On 2026-09-13 a turn containing
    four one-line progress messages and one dense final report produced five bullets derived
    entirely from the four short lines, discarding a report that named #831, #832, #842, a
    401-vs-refused control and a 12→10 key count.
  * "Bullets" have no place to put a decision or an open item, so those are simply lost.

Named fields fix the ask. Validation makes the fix enforceable: **a schema violation is
treated exactly like an API error** — retried, and on permanent failure turned into a marked
placeholder — because a response that does not fit the contract is not a summary, and
rendering it anyway is how malformed output reaches a memory file.

"Exactly like an API error" includes the part that matters most about an API error: some of
them will fail identically forever. Malformed JSON or a missing field is sampling noise and a
fresh attempt often fixes it, so those stay retryable. A list over its cap is determined by the
input — the log holds what it holds — so retrying it spends two more ~50k-token calls to
receive the same rejection. `SchemaError.retryable` is what separates the two.

**But "does not fit the contract" is two different things, and only one of them is a reason to
throw the summary away.** A response can be wrong — inventing tickets the log never mentions —
or it can merely be long. Treating both as violations cost three sessions' worth of builds and
an unknown number of digests: #849, #872 and #884 are the same bug found three times, each
time diagnosed as "the cap is too low" and fixed by raising a number. It was never the number.

A field whose ceiling is knowable from the event log VALIDATES, because exceeding it is
evidence of invention. A field with no knowable ceiling TRUNCATES, because no threshold makes
free prose wrong and a rejection would trade a whole session for one surplus bullet. That is
`_Field.bounded`, and it is the rule the three separate cap constants were groping towards.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import NamedTuple

SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """A model response that does not satisfy the digest contract.

    `retryable` mirrors `ProviderError.retryable` and defaults to **True**: most violations are
    a bad sample and the next one is usually clean. It is set False only where the violation is
    a property of the input rather than of the sample, which today means exactly one case — a
    BOUNDED list longer than its cap, where every attempt sees the same log and produces the
    same over-long field. An unbounded list over its cap is no longer an error at all; it is
    truncated and the digest is written. See `_Field.bounded`.

    **Keep these messages content-free.** A field name, a count, a type name, a JSON location —
    never a snippet of the response. The message becomes `Outcome.reason` and fans out to three
    sinks, and only the first of them is redacted:

      * the rendered placeholder, which `pipeline.py` re-scrubs with `Redactor` before writing;
      * `store.record_attempt(error=...)` / `store.set_status(error=...)`, written to the state
        database verbatim;
      * `SessionResult.errors`, which reaches the run report JSON and the CLI verbatim.

    Sanitising at one sink does not protect the other two, so the constraint lives here at the
    source. `test_schema.py::test_no_violation_message_quotes_the_response` enforces it.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


#: Cap for `found`, `decisions` and `open_items` -- model prose with nothing behind it to
#: bound it, so this is a genuine runaway guard: the event log does not say how many entries
#: are correct.
#:
#: **This number is not a rejection threshold.** These three are UNBOUNDED fields, so passing
#: the cap truncates rather than discards -- see `_LIST_FIELDS` for the rule and why the
#: distinction rather than the number is what matters. The cap still does real work: it is
#: declared in `json_schema`, so the model sheds to fit it, and truncation is the backstop for
#: when shedding is not enough.
#:
#: **What this comment used to say, and why it was wrong.** It claimed these three "have never
#: violated it once across 442 sessions", and argued that unlike `done`'s the observation was
#: uncensored, "so an absence of failures is real evidence of headroom". Both halves failed.
#: On 2026-09-17 `found` arrived with **44** items and took a whole session with it
#: (vikunja#884). And the reasoning was never sound: a violation is indeed logged whether or
#: not the digest survives, but that only makes the FAILURES visible -- it says nothing about
#: the written distribution, which is right-censored here exactly as it was for `done`. The
#: censoring merely hides better. Because the cap is declared, the model sheds and the
#: survivors land *below* 40 rather than piling up at it: measured across 458 written blocks
#: `found` tops out at 33, which reads as comfortable headroom and is an artefact of the cap.
#: The only uncensored view is the rejection, and it was 44.
MAX_ITEMS = 40

#: Cap for `done`, and the reason it is not `MAX_ITEMS`.
#:
#: `done` sat at 40 with the other three until vikunja#872. It was the only field ever to
#: violate a cap -- 30 distinct failures, every one of them `done`, at 41, 42, 43, 44, 45, 46,
#: 47, 49, 53, 65 and 67 items. Each one is a whole session lost, because a cap violation is
#: `retryable=False` and goes straight to a placeholder.
#:
#: **Do not re-derive this number from the written digests.** Counted across 418 written
#: blocks, `done` reads min 1 / median 14 / p90 26 / p99 37 / **max exactly 40**, zero above.
#: That looks like comfortable headroom and it is an artefact: the distribution is
#: **right-censored** at the cap, because everything above it was rejected and is therefore
#: absent from the sample. The survivors top out at 40 *because* the cap is 40.
#:
#: The failure tail is the uncensored view of the same field, and it reaches 67 -- but 67 is
#: a **floor** on the true ceiling, not the ceiling. `json_schema` declares `maxItems` to the
#: model, and #849 measured that a declared cap makes the model shed items rather than exceed
#: them (a declared 40 produced 27/35/28 tickets across three runs where 100 produced 53 each
#: time). So the observed output tail is itself a function of the cap being set, and cannot
#: be used to choose the cap without circularity.
#:
#: The only bound here that is independent of the cap is the **input**. `done` describes the
#: work a session did, and the richest material it has to cover is `rollup.commands`, measured
#: over 442 persisted event logs (#852): **max 187**, p99 164, 112 sessions over 40, 38 over
#: 100. A faithful `done` cannot enumerate more distinct actions than the session contained.
#:
#: 200 is above that measured input ceiling. 100 -- the obvious candidate, matching
#: `MAX_ROLLUP_ITEMS` -- is 1.5x a censored number and sits *below* the command counts of 38
#: sessions in the corpus, which is how this bug would recur with a bigger number.
MAX_DONE_ITEMS = 200

#: Cap for `tickets` and `artifacts`. Both are drawn from the extractor's own rollup, so they
#: have a knowable ceiling — the model cannot list more tickets than the log contains. Measured
#: over 432 sessions (`evidence/rollup-survey.py`, build plan scribe-schema-caps-2026-09):
#: `tickets` max 76 / p99 58; `artifacts` (files_written + prs + git_refs) max 58 / p99 48;
#: **zero** sessions over 80 in a month. 100 is 1.3x the observed max with no exceedance in the
#: corpus. Guarding these two with the free-text cap is what turned 13 faithful digests into
#: placeholders (vikunja#849) — a bounded field cannot run away, and does not need that guard.
#:
#: Do not lower this towards the observed max. Since `json_schema` declares the cap, the model
#: now sheds items to fit it: measured on `aa6634a7` (58 tickets in the rollup), a declared cap
#: of 40 produced 27, 35 and 28 tickets across three identical runs, while 100 produced 53 every
#: time. A cap near the ceiling does not reject the digest any more — it quietly shortens it, by
#: a different amount each run.
MAX_ROLLUP_ITEMS = 100

MAX_ITEM_CHARS = 2000


class _Field(NamedTuple):
    """One list field's contract: what it is called, whether it is required, and how it is
    guarded when it overflows."""

    name: str
    required: bool
    cap: int
    #: **The rule this whole module has been approximating for three builds.**
    #:
    #: True when the field's ceiling is knowable from the event log, so an over-long value
    #: means the MODEL WENT WRONG: `tickets` and `artifacts` cannot exceed what the rollup
    #: contains, and `done` cannot enumerate more distinct actions than the session performed.
    #: A count above the cap is then evidence of invention, and rejecting the response is the
    #: correct, safe answer.
    #:
    #: False when nothing on the input side bounds it -- `found`, `decisions` and `open_items`
    #: are free prose, and a long one means the model went LONG, not wrong. For these, ANY cap
    #: is an arbitrary cliff, so rejection is never the right answer: it trades a complete
    #: session for nothing. They truncate instead.
    #:
    #: The three-constant split (`MAX_ITEMS` / `MAX_DONE_ITEMS` / `MAX_ROLLUP_ITEMS`) was this
    #: distinction expressed as numbers, which is why raising a number kept looking like the
    #: fix and kept not being one. #849 raised `tickets` and `artifacts`; #872 raised `done`;
    #: #884 was `found`, and raising it would only have relocated the cliff. The failure MODE,
    #: not the threshold, is what differs between the two classes.
    bounded: bool


#: Only `asked` is required: a session with no findings and no decisions is a real session,
#: and forcing content into those fields is an invitation to invent it.
#:
#: **This tuple is the single source of the caps.** `json_schema` emits them as `maxItems`,
#: `parse` enforces them, and `prompt.SYSTEM` states them in prose — all three read from here.
#: The prose used to be hand-written, which is how `done` could be moved off `MAX_ITEMS`
#: while the prompt still told the model 40 (vikunja#872).
_LIST_FIELDS: tuple[_Field, ...] = (
    _Field("done", True, MAX_DONE_ITEMS, bounded=True),
    _Field("found", False, MAX_ITEMS, bounded=False),
    _Field("decisions", False, MAX_ITEMS, bounded=False),
    _Field("open_items", False, MAX_ITEMS, bounded=False),
    _Field("artifacts", False, MAX_ROLLUP_ITEMS, bounded=True),
    _Field("tickets", False, MAX_ROLLUP_ITEMS, bounded=True),
)


def field_caps() -> dict[str, int]:
    """Each list field's declared cap, in declaration order.

    Exists so the prompt can state the caps without restating them. A hand-written sentence
    and a validated constant are two copies of one fact, and they drifted: `done` moved from
    40 to 200 and a literal in the prompt would still have said 40 — declaring a limit the
    model then complies with, and is rejected for complying with, is exactly the failure
    #849 fixed and #872 re-ran.
    """
    return {f.name: f.cap for f in _LIST_FIELDS}


@dataclass
class Digest:
    """One session's summary, in the shape the renderer and the QC gate both expect."""

    #: Unbounded fields that overflowed, mapped to how many items were dropped. Empty for
    #: the overwhelming majority of digests.
    #:
    #: Carried on the digest rather than raised, because a truncated digest is a SUCCESS —
    #: it is summarized, and it is written. The renderer states it in the block so the loss is
    #: visible to whoever reads the memory file, and `pipeline` counts it so a run report can
    #: show it. Silent truncation would be the worst of the three options: #849 and #872 were
    #: both found because the failure was loud, and a quiet one would not have been.
    truncated: dict[str, int] = field(default_factory=dict)

    asked: str = ""
    done: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


def json_schema() -> dict:
    """The JSON Schema sent to providers that support structured output.

    `maxItems` is **declared**, not merely enforced. A model cannot respect a limit it is never
    given: the prompt asked for "ticket references that appear in the log" with no limit stated
    anywhere, and the validator then rejected a response that did exactly that. Declaring the
    cap also puts the choice of what to drop with the model, which can pick the least relevant
    items — strictly better than a downstream truncation taking whatever landed past position N.

    The `maxItems` emitted here is the same number `parse` enforces, per field. That is the
    whole point: the contract the model is shown and the contract it is judged against must be
    the same one.
    """
    props: dict = {"asked": {"type": "string"}}
    for f in _LIST_FIELDS:
        props[f.name] = {"type": "array", "items": {"type": "string"}, "maxItems": f.cap}
    return {
        "type": "object",
        "properties": props,
        "required": ["asked", "done"],
        "additionalProperties": False,
    }


def _as_list(value: object, f: _Field) -> tuple[list[str], int]:
    """Coerce a field to a list of non-empty strings, or raise. Returns `(values, dropped)`.

    A string where a list belongs is accepted as a one-element list. That is a real and
    common model output, it is unambiguous, and rejecting it would burn a retry on something
    that carries the intended meaning perfectly well. Anything else is a violation.

    The cap is a backstop rather than the primary enforcement, since `json_schema` declares it
    — it fires when a provider ignores `maxItems`, not when the model was simply never told.
    What happens on overflow depends on `_Field.bounded`:

      * **bounded** — raise, not retryable. The count cannot legitimately exceed the log, so
        this is the model inventing entries, and the response should not be rendered.
      * **unbounded** — drop the excess and report how many. There is no threshold at which
        free prose becomes wrong, so there is nothing a rejection could be protecting; it
        would only convert a complete session into no session at all (vikunja#884).

    Items are dropped from the END, which is the one genuinely unattractive part of this. The
    model, having been shown the cap, is the better judge of what to shed — that is why
    declaring `maxItems` stays the primary mechanism and this is only the backstop.
    """
    if value is None:
        return [], 0
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if not isinstance(value, list):
        raise SchemaError(f"field {f.name!r} must be a list of strings, got {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if isinstance(item, (int, float, bool)):
            item = str(item)
        if not isinstance(item, str):
            raise SchemaError(f"field {f.name!r} contains a {type(item).__name__}, expected string")
        item = item.strip()
        if item:
            out.append(item[:MAX_ITEM_CHARS])
    if len(out) <= f.cap:
        return out, 0
    if f.bounded:
        # Not retryable: the count follows from the log, so attempts two and three see the
        # same input and produce the same rejection. See the module docstring.
        raise SchemaError(
            f"field {f.name!r} has {len(out)} items, more than the {f.cap} cap", retryable=False
        )
    return out[: f.cap], len(out) - f.cap


def parse(raw: str | dict) -> Digest:
    """Validate a model response into a `Digest`, or raise `SchemaError`.

    Accepts a dict or a JSON string, including one wrapped in a ```json fence — models emit
    that often enough that failing on it would spend retries on presentation rather than
    content.
    """
    if isinstance(raw, dict):
        data = raw
    else:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else ""
            if text.rstrip().endswith("```"):
                text = text.rstrip()[: -len("```")]
        if not text.strip():
            raise SchemaError("empty response")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SchemaError(f"response is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SchemaError(f"response must be a JSON object, got {type(data).__name__}")

    asked = data.get("asked")
    if not isinstance(asked, str) or not asked.strip():
        raise SchemaError("field 'asked' is required and must be a non-empty string")

    digest = Digest(asked=asked.strip()[:MAX_ITEM_CHARS])
    for f in _LIST_FIELDS:
        values, dropped = _as_list(data.get(f.name), f)
        if f.required and not values:
            raise SchemaError(f"field {f.name!r} is required and must have at least one entry")
        setattr(digest, f.name, values)
        if dropped:
            digest.truncated[f.name] = dropped
    return digest
