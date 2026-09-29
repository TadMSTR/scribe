"""Grade the whole digest corpus block by block, and classify every path claim the gate rejects.

**This is an instrument, not a gate.** It reads; it writes nothing to the corpus. Its job is to
answer one question that two throwaway scripts answered differently: *why* does a path claim
fail `check_groundedness`, and in what proportions.

It exists as committed code rather than a scratch script because of how vikunja#876 arrived.
Two independent measurements of the same corpus agreed on the headline (~40% of blocks carry at
least one finding) and disagreed **16-fold** on the bucket that decides the fix — "genuinely
absent" was 34 in one and 568 in the other. Neither was reproducible, because neither was in the
repo, and the two candidate fixes address different buckets. A number that picks a fix has to be
re-runnable by whoever doubts it, including after the fix lands.

Two rules it follows, both learned the hard way:

  * **Grade per block, never per file.** A daily digest holds many session blocks and each is
    derived from its *own* event log. Grading a whole file against one session's log reports 92%
    failure and ~79 findings per block, all false (vikunja#852).
  * **Use the gate's own predicate.** Every "is this present?" test here routes through
    `qc.path_grounded` or a plain substring of `log.grounding_text()` — the same corpus the model
    was shown. Both prior measurements built their own corpus, and that is why they disagreed.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .eventlog import EventLogError, eventlog_path, load_eventlog
from .extract.models import EventLog
from .qc import (
    _CMD_RE,
    _ID_CONTEXT,
    _TOKEN_RE,
    ADJACENCY_WINDOW,
    CLAIM_CODE,
    CLAIM_COMMAND,
    CLAIM_IDENTIFIER,
    CLAIM_KINDS,
    CLAIM_PATH,
    CLAIM_TICKET,
    CLAIM_TOOL,
    MAX_SPAN_CHARS,
    MIN_PREFIX_SEGMENTS,
    MIN_SPAN_CHARS,
    MIN_TAIL_SEGMENTS,
    Report,
    _claims,
    check_groundedness,
    composition_split,
    grounding_terms,
    path_claims,
    path_grounded,
    path_literal,
    path_segments,
    span_grounded,
    strip_scribe_markers,
)
from .writeback import paired_blocks

#: The claim is present in the corpus exactly as written. A finding in this bucket is a
#: contradiction — the gate rejected something its own rule accepts — so it is carried as an
#: invariant rather than an expected outcome.
VERBATIM = "verbatim"
#: `~/x` where the log holds `/home/user/x`, or the reverse. One substitution, no composition.
HOME_EXPANSION = "home-expansion"
#: A directory the log names, joined to a relative tail the log also names. Both halves are
#: separately present; only their concatenation is not. This is vikunja#876's stated defect.
COMPOSED = "composed"
#: Some trailing run of segments is present, but no split of the claim has *both* halves
#: present. Weaker evidence than `COMPOSED`, and the bucket where a floor has to do real work.
SUFFIX = "suffix"
#: Not even the basename appears anywhere in the corpus. **The control set.** Whatever
#: tolerance lands, every member of this bucket must still fail — a gate that passes
#: everything is not a gate (vikunja#848, #868).
ABSENT = "absent"

#: Applied in this order, so the buckets are disjoint by construction and the earlier, stronger
#: explanation always wins. Reordering changes every number this module reports.
BUCKETS = (VERBATIM, HOME_EXPANSION, COMPOSED, SUFFIX, ABSENT)

# --- non-path buckets -------------------------------------------------------------------
# Of 289 findings this module classified 162 and counted the other 127 without naming a cause
# for any of them, because every bucket above is about a *path*. A tolerance cannot be chosen
# for a route whose failures cannot be attributed, so these exist before anything is tuned.

#: The digest wrote `#N`, and the corpus holds `N` only as an internal id -- `task_id=417`,
#: `"target": "417"`, `/tasks/417`, or an id/# table. **This is the gate working.** Verified
#: against the live tracker: id 541 is identifier #493 and id 930 is #847, so a digest writing
#: `#541` names a real but unrelated ticket. Nothing in this bucket wants a tolerance.
ID_CONFLATION = "id-conflation"
#: `N` appears in the corpus, but as a quantity rather than an id -- `73 pipelines`, `478
#: lines`, a digit run inside a sha256. Coincidence, not evidence. Also a true finding.
COINCIDENTAL_NUMBER = "coincidental-number"
#: The claim is an artefact of `_CMD_RE`'s delimiter mis-pairing rather than anything a model
#: asserted -- see the constant's docstring. **Must be 0**: the pattern was fixed, and this
#: bucket exists so a regression shows up as a named cause rather than as a worse rate.
DESYNC_ARTEFACT = "desync-artefact"
#: Grounds once surrounding quotes or brackets are removed -- `"operator"`, `{"prs":[]}`.
PUNCTUATION_WRAPPED = "punctuation-wrapped"
#: Fewer than two significant tokens, so `_tokens_co_occur` returns early and the claim had no
#: route but verbatim presence. Not a failure of tolerance so much as an absence of one.
SINGLE_TOKEN = "single-token"  # noqa: S105 -- a lexical token (`_TOKEN_RE`), not a secret
#: Every token is in the corpus, but never within `CO_OCCURRENCE_WINDOW` of the others.
TOKEN_SPREAD = "token-spread"  # noqa: S105 -- ditto
#: Some tokens are present and at least one is not -- a partly-grounded claim.
TOKEN_MISSING = "token-missing"  # noqa: S105 -- ditto

#: For `ticket`. Ordered strongest-explanation-first, like `BUCKETS`.
TICKET_BUCKETS = (ID_CONFLATION, COINCIDENTAL_NUMBER, ABSENT)
#: For `identifier`, `code` and `command` -- the backticked-span routes, which share a rule.
SPAN_BUCKETS = (
    DESYNC_ARTEFACT,
    PUNCTUATION_WRAPPED,
    SINGLE_TOKEN,
    TOKEN_SPREAD,
    TOKEN_MISSING,
    ABSENT,
)
#: For `tool`.
TOOL_BUCKETS = (ABSENT,)

#: Which bucket set attributes each claim kind. A kind absent from here would be counted and
#: never attributed, which is the gap this build opened with -- so `survey` asserts every
#: finding lands in a bucket rather than letting one fall through to a remainder.
BUCKETS_BY_KIND = {
    CLAIM_PATH: BUCKETS,
    CLAIM_TICKET: TICKET_BUCKETS,
    CLAIM_TOOL: TOOL_BUCKETS,
    CLAIM_COMMAND: SPAN_BUCKETS,
    CLAIM_CODE: SPAN_BUCKETS,
    CLAIM_IDENTIFIER: SPAN_BUCKETS,
}


def _segments(claim: str) -> list[str]:
    return path_segments(claim)


def longest_present_suffix(claim: str, corpus: str) -> int:
    """How many trailing segments of `claim` appear in the corpus as one run. 0 for none.

    Counted from the longest end down, so the answer is the *most* of the claim the corpus can
    account for — the figure a segment floor has to be chosen against.
    """
    segs = path_segments(claim)
    for n in range(len(segs), 0, -1):
        if "/".join(segs[-n:]) in corpus:
            return n
    return 0


def home_forms(claim: str, home: str) -> list[str]:
    """The same claim written the other way round: `~` expanded, or `$HOME` contracted."""
    home = home.rstrip("/")
    if not home:
        return []
    if claim.startswith("~/"):
        return [home + claim[1:]]
    if claim == "~":
        return [home]
    if claim.startswith(home + "/"):
        return ["~" + claim[len(home) :]]
    if claim == home:
        return ["~"]
    return []


@dataclass
class ClaimVerdict:
    """One rejected claim, and the strongest available explanation for its rejection."""

    claim: str
    bucket: str
    session_id: str
    digest: str
    #: Which route rejected it — one of `CLAIM_KINDS`. Read off the finding, never re-derived.
    kind: str = CLAIM_PATH
    #: Trailing segments of the claim the corpus can account for. The input to the floor.
    suffix_segments: int = 0
    #: The `(directory, tail)` that explains a `COMPOSED` claim; empty otherwise.
    split: tuple[str, str] = ("", "")


def classify_path_claim(
    claim: str, allowed_paths: set[str], corpus: str, *, home: str
) -> tuple[str, int, tuple[str, str]]:
    """Which bucket a rejected path claim falls in, plus the evidence behind that verdict.

    Tests are applied in `BUCKETS` order and the first hit wins, so a claim that is both a home
    expansion and a composition is counted once, as the narrower of the two.
    """
    suffix_n = longest_present_suffix(claim, corpus)
    if claim in corpus:
        return VERBATIM, suffix_n, ("", "")
    for alt in home_forms(claim, home):
        if path_grounded(alt, allowed_paths, corpus):
            return HOME_EXPANSION, suffix_n, ("", "")
    split = composition_split(claim, corpus)
    if split is not None:
        return COMPOSED, suffix_n, split
    if suffix_n:
        return SUFFIX, suffix_n, ("", "")
    return ABSENT, 0, ("", "")


@dataclass
class Block:
    """One digest block, paired with the session it was derived from."""

    session_id: str
    transcript_path: str
    digest: str
    body: str
    #: The anchor's turn uuid -- with `session_id`, the block's identity on disk.
    turn_uuid: str = ""
    #: The anchor's `provisional:` value: "" for a real digest, else what kind of stand-in.
    provisional: str = ""


def iter_blocks(digest_root: str | Path) -> Iterator[Block]:
    """Every complete block under the digest root, newest path order, recursively.

    Recursive because the live corpus is partitioned by agent (`digests/developer/*.md`) — a
    flat `glob("*.md")` finds nothing and reports a clean corpus, which is the most dangerous
    possible way for this to be wrong.
    """
    root = Path(digest_root).expanduser()
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for anchor, close in paired_blocks(text):
            yield Block(
                session_id=anchor.group(1),
                transcript_path=anchor.group(3),
                digest=str(path.relative_to(root)),
                body=text[anchor.end() : close.start()],
                turn_uuid=anchor.group(2),
                provisional=anchor.group(4) or "",
            )


def rejected_claims(report: Report, kind: str) -> list[str]:
    """The claims of one kind a report rejected, recovered from the findings themselves.

    Still read back out of the gate's own output rather than recomputed — that property is what
    `Survey.gate_disagreements` polices and it has not changed. What changed is that the claim
    is read from `Finding.claim`, a field the gate sets as it rejects, instead of being parsed
    back out of the English in `Finding.detail`.

    The parse could not be extended past the path route. `_ticket_detail` writes two different
    sentences and the interesting one puts the claim mid-sentence, so attributing tickets by
    prefix would have meant a regex over prose per route — a second implementation of the gate's
    classification, kept in sync by hand. That is the drift this function exists to prevent.
    """
    return [f.claim for f in report.findings if f.check == "groundedness" and f.kind == kind]


def rejected_paths(report: Report) -> list[str]:
    """The path claims a report rejected. The path route's `rejected_claims`, named."""
    return rejected_claims(report, CLAIM_PATH)


