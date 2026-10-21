"""`scribe correct`, and the corrections ledger the live path and `correct` both write.

The corpus is synthetic and holds every shape the rewrite has to tell apart: a conflated `#541`
against `task_id=541`, a grounded `#541`, `#65` beside `#655`, a coincidental number, a
provisional block, a torn block and a block with no event log. Only the first and the `#655`
half of the third are corrections.

The dry run is proven inert with hashes of every file under the tree, not by the absence of an
exception: a dry run that wrote a backup, a ledger line or a manifest line would pass a test
that only checked the digests.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe import correct_cli, corrections
from scribe.config import Config, ConfigError, ProviderConfig, StageConfig, load
from scribe.correct_cli import (
    EXIT_INTERNAL,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_USAGE,
    scan,
    take_lock,
)
from scribe.eventlog import write_eventlog
from scribe.extract.models import EventLog, Turn
from scribe.manifest import check, rebuild, write
from scribe.pipeline import run_once, summarize_run
from scribe.state import Store
from scribe.summarize.providers import Completion, Provider
from scribe.writeback import (
    PROVISIONAL_PLACEHOLDER,
    anchor,
    append_block,
    daily_path,
    paired_blocks,
)

WHEN = datetime(2026, 9, 23, 19, 40, tzinfo=UTC)
NEXT_DAY = datetime(2026, 9, 24, 9, 5, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"

#: session -> (agent, day, the log's only evidence, the digest body, provisional)
CORPUS = {
    "s-conflated": ("research", WHEN, "updated task_id=541", "- Closed #541 today\n", ""),
    "s-grounded": ("research", WHEN, "ticket #541 (task_id=541)", "- Closed #541 (id 541)\n", ""),
    "s-suffix": ("developer", WHEN, "updated task_id=655", "- Closed #655 and #65\n", ""),
    "s-coincidental": ("developer", WHEN, "ran 73 pipelines", "- See #73\n", ""),
    "s-provisional": (
        "developer",
        WHEN,
        "updated task_id=541",
        "- Closed #541\n",
        PROVISIONAL_PLACEHOLDER,
    ),
    "s-nolog": ("research", NEXT_DAY, None, "- Closed #541\n", ""),
}


def _transcript(session: str) -> str:
    return f"/var/tmp/projects/one/{session}.jsonl"


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(
        output_dir=str(tmp_path / "s" / "digests"),
        eventlog_dir=str(tmp_path / "s" / "eventlogs"),
    )
    for session, (agent, when, evidence, body, provisional) in CORPUS.items():
        if evidence is not None:
            write_eventlog(
                c.eventlog_dir,
                EventLog(
                    session_id=session,
                    transcript_path=_transcript(session),
                    turns=[Turn(turn_uuid=f"u-{session}", index=0, user_text=evidence)],
                ),
            )
        append_block(
            daily_path(c.output_dir, agent, when),
            body=body,
            session_id=session,
            turn_uuid=f"u-{session}",
            transcript_path=_transcript(session),
            when=when,
            provisional=provisional,
        )
    # A torn block: an anchor whose terminator never landed. Its log says it is conflated, so
    # only the tear keeps it out of the changes.
    write_eventlog(
        c.eventlog_dir,
        EventLog(
            session_id="s-torn",
            transcript_path=_transcript("s-torn"),
            turns=[Turn(turn_uuid="u-s-torn", index=0, user_text="task_id=541")],
        ),
    )
    md = daily_path(c.output_dir, "developer", WHEN)
    with md.open("a") as fh:
        fh.write(f"\n## Session 21:00\n\n{anchor('s-torn', 'u-s-torn', _transcript('s-torn'))}\n")
        fh.write("- Closed #541\n")
    write(c, rebuild(c))
    assert check(c).clean
    return c


def _toml(cfg: Config, tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "scribe.toml"
    p.write_text(
        f'[discovery]\noutput_dir = "{cfg.output_dir}"\neventlog_dir = "{cfg.eventlog_dir}"\n'
        + extra
    )
    return p


def _run(cfg: Config, tmp_path: Path, *args: str, extra: str = "") -> int:
    return correct_cli.main(["--config", str(_toml(cfg, tmp_path, extra)), *args])


def _tree(root: Path) -> dict[str, str]:
    """Every file under `root` -> its sha256. A dry run must leave this exactly as it was."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _ledger(cfg: Config) -> list[dict]:
    path = cfg.corrections_file()
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines()]


