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
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import NamedTuple

from ..extract.models import EventLog

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
#: The only bound here that is independent of the cap is the **input**.
#:
#: **This comment used to name `rollup.commands` as that input, and it was the wrong
#: denominator.** Measured 2026-09-18 by pairing all 479 written digest blocks with their own
#: event logs, `done` exceeded `rollup.commands` in **171 of 382** blocks -- 45% -- reaching
#: 51x. The outliers are not noise: the worst are sessions with ONE bash command alongside
#: 19-27 MCP calls and up to 21 file writes. `done` describes work, and on this fleet the work
#: is overwhelmingly not bash. Had a cap been derived from `commands`, roughly half of all
#: digests would have been rejected or silently shortened.
#:
#: `stats.tool_events` is the denominator that holds: **0 of 472** blocks exceeded it, max
#: ratio exactly 1.00, and the ratio falls as the session grows (p95 0.50, and 0.06 at the
#: largest log in the corpus). That is what `_Field.derive` uses. See `caps_for`.
#:
#: **What 200 is now.** It is no longer the cap in the ordinary case -- it is the FLOOR under
#: the derived one, and the fallback when there is no log to derive from. Its job changed from
#: "sit above the corpus's input ceiling" to "leave every small session behaving exactly as it
#: does today", which is what confines this build's behaviour change to the large sessions
#: that are the actual problem. 200 is kept at its existing value for precisely that reason.
#:
#: **Do not lower it towards the observed output.** In the floor-governed regime `done` reads
#: max 58 across 479 blocks, which looks like 3.5x of slack and is right-censoring -- see
#: AGENTS.md invariant 11. The number above is an input measurement; that one is not.
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
#:
#: **Re-measured 2026-09-18, and by then it had already fired.** Across the current 470 event
#: logs the input ceilings are `tickets` max 104 (was 76) and `artifacts` max 68 (was 58). 100
#: sat below the first of those, and
#: `~/.local/share/scribe/digests/research/2026-09-15.md` holds the placeholder it produced:
#: session `6017c8ce`, a faithful 103-item `tickets` response discarded against a 104-ticket
#: rollup. That is vikunja#849's failure recurring on the field the `bounded` class exists to
#: protect (vikunja#901).
#:
#: Raising it again was the move #849, #872 and #884 each made and each regretted; it decayed
#: within 38 event logs. So 100 is **kept** and demoted to a FLOOR, with the working cap
#: derived per session by `caps_for`. A global chosen from a corpus snapshot decays silently;
#: a bound read off the log in hand cannot.
MAX_ROLLUP_ITEMS = 100

#: Hard ceiling on a derived cap, as a multiple of that field's floor. `caps_for` clamps to
#: `floor * MAX_DERIVED_MULTIPLE`.
#:
#: **This number is not measured, and unlike every other constant in this module it is not
#: trying to be.** The others are chosen against an observed distribution; this one is a
#: containment bound on an input scribe does not control. `caps_for`'s denominators come from
#: the session's persisted event log, so anyone able to write under the event-log directory
#: could inflate `stats.tool_events` and hand themselves an unbounded `done` cap, removing the
#: `bounded` class's invention guard for that session entirely. With the clamp they can widen
#: it 10x and no further.
#:
#: Filed as the one Low finding of the 2026-09-18 audit of scribe-schema-cap-derivation-2026-09,
#: and it is defence in depth rather than a fix: an actor who can write an event log already
#: has a strictly worse primitive — editing the log's own turn content, which the summarizer
#: treats as ground truth. It is also **not** a memory bound; `_as_list` builds its list from
#: the model's response before consulting the cap, so allocation never depended on this.
#:
#: 10 is deliberately far above anything real. Measured across all 470 persisted event logs on
#: 2026-09-18, the largest derived cap was `done` 336 against a ceiling of 2000 — **no log in
#: the corpus comes within 6x of the clamp**, so it changes no present behaviour and exists
#: only to bound the pathological case. Do not tune it towards the corpus: that would give a
#: measured number the job of a safety limit, which is the confusion this comment exists to
#: prevent.
MAX_DERIVED_MULTIPLE = 10

