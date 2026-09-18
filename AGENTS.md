# AGENTS.md — scribe

Guidance for agents working in this repository.

## What this is

A deterministic extractor that turns a Claude Code JSONL transcript into a structured event
log, plus a schema'd summarizer that consumes it. It replaces a pipeline that sent its model
6.5% of each session and then asked it to name the tools, files and commands it had never
been shown.

## Invariants — do not weaken these without reading why they exist

**1. `scribe.extract` is stdlib-only and offline.**
No runtime dependencies, no network, no imports that could acquire either. It is the one
module that reads raw transcripts, which are the least trustworthy input in the system.
Summarizer dependencies belong in an extra, not in `[project.dependencies]` — as
`[telemetry]` does. The invariant is enforced by `tests/test_stdlib_only.py`, which walks the
package's imports; this is also why the `SPAN_EXTRACT` span wraps the *call* to `extract()`
in `pipeline.py` rather than living inside the extractor.

**2. Transcripts are read-only.**
Never write to `~/.claude/projects/*/*.jsonl`. They are the only durable record of what
happened and Claude Code already expires them after 30 days. `test_extract_never_writes_to_the_transcript`
asserts this on content *and* mtime; if it becomes inconvenient, the code is wrong.

**3. Every string entering the event log goes through `Redactor`.**
Not "most strings" — every one. The failure mode is not a missing redactor, it is a redactor
that is present, correct, and simply not on the path a newly-added field takes.
`test_contract.py` walks the entire rendered document rather than checking named fields,
precisely so a new field cannot quietly bypass it. If you add a field, add it to a fixture.

**4. Redact before truncating.**
`Redactor.digest()` scrubs the full string and then cuts it. Reversing that leaves the
leading half of a token in the output, which is a leak, not a near-miss.

**5. `secrets_redacted` is a count, and it must stay one.**
"No secret in the output" is also satisfied by an empty output. Any test of redaction needs
both halves: nothing sensitive present, *and* a positive count proving the filter ran.

**6. The byte-budget ladder never removes an event and never touches text.**
Order: result digests, then argument digests, then tighten targets. Targets are tightened,
never emptied — for a Bash event the target is the command line. If the floor is still over
budget, set `budget_exceeded` and return the document oversized; do not strip further. A
stripped log is indistinguishable from a session that did nothing.

**7. Turns are keyed on the user record's `uuid`.**
Not on `(session_id, HH:MM)`. That collided 62 times across 3,539 blocks in the pipeline this
replaces.

**8. A session declared complete can come back.**
Completion is inferred from an idle timer, because the Stop hook fires per *turn* and there is
no session-end signal. Claude Code appends to an existing transcript when a session resumes,
so state records how far the file was READ (`last_offset`), not merely that it was handled.
`upsert_observed` must never regress that offset: an observation says what is on disk, not
what has been processed, and conflating the two makes a rescan re-summarize finished work.

**9. Output ends with a newline.**
A missing one glued ~1,030 headings together in the memory files downstream.

**10. The journal preview must stay byte-identical to `tests/reference/recent_memory_preview.awk`.**
That file is the incumbent consumer's parser, extracted verbatim, and it is evidence rather
than an implementation — do not tidy it. `scribe.journal.preview` is a port of it, and the
failure it guards against is silent: the hook keeps firing, the file keeps being written, and
the injected block is empty because the content stopped parsing. A test that asserted scribe
wrote a file to a path would pass on every one of those days.

