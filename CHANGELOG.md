# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — scribe-schema-caps-2026-09

**#849 — the digest item cap was a rule the model was never given.** `MAX_ITEMS = 40` applied
to all six list fields and *raised* where its neighbour `MAX_ITEM_CHARS` truncates, while
`prompt.py` asked for "ticket references that appear in the log" and `json_schema()` emitted no
`maxItems` at all. Session `aa6634a7` holds 58 tickets in its rollup; the model listed 53 of
them and was rejected for it. The violation was retried like an API error, all three attempts
failed identically at ~50k input tokens each, and `render_failure` wrote a placeholder that
`summarize_run` then counted under `written`. 13 of 431 sessions are over the cap on `tickets`
alone, so a 429-session backfill would have silently placeholder'd roughly 3% of itself.

- **The cap is declared.** `json_schema()` emits `maxItems` per field and the system prompt
  states the limits and what exceeding them costs. Measured live on `aa6634a7`, three samples
  per arm: with the cap declared at 40 the model emitted 27, 35 and 28 tickets — under the
  limit every time, where before it was rejected every time. Declaring the rule is what fixed
  the failure; the validator is now a backstop.
- **The caps are per field.** `tickets` and `artifacts` come from the extractor's own rollup
  and have a knowable ceiling — measured over 432 sessions they top out at 76 and 58, with
  **zero** sessions over 80 in a month — so `MAX_ROLLUP_ITEMS = 100`. `done`, `found`,
  `decisions` and `open_items` are unbounded model prose (`rollup.commands` reaches 187, with
  104 sessions over 40) and keep `MAX_ITEMS = 40`, which is what a runaway guard is for. One
  constant was guarding both classes and only one of them can run away.

  **This turned out to be the phase that preserves the content, not defence in depth.** At a
  declared cap of 40 the model sheds about half the rollup's tickets and *which* half varies
  run to run (27 / 35 / 28 from identical input). At 100 it lands on 53 every time. The plan's
  expectation that the model drops "the least relevant" items is not established by this — what
  is observable is that the choice varies. A cap set below what the log holds is lossy whichever
  mechanism applies it, which is the argument for setting a rollup-backed cap above the corpus
  ceiling rather than near it.
- **A deterministic schema violation is no longer retried.** `SchemaError` carries `retryable`,
  defaulting to **True** — malformed JSON and missing fields are sampling noise and a fresh
  attempt often fixes them, which is the module's own long-standing argument. Only the cap
  violation sets it False: the count follows from the input, so attempts two and three buy an
  identical rejection. The live runs now show one provider call (61,309 input tokens), not three.
- **A placeholder is visible in the run totals.** `SessionResult` gains `placeholder` and
  `suppressed`; `summarize_run` reports both, `written` keeps its meaning — a block reached the
  file — and the totals now satisfy `written == summarized + suppressed + placeholders`.
  `run_cli` prints a loud line when `placeholders` is non-zero. After the first two changes the
  validator will rarely fire, which is correct but means it stops being the drift detector;
  this is what keeps that observable.

Tests 402 → 430. Each change was mutation-checked on a throwaway copy rather than assumed:
neutering the counter, dropping the CLI line, zeroing the total, removing the retry
short-circuit, removing `maxItems`, and collapsing the two caps back into one each turn the new
tests red. Removing `maxItems` also empties the parametrised cap test's parameter set, so it
would report green while checking nothing — an explicit non-vacuity guard catches that case.

Nothing here is deployed. The backfill stays blocked on widening the shadow, and vikunja#843
stays open until cutover.

### Fixed — scribe-shadow-fixes-2026-09

Both defects came out of the first `--live` shadow run, and neither was reachable before it:
they live past the `dry_run` short-circuit in `pipeline.py`, so the extractor work and the
367 tests could not have surfaced either. A `--dry-run` mode exercises the extractor, not the
writer or the grader.

- **#847 — digests were filed under the wall clock, not the session's own date.** `when` was
  `datetime.now(UTC)`, and it decides the daily filename, the `## Session HH:MM` heading and
  the `### HH:MM` heading alike. In steady state the clock and the session agree, so nothing
  looked wrong; a backfill is the case where they do not, and all 429 digests would have
  landed in one file named for the run day with the real dates unrecoverable. Now derived
  from the session — `started_at`, then `ended_at`, then the clock. The start is the anchor
  because a midnight-spanning session files all of its work under whichever day it names, and
  sessions end just past midnight far more often than they begin just before it. Verified on
  the five live sessions: four correctly-dated files, where previously all five collapsed
  into one.