def _block_hash(text: str, turn_uuid: str) -> str:
    for a, c in paired_blocks(text):
        if a.group(2) == turn_uuid:
            return hashlib.sha256(text[a.start() : c.end()].encode()).hexdigest()
    raise LookupError(turn_uuid)


def _research(cfg: Config) -> Path:
    return daily_path(cfg.output_dir, "research", WHEN)


def _developer(cfg: Config) -> Path:
    return daily_path(cfg.output_dir, "developer", WHEN)


# --------------------------------------------------------------------------------------
# Selection and the dry run
# --------------------------------------------------------------------------------------


def test_scan_finds_exactly_the_conflated_refs(cfg) -> None:
    found = scan(cfg)
    assert found.blocks == 6  # the torn block is not a block
    assert found.no_eventlog == 1
    assert found.bad_eventlog == 0
    assert found.provisional == 1
    assert found.torn == 1
    assert {c.session_id for c in found.changes} == {"s-conflated", "s-suffix"}
    assert found.refs == 2
    assert len(found.files) == 2
    edits = {c.session_id: c.edits for c in found.changes}
    assert edits["s-conflated"] == [{"line": 2, "before": "#541", "after": "id 541"}]
    # `#655` is the conflation; `#65` beside it is a different ticket and stays.
    assert edits["s-suffix"] == [{"line": 2, "before": "#655", "after": "id 655"}]


def test_an_edit_line_is_counted_within_the_block(cfg) -> None:
    """`line` 1 is the anchor. The ledger's line must point at the line that changed."""
    (ch,) = [c for c in scan(cfg).changes if c.session_id == "s-conflated"]
    block = ch.path.read_text()
    a, c = next((a, c) for a, c in paired_blocks(block) if a.group(2) == ch.turn_uuid)
    lines = block[a.start() : c.end()].splitlines()
    assert "#541" in lines[ch.edits[0]["line"] - 1]


def test_the_dry_run_reports_and_writes_nothing(cfg, tmp_path, capsys) -> None:
    root = tmp_path / "s"
    before = _tree(root)
    assert _run(cfg, tmp_path) == EXIT_PENDING
    assert _tree(root) == before
    assert not cfg.corrections_file().exists()
    assert not cfg.backup_root().exists()
    out = capsys.readouterr().out
    assert "6 block(s) scanned, 2 to change, 2 ref(s), 2 file(s)" in out
    assert "1 without an event log" in out
    assert "1 provisional, 1 torn" in out
    assert "research/2026-09-23.md:" in out and "#541 -> id 541" in out


def test_the_dry_run_ignores_a_held_lock(cfg, tmp_path) -> None:
    """It only reads, so a sweep in progress is no reason to refuse it."""
    lock = tmp_path / "scribe.lock"
    fd = take_lock(lock)
    try:
        assert _run(cfg, tmp_path, "--lock", str(lock)) == EXIT_PENDING
    finally:
        os.close(fd)


def test_json_lists_every_edit(cfg, tmp_path, capsys) -> None:
    assert _run(cfg, tmp_path, "--json") == EXIT_PENDING
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocks_to_change"] == 2
    assert payload["skipped"] == {
        "no_eventlog": 1,
        "bad_eventlog": 0,
        "provisional": 1,
        "torn": 1,
        "undecodable_files": 0,
    }
    assert sorted(e["before"] for e in payload["edits"]) == ["#541", "#655"]
    assert all(e["line"] == 2 for e in payload["edits"])


def test_an_unloadable_event_log_is_skipped_and_counted(cfg) -> None:
    (Path(cfg.eventlog_dir) / "s-conflated.json").write_text("{not json")
    found = scan(cfg)
    assert found.bad_eventlog == 1
    assert {c.session_id for c in found.changes} == {"s-suffix"}


# --------------------------------------------------------------------------------------
# --apply
# --------------------------------------------------------------------------------------


def test_apply_changes_only_the_conflated_refs(cfg, tmp_path) -> None:
    research, developer = _research(cfg).read_text(), _developer(cfg).read_text()
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    assert _research(cfg).read_text() == research.replace(
        "- Closed #541 today", "- Closed id 541 today"
    )
    assert _developer(cfg).read_text() == developer.replace(
        "- Closed #655 and #65", "- Closed id 655 and #65"
    )
    # Untouched: grounded, coincidental, provisional, torn, and the block with no log.
    assert "- Closed #541 (id 541)" in _research(cfg).read_text()
    assert daily_path(cfg.output_dir, "research", NEXT_DAY).read_text().count("#541") == 1


