"""Every appended file is 0600 from `O_CREAT`, not only after the chmod that follows it.

The scribe-qc-monitoring audit (F-01, CodeRabbit CR-01) found the run record created with
`open("a")`, which applies the process umask, then tightened by `secure_file`. That leaves a
window in which other local users can read it. The same gap was in `writeback.append_block`
(the digest body) and `telemetry.record_spend`.

The existing mode tests could not see it: they check the mode after `secure_file` has run.
These stub `secure_file` out in the module under test. Anything still 0600 was created that
way by the kernel, so the assertion is about creation and not about the chmod that follows.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from scribe import runrecord, telemetry, writeback


@pytest.fixture
def permissive_umask():
    """0022, the common default: a plain `open()` would create 0644."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def no_chmod(monkeypatch):
    for module in (runrecord, telemetry, writeback):
        monkeypatch.setattr(module, "secure_file", lambda path: path)


def _mode(path) -> int:
    return path.stat().st_mode & 0o777


def test_the_run_record_is_created_owner_only(tmp_path, permissive_umask, no_chmod) -> None:
    path = tmp_path / "runs.jsonl"
    assert runrecord.append(path, {"a": 1}) == ""
    assert _mode(path) == 0o600


def test_the_spend_log_is_created_owner_only(tmp_path, permissive_umask, no_chmod) -> None:
    path = tmp_path / "tokens.log"
    telemetry.record_spend(model="m", input_tokens=1, output_tokens=1, log_path=path)
    assert _mode(path) == 0o600


def test_a_new_daily_digest_is_created_owner_only(tmp_path, permissive_umask, no_chmod) -> None:
    path = tmp_path / "developer" / "2026-09-28.md"
    path.parent.mkdir()
    assert writeback.append_block(
        path,
        body="### 12:00\n\n- did a thing\n",
        session_id="s1",
        turn_uuid="t1",
        transcript_path="/t/s1.jsonl",
        when=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
    )
    assert _mode(path) == 0o600


def test_the_stub_is_doing_its_job(tmp_path, permissive_umask, no_chmod) -> None:
    """Pins the method: with the chmod stubbed out, a plain `open("a")` really is 0644.
    Without this, a no-op fixture would let the three tests above pass for the wrong reason."""
    path = tmp_path / "plain.txt"
    with path.open("a") as fh:
        fh.write("x")
    assert _mode(path) == 0o644
