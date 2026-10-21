"""Summarization backends, selected by config.

Two implementations cover the plan's three providers: an OpenAI-compatible HTTP client
(Mistral and Ollama-via-`ollama-queue-proxy` both speak it) and a `claude -p` subprocess.

**No hardcoded URLs.** The daemon being replaced hardcodes
`https://api.anthropic.com/v1/messages`, which is why swapping providers there meant editing
code. Every endpoint here comes from config.

**Credentials resolve at call time**, via `ProviderConfig.resolve_api_key()`. See
`scribe/config.py` for why that matters (vikunja#834).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from ..childenv import BASE_ENV, allowlisted_env
from ..config import ProviderConfig


class ProviderError(RuntimeError):
    """A provider call that failed.

    `retryable` distinguishes "try again" from "this will fail identically forever". A rate
    limit is retryable; a malformed request is not. Treating everything as retryable turns a
    permanent bug into a quota burn.

    `unavailable` is narrower, and says the PROVIDER is down rather than that this call went
    wrong: a transport error or timeout, a 429, a 5xx. It is what lets the pipeline leave a
    session pending through an outage instead of spending its retry budget on one (#1188
    (id 1271) item 4). Every unavailable error is retryable; the reverse does not hold. An
    unparseable response envelope is retryable and NOT unavailable, because it can be a
    property of the input -- treated as an outage, a session that reliably produces one would
    be deferred on every sweep forever, which is the 12-attempt burn `state.py` documents.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        unavailable: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable or unavailable
        self.status = status
        self.unavailable = unavailable


