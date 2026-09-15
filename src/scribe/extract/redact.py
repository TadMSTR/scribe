"""Redaction of secret-shaped values, applied at extraction time.

This is the mitigation for vikunja#638, #700 and #842 — three separate incidents of a
credential reaching a transcript that was then indexed into Milvus and OpenSearch. The
event log this package produces is written to disk and fed to a third-party API, so a
secret that survives extraction has already left the machine by the time anyone reads it.

Two design rules, both load-bearing:

1. **Redact before truncating.** A digest is a truncation, and truncating first can slice
   a token in half and leave the leading half in the output. `Redactor.digest()` therefore
   scrubs the full string and only then cuts it. Never truncate and then scrub.

2. **One choke point.** Every string that enters the event log passes through a `Redactor`
   method. Scattering `re.sub` calls across the parser is how a sanitising call gets missed
   on the third of three sinks; the parser holds a redactor and has no other way to emit a
   string. `tests/test_redact.py` walks the whole rendered document to assert this holds,
   rather than checking the fields someone remembered to check.

`Redactor` counts what it replaces. The count is reported as `stats.secrets_redacted` and
is the *positive* half of the redaction control in the build plan: an extractor that
produced nothing at all would satisfy "no secret appears in the output", so the test suite
asserts the filter ran, not merely that its output looks clean.
"""

from __future__ import annotations

import re

REDACTED = "«redacted»"

# Key names whose *value* is a secret wherever it appears in assignment position. Kept
# deliberately short and specific: a broad list (e.g. a bare "auth") matches "authentik"
# and "authorized" and would scrub ordinary prose, and an event log that redacts its own
# signal is no more useful than one that was never written.
_SENSITIVE_KEY = r"""(?:
      api[_-]?keys?
    | apikeys?
    | secret[_-]?(?:key|token|access[_-]?key)?
    | client[_-]?secret
    | password | passwd | pwd
    | access[_-]?token | refresh[_-]?token | id[_-]?token | auth[_-]?token
    | bearer[_-]?token
    | private[_-]?key
    | access[_-]?key[_-]?id
    | authorization
    # F-02, scribe-2026-09 audit. Bare `auth` was matched by the header rule but not here, so
    # `{"auth": "..."}`, `auth: ...` and `AUTH=...` all leaked -- a real field name in several
    # APIs, MQTT and webhook configs. Safe to add: every assignment pattern requires a
    # separator (`"`, `:`, `=`) immediately after the key, so `authentik` and `authorized`
    # cannot match. Asserted in test_redact.py rather than argued here.
    | auth
    | credentials?
    | [a-z0-9_]*_token
    | [a-z0-9_]*_key
    | token
)"""

# Vendor-prefixed tokens are recognisable on sight and carry no useful signal, so they are
# replaced wherever they occur — including in free prose, where no assignment pattern would
# fire. AKIA is matched with a fixed length because the prefix alone is not distinctive.
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # F-01, scribe-2026-09 audit. Claude Code truncates large tool output BEFORE it reaches
    # the transcript, so a key's closing marker is routinely cut -- 11,478 truncation markers
    # across the 429-file corpus, measured. The surviving fragment is bare base64 with no
    # assignment syntax, so no other rule here catches it either. This runs AFTER the paired
    # rule above, so any BEGIN it sees had no END: redact to end of string.
    ("pem_truncated", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?!-----END)[\s\S])*")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("gitlab", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}")),
    # `e` added for the token-rotation refresh prefix (F-03). The audit also cited
    # `xoxe.xoxp-1-...`, but that form was already caught by the embedded `xoxp-`.
    ("slack", re.compile(r"\bxox[baprse]-[A-Za-z0-9.\-]{8,}")),
    ("aws_akid", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # A JWT is three base64url segments; the leading eyJ is the encoded '{"'.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+")),
)

# Assignment shapes. These cover `.env` file bodies and `docker inspect` Env arrays for
# free — both are just KEY=VALUE — which is why there is no format-specific handling for
# either. The value is replaced; the key is kept, because "MISTRAL_API_KEY was set" is
# exactly the kind of fact a session summary should be able to state.
_ASSIGNMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Authorization: Bearer <tok>  /  Bearer <tok>
    ("bearer", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=\-]{8,})")),
    # curl -H 'X-Api-Key: v' / --header "Authorization: v"
    (
        "header",
        re.compile(
            r"(?i)(-H|--header)(\s+|=)(['\"]?)([A-Za-z0-9\-]*"
            r"(?:auth|key|token|secret|cookie)[A-Za-z0-9\-]*\s*:\s*)"
            r"(?!\s*«)([^'\"\n]+)(\3)"
        ),
    ),
    # JSON: "api_key": "v"
    (
        "json",
        re.compile(rf"(?ix)(\"[a-z0-9_\-]*{_SENSITIVE_KEY}\"\s*:\s*)(\"(?!«)[^\"\n]{{4,}}\")"),
    ),
    # TOML/YAML: api_key = "v"  /  password: v
    (
        "keyval",
        re.compile(
            rf"(?ixm)^(\s*[\"']?[a-z0-9_\-]*{_SENSITIVE_KEY}[\"']?\s*[:=]\s*)"
            r"(?!\s*(?:\{|\[|«|$))([^\n]+)$"
        ),
    ),
    # Shell/env: export FOO_TOKEN=v  /  FOO_TOKEN=v  (also docker inspect "Env" entries)
    (
        "envvar",
        re.compile(
            rf"(?ix)\b((?:export\s+)?[A-Z0-9_]*{_SENSITIVE_KEY}\s*=\s*)"
            r"(?!\s*$)(?!«)([^\s\"',\]\}}]+|\"[^\"\n]*\"|'[^'\n]*')"
        ),
    ),
)