- **#848 — the groundedness classifier over-fired, 70 false positives in 73 findings.** Every
  backticked span was treated as a `command` requiring a match in `rollup.commands`, so
  `parked`, `mistral-small-latest`, `credentials: source: env` and `set -euo pipefail` all
  read as fabricated commands and `groundedness_rate` reported 0.00 for five good digests.
  Spans are now classified by shape (command / code / identifier), and grounding is checked
  against `EventLog.grounding_text()` — the same document the model was shown — by three
  widening routes: the category set, verbatim presence, then all-token co-occurrence within a
  bounded window for a composed span. The category sets were never the model's input, so a claim absent from them
  but present in the corpus was always a true claim wrongly flagged. Findings across the five
  sessions: 73 → 1.
- The one surviving finding is the reason the gate exists, and it **reproduced on a fresh
  model call**: a digest wrote `#417` for something its log only ever calls `id 417`, and
  Vikunja's id 417 is identifier #398 — a real but unrelated ticket. It now carries a message
  naming the conflation rather than a generic miss.
- **F-01 (Medium, scribe-shadow-fixes-2026-09 audit)** — the composed-span route grounded a
  claim if every token appeared *anywhere* in the ~180 KB corpus, with no proximity
  requirement, so a fabricated command passed whenever its individual words occurred in
  unrelated sentences. The commit introducing the check asserted `systemctl restart nginx`
  "is still rejected", which was true only of the one fixture where those words are absent —
  a property of the test data, not of the code. Tokens must now co-occur within 120
  characters. Measured across the five real sessions: fabrications grounded by this route
  drop from 9 of 35 to 1 of 35, while both genuine compressions still pass. Co-occurrence is
  defined as the width of the smallest stretch of corpus containing every token, **not** as
  distance from an anchor token — the anchored form is asymmetric (with A and C 200 apart but
  each within 120 of B, the verdict depends on which is picked) and the anchor came from
  iterating a set, so the gate answered differently on different interpreter hash seeds. That
  was caught by its own regression test failing intermittently, and a non-deterministic gate
  cannot be falsified at all.
- **`python -m scribe.qc` graded more strictly than the pipeline** on identical inputs.
  `_log_from_dict` rebuilt only the rollup and an event skeleton, dropping `user_text`,
  `assistant_text` and `result_digest`, so its corpus was far thinner — and the CLI is what
  CI runs. It now rebuilds from `__dataclass_fields__`, and a test pins the two paths
  together.

### Added — Phases 2 to 5

- **Session discovery** from the filesystem, with quiet-period completion and durable state
  in SQLite. Idempotency is keyed on the turn uuid; a resumed session is detected by growth
  past the recorded read offset rather than by its status.
- **Config** with lazy credential resolution. A literal `api_key` is refused at load.
- **Summarizer** over a pluggable provider abstraction (OpenAI-compatible HTTP, `claude -p`),
  producing schema-validated JSON that is rendered to markdown by deterministic code. A
  schema violation is retried like an API error; contamination is retried with a reminder
  and then falls back to a marked suppression note.
- **Write-back** mirroring the live memory layout, newline-terminated, refusing to append a
  turn that is already present.
- **Spend metering** in the exact record shape `memsearch-spend.sh` already parses, and OTel
  spans reusing the `memsearch.summarize*` names so existing dashboards keep working.
- **QC gates** that assert against the event log rather than against another summary:
  groundedness of every path, command, ticket and tool a digest names, plus an
  event-coverage floor. `python -m scribe.qc` exits non-zero on a failure.

### Security — scribe-2026-09 audit remediations

- **F-01 (Medium)** — a PEM/private-key block whose closing marker was cut by Claude Code's
  upstream output truncation was not redacted. The surviving fragment is bare base64 with no
  assignment syntax, so no other rule caught it either. Measured: 11,478 truncation markers
  across the 429-file corpus, so this was a routine shape rather than an edge case. Redaction
  now runs from a bare `BEGIN` marker to end-of-string when no `END` is found.
- **F-02 (Medium)** — bare `auth` was matched by the header rule but not by the
  JSON/YAML/env assignment rules, so `{"auth": ...}`, `auth: ...` and `AUTH=...` all leaked.
  Added, with the `authentik`/`authorized` carve-out asserted rather than assumed.
