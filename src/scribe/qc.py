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
_CMD_RE = re.compile(r"`([^`\n]{3,200})`")

#: Claim classes for a backticked span. The split IS vikunja#848: the gate used to call every
#: one of these a `command` and demand a match in `rollup.commands`, so the ordinary
#: vocabulary of a good digest -- statuses, model names, config keys, field names -- was
#: reported as a fabricated command. On the first live run that was 70 of 73 findings, and
#: `groundedness_rate` read 0.0 for five digests that were materially better than the
#: incumbent's.
CLAIM_COMMAND = "command"
CLAIM_CODE = "code"
CLAIM_IDENTIFIER = "identifier"

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
_ID_CONTEXT = r'(?:"id"\s*:\s*|\bid\s*[:=]?\s*|/)'


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
    check: str
    detail: str


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

    def add(self, check: str, detail: str) -> None:
        self.findings.append(Finding(check, detail))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "events_total": self.events_total,
            "events_referenced": self.events_referenced,
            "coverage": round(self.coverage, 4),
            "findings": [{"check": f.check, "detail": f.detail} for f in self.findings],
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


#: Prefix of a path-claim groundedness finding. A constant rather than an inline f-string so
#: `qc_survey` can recover the claim from the finding it produced, instead of re-deriving
#: which claims the gate rejected. A second implementation of that one rule is precisely what
#: made the two pre-build measurements of vikunja#876 disagree 16-fold.
PATH_UNGROUNDED = "path not in the event log: "


#: A composition's directory half must name a real directory, not the filesystem root. Two
#: segments is the shortest thing that does. At one, every absolute path on the machine
#: composes out of `/home` plus a tail, which is not evidence of anything.
MIN_PREFIX_SEGMENTS = 2
#: And its relative half must be more than a bare filename -- see `composition_split`. Both
#: floors are derived from `qc_survey.cross_session_probe`, which measures how often each
#: candidate grounds a path harvested from a *different* session's digest — a claim the model
#: here demonstrably was not shown. Measured 2026-09-17 over the live corpus (470 blocks), with
#: 18,564 such controls against the corpus's own 880 failing claims:
#:
#:     prefix  tail   falsely grounded   truly grounded
#:          2     1     239   (1.3%)       774  (87.9%)
#:          2     2      70   (0.4%)       550  (62.5%)
#:          2     3      47   (0.2%)       315  (35.8%)
#:          1     2      90   (0.5%)       551  (62.6%)
#:          3     2      53   (0.3%)       517  (58.8%)
#:
#: `min_tail=2` is where the false-ground rate collapses — 3.4x lower than at 1 — and the step
#: after it buys almost nothing for another 27 points of true claims. A tail of one segment is
#: a bare filename, and `changelog.md` or `agents.md` appears in nearly every log, so at 1 any
#: directory the log names pairs with any common filename it names. `min_prefix=2` strictly
#: dominates 1 (lower false-ground at the same true-ground), and 3 buys 0.1% for 3.7 points.
#: Neither floor was chosen against the corpus it judges. Re-derive with
#: `python -m scribe.qc_survey --probe`; the corpus is live, so the figures drift slightly.
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
#: 1.3% false-ground for 224 true claims recovered. Requiring adjacency instead recovers 104
#: for 21, measured over the same 2026-09-17 snapshot — 330 own claims and 18,494 controls that
#: composition alone still fails:
#:
#:     window   falsely grounded   truly grounded
#:         40       8   (0.04%)      47  (14.2%)
#:         80      14   (0.08%)      95  (28.8%)
#:        120      21   (0.11%)     104  (31.5%)
#:        200      35   (0.19%)     111  (33.6%)
#:
#: 120 is 5.0 true claims per false one against the blanket rule's 1.3, and widening to 200
#: buys 7 more true claims for 14 more false. It is not coincidence that this lands on the same
#: number as `CO_OCCURRENCE_WINDOW` -- it is the same corpus and the same question.
ADJACENCY_WINDOW = 120


def _adjacent(prefix: str, tail: str, corpus: str, window: int) -> bool:
    """Whether `tail` follows some occurrence of `prefix` within `window` characters.

    Every occurrence is tried, not just the first: a directory named a hundred times in a log
    is named once next to the file in question, and stopping at the first would turn a true
    composition into a coin flip on ordering.
    """
    if window <= 0:
        return False
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
        if len(segs) - cut >= min_tail or _adjacent(prefix, tail, corpus, window):
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
    as the segment floors, this grounds 68 of the 226 claims left after composition (30.1%) and
    15 of 18,473 foreign claims (0.08%).
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
    clear 722 of the 880 path findings on the live corpus (vikunja#876), and none is a loosening
    in kind: both halves
    are things the model really was shown, and the floors keep a short common tail (`/src/`,
    `readme.md`) from excusing a claim on its own. Across the live corpus it takes block failure
    from 39.6% to 21.5% and path findings from 880 to 158, while every one of the 26 claims
    absent from their own log still fails.

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
            report.add("groundedness", f"{PATH_UNGROUNDED}{claim!r}")

    for claim in sorted(claimed["tools"]):
        if not _in_category(claim, allowed["tools"]) and claim not in corpus:
            report.add("groundedness", f"tool not in the event log: {claim!r}")

    # Exact, not containment: `#41` must not be excused by a log that mentions `#417`. The
    # `#` is part of the identifier, so the corpus check is for the sigil form too -- which
    # is exactly what separates `#639` (written as `#655/#639` in the log, and true) from
    # `#417` (written only as `id 417`, and false).
    for claim in sorted(claimed["tickets"]):
        if claim not in allowed["tickets"] and not _ticket_in_corpus(claim, corpus):
            report.add("groundedness", _ticket_detail(claim, corpus))

    for claim in sorted(claimed[CLAIM_COMMAND]):
        if (
            not _in_category(claim, allowed["commands"])
            and claim not in corpus
            and not _tokens_co_occur(claim, corpus)
        ):
            report.add("groundedness", f"command not in the event log: {claim!r}")

    # No category set exists for these, and none should be invented: there is no authoritative
    # list of the statuses, model names, config keys and field names a session touched. The
    # corpus is the only honest ground truth, and it is the right one.
    for kind in (CLAIM_CODE, CLAIM_IDENTIFIER):
        for claim in sorted(claimed[kind]):
            if claim not in corpus and not _tokens_co_occur(claim, corpus):
                report.add("groundedness", f"{kind} not in the event log: {claim!r}")


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