def test_apply_backs_up_the_original_bytes_first(cfg, tmp_path, capsys) -> None:
    originals = {p: p.read_bytes() for p in (_research(cfg), _developer(cfg))}
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    (run,) = list(cfg.backup_root().iterdir())
    assert run.name.startswith("correct-")
    assert str(run) in capsys.readouterr().out
    root = Path(cfg.output_dir)
    copies = sorted(p for p in run.rglob("*") if p.is_file())
    assert [p.relative_to(run) for p in copies] == sorted(p.relative_to(root) for p in originals)
    for p, data in originals.items():
        copy = run / p.relative_to(root)
        assert copy.read_bytes() == data
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert stat.S_IMODE(run.stat().st_mode) == 0o700


def test_apply_appends_one_manifest_line_per_changed_block(cfg, tmp_path) -> None:
    lines = cfg.manifest_file().read_text().splitlines()
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    added = [json.loads(ln) for ln in cfg.manifest_file().read_text().splitlines()[len(lines) :]]
    assert sorted(r["session_id"] for r in added) == ["s-conflated", "s-suffix"]
    assert check(cfg).clean


def test_apply_writes_one_ledger_record_per_changed_block(cfg, tmp_path) -> None:
    before = {p: p.read_text() for p in (_research(cfg), _developer(cfg))}
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    records = _ledger(cfg)
    assert len(records) == 2
    by_session = {r["session_id"]: r for r in records}
    for session, path in (("s-conflated", _research(cfg)), ("s-suffix", _developer(cfg))):
        rec = by_session[session]
        turn = f"u-{session}"
        assert rec["source"] == corrections.SOURCE_CORRECT
        assert rec["reason"] == corrections.REASON_ID_CONFLATION
        assert rec["record_version"] == corrections.RECORD_VERSION
        assert rec["turn_uuid"] == turn
        assert rec["digest"] == path.relative_to(cfg.output_dir).as_posix()
        assert rec["sha256_before"] == _block_hash(before[path], turn)
        assert rec["sha256_after"] == _block_hash(path.read_text(), turn)
        assert rec["sha256_before"] != rec["sha256_after"]
    assert stat.S_IMODE(cfg.corrections_file().stat().st_mode) == 0o600


def test_the_ledger_stores_tokens_never_lines(cfg, tmp_path) -> None:
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    raw = cfg.corrections_file().read_text()
    assert "Closed" not in raw and "today" not in raw
    for rec in _ledger(cfg):
        for e in rec["edits"]:
            assert set(e) == {"line", "before", "after"}


def test_the_ledger_sha_after_matches_the_manifest(cfg, tmp_path) -> None:
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    manifest = {}
    for ln in cfg.manifest_file().read_text().splitlines():
        r = json.loads(ln)
        manifest[(r["path"], r["turn_uuid"])] = r["sha256"]
    for rec in _ledger(cfg):
        assert manifest[(rec["digest"], rec["turn_uuid"])] == rec["sha256_after"]


def test_the_hook_runs_once_per_changed_file(cfg, tmp_path) -> None:
    calls = tmp_path / "hook-calls"
    extra = f'[index]\non_digest_written = ["/bin/sh", "-c", "echo \\"$0\\" >> {calls}"]\n'
    assert _run(cfg, tmp_path, "--apply", "--no-lock", extra=extra) == EXIT_OK
    called = sorted(calls.read_text().splitlines())
    assert called == sorted(str(p.absolute()) for p in (_developer(cfg), _research(cfg)))


def test_a_disabled_ledger_writes_none_and_still_corrects(cfg, tmp_path) -> None:
    extra = '[index]\ncorrections_path = ""\n'
    assert _run(cfg, tmp_path, "--apply", "--no-lock", extra=extra) == EXIT_OK
    assert "id 541 today" in _research(cfg).read_text()
    assert not cfg.corrections_file().exists()
    assert check(cfg).clean


def test_a_failed_backup_leaves_the_file_untouched(cfg, tmp_path) -> None:
    """No backup, no write. The backup root is a FILE here, so it cannot be created."""
    blocker = tmp_path / "s" / "not-a-dir"
    blocker.write_text("x")
    before = _tree(Path(cfg.output_dir))
    extra = f'[index]\nbackup_dir = "{blocker}"\n'
    assert _run(cfg, tmp_path, "--apply", "--no-lock", extra=extra) == EXIT_INTERNAL
    assert _tree(Path(cfg.output_dir)) == before
    assert _ledger(cfg) == []
    assert check(cfg).clean


