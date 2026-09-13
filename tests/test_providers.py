"""Tests for the summarization backends.

`httpx.post` is monkeypatched directly rather than through a mocking library. A mock library
that silently stops intercepting turns a "mocked" test into one that hits the real API — it
has happened on this fleet — and a hand-rolled stub cannot fail that way: if it is not
called, nothing responds.
"""

from __future__ import annotations

import subprocess

import pytest

from scribe.config import ProviderConfig
from scribe.summarize.providers import (
    ClaudeCliProvider,
    OpenAICompatibleProvider,
    ProviderError,
    build,
)

MISTRAL = ProviderConfig(
    name="mistral",
    type="openai-compatible",
    base_url="https://api.example.test/v1",
    api_key_env="SCRIBE_TEST_KEY",
    model="small-latest",
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok_payload(content: str = '{"asked":"a","done":["b"]}') -> dict:
    return {
        "model": "small-latest-2026",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


@pytest.fixture
def captured(monkeypatch):
    """Capture the outgoing request and return a canned response."""
    calls: list[dict] = []

    def fake_post(url, *, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(200, _ok_payload())

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_posts_to_the_configured_base_url(captured, monkeypatch) -> None:
    """No hardcoded endpoints. The daemon this replaces hardcodes the Anthropic URL, which
    is why swapping providers there meant editing code."""
    monkeypatch.setenv("SCRIBE_TEST_KEY", "k")
    OpenAICompatibleProvider(MISTRAL).complete("sys", "user")
    assert captured[0]["url"] == "https://api.example.test/v1/chat/completions"


def test_credential_is_read_at_call_time_not_construction(captured, monkeypatch) -> None:
    """vikunja#834. Constructing the provider with the variable unset must be fine."""
    monkeypatch.delenv("SCRIBE_TEST_KEY", raising=False)
    provider = OpenAICompatibleProvider(MISTRAL)  # no raise
    monkeypatch.setenv("SCRIBE_TEST_KEY", "set-after-construction")
    provider.complete("sys", "user")
    assert captured[0]["headers"]["Authorization"] == "Bearer set-after-construction"


def test_a_missing_required_credential_fails_at_call_time(monkeypatch) -> None:
    monkeypatch.delenv("SCRIBE_TEST_KEY", raising=False)
    with pytest.raises(ProviderError, match="SCRIBE_TEST_KEY"):
        OpenAICompatibleProvider(MISTRAL).complete("sys", "user")


def test_a_provider_needing_no_key_sends_no_auth_header(captured) -> None:
    local = ProviderConfig(
        name="ollama",
        type="openai-compatible",
        base_url="http://127.0.0.1:11435/v1",
        model="summarize:latest",
    )
    OpenAICompatibleProvider(local).complete("sys", "user")
    assert "Authorization" not in captured[0]["headers"]


def test_usage_and_model_are_reported(captured, monkeypatch) -> None:
    monkeypatch.setenv("SCRIBE_TEST_KEY", "k")
    c = OpenAICompatibleProvider(MISTRAL).complete("sys", "user")
    assert (c.input_tokens, c.output_tokens) == (100, 20)
    assert c.model == "small-latest-2026"
    assert c.provider == "mistral"


def test_json_object_response_format_is_requested(captured, monkeypatch) -> None:
    monkeypatch.setenv("SCRIBE_TEST_KEY", "k")
    OpenAICompatibleProvider(MISTRAL).complete("sys", "user")
    assert captured[0]["json"]["response_format"] == {"type": "json_object"}
    assert captured[0]["json"]["temperature"] == 0


@pytest.mark.parametrize(
    "status,retryable",
    [(429, True), (500, True), (503, True), (400, False), (401, False), (404, False)],
)
def test_status_codes_map_to_the_right_retryability(monkeypatch, status, retryable) -> None:
    """A rate limit is worth retrying; a malformed request is not. Treating everything as
    retryable turns a permanent bug into a quota burn."""
    import httpx

    monkeypatch.setenv("SCRIBE_TEST_KEY", "k")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(status))
    with pytest.raises(ProviderError) as exc:
        OpenAICompatibleProvider(MISTRAL).complete("sys", "user")
    assert exc.value.retryable is retryable
    assert exc.value.status == status


def test_a_rejected_request_body_is_not_echoed(monkeypatch) -> None:
    """A 4xx body can contain the request, headers included."""
    import httpx

    monkeypatch.setenv("SCRIBE_TEST_KEY", "supersecretvalue")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: FakeResponse(400, {"error": "bad key supersecretvalue"}),
    )
    with pytest.raises(ProviderError) as exc:
        OpenAICompatibleProvider(MISTRAL).complete("sys", "user")
    assert "supersecretvalue" not in str(exc.value)


def test_transport_error_is_retryable(monkeypatch) -> None:
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setenv("SCRIBE_TEST_KEY", "k")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(ProviderError) as exc:
        OpenAICompatibleProvider(MISTRAL).complete("sys", "user")
    assert exc.value.retryable is True


def test_unparseable_envelope_is_retryable(monkeypatch) -> None:
    import httpx

    monkeypatch.setenv("SCRIBE_TEST_KEY", "k")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(200, {"nope": 1}))
    with pytest.raises(ProviderError) as exc:
        OpenAICompatibleProvider(MISTRAL).complete("sys", "user")
    assert exc.value.retryable is True