def _desynced_spans(body: str) -> set[str]:
    """Backticked spans the OLD `_CMD_RE` would have invented from prose between two spans.

    Kept so `DESYNC_ARTEFACT` is a bucket a regression can land in and be named, rather than a
    silent worsening of the rate. Against the fixed pattern this is empty on every block.
    """
    text = strip_scribe_markers(body)
    live = {
        " ".join(m.group(1).split()).lower()
        for m in _CMD_RE.finditer(text)
        if MIN_SPAN_CHARS <= len(m.group(1)) <= MAX_SPAN_CHARS
    }
    old = {" ".join(m.group(1).split()).lower() for m in re.finditer(r"`([^`\n]{3,200})`", text)}
    return old - live


def classify_ticket_claim(claim: str, corpus: str) -> str:
    """Why a `#N` was rejected. See `ID_CONFLATION` — most of this bucket is the gate working."""
    number = claim.lstrip("#")
    if re.search(_ID_CONTEXT + re.escape(number) + r"\b", corpus):
        return ID_CONFLATION
    if re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", corpus):
        return COINCIDENTAL_NUMBER
    return ABSENT


def classify_span_claim(claim: str, corpus: str, *, desynced: set[str]) -> str:
    """Why a backticked span — `identifier`, `code` or `command` — was rejected."""
    if claim in desynced:
        return DESYNC_ARTEFACT
    stripped = claim.strip("\"'`.,;:()[]{}<>* ")
    if stripped and stripped != claim and stripped in corpus:
        return PUNCTUATION_WRAPPED
    tokens = {t.lower() for t in _TOKEN_RE.findall(claim)}
    if not tokens:
        return ABSENT
    missing = [t for t in tokens if t not in corpus]
    if len(missing) == len(tokens):
        return ABSENT
    if missing:
        return TOKEN_MISSING
    # Every token is present. Either there were too few for the co-occurrence route to run at
    # all, or they are present and never near each other.
    return SINGLE_TOKEN if len(tokens) < 2 else TOKEN_SPREAD