def test_a_splice_that_would_move_a_block_is_refused(cfg) -> None:
    """FW-13: the replace is whole either way; what is checked is that it is RIGHT."""
    found = scan(cfg)
    (ch,) = [c for c in found.changes if c.session_id == "s-conflated"]
    ch.body += "<!-- session:x turn:y transcript:x -->\nz\n<!-- /scribe turn:y -->\n"
    research = _research(cfg).read_bytes()
    done = correct_cli.apply(cfg, found)
    assert "moved a block" in done.aborted
    # Files sort `developer/` first, so that one was corrected before the refusal.
    assert _research(cfg).read_bytes() == research
    assert not list(Path(done.backup_dir).rglob("research/*"))


def test_a_file_changed_after_the_scan_is_left_alone(cfg, tmp_path) -> None:
    found = scan(cfg)
    md = _research(cfg)
    md.write_text(md.read_text() + "\n")
    done = correct_cli.apply(cfg, found)
    assert done.skipped_changed == ["research/2026-09-23.md"]
    assert "#541 today" in md.read_text()
    assert done.files == 1


def test_a_digest_with_carriage_returns_is_corrected_and_keeps_them(cfg, tmp_path) -> None:
    """Audit F-01. `read_text` translates `\r`, so a scan through it never matched the bytes
    re-read before the write: the file was skipped as changed on disk on every run, with exit 0."""
    md = _research(cfg)
    with md.open("ab") as fh:
        fh.write(b"\nmodel prose with a CRLF\r\nand a lone CR\r here\n")
    write(cfg, rebuild(cfg))
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    data = md.read_bytes()
    assert b"id 541 today" in data
    assert b"CRLF\r\n" in data and b"CR\r here" in data
    assert check(cfg).clean
    (rec,) = [r for r in _ledger(cfg) if r["session_id"] == "s-conflated"]
    manifest = [json.loads(ln) for ln in cfg.manifest_file().read_text().splitlines()]
    assert any(m["sha256"] == rec["sha256_after"] for m in manifest)
    assert _run(cfg, tmp_path) == EXIT_OK


def test_a_file_left_alone_makes_apply_exit_pending(cfg, tmp_path, monkeypatch, capsys) -> None:
    """Audit F-01, second half: a skipped file is a correction still pending, not a success."""
    real_scan = correct_cli.scan

    def scan_then_touch(c):
        found = real_scan(c)
        md = _research(c)
        md.write_text(md.read_text() + "\n")
        return found

    monkeypatch.setattr(correct_cli, "scan", scan_then_touch)
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_PENDING
    assert "changed on disk after the scan" in capsys.readouterr().out


def test_a_failed_replace_still_reports_progress(cfg, tmp_path, monkeypatch, capsys) -> None:
    """Audit F-02. The second file's replace fails: the first stays corrected and recorded,
    and the operator is told so, with the backup path, rather than handed a bare traceback."""
    real = correct_cli.atomic_replace
    calls = []

    def failing(path, text):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real(path, text)

    monkeypatch.setattr(correct_cli, "atomic_replace", failing)
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_INTERNAL
    out = capsys.readouterr().out
    (run,) = list(cfg.backup_root().iterdir())
    assert str(run) in out
    assert "1 block(s) corrected" in out
    assert "replace of research/2026-09-23.md failed" in out
    assert "#541 today" in _research(cfg).read_text()
    assert not list(Path(cfg.output_dir).rglob("*.tmp"))
    assert len(_ledger(cfg)) == 1
    assert check(cfg).clean


def test_a_file_that_is_not_utf8_is_skipped_and_counted(cfg, tmp_path, capsys) -> None:
    """Audit F-03. Decoding with `errors="replace"` and writing back would rewrite the bad byte
    in a block `correct` never meant to touch, and the ledger could not say so."""
    md = _research(cfg)
    with md.open("ab") as fh:
        fh.write(b"\nstray byte \xff here\n")
    before = md.read_bytes()
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    assert md.read_bytes() == before
    assert "1 file(s) not valid UTF-8" in capsys.readouterr().out
    assert scan(cfg).undecodable == 1


def test_with_the_rewrite_off_correct_does_nothing(cfg, tmp_path) -> None:
    before = _tree(tmp_path / "s")
    extra = "[qc]\nrewrite_id_conflations = false\n"
    assert _run(cfg, tmp_path, "--apply", "--no-lock", extra=extra) == EXIT_OK
    assert _tree(tmp_path / "s") == before


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


