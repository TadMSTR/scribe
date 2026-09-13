# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- **Phase 2** — session discovery and completion: filesystem watch, quiet-period detection,
  durable per-session state in SQLite.
- **Phase 3** — summarizer: provider abstraction (OpenAI-compatible HTTP and `claude -p`),
  schema'd JSON output validated before rendering, contamination guard ported from
  `memsearch-summarize.py`.
- **Phase 4** — write-back and daily roll-up, OTel spans, spend metering.
- **Phase 5** — QC gates: coverage against the transcript, groundedness against the event
  log, and an event-coverage floor.
- **Phase 6** — shadow run alongside `memsearch-summarize`.

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