**11. Never re-derive a cap from output the cap already filtered.**
`done`'s written lengths read min 1 / median 14 / p99 37 / max exactly 40 / zero above, which
looks like comfortable headroom under a cap of 40. It is right-censoring: everything over the
cap was rejected and is absent from the sample, so the survivors top out at the cap *because*
it is the cap. The failure log is the uncensored view, and it is still a floor rather than a
ceiling — `json_schema` declares `maxItems`, and a declared cap makes the model shed items
rather than exceed them, so the output distribution is a function of the number you are trying
to choose. Measure the **input**. This cost 30 sessions their digest once already
(vikunja#872), and the reassuring version of the data is what is easy to find.

**Measuring the input is necessary and not sufficient — the denominator has to be right too.**
This invariant used to end "`MAX_DONE_ITEMS` is set above `rollup.commands`' observed maximum",
and `rollup.commands` was the wrong denominator. Measured 2026-09-18 across 479 written blocks
paired with their own event logs, `done` exceeded the session's command count in **45%** of
them, reaching 51x; the worst cases are sessions with ONE bash command beside 19–27 MCP calls
and up to 21 file writes. `done` describes work, and on this fleet the work is not bash. The
denominator that holds is `stats.tool_events` — 0 of 472 blocks exceeded it. So an input
measurement against a quantity that does not actually bound the field reads exactly like a
sound one, and it survived three builds here.

**And a global derived from any snapshot decays.** `MAX_ROLLUP_ITEMS` was set above an
observed ceiling of 76 tickets; 38 event logs later the ceiling was 104 and a faithful digest
had been discarded (vikunja#901). The durable form is `schema.caps_for`, which reads the bound
off the session's own log — the rollup is known before the model is called. The constants
survive as FLOORS under the derived value and as the no-log fallback, which is why they are
still here and why lowering one is still forbidden. Re-measure with `scribe cap-survey` rather
than by hand.

**12. A block that is not a digest must say so in its anchor, and must stay replaceable.**
`render_failure`'s placeholder and the contamination guard's suppression note both occupy a
turn uuid like a real digest. Before `provisional:` existed, that made them permanent: the
session retried, the model produced a digest, and `append_block` refused it as a duplicate.
A final block is never overwritten; a provisional one always is. Note how this coexists with
invariant 8 — `mark_provisional` *does* advance `last_offset`, because leaving it behind is
what re-offered a failing session on every sweep forever. What says the session is not done is
the absence of its turns from `processed_turns`, not the offset.

**13. A counter that can only be inferred from a subtraction will not be noticed.**
`written == summarized + suppressed + placeholder` exists because "13 lost sessions" once read
as a clean backfill. The same mistake recurred one term along: a digest produced and *not*
written showed up as `summarized` with `written: False`, and nothing subtracted those either.
It has its own counter now (`discarded`). If you add an outcome, give it a name in the totals.

**A name in the totals is not enough if nothing prints it.** `truncated` and `truncated_items`
were aggregated correctly by `summarize_run` from the day they were added, and `run_cli`'s
human branch printed neither — only `--json` did, and the cron does not pass it. `grep -c
truncated ~/.pm2/logs/scribe-out.log` returned 0 for the whole period, so vikunja#887 asked for
a number to be allowed to accumulate and looked at, and it had been accumulating into nothing.
Give the counter a name, print it on the surface the operator actually reads, and keep the
per-field breakdown: a scalar says a cap fired, and only the breakdown says which one.

## Porting notes

`_INJECTED_PREFIXES` and the `isMeta` guard are ported verbatim from
`memsearch/plugins/claude-code/hooks/parse-transcript.sh`. They are a forge divergence from
upstream and they are why template placeholders stopped leaking into memory files. They look
redundant with each other and are not: `isMeta` covers skill loads, the prefix list covers the
two known shapes that arrive without it.

`detect_contamination()` and `build_fallback_note()` are to be ported from
`host-forge-scripts/scripts/memsearch-summarize.py` in Phase 3. Make scribe's copy the shared
one rather than a third duplicate.

## Testing

```bash
.venv/bin/python -m pytest -q --cov --cov-report=term   # 90% floor, enforced in CI
.venv/bin/ruff check src/ tests/ && .venv/bin/ruff format --check src/ tests/
python tests/check_gitleaks_gate.py                     # proves the secret gate can fire
```

**Run the gate prover against the gitleaks version CI pins**, not the distro binary that
happens to be on the forge host. Rulesets differ between versions and the difference is not
academic: gitleaks 8.28.0 allowlists AWS's documented example key `AKIAIOSFODNN7EXAMPLE` and
older versions do not, so a probe built on it passed locally and failed in CI. The prover
prints both versions and warns on a mismatch. Get the pinned one with the same commands the
workflow uses.

**Prove a new gate red before trusting it green.** The pattern used here: copy the tree to a
temporary directory, neuter the function body while keeping its signature, and confirm the
suite fails. Mutate a copy, never the working tree — an interrupted run leaves a survivor
behind, and a silently-neutered redactor in a repo about redaction is the worst possible
place for one.

**Force `PYTHONPATH` when you do.** Copying the tree copies `.venv`, whose editable install
holds an ABSOLUTE path back to the original checkout, so `/tmp/mutant/.venv/bin/pytest`
happily tests the unmutated original and every mutant survives. That happened here: four
mutations in a row reported the exact baseline pass count, which is the tell — a mutation run
whose failure count never moves is a broken harness, not a strong suite. Run
`PYTHONPATH=/tmp/mutant/src /tmp/mutant/.venv/bin/python -m pytest`, and assert your patch
anchor matched, because a `replace` that hit nothing looks identical to a survivor.

Verified this way at Phase 1: neutering `Redactor.scrub` fails 17 tests, skipping `tool_use`
blocks (the original defect) fails 9, and removing the `isMeta` guard fails 7.

## Test fixtures contain synthetic credentials

They have to — the component under test is a redactor. Every literal carries `FAKE` or
`NOTREAL`, and `.gitleaks.toml` allowlists **by that marker, not by path**. Path-scoped
allowlisting was tried first and is a hole: gitleaks ORs `paths` with `regexes`, so any
finding in an allowlisted file is excused regardless of shape. Never paste a real credential
into a fixture; the repo is private today and intended to go public.

## Repo conventions

- Feature branches and PRs. `~/repos/personal` carries `branch_required: true`.
- Baseline tier per `repo-conform.py`. Do not add flagship requirements ad hoc.
- Actions pinned to a commit SHA, never a tag.
