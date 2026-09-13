"""Deterministic guard against a model reproducing template or skill text.

Ported from `host-forge-scripts/scripts/memsearch-summarize.py`. vikunja#248 asks for this
logic to be unified rather than triplicated, so **this copy is intended to be the shared
one** — when scribe cuts over, the other copies should import it or be deleted, not diverge.

Nothing here involves a model. The signals are: an unresolved `<...>` placeholder; bare
template residue the angle-bracket pattern misses (count letters, `YYYY-MM-DD` date
templates); a known skill/template structural signature; and several consecutive non-trivial
lines reproduced verbatim from the source.
"""

from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9 _./-]{0,40}>")

# Structural signatures meaning the model reproduced source shape rather than describing it.
_TEMPLATE_SIGNATURES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?m)^\s*#{1,6}\s+Phase\s+\d"),
    re.compile(r"(?m)^\s*#{1,6}\s+Configuration\s*$"),
    re.compile(r"<specific step>"),
    re.compile(r"<Env vars"),
    re.compile(r"(?m)^\s*Base directory for this skill:"),
)

# Bare residue the angle-bracket regex misses: unfilled count letters (N, M), XX-style
# numeric placeholders, and unfilled date templates. Anchored to placeholder-like contexts so
# ordinary prose survives: a lone capital that is not N/M/XX ("option A", "plan B"), "N/A",
# and digit-attached forms like "500M" are all deliberately left alone.
_RESIDUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:[NM]|XX)\b(?=\s+[A-Za-z])"),
    re.compile(r"\b[NM]/[NM0-9]\b|\b\d+/[NM]\b"),
    re.compile(r"\bYYYY(?:-MM(?:-(?:DD|XX))?)?\b|\b\d{4}-\d{2}-XX\b"),
)

_MIN_OVERLAP_LINES = 3  # consecutive identical non-trivial lines that mean "copied"
_MIN_OVERLAP_LEN = 20  # ignore short/generic lines when comparing

RETRY_REMINDER = (
    "\n\nIMPORTANT: a previous attempt COPIED template/skill text verbatim. Do NOT "
    "reproduce any Markdown headers, numbered/phase step lists, or <...> placeholder "
    "tokens from the source. Also do NOT emit bare placeholder residue: use the ACTUAL "
    "numbers from the event log instead of count letters like N, M, or XX, and real dates "
    "instead of YYYY-MM-DD templates — if a value is unknown, omit it rather than leaving a "
    "placeholder. Describe only what happened. If a skill or template was loaded, state "
    "only that it was loaded and for what purpose — never its contents."
)


def _normalize_line(s: str) -> str:
    return s.strip().lstrip("-*# ").strip()


def detect_contamination(summary: str, raw: str) -> str | None:
    """Return a short reason if `summary` looks like copied template text, else None."""
    if not summary or not summary.strip():
        return "empty"
    if _PLACEHOLDER_RE.search(summary):
        return "placeholder_token"
    for res in _RESIDUE_RES:
        if res.search(summary):
            return "residue_token"
    for sig in _TEMPLATE_SIGNATURES:
        if sig.search(summary):
            return "template_signature"

    raw_lines = [
        n for n in (_normalize_line(x) for x in raw.splitlines()) if len(n) >= _MIN_OVERLAP_LEN
    ]
    sum_lines = [
        n for n in (_normalize_line(x) for x in summary.splitlines()) if len(n) >= _MIN_OVERLAP_LEN
    ]
    if len(sum_lines) >= _MIN_OVERLAP_LINES and raw_lines:
        raw_blob = "\n".join(raw_lines)
        for i in range(len(sum_lines) - _MIN_OVERLAP_LINES + 1):
            window = "\n".join(sum_lines[i : i + _MIN_OVERLAP_LINES])
            if window in raw_blob:
                return "verbatim_overlap"
    return None


def build_fallback_note(raw: str) -> str:
    """A minimal deterministic note for when the model keeps regurgitating template text.

    Emits no more source content than a normal summary would: the first real user line,
    truncated, plus a suppression marker.

    The quoted line is itself re-checked against the placeholder and signature detectors and
    dropped if it trips them. Without that, the "safe" fallback could re-leak up to 200
    characters of exactly the disallowed content — a MEDIUM audit finding on 2026-07-21. The
    overlap detector is skipped here on purpose: a single quoted line cannot form the
    three-line window it needs, and it is drawn from `raw` by definition.
    """
    first_user = ""
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("[User]:"):
            first_user = re.sub(r"\s+", " ", s[len("[User]:") :].strip())[:200]
            break
    if first_user and (
        _PLACEHOLDER_RE.search(first_user)
        or any(sig.search(first_user) for sig in _TEMPLATE_SIGNATURES)
    ):
        first_user = ""
    marker = (
        "- Summary suppressed: source contained template/skill text the summarizer could "
        "not describe without copying it (contamination guard)."
    )
    return f"- User turn: {first_user}\n{marker}" if first_user else marker
