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
    assert out.contamination_signal == "", "a rescued run was not suppressed"


def test_persistent_contamination_falls_back_to_a_suppression_note(slept) -> None:
    """Falls back to the deterministic note rather than to nothing: a suppressed summary
    still records that the session happened and why it is not here."""
    _delays, sleep = slept
    dirty = json.dumps({"asked": "did <specific step>", "done": ["a"]})
    out = summarize_log(_log(), Scripted(dirty, dirty, dirty), sleep=sleep, max_attempts=3)
    assert out.suppressed is True
    assert out.contamination_signal == "placeholder_token"
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


# --- vikunja#849: a deterministic schema violation must not buy two more calls -------------

OVER_CAP = json.dumps(
    {"asked": "a", "done": ["Listed the runs"], "tickets": [f"#{i}" for i in range(500)]}
)


def test_an_over_cap_response_is_not_retried(slept) -> None:
    """The discriminator is the call count, not the outcome.

    The script's second entry is a GOOD response, so a runner that still retried would
    succeed on attempt two and every other assertion here would pass. Only `len(calls) == 1`
    can tell the fix from its absence — and at ~50k input tokens a call, the two it saves are
    the entire point of the phase.
    """
    delays, sleep = slept
    provider = Scripted(OVER_CAP, GOOD, GOOD)
    out = summarize_log(_log(), provider, sleep=sleep)
    assert len(provider.calls) == 1
    assert out.attempts == 1
    assert delays == [], "a failure that will not be retried must not back off first"
    assert out.ok is False
    assert "not retryable" in out.reason
    assert "placeholder, not a summary" in out.markdown


def test_a_retryable_schema_violation_still_gets_its_retries(slept) -> None:
    """The narrow half of the fix. Malformed JSON is a bad sample, not a property of the
    input, so the module's original argument for retrying it is untouched."""
    _delays, sleep = slept
    provider = Scripted("not json at all", OVER_CAP, GOOD)
    out = summarize_log(_log(), provider, sleep=sleep)
    assert len(provider.calls) == 2, "attempt 1 retried; attempt 2 hit the cap and stopped"
    assert out.ok is False


# --- 0.17.0: degrade per item on a terminal `residue_token` (vikunja#1216) ----------------------

from scribe.extract.redact import REDACTED  # noqa: E402
from scribe.summarize.contamination import Detection, detect_contamination  # noqa: E402
from scribe.summarize.render import WITHHELD_MARKER  # noqa: E402

CLEAN = [
    "Read the restic check flags",
    "Confirmed the subset parser rejects zero",
    "Ran the bats suite green",
    "Tagged the release candidate",
    "Opened the audit request",
]
#: Long enough to count for the overlap detector, and present in `_overlap_log`'s source.
SOURCE = [
    "The restic subset flag accepts a fraction of the packs",
    "The parser rejects a denominator above the allowed bound",
    "The bats suite covers both of the rejected input shapes",
]


def _digest(asked: str = "Check the release workflow", **fields) -> str:
    """`done` is required by the schema; a short default stays out of the overlap window."""
    return json.dumps({"asked": asked, "done": ["Listed the runs"], **fields})


@pytest.fixture
def line_corpus(monkeypatch):
    """A multi-line corpus holding `SOURCE`. `grounding_text()` is one JSON line, which the
    three-line overlap window can never match, so the overlap cases supply their own corpus
    to reach the runner's handling of that signal at all."""
    monkeypatch.setattr("scribe.summarize.runner.grounding_corpus", lambda log: "\n".join(SOURCE))


def _three(text: str) -> Scripted:
    return Scripted(text, text, text)