def test_provider_without_base_url_or_model_is_refused() -> None:
    with pytest.raises(ProviderError, match="base_url"):
        OpenAICompatibleProvider(ProviderConfig(name="x", type="openai-compatible"))
    with pytest.raises(ProviderError, match="model"):
        OpenAICompatibleProvider(
            ProviderConfig(name="x", type="openai-compatible", base_url="http://h/v1")
        )


def test_build_dispatches_on_type() -> None:
    assert isinstance(build(MISTRAL), OpenAICompatibleProvider)
    assert isinstance(build(ProviderConfig(name="c", type="claude-cli")), ClaudeCliProvider)
    with pytest.raises(ProviderError, match="unknown type"):
        build(ProviderConfig(name="z", type="carrier-pigeon"))


# --- claude -p ---------------------------------------------------------------------

CLI = ProviderConfig(name="claude-cli", type="claude-cli")


def _fake_run(monkeypatch, *, returncode=0, stdout="out", raises=None):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/local/bin/claude")
    return calls


def test_claude_cli_returns_stdout(monkeypatch) -> None:
    _fake_run(monkeypatch, stdout='{"asked":"a","done":["b"]}')
    c = ClaudeCliProvider(CLI).complete("sys", "user")
    assert c.text.startswith("{")
    assert c.provider == "claude-cli"


def test_claude_cli_missing_binary_is_not_retryable(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _b: None)
    with pytest.raises(ProviderError) as exc:
        ClaudeCliProvider(CLI).complete("sys", "user")
    assert exc.value.retryable is False


def test_claude_cli_empty_output_is_a_failure(monkeypatch) -> None:
    """Exit 0 with nothing printed is not a summary."""
    _fake_run(monkeypatch, returncode=0, stdout="   ")
    with pytest.raises(ProviderError, match="no output") as exc:
        ClaudeCliProvider(CLI).complete("sys", "user")
    assert exc.value.retryable is True


@pytest.mark.parametrize("code,retryable", [(1, False), (2, False), (124, True), (142, True)])
def test_claude_cli_exit_codes(monkeypatch, code, retryable) -> None:
    _fake_run(monkeypatch, returncode=code)
    with pytest.raises(ProviderError) as exc:
        ClaudeCliProvider(CLI).complete("sys", "user")
    assert exc.value.retryable is retryable


def test_claude_cli_timeout_is_retryable(monkeypatch) -> None:
    _fake_run(monkeypatch, raises=subprocess.TimeoutExpired("claude", 5))
    with pytest.raises(ProviderError, match="timed out") as exc:
        ClaudeCliProvider(CLI).complete("sys", "user", timeout=5)
    assert exc.value.retryable is True


def test_claude_cli_passes_the_model_when_configured(monkeypatch) -> None:
    calls = _fake_run(monkeypatch, stdout="x")
    ClaudeCliProvider(CLI, model="opus").complete("sys", "user")
    assert "--model" in calls[0] and "opus" in calls[0]


def test_claude_cli_exit_zero_with_error_text_is_not_caught_here(monkeypatch) -> None:
    """upstream memsearch#527: `claude -p` can exit 0 having printed a rate-limit notice.

    This provider deliberately does NOT try to detect that by pattern-matching the content —
    the schema check is what rejects it, and encoding a second, weaker content check here
    would be a place for the two to disagree.
    """
    _fake_run(monkeypatch, returncode=0, stdout="You've hit your limit - resets 11am")
    c = ClaudeCliProvider(CLI).complete("sys", "user")
    assert "hit your limit" in c.text  # passed through; the schema rejects it downstream
