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
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """A model response that does not satisfy the digest contract.

    `retryable` mirrors `ProviderError.retryable` and defaults to **True**: most violations are
    a bad sample and the next one is usually clean. It is set False only where the violation is
    a property of the input rather than of the sample, which today means exactly one case — a
    list longer than its cap, where every attempt sees the same log and produces the same
    over-long field.

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


#: Cap for the four free-text fields. `done`, `found`, `decisions` and `open_items` are model
#: prose with nothing behind them to bound them, so this is a genuine runaway guard: the event
#: log does not say how many entries are correct. `rollup.commands` reaches 187 with 104 of 432
#: sessions over 40, and that is the material `done` has to cover.
MAX_ITEMS = 40

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

#: `(field, required, cap)`. Only `asked` is required: a session with no findings and no
#: decisions is a real session, and forcing content into those fields is an invitation to
#: invent it. The cap is per-field because a single constant cannot guard both a field with a
#: rollup ceiling and one without — see `MAX_ITEMS` and `MAX_ROLLUP_ITEMS`.
_LIST_FIELDS: tuple[tuple[str, bool, int], ...] = (
    ("done", True, MAX_ITEMS),
    ("found", False, MAX_ITEMS),
    ("decisions", False, MAX_ITEMS),
    ("open_items", False, MAX_ITEMS),
    ("artifacts", False, MAX_ROLLUP_ITEMS),
    ("tickets", False, MAX_ROLLUP_ITEMS),
)


@dataclass
class Digest:
    """One session's summary, in the shape the renderer and the QC gate both expect."""

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
    for name, _required, cap in _LIST_FIELDS:
        props[name] = {"type": "array", "items": {"type": "string"}, "maxItems": cap}
    return {
        "type": "object",
        "properties": props,
        "required": ["asked", "done"],
        "additionalProperties": False,
    }


def _as_list(value: object, name: str, cap: int) -> list[str]:
    """Coerce a field to a list of non-empty strings, or raise.

    A string where a list belongs is accepted as a one-element list. That is a real and
    common model output, it is unambiguous, and rejecting it would burn a retry on something
    that carries the intended meaning perfectly well. Anything else is a violation.

    `cap` is the field's own limit, and since `json_schema` now declares it the check here is a
    backstop rather than the primary enforcement — it fires when a provider ignores `maxItems`,
    not when the model was simply never told.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if not isinstance(value, list):
        raise SchemaError(f"field {name!r} must be a list of strings, got {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if isinstance(item, (int, float, bool)):
            item = str(item)
        if not isinstance(item, str):
            raise SchemaError(f"field {name!r} contains a {type(item).__name__}, expected string")
        item = item.strip()
        if item:
            out.append(item[:MAX_ITEM_CHARS])
    if len(out) > cap:
        # Not retryable: the count follows from the log, so attempts two and three see the
        # same input and produce the same rejection. See the module docstring.
        raise SchemaError(
            f"field {name!r} has {len(out)} items, more than the {cap} cap", retryable=False
        )
    return out


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
    for name, required, cap in _LIST_FIELDS:
        values = _as_list(data.get(name), name, cap)
        if required and not values:
            raise SchemaError(f"field {name!r} is required and must have at least one entry")
        setattr(digest, name, values)
    return digest
