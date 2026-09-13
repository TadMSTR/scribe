"""Tests for the summarization runner's retry policy.

`sleep` is injected, so backoff is asserted on the delays requested rather than by waiting.
A test that genuinely sleeps gets deleted the first time someone is in a hurry.
"""

from __future__ import annotations

import json

import pytest

from scribe.extract.models import EventLog, Rollup, Stats, ToolEvent, Turn
from scribe.summarize.providers import Completion, Provider, ProviderError
from scribe.summarize.runner import RATE_LIMIT_MULTIPLIER, summarize_log

GOOD = json.dumps(
    {
        "asked": "Check the release workflow",
        "done": ["Listed the runs"],
        "found": ["Three jobs passed"],
        "tickets": ["#843"],
    }
)


def _log() -> EventLog:
    turn = Turn(turn_uuid="u1", index=0, user_text="Check the release workflow")
    turn.assistant_text = ["Listing runs."]
    turn.events.append(ToolEvent(seq=0, tool="Bash", kind="bash", target="gh run list", ok=True))
    return EventLog(
        session_id="s1",
        transcript_path="/p/t.jsonl",
        turns=[turn],
        rollup=Rollup(),
        stats=Stats(),
    )


class Scripted(Provider):
    """Returns, or raises, one scripted item per call."""

    name = "scripted"

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, timeout: float = 120.0) -> Completion:
        self.calls.append((system, user))
        item = self.script.pop(0) if self.script else self.script_default()
        if isinstance(item, Exception):
            raise item
        return Completion(
            text=item, input_tokens=10, output_tokens=5, model="m", provider="scripted"
        )

    def script_default(self):
        return GOOD


@pytest.fixture
def slept():
    delays: list[float] = []
    return delays, delays.append


def test_a_good_first_response_is_used(slept) -> None:
    delays, sleep = slept
    out = summarize_log(_log(), Scripted(GOOD), sleep=sleep)
    assert out.ok is True
    assert out.attempts == 1
    assert delays == []
    assert out.digest.tickets == ["#843"]
    assert "**Asked:** Check the release workflow" in out.markdown


def test_output_always_ends_with_a_newline(slept) -> None:
    _delays, sleep = slept
    assert summarize_log(_log(), Scripted(GOOD), sleep=sleep).markdown.endswith("\n")


def test_a_retryable_error_is_retried_then_succeeds(slept) -> None:
    delays, sleep = slept
    provider = Scripted(ProviderError("boom", retryable=True), GOOD)
    out = summarize_log(_log(), provider, sleep=sleep, base_backoff=2.0)
    assert out.ok is True
    assert out.attempts == 2
    assert delays == [2.0]


def test_a_429_backs_off_three_times_as_long(slept) -> None:
    """Matches the daemon being replaced, so operational behaviour is unchanged at cutover."""
    delays, sleep = slept
    provider = Scripted(ProviderError("limited", retryable=True, status=429), GOOD)
    summarize_log(_log(), provider, sleep=sleep, base_backoff=2.0)
    assert delays == [2.0 * RATE_LIMIT_MULTIPLIER]


def test_a_non_retryable_error_stops_immediately(slept) -> None:
    delays, sleep = slept
    provider = Scripted(ProviderError("bad request", retryable=False), GOOD)
    out = summarize_log(_log(), provider, sleep=sleep)
    assert out.ok is False
    assert out.attempts == 1
    assert delays == []
    assert len(provider.calls) == 1


def test_a_schema_violation_is_retried_like_an_api_error(slept) -> None:
    """The plan's requirement. A response that does not fit the contract is not a summary,
    and rendering it anyway is how malformed output reaches a memory file."""
    _delays, sleep = slept
    provider = Scripted("not json at all", GOOD)
    out = summarize_log(_log(), provider, sleep=sleep)
    assert out.ok is True
    assert out.attempts == 2
    assert any("schema" in e for e in out.errors)


def test_persistent_schema_violation_yields_a_marked_placeholder(slept) -> None:
    _delays, sleep = slept
    out = summarize_log(_log(), Scripted("x", "y", "z"), sleep=sleep, max_attempts=3)
    assert out.ok is False
    assert "placeholder, not a summary" in out.markdown
    assert "/p/t.jsonl" in out.markdown
    assert out.attempts == 3


def test_the_placeholder_carries_the_path_and_not_the_transcript(slept) -> None:
    """vikunja#386 — leaving raw content on disk for a failed summary is a standing
    secret-exposure path."""
    _delays, sleep = slept
    out = summarize_log(_log(), Scripted("x", "y", "z"), sleep=sleep, max_attempts=3)
    assert "/p/t.jsonl" in out.markdown
    assert "Check the release workflow" not in out.markdown
    assert "gh run list" not in out.markdown


def test_contamination_is_retried_with_a_reminder(slept) -> None:
    _delays, sleep = slept
    dirty = json.dumps({"asked": "did <specific step>", "done": ["a"]})
    provider = Scripted(dirty, GOOD)
    out = summarize_log(_log(), provider, sleep=sleep)
    assert out.ok is True
    assert out.attempts == 2
    first_system, second_system = provider.calls[0][0], provider.calls[1][0]
    assert "IMPORTANT" not in first_system
    assert "IMPORTANT" in second_system, "the retry must carry the reminder"


def test_persistent_contamination_falls_back_to_a_suppression_note(slept) -> None:
    """Falls back to the deterministic note rather than to nothing: a suppressed summary
    still records that the session happened and why it is not here."""
    _delays, sleep = slept
    dirty = json.dumps({"asked": "did <specific step>", "done": ["a"]})
    out = summarize_log(_log(), Scripted(dirty, dirty, dirty), sleep=sleep, max_attempts=3)
    assert out.suppressed is True
    assert out.ok is False
    assert "contamination guard" in out.markdown
    assert "<specific step>" not in out.markdown
    assert out.markdown.endswith("\n")


def test_token_usage_accumulates_across_attempts(slept) -> None:
    _delays, sleep = slept
    out = summarize_log(_log(), Scripted("bad", GOOD), sleep=sleep)
    assert out.input_tokens == 20
    assert out.output_tokens == 10


def test_backoff_grows_with_the_attempt(slept) -> None:
    delays, sleep = slept
    err = ProviderError("boom", retryable=True)
    summarize_log(_log(), Scripted(err, err, GOOD), sleep=sleep, base_backoff=1.0, max_attempts=3)
    assert delays == [1.0, 2.0]


def test_the_last_attempt_does_not_sleep_before_giving_up(slept) -> None:
    delays, sleep = slept
    err = ProviderError("boom", retryable=True)
    summarize_log(_log(), Scripted(err, err), sleep=sleep, base_backoff=1.0, max_attempts=2)
    assert delays == [1.0], "no point sleeping after the final attempt"


def test_a_heading_is_threaded_through_every_path(slept) -> None:
    _delays, sleep = slept
    ok = summarize_log(_log(), Scripted(GOOD), sleep=sleep, heading="14:52")
    fail = summarize_log(_log(), Scripted("x", "y", "z"), sleep=sleep, heading="14:52")
    assert ok.markdown.startswith("### 14:52")
    assert fail.markdown.startswith("### 14:52")


def test_the_prompt_carries_the_event_log(slept) -> None:
    """The whole thesis: the model is shown the tool events, not just the prose."""
    _delays, sleep = slept
    provider = Scripted(GOOD)
    summarize_log(_log(), provider, sleep=sleep)
    _system, user = provider.calls[0]
    assert "gh run list" in user
    assert "Bash" in user
