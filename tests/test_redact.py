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


# --- scribe-2026-09 audit remediations ---------------------------------------------


def test_a_truncated_pem_is_still_redacted() -> None:
    """F-01 (Medium). Claude Code truncates large tool output BEFORE it reaches the
    transcript — 11,478 truncation markers across the 429-file corpus — so a key's closing
    marker is routinely cut. The surviving fragment is bare base64 with no assignment syntax,
    which no other rule here catches.
    """
    r = Redactor()
    out = r.scrub("-----BEGIN RSA PRIVATE KEY-----\nMIIFAKEkeymaterial123\n… output truncated")
    assert "FAKEkeymaterial" not in out
    assert r.count == 1


def test_a_complete_pem_is_redacted_without_swallowing_what_follows() -> None:
    """The paired rule must still win, or the truncation fallback would eat the rest of the
    document from the first BEGIN marker onward."""
    r = Redactor()
    out = r.scrub(
        "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----\n"
        "and then some ordinary prose worth keeping"
    )
    assert "FAKE" not in out
    assert "ordinary prose worth keeping" in out


@pytest.mark.parametrize(
    "text",
    [
        '{"auth": "FAKEvalue123"}',
        "auth: FAKEvalue123",
        "AUTH=FAKEvalue123",
        "curl -H 'Auth: FAKEvalue123' https://x",
    ],
)
def test_bare_auth_is_redacted_in_every_assignment_shape(text: str) -> None:
    """F-02 (Medium). The header rule matched bare `auth`; the json/keyval/envvar rules did
    not, so the same key name leaked in three of four shapes."""
    r = Redactor()
    out = r.scrub(text)
    assert "FAKEvalue123" not in out
    assert r.count >= 1


@pytest.mark.parametrize(
    "text",
    [
        "AUTHENTIK_HOST=auth.example.com",
        "authentik: enabled",
        "authorized_users: 5",
        "The user is authorized to authenticate",
        '{"authentik_version": "2026.1"}',
        "auth.example.com is the SSO host",
    ],
)
def test_adding_bare_auth_did_not_clobber_authentik_or_authorized(text: str) -> None:
    """The carve-out that made `auth` risky in the first place. Every assignment pattern
    requires a separator immediately after the key, which is why this holds — asserted rather
    than argued."""
    r = Redactor()
    assert r.scrub(text) == text
    assert r.count == 0


def test_slack_rotation_prefix_is_covered() -> None:
    """F-03 (Low). `xoxe-` is the token-rotation refresh prefix."""
    r = Redactor()
    assert "FAKE" not in r.scrub("xoxe-1-FAKEaaaabbbbccccdddd")
    assert r.count == 1


def test_the_other_slack_form_the_audit_cited_was_already_covered() -> None:
    """Correction to the audit: `xoxe.xoxp-1-…` was never a gap — the embedded `xoxp-`
    already matched. Pinned so the claim is checkable rather than asserted in prose."""
    r = Redactor()
    assert "FAKE" not in r.scrub("xoxe.xoxp-1-FAKEaaaabbbbcccc")


def test_captured_values_are_stripped_of_the_quotes_that_delimited_them() -> None:
    """A capture that keeps its delimiters gives the WRONG answer, in the reassuring direction.

    `json`'s value group is the *quoted* value, and `envvar`'s alternation includes the
    `"..."` and `'...'` forms. The only consumer is `eventlog.contains_value`, which substring-
    matches against the log's PARSED string leaves — where the value is bare, because the
    quotes were JSON syntax the parser consumed.

    So `"abc"` would not be found in a leaf containing `abc`, and `classify_post_render` would
    answer `model-output` — "the model invented this, extraction is fine" — about a value
    extraction had genuinely leaked. Measured: before the strip, `contains_value` returned
    False for a value demonstrably present in the log.
    """
    for text in (
        '{"api_key": "jsonsecretvalue"}',
        'export FOO_TOKEN="jsonsecretvalue"',
        "export FOO_TOKEN='jsonsecretvalue'",
    ):
        r = Redactor(capture=True)
        r.scrub(text)
        assert r.captured == ["jsonsecretvalue"], f"delimiters survived for {text!r}"

    # A bare value is untouched — the strip must not eat a character off an unquoted secret.
    bare = Redactor(capture=True)
    bare.scrub("export FOO_TOKEN=barevalue")
    assert bare.captured == ["barevalue"]


