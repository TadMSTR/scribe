"""Tests for the summarization backends.

`httpx.post` is monkeypatched directly rather than through a mocking library. A mock library
that silently stops intercepting turns a "mocked" test into one that hits the real API — it
has happened on this fleet — and a hand-rolled stub cannot fail that way: if it is not
called, nothing responds.
"""

from __future__ import annotations

import os
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


# --- claude -p: the child's environment (vikunja#961, SC-06) -------------------------


def _captured_env(monkeypatch, cfg: ProviderConfig = CLI) -> dict[str, str]:
    """Run the provider against a fake `subprocess.run` and return the `env=` it was handed.

    Asserting on `child_env()` alone would pass with `complete()` never passing it on; what
    matters is what reaches the subprocess call."""
    seen: dict = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "out", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/local/bin/claude")
    ClaudeCliProvider(cfg).complete("sys", "user")
    assert isinstance(seen.get("env"), dict), "claude -p ran with the sweep's own environment"
    return seen["env"]


def test_claude_cli_env_withholds_another_providers_key(monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-mistral-not-for-claude")
    assert os.environ["MISTRAL_API_KEY"] == "sk-mistral-not-for-claude"  # control: parent has it
    env = _captured_env(monkeypatch)
    assert "MISTRAL_API_KEY" not in env
    assert "sk-mistral-not-for-claude" not in env.values()


def test_claude_cli_env_withholds_lookalike_names(monkeypatch) -> None:
    """Enumerated, not globbed: a name that merely starts like a credential is not one."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL_OTHER", "x")
    monkeypatch.setenv("CLAUDE_SOMETHING_ELSE", "y")
    env = _captured_env(monkeypatch)
    assert "ANTHROPIC_BASE_URL_OTHER" not in env and "CLAUDE_SOMETHING_ELSE" not in env


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
)
def test_claude_cli_env_passes_its_own_credential(monkeypatch, name) -> None:
    monkeypatch.setenv(name, f"value-of-{name}")
    assert _captured_env(monkeypatch)[name] == f"value-of-{name}"


def test_claude_cli_env_passes_path_home_and_config_dir(monkeypatch, tmp_path) -> None:
    """HOME carries `claude`'s credentials file and its settings `env` block; PATH finds node.
    `CLAUDE_CONFIG_DIR`, when set, moves both -- dropping it would authenticate as someone
    else rather than fail."""
    monkeypatch.setenv("PATH", "/opt/x/bin:/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    env = _captured_env(monkeypatch)
    assert env["PATH"] == "/opt/x/bin:/usr/bin"
    assert env["HOME"] == str(tmp_path)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_BASE_URL",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "NO_PROXY",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
    ],
)
def test_claude_cli_env_passes_where_the_credential_goes(monkeypatch, name) -> None:
    monkeypatch.setenv(name, f"value-of-{name}")
    assert _captured_env(monkeypatch)[name] == f"value-of-{name}"


def test_claude_cli_env_keeps_a_gateway_token_with_its_gateway(monkeypatch) -> None:
    """The failure this pairing prevents: a gateway's token passed while its base URL is
    dropped, so `claude` presents the token to the default endpoint instead (CodeRabbit, #25).
    """
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.com")
    env = _captured_env(monkeypatch)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"


def test_claude_cli_env_passes_a_configured_api_key_env_and_only_when_configured(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MY_CLAUDE_KEY", "k")
    assert "MY_CLAUDE_KEY" not in _captured_env(monkeypatch)  # control: not named, not passed
    named = ProviderConfig(name="claude-cli", type="claude-cli", api_key_env="MY_CLAUDE_KEY")
    assert _captured_env(monkeypatch, named)["MY_CLAUDE_KEY"] == "k"


def test_claude_cli_env_and_the_hook_share_one_base_set() -> None:
    """Defined once, so the two children cannot drift apart."""
    from scribe import childenv, pipeline

    assert pipeline.HOOK_BASE_ENV is childenv.BASE_ENV
