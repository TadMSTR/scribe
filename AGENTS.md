# AGENTS.md — scribe

Guidance for agents working in this repository.

## What this is

A deterministic extractor that turns a Claude Code JSONL transcript into a structured event
log, plus (planned) a schema'd summarizer that consumes it. It replaces a pipeline that sent
its model 6.5% of each session and then asked it to name the tools, files and commands it had
never been shown.

## Invariants — do not weaken these without reading why they exist

**1. `scribe.extract` is stdlib-only and offline.**
No runtime dependencies, no network, no imports that could acquire either. It is the one
module that reads raw transcripts, which are the least trustworthy input in the system.
Summarizer dependencies belong in a Phase 3 extra, not in `[project.dependencies]`.

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

**8. Output ends with a newline.**
A missing one glued ~1,030 headings together in the memory files downstream.

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

**Prove a new gate red before trusting it green.** The pattern used here: copy the tree to a
temporary directory, neuter the function body while keeping its signature, and confirm the
suite fails. Mutate a copy, never the working tree — an interrupted run leaves a survivor
behind, and a silently-neutered redactor in a repo about redaction is the worst possible
place for one.

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
