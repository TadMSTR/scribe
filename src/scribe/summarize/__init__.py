"""Session summarization: providers, schema, contamination guard, rendering."""

from .contamination import build_fallback_note, detect_contamination
from .providers import Completion, Provider, ProviderError, build
from .render import render_digest, render_failure, render_suppressed
from .schema import Digest, SchemaError, json_schema, parse

__all__ = [
    "Completion",
    "Digest",
    "Provider",
    "ProviderError",
    "SchemaError",
    "build",
    "build_fallback_note",
    "detect_contamination",
    "json_schema",
    "parse",
    "render_digest",
    "render_failure",
    "render_suppressed",
]
