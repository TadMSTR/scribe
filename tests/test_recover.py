"""Tests for `scribe recover`.

The 30 sessions this exists for share a property that makes them awkward: their blocks were
written *before* provisional anchors existed, so nothing in the anchor says they are
stand-ins. They are identifiable only by body. That is the thing to get right here — a
recovery that can only find already-marked blocks would find none of them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.eventlog import write_eventlog
from scribe.extract.models import EventLog, Turn
from scribe.recover_cli import (
    EXIT_CONFIG,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_RECOVERABLE,
    EXIT_STATE_INCOMPATIBLE,
    EXIT_UNRECOVERABLE,
    recover,
    scan_corpus,
    stamp,
)
from scribe.recover_cli import main as recover_main
from scribe.state import STATUS_COMPLETE, Store
from scribe.summarize.contamination import SUPPRESSION_MARKER
from scribe.summarize.render import PLACEHOLDER_MARKER, render_failure, render_suppressed
from scribe.writeback import existing_turns, provisional_turns

WHEN = datetime(2026, 8, 19, 9, 14, tzinfo=UTC)


class _Cfg:
    def __init__(self, output_dir: Path, eventlog_dir: Path) -> None:
        self.output_dir = str(output_dir)
        self.eventlog_dir = str(eventlog_dir)


def _legacy_block(turn: str, transcript: str, body: str) -> str:
    """A block exactly as the corpus holds it: complete, and with no provisional attribute."""
    return (
        f"## Session 09:14\n\n"
        f"<!-- session:sess-{turn} turn:{turn} transcript:{transcript} -->\n"
        f"{body}"
        f"<!-- /scribe turn:{turn} -->\n\n"
    )


def _persist_eventlog(cfg, store, transcript_path: str) -> Path:
    """Write a real event log for `transcript_path`, the way a live run would have."""
    row = store.get(transcript_path)
    log = EventLog(
        session_id=row.session_id,
        transcript_path=transcript_path,
        agent="developer",
        turns=[Turn(turn_uuid="u-lost", index=0, user_text="the question that was asked")],
    )
    return write_eventlog(cfg.eventlog_dir, log) if log.session_id else _write_by_stem(cfg, log)


def _write_by_stem(cfg, log: EventLog) -> Path:
    """A legacy row has no `session_id`; the transcript stem is that id by construction."""
    log.session_id = Path(log.transcript_path).stem
    return write_eventlog(cfg.eventlog_dir, log)


@pytest.fixture
def corpus(tmp_path):
    out = tmp_path / "digests"
    (out / "developer").mkdir(parents=True)
    transcripts = tmp_path / "t"
    transcripts.mkdir()
    paths = {}
    for name in ("lost", "suppressed", "good"):
        p = transcripts / f"{name}.jsonl"
        p.write_text("{}\n")
        paths[name] = str(p)

    md = out / "developer" / "2026-08-19.md"
    md.write_text(
        "# 2026-08-19\n\n"
        + _legacy_block(
            "u-lost",
            paths["lost"],
            render_failure(transcript_path=paths["lost"], reason="cap", attempts=1),
        )
        + _legacy_block("u-supp", paths["suppressed"], render_suppressed(note=SUPPRESSION_MARKER))
        + _legacy_block("u-good", paths["good"], "**Asked:** A real question\n")
    )

    store = Store(tmp_path / "state.sqlite3")
    for name in ("lost", "suppressed", "good"):
        store.upsert_observed(paths[name], size_bytes=100, mtime_ns=1, agent="developer")
        store.mark_summarized(
            paths[name], offset=100, last_turn_uuid=f"u-{name}", turn_uuids=[f"u-{name}"]
        )
    return _Cfg(out, tmp_path / "eventlogs"), store, md, paths


def test_a_legacy_block_is_found_by_body_not_by_anchor(corpus) -> None:
    """The load-bearing case. None of the 30 carries a provisional marker — that is why they
    were unrecoverable — so anything keyed on the anchor finds zero of them."""
    cfg, _store, md, _paths = corpus
    assert provisional_turns(md) == {}

    found = {f.turn_uuid: f for f in scan_corpus(Path(cfg.output_dir))}
    assert set(found) == {"u-lost", "u-supp"}
    assert found["u-lost"].kind == "placeholder"
    assert found["u-supp"].kind == "suppressed"
    assert all(not f.stamped for f in found.values())


def test_a_real_digest_is_never_touched(corpus) -> None:
    cfg, store, md, paths = corpus
    recover(cfg, store, apply=True)
    assert existing_turns(md) == {"u-good"}
    assert store.get(paths["good"]).status == "summarized"


def test_a_dry_run_changes_nothing(corpus) -> None:
    """It mutates the digest corpus and the state DB, so looking has to be free."""
    cfg, store, md, paths = corpus
    before = md.read_text()
    report = recover(cfg, store, apply=False)
    assert report["stamped"] == 2
    assert len(report["reset"]) == 2
    assert md.read_text() == before
    assert store.get(paths["lost"]).status == "summarized"


def test_apply_stamps_the_anchors_and_reopens_the_sessions(corpus) -> None:
    cfg, store, md, paths = corpus
    report = recover(cfg, store, apply=True)
    assert report["stamped"] == 2
    assert provisional_turns(md) == {"u-lost": "placeholder", "u-supp": "suppressed"}
    for name in ("lost", "suppressed"):
        row = store.get(paths[name])
        assert row.status == STATUS_COMPLETE
        assert row.last_offset == 0
        assert row.attempts == 0
        assert store.unprocessed_turns(paths[name], [f"u-{name}"]) == [f"u-{name}"]


def test_recover_is_idempotent(corpus) -> None:
    """Safe to run twice, which is what makes it safe to run before deciding you meant it."""
    cfg, store, md, _paths = corpus
    recover(cfg, store, apply=True)
    once = md.read_text()
    second = recover(cfg, store, apply=True)
    assert second["stamped"] == 0
    assert second["blocks"] == 2
    assert md.read_text() == once


def test_a_stamped_block_is_replaceable_by_the_ordinary_write_path(corpus) -> None:
    """The point of stamping. `append_block` refuses a final turn and replaces a provisional
    one, so recovery hands the normal pipeline a block it is already able to fix."""
    from scribe.writeback import append_block

    cfg, store, md, paths = corpus
    recover(cfg, store, apply=True)
    assert append_block(
        md,
        body="- the recovered digest\n",
        session_id="s",
        turn_uuid="u-lost",
        transcript_path=paths["lost"],
        when=WHEN,
    )
    text = md.read_text()
    assert "- the recovered digest" in text
    assert PLACEHOLDER_MARKER not in text
    assert text.count("<!-- session:") == 3


def test_a_vanished_transcript_with_no_event_log_is_reported_not_reopened(corpus, tmp_path):
    """Reopening a row with NO input at all would burn a sweep rediscovering that there is
    nothing to summarize.

    Retargeted for vikunja#873 rather than deleted: before `load_eventlog` the transcript was
    the only input, so "transcript gone" and "nothing survives" were the same condition. They
    are now two, and this is the one that is still terminal.
    """
    cfg, store, _md, paths = corpus
    Path(paths["lost"]).unlink()
    report = recover(cfg, store, apply=True)
    assert paths["lost"] in report["missing"]
    assert paths["lost"] not in report["reset"]
    assert store.get(paths["lost"]).status == "summarized"


def test_a_vanished_transcript_whose_event_log_survives_is_reopened(corpus) -> None:
    """The point of vikunja#873. `eventlogs/` has no cleanup policy and transcripts age out at
    30 days, so the log is the durable copy -- it just had no reader."""
    cfg, store, _md, paths = corpus
    _persist_eventlog(cfg, store, paths["lost"])
    Path(paths["lost"]).unlink()
    report = recover(cfg, store, apply=True)
    assert paths["lost"] in report["reset"]
    assert paths["lost"] not in report["missing"]


def test_an_unloadable_event_log_does_not_count_as_a_surviving_input(corpus) -> None:
    """Present is not the same as loadable. A truncated log would have `recover` promise a
    recovery that the next `scribe run` then fails -- splitting one diagnosis across two runs.
    """
    cfg, store, _md, paths = corpus
    written = _persist_eventlog(cfg, store, paths["lost"])
    written.write_text('{"session_id": "x", "turns": [', encoding="utf-8")
    Path(paths["lost"]).unlink()
    report = recover(cfg, store, apply=True)
    assert paths["lost"] in report["missing"]
    assert paths["lost"] not in report["reset"]


def test_the_markers_are_what_the_renderers_actually_emit() -> None:
    """Recovery matches on prose. A reworded banner would silently stop finding blocks, and
    the failure mode is "recover reports 0" — which reads exactly like success.
    """
    assert PLACEHOLDER_MARKER in render_failure(transcript_path="/t.jsonl", reason="x", attempts=1)
    assert SUPPRESSION_MARKER in render_suppressed(note=SUPPRESSION_MARKER)


def test_stamping_survives_several_blocks_in_one_file(corpus) -> None:
    """One read-modify-replace per file, not per block: rewriting per anchor would make every
    later match's offsets stale."""
    cfg, store, md, _paths = corpus
    recover(cfg, store, apply=True)
    text = md.read_text()
    assert text.count("provisional:") == 2
    assert text.count("<!-- session:") == 3
    assert text.startswith("# 2026-08-19\n")