@dataclass
class Completion:
    """What a provider returned, plus what it cost."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    #: The model the RESPONSE named, verbatim, or "" when it named none. Kept apart from
    #: `model`, which falls back to the requested name so the spend meter always has one: a
    #: run record that filled this from the request would report an alias as a resolution
    #: nobody observed. On Mistral it is the alias echoed back (`mistral-small-latest`), so it
    #: records what was answered, not which dated model answered it.
    model_resolved: str = ""


class Provider:
    """Interface every backend implements."""

    name = "provider"

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> Completion:
        raise NotImplementedError


def _endpoint_down_errors() -> tuple[type[BaseException], ...]:
    """The httpx errors that mean the endpoint did not answer at all."""
    import httpx

    return (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError)


class OpenAICompatibleProvider(Provider):
    """Chat-completions over HTTP. Covers Mistral and Ollama.

    `httpx` is imported inside `complete()` rather than at module scope so that importing
    `scribe.summarize` — which the QC gate and the renderer do — does not require a network
    library to be installed. It also keeps `scribe.extract` provably clear of it.
    """

    def __init__(self, cfg: ProviderConfig, model: str = "") -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.model = model or cfg.model
        if not cfg.base_url:
            raise ProviderError(f"provider {cfg.name!r} has no base_url")
        if not self.model:
            raise ProviderError(f"provider {cfg.name!r} has no model configured")

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> Completion:
        import httpx

        endpoint_down = _endpoint_down_errors()
        headers = {"Content-Type": "application/json"}
        # Resolved HERE, not at construction and not at config load.
        key = self.cfg.resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        elif self.cfg.api_key_env:
            raise ProviderError(
                f"provider {self.cfg.name!r} needs ${self.cfg.api_key_env}, which is unset",
                retryable=False,
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except endpoint_down as exc:
            raise ProviderError(
                f"{self.name}: endpoint unreachable: {type(exc).__name__}: {exc}",
                retryable=True,
                unavailable=True,
            ) from exc
        except httpx.TransportError as exc:
            # Reached the endpoint, or might have, and then failed: a read/write timeout or a
            # dropped stream. Worth retrying, and NOT an outage, because a slow answer can be a
            # property of the input -- a session big enough that the model always needs more
            # than `timeout` would otherwise be deferred forever (audit F-01).
            raise ProviderError(
                f"{self.name}: transport error: {type(exc).__name__}: {exc}", retryable=True
            ) from exc
        except Exception as exc:
            # Raised before the request left: the payload could not be built or encoded. That
            # is determined by the input, so retrying buys the same error (audit F-01).
            raise ProviderError(
                f"{self.name}: request could not be sent: {type(exc).__name__}: {exc}",
                retryable=False,
            ) from exc

        if resp.status_code == 429:
            raise ProviderError(
                f"{self.name}: rate limited", retryable=True, status=429, unavailable=True
            )
        if resp.status_code >= 500:
            raise ProviderError(
                f"{self.name}: server error {resp.status_code}",
                retryable=True,
                status=resp.status_code,
                unavailable=True,
            )
        if resp.status_code >= 400:
            # 4xx other than 429 will fail identically on retry. The body is NOT included:
            # a rejected request can be echoed back with its headers.
            raise ProviderError(
                f"{self.name}: request rejected with {resp.status_code}",
                retryable=False,
                status=resp.status_code,
            )

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"{self.name}: unparseable response envelope: {exc}", retryable=True
            ) from exc

        usage = data.get("usage") or {}
        return Completion(
            text=text or "",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or self.model),
            provider=self.name,
            model_resolved=str(data.get("model") or ""),
        )


#: The credentials `claude` itself authenticates with, by exact name. Enumerated, not globbed:
#: `ANTHROPIC_*` / `CLAUDE_*` would also pass whatever else a host happens to name that way,
#: and a grant nobody can read off the source is not a grant anybody reviewed.
CLAUDE_CLI_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
#: Not a credential, but it moves where `claude` looks for one, and for its settings. Dropping
#: it would not fail -- it would silently authenticate as whatever `~/.claude` holds instead.
CLAUDE_CLI_CONFIG_ENV = ("CLAUDE_CONFIG_DIR",)
#: Where the credential above is SENT. These travel with it or not at all: passing
#: `ANTHROPIC_AUTH_TOKEN` while dropping `ANTHROPIC_BASE_URL` would send a gateway's token to
#: the default endpoint instead of the gateway, and dropping the proxy or its CA bundle would
#: stop a proxied host producing summaries at all. A proxy URL can carry its own credentials;
#: it is passed because `claude` cannot reach anything without it, not because it is safe.
#: Both spellings of the proxy names, because both are honoured.
CLAUDE_CLI_ROUTING_ENV = (
    "ANTHROPIC_BASE_URL",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
)


class ClaudeCliProvider(Provider):
    """`claude -p` as a subprocess. No API key; already proven on forge.

    **Exit status alone is not a success signal here.** `claude -p` can exit 0 having printed
    a rate-limit notice instead of content — that is upstream memsearch#527, and it is why
    the response is schema-validated by the caller rather than trusted because the process
    returned zero. This class reports transport-level failure; the contract check is the
    schema's job.

    **The child gets a named environment, not the sweep's** (vikunja#961, SC-06). The sweep
    holds every other provider's key, and `claude` has no use for any of them. It gets
    `childenv.BASE_ENV`, its own credential names, `CLAUDE_CONFIG_DIR`, the settings that
    decide where the credential is sent (endpoint, proxy, CA bundle), and the provider's
    `api_key_env` if the config names one -- see `child_env`.
    """

    name = "claude-cli"

    def __init__(self, cfg: ProviderConfig, model: str = "", binary: str = "claude") -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.model = model or cfg.model
        self.binary = binary

    def child_env(self) -> dict[str, str]:
        """The environment `claude -p` runs with. Read at call time, as credentials are."""
        extra = (self.cfg.api_key_env,) if self.cfg.api_key_env else ()
        return allowlisted_env(
            (
                *BASE_ENV,
                *CLAUDE_CLI_CREDENTIAL_ENV,
                *CLAUDE_CLI_CONFIG_ENV,
                *CLAUDE_CLI_ROUTING_ENV,
                *extra,
            )
        )

    def complete(self, system: str, user: str, *, timeout: float = 300.0) -> Completion:
        exe = shutil.which(self.binary)
        if exe is None:
            raise ProviderError(f"{self.name}: {self.binary!r} not on PATH", retryable=False)
        argv = [exe, "-p", "--output-format", "text"]
        if self.model:
            argv += ["--model", self.model]
        prompt = f"{system}\n\n{user}"
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=self.child_env(),
            )
        except subprocess.TimeoutExpired as exc:
            # Retryable, NOT an outage: `claude -p`'s run time scales with the input, so a
            # session big enough to always exceed `timeout` would otherwise be deferred
            # forever -- the shape audit F-01 removed from httpx's read timeout (audit R2-02).
            raise ProviderError(f"{self.name}: timed out after {timeout}s", retryable=True) from exc
        except OSError as exc:
            raise ProviderError(f"{self.name}: {exc}", retryable=False) from exc

        if proc.returncode != 0:
            # 124/142 are the shell's timeout conventions: worth retrying, and like
            # `TimeoutExpired` not an outage (audit R2-02). Anything else is likely to repeat.
            timed_out = proc.returncode in (124, 142)
            raise ProviderError(f"{self.name}: exited {proc.returncode}", retryable=timed_out)
        if not (proc.stdout or "").strip():
            raise ProviderError(f"{self.name}: produced no output", retryable=True)
        return Completion(text=proc.stdout, model=self.model, provider=self.name)


def build(cfg: ProviderConfig, model: str = "") -> Provider:
    """Construct the provider named by a config block."""
    if cfg.type == "openai-compatible":
        return OpenAICompatibleProvider(cfg, model)
    if cfg.type == "claude-cli":
        return ClaudeCliProvider(cfg, model)
    raise ProviderError(f"provider {cfg.name!r} has unknown type {cfg.type!r}", retryable=False)
