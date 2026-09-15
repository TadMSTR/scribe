# scribe

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![CI](https://github.com/TadMSTR/scribe/actions/workflows/ci.yml/badge.svg)](https://github.com/TadMSTR/scribe/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A deterministic extractor that turns a Claude Code transcript into a structured event log, so session summaries stop being guesswork.**

Summarizing a coding session is normally set up as: hand a model the conversation text and ask it what happened. That fails for a reason that has nothing to do with the model. The conversation text is a small and unrepresentative fraction of the session — the tool calls, the commands, the file edits and their results are all in the transcript, and none of them are in the text.

Scribe extracts them first.

---

## The measurement that motivated this

Claude Code writes each session to a JSONL transcript. The pipeline scribe replaces parsed those transcripts by keeping user text and assistant text and skipping everything else. Measured against one real 2.5 MB session:

| Block type | Count | Characters | Fate under the old parser |
|---|---:|---:|---|
| user text | 12 | 1,733 | kept |
| assistant text | 35 | 32,262 | kept |
| `tool_use` | 176 | 227,893 | **dropped** |
| `tool_result` | 176 | 261,658 | **dropped** |
| `thinking` | 96 | — | dropped |

**33,995 of 523,546 characters — 6.5% — reached the summarizer.** The prompt then asked that model to "mention file names, function names, tool names, and concrete outcomes": a category of fact that had been removed from its input. A capable model degrades to filler under that instruction; a smaller one invents plausible file paths. Both look like a model-quality problem, and neither is.

Running scribe over the same session captures all 176 tool events.

## Why not just stop dropping the tool blocks

Three reasons, and the extractor addresses all three at once:

| Problem with raw tool bodies | What scribe does |
|---|---|
| **Cost.** Full-fidelity tool traffic runs roughly $26/month against a $30 allowance. | Compresses each call to a name, a bounded argument digest, exit status and the path or command it touched. |
| **Secrets.** Raw `tool_result` bodies carry `docker inspect` env blocks and `Bearer` headers verbatim into a file that then gets indexed. Measured across 429 real transcripts: **8,015 secret-shaped values in 324 of them (76%)**. | Redacts at extraction time, and reports a count so you can tell "found nothing" from "did not run". |
| **Granularity.** Per-turn summarization cannot express a cross-turn finding, because the summarizer never sees two turns at once. | Emits the whole session as one document. |

## What it produces

```bash
python -m scribe.extract path/to/transcript.jsonl --json | jq '.stats'
```

```json
{
  "turns": 12,
  "tool_events": 176,
  "tool_results": 176,
  "failures": 4,
  "raw_content_chars": 642858,
  "extracted_chars": 194667,
  "compression_ratio": 0.3032,
  "token_estimate": 48667,
  "secrets_redacted": 45,
  "degradation_level": 1,
  "budget_exceeded": false
}
```

The document is a list of turns. Each turn carries the user's message, the assistant's replies, every tool call as an event, and a **rollup** — files read, files written, commands run, MCP tools called, failures, tickets, PRs and git refs, all derived in code rather than inferred by a model:

```json
{
  "turn_uuid": "9f2c1e4a-…",
  "user_text": "Check the release workflow",
  "events": [
    {"seq": 0, "tool": "Bash", "kind": "bash",
     "target": "gh run list --workflow release.yml", "ok": true,
     "result_digest": "completed  success  v0.5.1  …"}
  ],
  "rollup": {
    "commands": ["gh run list --workflow release.yml"],
    "files_written": ["/repo/CHANGELOG.md"],
    "tickets": ["#843"],
    "prs": ["TadMSTR/scribe#1"]
  }
}
```

That rollup is what makes a summary checkable. A digest claiming a file was edited can be tested against `files_written` — deterministically, with no second model involved.

Without `--json` you get a readable rendering of the same thing.

## Install

```bash
git clone https://github.com/TadMSTR/scribe.git && cd scribe
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m scribe.extract ~/.claude/projects/*/SESSION.jsonl
```

The extractor has **no runtime dependencies** and makes no network calls. That is deliberate and worth keeping: it is the one component that reads raw transcripts, which are the least trustworthy input in the system.

## Design commitments

**Read-only on transcripts.** Scribe never writes to `~/.claude/projects/*/*.jsonl`. Those are the only durable record of what happened, and Claude Code already expires them after 30 days. The test suite asserts this on both content and mtime rather than trusting code review.

**Redaction is counted, not just applied.** `secrets_redacted` is a number. "No secret appears in the output" is also true of an extractor that produced nothing, so absence alone proves nothing — the count is what shows the filter ran.

**Redact before truncating.** A digest is a truncation, and truncating first can cut a token in half and leave the leading half. A shorter fragment of a credential is not a safer one.

**Tool identity is never discarded.** When a session exceeds the byte budget, scribe drops result digests first, then argument digests, then *tightens* targets — never removing them. A Bash event's target is its command line, the single most valuable fact in the log. If even the floor exceeds the budget, it says so via `budget_exceeded` rather than shipping a stripped log that looks like a quiet session.

Measured across 429 real transcripts at the default 200,000-character budget: 74% need no degradation at all, the busiest reaches the bottom of the ladder, and 5 exceed the budget on user and assistant text alone.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--json` | off | Emit the event log as JSON instead of a readable digest |
| `--indent N` | compact | Pretty-print the JSON |
| `--max-chars N` | `200000` | Byte budget before degradation; `0` disables it |

Exit codes: `0` success (including an empty session, which is a real outcome), `2` the transcript could not be read.

## The rest of the pipeline

```bash
python -m scribe run                 # dry run: discover, extract, check. No network.
python -m scribe run --live          # shadow run: also summarize and write
python -m scribe events SESSION_ID   # print the event log behind a digest
python -m scribe qc --digest D --events E   # exits non-zero on an ungrounded digest
python -m scribe journal             # SessionStart hook payload for this agent
```

**`run` defaults to a dry run.** It discovers finished sessions, extracts them and reports —
without constructing a provider, reading a credential, calling anything or writing anything.
The mode that spends money and sends data off the machine has to be asked for by name.

A session is finished once its transcript has been idle past a quiet period, because the Stop
hook fires per *turn* and there is no session-end signal. That inference can be wrong in one
direction — Claude Code appends to an existing transcript when a session resumes — so state
records how far the file was *read*, and growth past that offset makes a session live again
regardless of its status.

### Three tiers, and how to get between them

What scribe keeps is layered by cost, and each layer is reachable from the one above it:

| Tier | Where | Size per session | Indexed |
|---|---|---|---|
| **Digest** — what happened | `~/.local/share/scribe/digests/<agent>/<date>.md` | ~2 KB | yes — qmd `session-digests` |
| **Event log** — the evidence | `~/.local/share/scribe/eventlogs/<session-id>.json` | ~190 KB | **no** |
| **Raw transcript** — everything | `~/.claude/projects/*/*.jsonl`, and Backrest | ~2.5 MB | no |

The middle tier is the one that makes a digest checkable after the fact. It is written
**before** the summarization call, so a run whose model call failed still leaves a complete
record of what it saw — which is the case you most want it for — and a replay can skip
re-extraction.

Going from a digest line to its evidence is a lookup, not a search. Every block carries its
session id in the anchor comment above it:

```bash
sed -n 's/.*session:\([^ ]*\).*/\1/p' ~/.local/share/scribe/digests/research/2026-09-15.md
python -m scribe events "$SESSION_ID" | jq '.turns[].rollup'
python -m scribe qc --digest "$DIGEST" --events "$(python -m scribe events "$SESSION_ID" --path)"
```

**Event logs are deliberately not indexed**, and live in a sibling directory rather than
under `digests/` so that stays true by construction. They are evidence reached *from* a
digest, not a search target; indexing ~39 MB of tool arguments would swamp the digest signal
in every semantic query. They are also the least redacted thing scribe keeps, so they are
written `0600` inside a `0700` directory, like everything else derived from a transcript.

Retention is settled: **keep everything.** The measured corpus is 429 sessions in ~39 MB,
growing at roughly 0.46 GB/year. There is no pruning and none is planned.

### Feeding the SessionStart injection

Digests are also **pushed**, not only retrieved. `python -m scribe journal` emits the JSON a
Claude Code `SessionStart` hook writes to stdout, built from this agent's two most recent
digests:

```bash
CLAUDE_PROJECT_DIR=~/.claude/projects/sysadmin python -m scribe journal | jq -r \
  '.hookSpecificOutput.additionalContext'
```

`hooks/session-start.sh` is the wrapper to register. Point `SCRIBE_PYTHON` at an interpreter
that can import scribe:

```json
"hooks": { "SessionStart": [ { "matcher": "", "hooks": [
  { "type": "command", "command": "/path/to/scribe/hooks/session-start.sh" } ] } ] }
```

**`hookSpecificOutput.additionalContext` is the field that carries content.** A hook emitting
a bare `systemMessage` is dropped for agent sessions, so the status line this also emits is an
operator signal in a terminal, not a delivery mechanism. That asymmetry was measured, not
assumed, and it is why the payload is shaped the way it is.

This replaces an injection that read a journal written by a component being retired. The
format needed no porting — the incumbent's parser, run over a real scribe digest, produced 177
lines of clean output unmodified — so what moved is the directory and what is asserted is
agreement: `tests/reference/recent_memory_preview.awk` holds that parser verbatim and
`tests/test_journal.py` requires byte-for-byte equality with it. The point is to fail the day
the digest format drifts, which is the day a `SessionStart` hook would otherwise start
injecting an empty block and say nothing about it.

Absence is reported rather than inferred. "scribe has not run yet" and "scribe is writing
somewhere else" produce the same empty injection and are distinguished only by the status
line, which names the directory it looked in.

### The QC gate

The gate this replaces graded a summary for fidelity to its source, and its source was the
already-starved memory file: a faithful summary of an impoverished input scored PASS. The
grader worked; it was pointed at the wrong artefact. So every check here asserts against the
**event log**, never against another summary:

- **Groundedness** — every path, command, ticket and tool name a digest asserts must appear
  in the event log.
- **Event-coverage floor** — a digest referencing too few of the session's events FAILS.
  Without a floor, an extractor regression that silently drops events reads as a clean pass:
  the digest stays perfectly grounded in a log that has lost most of its content.
- **Freshness** — a digest exists and is newer than the transcript it summarizes.

## Status

All six phases are built, and the shadow run **has been run**: 429 sessions swept in dry mode,
plus live runs behind vikunja#847, #848, #849 and #852. `memsearch-summarize` is untouched and
remains in production — the shadow run writes to scribe's own output root and changes nothing
in the live path.

What remains is not scribe's to do. Registration of the `SessionStart` feed above, and the
cutover itself, belong to the retirement build (vikunja#863) and are sequenced behind a
backfill: only one agent has a digest today, so registering the hook first would leave the
others with an empty injection and no error.

See `CHANGELOG.md`.

## Telemetry

Off by default. Spans are emitted only when **both** of these are true:

```sh
pip install 'scribe[telemetry]'          # 1. the extra is installed
export OTEL_EXPORTER_OTLP_ENDPOINT=...   # 2. the endpoint is set (forge: http://127.0.0.1:4317)
```

**Neither alone does anything**, and that is the part worth stating plainly, because forge has
shipped the half-configured version of it twice: vikunja#336 (endpoint set, extra never
installed — telemetry dead for two months, one warning line at startup) and vikunja#579
(extras not part of venv-deploy, so a redeploy silently dropped them again). Setting the env
var is the visible step; installing the extra is the one that matters.

scribe makes the half-configured case noisy rather than silent: with the endpoint set and the
packages missing, `scribe run` prints a warning to stderr naming the install command. It is a
warning and not a fatal error on purpose — this is a batch job over a corpus, and dying over
an observability extra would trade a complete run for a complete outage.

Three spans are emitted, and the names are deliberately the *incumbent's* so existing SigNoz
dashboards keep working across the cutover:

| span | when |
|---|---|
| `memsearch.summarize` | each summarization call |
| `memsearch.summarize_rejected` | each contamination rejection (`signal`, `attempt`, `fallback`) |
| `memsearch.summarize_extract` | each transcript extraction |

The exporter is OTLP over **gRPC**, which is why the endpoint is the collector's 4317 and
carries no `/v1/traces` path suffix.

## License

MIT