def test_a_terminal_residue_item_is_withheld_not_the_session(slept) -> None:
    """G1. Five clean items and one paraphrased `n/N` line: the five are written."""
    _delays, sleep = slept
    found = [*CLEAN, "values of N above 256 are still accepted"]
    out = summarize_log(_log(), _three(_digest(found=found)), sleep=sleep)
    assert out.ok is True and out.suppressed is False
    assert out.digest.found == CLEAN
    assert out.withheld_items == 1 and out.withheld_fields == {"found": 1}
    assert out.contamination_signal == "residue_token" and out.contamination_rule == "count_letter"
    assert "values of N above 256" in out.contamination_excerpt
    assert f"{WITHHELD_MARKER} 1 item dropped by the contamination guard (residue_token)._" in (
        out.markdown.splitlines()
    )
    assert "N above 256" not in out.markdown, "the marker must not quote the withheld text"
    assert all(f"- {c}" in out.markdown for c in CLEAN)
    assert detect_contamination(out.markdown, "") is None


def test_the_guards_own_audit_shape_degrades(slept) -> None:
    """G2. d2cbddef: an audit of the guard has to name its positive controls."""
    _delays, sleep = slept
    out = summarize_log(
        _log(),
        _three(_digest(done=[*CLEAN[:3], "Checked that wrote N docs still fires"])),
        sleep=sleep,
    )
    assert out.ok is True and out.digest.done == CLEAN[:3] and out.withheld_items == 1


def test_a_digest_that_is_mostly_residue_is_still_suppressed(slept) -> None:
    """G3. Withholding more than half the items would launder a template echo."""
    _delays, sleep = slept
    dirty = _digest(done=[CLEAN[0], "Closed N tickets", "wrote N docs"])
    out = summarize_log(_log(), _three(dirty), sleep=sleep)
    assert out.suppressed is True and out.ok is False and out.withheld_items == 0
    assert out.contamination_signal == "residue_token"
    assert "contamination guard" in out.markdown


@pytest.mark.parametrize(
    "clean,dirty,degraded",
    [
        (2, 2, True),  # exactly half withheld: degraded
        (1, 2, False),  # one clean item fewer: suppressed
        (3, 3, True),
        (2, 3, False),
        (0, 1, False),  # nothing would be left
    ],
)
def test_the_majority_floor_boundary(slept, clean: int, dirty: int, degraded: bool) -> None:
    """G3, the boundary on both sides. Items are spread over two fields, because the floor
    counts the digest's list items in total, not per field."""
    _delays, sleep = slept
    bad = [f"wrote N docs in batch {i}" for i in range(dirty)]
    text = _digest(done=[*CLEAN[:clean], *bad[:1]], found=bad[1:])
    out = summarize_log(_log(), _three(text), sleep=sleep)
    assert out.ok is degraded and out.suppressed is not degraded
    assert out.withheld_items == (dirty if degraded else 0)


def test_a_field_emptied_by_the_guard_keeps_its_label_and_marker(slept) -> None:
    _delays, sleep = slept
    out = summarize_log(
        _log(), _three(_digest(done=CLEAN[:2], found=["wrote N docs"])), sleep=sleep
    )
    assert out.ok is True and out.digest.found == []
    lines = out.markdown.splitlines()
    assert lines[lines.index("**Found:**") + 1].startswith(WITHHELD_MARKER)


def test_residue_in_asked_always_suppresses(slept) -> None:
    """G4. `asked` is one string, read first, with no partial form."""
    _delays, sleep = slept
    out = summarize_log(_log(), _three(_digest(asked="Close N tickets", done=CLEAN)), sleep=sleep)
    assert out.suppressed is True and out.ok is False and out.withheld_items == 0


def test_degrade_is_never_reached_before_the_last_attempt(slept) -> None:
    """G5. The reminder gets its chance first."""
    _delays, sleep = slept
    dirty = _digest(found=[*CLEAN, "wrote N docs"])
    provider = Scripted(dirty, GOOD)
    out = summarize_log(_log(), provider, sleep=sleep)
    assert out.ok is True and out.attempts == 2
    assert out.withheld_items == 0 and out.contamination_signal == ""
    assert WITHHELD_MARKER not in out.markdown
    assert "IMPORTANT" in provider.calls[1][0]


