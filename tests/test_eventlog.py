"""The persisted event log — the drill-down tier.

Two properties carry the weight here, and neither is "a file appeared":

  * **What lands is a valid `scribe qc --events` input.** That is the whole reason to keep
    it, so it is checked by feeding a written file back through the gate's own loader rather
    than by comparing it to a schema this test wrote down separately.
  * **The session id cannot escape the root.** It comes out of the transcript, so it is
    untrusted, and a path built from untrusted input is a traversal until proven otherwise.
"""

from __future__ import annotations

import json
import stat
import tempfile
from pathlib import Path

import pytest

from scribe.config import DEFAULT_EVENTLOG_DIR, DEFAULT_OUTPUT_DIR
from scribe.eventlog import (
    SUFFIX,
    contains_value,
    eventlog_path,
    read_eventlog,
    safe_stem,
    write_eventlog,
)
from scribe.extract import extract
from scribe.extract.redact import Redactor
from scribe.qc_cli import _log_from_dict

FIXTURES = Path(__file__).parent / "fixtures"


def _log(tmp_path: Path, session_id: str = "0192f3c4-aaaa-bbbb-cccc-0123456789ab"):
    src = FIXTURES / "transcript-structural.jsonl"
    log = extract(src)
    log.session_id = session_id
    return log


def test_a_plain_session_id_is_used_verbatim() -> None:
    sid = "0192f3c4-aaaa-bbbb-cccc-0123456789ab"
    assert safe_stem(sid, "/t.jsonl") == sid


#: Each of these is a session id that must NOT become part of the path. Separators and
#: relative segments are the traversal; the rest are names that would be a file scribe did
#: not mean to create (a dotfile, a stem carrying its own extension, an empty name).
@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "..",
        ".",
        "/etc/passwd",
        "a/b",
        "a\\b",
        "",
        "   ",
        ".hidden",
        "sess.json",
        "s" * 129,
        "sess\x00id",
        "sess\nid",
        "sess id",
        "sessið",
    ],
)
def test_a_hostile_session_id_cannot_leave_the_root(hostile, tmp_path) -> None:
    """The resolved file must sit directly in the root, whatever the id said.

    Asserted on the *resolved* path rather than the string, because `a/../../b` is inside
    the root as a string and outside it as a location.
    """
    root = tmp_path / "eventlogs"
    root.mkdir()
    path = eventlog_path(root, hostile, "/home/ted/.claude/projects/p/sess.jsonl")
    assert path.resolve().parent == root.resolve()
    assert path.suffix == SUFFIX
    assert path.name.count(SUFFIX) == 1


def test_two_hostile_ids_from_different_transcripts_do_not_collide(tmp_path) -> None:
    """The fallback is derived from the transcript path, so two of them stay distinct.

    Sanitising instead of replacing is what makes this fail: `../a` and `..\\a` scrub to the
    same stem, and the second session then overwrites the first one's evidence.
    """
    a = eventlog_path(tmp_path, "../a", "/projects/one/sess.jsonl")
    b = eventlog_path(tmp_path, "..\\a", "/projects/two/sess.jsonl")
    assert a != b


def test_the_fallback_is_stable_across_runs(tmp_path) -> None:
    """A re-run must update that session's file, not accumulate a new one each sweep."""
    first = eventlog_path(tmp_path, "", "/projects/one/sess.jsonl")
    second = eventlog_path(tmp_path, "", "/projects/one/sess.jsonl")
    assert first == second


def test_what_is_written_is_a_valid_qc_events_input(tmp_path) -> None:
    """The contract, checked through the gate's own loader.

    `scribe qc --events` parses the file with `_log_from_dict`, and everything
    `grounding_text()` reads has to survive that round trip — otherwise the persisted log is
    a file that merely looks right and fails the moment anyone tries to re-check a digest
    with it.
    """
    log = _log(tmp_path)
    path = write_eventlog(tmp_path / "eventlogs", log)

    rebuilt = _log_from_dict(json.loads(path.read_text(encoding="utf-8")))

    assert rebuilt.session_id == log.session_id
    assert len(rebuilt.turns) == len(log.turns)
    assert [t.turn_uuid for t in rebuilt.turns] == [t.turn_uuid for t in log.turns]
    # The grounding corpus is what the gate actually scores against. Comparing it directly
    # is the difference between "the fields are present" and "the evidence is intact".
    assert rebuilt.grounding_text() == log.grounding_text()


def test_the_serialized_form_matches_extract_json_exactly(tmp_path) -> None:
    """Not a new format. `scribe extract --json` emits `to_dict`; so does this."""
    log = _log(tmp_path)
    path = write_eventlog(tmp_path / "eventlogs", log)
    assert json.loads(path.read_text(encoding="utf-8")) == log.to_dict()


