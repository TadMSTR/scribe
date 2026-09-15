# The QC regression set

The groundedness gate is tuned against two things: a synthetic fixture that lives in this
repo and runs in CI, and a replay against the real session corpus that does not and cannot.
This file records the second, because the fixture alone does not tell you where the numbers
came from.

## Why the committed fixture is synthetic

The plan for `scribe-shadow-fixes-2026-09` asked for the five sessions of the first `--live`
shadow run to be added as a regression fixture, naming them: `2efed8b7`, `f4b2a2e3`,
`a6b9ac3c`, `aa6634a7`, `5ceda13a`.

They are not committed. Those transcripts are roughly 12 MB of real working sessions, they
are mode 0600 deliberately, and they are the *unredacted source* of the 146 secret-shaped
strings the extractor scrubbed on that run -- redaction happens downstream of the file.
Committing them would move all of that into git history and onto every runner that clones
the repo, and no amount of care afterwards takes it back out.

`tests/fixtures/transcript-qc-regression.jsonl` reproduces the three structural shapes the
live run exposed, which is what the tests actually need:

| Shape | In the fixture | Live original |
|---|---|---|
| A bare id that is not an identifier | `{"id":417,"identifier":"#398"}` and `/tasks/417`, never `#417` | `f4b2a2e3` |
| A `#N` the ref-collector drops | `#655/#639` -- the `/` defeats `_TICKET_RE`'s lookbehind, so `#639` never reaches `rollup.tickets` | `aa6634a7` |
| Digest vocabulary read as commands | `parked`, `mistral-small-latest`, `credentials: source: env`, `max_retries=2`, `popen([...], env=child_env)` | all five |

## Replaying the real corpus

The evidence from the live run is preserved outside the repo, under the build plan's
`evidence/` directory. The digests as written are in `digest-as-written-WRONG-DATE.md`;
because every block carries its own anchor, the set can be re-graded against freshly
extracted logs at zero cost and with no model call:

```python
from scribe.extract import extract
from scribe.qc import check_digest
from scribe.writeback import ANCHOR_RE

text = open(".../evidence/digest-as-written-WRONG-DATE.md").read()
marks = list(ANCHOR_RE.finditer(text))
for i, m in enumerate(marks):
    end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
    print(m.group(1)[:8], check_digest(text[m.end() : end], extract(m.group(3))).ok)
```

**The terminator no longer has to be trimmed by hand.** This snippet used to cut each block
at `TERMINATOR_RE` with the note *"the pipeline grades BEFORE this is appended; leaving it in
flags `/scribe` as a path"*. That was a workaround for a real defect rather than a quirk of
replaying: `<!-- /scribe turn:… -->` is on every block scribe writes, `_PATH_RE` read `/scribe`
out of it as a file path, and `/scribe` is in no event log — so `scribe qc --digest <any file
scribe wrote>` was structurally incapable of passing. The workaround made this replay work
while leaving the actual CLI broken, and it is exactly why the defect survived: the one place
someone hit it, they stepped around it and wrote the step-around down. `_claims` now strips
scribe's own markers, and `test_the_terminator_is_not_read_as_a_file_path` pins it.

## The numbers

| | before | after |
|---|---:|---:|
| findings across the 5 sessions | 73 | 1 |
| `qc_passed` | 0 / 5 | 4 / 5 |
| `groundedness_rate` | 0.00 | 0.80 |

The one surviving finding is `#417` in `f4b2a2e3`, and it is the reason the gate exists. That
digest wrote *"Filed ticket #417"* for something its transcript only ever calls `id 417`.
Vikunja's id 417 is identifier **#398**, so `#417` is a real ticket -- a different one. It
reads as an entirely ordinary cross-reference and no reviewer would catch it by eye.

**4 of 5 is the target, not 5 of 5.** A gate tuned until everything passes has been tuned
into uselessness, so both directions are asserted:
`test_the_vocabulary_of_a_good_digest_is_not_reported_as_hallucination` and
`test_a_vikunja_id_rendered_as_an_identifier_is_still_caught`.

## What changed, and why it is not just a loosening

Grounding is checked against `EventLog.grounding_text()` -- the serialized event log, which is
**the same document the model was shown** -- by three widening routes: the derived category
set, verbatim presence, then all-token co-occurrence within a bounded window for a composed
span.

The category sets (`rollup.commands`, `files_read`, ...) were never the model's input; they
are a summary of part of it. A claim absent from them but present in the corpus was always a
true claim wrongly flagged. Asking "was the model shown this?" is the definition of
groundedness, so this is a correction rather than a relaxation -- and the checks that must
still bite do:

- `systemctl restart nginx` is three ordinary words and is rejected, because the composed-span
  route requires *all* tokens **and** requires them to co-occur within 120 characters.

  The proximity half was missing in the first version and the audit caught it: scattered words
  alone grounded the claim, and the commit message asserted otherwise on the strength of a
  fixture where the words simply did not appear. Measured across the five real sessions with
  fabricated commands built only from words each session genuinely contains, unbounded overlap
  grounds 9 of 35 and a 120-character window grounds 1.

  That remaining one is real and is left alone deliberately: `docker compose down` against a
  session that plausibly did discuss exactly that. Shrinking the window until nothing fails
  would be the original defect wearing the opposite sign. A fabrication whose words genuinely
  appear together in the log still passes, and that is the irreducible cost of admitting
  compressions at all.

  "Together" means the width of the smallest stretch of corpus containing every token, not
  distance from an anchor token. The anchored form is asymmetric — with tokens A and C 200
  apart but each within 120 of B, anchoring on B admits the claim and anchoring on A rejects
  it — and the anchor was chosen by iterating a set, so the verdict moved with the
  interpreter's hash seed. `test_co_occurrence_is_a_span_not_a_distance_from_an_anchor` and
  `test_the_verdict_does_not_depend_on_token_order` pin that.
- Tickets match exactly and their corpus check is boundary-anchored, so `#65` cannot ride on
  a log that mentions `#655`.
- File paths and tickets keep the narrow treatment. That is where the one real finding came
  from.

## A related fix in the same area

`scribe.qc_cli._log_from_dict` rebuilt only the rollup and an event skeleton, dropping
`user_text`, `assistant_text` and `result_digest`. The corpus it produced was therefore much
thinner than the pipeline's, and the *same* digest graded against the *same* log was stricter
through `python -m scribe.qc` than through the pipeline -- which matters because the CLI is
what CI runs. It now rebuilds from `__dataclass_fields__`, so a new field cannot silently
reintroduce the gap, and `test_the_cli_and_the_pipeline_grade_a_digest_identically` pins them
together.