def classify_claim(
    claim: str, kind: str, allowed_paths: set[str], corpus: str, *, home: str, desynced: set[str]
) -> tuple[str, int, tuple[str, str]]:
    """Which bucket a rejected claim of any kind falls in. Dispatches on the finding's `kind`."""
    if kind == CLAIM_PATH:
        return classify_path_claim(claim, allowed_paths, corpus, home=home)
    if kind == CLAIM_TICKET:
        return classify_ticket_claim(claim, corpus), 0, ("", "")
    if kind == CLAIM_TOOL:
        return ABSENT, 0, ("", "")
    return classify_span_claim(claim, corpus, desynced=desynced), 0, ("", "")


def attribute(
    report: Report, body: str, log: EventLog, *, home: str = ""
) -> list[tuple[str, str, str]]:
    """`(kind, bucket, claim)` for every groundedness finding in one report.

    The run record's view of the same classification `survey` performs, for one block at a
    time. Every kind is read off the findings with `rejected_claims`, including paths: `survey`
    recomputes the path set only so it can grade under a rule other than the shipped one and
    count disagreements, and the run record grades under the shipped rule by definition. Under
    `RULE_CURRENT` the two sets are identical, which `gate_disagreements == 0` asserts on every
    survey run.
    """
    home = home or str(Path.home())
    allowed = grounding_terms(log)["paths"]
    corpus = log.grounding_text().lower()
    desynced = _desynced_spans(body)
    out = []
    for kind in CLAIM_KINDS:
        for claim in rejected_claims(report, kind):
            bucket, _n, _split = classify_claim(
                claim, kind, allowed, corpus, home=home, desynced=desynced
            )
            out.append((kind, bucket, claim))
    return out


