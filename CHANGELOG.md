# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`done`'s item cap was below its real ceiling, and cost 30 sessions their digest**
  (vikunja#872). It was the only field ever to violate a cap — 30 rejections at 41–67 items
  against a limit of 40 — and a cap violation is `retryable=False`, so each one became a
  placeholder immediately. `MAX_DONE_ITEMS = 200`, chosen against the *input* ceiling
  (`rollup.commands` reaches 187 across the 442 persisted event logs), not against the
  written digests: those top out at exactly 40 with nothing above, which reads as comfortable
  headroom and is right-censoring. 100 would have been 1.5x a censored number and below 38
  sessions' command counts.

- **The prompt's declared caps are generated from `_LIST_FIELDS`** rather than hand-written.
  The sentence and the validated constants were two copies of one fact; a prompt still saying
  40 while the validator permits 200 declares a limit the model complies with and is never
  rejected for, so it would have written short digests silently, every run.

- **The contamination guard fired on any angle-bracketed word** (vikunja#868). It was the
  only detector ever to fire in production — 51 fires, 22 sessions suppressed — and every one
  was a session that talked *about* template syntax rather than reproducing it. It now fires
  on a placeholder in *value position*: strip the placeholders from a line and judge what is
  left. `build_fallback_note` keeps the broad pattern deliberately, and is tested for it.

- **A digest that was never written is no longer terminal.** A suppression went straight to
  `summarized`; a placeholder set `failed` but left `last_offset` behind, so the byte check
  re-offered it on every sweep — one session reached 12 summarization calls and could never
  have landed, because `append_block` refused the result. Both are now
  `STATUS_PROVISIONAL`: offset advanced, turns unrecorded, retried a bounded number of times.

- **A provisional block is replaceable on disk.** The block's anchor records whether its body
  is final or a stand-in; `append_block` replaces a stand-in in place and still refuses a
  final turn, so a real digest is never overwritten. Absent attribute means final, so blocks
  written before this read correctly without being rewritten.

- **`sessions.session_id` is populated.** It was empty on all 441 rows — `scan` upserts from
  a filesystem stat and the id is inside the transcript — which made it look like a usable
  join key when it matched nothing.

- **`recover` stamped anchors by uuid text rather than by the block it found**
  (audit finding, Medium). `ANCHOR_RE.sub` rewrote every anchor-shaped match carrying a found
  block's uuid, including one quoted inside another block's body. `stamp` now splices the exact
  byte spans `scan_corpus` recorded, in reverse order, re-checking each span before writing.

- **An anchor or terminator is recognised only alone on its line.** A digest body can contain
  this syntax — one live digest quotes the terminator, because the session was about the
  format. Bounding the terminator search at the next anchor (the first half of this fix) made
  a *quoted* anchor able to hide the block containing it: the containing block's real
  terminator fell outside the bound, so a complete, final digest read as torn and invited a
  duplicate over the top of it. Line anchoring separates structure from quotation, and matches
  the live corpus exactly: 444 of 444 anchors and 444 of 445 terminators are alone on theirs.

### Added

- **`scribe recover`** — reopens sessions whose digest was never written, for blocks that
  predate the provisional marker. Identifies them by body rather than anchor, stamps the
  anchors, and clears the state. Dry by default, idempotent, joins on `transcript_path`.

- **A `discarded` run counter** — a digest produced and then not written. The term missing
  from `written == summarized + suppressed + placeholder`, and the reason 30 lost sessions
  reported as clean. It should be unreachable; it is counted so that is checkable.

## [0.1.0] — 2026-09-15

**scribe's first tagged release.** No tag existed before this one, so there is no predecessor
to be consistent with — `0.1.0` is what `pyproject.toml` has said throughout, and the seven
merged PRs below it *built* that version rather than changing a released one. Bumping would
have implied a public 0.1.0 that never existed.

**Why this exists, in one measurement.** The pipeline it replaces showed its model **6.5% of
a session** — 33,995 of 523,546 characters on the reference transcript — and then asked it to
name the tools, files and commands it had never been shown. 176 tool calls and 176 tool
results were dropped before the model saw anything. scribe is the part that recovers them:
a deterministic, stdlib-only, offline extractor that turns the full transcript into a
structured event log, a schema'd summarizer over that log, and a QC gate that grades the
result against the log rather than against another summary.

Shipped: five verbs (`extract`, `run`, `journal`, `qc`, `events`), three retention tiers with
a lookup path between them, redaction that is counted rather than assumed, and optional OTel.
One runtime dependency (`httpx`). 612 tests.

**Nothing is open against this release.** vikunja#850 and #856 are both closed by it; #863
(registration and cutover) is a separate build and is deliberately not part of this one.

### Added — telemetry that actually emits (vikunja#336, #579)

**Two of the three declared span names had no call site, and OpenTelemetry was not installed
at all.** `SPAN_REJECTED` and `SPAN_EXTRACT` were constants nothing referenced, there was no
`[telemetry]` extra, and no `OTEL_*` variable was set — so scribe emitted zero spans. That is
defensible for a shadow component, but the span names were deliberately kept identical to
`memsearch-summarize`'s so existing SigNoz dashboards would survive the cutover, and those
dashboards query `memsearch.summarize_rejected`. Shipping as-is meant two of three going dark
the moment the incumbent stopped.

- **A `[telemetry]` extra**, pinning the OTLP **gRPC** exporter. gRPC because forge's
  `OTEL_EXPORTER_OTLP_ENDPOINT` is `http://127.0.0.1:4317` — the collector's gRPC port — and
  because the three sibling venvs on forge with working telemetry all carry proto-grpc.
- **`SPAN_EXTRACT` wraps the extraction call in `pipeline.py`**, not the extractor itself.
  `scribe.extract` is asserted stdlib-only; importing a telemetry module into it would make
  that invariant depend on what `telemetry.py` happens to import.
- **`SPAN_REJECTED` fires once per rejected attempt** on the contamination path, carrying
  `signal`, `attempt` and `fallback` — matching the incumbent's `_record_rejection` so the
  dashboards keep grouping the same way. Inside the retry loop rather than after it: a session
  rejected on attempt 1 and accepted on attempt 2 is a real event, and "how often does the
  reminder rescue a run" is what the dashboard is for.
- **`setup_tracing()` — the step whose absence was invisible.** `trace.get_tracer()` returns a
  *no-op* tracer until an SDK `TracerProvider` is installed, so installing the packages and
  setting the endpoint was still not enough: every call site would have looked correct while
  the process emitted nothing. This is the shape of vikunja#320. `run` installs the provider
  and flushes it in a `finally`, because `BatchSpanProcessor` exports on a timer and a batch
  CLI can otherwise exit having dropped the whole batch.
- **The half-configured case is now loud.** Endpoint set and packages missing prints to stderr
  naming the install command. That is #336's own recommendation — it sat two months with the
  endpoint set and the extra uninstalled, and the only signal was one warning line. A warning
  rather than a fatal error, deliberately: scribe is a batch job over a corpus, and dying over
  an observability extra would trade a complete run for a complete outage.
- **Emission is asserted against a real exporter**, driving `setup_tracing()` itself rather
  than building a provider in the test — a test that builds its own would pass while
  production stayed dark. Verified by neutering the provider install and confirming both
  emission tests go red. **CI installs `.[dev,telemetry]`** and imports the exporter as an
  explicit step, so the test cannot silently `importorskip` in the one place that gates
  merges, and so the install is *run* rather than merely resolved (#578).

### Fixed — the post-render detector named a file it could not know was at fault (vikunja#856)

**A post-render redaction fire was reported as proof that extraction had missed something,
and told the reader to investigate `redact.py`.** It could not know that. Measured: two
`--live --limit 3` runs over the same three sessions gave `post_render_redactions` of 1 then
0 — same input, different outcome, so the trigger was the model's output, not the input
document. Separately, all three persisted event logs scrubbed field-by-field fired 0 times
across 1,797 string leaves.

The two causes were indistinguishable until #852 persisted the event log. Now they are
separable, so the code asks instead of asserting:

- **`classify_post_render` consults the persisted event log** and returns one of
  `extraction-miss`, `model-output` or `undetermined`, surfaced as `post_render_cause`. The
  three answers are not symmetric: one value found proves an extraction miss regardless of the
  others, a clean "absent" is only meaningful once a log has actually been read, and no log at
  all proves nothing either way.
- **The message branches with it.** The model-output case now says so explicitly and states
  that `redact.py` is *not* the file to look at.
- **`Redactor(capture=True)`** is the opt-in that makes this answerable. Off by default and
  never used by extraction: a capturing redactor holds the plaintext of what it matched, which
  is the thing the class exists to remove. The captured values never reach `result.errors`,
  the JSON report or a span attribute.
- **The detector still fires loudly, and the digest on disk was always correct.** Only the
  diagnosis changed.
- **The serialized-log trap is recorded as a test.** Scrubbing a *serialized* event log
  produces matches field-level scrubbing does not — the `envvar` rule's negated class stops at
  a quote character but knows nothing about JSON structure, so a match runs through a `", "`
  boundary and swallows its neighbours. Field-level: 0 fires across 1,797 leaves. Whole-file:
  7, all spurious. The tell asserted is that a whole-file scrub **corrupts the document**,
  which is stronger than the match length. Anyone auditing `eventlogs/` will reach for a file
  scanner first; this is the note saying why that is the wrong tool.

### Fixed — docs described a tool that had never been run (vikunja#850)

- `AGENTS.md` called the summarizer "(planned)". It shipped and has been shadow-run live.
- `README.md`'s Status said Phase 6 "has its runner but has not been run". It has been run
  repeatedly — 429 sessions dry, plus live runs behind #847, #848, #849 and #852. Status now
  states what remains and whose build it is (#863), rather than implying scribe is unfinished.
- A **Telemetry** section states the two-part deploy requirement plainly, because the extra
  and the endpoint are each useless alone and that is exactly what #336 and #579 each got
  wrong.
- The rest of the README was checked against the live CLI — all five subcommands' flags, the
  three-tier table and the drill-down commands — and is accurate.

### Security — post-audit remediation

Audit `scribe-release-readiness-2026-09`: **one Low, two Info, nothing at Medium or above.**

- **Low — the delimiter strip can mangle a `keyval` value.** Correct observation: that rule's
  value group is unconstrained, so an *unquoted* value that happens to begin and end with the
  same quote character loses those characters. Behaviour is **pinned by test rather than
  changed**, because the predicted consequence does not follow: `contains_value` does
  substring containment and the stripped value is by construction a substring of the
  unstripped one, so a mangle can never turn a match into a miss. Its only possible error is
  the conservative direction. The audit's alternative suggestion — scoping the strip to
  `json`/`envvar` — would have reintroduced the inverted verdict for `keyval`, and there is
  now a test asserting exactly that so it is not "fixed" later.
- **Info — `captured` relied on scope rather than an explicit clear.** Now cleared explicitly
  after classification. The plaintext was already provably unreachable; this makes the bounded
  retention window visible in the code instead of implicit.
- **Info — `pip` CVEs in the local dev venv.** Pre-dates this branch, is packaging tooling
  rather than a shipped dependency, and does not reach the release artefact. Carried into the
  deploy task so the new `/opt/venvs/scribe` starts current.

### Fixed — agent attribution truncated hyphenated names

**The writer and the reader disagreed about where a hyphenated agent's digests live.**
`_agent_from_project_dir` split Claude Code's flattened directory name at the first hyphen,
so `-home-ted--claude-projects-doc-health` resolved to `doc`. Three of forge's ten agents are
affected: `doc-health`, `helm-build`, `memory-sync`.

Raised as Low by the scribe-journal-feed audit, on the grounds that no colliding `doc/`
directory exists so it fails safe. That is true of the contamination risk and it is not the
live consequence. The truncation landed on the **write** side — `pipeline.py` builds the
digest path from this name — while the SessionStart hook resolves `$CLAUDE_PROJECT_DIR`,
which keeps its separators and yields `doc-health`. So those agents' digests were written to a
directory no reader would ever look in, and the injection would have been permanently empty
for them without ever erroring.

- **Attribution now comes from the session's `cwd`**, which is exact rather than heuristic: a
  path keeps its separators, so there is nothing to disambiguate. The parser was already
  reading that field into `log.cwd`; it simply was not used for this. Re-resolved after the
  parse, since `cwd` is not known before it.
- **One resolver, `agent_for_path`, shared by the extractor, discovery and the hook** — they
  did not merely agree before, they were different code, and the disagreement was silent.
- **The flattened form is resolved against the directory those names live in**, longest
  candidate first, falling back to the first segment. The encoding is genuinely lossy — a
  separator and a hyphen inside a name both become `-` — so `-...-projects-doc-health` is
  equally `projects/doc-health` and `projects/doc/health`, and the filesystem is the only
  thing that can say which.
- **The flattened form is checked first, and that is a form discriminator rather than a
  ranking.** A flattened transcript directory really does live at
  `~/.claude/projects/-home-ted--claude-projects-sysadmin`, so it satisfies the
  working-directory shape exactly; resolving `cwd` first would answer
  `-home-ted--claude-projects-sysadmin` — well-formed, and wrong. An existing test caught it.

No migration: no digests had been written for any affected agent.

### Added — memory-consolidation-2026-09 part 2

**The SessionStart injection is the only memory push surface that demonstrably reaches a
CloudCLI agent session, and the component feeding it is being retired.** Measured live
2026-09-15: a hook emitting `hookSpecificOutput.additionalContext` is surfaced to the agent;
the sibling hook emits a bare `systemMessage` and CloudCLI drops it for agent sessions. The
journal that injection reads is written by the memsearch Stop hook and rewritten by the
`memsearch-summarize` service. Removing those would not have failed loudly — `stop.sh`'s own
comment says the raw transcript block it appends "is only transient because
memsearch-summarize replaces it" — so the injection would have filled with raw transcript
rather than emptied.

- **`python -m scribe journal`** emits the hook payload for one agent, built from the two
  most recent `YYYY-MM-DD.md` digests under `<output_dir>/<agent>/`. `hooks/session-start.sh`
  is the wrapper to register; `SCRIBE_PYTHON` names the interpreter. No output location moved
  and no format changed, so the `session-digests` qmd collection is untouched.
- **`scribe.journal.preview` is a port of the incumbent consumer's parser**, which is kept
  verbatim at `tests/reference/recent_memory_preview.awk` and asserted against byte for byte —
  over digests built through `render_digest` + `append_block` rather than hand-written
  fixtures, over adversarial edge cases, and over this host's real digests when present.
  Verified on forge: the emitted context is byte-identical to what the incumbent produced
  from the same file. The format itself never needed porting; only the directory differed.
- **Ported rather than shelled out to awk**, because the property worth holding is "the
  digests still parse" and that has to be assertable. A test that only checked scribe wrote a
  file to the expected path would pass unchanged on the day the content stopped parsing.
- **One incumbent quirk deliberately not reproduced.** Its context assembly is built in bash
  double quotes — `context="# Recent Memory\n\n"` — where `\n` is two literal characters,
  which `jq -Rs` then encodes faithfully; live injected context on forge shows the literal
  `\n`. Real newlines here, pinned by a test.
- **Absence is reported, not inferred.** "scribe has not run" and "scribe is writing
  elsewhere" both produce an empty injection; the status line names the directory it looked
  in. A missing digest directory exits 0 — that is the ordinary state before the first run.
  Exit 2 means the command refused, and it writes nothing to stdout so a refusal cannot be
  mistaken for an empty result.
- **Agent resolution handles both project-directory shapes.** A hook sees
  `~/.claude/projects/<agent>`; the extractor sees Claude Code's flattened transcript
  directory. Handling only the flattened shape the codebase already knew would have returned
  no agent for every real hook invocation. An unrecognised directory yields no agent rather
  than a guess — a wrong guess injects one agent's sessions into another's context.
- **Journal selection does not follow symlinks**, matching `find -type f` without `-L`.
  Everything reachable from there is read and injected, so a link dropped into the digest
  directory would be a read primitive aimed at anything the account can open.

### Added — scribe-storage-model-2026-09

**#852 — the extracted event log was never persisted, so the drill-down tier did not exist.**
`pipeline.py` built the `EventLog` in memory, handed it to the summarizer and the QC gate, and
dropped it. Three tiers were assumed and two existed: going from a digest line to its evidence
meant re-parsing a 2.5 MB transcript out of a Backrest restore, and a digest could never be
re-checked afterwards because `scribe qc` needs the log and nothing kept it.

- **The event log is written per session**, to `[discovery] eventlog_dir`
  (`~/.local/share/scribe/eventlogs/<session-id>.json`), `0600` inside a `0700` directory. The
  serialized form is `EventLog.to_dict()` verbatim — the same JSON `scribe extract --json`
  emits and `scribe qc --events` consumes, so a file is a valid input to the gate the moment
  it lands.
- **Written before the summarization call**, and after the dry-run guard. Before the call,
  because afterwards it would be missing in exactly the case it exists for — a failed model
  call, where it is the only record of what the run saw. After the dry-run guard, because a
  dry run is documented to write nothing and that inertness is what makes it safe to sweep
  the real corpus with. Both are pinned by tests: one observes the event log's existence
  *from inside* the provider call, because asserting it afterwards cannot tell "written
  before" from "written after".
- **Flat, keyed on session id.** The digest anchor carries the session id and nothing else,
  so a flat layout makes the drill-down a lookup where date partitioning would force a glob.
  The id comes out of the transcript, so anything that is not a bare identifier is *replaced*
  with a hash of the transcript path rather than sanitised — a scrubbed id can collide with a
  real one, and a collision here silently overwrites another session's evidence.
- **`python -m scribe events <session-id>`** resolves a session to its event log and prints
  it, or just its path with `--path` for piping into `scribe qc --events`. Exit 1 for "no
  event log kept" is distinct from exit 2 for "could not look".
- **Event logs are not indexed**, and sit in a sibling directory rather than under
  `digests/`, so the new `session-digests` qmd collection excludes them by construction
  rather than by a pattern someone has to keep correct.
- Retention is settled: keep everything. 429 sessions in ~39 MB, ~0.46 GB/year. No pruning.

### Security — scribe-storage-model-2026-09

**The event log's temp file was created at the process umask, then chmod'd.** `write_eventlog`
opened it with `tmp.open("w")` — 0644 at the usual 0022 — and tightened it only afterwards, so
the payload sat world-readable for the whole write. That payload is the least redacted thing
scribe keeps: every tool argument, target and result digest of a session, derived from a 0600
transcript. Seven `agent-*` local accounts exist on this host, none in group `ted`, and the
world-read bit is precisely the bit that grants them access. FW-03 in the fleet pattern
knowledge base, whose rule also names the half that outlives the window — `rename` preserves
the *source's* permissions, so a 0644 temp file downgrades an already-0600 destination once it
lands.

New `paths.secure_create()` uses `os.open(…, 0o600)`, so the mode is applied by the kernel at
`O_CREAT` and there is no window to have. Found by this build's own pre-audit baseline, before
the security audit ran.

The test observes the mode at `fsync`, **not** at rename, and that distinction is the finding:
the original code was already 0600 by the time it renamed, so a rename-time assertion passes
while the content was exposed for the entire write. Parametrized over umask `0o000`/`0o022`
because under the developer's own `0o077` a plain `open()` also yields 0600 and the test could
not fail.

### Fixed — scribe-storage-model-2026-09

**`scribe qc --digest <a file scribe wrote>` could never pass.** The groundedness check read
scribe's own block terminator, `<!-- /scribe turn:… -->`, as a claim that the file `/scribe`
was touched — a path in no event log, on every block, so every digest file carried a
guaranteed finding. It went unseen because the pipeline grades the rendered block *before* the
markers are attached while the CLI grades the file *after*: the two graded different
documents, and only the CLI's is the one a human re-checking a digest later actually has.
Found by persisting the event log and then trying to use it for the thing it was persisted
for. `_claims` now strips the two known markers — not all HTML comments, which would also
silence a hallucinated path a model happened to put inside one.

**#851 — the test suite wrote to the production spend log.** `record_spend` falls back to
`~/logs/scribe-tokens.log` whenever `log_path` is `None`, and isolation depended on every call
site remembering to override it. Measured: 1,182 phantom `stub-1` records, **99.1% of the
file**, at +20 per `pytest` run — well-formed records lying in wait for the day the spend meter
is pointed at that path. A new `tests/conftest.py` redirects `HOME` for every test, rather than
patching each default: `~` is the single thing all of them go through, so a default added later
is covered without anyone remembering. The regression tests assert against the **real** home,
captured before the fixture runs — a test checking only that the tmp file was written cannot
tell isolation-working from isolation-removed.

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

The security audit returned 0 findings at Medium or above. One of its two Info notes observed
that `Outcome.reason` reaches three sinks and only the first is redacted — the rendered
placeholder is re-scrubbed before `append_block`, but `store.record_attempt(error=...)` and
`SessionResult.errors` carry it verbatim into the state database, the run report JSON and the
CLI. Nothing leaks today, because every `SchemaError` message carries only a field name, a
count, a type name or a JSON location. That constraint is now written at `SchemaError` itself
and enforced by a test that fails the moment any raise site quotes the response.

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
