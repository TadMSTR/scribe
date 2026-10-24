"""Deterministic guard against a model reproducing template or skill text.

Ported from `host-forge-scripts/scripts/memsearch-summarize.py`. vikunja#248 asks for this
logic to be unified rather than triplicated, so **this copy is intended to be the shared
one** — when scribe cuts over, the other copies should import it or be deleted, not diverge.

Nothing here involves a model. The signals are: an unresolved `<...>` placeholder; bare
template residue the angle-bracket pattern misses (count letters, but not the `#N (id M)`
ticket notation; `YYYY-MM-DD` date templates); a known skill/template structural
signature; and several consecutive non-trivial lines reproduced verbatim from the source.
"""

from __future__ import annotations

import bisect
import re

_PLACEHOLDER_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9 _./-]{0,40}>")

#: Real words that must remain on a line once its placeholders are removed, before the line
#: counts as prose rather than scaffolding.
#:
#: The guard used to fire on `_PLACEHOLDER_RE.search(summary)` — **any** angle-bracketed word
#: anywhere in the digest. It was the only detector that ever fired in production: 51 fires,
#: 22 sessions suppressed, and on inspection every one was a session that had *talked about*
#: template syntax rather than reproduced it (vikunja#868). This fleet writes `<build-name>`,
#: `<agent>` and `<programme>` constantly, so a faithful digest of a session about naming
#: conventions was indistinguishable from a failed generation.
#:
#: Same shape as #848, where the QC classifier treated every backticked span as a command
#: needing a verbatim log match and 72 of 73 flags were false positives. The lesson there
#: applies unchanged: a guard keyed on surface syntax cannot tell content *about* a form from
#: content *in* that form — so key it on **position** instead.
#:
#: Scaffolding puts a placeholder where the *value* goes, which means the line around it is a
#: label and little else: `- <specific step>`, `**Goal:** <1 sentence from plan>`, `<agent>`.
#: Prose puts it inside a sentence: `Renamed <agent> to the real path`, `Wrote
#: <programme>-p<N>-<slug> directories`. Strip the placeholders and the difference is what is
#: left — nearly nothing, or a sentence.
#:
#: Three, not two or four. Two admits `**Asked:** <what the user wanted>` (two real tokens);
#: four rejects `Discussed the <build-name> convention` (three). Tuned against #868's own
#: table of real digest sentences, with `test_contamination.py` holding both directions.
_MIN_PROSE_WORDS = 3

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

#: The `_RESIDUE_RES` patterns whose `N`/`M` hits `_is_notation` may excuse. The date pattern
#: and `XX` are never excused: neither is part of the ticket notation, and neither has
#: produced a false positive.
_NOTATION_EXEMPT = frozenset({0, 1})

#: The fleet writes ticket references as `#N (id M)` (adopted 2026-10-21, fleet-wide via
#: vikunja#1190), and every agent's CLAUDE.md now explains it. A faithful digest of a session
#: *about* that convention has to say `#N`, `id M` and "N as an id", and `_RESIDUE_RES[0]`
#: read every one of them as an unfilled count letter. Two sessions were suppressed on all
#: nine attempts (vikunja#1216): the same defect as #868's placeholders, a guard keyed on
#: surface syntax that cannot tell content *about* a form from content *in* it. The fix is
#: the same too: key on **position**, and leave the letter patterns alone so `wrote N docs`
#: still fires.
#:
#: A letter is notation when it is written as a ticket ref, `#N` or `id M` / `(id M)`, or
#: when it sits inside inline code, `` `n/N` ``. Templates do not wrap their count letters
#: in backticks, so the cost of that exemption is a digest that keeps an `N`; the cost of
#: the false positive was a digest lost.
_REF_BEFORE_RE = re.compile(r"(?:#|\bid\s{1,8}\(?)$", re.IGNORECASE)
#: How far back from a hit `_REF_BEFORE_RE` looks: `id` + up to 8 spaces + `(` fits. Bounded
#: so the per-hit cost does not grow with the line.
_REF_WINDOW = 12
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")

#: A notation use earlier on the same line: `#N`, `id M`, or inline code holding a bare
#: `N`/`M`. A bare letter *after* one of these is excused too (option ii of the build plan's
#: C4/C5): in "`#M` as written, N as an id" the `N` names the notation the line just wrote,
#: and nothing about the letter itself can tell it from "wrote N docs". Only what comes
#: after counts, so "Closed N tickets" and "the N in #N" both still fire. A template line
#: that pairs a count letter with a literal `#N` ("Closed N tickets: #N") also still fires,
#: because its count comes first.
_NOTATION_ANCHOR_RE = re.compile(r"#[NM]\b|\bid\s+\(?[NM]\b|`[^`\n]*\b[NM]\b[^`\n]*`")

_MIN_OVERLAP_LINES = 3  # consecutive identical non-trivial lines that mean "copied"
_MIN_OVERLAP_LEN = 20  # ignore short/generic lines when comparing