def test_capture_skips_redaction_residue() -> None:
    """`Bearer «redacted»` is what a later rule sees after an earlier one fired. It is not a
    secret, and recording it adds a verdict that is always False for a non-value."""
    r = Redactor(capture=True)
    r.scrub("Authorization: Bearer abc123XYZtoken")
    assert r.captured == ["abc123XYZtoken"]
    assert not any(REDACTED in v for v in r.captured)


def test_capture_is_off_by_default_so_extraction_never_holds_plaintext() -> None:
    """The default is the whole safety property: extraction must not retain what it scrubs."""
    r = Redactor()
    r.scrub("export FOO_TOKEN=barevalue")
    assert r.count == 1, "it must still redact — otherwise this passes vacuously"
    assert r.captured == []


def test_the_delimiter_strip_can_mangle_a_keyval_value_and_that_is_still_safe() -> None:
    """LOW finding, scribe-release-readiness-2026-09 audit. Behaviour pinned, not changed.

    The audit is right that the strip can mangle a `keyval` value: that rule's value group is
    unconstrained (`[^\\n]+`), so a value that is *unquoted* in the source but happens to begin
    and end with the same quote character loses those characters.

    It is wrong about the consequence, and the difference matters because the two point at
    opposite fixes. The audit predicted a missed match — a real extraction-miss reported as
    `model-output`, the reassuring-wrong direction. **That cannot happen**, for a structural
    reason: `eventlog.contains_value` does SUBSTRING containment, and the stripped value is by
    construction a substring of the unstripped one. If the original is present in the log, so
    is any substring of it. Stripping can only ever turn a False into a True — yielding
    `extraction-miss`, which sends the reader to `redact.py`. That is the conservative
    direction.

    **Do not "fix" this by scoping the strip to `json` and `envvar`.** That was the audit's
    other suggestion and it reintroduces the exact bug commit `2d2373f` fixed: for a quoted
    `keyval` source (`password: "abc"`) whose event log holds the parsed bare value (`abc`),
    the unstripped needle `"abc"` does not match and the verdict inverts to `model-output`.
    Measured both ways; the case is asserted below so the regression is caught rather than
    argued.
    """
    # The mangle is real.
    r = Redactor(capture=True)
    r.scrub('password: "quoted"value"')
    assert r.captured == ['quoted"value'], "outer quote characters are stripped"

    # And it is harmless, because the needle only ever gets shorter.
    for source in ('password: "quoted"value"', 'password: "abc"', "password: 'abc'"):
        captured = Redactor(capture=True)
        captured.scrub(source)
        assert captured.captured[0] in source, (
            "the captured value must remain a substring of its source — that is the whole "
            "reason a mangle cannot cause a missed match"
        )


def test_stripping_is_required_for_keyval_not_just_json_and_envvar() -> None:
    """The guard against the audit's harmful suggestion, stated as an executable case.

    A quoted `keyval` source whose event log holds the parsed bare value: without the strip
    the needle carries quotes the log does not have, the lookup misses, and the verdict
    inverts to `model-output` for a value extraction genuinely leaked.
    """
    r = Redactor(capture=True)
    r.scrub('password: "abc123secret"')
    assert r.captured == ["abc123secret"], (
        "if this ever captures '\"abc123secret\"' again, the keyval strip has been removed "
        "and vikunja#856's inverted verdict is back for this rule"
    )