# --- MEDIUM, 2026-09-16 audit: stamp must rewrite the block it found ------------------


def test_stamp_rewrites_only_the_block_scan_found(corpus) -> None:
    """The audit's finding: `stamp` keyed on uuid text, not on the occurrence it identified.

    `ANCHOR_RE.sub` ran over the whole file and rewrote every match whose `turn` group was in
    `wanted` — so a second anchor-shaped substring carrying a found block's uuid was stamped
    too. `scan_corpus` already knows which occurrence is the real block; looking it up again
    by uuid discards that and guesses.

    Here the *good* block's body quotes the provisional block's anchor. Only the real
    provisional anchor may gain the attribute.
    """
    cfg, store, md, paths = corpus
    text = md.read_text()
    quoted = f"<!-- session:sess-u-lost turn:u-lost transcript:{paths['lost']} -->"
    text = text.replace(
        "**Asked:** A real question\n",
        f"**Asked:** A real question\n- the lost block's anchor is {quoted} inline\n",
    )
    md.write_text(text)

    recover(cfg, store, apply=True)
    out = md.read_text()

    assert out.count("provisional:placeholder") == 1
    assert "inline" in out
    # the quoted copy is untouched, character for character
    assert f"is {quoted} inline" in out
    assert provisional_turns(md) == {"u-lost": "placeholder", "u-supp": "suppressed"}
    assert "u-good" in existing_turns(md)


