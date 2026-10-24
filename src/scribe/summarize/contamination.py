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
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

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

#: A stable name for each `_RESIDUE_RES` entry, in the same order, recorded as
#: `Detection.rule`. Names rather than indices, so a reorder of the tuple cannot silently
#: relabel the run record's history.
_RESIDUE_RULES: tuple[str, ...] = ("count_letter", "ratio", "date")

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
#:
#: `id` is matched in lowercase only, the way the notation writes it, so `ID M tickets` still
#: fires (audit F-04).
# SECURITY[accepted]: `PR #N` and other `#`-prefixed template forms are excused with the
# ticket notation; on syntax alone they are the same token, and the guard is a quality
# control on model output, not a security boundary. Audit: 2026-10-24/scribe-residue-guard-2026-10.
_REF_BEFORE_RE = re.compile(r"(?:#|\bid\s{1,8}\(?)$")
#: How far back from a hit `_REF_BEFORE_RE` looks: `id` + up to 8 spaces + `(` fits. Bounded
#: so the per-hit cost does not grow with the line.
_REF_WINDOW = 12
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")

#: A notation use earlier on the same line: `#N`, `id M`, or inline code holding an `n/N`
#: ratio (`_NOTATION_SPAN_RE`). A bare `N`/`M` count letter (`_RESIDUE_RES[0]` only) *after*
#: one of these is excused too (option ii of the build plan's C4/C5): in "`#M` as written, N
#: as an id" the `N` names the notation the line just wrote, and nothing about the letter
#: itself can tell it from "wrote N docs". Only what comes after counts, so "Closed N
#: tickets" and "the N in #N" both still fire. A template line that pairs a count letter with
#: a literal `#N` ("Closed N tickets: #N") also still fires, because its count comes first.
#:
#: Narrowed after the audit (F-02): inline code that merely contains a standalone `N`/`M`
#: (`` `M-x compile` ``, `` `--max N` ``) is not an anchor, and an `N/M` pair after an
#: anchor still fires. The letter inside such a span is still excused by the span itself.
#: The span alternative was also taken out of this regex and checked against the spans
#: `_line_facts` already has: as an alternative here it backtracked quadratically on a line
#: with an unclosed backtick (F-01).
#:
#: A `#N` that a word labels (`PR #N`, `Step #N`) anchors nothing unless `(id` follows it: it
#: is excused itself (accepted, F-04), but it is a template form as often as it is the
#: notation, so it must not excuse the rest of its line too (audit R2-01).
_NOTATION_ANCHOR_RE = re.compile(r"(?<!\w)(?<!\w\s)#[NM]\b|#[NM](?=\s*\(id\b)|\bid\s{1,8}\(?[NM]\b")
_NOTATION_SPAN_RE = re.compile(r"\b[nm]/[NM]\b")

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
    anchor_end = anchor.end() if anchor else None
    for a, b in spans:
        if anchor_end is not None and b >= anchor_end:
            break
        if _NOTATION_SPAN_RE.search(text, a, b):
            anchor_end = b
            break
    return [a for a, _ in spans], [b for _, b in spans], anchor_end


