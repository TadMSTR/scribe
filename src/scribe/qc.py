"""Quality gates that can actually fail.

The gate this replaces, `memory-compact-qc.sh`, has run weekly for months and could never
have caught the defect that motivated this whole component. It grades `compact` output for
fidelity to *its source*, and its source is the already-starved memory file. A faithful
summary of an impoverished input scores PASS. The grader was working; it was pointed at the
wrong artefact.

So every check here asserts against the **event log**, which is derived from the transcript,
never against another summary.

Three deterministic checks, no LLM:

1. **Coverage** — a session with a transcript in the window has a digest, and that digest is
   newer than the transcript's last write.
2. **Groundedness** — every file path, command, ticket and tool name the digest names appears
   in the event log. This is what makes hallucination detectable rather than a matter of
   taste.
3. **Event-coverage floor** — a digest referencing fewer than N% of the session's non-trivial
   events FAILS. Without a floor, an extractor regression that silently drops events reads as
   a clean pass: the digest stays perfectly grounded in a log that has lost most of its
   content, and checks 1 and 2 both go green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .extract.models import EventLog
from .summarize.render import TRUNCATION_RE
from .writeback import ANCHOR_RE, TERMINATOR_RE

#: Below this share of the session's non-trivial events, a digest is a FAIL rather than a
#: terse success. Deliberately low: the floor exists to catch an extractor that has stopped
#: extracting, not to mandate a verbose summary.
DEFAULT_COVERAGE_FLOOR = 0.10

#: A file-path claim. The last character is constrained SEPARATELY from the body, and that
#: is the whole point: `.` is both a legal path character and the character an English
#: sentence ends with, so a body-only class captures the terminator as part of the path.
#: Observed live 2026-09-16 -- a digest truthfully citing `.../request.md` was graded against
#: `.../request.md.`, which appears in no event log, so a TRUE claim was recorded as
#: ungrounded (vikunja#874). Two thirds of real groundedness findings were this artifact.
#:
#: A bare `rstrip(".")` is the obvious fix and is wrong on its own: `/repo/src/..` and
#: `../../..` are real paths whose last character is a dot. The distinction that holds is
#: POSITIONAL -- trailing dots are legitimate only as a complete final segment, i.e. when
#: preceded by `/`. Prefixes (`./`, `../`) were never at risk; they are matched before the
#: body. A dotted extension is not at risk either; its last character is a word character.
#: The `{2,}` body plus one final character preserves the original 3-character floor.
_PATH_RE = re.compile(
    r"""
    (?:(?<=\s)|^)              # a claim starts a token, never mid-word
    (?:~|\.{0,2})/             # ~/ | / | ./ | ../
    [\w./~-]{2,}               # the body, where a dot is an ordinary path character
    (?:[\w~/-]|(?<=/)\.{1,2}) # the last: not a bare dot, unless it IS the final segment
    """,
    re.VERBOSE,
)
_TICKET_RE = re.compile(r"(?<![/\w\d])#([1-9]\d{0,5})\b")
# Tool claims are split by ambiguity, because several tool names are also ordinary English.
# A digest saying "Read the logs" or "Task complete" is not naming a tool, and flagging it
# would fail a true claim -- a gate that cries wolf is one that gets switched off. So the
# ambiguous names count only when marked as code or prefixed, while the distinctive ones
# count wherever they appear.
_TOOL_AMBIGUOUS = ("Read", "Write", "Edit", "Task", "Skill", "Agent")
_TOOL_DISTINCT = (
    "WebFetch",
    "WebSearch",
    "ToolSearch",
    "NotebookEdit",
    "NotebookRead",
    "AskUserQuestion",
    "MultiEdit",
    "Bash",
    "Glob",
    "Grep",
)
_TOOL_RE = re.compile(
    r"\bmcp__[\w-]+\b"
    r"|`(?:" + "|".join(_TOOL_AMBIGUOUS) + r")`"
    r"|\b(?:" + "|".join(_TOOL_DISTINCT) + r")\b"
)
#: A backticked span. The length bounds are applied to the CAPTURED CONTENT in `_claims`, not
#: baked into the pattern, and that separation is load-bearing rather than stylistic.
#:
#: This read `` r"`([^`\n]{3,200})`" `` and silently mis-paired its own delimiters. A span
#: shorter than three characters cannot satisfy `{3,200}`, so the match starting at its opening
#: backtick fails; the scan then resumes from that span's CLOSING backtick and pairs it with the
#: NEXT span's OPENING one, capturing the ordinary prose in between. `` `ps` showing `nats pub
#: ...` `` yields the claim `showing`; `` (`/`) to `/dashboard` `` yields `) to`; `` `#N` refs in
#: `tasks_bulk_update` `` yields `refs in`. Measured over the live corpus 2026-09-19: 56
#: fabricated spans across 36 blocks, producing 22 of the 65 non-path groundedness findings.
#:
#: It fails in both directions at once, which is why the rate alone never showed it. The
#: fabricated span is graded against the event log and reported as a hallucinated identifier --
#: a claim no model ever asserted, the same category error `strip_scribe_markers` exists to
#: prevent -- while the 30 REAL spans it swallowed were never graded at all. A short span is not
#: rare in a digest: `ps`, `uv`, `id`, `/`, `v1`, `#N` are exactly the tokens prose backticks.
#:
#: Matching `[^`\n]*` pairs delimiters the way a reader does, and the floor then does the job it
#: was written for -- keeping a joining word from carrying a claim -- on the content rather than
#: on the pairing.
_CMD_RE = re.compile(r"`([^`\n]*)`")
#: A backticked span shorter than this carries no claim: `in`, `to`, `of`. Unchanged in value
#: from the bound that used to live in `_CMD_RE`; only where it applies has changed.
MIN_SPAN_CHARS = 3
#: And one longer than this is a code block quoted inline, not a claim about a single thing.
MAX_SPAN_CHARS = 200

#: Claim classes for a backticked span. The split IS vikunja#848: the gate used to call every
#: one of these a `command` and demand a match in `rollup.commands`, so the ordinary
#: vocabulary of a good digest -- statuses, model names, config keys, field names -- was
#: reported as a fabricated command. On the first live run that was 70 of 73 findings, and
#: `groundedness_rate` read 0.0 for five digests that were materially better than the
#: incumbent's.
CLAIM_COMMAND = "command"
CLAIM_CODE = "code"
CLAIM_IDENTIFIER = "identifier"
#: The three claim classes that are not backticked spans. Named alongside the others because
#: every finding now carries its kind as data -- see `Finding.kind`.
CLAIM_PATH = "path"
CLAIM_TOOL = "tool"
CLAIM_TICKET = "ticket"
#: Every kind a groundedness finding can carry, in the order `check_groundedness` tests them.
CLAIM_KINDS = (
    CLAIM_PATH,
    CLAIM_TOOL,
    CLAIM_TICKET,
    CLAIM_COMMAND,
    CLAIM_CODE,
    CLAIM_IDENTIFIER,
)

_SHELL_META = ("|", "&&", "||", ">", "<", ";", "$(")
_CODE_CHARS = frozenset("()[]{}=")
#: Deliberately short. It exists to catch the common invocations whose first token carries no
#: other syntactic signal (`systemctl restart nginx` has no flag and no operator); anything
#: with a flag or an operator is classified on shape and needs no entry here. A list that
#: tried to be exhaustive would rot, and a miss is not a failure -- it downgrades a claim to
#: `code` or `identifier`, which is checked against the same corpus by a different route.
_BINARIES = frozenset(
    [
        "apt",
        "awk",
        "bash",
        "cat",
        "chmod",
        "chown",
        "cp",
        "curl",
        "df",
        "docker",
        "du",
        "echo",
        "find",
        "git",
        "grep",
        "head",
        "jq",
        "kill",
        "ln",
        "ls",
        "make",
        "mkdir",
        "mv",
        "node",
        "npm",
        "pip",
        "pkill",
        "pm2",
        "pnpm",
        "python",
        "python3",
        "pytest",
        "rg",
        "rm",
        "rsync",
        "ruff",
        "sed",
        "sort",
        "ssh",
        "sudo",
        "systemctl",
        "tail",
        "tar",
        "uv",
        "wc",
    ]
)
#: 3+ characters, so that joining words (`in`, `to`, `of`) never carry a claim on their own.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{2,}|\d{2,}")
#: `417` written as an internal id rather than as a `#` identifier. See `_ticket_detail`.
#:
#: **This selects a MESSAGE, never a verdict.** Every branch of `_ticket_detail` returns a
#: finding; the only question is whether it names the conflation or says "not in the event log".
#: Widening it therefore cannot change the failure rate, and the rate was measured unchanged
#: either side of this edit.
#:
#: It was too narrow to earn its keep. `\bid` requires a word boundary before `id`, and the
#: form the corpus actually holds is `task_id=541` -- where `id` is preceded by an underscore,
#: which is a word character, so the boundary fails. Measured over the live corpus 2026-09-19:
#: **31 of 62 ticket findings** were true id conflations that fell through to the generic
#: message, against 30 the pattern caught. The two commonest missed forms are the MCP argument
#: digest (`task_id=541`, `"target": "930"`) and the id/identifier tables agents write into
#: their own notes (`| **378 / #359** |`).
#:
#: Verified against the live tracker rather than inferred: id 541 is identifier #493, and id
#: 930 is #847. A digest writing `#541` for id 541 names a real but unrelated ticket, which is
#: the whole reason this message exists.
#: **Every alternative starts with a literal and carries no unbounded quantifier.** That is a
#: correctness requirement here, not style. The first draft opened with `\w*_id`, and `\w*` is
#: re-tried at every start position: measured quadratic -- 0.02s at 4k characters, 0.39s at
#: 16k, 13.8s at 100k, and **232 seconds** against this corpus's longest event log at 414,187
#: characters. The live corpus hid it because event logs are JSON, where runs of word
#: characters are short; one session logging a base64 blob or a minified file would have hung
#: the gate outright.
#:
#: Note where this was found: the same module vikunja#889 had just capped a scan in. A pattern
#: is as able to be quadratic as a loop, and only the loop had been looked at.
_ID_CONTEXT = (
    r'(?:"\w{0,32}id"\s*:\s*"?'  # {"id": 417}, {"task_id": "417"}
    r"|\bid\s*[:=]?\s*"  # id 417, id: 417, id=417
    r'|_id\s*[:=]\s*"?'  # task_id=417  <- the commonest miss
    r'|"target"\s*:\s*"'  # {"target": "417"} in an MCP event's args digest
    r"|/)"  # .../tasks/417
)


def classify_span(span: str) -> str:
    """Which kind of claim a backticked span makes: command, code, or identifier.

    Shape, not backticks. A digest writes `parked`, `mistral-small-latest` and
    `credentials: source: env` because those are the words for what happened; none is a
    command, and demanding each appear in the command log fails a true claim. A gate that
    cries wolf 70 times out of 73 is one that gets switched off.
    """
    text = span.strip()
    if not text:
        return CLAIM_IDENTIFIER
    tokens = text.split()
    head = tokens[0]
    if any(meta in text for meta in _SHELL_META):
        return CLAIM_COMMAND
    if head in _BINARIES:
        return CLAIM_COMMAND
    if len(tokens) > 1 and tokens[1].startswith("-") and head.replace("-", "_").isidentifier():
        return CLAIM_COMMAND
    if _CODE_CHARS & set(text):
        return CLAIM_CODE
    return CLAIM_IDENTIFIER


@dataclass
class Finding:
    """One thing wrong with a digest, and -- for a groundedness finding -- what it was about.

    `kind` and `claim` are **data the gate already had** at the moment it rejected something,
    recorded rather than re-derived. Before they existed the only record of either was the
    English prose in `detail`, and `qc_survey` recovered path claims by matching a shared
    prefix constant and `literal_eval`-ing the tail.

    That worked for exactly one route and could not be extended to the others, which is what
    this build ran into first. `_ticket_detail` has two message shapes and the more
    interesting one -- the id-vs-identifier conflation, **30 of 62 ticket findings on the live
    corpus** -- puts the claim in the *middle* of a sentence:

        ticket '#480' is not in the event log, which contains '480' only as an internal id ...

    A prefix constant cannot recover that. Extending the instrument by prefix-matching would
    have meant a regex over prose for every route, kept in sync by hand with the sentences the
    gate happens to write -- a second implementation of the gate's own classification, which is
    precisely the drift `qc_survey.rejected_paths`' docstring exists to forbid.

    So the survey still reads verdicts "back out of the gate's own output rather than
    recomputed"; it just reads a field instead of parsing a sentence. `detail` is unchanged and
    stays the human-readable line -- these are additive, and `Report.to_dict` gains two keys
    rather than changing any.
    """

    check: str
    detail: str
    #: Which claim class this is about: one of `CLAIM_KINDS`. Empty for non-groundedness
    #: findings, which are not about a claim at all.
    kind: str = ""
    #: The claim itself, exactly as the gate tested it -- already normalized and lowercased.
    claim: str = ""


@dataclass
class Report:
    """The verdict, plus every claim that could not be grounded."""

    session_id: str = ""
    transcript_path: str = ""
    findings: list[Finding] = field(default_factory=list)
    events_total: int = 0
    events_referenced: int = 0
    coverage: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, check: str, detail: str, *, kind: str = "", claim: str = "") -> None:
        self.findings.append(Finding(check, detail, kind=kind, claim=claim))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "events_total": self.events_total,
            "events_referenced": self.events_referenced,
            "coverage": round(self.coverage, 4),
            "findings": [
                {"check": f.check, "detail": f.detail, "kind": f.kind, "claim": f.claim}
                for f in self.findings
            ],
        }


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def grounding_terms(log: EventLog) -> dict[str, set[str]]:
    """Everything the digest is permitted to assert, by category.

    Built from the event log rather than the transcript on purpose: the log is what the model
    was shown, so a claim grounded in the transcript but absent from the log is still a claim
    the model could not have known — and would be a bug in the extractor's budget, worth
    surfacing rather than excusing.
    """
    r = log.rollup
    paths = {p.lower() for p in (*r.files_read, *r.files_written)}
    commands = {_norm(c) for c in r.commands}
    tools: set[str] = set()
    for turn in log.turns:
        for ev in turn.events:
            tools.add(ev.tool.lower())
            if ev.kind in ("file_read", "file_write") and ev.target:
                paths.add(ev.target.lower())
    return {
        "paths": paths,
        "commands": commands,
        "tools": tools,
        "tickets": {t.lower() for t in (*r.tickets, *r.prs)},
    }


def strip_scribe_markers(text: str) -> str:
    """Remove scribe's own block markers before anything is read as a claim.

    **A claim is something the model asserted.** The anchor and the terminator are written by
    `writeback`, not by any summarizer, so grading them as model output is a category error —
    and it is not a harmless one. The terminator is literally `<!-- /scribe turn:... -->`, and
    `_PATH_RE` reads `/scribe` out of it as a file path that is in no event log. Every digest
    file scribe writes therefore carries a guaranteed groundedness finding, which made
    `scribe qc --digest <a file scribe wrote>` structurally incapable of passing.

    That went unseen because the pipeline grades the rendered block *before* the markers are
    attached, while the CLI grades the file *after* — the two graded different documents, and
    only the CLI's is the one a human re-checking a digest later actually has.

    Only the known markers are removed, not all HTML comments. Stripping every comment
    would also silence a hallucinated path a model happened to put inside one, which is a
    hole this does not need to open.

    `TRUNCATION_RE` is here on the same argument and not as an afterthought: the truncation
    note is written by `render_digest` when an unbounded field overflows (vikunja#884), so it
    is scribe's prose, not the model's. Grading it would attach a guaranteed finding to
    exactly the digests this build exists to stop losing.
    """
    return TRUNCATION_RE.sub(" ", TERMINATOR_RE.sub(" ", ANCHOR_RE.sub(" ", text)))


def _claims(text: str) -> dict[str, set[str]]:
    """Concrete, checkable assertions in a digest, keyed by claim class.

    Backticked spans are split three ways by `classify_span` rather than all being called
    commands -- see vikunja#848.
    """
    text = strip_scribe_markers(text)
    out: dict[str, set[str]] = {
        "paths": {m.group(0).strip().lower() for m in _PATH_RE.finditer(text)},
        "tools": {m.group(0).strip("`").lower() for m in _TOOL_RE.finditer(text)},
        "tickets": {f"#{m.group(1)}".lower() for m in _TICKET_RE.finditer(text)},
        CLAIM_COMMAND: set(),
        CLAIM_CODE: set(),
        CLAIM_IDENTIFIER: set(),
    }
    for m in _CMD_RE.finditer(text):
        span = m.group(1)
        if not MIN_SPAN_CHARS <= len(span) <= MAX_SPAN_CHARS:
            continue
        out[classify_span(span)].add(_norm(span))
    return out


def _in_category(claim: str, allowed: set[str]) -> bool:
    """Bidirectional substring containment against a derived category set.

    A digest may legitimately name `src/scribe/parser.py` where the log holds the absolute
    path, and may quote the head of a long command. Requiring equality would fail true claims.
    """
    if claim in allowed:
        return True
    return any(claim in term or term in claim for term in allowed)


#: Prefix of a path-claim groundedness finding.
#:
#: It no longer carries the recovery — `Finding.claim` does, because the prefix trick could not
#: be extended to the routes that needed it next. It is still a constant rather than an inline
#: f-string, and `qc_survey` still asserts against it, because the *wording* of this finding is
#: read by humans triaging a digest and a silent rewording is the kind of drift that made the
#: two pre-build measurements of vikunja#876 disagree 16-fold.
PATH_UNGROUNDED = "path not in the event log: "


#: A composition's directory half must name a real directory, not the filesystem root. Two
#: segments is the shortest thing that does. At one, every absolute path on the machine
#: composes out of `/home` plus a tail, which is not evidence of anything.
MIN_PREFIX_SEGMENTS = 2
#: And its relative half must be more than a bare filename -- see `composition_split`. Both
#: floors are derived from `qc_survey.cross_session_probe`, which measures how often each
#: candidate grounds a path harvested from a *different* session's digest — a claim the model
#: here demonstrably was not shown. Re-measured 2026-09-19 over the live corpus (494 blocks),
#: with 19,495 such controls against the corpus's own 899 failing claims:
#:
#:     prefix  tail   falsely grounded   truly grounded
#:          2     1     262   (1.3%)       790  (87.9%)
#:          2     2      86   (0.4%)       562  (62.5%)
#:          2     3      49   (0.2%)       323  (35.9%)
#:          1     2      98   (0.5%)       563  (62.6%)
#:          3     2      61   (0.3%)       528  (58.7%)
#:
#: `min_tail=2` is where the false-ground rate collapses — 3.4x lower than at 1 — and the step
#: after it buys almost nothing for another 27 points of true claims. A tail of one segment is
#: a bare filename, and `changelog.md` or `agents.md` appears in nearly every log, so at 1 any
#: directory the log names pairs with any common filename it names. `min_prefix=2` strictly
#: dominates 1 (lower false-ground at the same true-ground), and 3 buys 0.1% for 3.7 points.
#: Neither floor was chosen against the corpus it judges. Re-derive with
#: `python -m scribe.qc_survey --probe`; the corpus is live, so the figures drift slightly.
#:
#: The shape is unchanged from the 2026-09-17 snapshot at 470 blocks and every conclusion above
#: still holds -- which is the point of re-stating it rather than trusting it. Each absolute
#: count moved, and a table that reads as measured has to have been.
MIN_TAIL_SEGMENTS = 2


def path_segments(claim: str) -> list[str]:
    """A path claim's non-empty segments. `~` is one of them, and that is deliberate."""
    return [s for s in claim.split("/") if s]


#: How close a directory and a **single-segment** tail must sit in the corpus to count as one
#: composition, in characters. Same mechanism and same measured value as
#: `CO_OCCURRENCE_WINDOW`, for the same reason: presence alone is too weak once the tail is a
#: bare filename, but presence *together* is not.
#:
#: The bare filename case is the corpus's largest remaining shape by far -- a repository
#: directory the log names plus `changelog.md`, `agents.md`, `pyproject.toml`. Admitting it on
#: presence alone means any directory pairs with any common filename, which the probe prices at
#: 1.3% false-ground for 224 true claims recovered. Requiring adjacency instead recovers 107
#: for 25, re-measured 2026-09-19 over 494 blocks — 337 own claims and 19,409 controls that
#: composition alone still fails:
#:
#:     window   falsely grounded   truly grounded   true per false
#:         40      12   (0.06%)      48  (14.2%)             4.0
#:         80      20   (0.10%)      98  (29.1%)             4.9
#:        120      25   (0.13%)     107  (31.8%)             4.3
#:        200      40   (0.21%)     114  (33.8%)             2.9
#:        400      51   (0.26%)     133  (39.5%)             2.6
#:
#: 120 is 4.3 true claims per false one against the blanket rule's 1.3, and widening to 200
#: buys 7 more true claims for 15 more false. It is not coincidence that this lands on the same
#: number as `CO_OCCURRENCE_WINDOW` -- it is the same corpus and the same question.
#:
#: **80 has the better pooled ratio here, and 120 is kept anyway — on the margin, not the
#: pool.** The step from 80 to 120 buys 9 true claims for 5 false, which is 1.8 and still above
#: the 1.3 this gate already declined as too weak; 80 scores higher only because it averages in
#: the cheap first 80 characters, where the evidence is densest. That was equally true of the
#: 2026-09-17 snapshot (80 scored 6.8 against 120's 5.0) and 120 was chosen then for the same
#: reason. Recorded because the ranking invites the opposite conclusion at a glance.
ADJACENCY_WINDOW = 120


#: How much of the corpus `_adjacent` will scan for one claim, in characters. `0` disables the
#: cap. vikunja#889: `_adjacent` walks every occurrence of `prefix`, and `composition_split`
#: calls it for every cut point of every path claim, so the work is quadratic in corpus size
#: crossed with a claim's segment count.
#:
#: **The cap is on LENGTH, not on occurrences, and the difference is the whole finding.** An
#: iteration cap -- "bail after N hits of `prefix`" -- makes the verdict depend on where in the
#: log the evidence happens to sit: a true composition whose directory is named once, late,
#: after N earlier mentions, starts failing. That is a NEW false positive in a gate built to
#: remove them, which is why #889 was deferred rather than patched in one line. A length cap
#: truncates the same prefix of the same corpus on every run, so the verdict is a function of
#: the corpus and the claim alone -- order-independent, and reproducible.
#:
#: It is not free, and the cost is stated rather than hidden: evidence sitting beyond the cap
#: is not seen. So the value is DERIVED, by the same method as the floors above -- sweep it and
#: keep the tightest one that costs nothing. Measured 2026-09-19 over the live corpus, 494
#: blocks, against the uncapped rule:
#:
#:     cap          path claims rejected   verdict changes vs uncapped
#:     uncapped                     162       -
#:     1,048,576                    162       0
#:       524,288                    162       0
#:       262,144                    162       0
#:       131,072                    168       6
#:        65,536                    187      25
#:
#: 262,144 is the tightest cap that changes no verdict; below it the cap starts failing true
#: compositions, 6 of them at half the value and 25 at a quarter.
#:
#: Note what it does NOT say. 11 of the 494 blocks (2.2%) have a corpus longer than this --
#: `grounding_text()` runs to 414,187 characters at the longest, 197,432 at p95, 68,082 at the
#: median -- so the cap really does truncate on real logs, and the verdicts are identical
#: anyway. A cap "above the largest corpus" would have been 524,288 and would have bounded
#: nothing that matters; this one bites and is still free, which is the property worth having.
#: Re-derive by sweeping `cap` in `composition_split`; the corpus is live and grows.
ADJACENCY_SCAN_CAP = 262_144


def _adjacent(prefix: str, tail: str, corpus: str, window: int, *, cap: int = 0) -> bool:
    """Whether `tail` follows some occurrence of `prefix` within `window` characters.

    Every occurrence is tried, not just the first: a directory named a hundred times in a log
    is named once next to the file in question, and stopping at the first would turn a true
    composition into a coin flip on ordering. `cap` bounds the corpus scanned, never the number
    of occurrences visited — see `ADJACENCY_SCAN_CAP`.
    """
    if window <= 0:
        return False
    if cap:
        # Truncate the corpus, not the loop. The window is kept whole past the cut so a
        # composition straddling the boundary is judged the same way as one before it.
        corpus = corpus[: cap + window]
    i = corpus.find(prefix)
    while i != -1:
        end = i + len(prefix)
        if tail in corpus[end : end + window]:
            return True
        i = corpus.find(prefix, i + 1)
    return False


def composition_split(
    claim: str,
    corpus: str,
    *,
    min_prefix: int = MIN_PREFIX_SEGMENTS,
    min_tail: int = MIN_TAIL_SEGMENTS,
    window: int = ADJACENCY_WINDOW,
    cap: int = ADJACENCY_SCAN_CAP,
) -> tuple[str, str] | None:
    """Split `claim` into a directory the corpus names and a relative tail it also names.

    Returns the split with the **longest tail** among those that work, because that is the one
    that concedes least: a claim explained by `/home` plus everything else is far weaker
    evidence than a deep project directory plus a short tail, and returning the most flattering
    split available would make the bucket look better than it is. A tail shorter than `min_tail`
    is admitted only when the two halves are `_adjacent`; `window=0` disables that route, which
    is what lets the probe re-derive `min_tail` without it.
    """
    segs = path_segments(claim)
    lead = "/" if claim.startswith("/") else ""
    for cut in range(min_prefix, len(segs)):
        prefix = lead + "/".join(segs[:cut])
        tail = "/".join(segs[cut:])
        if prefix not in corpus or tail not in corpus:
            continue
        if len(segs) - cut >= min_tail or _adjacent(prefix, tail, corpus, window, cap=cap):
            return prefix, tail
    return None


def path_claims(digest_text: str) -> set[str]:
    """Every path a digest asserts. Public so the survey reads the same claims the gate does."""
    return _claims(digest_text)["paths"]


def _tilde_anchored(claim: str, allowed_paths: set[str], corpus: str) -> bool:
    """Whether an absolute claim grounds once re-anchored at `~`.

    The log writes home-relative paths as `~/repos/gitea/...` because that is how the commands
    and file references in a session are written; a digest routinely expands the same path to
    `/home/ted/repos/gitea/...`. Neither composes, because the claim's own directory prefix is
    absolute and appears nowhere.

    **`~` is read as a literal in the corpus, never as `$HOME`.** That is the whole reason this
    is safe to ship: a rule that resolved `~` against the process environment would grade the
    same digest and the same log differently on a different machine, and a gate whose verdict
    depends on who ran it is not one. It also removes any need for the `log.cwd`-join that was
    on the table — `cwd` is the agent's project directory, not a repo root, so joining a
    relative path to it manufactures paths that were never real.

    The `MIN_TAIL_SEGMENTS` floor is what keeps it honest: the re-anchored remainder must still
    be at least that long, so `~/audit.md` is never a candidate. Measured over the same controls
    as the segment floors and re-run 2026-09-19 at 494 blocks, this grounds 68 of the 230 claims
    left after composition (29.6%) and 12 of 19,384 foreign claims (0.06%).
    """
    if claim.startswith("~"):
        return False
    segs = path_segments(claim)
    for cut in range(1, len(segs) - MIN_TAIL_SEGMENTS + 1):
        alt = "~/" + "/".join(segs[cut:])
        if path_literal(alt, allowed_paths, corpus) or composition_split(alt, corpus) is not None:
            return True
    return False


def path_literal(claim: str, allowed_paths: set[str], corpus: str) -> bool:
    """The pre-vikunja#876 rule: the derived set with containment, or the corpus **exactly**.

    Kept as a named function rather than folded inline so the survey can still grade the corpus
    the old way from this same commit. A "before" number that requires checking out an older
    commit to reproduce is one nobody re-checks.
    """
    return _in_category(claim, allowed_paths) or claim in corpus


def path_grounded(claim: str, allowed_paths: set[str], corpus: str) -> bool:
    """Whether a path claim is grounded. The gate's rule for a path, as one callable.

    **A path claim is grounded when the log holds it — literally, or as a directory prefix
    plus the whole remaining relative tail, a one-segment tail only when the two sit within
    `ADJACENCY_WINDOW` characters — and an absolute claim is tested again with its leading
    directories replaced by `~`.** That sentence is the entire tolerance, and being
    able to state it in one is a requirement rather than a nicety -- vikunja#848's real defect
    was a classifier whose behaviour nobody could articulate.

    The third route is why this exists. `_in_category` already tolerates containment, but it
    consults only the derived rollup set; the corpus route was **exact substring**. So a true
    claim that *composes* -- a directory the log names joined to a relative path the log also
    names -- was neither literal and graded as a hallucination. Between them the three routes
    clear 737 of the 899 path findings on the live corpus (vikunja#876, re-measured 2026-09-19
    at 494 blocks), and none is a loosening in kind: both halves
    are things the model really was shown, and the floors keep a short common tail (`/src/`,
    `readme.md`) from excusing a claim on its own. Path findings fall from 899 to 162, while
    every one of the 26 claims absent from their own log still fails.

    Extracted from `check_groundedness` so that the survey which measures this gate and the
    gate itself cannot drift: every tolerance lives here, and there is exactly one of it.
    """
    if path_literal(claim, allowed_paths, corpus):
        return True
    if composition_split(claim, corpus) is not None:
        return True
    return _tilde_anchored(claim, allowed_paths, corpus)


#: How close together a claim's tokens must appear to count as co-occurring, in characters of
#: serialized corpus. Chosen by measurement, not taste: across the five real sessions,
#: unbounded overlap grounded 9 of 35 fabricated commands built from words the session really
#: contained, and a 120-character window grounds 1. Both true compressions this route exists
#: to admit survive it comfortably, and they still pass at 40. Widening it past ~200 restores
#: the original defect.
CO_OCCURRENCE_WINDOW = 120


def _tokens_co_occur(claim: str, corpus: str, *, window: int = CO_OCCURRENCE_WINDOW) -> bool:
    """Every significant token of a claim appears in the corpus, and appears *together*.

    This is what lets a faithful *compression* through without letting an invention through.
    `popen(["claude","-p"], env=child_env)` appears verbatim nowhere -- the log holds
    `subprocess.Popen(`, `["claude", "-p",` and `child_env` in three nearby places -- and a
    digest that reassembles them is summarizing, not inventing. Likewise
    `approved -> in-progress -> completed` composes three real statuses into one span.

    **The proximity requirement is the whole point, and it was missing.** The first version of
    this asked only that each token appear *somewhere* in the ~180 KB corpus, which grounds a
    fabricated command whenever its individual words happen to occur in unrelated sentences --
    a routine situation in any session touching several topics. The scribe-shadow-fixes-2026-09
    audit reproduced exactly that: a digest claiming `systemctl restart nginx` graded clean
    against a log whose three words sat in three unrelated turns. Worse, the commit that
    introduced the check asserted that this specific command "is still rejected", which was
    true only of the one fixture where those words are absent -- a property of the test data,
    not of the code. Requiring the tokens to co-occur makes it a property of the code.

    **"Together" is defined as a span, not as distance from an anchor.** The obvious
    implementation -- pick one token, require the others within +/-window of it -- is not
    symmetric: with tokens A, B, C where A and C are 200 apart but each within 120 of B,
    anchoring on B admits the claim and anchoring on A rejects it. Since the anchor was chosen
    by iterating a set, the verdict changed with the interpreter's hash seed, and the gate
    returned different answers for the same input on different runs. A non-deterministic gate
    cannot be falsified, which is worse than a loose one. So the test is the width of the
    smallest stretch of corpus containing every token: well-defined, symmetric, and identical
    on every run.

    Requiring **all** tokens rather than a majority is the other half: a single fabricated
    identifier anywhere in the span fails the whole claim. Spans of fewer than two significant
    tokens get nothing from this path -- verbatim containment already covers them, and a
    one-token overlap test would ground almost anything.

    This does not make coincidence impossible, only unlikely enough to be worth the trade. A
    fabrication whose words genuinely appear together in the log will still pass, and that is
    the irreducible cost of admitting compressions at all.
    """
    tokens = {t.lower() for t in _TOKEN_RE.findall(claim)}
    if len(tokens) < 2:
        return False

    spans: list[tuple[int, int, str]] = []
    for token in tokens:
        found = [(m.start(), m.end(), token) for m in re.finditer(re.escape(token), corpus)]
        if not found:
            return False
        spans.extend(found)
    spans.sort()

    # Smallest stretch of corpus containing every token, by sliding window over the merged
    # occurrences. `seen` counts how many of each token are inside the window; `distinct` is
    # how many token types are, so the window is a candidate exactly when it reaches len(tokens).
    seen: dict[str, int] = {}
    distinct = 0
    left = 0
    for _start, end, token in spans:
        if not seen.get(token):
            distinct += 1
        seen[token] = seen.get(token, 0) + 1
        while distinct == len(tokens):
            if end - spans[left][0] <= window:
                return True
            lt = spans[left][2]
            seen[lt] -= 1
            if not seen[lt]:
                distinct -= 1
            left += 1
    return False


def span_grounded(claim: str, kind: str, allowed: dict[str, set[str]], corpus: str) -> bool:
    """Whether a backticked span — `command`, `code` or `identifier` — is grounded.

    Extracted from `check_groundedness` for the reason `path_grounded` was: the survey that
    measures this gate has to be able to ask the gate's own question, and a second
    implementation of it is how two measurements of the same corpus came to disagree 16-fold.

    The rule is unchanged by this build and that is a measured decision rather than an
    omission. The one tolerance the residual findings suggest — testing the claim with its
    wrapping punctuation removed, so `_is_loopback()` is grounded by a log holding
    `_is_loopback` — is priced by `qc_survey.span_trim_probe` at **1.2 true claims per false
    one at best**, against the 5.0 that bought `ADJACENCY_WINDOW` and the 1.3 that was rejected
    as too weak in #876. It does not earn its place, so it is not here.

    What these routes actually needed was `_CMD_RE`: its delimiter mis-pairing was manufacturing
    22 of their 65 findings out of prose no model ever wrote.
    """
    if kind == CLAIM_COMMAND and _in_category(claim, allowed["commands"]):
        return True
    return claim in corpus or _tokens_co_occur(claim, corpus)


def _ticket_in_corpus(claim: str, corpus: str) -> bool:
    """Is this exact `#N` present, as a whole reference?

    Plain substring containment is wrong here and nowhere else: every other claim class gets
    more accurate as the matched span grows, but `#65` is a *substring* of `#655` while being
    a different ticket. The trailing boundary is the whole point.
    """
    return re.search(re.escape(claim) + r"\b", corpus) is not None


def _ticket_detail(claim: str, corpus: str) -> str:
    """The message for an ungrounded `#N`, naming the id-vs-identifier conflation by name.

    This is the finding the whole gate earns its keep on. On the live run a digest wrote
    `#417` for something the log only ever calls `id 417`; Vikunja's id 417 is identifier
    #398, so `#417` points at a real but unrelated ticket. It reads as an ordinary
    cross-reference and no human reviewer would catch it. A generic "not in the event log"
    leaves the reader to rediscover that, so when the bare number turns up in an id-shaped
    context we say so.
    """
    number = claim.lstrip("#")
    if re.search(_ID_CONTEXT + re.escape(number) + r"\b", corpus):
        return (
            f"ticket {claim!r} is not in the event log, which contains {number!r} only as an "
            f"internal id -- a Vikunja id is not its #identifier, so this points at a "
            f"different ticket"
        )
    return f"ticket not in the event log: {claim!r}"


def check_groundedness(digest_text: str, log: EventLog, report: Report) -> None:
    """Flag every concrete claim the model could not have drawn from what it was shown.

    Three routes to grounded, in widening order: the derived category set, verbatim presence
    in the corpus, then all-token co-occurrence within a bounded window for a composed span.
    The corpus is the serialized event log -- **the same document the model received** -- so
    this asks "was the model shown this?", which is the actual definition of groundedness. It
    is not a loosening: the category sets were never the model's input, only a summary of
    part of it, so a claim absent from them but present in the corpus was always a true claim
    wrongly flagged.

    Tickets and file paths keep the narrower treatment, because that is where the one real
    finding came from.
    """
    allowed = grounding_terms(log)
    corpus = log.grounding_text().lower()
    claimed = _claims(digest_text)

    for claim in sorted(claimed["paths"]):
        if not path_grounded(claim, allowed["paths"], corpus):
            report.add("groundedness", f"{PATH_UNGROUNDED}{claim!r}", kind=CLAIM_PATH, claim=claim)

    for claim in sorted(claimed["tools"]):
        if not _in_category(claim, allowed["tools"]) and claim not in corpus:
            report.add(
                "groundedness",
                f"tool not in the event log: {claim!r}",
                kind=CLAIM_TOOL,
                claim=claim,
            )

    # Exact, not containment: `#41` must not be excused by a log that mentions `#417`. The
    # `#` is part of the identifier, so the corpus check is for the sigil form too -- which
    # is exactly what separates `#639` (written as `#655/#639` in the log, and true) from
    # `#417` (written only as `id 417`, and false).
    for claim in sorted(claimed["tickets"]):
        if claim not in allowed["tickets"] and not _ticket_in_corpus(claim, corpus):
            report.add(
                "groundedness", _ticket_detail(claim, corpus), kind=CLAIM_TICKET, claim=claim
            )

    # No category set exists for `code` or `identifier`, and none should be invented: there is
    # no authoritative list of the statuses, model names, config keys and field names a session
    # touched. The corpus is the only honest ground truth, and it is the right one. `command`
    # has one and consults it first; otherwise all three share `span_grounded`.
    for kind in (CLAIM_COMMAND, CLAIM_CODE, CLAIM_IDENTIFIER):
        for claim in sorted(claimed[kind]):
            if not span_grounded(claim, kind, allowed, corpus):
                report.add(
                    "groundedness",
                    f"{kind} not in the event log: {claim!r}",
                    kind=kind,
                    claim=claim,
                )


def check_event_coverage(
    digest_text: str, log: EventLog, report: Report, *, floor: float = DEFAULT_COVERAGE_FLOOR
) -> None:
    """Assert the digest reflects a minimum share of the session's non-trivial events.

    "Non-trivial" excludes events with no target: a tool call the log could not describe is
    not something a digest can be blamed for omitting.
    """
    haystack = _norm(digest_text)
    events = [ev for turn in log.turns for ev in turn.events if ev.target]
    report.events_total = len(events)
    if not events:
        report.coverage = 1.0
        return
    referenced = 0
    for ev in events:
        needle = _norm(ev.target)[:60]
        if (needle and needle in haystack) or ev.tool.lower() in haystack:
            referenced += 1
    report.events_referenced = referenced
    report.coverage = referenced / len(events)
    if report.coverage < floor:
        report.add(
            "event_coverage",
            f"digest references {referenced}/{len(events)} events "
            f"({report.coverage:.1%}), below the {floor:.0%} floor",
        )


def check_freshness(digest_path: Path, transcript_path: Path, report: Report) -> None:
    """A digest must exist and be newer than the transcript's last write."""
    if not digest_path.exists():
        report.add("coverage", f"no digest for {transcript_path}")
        return
    try:
        if digest_path.stat().st_mtime_ns < transcript_path.stat().st_mtime_ns:
            report.add(
                "coverage",
                f"digest {digest_path} is older than the transcript it summarizes",
            )
    except OSError as exc:
        report.add("coverage", f"cannot compare timestamps: {exc}")


def check_digest(
    digest_text: str, log: EventLog, *, floor: float = DEFAULT_COVERAGE_FLOOR
) -> Report:
    """Run the deterministic gates over one digest. `Report.ok` is the verdict."""
    report = Report(session_id=log.session_id, transcript_path=log.transcript_path)
    if not digest_text.strip():
        report.add("coverage", "digest is empty")
        return report
    check_groundedness(digest_text, log, report)
    check_event_coverage(digest_text, log, report, floor=floor)
    return report


if __name__ == "__main__":  # pragma: no cover
    # `python -m scribe.qc` is the command the build plan specifies, and without this block
    # it imports this module, runs nothing and exits 0 -- a gate that cannot fail, which is
    # the exact defect the gate exists to catch. The CLI lives in qc_cli so that importing
    # `scribe.qc` as a library pulls in no argparse machinery.
    import sys

    from .qc_cli import main

    sys.exit(main())