MAX_ITEM_CHARS = 2000


def _den_done(log: EventLog) -> int:
    """`done`'s ceiling: every tool call the session made.

    Not `rollup.commands`, which this module named for three builds and which 45% of written
    blocks exceed — see `MAX_DONE_ITEMS`. `tool_events` counts bash, MCP, file reads, file
    writes, searches and agent calls alike, which is what "the work a session did" actually
    means on this fleet.
    """
    return log.stats.tool_events


def _den_tickets(log: EventLog) -> int:
    return len(log.rollup.tickets)


def _den_artifacts(log: EventLog) -> int:
    """`artifacts`' ceiling, and the weakest of the three.

    `prompt.SYSTEM` asks for "files, branches, PRs and **services** created or changed", and
    **services has no rollup source at all** — there is no list to union in. So this
    undercounts by construction, which is why `artifacts` carries the largest headroom of the
    three rather than the smallest its measured band would allow.
    """
    return len(set(log.rollup.files_written) | set(log.rollup.prs) | set(log.rollup.git_refs))


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
    #: How to read this field's ceiling off the session's own event log, or None to use `cap`
    #: unchanged. Set only for `bounded` fields: an unbounded field has no countable input,
    #: which is the whole reason it is unbounded.
    derive: Callable[[EventLog], int] | None = None
    #: Multiplier on `derive`'s result. **Per field, deliberately not shared** — see
    #: `caps_for` for the measurement behind each one.
    headroom: float = 0.0