def _is_notation(
    text: str, m: re.Match[str], newlines: list[int], facts: dict[int, tuple], *, anchored: bool
) -> bool:
    """True when the residue hit `m` in `text` is the ticket notation, not a count letter.

    Judged on the hit's own line: a ref prefix right before it, an enclosing code span, or
    an earlier notation use on the line. See `_REF_BEFORE_RE`, `_NOTATION_ANCHOR_RE` and
    `_NOTATION_SPAN_RE`.
    `newlines` holds every newline offset in `text`; `facts` caches `_line_facts` by line
    start. Both live for one scan. `anchored` allows the earlier-notation rule, which only
    a bare count letter gets (F-02).
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
    return anchored and anchor_end is not None and anchor_end <= pos


def _residue_hits(text: str) -> Iterator[tuple[re.Match[str], str]]:
    """Every unexcused residue hit in `text`, with the name of the rule that made it, pattern
    by pattern in `_RESIDUE_RES` order."""
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]
    facts: dict[int, tuple] = {}
    for i, res in enumerate(_RESIDUE_RES):
        for m in res.finditer(text):
            exemptable = i in _NOTATION_EXEMPT and "XX" not in m.group()
            if not (exemptable and _is_notation(text, m, newlines, facts, anchored=i == 0)):
                yield m, _RESIDUE_RULES[i]


def _first_residue(text: str) -> tuple[re.Match[str], str] | None:
    """The earliest unexcused hit in `text` and its rule, or None; the longer match wins a
    tie. By position rather than by pattern order, so `N/M` reports as the `ratio` it is and
    not as the `M` that `count_letter` also sees in it."""
    return min(_residue_hits(text), key=lambda h: (h[0].start(), -len(h[0].group())), default=None)


def _has_residue(text: str) -> bool:
    """Whether `text` carries template residue. The detector, the fallback re-check (F-05)
    and the per-item degrade in `runner` share `_residue_hits`, so they cannot disagree about
    what residue is. Stops at the first hit."""
    return next(_residue_hits(text), None) is not None


#: Longest excerpt `Detection` carries. Enough to see what the model wrote around the token,
#: short enough that the run record holds a quote rather than a paragraph.
EXCERPT_CHARS = 80


@dataclass(frozen=True)
class Detection:
    """What the guard matched, not only that it matched (vikunja#1216).

    Before 0.17.0 the rejected render was thrown away, and three investigations of this
    detector started from a guess at what the model had written. `excerpt` is that answer.

    **`excerpt` and `source` are unredacted model output** derived from the transcript.
    Neither may be stored as they are: `runner` stores `redacted_excerpt(...)` instead, and
    puts neither on a span nor in `Outcome.errors`. Both are excluded from `repr`, so a debug
    print of a detection does not become the leak.
    """

    #: The same strings `detect_contamination` returns.
    signal: str
    #: The matched text (`N`, `n/N`, `YYYY-MM-DD`). "" for signals that are not one token.
    token: str = ""
    #: Which pattern fired: a `_RESIDUE_RULES` name for `residue_token`, else "".
    rule: str = ""
    #: The hit's line, whitespace-collapsed, cut to `EXCERPT_CHARS` around the hit.
    excerpt: str = field(default="", repr=False)
    #: The whole text the detection was made on, for `redacted_excerpt`. In memory only.
    source: str = field(default="", repr=False)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _window(line: str, start: int, end: int) -> str:
    """`line` cut to at most `EXCERPT_CHARS`, centred on `[start, end)`, in whole words.

    Whole words, so a cut never leaves a fragment of a longer string: a secret-shaped word is
    either in the excerpt complete, where the redactor can see it, or not at all. A hit whose
    own word is longer than the window is reported as the matched text alone.
    """
    if len(line) <= EXCERPT_CHARS:
        return line
    lo = max(0, (start + end) // 2 - EXCERPT_CHARS // 2)
    hi = min(len(line), lo + EXCERPT_CHARS)
    lo = max(0, hi - EXCERPT_CHARS)
    # Move each cut inwards to a word boundary unless it already sits on one.
    if lo > 0 and line[lo - 1] != " ":
        sp = line.find(" ", lo)
        lo = len(line) if sp == -1 else sp + 1
    if hi < len(line) and line[hi] != " ":
        sp = line.rfind(" ", 0, hi)
        hi = 0 if sp == -1 else sp
    if lo > start or hi < end:
        return line[start:end][:EXCERPT_CHARS]
    return line[lo:hi].strip()


def _residue_detection(text: str) -> Detection | None:
    hit = _first_residue(text)
    if hit is None:
        return None
    m, rule = hit
    # The hit's own line, collapsed, with the hit's offsets carried into the collapsed form.
    ls = text.rfind("\n", 0, m.start()) + 1
    le = text.find("\n", m.end())
    le = len(text) if le == -1 else le
    before = _collapse(text[ls : m.start()])
    after = _collapse(text[m.end() : le])
    lead = before + " " if before and text[m.start() - 1].isspace() else before
    trail = " " + after if after and text[m.end()].isspace() else after
    line = lead + m.group() + trail
    start = len(lead)
    return Detection(
        signal="residue_token",
        token=m.group(),
        rule=rule,
        excerpt=_window(line, start, start + len(m.group())),
        source=text,
    )


def redacted_excerpt(detection: Detection, scrub: Callable[[str], str]) -> str:
    """An excerpt taken from the detection's WHOLE source text after `scrub` has run over it.

    Scrubbing the 80-character window instead would let the cut split a secret, or drop the
    key that tells the redactor the value next to it is one (`password=...`). Scrubbing only
    the hit's line is not enough either: the redactor has rules that span lines, such as a
    PEM block whose BEGIN is in one item and whose body is in the next. The post-render
    guard scrubs the whole digest, so this does too (pre-audit F-1). The first residue hit is
    then found again in what is left, and the window is taken from that.

    If no residue is left, the hit was inside something the redactor removed. The result is
    then "", because there is nothing safe left to centre on. The window is scrubbed once
    more on the way out; scrubbing is idempotent, so that can only remove, never restore.
    """
    if not detection.source:
        return ""
    found = _residue_detection(scrub(detection.source))
    return scrub(found.excerpt) if found is not None else ""


def explain_contamination(summary: str, raw: str) -> Detection | None:
    """`detect_contamination`, with what it matched. Same order, same signals, same `None`."""
    if not summary or not summary.strip():
        return Detection(signal="empty")
    if any(_scaffold_line(line) for line in summary.splitlines()):
        return Detection(signal="placeholder_token")
    residue = _residue_detection(summary)
    if residue is not None:
        return residue
    for sig in _TEMPLATE_SIGNATURES:
        if sig.search(summary):
            return Detection(signal="template_signature")

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
                return Detection(signal="verbatim_overlap")
    return None


def detect_contamination(summary: str, raw: str) -> str | None:
    """Return a short reason if `summary` looks like copied template text, else None.

    A thin wrapper over `explain_contamination`, kept because `build_fallback_note`, the tests
    and the deploy replay one-liners call it, and its behaviour must not change."""
    d = explain_contamination(summary, raw)
    return d.signal if d else None


def echoes_source(summary: str, raw: str) -> bool:
    """Whether `summary` has `_MIN_OVERLAP_LINES` consecutive non-trivial lines that each
    appear verbatim in `raw`. Used before the per-item degrade (audit F-01,
    scribe-residue-degrade-2026-10).

    `verbatim_overlap` cannot stand in for this. It looks for a three-line window inside a
    line-structured `raw`, and scribe's corpus is `grounding_text()`, one JSON line, so it
    never fires (vikunja#1217). And even with a line corpus, trimming one item out of a copied
    run breaks the run it needs. So this tests line by line, on the UNTRIMMED digest, and
    accepts either the line as written or its JSON-escaped form, which is how it sits in the
    one-line corpus.

    It gates only the degrade. A digest that fails it is suppressed exactly as 0.16.0 would
    have suppressed it, so this cannot cost a session that 0.16.0 wrote.
    """
    run = 0
    for line in summary.splitlines():
        n = _normalize_line(line)
        if len(n) < _MIN_OVERLAP_LEN:
            continue
        if n in raw or json.dumps(n, ensure_ascii=False)[1:-1] in raw:
            run += 1
            if run >= _MIN_OVERLAP_LINES:
                return True
        else:
            run = 0
    return False


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
