"""A later block for a session replaces the block it supersedes (vikunja#1347, Phase 4).

Measured before this: 46 of 681 sessions since 2026-10-01 held 2-5 blocks, every later one a
cumulative re-summary from the session's start, and both versions indexed. Developer session
808a51cf said 136 tests in one block and 198 in the next. A sweep summarizes the whole
session every time, so its block covers everything an earlier one said.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scribe import manifest
from scribe.config import Config, ProviderConfig, StageConfig
from scribe.pipeline import output_root, run_once
from scribe.recover_cli import EXIT_OK
from scribe.recover_cli import main as recover_main
from scribe.state import Store
from scribe.summarize.providers import Completion, Provider
from scribe.writeback import (
    PROVISIONAL_PLACEHOLDER,
    append_block,
    daily_path,
    paired_blocks,
    write_block,
)

FIXTURES = Path(__file__).parent / "fixtures"
WHEN = datetime(2026, 10, 31, 13, 40, tzinfo=UTC)


def _write(path: Path, *, session="S", turn="t1", body="- first\n", supersede=True, **kw):
    return write_block(
        path,
        body=body,
        session_id=session,
        turn_uuid=turn,
        transcript_path=f"/p/{session}.jsonl",
        when=WHEN,
        supersede=supersede,
        **kw,
    )


def _blocks(path: Path) -> list[tuple[str, str, str]]:
    """`(session, turn, body)` per complete block, in file order."""
    text = path.read_text()
    return [
        (a.group(1), a.group(2), text[a.end() : c.start()].strip()) for a, c in paired_blocks(text)
    ]


# --- writeback -----------------------------------------------------------------------------


def test_a_session_written_twice_ends_with_one_block_holding_the_second(tmp_path) -> None:
    md = tmp_path / "research" / "2026-10-31.md"
    _write(md, turn="t6", body="- 136 tests pass\n")
    _write(md, session="X", turn="x1", body="- another session\n")
    w = _write(md, turn="t14", body="- 198 tests pass\n")
    assert w.written and w.superseded == ["t6"]
    assert _blocks(md) == [("S", "t14", "- 198 tests pass"), ("X", "x1", "- another session")]
    text = md.read_text()
    # The session keeps its place and its one heading; nothing is left orphaned.
    assert text.count("## Session 13:40") == 2
    assert "136 tests" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_every_earlier_block_goes_and_takes_its_heading(tmp_path) -> None:
    md = tmp_path / "research" / "2026-10-31.md"
    for i, turn in enumerate(("t1", "t2", "t3")):
        append_block(
            md,
            body=f"- render {i}\n",
            session_id="S",
            turn_uuid=turn,
            transcript_path="/p/S.jsonl",
            when=WHEN,
        )
    w = _write(md, turn="t4", body="- render 3\n")
    assert w.superseded == ["t1", "t2", "t3"]
    assert _blocks(md) == [("S", "t4", "- render 3")]
    assert md.read_text().count("## Session") == 1


def test_a_provisional_block_is_superseded_too(tmp_path) -> None:
    md = tmp_path / "research" / "2026-10-31.md"
    _write(md, turn="t6", body="- placeholder\n", provisional=PROVISIONAL_PLACEHOLDER)
    w = _write(md, turn="t14", body="- the digest\n")
    assert w.superseded == ["t6"]
    assert _blocks(md) == [("S", "t14", "- the digest")]


def test_a_provisional_then_real_upgrade_of_one_turn_still_works(tmp_path) -> None:
    md = tmp_path / "research" / "2026-10-31.md"
    _write(md, turn="t6", body="- placeholder\n", provisional=PROVISIONAL_PLACEHOLDER)
    w = _write(md, turn="t6", body="- the digest\n")
    assert w.written and w.superseded == []
    ((anchor, _c),) = paired_blocks(md.read_text())
    assert anchor.group(4) is None
    assert _blocks(md) == [("S", "t6", "- the digest")]


def test_a_final_block_for_the_same_turn_is_still_never_overwritten(tmp_path) -> None:
    md = tmp_path / "research" / "2026-10-31.md"
    _write(md, turn="t6", body="- final\n")
    w = _write(md, turn="t6", body="- other\n")
    assert not w.written
    assert _blocks(md) == [("S", "t6", "- final")]


def test_without_supersede_the_old_append_behaviour_holds(tmp_path) -> None:
    md = tmp_path / "research" / "2026-10-31.md"
    _write(md, turn="t6", supersede=False)
    _write(md, turn="t14", supersede=False)
    assert [t for _s, t, _b in _blocks(md)] == ["t6", "t14"]


def test_a_quoted_anchor_in_a_body_does_not_count_as_a_block(tmp_path) -> None:
    md = tmp_path / "research" / "2026-10-31.md"
    _write(
        md, session="X", turn="x1", body="- quoting `<!-- session:S turn:t0 transcript:/p -->`\n"
    )
    w = _write(md, turn="t1")
    assert w.superseded == []
    assert len(_blocks(md)) == 2


# --- manifest ------------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    return Config(
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
    )


def test_a_tombstone_retires_the_key_and_check_stays_clean(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    md = Path(cfg.output_dir) / "research" / "2026-10-31.md"
    _write(md, turn="t6")
    manifest.append_turn(cfg, md, "t6")
    _write(md, turn="t14")
    manifest.append_tombstone(cfg, manifest.tombstone(cfg, md, "S", "t6", "t14"))
    manifest.append_turn(cfg, md, "t14")

    current = manifest.read(cfg.manifest_file())
    assert set(current.records) == {("research/2026-10-31.md", "t14")}
    assert current.tombstones == 1
    assert manifest.check(cfg).clean


def test_a_tombstone_is_skipped_by_a_consumer_that_wants_real_digests(tmp_path) -> None:
    """The README tells push indexers to filter on `provisional == ""`; a tombstone must not
    pass that filter even for a consumer that has never heard of `superseded_by`."""
    cfg = _cfg(tmp_path)
    md = Path(cfg.output_dir) / "research" / "2026-10-31.md"
    _write(md, turn="t14")
    manifest.append_tombstone(cfg, manifest.tombstone(cfg, md, "S", "t6", "t14"))
    lines = [json.loads(x) for x in cfg.manifest_file().read_text().splitlines()]
    assert [x for x in lines if x["provisional"] == ""] == []
    assert lines[0]["superseded_by"] == "t14"


def test_without_the_tombstone_check_reports_the_removed_block_stale(tmp_path) -> None:
    """Proves the tombstone is load-bearing, not decoration."""
    cfg = _cfg(tmp_path)
    md = Path(cfg.output_dir) / "research" / "2026-10-31.md"
    _write(md, turn="t6")
    manifest.append_turn(cfg, md, "t6")
    _write(md, turn="t14")
    manifest.append_turn(cfg, md, "t14")
    assert manifest.check(cfg).stale == [("research/2026-10-31.md", "t6")]


# --- the pipeline: the 808a51cf replay ------------------------------------------------------


class Sequenced(Provider):
    """Answers each call with the next digest, so two sweeps write two different renders."""

    name = "stub"
    model = "m"

    def __init__(self, *done: str) -> None:
        self.done = list(done)

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        text = json.dumps({"asked": "Run the build", "done": [self.done.pop(0)]})
        return Completion(text=text, input_tokens=1, output_tokens=1, model="m", provider="stub")


@pytest.fixture
def sweep_env(tmp_path):
    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-writer"
    d.mkdir(parents=True)
    target = d / "sess-subagents.jsonl"
    cfg = Config(
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out"),
        eventlog_dir=str(tmp_path / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
        qc_run_record=str(tmp_path / "runs.jsonl"),
        providers={"stub": ProviderConfig(name="stub", type="openai-compatible")},
        stages={"session": StageConfig(provider="stub", model="m")},
    )
    return cfg, Store(cfg.state_path), target


def _put(target: Path, lines: list[str], now: float) -> None:
    target.write_text("".join(lines))
    os.utime(target, (now - 3600, now - 3600))


def test_two_sweeps_leave_one_block_with_the_later_render(sweep_env, tmp_path) -> None:
    cfg, store, target = sweep_env
    lines = (FIXTURES / "transcript-subagents.jsonl").read_text().splitlines(keepends=True)
    cut = next(i for i, x in enumerate(lines) if '"uuid": "u6"' in x)
    provider = Sequenced("Ran 136 tests", "Ran 198 tests")

    _put(target, lines[:cut], 1_000_000.0)
    (first,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: provider)
    _put(target, lines, 1_010_000.0)
    (second,) = run_once(cfg, store, now=1_010_000.0, provider_factory=lambda: provider)

    assert first.written and first.superseded_blocks == 0
    assert second.written and second.superseded_blocks == 1
    (md,) = output_root(cfg).rglob("*.md")
    text = md.read_text()
    assert len(paired_blocks(text)) == 1
    assert "198 tests" in text and "136 tests" not in text
    assert manifest.check(cfg).clean
    (rec2,) = [r for r in map(json.loads, Path(cfg.qc_run_record).read_text().splitlines())][1:]
    assert rec2["superseded_blocks"] == 1

    # `recover` must not read the replaced turn as a lost digest.
    toml = tmp_path / "scribe.toml"
    toml.write_text(
        f'[discovery]\noutput_dir = "{cfg.output_dir}"\neventlog_dir = "{cfg.eventlog_dir}"\n'
        f'state_path = "{cfg.state_path}"\n'
    )
    assert recover_main(["--config", str(toml)]) == EXIT_OK


def test_a_block_left_in_the_next_days_file_is_reported(sweep_env) -> None:
    cfg, store, target = sweep_env
    lines = (FIXTURES / "transcript-subagents.jsonl").read_text().splitlines(keepends=True)
    _put(target, lines, 1_000_000.0)
    stray = daily_path(cfg.output_dir, "writer", WHEN.replace(day=1, month=11) + timedelta(days=1))
    append_block(
        stray,
        body="- stray\n",
        session_id="sess-subagents",
        turn_uuid="old",
        transcript_path=str(target),
        when=WHEN,
    )
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=lambda: Sequenced("x"))
    assert any("did not supersede" in e and stray.name in e for e in r.errors)


# --- Phase 7: `scribe index --check` enforces one block per session -------------------------


def _index_main(cfg: Config, tmp_path: Path, *args: str) -> int:
    from scribe.index_cli import main as index_main

    toml = tmp_path / "idx.toml"
    toml.write_text(
        f'[discovery]\noutput_dir = "{cfg.output_dir}"\neventlog_dir = "{cfg.eventlog_dir}"\n'
    )
    return index_main(["--check", "--config", str(toml), *args])


def _two_blocks(cfg: Config) -> Path:
    md = Path(cfg.output_dir) / "research" / "2026-10-31.md"
    _write(md, turn="t6", supersede=False)
    _write(md, turn="t14", supersede=False)
    manifest.write(cfg, manifest.rebuild(cfg))
    return md


def test_a_session_with_two_final_blocks_fails_with_its_own_code(tmp_path, capsys) -> None:
    from scribe.index_cli import EXIT_MULTI_BLOCK

    cfg = _cfg(tmp_path)
    _two_blocks(cfg)
    assert manifest.check(cfg).clean  # not drift: the manifest is right about the disk
    assert _index_main(cfg, tmp_path) == EXIT_MULTI_BLOCK
    out = capsys.readouterr().out
    assert out.startswith("manifest:")  # the scheduled caller's contract line
    assert "1 session(s) with more than one block" in out
    assert "multi    session:S  2 blocks: research/2026-10-31.md turn:t6" in out


def test_the_json_names_the_session_and_its_blocks(tmp_path, capsys) -> None:
    cfg = _cfg(tmp_path)
    _two_blocks(cfg)
    _index_main(cfg, tmp_path, "--json")
    doc = json.loads(capsys.readouterr().out)
    assert doc["one_block_per_session"] is False
    assert doc["multi_block"] == [
        {
            "session_id": "S",
            "blocks": [["research/2026-10-31.md", "t6"], ["research/2026-10-31.md", "t14"]],
        }
    ]


def test_drift_takes_precedence_over_multiple_blocks(tmp_path) -> None:
    from scribe.index_cli import EXIT_DRIFT

    cfg = _cfg(tmp_path)
    _two_blocks(cfg)
    cfg.manifest_file().unlink()
    assert _index_main(cfg, tmp_path) == EXIT_DRIFT


def test_a_provisional_extra_block_does_not_count(tmp_path) -> None:
    from scribe.index_cli import EXIT_OK

    cfg = _cfg(tmp_path)
    md = Path(cfg.output_dir) / "research" / "2026-10-31.md"
    _write(md, turn="t6", supersede=False, provisional=PROVISIONAL_PLACEHOLDER)
    _write(md, turn="t14", supersede=False)
    manifest.write(cfg, manifest.rebuild(cfg))
    assert _index_main(cfg, tmp_path) == EXIT_OK


def test_blocks_in_two_files_count_as_one_session(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    for day in ("2026-10-31", "2026-11-01"):
        _write(Path(cfg.output_dir) / "research" / f"{day}.md", turn=day, supersede=False)
    assert [sid for sid, _ in manifest.check(cfg).multi_block] == ["S"]


def test_an_unexpected_failure_in_the_check_is_not_drift(tmp_path, monkeypatch) -> None:
    """Baseline OV-27: an uncaught exception used to exit 1, the drift code."""
    from scribe import index_cli

    cfg = _cfg(tmp_path)

    def boom(_cfg):
        raise RuntimeError("x")

    monkeypatch.setattr(index_cli, "check", boom)
    assert _index_main(cfg, tmp_path) == index_cli.EXIT_INTERNAL


def test_pending_blocks_are_counted(tmp_path, capsys) -> None:
    cfg = _cfg(tmp_path)
    md = Path(cfg.output_dir) / "writer" / "2026-10-31.md"
    _write(md, turn="t1", provisional="pending")
    manifest.write(cfg, manifest.rebuild(cfg))
    _index_main(cfg, tmp_path)
    assert "1 pending block(s)" in capsys.readouterr().out