def test_a_second_apply_changes_nothing(cfg, tmp_path, capsys) -> None:
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    root = tmp_path / "s"
    after_first = _tree(root)
    backups = list(cfg.backup_root().iterdir())
    assert _run(cfg, tmp_path, "--apply", "--no-lock") == EXIT_OK
    assert _tree(root) == after_first
    assert list(cfg.backup_root().iterdir()) == backups
    assert len(_ledger(cfg)) == 2
    capsys.readouterr()
    assert _run(cfg, tmp_path) == EXIT_OK
    assert "0 to change" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------------------


def test_a_held_lock_exits_75_and_writes_nothing(cfg, tmp_path) -> None:
    lock = tmp_path / "scribe.lock"
    fd = take_lock(lock)
    assert fd is not None
    root = tmp_path / "s"
    before = _tree(root)
    try:
        assert _run(cfg, tmp_path, "--apply", "--lock", str(lock)) == EXIT_LOCKED
    finally:
        os.close(fd)
    assert _tree(root) == before


def test_a_free_lock_is_taken_and_the_apply_runs(cfg, tmp_path) -> None:
    lock = tmp_path / "scribe.lock"
    assert _run(cfg, tmp_path, "--apply", "--lock", str(lock)) == EXIT_OK
    assert "id 541 today" in _research(cfg).read_text()


def test_take_lock_excludes_a_second_holder(tmp_path) -> None:
    lock = tmp_path / "scribe.lock"
    first = take_lock(lock)
    try:
        assert take_lock(lock) is None
    finally:
        os.close(first)
    second = take_lock(lock)
    assert second is not None
    os.close(second)


def test_apply_without_a_lock_choice_is_refused(cfg, tmp_path) -> None:
    root = tmp_path / "s"
    before = _tree(root)
    assert _run(cfg, tmp_path, "--apply") == EXIT_USAGE
    assert _tree(root) == before