# Which capture group holds the VALUE, per assignment rule — used only when a `Redactor` is
# built with `capture=True`. Derived from `_ASSIGNMENT_REPL` above: the group that is NOT
# written back into the replacement is the one being discarded, which is the secret.
# Vendor-token rules have no groups; the whole match is the value.
_VALUE_GROUP: dict[str, int] = {
    "bearer": 1,
    "header": 5,
    "json": 2,
    "keyval": 2,
    "envvar": 2,
}

# Substitutions that keep the value's *shape* — the key name, the header name — while
# discarding the value, so the reader can still see that a credential was involved.
_ASSIGNMENT_REPL: dict[str, str] = {
    "bearer": r"Bearer " + REDACTED,
    "header": r"\1\2\3\4" + REDACTED + r"\6",
    "json": r"\g<1>" + f'"{REDACTED}"',
    "keyval": r"\g<1>" + REDACTED,
    "envvar": r"\g<1>" + REDACTED,
}


class Redactor:
    """Scrubs secret-shaped values and counts what it replaced.

    A single instance is shared for one extraction run, so `count` is the session total
    reported as `stats.secrets_redacted`.

    **`capture` is off by default and extraction must never turn it on.** A capturing
    redactor holds the plaintext of everything it matched, which is the one thing this class
    exists to get rid of. It exists for exactly one caller: the post-render detector in
    `pipeline.py`, which needs the matched value in memory just long enough to ask "was this
    in the event log?" and then discards it. The values are never written anywhere — not to
    the error list, not to the JSON report, not to a span attribute. See vikunja#856.
    """

    def __init__(self, *, capture: bool = False) -> None:
        #: Net secrets removed — the number of redaction markers this instance introduced.
        #: This is the figure reported as `stats.secrets_redacted`.
        self.count = 0
        #: Per-rule fire counts, for diagnosing *which* rule caught something. These sum to
        #: MORE than `count` whenever one secret satisfies two rules, which is normal and
        #: not a defect: `Authorization: Bearer sk-ant-…` is caught once as a vendor token
        #: and once as a header assignment, but it is one secret. Counting markers rather
        #: than rule fires is what keeps the headline number equal to reality, and it holds
        #: for overlaps nobody enumerated in advance.
        self.by_kind: dict[str, int] = {}
        self._capture = capture
        #: Plaintext of every value replaced, when `capture=True`; always empty otherwise.
        #: Transient by contract — see the class docstring.
        self.captured: list[str] = []

    def _record(self, kind: str, pat: re.Pattern[str], text: str) -> None:
        """Record the matched values for `kind`, if capturing."""
        if not self._capture:
            return
        group = _VALUE_GROUP.get(kind, 0)
        for m in pat.finditer(text):
            value = m.group(group)
            if value:
                self.captured.append(value)

    def _bump(self, kind: str, n: int) -> None:
        if n:
            self.by_kind[kind] = self.by_kind.get(kind, 0) + n

    def scrub(self, text: str | None) -> str:
        """Return `text` with every secret-shaped value replaced.

        Vendor tokens are replaced first: a `Bearer sk-ant-...` matches both the token
        pattern and the bearer pattern, and running tokens first means the residual
        `Bearer «redacted»` no longer looks like a bearer assignment, so it is counted
        once rather than twice.
        """
        if not text:
            return ""
        out = text
        for kind, pat in _TOKEN_PATTERNS:
            self._record(kind, pat, out)
            out, n = pat.subn(REDACTED, out)
            self._bump(kind, n)
        for kind, pat in _ASSIGNMENT_PATTERNS:
            self._record(kind, pat, out)
            out, n = pat.subn(_ASSIGNMENT_REPL[kind], out)
            self._bump(kind, n)
        self.count += out.count(REDACTED) - text.count(REDACTED)
        return out

    def digest(self, text: str | None, limit: int) -> str:
        """Scrub `text`, then truncate to `limit` characters.

        The order is the point. Truncating first can cut a token in half and leave the
        leading half — which is still a leak, and a shorter one is not a safer one.
        """
        scrubbed = self.scrub(text)
        if len(scrubbed) <= limit:
            return scrubbed
        return scrubbed[:limit] + f"…(+{len(scrubbed) - limit} chars)"
