"""The digest manifest (`index.jsonl`), `scribe index`, and opt-in frontmatter.

The manifest is a contract with consumers scribe will never see, so these tests pin the two
rules a consumer has to implement -- record identity is `(path, turn_uuid)` with the last line
winning, and the indexable document is the file -- against the REAL write path
(`append_block`), including the in-place replacement of a provisional block. A test that only
appended fresh blocks would pass on a manifest that gets the replacement case wrong.

`--check` is proven red once per drift shape. A check shown to fire on a missing line says
nothing about whether it fires on a stale one.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scribe.config import Config, ConfigError, load
from scribe.index_cli import EXIT_DRIFT, EXIT_OK
from scribe.index_cli import main as index_main
from scribe.journal import preview
from scribe.manifest import DERIVED_FIELDS, append_turn, check, read, rebuild, write
from scribe.writeback import (
    PROVISIONAL_PLACEHOLDER,
    append_block,
    apply_frontmatter,
    daily_path,
    strip_frontmatter,
)

WHEN = datetime(2026, 9, 23, 19, 40, tzinfo=UTC)
LATER = datetime(2026, 9, 23, 21, 5, tzinfo=UTC)
AWK = shutil.which("awk")
REFERENCE_AWK = Path(__file__).parent / "reference" / "recent_memory_preview.awk"
needs_awk = pytest.mark.skipif(AWK is None, reason="awk not installed")


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        output_dir=str(tmp_path / "scribe" / "digests"),
        eventlog_dir=str(tmp_path / "scribe" / "eventlogs"),
    )


def _body(text: str) -> str:
    return f"### {WHEN:%H:%M}\n- {text}\n"


def _write(cfg: Config, turn: str, *, session: str = "sess-a", when=WHEN, provisional="") -> Path:
    md = daily_path(cfg.output_dir, "research", when)
    append_block(
        md,
        body=_body(f"work for {turn} ({provisional or 'final'})"),
        session_id=session,
        turn_uuid=turn,
        transcript_path=f"/t/{session}.jsonl",
        when=when,
        provisional=provisional,
    )
    return md


def _lines(cfg: Config) -> list[dict]:
    return [json.loads(ln) for ln in cfg.manifest_file().read_text().splitlines()]


def _cli(cfg: Config, tmp_path: Path, *args: str) -> int:
    toml = tmp_path / "scribe.toml"
    toml.write_text(
        f'[discovery]\noutput_dir = "{cfg.output_dir}"\neventlog_dir = "{cfg.eventlog_dir}"\n'
    )
    return index_main(["--config", str(toml), *args])


# --------------------------------------------------------------------------------------
# Placement and permissions
# --------------------------------------------------------------------------------------


def test_the_default_manifest_is_a_sibling_of_the_digest_root(cfg) -> None:
    root = Path(cfg.output_dir)
    assert cfg.manifest_file() == root.parent / "index.jsonl"
    assert root not in cfg.manifest_file().parents


def test_the_manifest_is_owner_only(cfg) -> None:
    md = _write(cfg, "t1")
    append_turn(cfg, md, "t1")
    assert stat.S_IMODE(cfg.manifest_file().stat().st_mode) == 0o600


def test_a_rebuilt_manifest_is_owner_only_too(cfg) -> None:
    _write(cfg, "t1")
    write(cfg, rebuild(cfg))
    assert stat.S_IMODE(cfg.manifest_file().stat().st_mode) == 0o600


def test_an_existing_parent_directory_is_not_tightened(tmp_path) -> None:
    """`manifest_path` can point anywhere, `~` included. Its file is 0600; its directory is
    not ours to chmod -- `secure_dir` on an existing path would have done exactly that."""
    home = tmp_path / "operator-home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    cfg = Config(
        output_dir=str(tmp_path / "d"),
        eventlog_dir=str(tmp_path / "e"),
        manifest_path=str(home / "index.jsonl"),
    )
    md = _write(cfg, "t1")
    append_turn(cfg, md, "t1")
    assert stat.S_IMODE(home.stat().st_mode) == 0o755


def test_a_missing_parent_is_created_owner_only(tmp_path) -> None:
    cfg = Config(
        output_dir=str(tmp_path / "d"),
        eventlog_dir=str(tmp_path / "e"),
        manifest_path=str(tmp_path / "new" / "index.jsonl"),
    )
    md = _write(cfg, "t1")
    append_turn(cfg, md, "t1")
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700


# --------------------------------------------------------------------------------------
# The record, and the two identity rules
# --------------------------------------------------------------------------------------


def test_a_record_carries_every_field_with_a_relative_path(cfg) -> None:
    md = _write(cfg, "t1")
    evdir = Path(cfg.eventlog_dir)
    evdir.mkdir(parents=True)
    (evdir / "sess-a.json").write_text("{}")
    rec = append_turn(cfg, md, "t1", now=WHEN)
    (line,) = _lines(cfg)
    assert list(line) == [*DERIVED_FIELDS, "written_at"]
    assert line["path"] == "research/2026-09-23.md"
    assert line["agent"] == "research"
    assert line["date"] == "2026-09-23"
    assert line["session_id"] == "sess-a"
    assert line["turn_uuid"] == "t1"
    assert line["provisional"] == ""
    assert line["eventlog_path"] == "sess-a.json"
    assert line["written_at"] == "2026-09-23T19:40:00+00:00"
    assert rec["sha256"] == line["sha256"] and len(line["sha256"]) == 64


def test_an_absent_event_log_is_an_empty_string_not_a_guess(cfg) -> None:
    md = _write(cfg, "t1")
    assert append_turn(cfg, md, "t1")["eventlog_path"] == ""


def test_the_hash_is_of_the_block_so_a_later_append_does_not_stale_it(cfg) -> None:
    """A file-level hash would change on every append and fail `--check` on every earlier
    line of every multi-session day. The block hash is stable until the block changes."""
    md = _write(cfg, "t1")
    first = append_turn(cfg, md, "t1")["sha256"]
    _write(cfg, "t2", session="sess-b", when=LATER)
    append_turn(cfg, md, "t2")
    assert check(cfg).clean
    assert {r["turn_uuid"]: r["sha256"] for r in rebuild(cfg)}["t1"] == first


def test_a_provisional_replaced_in_place_gives_two_lines_for_one_key(cfg) -> None:
    """Rule one: record identity is `(path, turn_uuid)` and the last line wins."""
    md = _write(cfg, "t1", provisional=PROVISIONAL_PLACEHOLDER)
    append_turn(cfg, md, "t1")
    _write(cfg, "t1")  # the real digest arrives -- replace_block, same turn uuid
    append_turn(cfg, md, "t1")

    first, second = _lines(cfg)
    assert (first["path"], first["turn_uuid"]) == (second["path"], second["turn_uuid"])
    assert first["provisional"] == "placeholder"
    assert second["provisional"] == ""
    assert first["sha256"] != second["sha256"]
    # `--check` reads the LAST line as current, so the superseded one is not drift.
    assert check(cfg).clean


def test_two_sessions_on_one_day_give_one_path_and_two_keys(cfg) -> None:
    """Rule two: the same `path` recurs with different turn uuids -- the document is the file."""
    md = _write(cfg, "t1", session="sess-a")
    append_turn(cfg, md, "t1")
    _write(cfg, "t2", session="sess-b", when=LATER)
    append_turn(cfg, md, "t2")

    a, b = _lines(cfg)
    assert a["path"] == b["path"]
    assert a["turn_uuid"] != b["turn_uuid"]
    assert check(cfg).clean


def test_a_torn_block_is_not_recorded(cfg) -> None:
    md = _write(cfg, "t1")
    with md.open("a") as fh:
        fh.write("\n<!-- session:s turn:torn transcript:/t/x.jsonl -->\nhalf a body\n")
    assert [r["turn_uuid"] for r in rebuild(cfg)] == ["t1"]
    with pytest.raises(LookupError):
        append_turn(cfg, md, "torn")


# --------------------------------------------------------------------------------------
# --rebuild and --check
# --------------------------------------------------------------------------------------


@pytest.fixture
def built(cfg, tmp_path):
    """A corpus of three blocks across two files, and a clean rebuilt manifest over it."""
    _write(cfg, "t1", session="sess-a")
    _write(cfg, "t2", session="sess-b", when=LATER)
    _write(cfg, "t3", session="sess-c", when=datetime(2026, 9, 24, 8, 0, tzinfo=UTC))
    assert _cli(cfg, tmp_path, "--rebuild") == EXIT_OK
    return cfg


def _rewrite(cfg: Config, lines: list[dict]) -> None:
    cfg.manifest_file().write_text("".join(json.dumps(ln) + "\n" for ln in lines))


def test_a_rebuild_round_trips_clean(built, tmp_path, capsys) -> None:
    assert len(_lines(built)) == 3
    assert all(ln["written_at"] is None for ln in _lines(built))
    assert _cli(built, tmp_path, "--check") == EXIT_OK
    out = capsys.readouterr().out
    assert "clean" in out
    assert "written_at is informational" in out


def test_check_fires_on_a_missing_line(built, tmp_path, capsys) -> None:
    lines = _lines(built)
    _rewrite(built, lines[1:])
    assert _cli(built, tmp_path, "--check") == EXIT_DRIFT
    assert f"missing  {lines[0]['path']} turn:{lines[0]['turn_uuid']}" in capsys.readouterr().out


def test_check_fires_on_a_stale_hash(built, tmp_path, capsys) -> None:
    lines = _lines(built)
    lines[1]["sha256"] = "0" * 64
    _rewrite(built, lines)
    assert _cli(built, tmp_path, "--check") == EXIT_DRIFT
    assert f"changed  {lines[1]['path']} turn:{lines[1]['turn_uuid']}  (sha256)" in (
        capsys.readouterr().out
    )


def test_check_fires_on_a_line_for_a_deleted_digest(built, tmp_path, capsys) -> None:
    lines = _lines(built)
    ghost = dict(lines[0], path="research/2026-01-01.md", turn_uuid="gone")
    _rewrite(built, [*lines, ghost])
    assert _cli(built, tmp_path, "--check") == EXIT_DRIFT
    assert "stale    research/2026-01-01.md turn:gone" in capsys.readouterr().out


def test_check_fires_on_a_malformed_line(built, tmp_path, capsys) -> None:
    with built.manifest_file().open("a") as fh:
        fh.write("{ not json\n")
    assert _cli(built, tmp_path, "--check") == EXIT_DRIFT
    assert "malformed line 4" in capsys.readouterr().out


def test_check_fires_when_there_is_no_manifest_at_all(cfg, tmp_path, capsys) -> None:
    _write(cfg, "t1")
    assert _cli(cfg, tmp_path, "--check") == EXIT_DRIFT
    assert "no manifest file" in capsys.readouterr().out


def test_a_differing_written_at_is_not_drift(built, tmp_path) -> None:
    """The half of the pair that stops `--check` failing on every line forever."""
    lines = _lines(built)
    for ln in lines:
        ln["written_at"] = "1999-01-01T00:00:00+00:00"
    _rewrite(built, lines)
    assert _cli(built, tmp_path, "--check") == EXIT_OK


def test_an_empty_corpus_with_an_empty_manifest_is_clean(cfg, tmp_path) -> None:
    assert _cli(cfg, tmp_path, "--rebuild") == EXIT_OK
    assert cfg.manifest_file().read_text() == ""
    assert _cli(cfg, tmp_path, "--check") == EXIT_OK


def test_check_json_reports_the_discrepancy_structurally(built, tmp_path, capsys) -> None:
    _rewrite(built, _lines(built)[1:])
    assert _cli(built, tmp_path, "--check", "--json") == EXIT_DRIFT
    report = json.loads(capsys.readouterr().out)
    assert report["clean"] is False
    assert len(report["missing"]) == 1


def test_rebuild_and_check_are_mutually_exclusive_and_one_is_required(cfg, tmp_path) -> None:
    with pytest.raises(SystemExit):
        _cli(cfg, tmp_path)
    with pytest.raises(SystemExit):
        _cli(cfg, tmp_path, "--rebuild", "--check")


def test_later_lines_win_when_reading(cfg) -> None:
    md = _write(cfg, "t1")
    append_turn(cfg, md, "t1")
    append_turn(cfg, md, "t1")
    cur = read(cfg.manifest_file())
    assert cur.lines == 2 and len(cur.records) == 1


def test_no_manifest_path_resolves_inside_the_event_log_tree(built) -> None:
    evroot = Path(built.eventlog_dir).resolve()
    root = Path(built.output_dir)
    for ln in _lines(built):
        assert evroot not in (root / ln["path"]).resolve().parents
    assert evroot not in built.manifest_file().resolve().parents


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


def _load(tmp_path, body: str) -> Config:
    p = tmp_path / "c.toml"
    p.write_text(
        f'[discovery]\noutput_dir = "{tmp_path}/s/digests"\n'
        f'eventlog_dir = "{tmp_path}/s/eventlogs"\n' + body
    )
    return load(p)


def test_no_index_block_means_no_hook_and_the_default_manifest(tmp_path) -> None:
    cfg = _load(tmp_path, "")
    assert cfg.on_digest_written == ()
    assert cfg.on_digest_written_timeout_seconds == 30.0
    assert cfg.emit_frontmatter is False
    assert cfg.manifest_file() == tmp_path / "s" / "index.jsonl"


def test_a_hook_argv_list_loads(tmp_path) -> None:
    cfg = _load(tmp_path, '[index]\non_digest_written = ["/bin/idx", "--file"]\n')
    assert cfg.on_digest_written == ("/bin/idx", "--file")


def test_a_shell_string_hook_is_refused_at_load(tmp_path) -> None:
    with pytest.raises(ConfigError, match="argv list"):
        _load(tmp_path, '[index]\non_digest_written = "curl $PATH"\n')


@pytest.mark.parametrize("value", ["[]", '["/bin/x", ""]', '["/bin/x", 3]', "{ a = 1 }"])
def test_a_malformed_hook_is_refused(tmp_path, value) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, f"[index]\non_digest_written = {value}\n")


@pytest.mark.parametrize("value", ["0", "-1", "true", '"30"'])
def test_a_nonsense_hook_timeout_is_refused(tmp_path, value) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, f"[index]\non_digest_written_timeout_seconds = {value}\n")


@pytest.mark.parametrize("where", ["s/digests/index.jsonl", "s/eventlogs/x/index.jsonl"])
def test_a_manifest_inside_either_tree_is_refused(tmp_path, where) -> None:
    with pytest.raises(ConfigError, match="inside"):
        _load(tmp_path, f'[index]\nmanifest_path = "{tmp_path}/{where}"\n')


def test_a_default_manifest_swallowed_by_the_event_log_tree_is_refused(tmp_path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(f'[discovery]\noutput_dir = "{tmp_path}/s/d"\neventlog_dir = "{tmp_path}/s"\n')
    with pytest.raises(ConfigError, match="eventlog_dir"):
        load(p)


def test_emit_frontmatter_must_be_a_boolean(tmp_path) -> None:
    assert _load(tmp_path, "emit_frontmatter = true\n").emit_frontmatter is True
    with pytest.raises(ConfigError):
        _load(tmp_path, 'emit_frontmatter = "yes"\n')


# --------------------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------------------


def _reference(path: Path) -> str:
    return subprocess.run(  # noqa: S603
        [str(AWK), "-f", str(REFERENCE_AWK), str(path)], capture_output=True, text=True, check=True
    ).stdout


def _pair(tmp_path) -> tuple[Path, Path]:
    """The same two-session day, written once without frontmatter and once with it."""
    out = []
    for name, fm in (("off", False), ("on", True)):
        c = Config(output_dir=str(tmp_path / name / "d"), eventlog_dir=str(tmp_path / name / "e"))
        md = _write(c, "t1", session="sess-a")
        if fm:
            apply_frontmatter(md)
        _write(c, "t2", session="sess-b", when=LATER)
        if fm:
            apply_frontmatter(md)
        out.append(md)
    return out[0], out[1]


@needs_awk
def test_frontmatter_does_not_move_the_reference_preview(tmp_path) -> None:
    """Invariant 10, asserted on the parser rather than on the file existing."""
    off, on = _pair(tmp_path)
    assert on.read_text().startswith("---\n")
    expected = _reference(off)
    assert expected, "the reference produced nothing -- the comparison would be vacuous"
    assert _reference(on) == expected


def test_frontmatter_does_not_move_the_python_preview(tmp_path) -> None:
    off, on = _pair(tmp_path)
    assert preview(on.read_text()) == preview(off.read_text())


def test_frontmatter_leaves_every_block_byte_identical(tmp_path) -> None:
    off, on = _pair(tmp_path)
    assert strip_frontmatter(on.read_text()) == off.read_text()


def test_frontmatter_names_the_day_and_its_sessions(tmp_path) -> None:
    _off, on = _pair(tmp_path)
    head = on.read_text().split("---\n")[1]
    assert head.splitlines() == [
        'agent: "research"',
        "date: 2026-09-23",
        "source: scribe",
        'session_ids: ["sess-a", "sess-b"]',
    ]


def test_frontmatter_is_idempotent(tmp_path) -> None:
    _off, on = _pair(tmp_path)
    before = on.read_text()
    assert apply_frontmatter(on) is False
    assert on.read_text() == before


def test_an_agent_named_like_a_yaml_literal_stays_a_string(tmp_path) -> None:
    md = daily_path(tmp_path, "true", WHEN)
    append_block(
        md, body=_body("x"), session_id="s", turn_uuid="t", transcript_path="/t", when=WHEN
    )
    apply_frontmatter(md)
    assert 'agent: "true"' in md.read_text()


def test_somebody_elses_frontmatter_is_left_alone(tmp_path) -> None:
    md = tmp_path / "research" / "2026-09-23.md"
    md.parent.mkdir()
    original = "---\ntitle: mine\n---\n# 2026-09-23\n"
    md.write_text(original)
    assert apply_frontmatter(md) is False
    assert md.read_text() == original


def test_frontmatter_does_not_disturb_the_manifest(tmp_path) -> None:
    off, on = _pair(tmp_path)
    hashes = []
    for md in (off, on):
        c = Config(
            output_dir=str(md.parent.parent), eventlog_dir=str(md.parent.parent.parent / "e")
        )
        hashes.append([r["sha256"] for r in rebuild(c)])
    assert hashes[0] == hashes[1]


def test_hook_env_passthrough_takes_exact_names_only(tmp_path) -> None:
    cfg = _load(tmp_path, '[index]\non_digest_written_env = ["MY_TOKEN"]\n')
    assert cfg.on_digest_written_env == ("MY_TOKEN",)
    for bad in ('["MY_*"]', '["A B"]', '"MY_TOKEN"', "[1]"):
        with pytest.raises(ConfigError, match="exact"):
            _load(tmp_path, f"[index]\non_digest_written_env = {bad}\n")