@dataclass
class Survey:
    """The corpus-wide result. Every headline this build reports comes from here."""

    rule: str = "current"
    blocks: int = 0
    blocks_failing: int = 0
    #: Blocks where this instrument's path verdicts differed from the gate's own findings.
    #: Must be 0 under `RULE_CURRENT`; anything else means the two have drifted.
    gate_disagreements: int = 0
    blocks_no_log: int = 0
    findings_total: int = 0
    findings_path: int = 0
    #: Findings carrying no claim kind — a groundedness route that forgot to record one, or a
    #: coverage finding. Reported rather than absorbed: an unattributed remainder is exactly
    #: what this build had to fix before it could choose anything.
    findings_unattributed: int = 0
    buckets: Counter = field(default_factory=Counter)
    #: Findings by claim kind, and by `(kind, bucket)`. Every groundedness finding lands in
    #: exactly one of the latter.
    kinds: Counter = field(default_factory=Counter)
    kind_buckets: Counter = field(default_factory=Counter)
    verdicts: list[ClaimVerdict] = field(default_factory=list)

    @property
    def failure_rate(self) -> float:
        return self.blocks_failing / self.blocks if self.blocks else 0.0

    def control_set(self) -> list[ClaimVerdict]:
        """Every PATH claim in `ABSENT` — what must still fail after any tolerance lands.

        Deliberately still scoped to the path route. `ABSENT` now exists for other kinds too,
        and widening this to include them would silently redefine the control set that three
        releases of measurements are stated against — the comparison would stop meaning what it
        meant. `absent_by_kind` reports the others separately.
        """
        return [v for v in self.verdicts if v.kind == CLAIM_PATH and v.bucket == ABSENT]

    def absent_by_kind(self) -> dict[str, int]:
        """`ABSENT` counts for every kind, so the non-path controls are visible but separate."""
        return {
            k: self.kind_buckets.get((k, ABSENT), 0)
            for k in CLAIM_KINDS
            if self.kind_buckets.get((k, ABSENT), 0)
        }

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "gate_disagreements": self.gate_disagreements,
            "blocks": self.blocks,
            "blocks_failing": self.blocks_failing,
            "blocks_no_log": self.blocks_no_log,
            "failure_rate": round(self.failure_rate, 4),
            "findings_total": self.findings_total,
            "findings_path": self.findings_path,
            "findings_unattributed": self.findings_unattributed,
            "buckets": {b: self.buckets.get(b, 0) for b in BUCKETS},
            "kinds": {k: self.kinds.get(k, 0) for k in CLAIM_KINDS},
            "kind_buckets": {
                k: {b: self.kind_buckets.get((k, b), 0) for b in BUCKETS_BY_KIND[k]}
                for k in CLAIM_KINDS
            },
            "suffix_segments": dict(
                sorted(Counter(v.suffix_segments for v in self.verdicts).items())
            ),
            "verdicts": [
                {
                    "claim": v.claim,
                    "kind": v.kind,
                    "bucket": v.bucket,
                    "session_id": v.session_id,
                    "digest": v.digest,
                    "suffix_segments": v.suffix_segments,
                    "split": list(v.split),
                }
                for v in self.verdicts
            ],
        }