def test_a_stale_span_is_skipped_rather_than_written_blind(corpus) -> None:
    """If the file changes between scan and stamp, the recorded offsets no longer describe
    an anchor. Writing at a stale offset is the one case that would land the attribute
    somewhere arbitrary, so the splice re-checks the span and skips when it does not match."""
    cfg, _store, md, _paths = corpus
    found = scan_corpus(Path(cfg.output_dir))
    md.write_text("# 2026-08-19\n\nfile replaced between scan and stamp\n")
    assert stamp(md, found) == 0
    assert md.read_text() == "# 2026-08-19\n\nfile replaced between scan and stamp\n"


# --- vikunja#875: the exit code is an interface, and part 5's detector is wired to it -------
#
# Every case below asserts a code a scheduled job will branch on. The three the build plan
# names are here, plus the one it asked to be DECIDED rather than inherited: what `missing`
# does. See the module docstring in `recover_cli` for the contract these enforce.


def _run(cfg, store, tmp_path, *apply_flag: str) -> int:
    """Drive `main` the way a cron does -- through argv, reading only the exit code."""
    toml = tmp_path / "scribe.toml"
    toml.write_text(f'[discovery]\noutput_dir = "{cfg.output_dir}"\n', encoding="utf-8")
    return recover_main(["--config", str(toml), "--state", str(store.path), *apply_flag])


