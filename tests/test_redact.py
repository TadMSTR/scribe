"""Unit tests for the redactor.

The suite is deliberately weighted toward the two ways a redactor fails quietly: replacing
something it should have left alone (destroying the signal the event log exists to carry),
and reporting a count that does not correspond to what it removed.
"""

from __future__ import annotations

import pytest

from scribe.extract.redact import REDACTED, Redactor


@pytest.mark.parametrize(
    "text",
    [
        "curl -H 'Authorization: Bearer sk-ant-api03-FAKEdeadbeef1234567890' https://api",
        "MISTRAL_API_KEY=FAKEvalue123456",
        "export GITHUB_TOKEN=ghp_FAKE1234567890abcdefgh",
        'api_key = "FAKEs3cr3tv4lue"',
        '{"api_key": "FAKElivekey12345"}',
        '"Env": ["POSTGRES_PASSWORD=FAKEhunter2", "PATH=/usr/bin"]',
        "aws key AKIAFAKE1234567890XY here",
        "glpat-FAKE1234567890abcdef",
        "xoxb-FAKE-1234567890-abcdef",
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.FAKEsignaturehere",
        "-----BEGIN RSA PRIVATE KEY-----\nFAKEkeymaterial\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_secret_shapes_are_removed_and_counted(text: str) -> None:
    r = Redactor()
    out = r.scrub(text)
    assert REDACTED in out
    # The positive half. "No secret in the output" is also true of an empty string, so the
    # count is what proves the filter ran rather than that it had nothing to do.
    assert r.count >= 1
    for token in ("FAKE", "sk-ant-", "ghp_", "AKIA", "glpat-", "xoxb-", "eyJhbGci"):
        assert token not in out


@pytest.mark.parametrize(
    "text",
    [
        "The token estimate was 4000 tokens for this session.",
        "commit a04c4c9 and sha256:18cfe3ef0011 image postgres:17-alpine",
        "PATH=/usr/local/bin:/usr/bin",
        "LOG_LEVEL=info",
        "PORT=8499",
        "Authentik handles authorization for the domain.",
        "plain prose with no credentials at all",
        "",
    ],
)
def test_signal_is_preserved(text: str) -> None:
    """Over-redaction is a real failure mode, not a safe default.

    Git SHAs, image digests and ordinary prose about tokens are exactly the facts a session
    digest should be able to state, and the Phase 5 groundedness gate checks digest claims
    against this log. A redactor that eats `a04c4c9` makes a true claim ungroundable.
    """
    r = Redactor()
    out = r.scrub(text)
    assert out == text
    assert r.count == 0


def test_one_secret_counts_once_even_when_two_rules_match() -> None:
    """`Bearer sk-ant-…` satisfies both the vendor-token rule and the header rule."""
    r = Redactor()
    r.scrub("curl -H 'Authorization: Bearer sk-ant-api03-FAKEdeadbeef1234567890' https://x")
    assert r.count == 1
    assert sum(r.by_kind.values()) > r.count, "expected overlapping rule fires"


def test_count_accumulates_across_calls() -> None:
    r = Redactor()
    r.scrub("A_TOKEN=FAKEone123456")
    r.scrub("B_SECRET=FAKEtwo123456")
    assert r.count == 2


def test_digest_redacts_before_truncating() -> None:
    """The ordering rule. Truncating first can leave the leading half of a token.

    A shorter fragment of a credential is not a safer one, so this asserts the tail is gone
    rather than merely that the output is short.
    """
    r = Redactor()
    secret = "ghp_FAKE1234567890abcdefTAIL"
    out = r.digest(f"prefix prefix prefix GITHUB_TOKEN={secret}", 30)
    assert "ghp_" not in out
    assert "FAKE" not in out
    assert r.count == 1


def test_digest_truncation_reports_the_elision() -> None:
    r = Redactor()
    out = r.digest("x" * 500, 100)
    assert out.startswith("x" * 100)
    assert "+400 chars" in out


def test_digest_leaves_short_text_untouched() -> None:
    r = Redactor()
    assert r.digest("short", 100) == "short"


def test_scrub_handles_none() -> None:
    assert Redactor().scrub(None) == ""