#: Grade with the rule shipped in `qc.path_grounded`. The real measurement.
RULE_CURRENT = "current"
#: Grade with `qc.path_literal` — the pre-vikunja#876 rule. Reproduces the "before" number
#: from the same commit as the "after" one, so the comparison needs no checkout.
RULE_LITERAL = "literal"
RULES = {RULE_CURRENT: path_grounded, RULE_LITERAL: path_literal}


def survey(
    digest_root: str | Path,
    eventlog_root: str | Path,
    *,
    home: str = "",
    limit: int = 0,
    rule: str = RULE_CURRENT,
) -> Survey:
    """Grade every block against its own event log and classify every path claim rejected.

    Path claims are judged by `RULES[rule]` directly rather than read out of the report, so the
    "before" and "after" numbers come from one code path. Under `RULE_CURRENT` that must agree
    exactly with what the gate itself did, and `Survey.gate_disagreements` counts any case where
    it does not — an instrument that can drift from the thing it measures is how vikunja#876
    ended up with two irreconcilable sets of figures.
    """
    home = home or str(Path.home())
    predicate = RULES[rule]
    out = Survey(rule=rule)
    for block in iter_blocks(digest_root):
        if limit and out.blocks >= limit:
            break
        log = _load(eventlog_root, block)
        if log is None:
            out.blocks_no_log += 1
            continue
        out.blocks += 1
        report = Report(session_id=log.session_id, transcript_path=log.transcript_path)
        check_groundedness(block.body, log, report)

        allowed = grounding_terms(log)["paths"]
        corpus = log.grounding_text().lower()
        rejected = sorted(c for c in path_claims(block.body) if not predicate(c, allowed, corpus))
        if rule == RULE_CURRENT and rejected != sorted(rejected_paths(report)):
            out.gate_disagreements += 1

        other = len(report.findings) - len(rejected_paths(report))
        if rejected or other:
            out.blocks_failing += 1
        out.findings_total += other + len(rejected)
        out.findings_path += len(rejected)

        # Every groundedness finding is attributed, not just the path ones. Path claims keep
        # coming from `rejected` — which is the independently recomputed set the disagreement
        # counter compares — so the path numbers are unchanged by this. The other kinds are
        # read straight off the findings, where there is nothing to disagree with.
        desynced = _desynced_spans(block.body)
        for kind in CLAIM_KINDS:
            claims = rejected if kind == CLAIM_PATH else rejected_claims(report, kind)
            out.kinds[kind] += len(claims)
            for claim in claims:
                bucket, suffix_n, split = classify_claim(
                    claim, kind, allowed, corpus, home=home, desynced=desynced
                )
                if kind == CLAIM_PATH:
                    out.buckets[bucket] += 1
                out.kind_buckets[(kind, bucket)] += 1
                out.verdicts.append(
                    ClaimVerdict(
                        claim=claim,
                        bucket=bucket,
                        kind=kind,
                        session_id=block.session_id,
                        digest=block.digest,
                        suffix_segments=suffix_n,
                        split=split,
                    )
                )
        out.findings_unattributed += sum(
            1 for f in report.findings if f.check == "groundedness" and f.kind not in CLAIM_KINDS
        )
    return out


def _load(eventlog_root: str | Path, block: Block) -> EventLog | None:
    path = eventlog_path(eventlog_root, block.session_id, block.transcript_path)
    if not path.is_file():
        return None
    try:
        return load_eventlog(path)
    except EventLogError:
        return None