def test_lock_and_no_lock_together_are_refused(cfg, tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(cfg, tmp_path, "--apply", "--lock", "x", "--no-lock")
    assert exc.value.code == EXIT_USAGE


def test_an_unopenable_lock_is_a_usage_error(cfg, tmp_path) -> None:
    missing = tmp_path / "no" / "such" / "dir" / "scribe.lock"
    assert _run(cfg, tmp_path, "--apply", "--lock", str(missing)) == EXIT_USAGE


def test_a_config_error_is_a_usage_error(tmp_path) -> None:
    assert correct_cli.main(["--config", str(tmp_path / "missing.toml")]) == EXIT_USAGE


def test_an_unexpected_failure_is_exit_4(cfg, tmp_path, monkeypatch) -> None:
    def boom(_cfg):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(correct_cli, "scan", boom)
    assert _run(cfg, tmp_path) == EXIT_INTERNAL


# --------------------------------------------------------------------------------------
# Config: the ledger and the backup root stay outside both trees
# --------------------------------------------------------------------------------------


def _load(tmp_path: Path, body: str) -> Config:
    p = tmp_path / "c.toml"
    p.write_text(
        f'[discovery]\noutput_dir = "{tmp_path}/s/digests"\n'
        f'eventlog_dir = "{tmp_path}/s/eventlogs"\n' + body
    )
    return load(p)


def test_the_defaults_are_siblings_of_the_digest_root(tmp_path) -> None:
    cfg = _load(tmp_path, "")
    assert cfg.corrections_file() == tmp_path / "s" / "corrections.jsonl"
    assert cfg.backup_root() == tmp_path / "s" / "backups"


def test_an_empty_corrections_path_disables_the_ledger(tmp_path) -> None:
    assert _load(tmp_path, '[index]\ncorrections_path = ""\n').corrections_file() is None


@pytest.mark.parametrize("key", ["corrections_path", "backup_dir"])
@pytest.mark.parametrize("where", ["s/digests/x", "s/eventlogs/x", "s/digests"])
def test_a_path_inside_either_tree_is_refused(tmp_path, key, where) -> None:
    with pytest.raises(ConfigError, match=f"index.{key}.*inside"):
        _load(tmp_path, f'[index]\n{key} = "{tmp_path}/{where}"\n')


@pytest.mark.parametrize("key", ["corrections_path", "backup_dir"])
def test_a_non_string_path_is_refused(tmp_path, key) -> None:
    with pytest.raises(ConfigError, match=key):
        _load(tmp_path, f"[index]\n{key} = 3\n")


def test_a_default_swallowed_by_the_event_log_tree_is_refused(tmp_path) -> None:
    """`eventlog_dir` = the digest root's parent puts every default sibling inside it."""
    p = tmp_path / "c.toml"
    p.write_text(
        f'[discovery]\noutput_dir = "{tmp_path}/s/d"\neventlog_dir = "{tmp_path}/s"\n'
        f'[index]\nmanifest_path = "{tmp_path}/elsewhere/index.jsonl"\n'
    )
    with pytest.raises(ConfigError, match=r"backup_dir.*eventlog_dir"):
        load(p)


# --------------------------------------------------------------------------------------
# The live path writes the ledger too
# --------------------------------------------------------------------------------------

CONFLATED = json.dumps(
    {
        "asked": "Check the queue entry for id 417 and summarise the upstream PRs.",
        "done": ["Read `/srv/app/src/tools/queue.py`"],
        "tickets": ["#417", "#655"],
    }
)


class Stub(Provider):
    name = "stub"
    model = "m"

    def complete(self, system, user, *, timeout=120.0) -> Completion:
        return Completion(text=CONFLATED, input_tokens=1, output_tokens=1, provider="stub")


@pytest.fixture
def live(tmp_path):
    projects = tmp_path / "projects"
    d = projects / "-home-user--claude-projects-research"
    d.mkdir(parents=True)
    target = d / "sess.jsonl"
    target.write_bytes((FIXTURES / "transcript-qc-regression.jsonl").read_bytes())
    old = 1_000_000.0 - 3600
    os.utime(target, (old, old))
    cfg = Config(
        project_globs=(str(projects / "*") + "/",),
        output_dir=str(tmp_path / "out" / "digests"),
        eventlog_dir=str(tmp_path / "out" / "eventlogs"),
        state_path=str(tmp_path / "state.sqlite3"),
        qc_run_record="",
        providers={"stub": ProviderConfig(name="stub", type="openai-compatible", model="m")},
        stages={"session": StageConfig(provider="stub", model="m")},
    )
    return cfg, Store(cfg.state_path)


def test_a_live_rewrite_appends_one_live_record(live) -> None:
    cfg, store = live
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert r.written and r.ticket_refs_rewritten == 1
    assert r.correction_error == ""
    (rec,) = _ledger(cfg)
    assert rec["source"] == corrections.SOURCE_LIVE
    # The unrewritten block never existed on disk, so there is nothing to hash.
    assert rec["sha256_before"] is None
    assert rec["edits"] == [{"line": rec["edits"][0]["line"], "before": "#417", "after": "id 417"}]
    (md,) = list(Path(cfg.output_dir).rglob("*.md"))
    text = md.read_text()
    assert rec["sha256_after"] == _block_hash(text, rec["turn_uuid"])
    (manifest_line,) = cfg.manifest_file().read_text().splitlines()
    assert json.loads(manifest_line)["sha256"] == rec["sha256_after"]
    a, c = next((a, c) for a, c in paired_blocks(text) if a.group(2) == rec["turn_uuid"])
    lines = text[a.start() : c.end()].splitlines()
    assert "id 417" in lines[rec["edits"][0]["line"] - 1]


def test_a_live_block_with_nothing_rewritten_gets_no_record(live) -> None:
    cfg, store = live
    cfg.qc_rewrite_id_conflations = False
    (r,) = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    assert r.written
    assert _ledger(cfg) == []


def test_a_failed_ledger_write_is_counted_and_the_block_is_still_written(live) -> None:
    cfg, store = live
    blocker = Path(cfg.output_dir).parent / "ledger-is-a-dir"
    blocker.mkdir(parents=True)
    cfg.corrections_path = str(blocker)
    results = run_once(cfg, store, now=1_000_000.0, provider_factory=Stub)
    (r,) = results
    assert r.written and r.manifest_appended
    assert r.correction_error
    assert r.correction_error not in r.errors
    assert summarize_run(results)["correction_errors"] == 1
    (md,) = list(Path(cfg.output_dir).rglob("*.md"))
    assert "id 417" in md.read_text()


def test_record_refuses_an_unknown_source_or_reason() -> None:
    kw = dict(
        digest="d.md",
        session_id="s",
        turn_uuid="u",
        edits=[],
        sha256_before=None,
        sha256_after=None,
    )
    with pytest.raises(ValueError, match="source"):
        corrections.record(source="hand", **kw)
    with pytest.raises(ValueError, match="reason"):
        corrections.record(source=corrections.SOURCE_LIVE, reason="typo", **kw)
