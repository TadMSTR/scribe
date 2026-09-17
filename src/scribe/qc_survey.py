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

import ast
import random
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .eventlog import EventLogError, eventlog_path, load_eventlog
from .extract.models import EventLog
from .qc import (
    ADJACENCY_WINDOW,
    MIN_PREFIX_SEGMENTS,
    MIN_TAIL_SEGMENTS,
    PATH_UNGROUNDED,
    Report,
    check_groundedness,
    composition_split,
    grounding_terms,
    path_claims,
    path_grounded,
    path_literal,
    path_segments,
)
from .writeback import paired_blocks

#: The claim is present in the corpus exactly as written. A finding in this bucket is a
#: contradiction — the gate rejected something its own rule accepts — so it is carried as an
#: invariant rather than an expected outcome.
VERBATIM = "verbatim"
#: `~/x` where the log holds `/home/ted/x`, or the reverse. One substitution, no composition.
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
    """One rejected path claim, and the strongest available explanation for its rejection."""

    claim: str
    bucket: str
    session_id: str
    digest: str
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
            )


def rejected_paths(report: Report) -> list[str]:
    """The path claims a report rejected, recovered from the findings themselves.

    Read back out of the gate's own output rather than recomputed, so this cannot disagree with
    what the gate did. `PATH_UNGROUNDED` is shared with `check_groundedness` for the same reason.
    """
    out = []
    for f in report.findings:
        if f.check == "groundedness" and f.detail.startswith(PATH_UNGROUNDED):
            out.append(ast.literal_eval(f.detail[len(PATH_UNGROUNDED) :]))
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
    buckets: Counter = field(default_factory=Counter)
    verdicts: list[ClaimVerdict] = field(default_factory=list)

    @property
    def failure_rate(self) -> float:
        return self.blocks_failing / self.blocks if self.blocks else 0.0

    def control_set(self) -> list[ClaimVerdict]:
        """Every claim in `ABSENT` — what must still fail after any tolerance lands."""
        return [v for v in self.verdicts if v.bucket == ABSENT]

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
            "buckets": {b: self.buckets.get(b, 0) for b in BUCKETS},
            "suffix_segments": dict(
                sorted(Counter(v.suffix_segments for v in self.verdicts).items())
            ),
            "verdicts": [
                {
                    "claim": v.claim,
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

        for claim in rejected:
            bucket, suffix_n, split = classify_path_claim(claim, allowed, corpus, home=home)
            out.buckets[bucket] += 1
            out.verdicts.append(
                ClaimVerdict(
                    claim=claim,
                    bucket=bucket,
                    session_id=block.session_id,
                    digest=block.digest,
                    suffix_segments=suffix_n,
                    split=split,
                )
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


if __name__ == "__main__":  # pragma: no cover
    # Mirrors `qc.py`: without this, `python -m scribe.qc_survey` imports the module, runs
    # nothing and exits 0. The CLI lives in `qc_survey_cli` so importing this as a library
    # pulls in no argparse machinery.
    import sys

    from .qc_survey_cli import main

    sys.exit(main())