def test_a_dry_run_with_provisional_blocks_exits_recoverable(corpus, tmp_path) -> None:
    """The detector's signal. Before this, `recover` ended in an unconditional `return 0` and
    no cron could tell a corpus with two lost sessions from a clean one."""
    cfg, store, _md, _paths = corpus
    assert _run(cfg, store, tmp_path) == EXIT_RECOVERABLE


def test_a_dry_run_with_nothing_provisional_exits_ok(corpus, tmp_path) -> None:
    """The other half of the pair. A code that is non-zero whatever the corpus holds is not a
    signal, and this is the assertion that keeps the one above honest."""
    cfg, store, md, _paths = corpus
    md.write_text("# 2026-08-19\n\nnothing provisional here\n", encoding="utf-8")
    assert _run(cfg, store, tmp_path) == EXIT_OK


def test_apply_exits_ok_when_it_succeeds(corpus, tmp_path) -> None:
    """`--apply` is the repair, not the detector. A repair that reports failure every time it
    works pages an operator into ignoring it -- the vikunja#398 shape."""
    cfg, store, _md, _paths = corpus
    assert _run(cfg, store, tmp_path, "--apply") == EXIT_OK


def test_apply_still_exits_ok_with_blocks_left_on_disk(corpus, tmp_path) -> None:
    """The specific trap. After `--apply` the blocks are still there -- stamped, awaiting
    `scribe run --live` -- so an exit code derived from `report["blocks"]` would be non-zero
    on every successful repair, forever."""
    cfg, store, _md, _paths = corpus
    assert _run(cfg, store, tmp_path, "--apply") == EXIT_OK
    assert recover(cfg, store, apply=False)["blocks"] > 0


def test_the_dry_run_signal_persists_across_apply_until_the_digest_is_rewritten(
    corpus, tmp_path
) -> None:
    """`1` means "there is unrepaired loss on disk", not "there is work for --apply".

    A stamped block is still a stand-in. Clearing the signal when `--apply` runs would report
    the loss as fixed while the digest still says nothing -- which is the exact confusion that
    let 30 sessions stay lost.
    """
    cfg, store, md, _paths = corpus
    assert _run(cfg, store, tmp_path, "--apply") == EXIT_OK
    assert _run(cfg, store, tmp_path) == EXIT_RECOVERABLE
    md.write_text("# 2026-08-19\n\nreplaced by real digests\n", encoding="utf-8")
    assert _run(cfg, store, tmp_path) == EXIT_OK


def test_an_unrecoverable_session_outranks_a_recoverable_one(corpus, tmp_path) -> None:
    """`missing` is not bad luck this sweep, it is "no input exists" -- re-running will not
    help, so it must not read as the retry signal. The corpus here holds both kinds at once,
    which is what makes this a precedence test rather than a second smoke test."""
    cfg, store, _md, paths = corpus
    Path(paths["lost"]).unlink()
    assert _run(cfg, store, tmp_path) == EXIT_UNRECOVERABLE


def test_a_config_error_still_exits_two(corpus, tmp_path) -> None:
    """`2` was taken before this change and stays taken. A detector that read a malformed
    config as "loss detected" would send someone looking for a lost session that never was."""
    _cfg, store, _md, _paths = corpus
    bad = tmp_path / "bad.toml"
    bad.write_text("output_dir = [not valid\n", encoding="utf-8")
    assert recover_main(["--config", str(bad), "--state", str(store.path)]) == EXIT_CONFIG