def cross_session_probe(
    digest_root: str | Path,
    eventlog_root: str | Path,
    *,
    floors: tuple[tuple[int, int], ...] = ((2, 1), (2, 2), (2, 3), (1, 2), (3, 2)),
    per_block: int = 40,
    seed: int = 20260917,
) -> dict:
    """How often each candidate floor grounds a claim the session was **not** shown.

    This is what the floors are derived from, and it is deliberately not "which floor makes the
    corpus pass" — a floor chosen against the thing it is meant to judge measures nothing. The
    same method picked `CO_OCCURRENCE_WINDOW`.

    The control is a *foreign* claim: a path harvested from some other session's digest, kept
    only when this session's log does not already contain it. By construction the model that
    wrote this block could not have drawn it from its input, so any floor that grounds one is
    grounding a claim it should have failed. Reported alongside is the true-claim cost — how
    many of this block's own currently-failing claims each floor still admits — because a floor
    that grounds nothing foreign by also grounding nothing real is not a win.
    """
    # Seeded and reproducible on purpose: the floors in `qc` are derived from this sample,
    # so a run that cannot be repeated is a number that cannot be checked. Nothing here is
    # a secret or a token.
    rng = random.Random(seed)  # noqa: S311
    blocks = list(iter_blocks(digest_root))
    pool: list[str] = []
    for b in blocks:
        pool.extend(path_claims(b.body))
    pool = sorted(set(pool))

    false_ground = {f: 0 for f in floors}
    true_ground = {f: 0 for f in floors}
    foreign_total = 0
    own_total = 0
    shipped_false = shipped_true = 0
    for b in blocks:
        log = _load(eventlog_root, b)
        if log is None:
            continue
        allowed = grounding_terms(log)["paths"]
        corpus = log.grounding_text().lower()
        own = set(path_claims(b.body))

        foreign = [
            c
            for c in rng.sample(pool, min(per_block, len(pool)))
            if c not in own and not path_literal(c, allowed, corpus)
        ]
        foreign_total += len(foreign)
        real = [c for c in own if not path_literal(c, allowed, corpus)]
        own_total += len(real)

        for f in floors:
            mp, mt = f
            # `window=0` disables the adjacency route, so this sweep measures the segment
            # floors alone. Sweeping them with adjacency on would price two decisions as one.
            for c in foreign:
                if composition_split(c, corpus, min_prefix=mp, min_tail=mt, window=0) is not None:
                    false_ground[f] += 1
            for c in real:
                if composition_split(c, corpus, min_prefix=mp, min_tail=mt, window=0) is not None:
                    true_ground[f] += 1
        for c in foreign:
            if composition_split(c, corpus) is not None:
                shipped_false += 1
        for c in real:
            if composition_split(c, corpus) is not None:
                shipped_true += 1

    return {
        "foreign_claims": foreign_total,
        "own_failing_claims": own_total,
        "shipped": {
            "min_prefix": MIN_PREFIX_SEGMENTS,
            "min_tail": MIN_TAIL_SEGMENTS,
            "window": ADJACENCY_WINDOW,
            "false_ground": shipped_false,
            "false_ground_rate": round(shipped_false / foreign_total, 4) if foreign_total else 0.0,
            "true_ground": shipped_true,
            "true_ground_rate": round(shipped_true / own_total, 4) if own_total else 0.0,
        },
        "floors": [
            {
                "min_prefix": mp,
                "min_tail": mt,
                "false_ground": false_ground[(mp, mt)],
                "false_ground_rate": round(false_ground[(mp, mt)] / foreign_total, 4)
                if foreign_total
                else 0.0,
                "true_ground": true_ground[(mp, mt)],
                "true_ground_rate": round(true_ground[(mp, mt)] / own_total, 4)
                if own_total
                else 0.0,
            }
            for mp, mt in floors
        ],
    }


#: Leading/trailing decoration a model adds around a real thing: quotes, parens, braces, a
#: leading `@` or `#`. Path characters are deliberately NOT stripped.
_TRIM_RE = re.compile(r"^[^0-9A-Za-z_~/.-]+|[^0-9A-Za-z_~/.-]+$")


def trim_decoration(claim: str) -> str:
    """`claim` with wrapping punctuation removed. `'"operator"'` -> `'operator'`."""
    return _TRIM_RE.sub("", claim)