RETRY_REMINDER = (
    "\n\nIMPORTANT: a previous attempt COPIED template/skill text verbatim. Do NOT "
    "reproduce any Markdown headers, numbered/phase step lists, or <...> placeholder "
    "tokens from the source. Also do NOT emit bare placeholder residue: use the ACTUAL "
    "numbers from the event log instead of count letters like N, M, or XX, and real dates "
    "instead of YYYY-MM-DD templates — if a value is unknown, omit it rather than leaving a "
    "placeholder. The ticket notation itself is not residue: if the session was about the "
    "`#N (id M)` citation form, you may write it, in backticks. Describe only what "
    "happened. If a skill or template was loaded, state only that it was loaded and for "
    "what purpose — never its contents."
)


#: The line that identifies a suppression block on disk. A constant for the same reason as
#: `render.PLACEHOLDER_MARKER`: `scribe recover` matches it against a corpus written before
#: provisional anchors existed.
SUPPRESSION_MARKER = (
    "- Summary suppressed: source contained template/skill text the summarizer could "
    "not describe without copying it (contamination guard)."
)


def _normalize_line(s: str) -> str:
    return s.strip().lstrip("-*# ").strip()


def _scaffold_line(line: str) -> bool:
    """True when `line` uses a placeholder as a *value slot* rather than mentioning one.

    Removing every placeholder leaves the line's real content behind. A template line is a
    label with the value cut out, so almost nothing remains; a sentence that happens to name
    a placeholder still reads as a sentence.

    Markdown furniture is stripped before counting — `-`, `*`, `#`, `>` and the bold/emphasis
    runs around a label — so `**Goal:** <1 sentence>` is judged on `Goal:`, which is what it
    actually says. Without that, a template line dressed in bullet syntax would count its own
    decoration as prose and slip through.
    """
    if not _PLACEHOLDER_RE.search(line):
        return False
    stripped = _PLACEHOLDER_RE.sub(" ", line)
    stripped = re.sub(r"[*_`#>~|\[\]()]+", " ", stripped)
    words = [w for w in re.split(r"[\s:;,.\-/]+", stripped) if w.strip()]
    return len(words) < _MIN_PROSE_WORDS


def _line_facts(text: str, start: int) -> tuple[list[int], list[int], int | None]:
    """Code-span starts and ends, and the end of the earliest notation use, for the line of
    `text` beginning at `start`. Computed once per line, never once per hit: a digest is
    model output, and a long line of excused hits went quadratic when every hit re-scanned
    its line (IV-47, 0.16.0 pre-audit)."""
    end = text.find("\n", start)
    end = len(text) if end == -1 else end
    spans = [(c.start(), c.end()) for c in _CODE_SPAN_RE.finditer(text, start, end)]
    anchor = _NOTATION_ANCHOR_RE.search(text, start, end)
    return [a for a, _ in spans], [b for _, b in spans], (anchor.end() if anchor else None)


def _is_notation(text: str, m: re.Match[str], newlines: list[int], facts: dict[int, tuple]) -> bool:
    """True when the residue hit `m` in `text` is the ticket notation, not a count letter.

    Judged on the hit's own line: a ref prefix right before it, an enclosing code span, or
    an earlier notation use on the line. See `_REF_BEFORE_RE` and `_NOTATION_ANCHOR_RE`.
    `newlines` holds every newline offset in `text`; `facts` caches `_line_facts` by line
    start. Both live for one scan.
    """
    pos = m.start()
    k = bisect.bisect_left(newlines, pos)
    start = newlines[k - 1] + 1 if k else 0
    if _REF_BEFORE_RE.search(text, max(start, pos - _REF_WINDOW), pos):
        return True
    if start not in facts:
        facts[start] = _line_facts(text, start)
    starts, ends, anchor_end = facts[start]
    i = bisect.bisect_left(starts, pos) - 1
    if i >= 0 and pos < ends[i]:
        return True
    return anchor_end is not None and anchor_end <= pos


def _has_residue(text: str) -> bool:
    """Whether `text` carries template residue. The detector and the fallback re-check
    (F-05) share this, so they cannot disagree about what residue is."""
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]
    facts: dict[int, tuple] = {}
    for i, res in enumerate(_RESIDUE_RES):
        for m in res.finditer(text):
            exemptable = i in _NOTATION_EXEMPT and "XX" not in m.group()
            if not (exemptable and _is_notation(text, m, newlines, facts)):
                return True
    return False


def detect_contamination(summary: str, raw: str) -> str | None:
    """Return a short reason if `summary` looks like copied template text, else None."""
    if not summary or not summary.strip():
        return "empty"
    if any(_scaffold_line(line) for line in summary.splitlines()):
        return "placeholder_token"
    if _has_residue(summary):
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

    The quoted line is itself re-checked against the placeholder, signature **and residue**
    detectors, and dropped if it trips any of them. Without that re-check the "safe" fallback
    could re-leak up to 200 characters of exactly the disallowed content — a MEDIUM audit
    finding on 2026-07-21.

    Three of the four detectors run, not two. Upstream ran only placeholder and signature;
    `_RESIDUE_RES` was added here (F-05, scribe-2026-09 audit) because upstream's omission of
    it carried no stated reason, and a residue token in the quoted line is the same class of
    template artefact the other two catch.

    The overlap detector is the one deliberate exclusion, and that is upstream's reasoning
    kept intact: a single quoted line cannot form the three-line window it needs, and it is
    drawn from `raw` by definition.
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
        or _has_residue(first_user)
    ):
        first_user = ""
    return f"- User turn: {first_user}\n{SUPPRESSION_MARKER}" if first_user else SUPPRESSION_MARKER