@pytest.mark.parametrize(
    "fields,signal",
    [
        ({"done": [*CLEAN, "set <Env vars here"]}, "template_signature"),
        ({"done": [*CLEAN, "did <specific step>", "wrote N docs"]}, "placeholder_token"),
        ({"found": SOURCE}, "verbatim_overlap"),
    ],
)
def test_other_terminal_signals_are_never_degraded(
    slept, line_corpus, fields: dict, signal: str
) -> None:
    """G6."""
    _delays, sleep = slept
    out = summarize_log(_log(), _three(_digest(**fields)), sleep=sleep)
    assert out.suppressed is True and out.ok is False
    assert out.contamination_signal == signal and out.withheld_items == 0
    assert WITHHELD_MARKER not in out.markdown


def test_a_recheck_that_finds_something_else_suppresses_and_records_it(slept, line_corpus) -> None:
    """G7. Dropping the residue item joins three source lines into a verbatim run, which the
    first pass never got to because residue is checked before overlap."""
    _delays, sleep = slept
    found = [SOURCE[0], SOURCE[1], "wrote N docs", SOURCE[2]]
    out = summarize_log(_log(), _three(_digest(found=found)), sleep=sleep)
    assert out.suppressed is True and out.ok is False and out.withheld_items == 0
    assert out.contamination_signal == "verbatim_overlap"
    assert out.contamination_rule == "" and out.contamination_excerpt == ""
    assert out.reason == "contamination: verbatim_overlap"


def test_the_recheck_runs_the_full_detector_once(slept, monkeypatch) -> None:
    """G7, mechanically: whatever the re-check returns is what is recorded, and there is no
    second pruning pass."""
    _delays, sleep = slept
    calls: list[str] = []

    def fake(rendered, corpus):
        calls.append(rendered)
        if WITHHELD_MARKER in rendered:
            return Detection(signal="residue_token", token="M", rule="count_letter")
        return Detection(signal="residue_token", token="N", rule="count_letter")

    monkeypatch.setattr("scribe.summarize.runner.explain_contamination", fake)
    out = summarize_log(_log(), _three(_digest(found=[*CLEAN, "wrote N docs"])), sleep=sleep)
    assert out.suppressed is True and len(calls) == 4, "three attempts and one re-check"


def test_the_stored_excerpt_is_redacted(slept, monkeypatch) -> None:
    """X1 at the runner. Must FAIL with the scrub call removed: the secret sits inside the
    raw 80-character window, so only the redactor can keep it out."""
    _delays, sleep = slept
    secret = "ghp_FAKE1234567890abcdefghijklmnop"
    for text in (
        _digest(asked=f"Close N tickets using {secret}", done=CLEAN),  # suppressed
        _digest(found=[*CLEAN, f"wrote N docs with {secret}"]),  # degraded
    ):
        out = summarize_log(_log(), _three(text), sleep=sleep)
        assert out.contamination_excerpt, "the probe must store an excerpt"
        assert secret not in out.contamination_excerpt and "FAKE" not in out.contamination_excerpt
        assert REDACTED in out.contamination_excerpt
        assert not any(secret in e or REDACTED in e for e in out.errors)


def test_a_lone_surrogate_in_the_excerpt_cannot_break_the_record(slept) -> None:
    """Pre-audit F-2. The excerpt is model text, and provider JSON can decode to a lone
    surrogate, which a strict UTF-8 write of the run record refuses."""
    _delays, sleep = slept
    out = summarize_log(
        _log(), _three(_digest(asked="Close N tickets \ud83d now", done=CLEAN)), sleep=sleep
    )
    assert out.contamination_excerpt
    out.contamination_excerpt.encode("utf-8")  # strict: raises if a surrogate survived


@pytest.mark.parametrize(
    "token,shown",
    [("N", "N"), ("256/N", "256/N"), ("YYYY-MM-DD", "YYYY-MM-DD"), ("1234567890123456/N", "#/N")],
)
def test_the_span_token_masks_long_digit_runs(token: str, shown: str) -> None:
    from scribe.summarize.runner import SPAN_TOKEN_CHARS, _span_token

    assert _span_token(token) == shown and len(shown) <= SPAN_TOKEN_CHARS
