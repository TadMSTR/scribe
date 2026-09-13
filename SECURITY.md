# Security Policy

## Reporting a vulnerability

Open a private security advisory on the GitHub repository. Please do not open a public issue
for a vulnerability.

## Threat model

Scribe reads Claude Code session transcripts. Those transcripts are, empirically, full of
credentials: measured across 429 real transcripts on the development host, **8,015
secret-shaped values appeared in 324 of them (76%)**. Anything scribe emits may be written to
disk, indexed into a search backend, and sent to a third-party summarization API. The primary
security property is therefore that **no credential survives extraction**.

### Controls

| Control | Where |
|---|---|
| Redaction at extraction time, before any value enters the event log | `src/scribe/extract/redact.py` |
| Redaction applied before truncation, so a digest cannot contain a token fragment | `Redactor.digest()` |
| Whole-document scan asserting no planted secret survives on any input path | `tests/test_contract.py` |
| Positive redaction count, so a clean output cannot be confused with an unrun filter | `stats.secrets_redacted` |
| Read-only access to transcripts, asserted on content and mtime | `tests/test_parser.py` |
| Secret scanning of the full git history, weekly and on every push | `.github/workflows/secret-scan.yml` |
| Proof the secret gate can fire, run before any clean result is believed | `tests/check_gitleaks_gate.py` |

### Known limitations

- Redaction is pattern-based. A credential with no recognisable shape — a bare high-entropy
  string in no assignment context — will not be caught. Generic entropy detection was
  considered and rejected: it also matches git SHAs and image digests, which are exactly the
  facts the event log exists to preserve.
- Test fixtures contain synthetic credentials by necessity. They carry a `FAKE` or `NOTREAL`
  marker and `.gitleaks.toml` allowlists on that marker, not on path.

## Supported versions

Only the latest release is supported.