def span_trim_probe(
    digest_root: str | Path,
    eventlog_root: str | Path,
    *,
    min_lengths: tuple[int, ...] = (0, 3, 4, 5, 6, 8, 10, 12),
    per_block: int = 40,
    seed: int = 20260919,
) -> dict:
    """Price the one tolerance the `identifier`/`code`/`command` routes actually suggest.

    **This exists to record a REJECTION, and it is committed for the same reason the accepted
    derivations are: a measurement that picked a decision has to be re-runnable by whoever
    doubts it.** Reading the 43 residual span findings one by one, the dominant cause is not a
    missing tolerance but decoration — `_is_loopback()` where the log holds `_is_loopback`,
    `"operator"` where it holds `operator`, `@rootfs-pre-...` where it holds the bare snapshot
    name. The obvious fix is to test the trimmed form too.

    Priced by `cross_session_probe`'s method — foreign claims harvested from other sessions'
    digests, which the model here demonstrably was not shown — it does not earn its place, and
    it is not close. Measured 2026-09-19 over 494 blocks, ~17,800 controls per threshold:

        min length   falsely grounded   truly grounded   true per false
                 0     309   (1.74%)      14  (32.6%)             0.05
                 3     266   (1.49%)      13  (30.2%)             0.05
                 4     178   (1.00%)      12  (27.9%)             0.07
                 6     136   (0.77%)      10  (23.3%)             0.07
                 8      61   (0.34%)       8  (18.6%)             0.13
                12       5   (0.03%)       6  (14.0%)             1.20

    The bar is the project's own. `ADJACENCY_WINDOW` was chosen at **5.0 true claims per false
    one** and the blanket composition rule was REJECTED at **1.3**. Trimming tops out at 1.2 —
    below the number #876 already declined — while recovering 6 of 43 findings. So the span
    routes ship unchanged, and the 22 findings this build removed from them came from fixing
    `_CMD_RE`'s delimiter pairing, which was a correctness defect rather than a tolerance.

    Re-run with `python -m scribe.qc_survey --probe-spans`.
    """
    rng = random.Random(seed)  # noqa: S311
    span_kinds = (CLAIM_IDENTIFIER, CLAIM_CODE, CLAIM_COMMAND)
    blocks = []
    pool: list[str] = []
    for b in iter_blocks(digest_root):
        log = _load(eventlog_root, b)
        if log is None:
            continue
        claimed = _claims(b.body)
        spans = {k: sorted(claimed[k]) for k in span_kinds}
        blocks.append((spans, grounding_terms(log), log.grounding_text().lower()))
        for k in span_kinds:
            pool.extend(spans[k])
    pool = sorted(set(pool))

    rows = []
    for min_len in min_lengths:
        false_ground = foreign_total = true_ground = own_total = 0
        for spans, allowed, corpus in blocks:
            own = {c for k in span_kinds for c in spans[k]}

            def admits(claim: str, _corpus: str = "", _n: int = min_len) -> bool:
                t = trim_decoration(claim)
                return bool(t) and t != claim and len(t) >= _n and t in _corpus

            foreign = [
                c
                for c in rng.sample(pool, min(per_block, len(pool)))
                if c not in own and not span_grounded(c, CLAIM_IDENTIFIER, allowed, corpus)
            ]
            foreign_total += len(foreign)
            false_ground += sum(1 for c in foreign if admits(c, corpus))
            for kind in span_kinds:
                real = [c for c in spans[kind] if not span_grounded(c, kind, allowed, corpus)]
                own_total += len(real)
                true_ground += sum(1 for c in real if admits(c, corpus))
        rows.append(
            {
                "min_length": min_len,
                "false_ground": false_ground,
                "false_ground_rate": round(false_ground / foreign_total, 4)
                if foreign_total
                else 0.0,
                "true_ground": true_ground,
                "true_ground_rate": round(true_ground / own_total, 4) if own_total else 0.0,
                "true_per_false": round(true_ground / false_ground, 2) if false_ground else 0.0,
                "foreign_claims": foreign_total,
                "own_failing_claims": own_total,
            }
        )
    return {"candidates": rows}


if __name__ == "__main__":  # pragma: no cover
    # Mirrors `qc.py`: without this, `python -m scribe.qc_survey` imports the module, runs
    # nothing and exits 0. The CLI lives in `qc_survey_cli` so importing this as a library
    # pulls in no argparse machinery.
    import sys

    from .qc_survey_cli import main

    sys.exit(main())
