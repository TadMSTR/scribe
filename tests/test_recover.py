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

from scribe.recover_cli import recover, scan_corpus, stamp
from scribe.state import STATUS_COMPLETE, Store
from scribe.summarize.contamination import SUPPRESSION_MARKER
from scribe.summarize.render import PLACEHOLDER_MARKER, render_failure, render_suppressed
from scribe.writeback import existing_turns, provisional_turns

WHEN = datetime(2026, 8, 19, 9, 14, tzinfo=UTC)


class _Cfg:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = str(output_dir)


def _legacy_block(turn: str, transcript: str, body: str) -> str:
    """A block exactly as the corpus holds it: complete, and with no provisional attribute."""
    return (
        f"## Session 09:14\n\n"
        f"<!-- session:sess-{turn} turn:{turn} transcript:{transcript} -->\n"
        f"{body}"
        f"<!-- /scribe turn:{turn} -->\n\n"
    )


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
    return _Cfg(out), store, md, paths


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


def test_a_vanished_transcript_is_reported_not_reopened(corpus, tmp_path) -> None:
    """Reopening a row whose transcript is gone would burn a sweep rediscovering that there
    is nothing to summarize."""
    cfg, store, _md, paths = corpus
    Path(paths["lost"]).unlink()
    report = recover(cfg, store, apply=True)
    assert paths["lost"] in report["missing"]
    assert paths["lost"] not in report["reset"]
    assert store.get(paths["lost"]).status == "summarized"


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
