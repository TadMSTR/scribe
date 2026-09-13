# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