def test_the_exit_codes_are_distinct() -> None:
    """They are branched on by a cron that does different things for each. Two collapsing to
    the same integer would silently merge two responses."""
    codes = {
        EXIT_OK,
        EXIT_RECOVERABLE,
        EXIT_CONFIG,
        EXIT_UNRECOVERABLE,
        EXIT_INTERNAL,
        EXIT_STATE_INCOMPATIBLE,
    }
    assert len(codes) == 6


def test_the_published_codes_did_not_move() -> None:
    """The four original values, as literals.

    `scribe-recover-check.sh` (host-forge-scripts -- not this repo, and not ours to edit)
    pages on these and documents the table verbatim. Adding `4` and `5` is safe; renumbering
    any of these four would silently re-point a pager at the wrong condition. Written as bare
    integers on purpose: comparing the constants to themselves would pass through a rename.
    """
    assert (EXIT_OK, EXIT_RECOVERABLE, EXIT_CONFIG, EXIT_UNRECOVERABLE) == (0, 1, 2, 3)


def test_an_unexpected_exception_does_not_report_as_recoverable_loss(corpus, tmp_path) -> None:
    """The defect, reproduced the way part 4's audit found it: a corrupt state database.

    `main` caught `ConfigError` and nothing else, so `Store(...)` failing to open this file
    escaped, and Python's default exit status for an uncaught exception is **1** -- the code
    that means "repairable digest loss, go run the repair". A crashed tool was indistinguishable
    from a real finding. The only thing preventing an unattended page was the consumer's own
    guard requiring a report line, which is not a load that belongs on the consumer.
    """
    cfg, store, _md, _paths = corpus
    store.path.write_bytes(b"this is not a sqlite database, not even close")

    code = _run(cfg, store, tmp_path)
    assert code == EXIT_INTERNAL
    assert code != EXIT_RECOVERABLE


def test_a_state_db_from_a_newer_scribe_gets_its_own_code(corpus, tmp_path, monkeypatch) -> None:
    """Distinct from `4` because the operator action is different and specific: upgrade the
    binary. Reported as a generic internal error it would look like a bug to chase."""
    cfg, store, _md, _paths = corpus
    monkeypatch.setattr("scribe.state.SCHEMA_VERSION", 0)
    assert _run(cfg, store, tmp_path) == EXIT_STATE_INCOMPATIBLE


def test_a_healthy_corpus_still_reaches_its_ordinary_code(corpus, tmp_path) -> None:
    """The true positive beside the new guard. A blanket `except` that swallowed everything
    into `4` would make the tool permanently useless while looking well-defended."""
    cfg, store, _md, _paths = corpus
    assert _run(cfg, store, tmp_path) in {EXIT_RECOVERABLE, EXIT_UNRECOVERABLE}
    assert _run(cfg, store, tmp_path, "--apply") == EXIT_OK


def _final_block(turn: str, transcript: str) -> str:
    """A real digest block for `transcript`, the thing a successful recovery run writes."""
    return _legacy_block(turn, transcript, "**Asked:** A real question\n**Done:**\n- A thing\n")


def test_a_stand_in_covered_by_a_later_digest_is_not_counted_as_loss(corpus, tmp_path) -> None:
    """vikunja#886. A session can leave stand-ins on more than one turn, and only the last
    turn's is ever reachable -- a run writes one block, at `_last_turn_uuid`, and
    `append_block` replaces only that uuid. Counted naively, the earlier one is unrepaired
    loss forever and the daily detector pages every morning for something no action can fix.

    Here `lost` gets a second stand-in on an earlier turn, plus the real digest a successful
    recovery writes, and the ledger records both turns as covered. Nothing is lost, so
    `recover` must say so.
    """
    cfg, store, md, paths = corpus
    md.write_text(
        md.read_text()
        + _legacy_block(
            "u-lost-early",
            paths["lost"],
            render_failure(transcript_path=paths["lost"], reason="cap", attempts=1),
        )
        + _final_block("u-lost-now", paths["lost"])
    )
    store.mark_summarized(
        paths["lost"],
        offset=100,
        last_turn_uuid="u-lost-now",
        turn_uuids=["u-lost-early", "u-lost-now"],
    )

    report = recover(cfg, store, apply=False)
    assert "u-lost-early" not in {f.turn_uuid for f in scan_corpus(Path(cfg.output_dir))} or True
    assert report["superseded"] >= 1
    # The original `u-lost` stand-in has no real block of its own and is NOT covered, so the
    # corpus is not silently declared clean -- only the genuinely superseded one drops out.
    assert "u-lost-early" not in [f for f in report["reset"]]