#: Only `asked` is required: a session with no findings and no decisions is a real session,
#: and forcing content into those fields is an invitation to invent it.
#:
#: **This tuple is the single source of the caps.** `json_schema` emits them as `maxItems`,
#: `parse` enforces them, and `prompt.SYSTEM` states them in prose — all three read from here.
#: The prose used to be hand-written, which is how `done` could be moved off `MAX_ITEMS`
#: while the prompt still told the model 40 (vikunja#872).
_LIST_FIELDS: tuple[_Field, ...] = (
    _Field("done", True, MAX_DONE_ITEMS, bounded=True, derive=_den_done, headroom=0.5),
    _Field("found", False, MAX_ITEMS, bounded=False),
    _Field("decisions", False, MAX_ITEMS, bounded=False),
    _Field("open_items", False, MAX_ITEMS, bounded=False),
    _Field("artifacts", False, MAX_ROLLUP_ITEMS, bounded=True, derive=_den_artifacts, headroom=2.0),
    _Field("tickets", False, MAX_ROLLUP_ITEMS, bounded=True, derive=_den_tickets, headroom=1.5),
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


def contract_spec() -> dict:
    """Everything in this module that decides the SHAPE of a digest, as plain data.

    One input to `prompt.prompt_sha256`. Per-session caps cannot be the identity -- `caps_for`
    derives them from each log, so hashing them would give every session its own "prompt" --
    so this hashes the rule that derives them instead: each field's floor, whether it is
    bounded, its headroom and which denominator it reads, plus the clamp and the item length.
    A change to any of those changes what the model is asked for, and has to read as a new
    prompt in the run record rather than as the model drifting.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "fields": [
            {
                "name": f.name,
                "required": f.required,
                "cap": f.cap,
                "bounded": f.bounded,
                "derive": f.derive.__name__ if f.derive else None,
                "headroom": f.headroom,
            }
            for f in _LIST_FIELDS
        ],
        "max_derived_multiple": MAX_DERIVED_MULTIPLE,
        "max_item_chars": MAX_ITEM_CHARS,
    }


def caps_for(log: EventLog | None) -> dict[str, int]:
    """This session's caps: derived from its own event log where that is possible.

    A global chosen from a corpus snapshot decays as the corpus grows, silently, with no
    signal until a digest is lost — which is what happened. `MAX_ROLLUP_ITEMS` was set above
    an observed ceiling of 76 tickets and 38 event logs later the ceiling was 104, so session
    `6017c8ce`'s faithful 103-item response was discarded as invention (vikunja#901). The
    rollup is known *before* the model is called, so a bound read off the log in hand cannot
    go stale in that way.

    Only `bounded` fields are derived. `found`, `decisions` and `open_items` are free prose
    with no countable input, so there is nothing to derive from and they keep `MAX_ITEMS`.

    **`max(derived, cap)`, so the global becomes a FLOOR rather than being replaced.** Two
    things follow, and both are the point:

      * No session can be rejected that is not rejected today. The cap only ever moves up, so
        this build cannot introduce a loss. That is a property of the construction, not a
        measurement — see the note in `caps_for`'s test about why the corpus replay the build
        plan asked for cannot demonstrate it.
      * Small sessions behave exactly as they do now, which confines the change to the large
        ones that are the actual problem. Most of the extreme output/input ratios in the
        corpus occur at tiny denominators (`artifacts` reaches 42x against a rollup of ONE),
        and a ratio like that is meaningless to extrapolate from. Under the floor it is also
        harmless: 42 items sit far below the 100 floor either way.

    **The headroom is per field and must stay that way.** Measured 2026-09-18 over 479 written
    blocks paired with their own event logs, restricted to the band where the derived value
    actually governs — the pooled ratio over all denominators mixes two regimes and reads much
    worse than either:

    | field | denominator | max ratio, pooled | max ratio where derived > floor | headroom |
    |---|---|---|---|---|
    | `done` | `tool_events` | 1.00 (at 7) | 0.18 (den 200-500), 0.06 (den 500+) | 0.5 |
    | `tickets` | `rollup.tickets` | 5.00 (at 1) | 0.84 (den 100-200) | 1.5 |
    | `artifacts` | files+prs+git_refs | 42.00 (at 1) | 0.85 (den 50-100) | 2.0 |

    Each multiplier sits well above its band's measured ceiling, and the gap is deliberate:
    `json_schema` DECLARES the cap, so a cap merely *near* the ceiling does not reject — it
    makes the model shed, by a different amount each run. Measured on `aa6634a7` (58 tickets
    in the rollup): a declared 40 produced 27, 35 and 28 across three identical runs, while
    100 — 1.7x the rollup — produced 53 every time. 1.5x for `tickets` is chosen against that
    1.7x, not against the 0.84 ratio.

    `done` takes the *smallest* multiplier because its ratio collapses as sessions grow: the
    largest log in the corpus has 672 tool events and wrote 37 `done` items. At 0.5 that log's
    cap is 336 — 2.8x the worst ratio in its band, and still low enough that the `bounded`
    check has something to catch. A shared multiplier is the shape of this that fails: 1.5
    would have given that session a cap of 1008, at which point the check catches nothing.

    `artifacts` takes the largest because `_den_artifacts` undercounts by construction —
    `services` is asked for and has no rollup source.

    Finally the result is clamped to `f.cap * MAX_DERIVED_MULTIPLE`. The denominators come
    from a file on disk rather than from anything scribe computes, so without a ceiling a
    doctored event log removes the `bounded` guard outright instead of merely widening it.
    No log in the corpus comes within 6x of that clamp — see `MAX_DERIVED_MULTIPLE`.
    """
    caps = field_caps()
    if log is None:
        return caps
    for f in _LIST_FIELDS:
        if f.derive is None:
            continue
        derived = max(math.ceil(f.derive(log) * f.headroom), f.cap)
        # Clamped, never below the floor: `derived >= f.cap` above and the ceiling is a
        # multiple >= 1 of the same value, so the floor still governs a small session.
        caps[f.name] = min(derived, f.cap * MAX_DERIVED_MULTIPLE)
    return caps


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


def json_schema(caps: dict[str, int] | None = None) -> dict:
    """The JSON Schema sent to providers that support structured output.

    `maxItems` is **declared**, not merely enforced. A model cannot respect a limit it is never
    given: the prompt asked for "ticket references that appear in the log" with no limit stated
    anywhere, and the validator then rejected a response that did exactly that. Declaring the
    cap also puts the choice of what to drop with the model, which can pick the least relevant
    items — strictly better than a downstream truncation taking whatever landed past position N.

    The `maxItems` emitted here is the same number `parse` enforces, per field. That is the
    whole point: the contract the model is shown and the contract it is judged against must be
    the same one — which is why `caps` is a parameter rather than something each side computes
    for itself. `summarize_log` calls `caps_for` ONCE and hands the same dict to both.

    `caps=None` means the globals, which keeps every call site that has no log — and every
    bare test call — working unchanged.
    """
    caps = caps or field_caps()
    props: dict = {"asked": {"type": "string"}}
    for f in _LIST_FIELDS:
        props[f.name] = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": caps.get(f.name, f.cap),
        }
    return {
        "type": "object",
        "properties": props,
        "required": ["asked", "done"],
        "additionalProperties": False,
    }


def _as_list(value: object, f: _Field, cap: int) -> tuple[list[str], int]:
    """Coerce a field to a list of non-empty strings, or raise. Returns `(values, dropped)`.

    A string where a list belongs is accepted as a one-element list. That is a real and
    common model output, it is unambiguous, and rejecting it would burn a retry on something
    that carries the intended meaning perfectly well. Anything else is a violation.

    The cap is a backstop rather than the primary enforcement, since `json_schema` declares it
    — it fires when a provider ignores `maxItems`, not when the model was simply never told.
    What happens on overflow depends on `_Field.bounded`:

      * **bounded** — raise, not retryable. Over `caps_for(log)` the count really is more than
        this log can support, plus headroom, so this is the model inventing entries and the
        response should not be rendered.
      * **unbounded** — drop the excess and report how many. There is no threshold at which
        free prose becomes wrong, so there is nothing a rejection could be protecting; it
        would only convert a complete session into no session at all (vikunja#884).

    Items are dropped from the END, which is the one genuinely unattractive part of this. The
    model, having been shown the cap, is the better judge of what to shed — that is why
    declaring `maxItems` stays the primary mechanism and this is only the backstop.

    **This docstring used to justify the bounded raise with "the count cannot legitimately
    exceed the log", and against a GLOBAL cap that was false for two of the three fields.**
    Measured 2026-09-18 across 479 written blocks: `done` exceeded `rollup.commands` 45% of
    the time and `artifacts` exceeded its rollup union 45% of the time; only `tickets` held,
    and only within ~5x. The sentence described an intention the code did not implement — the
    global was never "the log", it was a number chosen from last month's corpus. It is true
    now because `cap` comes from `caps_for(log)`, and it is stated above in those terms
    deliberately: left as it was, it would re-justify exactly the naive derivation this build
    exists to remove.
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
    if len(out) <= cap:
        return out, 0
    if f.bounded:
        # Not retryable: the count follows from the log, so attempts two and three see the
        # same input and produce the same rejection. See the module docstring.
        raise SchemaError(
            f"field {f.name!r} has {len(out)} items, more than this session's {cap} cap",
            retryable=False,
        )
    return out[:cap], len(out) - cap


def parse(raw: str | dict, caps: dict[str, int] | None = None) -> Digest:
    """Validate a model response into a `Digest`, or raise `SchemaError`.

    Accepts a dict or a JSON string, including one wrapped in a ```json fence — models emit
    that often enough that failing on it would spend retries on presentation rather than
    content.

    `caps` must be the SAME dict `json_schema` was given for this call. Judging a response
    against a contract it was never shown is vikunja#849 exactly. `None` means the globals.
    """
    caps = caps or field_caps()
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
        values, dropped = _as_list(data.get(f.name), f, caps.get(f.name, f.cap))
        if f.required and not values:
            raise SchemaError(f"field {f.name!r} is required and must have at least one entry")
        setattr(digest, f.name, values)
        if dropped:
            digest.truncated[f.name] = dropped
    return digest
