"""Tests for config loading.

Weighted toward the two failure modes that have actually bitten this fleet: a literal
credential in a config file (vikunja#437), and eager credential resolution turning an
unrelated provider's absence into a startup crash (vikunja#834).
"""

from __future__ import annotations

import pytest

from scribe.config import Config, ConfigError, ProviderConfig, load

FULL = """
[extract]
quiet_period_minutes = 20
max_session_bytes    = 150000

[discovery]
project_globs = ["~/.claude/projects/*/"]
exclude       = [".memsearch"]

[summarize.session]
provider = "mistral"
model    = "mistral-small-latest"

[summarize.daily]
provider = "claude-cli"

[providers.mistral]
type     = "openai-compatible"
base_url = "https://api.mistral.ai/v1"
api_key  = { env = "MISTRAL_API_KEY" }

[providers.claude-cli]
type = "claude-cli"

[service]
host = "127.0.0.1"
port = 8499
"""


def _write(tmp_path, text: str):
    p = tmp_path / "scribe.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_without_a_file() -> None:
    cfg = load(None)
    assert cfg.quiet_period_minutes == 15
    assert cfg.port == 8499
    assert cfg.providers == {}


def test_full_config_loads(tmp_path) -> None:
    cfg = load(_write(tmp_path, FULL))
    assert cfg.quiet_period_minutes == 20
    assert cfg.max_session_chars == 150000
    assert cfg.port == 8499
    assert set(cfg.providers) == {"mistral", "claude-cli"}
    assert cfg.stages["session"].model == "mistral-small-latest"


def test_api_key_is_stored_as_an_env_name_not_a_value(tmp_path) -> None:
    cfg = load(_write(tmp_path, FULL))
    assert cfg.providers["mistral"].api_key_env == "MISTRAL_API_KEY"


def test_a_literal_api_key_is_refused(tmp_path) -> None:
    """vikunja#437: `memsearch-config.toml` committed a live key.

    Documenting "use an env ref" is what was in place when that happened. Refusing to load
    the literal form is the only version of the rule that is actually enforced.
    """
    bad = FULL.replace('api_key  = { env = "MISTRAL_API_KEY" }', 'api_key  = "sk-live-value"')
    with pytest.raises(ConfigError, match="must be an env reference"):
        load(_write(tmp_path, bad))


def test_api_key_table_without_env_is_refused(tmp_path) -> None:
    bad = FULL.replace('api_key  = { env = "MISTRAL_API_KEY" }', 'api_key  = { name = "X" }')
    with pytest.raises(ConfigError, match="non-empty `env` name"):
        load(_write(tmp_path, bad))


def test_key_resolution_is_lazy_and_reads_the_environment_at_call_time(monkeypatch) -> None:
    """vikunja#834: eager resolution at config-load turned an unrelated provider outage
    into a crash. Loading must succeed with the variable unset."""
    prov = ProviderConfig(name="m", type="openai-compatible", api_key_env="SCRIBE_TEST_KEY")
    monkeypatch.delenv("SCRIBE_TEST_KEY", raising=False)
    assert prov.resolve_api_key() == ""  # absent, and that is not an error here
    monkeypatch.setenv("SCRIBE_TEST_KEY", "value-set-after-load")
    assert prov.resolve_api_key() == "value-set-after-load"


def test_loading_succeeds_with_no_credentials_in_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    cfg = load(_write(tmp_path, FULL))
    assert cfg.provider_for("session").name == "mistral"


def test_provider_with_no_key_env_resolves_to_empty() -> None:
    assert ProviderConfig(name="c", type="claude-cli").resolve_api_key() == ""


def test_stage_naming_an_undeclared_provider_fails_at_load(tmp_path) -> None:
    """A dangling reference is caught at load — which is a different thing from resolving
    its credential at load."""
    bad = FULL.replace('provider = "mistral"', 'provider = "nonexistent"')
    with pytest.raises(ConfigError, match="not declared"):
        load(_write(tmp_path, bad))


def test_stage_without_provider_is_refused(tmp_path) -> None:
    bad = FULL.replace('[summarize.daily]\nprovider = "claude-cli"', "[summarize.daily]\n")
    with pytest.raises(ConfigError, match="`provider` is required"):
        load(_write(tmp_path, bad))


def test_missing_file_raises_config_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match="config not found"):
        load(tmp_path / "absent.toml")


def test_malformed_toml_raises_config_error(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load(_write(tmp_path, "this is not = = toml"))


def test_provider_block_must_be_a_table(tmp_path) -> None:
    with pytest.raises(ConfigError, match="expected a table"):
        load(_write(tmp_path, "[providers]\nmistral = 5\n"))


def test_unknown_stage_lookup_raises() -> None:
    with pytest.raises(ConfigError, match="no configuration for stage"):
        Config().provider_for("session")