- **F-03 (Low)** — added Slack's `xoxe-` token-rotation prefix. (The audit also cited
  `xoxe.xoxp-1-…`; that form was already covered by the embedded `xoxp-`.)
- **F-04 (Low)** — a torn append is now **detectable**. Blocks end with a terminator and
  `existing_turns` counts a turn only when anchor and terminator are both present, so a
  crash mid-write is retried instead of being recorded as done by both dedup guards and lost
  permanently. Chosen over read-modify-replace of the whole daily file per append: the harm
  was never the tear, it was that nothing could see it.
- **F-05 (Info)** — the contamination-guard fallback re-check now runs the residue detector
  as well as placeholder and signature. Overlap remains excluded, with upstream's reasoning.
- **Defence in depth** — the rendered digest is re-scrubbed immediately before write-back.
  It is also a *detector*: the digest derives from an already-scrubbed event log, so any hit
  means extraction missed something, and it is reported as an error rather than quietly
  cleaned up. This protects the on-disk digest only — the outbound call happens earlier, so
  it is not a substitute for the extraction-layer fixes above.

### Planned

- **Phase 6** — shadow run alongside `memsearch-summarize`, then a cutover decision.

### Notes

- `memsearch-spend.sh` does **not** only meter compact, as the build plan states —
  `SUMMARIZE_LOG` is already in its stream list. The real defect is a path mismatch: the
  producer writes to the PM2 stdout log while the meter reads an empty file, so reported
  spend was about half the real figure. Filed as vikunja#846; scribe writes to a dedicated
  log and is unaffected.
- `python -m scribe.qc` initially exited 0 while doing nothing, because `qc.py` had no
  `__main__` guard — a gate that could not fail, which is the defect the gate exists to
  catch. Now covered by a subprocess test rather than by calling `main()` directly.

## [0.1.0] - 2026-09-13

Phase 1 — the deterministic extractor. No LLM, no network, no runtime dependencies.

### Added

- `scribe.extract` — turns a Claude Code JSONL transcript into a structured event log.
  Captures every `tool_use` and `tool_result` as a bounded event with tool name, kind,
  target, argument digest, exit status and result digest. The pipeline this replaces
  captured none of them.
- Per-turn and per-session rollups derived in code rather than inferred by a model: files
  read, files written, commands run, MCP tools called, agents invoked, URLs, Vikunja
  tickets, pull requests, git refs and failures.
- Redaction at extraction time, counted and reported as `stats.secrets_redacted`. Covers
  vendor token prefixes, PEM blocks, JWTs, bearer headers, `-H`/`--header` values and
  assignment shapes — the last of which covers `.env` bodies and `docker inspect` env
  arrays without special-casing either.
- Byte-budget degradation with a fixed priority order, and `budget_exceeded` for sessions
  no ladder can bring under budget.
- `python -m scribe.extract` CLI with `--json`, `--indent` and `--max-chars`.
- Test suite of 76 tests at 94.6% coverage, including a whole-document scan asserting no
  planted secret survives on any of eight distinct paths into the log.
- `tests/check_gitleaks_gate.py` — proves the secret-scanning gate can actually fire before
  a clean result is believed.

### Measured

Against the 429-transcript, 596 MB corpus on forge:

- 47,270 tool events extracted; 0 orphaned results, 0 unparsable records.
- **8,015 secret-shaped values redacted across 324 of 429 transcripts (76%)** — the case for
  redaction-at-extraction, which had been argued from three past incidents rather than from
  a measurement.
- At the default 200,000-character budget: 74% of sessions need no degradation, and 5 exceed
  it on user and assistant text alone.
- Whole-corpus summarization cost at `mistral-small` pricing works out to **$2.14/month
  undegraded**, against a $30 allowance. The byte budget is a context-window control, not a
  cost control.

### Notes

- The reference session's text-only floor (33,989 characters) reproduces the old pipeline's
  entire output, confirming the 6.5% measurement from the extractor's own numbers.
- Vikunja tickets and pull requests are separated in the rollup. Folding them together put
  `#1`–`#12` at the top of the frequency table, every one a PR number. Residual ambiguity
  remains for a bare `#N` in a markdown table.

[Unreleased]: https://github.com/TadMSTR/scribe/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/TadMSTR/scribe/releases/tag/v0.1.0
