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
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """A model response that does not satisfy the digest contract."""


#: `(field, required)`. Only `asked` is required: a session with no findings and no decisions
#: is a real session, and forcing content into those fields is an invitation to invent it.
_LIST_FIELDS: tuple[tuple[str, bool], ...] = (
    ("done", True),
    ("found", False),
    ("decisions", False),
    ("open_items", False),
    ("artifacts", False),
    ("tickets", False),
)

MAX_ITEMS = 40
MAX_ITEM_CHARS = 2000


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
    """The JSON Schema sent to providers that support structured output."""
    props: dict = {"asked": {"type": "string"}}
    for name, _required in _LIST_FIELDS:
        props[name] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": props,
        "required": ["asked", "done"],
        "additionalProperties": False,
    }


def _as_list(value: object, name: str) -> list[str]:
    """Coerce a field to a list of non-empty strings, or raise.

    A string where a list belongs is accepted as a one-element list. That is a real and
    common model output, it is unambiguous, and rejecting it would burn a retry on something
    that carries the intended meaning perfectly well. Anything else is a violation.
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
    if len(out) > MAX_ITEMS:
        raise SchemaError(f"field {name!r} has {len(out)} items, more than the {MAX_ITEMS} cap")
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
    for name, required in _LIST_FIELDS:
        values = _as_list(data.get(name), name)
        if required and not values:
            raise SchemaError(f"field {name!r} is required and must have at least one entry")
        setattr(digest, name, values)
    return digest