def test_the_event_log_is_owner_only(tmp_path) -> None:
    """It is the least redacted thing scribe keeps, derived from a 0600 transcript."""
    root = tmp_path / "eventlogs"
    path = write_eventlog(root, _log(tmp_path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_rewriting_replaces_rather_than_accumulates(tmp_path) -> None:
    root = tmp_path / "eventlogs"
    log = _log(tmp_path)
    write_eventlog(root, log)
    log.agent = "changed"
    path = write_eventlog(root, log)

    assert list(root.iterdir()) == [path]
    assert json.loads(path.read_text(encoding="utf-8"))["agent"] == "changed"


def test_no_temp_file_survives_a_write(tmp_path) -> None:
    """A leftover `.tmp` is an unindexed, unowned copy of the same evidence."""
    root = tmp_path / "eventlogs"
    write_eventlog(root, _log(tmp_path))
    assert [p.name for p in root.iterdir() if p.name.endswith(".tmp")] == []


def test_read_eventlog_resolves_a_session_id(tmp_path) -> None:
    root = tmp_path / "eventlogs"
    log = _log(tmp_path)
    written = write_eventlog(root, log)
    assert read_eventlog(root, log.session_id) == written


def test_read_eventlog_returns_none_when_there_is_none(tmp_path) -> None:
    """Absent must be distinguishable from present-but-empty by the caller."""
    assert read_eventlog(tmp_path / "eventlogs", "no-such-session") is None


def test_the_eventlog_root_is_outside_the_digest_tree() -> None:
    """The structural exclusion, asserted rather than trusted.

    The `session-digests` qmd collection globs `<output_dir>/**/*.md`. Event logs stay
    unindexed because they are not under that root at all — so if someone ever nests them
    inside it, this fails here rather than silently swamping the digest signal with 39 MB of
    tool arguments in every semantic query.
    """
    digests = Path(DEFAULT_OUTPUT_DIR).expanduser()
    eventlogs = Path(DEFAULT_EVENTLOG_DIR).expanduser()
    assert digests != eventlogs
    assert digests not in eventlogs.parents
    # And nothing written there can match the collection's pattern even by accident.
    assert SUFFIX != ".md"


def test_field_level_and_whole_file_scrubbing_disagree_and_field_level_is_right() -> None:
    """The trap anyone auditing `eventlogs/` will walk into, recorded where they will hit it.

    Scrubbing a *serialized* event log produces matches that field-level scrubbing does not.
    `Redactor`'s `envvar` rule ends in the negated class ``[^\\s\\"',\\]\\}]+``, which stops at
    a quote *character* but knows nothing about JSON structure. In serialized form the match
    therefore runs straight through a structural `", "` boundary and swallows its neighbours.

    Measured on two real event logs: field-level fired **0** times across 1,797 string leaves;
    whole-file fired **7**, and all seven were spurious, reporting "values" of 155-668
    characters. The obvious audit approach — point a file scanner at the directory — is the
    wrong tool, and it fails in the direction that wastes the most time, by manufacturing
    findings rather than missing them.

    The tell asserted here is stronger than the match length, which varies with the
    surrounding content: a whole-file scrub **corrupts the document**, because it replaces
    bytes that were JSON syntax rather than JSON content. A redaction pass that destroys the
    evidence it is auditing is self-evidently the wrong pass.

    This is why `eventlog.contains_value` walks string leaves instead of searching raw text.
    """
    # Two shapes, both clean field-by-field: an unescaped structural boundary, and the
    # escaped-quote form a tool-argument blob actually takes.
    for payload in (
        {
            "cmd": "export API_TOKEN=",
            "next": "plain prose that follows",
            "third": "more ordinary text",
        },
        {"cmd": 'sh -c "export API_TOKEN=" && echo done', "note": "tail"},
    ):
        serialized = json.dumps(payload)

        field_level = Redactor()
        for leaf in payload.values():
            field_level.scrub(leaf)
        assert field_level.count == 0, f"field-level should find nothing in {payload}"

        whole_file = Redactor()
        scrubbed = whole_file.scrub(serialized)
        assert whole_file.count > field_level.count, (
            "if this ever stops being true the trap is gone and this test should be deleted "
            "rather than adjusted — but check that before assuming it"
        )
        with pytest.raises(ValueError):
            json.loads(scrubbed)


def test_contains_value_finds_a_value_field_level_and_answers_none_when_it_cannot() -> None:
    """`contains_value`'s three answers, including the one that is not a boolean."""
    payload = {"turns": [{"text": "the command was FOO=barbaz here"}], "n": 3}
    path = Path(tempfile.mkdtemp()) / "log.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert contains_value(path, "barbaz") is True
    assert contains_value(path, "not-in-here") is False
    # Not a boolean: there is nothing to consult, which supports neither cause.
    assert contains_value("", "barbaz") is None
    assert contains_value(path.with_name("absent.json"), "barbaz") is None

    path.write_text("{not json", encoding="utf-8")
    assert contains_value(path, "barbaz") is None