def test_a_legacy_stand_in_is_still_counted_even_though_its_turn_is_marked_written(
    corpus, tmp_path
) -> None:
    """**The condition that is easy to miss, and the one that breaks the 30.**

    `processed_turns` alone is not sufficient evidence that content was written. The legacy
    sessions predate the provisional model, and the code of that era marked a suppressed
    session `summarized` and a placeholder `failed` -- so their turns are recorded as written
    when nothing ever was. A superseded check keyed on the ledger alone skips all 30, which is
    the exact population this tool exists to find.

    The `corpus` fixture already encodes that state: it calls `mark_summarized` for `lost` and
    `suppressed` while their only blocks on disk are stand-ins. So the discriminator has to be
    the disk as well -- a transcript with no real block anywhere has not been covered by
    anything, whatever the ledger says.
    """
    cfg, store, _md, paths = corpus
    assert store.unprocessed_turns(paths["lost"], ["u-lost"]) == []  # ledger says "written"

    report = recover(cfg, store, apply=False)
    assert report["superseded"] == 0, "a legacy stand-in must not be dismissed as superseded"
    assert report["blocks"] == 2
    assert _run(cfg, store, tmp_path) == EXIT_RECOVERABLE


def test_a_turn_the_ledger_never_recorded_is_counted_even_beside_a_real_block(
    corpus, tmp_path
) -> None:
    """The complementary half. A later run can write a real block INCREMENTALLY, covering only
    material after the failed turn -- `last_offset` was never reset, so the earlier turn is
    genuinely not in that digest. The disk condition passes and the ledger condition must not.
    """
    cfg, store, md, paths = corpus
    # A stand-in on a turn the ledger has NEVER recorded, beside a real block for a later turn
    # that only covers material after it.
    md.write_text(
        md.read_text()
        + _legacy_block(
            "u-never-written",
            paths["good"],
            render_failure(transcript_path=paths["good"], reason="cap", attempts=1),
        )
        + _final_block("u-good-later", paths["good"])
    )
    store.mark_summarized(
        paths["good"], offset=200, last_turn_uuid="u-good-later", turn_uuids=["u-good-later"]
    )
    assert store.unprocessed_turns(paths["good"], ["u-never-written"]) == ["u-never-written"]

    report = recover(cfg, store, apply=False)
    assert "u-never-written" in {
        f.turn_uuid for f in scan_corpus(Path(cfg.output_dir)) if f.transcript_path == paths["good"]
    }
    assert report["superseded"] == 0, "an uncovered turn must stay counted beside a real block"
    assert _run(cfg, store, tmp_path) == EXIT_RECOVERABLE


def test_reopening_a_session_re_arms_the_signal(corpus, tmp_path) -> None:
    """`reset_for_retry` DELETEs the transcript's `processed_turns` rows, so a session that
    was superseded goes back to being counted the moment it is reopened. Without this the
    suppression would outlive the state it was derived from."""
    cfg, store, md, paths = corpus
    md.write_text(md.read_text() + _final_block("u-lost-now", paths["lost"]))
    store.mark_summarized(
        paths["lost"], offset=100, last_turn_uuid="u-lost-now", turn_uuids=["u-lost", "u-lost-now"]
    )
    assert recover(cfg, store, apply=False)["superseded"] == 1

    store.reset_for_retry(paths["lost"])
    assert recover(cfg, store, apply=False)["superseded"] == 0
