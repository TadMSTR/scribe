"""Tests for the SessionStart journal feed.

**These assert the consumer, not the writer.** The failure this component exists to prevent
is silent: the journal keeps being written, the hook keeps firing, and the injected block is
empty or stale because the content stopped parsing. A test that checked scribe wrote a file
to the expected path would pass on every one of those days. So the load-bearing assertion
here is byte-for-byte agreement with `tests/reference/recent_memory_preview.awk` — the incumbent
consumer's parser, extracted verbatim — over digests produced by scribe's own writer.

`render_digest` + `append_block` are used to build the corpus rather than markdown written
out by hand in a fixture file. A hand-written fixture asserts agreement with what the author
believed the format to be; going through the real writer asserts agreement with the format,
and it breaks if either end moves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scribe.__main__ import main as scribe_main
from scribe.config import DEFAULT_OUTPUT_DIR
from scribe.extract.parser import _agent_from_cwd as agent_from_cwd
from scribe.extract.parser import agent_for_path
from scribe.journal import (
    agent_dir,
    agent_for_project,
    build_context,
    hook_payload,
    preview,
    recent_journals,
    status_line,
    valid_agent,
)
from scribe.journal_cli import main as journal_main
from scribe.summarize.render import render_digest
from scribe.summarize.schema import Digest
from scribe.writeback import append_block, daily_path

#: Resolved absolutely so the tests cannot pick up a different `awk` or `bash` from a
#: caller-supplied PATH — the reference parser is evidence, and evidence read through an
#: ambiguous interpreter is not evidence.
AWK = shutil.which("awk")
BASH = shutil.which("bash")

REFERENCE_AWK = Path(__file__).parent / "reference" / "recent_memory_preview.awk"
HOOK = Path(__file__).parent.parent / "hooks" / "session-start.sh"
SRC = Path(__file__).parent.parent / "src"

DIGEST = Digest(
    asked="Check the release workflow",
    done=["Listed the runs", "Read the failing job's logs"],
    found=["All three jobs passed on the retry"],
    decisions=["Deferred the arm64 matrix entry"],
    open_items=["Set the default branch protection"],
)


def reference_preview(path: Path, max_lines: int = 40) -> str:
    """Run the incumbent's awk exactly as `_recent_memory_preview` did, `tail` included.

    `.rstrip("\\n")` reproduces command substitution, which is how the hook consumed it:
    `content=$(_recent_memory_preview "$f" 40)` drops every trailing newline.
    """
    out = subprocess.run(  # noqa: S603
        [str(AWK), "-f", str(REFERENCE_AWK), str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return "\n".join(out.split("\n")[:-1][-max_lines:]) if out else ""


def write_digest(root: Path, agent: str, when: datetime, turns: int = 1) -> Path:
    """Build a digest through scribe's real render + append path."""
    path = daily_path(root, agent, when)
    for i in range(turns):
        append_block(
            path,
            body=render_digest(DIGEST, heading=f"{when:%H:%M}"),
            session_id=f"session-{when:%Y%m%d}-{i}",
            turn_uuid=f"turn-{when:%Y%m%d}-{i}",
            transcript_path=f"/transcripts/{when:%Y-%m-%d}-{i}.jsonl",
            when=when,
        )
    return path


needs_awk = pytest.mark.skipif(AWK is None, reason="awk not installed")


# --------------------------------------------------------------------------------------
# Parity with the incumbent parser
# --------------------------------------------------------------------------------------


@needs_awk
def test_preview_matches_the_reference_awk_on_a_real_digest(tmp_path) -> None:
    path = write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2), turns=3)
    expected = reference_preview(path)
    assert expected, "the reference parser produced nothing — the comparison would be vacuous"
    assert preview(path.read_text(encoding="utf-8")) == expected


#: Shapes chosen because they are where a port drifts: the h1 no rule matches, a heading
#: level one past the range, a section with no bullets, and bullets that arrive before any
#: section has opened.
EDGE_CASES = {
    "h1_is_dropped": "# 2026-09-15\n\n## Session 09:00\n- a bullet\n",
    "five_hashes_dropped": "## Session 09:00\n##### too deep\n- a bullet\n",
    "four_hashes_kept": "## Session 09:00\n#### deep\n- a bullet\n",
    "bulletless_section_dropped": "## Session 09:00\n### 09:00\n**Done:**\n",
    "bullets_before_any_section": "- orphan bullet\n## Session 09:00\n- a bullet\n",
    "tab_after_hashes": "##\tSession 09:00\n- a bullet\n",
    "no_trailing_newline": "## Session 09:00\n- a bullet",
    "empty": "",
    "headings_only": "## a\n### b\n#### c\n",
    "hash_without_space": "##nospace\n- a bullet\n",
    "dash_without_space": "## Session 09:00\n-nospace\n- a bullet\n",
}


@needs_awk
@pytest.mark.parametrize("name", sorted(EDGE_CASES))
def test_preview_matches_the_reference_awk_on_edge_cases(tmp_path, name) -> None:
    path = tmp_path / "2026-09-15.md"
    path.write_text(EDGE_CASES[name], encoding="utf-8")
    assert preview(path.read_text(encoding="utf-8")) == reference_preview(path)


@needs_awk
def test_preview_matches_the_reference_awk_at_the_line_cap(tmp_path) -> None:
    """The cap is applied to the whole output, not per section — `tail -n 40` was outside
    the awk. A port that truncated each section instead would agree on short files."""
    path = write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2), turns=12)
    text = path.read_text(encoding="utf-8")
    assert len(preview(text, 500).split("\n")) > 40, "corpus too small to exercise the cap"
    for cap in (1, 7, 40, 500):
        assert preview(text, cap) == reference_preview(path, cap)


@needs_awk
def test_preview_matches_the_reference_awk_on_this_hosts_real_digests(real_home) -> None:
    """Runs on forge against digests scribe actually produced; skips elsewhere.

    The hermetic tests above pin the port to the reference parser over a corpus this file
    controls. This one is the check that neither of them can be: agreement over content
    nobody wrote for a test. It is what the `real_home` fixture -- captured before `~` is
    redirected -- exists for.
    """
    real = sorted((real_home / Path(DEFAULT_OUTPUT_DIR).relative_to("~")).glob("*/*.md"))
    if not real:
        pytest.skip("no digests on this host")
    for path in real:
        assert preview(path.read_text(encoding="utf-8", errors="replace")) == reference_preview(
            path
        ), path


# --------------------------------------------------------------------------------------
# preview() behaviour, stated directly rather than only by comparison
# --------------------------------------------------------------------------------------


def test_a_section_without_bullets_is_dropped() -> None:
    assert preview("## Session 09:00\n### 09:00\n**Done:**\n") == ""


def test_a_section_with_bullets_keeps_its_heading_and_subheads() -> None:
    out = preview("## Session 09:00\n### 09:00\n**Done:**\n- did a thing\n")
    assert out == "## Session 09:00\n### 09:00\n- did a thing"


def test_the_cap_keeps_the_most_recent_lines() -> None:
    text = "## S\n" + "".join(f"- bullet {i}\n" for i in range(100))
    assert preview(text, 3).split("\n") == ["- bullet 97", "- bullet 98", "- bullet 99"]


def test_a_non_positive_cap_is_refused() -> None:
    """`lines[-0:]` is the whole list, so a clamp here would silently uncap the injection."""
    with pytest.raises(ValueError):
        preview("## S\n- a\n", 0)


# --------------------------------------------------------------------------------------
# File selection
# --------------------------------------------------------------------------------------


def test_the_two_most_recent_journals_are_chosen_newest_first(tmp_path) -> None:
    for day in (10, 14, 15, 12):
        write_digest(tmp_path, "developer", datetime(2026, 9, day, 9, 0))
    got = recent_journals(agent_dir(tmp_path, "developer"))
    assert [p.name for p in got] == ["2026-09-15.md", "2026-09-14.md"]


def test_selection_ignores_names_that_are_not_journals(tmp_path) -> None:
    d = agent_dir(tmp_path, "developer")
    d.mkdir(parents=True)
    for name in ("README.md", "2026-09-15.txt", "2026-9-15.md", "2026-09-15.md.bak", "notes.md"):
        (d / name).write_text("## S\n- a\n", encoding="utf-8")
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 9, 0))
    assert [p.name for p in recent_journals(d)] == ["2026-09-15.md"]


def test_selection_does_not_descend_into_subdirectories(tmp_path) -> None:
    d = agent_dir(tmp_path, "developer")
    (d / "archive").mkdir(parents=True)
    (d / "archive" / "2026-09-15.md").write_text("## S\n- a\n", encoding="utf-8")
    assert recent_journals(d) == []


def test_selection_skips_symlinks(tmp_path) -> None:
    """Everything reachable from here is read and injected. `find -type f` without `-L` does
    not follow a symlink, and neither may this: a link dropped in the digest directory would
    otherwise be a read primitive aimed at anything the account can open."""
    secret = tmp_path / "secret.md"
    secret.write_text("## S\n- a token\n", encoding="utf-8")
    d = agent_dir(tmp_path, "developer")
    d.mkdir(parents=True)
    (d / "2026-09-15.md").symlink_to(secret)
    assert recent_journals(d) == []


def test_a_missing_directory_is_not_an_error(tmp_path) -> None:
    """The ordinary state before scribe's first run."""
    assert recent_journals(tmp_path / "nope") == []


def test_a_non_positive_limit_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        recent_journals(tmp_path, 0)


# --------------------------------------------------------------------------------------
# Agent resolution — the per-agent / per-project equivalence
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("project_dir", "expected"),
    [
        ("/home/ted/.claude/projects/developer", "developer"),
        ("/home/ted/.claude/projects/developer/", "developer"),
        ("/home/ted/.claude/projects/-home-ted--claude-projects-sysadmin", "sysadmin"),
        ("/home/ted/repos/personal/scribe", ""),
        ("/home/ted/.claude/projects", ""),
        ("", ""),
    ],
)
def test_agent_is_resolved_from_either_project_directory_shape(project_dir, expected) -> None:
    """A hook sees the working directory `~/.claude/projects/<agent>`; the extractor sees
    Claude Code's flattened transcript directory. Handling only the flattened shape — the one
    the codebase already knew — would have returned "" for every real hook invocation."""
    assert agent_for_project(project_dir) == expected


def test_an_unrecognised_project_directory_yields_no_agent() -> None:
    """Better to inject nothing than to guess: a wrong guess puts one agent's sessions into
    another agent's context, and nothing downstream would flag it."""
    assert agent_for_project("/tmp/somewhere") == ""


# --------------------------------------------------------------------------------------
# Payload assembly
# --------------------------------------------------------------------------------------


def test_context_carries_each_file_under_its_own_heading(tmp_path) -> None:
    for day in (14, 15):
        write_digest(tmp_path, "developer", datetime(2026, 9, day, 9, 0))
    paths = recent_journals(agent_dir(tmp_path, "developer"))
    ctx = build_context(paths)
    assert ctx.startswith("# Recent Memory\n\n")
    assert "## 2026-09-15.md\n" in ctx
    assert "## 2026-09-14.md\n" in ctx
    assert "- Listed the runs" in ctx


def test_context_separators_are_real_newlines(tmp_path) -> None:
    r"""The incumbent built this in bash double quotes — `context="# Recent Memory\n\n"` —
    where `\n` is two literal characters, and `jq -Rs` then encoded the backslash faithfully.
    Live injected context on forge shows a literal `\n` between the heading and the first
    file, and again between files. Not reproduced here, so it is asserted here."""
    for day in (14, 15):
        write_digest(tmp_path, "developer", datetime(2026, 9, day, 9, 0))
    ctx = build_context(recent_journals(agent_dir(tmp_path, "developer")))
    assert "\\n" not in ctx
    assert ctx.startswith("# Recent Memory\n\n## 2026-09-15.md\n")
    assert "\n\n## 2026-09-14.md\n" in ctx


def test_no_files_means_no_context() -> None:
    assert build_context([]) == ""


def test_a_file_that_previews_to_nothing_contributes_no_heading(tmp_path) -> None:
    (tmp_path / "2026-09-15.md").write_text("## Session 09:00\n**Done:**\n", encoding="utf-8")
    (tmp_path / "2026-09-14.md").write_text("## Session 09:00\n- real content\n", encoding="utf-8")
    ctx = build_context(recent_journals(tmp_path))
    assert "## 2026-09-15.md" not in ctx
    assert "## 2026-09-14.md" in ctx


def test_an_unreadable_file_does_not_cost_the_other_its_injection(tmp_path) -> None:
    ctx = build_context([tmp_path / "gone.md", _written(tmp_path, "2026-09-14.md")])
    assert "- real content" in ctx


def _written(d: Path, name: str) -> Path:
    p = d / name
    p.write_text("## Session 09:00\n- real content\n", encoding="utf-8")
    return p


def test_empty_context_omits_hook_specific_output() -> None:
    """An injection of a bare `# Recent Memory` with nothing under it spends context to say
    nothing, and reads to an agent as though the memory surface is empty rather than quiet."""
    assert "hookSpecificOutput" not in hook_payload("", "status")
    assert hook_payload("", "status")["systemMessage"] == "status"


def test_payload_names_the_hook_event_cloudcli_surfaces() -> None:
    """`hookSpecificOutput.additionalContext` is the ONLY field measured to reach a CloudCLI
    agent session. A bare `systemMessage` is dropped for agent sessions."""
    p = hook_payload("# Recent Memory\n\n## f\n- a\n", "status")
    assert p["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "- a" in p["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    ("agent", "paths", "expect"),
    [
        ("", [], "no agent resolved"),
        ("developer", [], "no digests under"),
        ("developer", [Path("/d/2026-09-15.md")], "1 file(s) | latest 2026-09-15.md"),
    ],
)
def test_status_reports_why_nothing_was_injected(agent, paths, expect) -> None:
    """Absence must be distinguishable from a quiet day. `scribe not running` and `scribe
    writing elsewhere` are the two ways this goes wrong and they look identical without it."""
    assert expect in status_line(agent, Path("/d"), paths)


# --------------------------------------------------------------------------------------
# The real hook, end to end
# --------------------------------------------------------------------------------------


def run_hook(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Drive `hooks/session-start.sh` as Claude Code would.

    `PYTHONPATH` is supplied by this harness rather than by the hook, which is the same
    division as in production: a deployment points `SCRIBE_PYTHON` at an interpreter that can
    import scribe, and the hook does not go looking for one.
    """
    e = {**os.environ, "SCRIBE_PYTHON": sys.executable}
    e["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), e.get("PYTHONPATH", "")]))
    e.pop("CLAUDE_PROJECT_DIR", None)
    e.update(env or {})
    assert BASH, "bash is required to drive the hook"
    return subprocess.run(  # noqa: S603
        [BASH, str(HOOK), *args], capture_output=True, text=True, env=e, timeout=60
    )


def test_the_real_hook_emits_injectable_context(tmp_path) -> None:
    """Phase 3 of the build plan, automated: a unit test over the parser is necessary and not
    sufficient, because the hook also has to resolve an agent, find the files and emit JSON.
    Each of those is a way to emit a well-formed payload with nothing in it."""
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2))
    proc = run_hook("--digests", str(tmp_path), "--agent", "developer")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    ctx = doc["hookSpecificOutput"]["additionalContext"]
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "## 2026-09-15.md" in ctx
    assert "- Listed the runs" in ctx
    assert "2026-09-15.md" in doc["systemMessage"]


def test_the_real_hook_resolves_the_agent_from_the_environment(tmp_path) -> None:
    """The mapping that makes scribe's per-agent layout serve a per-project journal."""
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2))
    proc = run_hook(
        "--digests",
        str(tmp_path),
        env={"CLAUDE_PROJECT_DIR": "/home/ted/.claude/projects/developer"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "- Listed the runs" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_real_hook_says_so_when_there_are_no_digests(tmp_path) -> None:
    proc = run_hook("--digests", str(tmp_path), "--agent", "developer")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert "hookSpecificOutput" not in doc
    assert "no digests under" in doc["systemMessage"]


def test_the_real_hook_reports_an_unusable_interpreter_instead_of_breaking_a_session(
    tmp_path,
) -> None:
    """A hook that emitted a traceback on stdout would hand Claude Code unparseable JSON at
    every session start on the host."""
    proc = run_hook("--digests", str(tmp_path), env={"SCRIBE_PYTHON": "/nonexistent/python"})
    assert proc.returncode == 0
    assert "journal hook failed" in json.loads(proc.stdout)["systemMessage"]


def test_the_real_hook_emits_nothing_on_stdout_when_it_refuses(tmp_path) -> None:
    """Exit 2 means "could not do the job". Emitting a partial payload alongside it would
    make a refusal indistinguishable from an empty result to anything that only parses."""
    proc = run_hook("--digests", str(tmp_path), "--agent", "developer", "--max-lines", "0")
    doc = json.loads(proc.stdout)
    assert "hookSpecificOutput" not in doc
    assert "journal hook failed" in doc["systemMessage"]
    assert "--max-lines must be positive" in proc.stderr


# --------------------------------------------------------------------------------------
# The CLI, in process
# --------------------------------------------------------------------------------------
#
# The hook tests above drive the same code through `bash` and a subprocess, which is the
# right shape for proving the consumer works and the wrong shape for seeing inside it:
# coverage cannot follow a subprocess, so every branch below reads as unexercised and a
# regression in argument or config handling would land against a green report.


def test_cli_writes_a_payload_and_a_trailing_newline(tmp_path, capsys) -> None:
    """vikunja#439 again: the trailing newline is asserted on every emitting path, not on
    the one that happened to be remembered."""
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2))
    assert journal_main(["--digests", str(tmp_path), "--agent", "developer"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert "- Listed the runs" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_cli_defaults_the_digest_root_to_the_configured_output_dir(tmp_path, capsys) -> None:
    """The default is the one path nobody passes explicitly, so it is the one that silently
    points at nothing after an `output_dir` change."""
    root = Path(os.path.expanduser(DEFAULT_OUTPUT_DIR))
    write_digest(root, "developer", datetime(2026, 9, 15, 14, 2))
    assert journal_main(["--agent", "developer"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "- Listed the runs" in doc["hookSpecificOutput"]["additionalContext"]


def test_cli_resolves_the_agent_from_the_environment(tmp_path, capsys, monkeypatch) -> None:
    write_digest(tmp_path, "sysadmin", datetime(2026, 9, 15, 14, 2))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/home/ted/.claude/projects/sysadmin")
    assert journal_main(["--digests", str(tmp_path)]) == 0
    assert "- Listed the runs" in capsys.readouterr().out


def test_cli_prefers_an_explicit_agent_over_the_environment(tmp_path, capsys, monkeypatch) -> None:
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/home/ted/.claude/projects/sysadmin")
    assert journal_main(["--digests", str(tmp_path), "--agent", "developer"]) == 0
    assert "developer" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_cli_without_an_agent_injects_nothing(tmp_path, capsys, monkeypatch) -> None:
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert journal_main(["--digests", str(tmp_path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "hookSpecificOutput" not in doc
    assert "no agent resolved" in doc["systemMessage"]


@pytest.mark.parametrize("bad", [["--max-lines", "0"], ["--files", "0"], ["--files", "-1"]])
def test_cli_refuses_a_non_positive_count_and_emits_nothing(tmp_path, capsys, bad) -> None:
    assert journal_main(["--digests", str(tmp_path), "--agent", "developer", *bad]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be positive" in captured.err


def test_cli_reports_a_malformed_config_rather_than_injecting(tmp_path, capsys) -> None:
    bad = tmp_path / "scribe.toml"
    bad.write_text("[discovery\n", encoding="utf-8")
    assert journal_main(["--config", str(bad), "--agent", "developer"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_honours_output_dir_from_config(tmp_path, capsys) -> None:
    root = tmp_path / "elsewhere"
    write_digest(root, "developer", datetime(2026, 9, 15, 14, 2))
    cfg = tmp_path / "scribe.toml"
    cfg.write_text(f'[discovery]\noutput_dir = "{root}"\n', encoding="utf-8")
    assert journal_main(["--config", str(cfg), "--agent", "developer"]) == 0
    assert "- Listed the runs" in capsys.readouterr().out


def test_cli_caps_the_injected_lines(tmp_path, capsys) -> None:
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2), turns=12)
    assert (
        journal_main(["--digests", str(tmp_path), "--agent", "developer", "--max-lines", "5"]) == 0
    )
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx.splitlines()) == 8  # heading, blank, file heading, 5 content lines


def test_dispatch_routes_the_journal_subcommand(tmp_path, capsys) -> None:
    """`python -m scribe journal` is the documented invocation and the one the hook runs."""
    write_digest(tmp_path, "developer", datetime(2026, 9, 15, 14, 2))
    assert scribe_main(["journal", "--digests", str(tmp_path), "--agent", "developer"]) == 0
    assert "- Listed the runs" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# The agent name is an external identifier that reaches a path
# --------------------------------------------------------------------------------------
#
# IV-01 in the fleet's pattern knowledge base, recurrence 14. It arrives from
# `$CLAUDE_PROJECT_DIR` or `--agent` and is joined onto the digest root, and everything under
# the directory that produces is read and injected into a session.


@pytest.mark.parametrize(
    "name",
    ["developer", "sysadmin", "agent-01", "a.b_c", "X"],
)
def test_ordinary_agent_names_are_accepted(name) -> None:
    assert valid_agent(name)


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../secrets", "a/b", "a\\b", "a b", "a\0b", "a;b", "*", "~", "a\nb"],
)
def test_agent_names_that_are_not_one_path_component_are_refused(name) -> None:
    """A leading dash is deliberately NOT in this list: `-r` is a legal directory name and
    nothing here hands the value to a shell, so refusing it would be guarding the wrong
    thing. What is refused is anything that is not exactly one path component."""
    assert not valid_agent(name)


@pytest.mark.parametrize("name", ["..", "../..", "../../.claude", "a/../.."])
def test_a_traversing_agent_name_is_refused_loudly(tmp_path, name) -> None:
    """Loudly, not by falling back to a default. Substituting `unknown` would turn a refusal
    into an empty injection, which is indistinguishable from a quiet day."""
    with pytest.raises(ValueError):
        agent_dir(tmp_path, name)


def test_agent_dir_containment_is_asserted_on_the_resolved_path(tmp_path) -> None:
    """The character allowlist states an intention about names; this states the property
    about paths, and it is the one that has to hold."""
    assert agent_dir(tmp_path, "developer") == tmp_path / "developer"
    with pytest.raises(ValueError):
        agent_dir(tmp_path, "..")


def test_a_traversing_project_directory_yields_no_agent() -> None:
    """`~/.claude/projects/..` satisfies every structural test — it really is a directory
    under `projects` whose parent's parent is `.claude` — and `Path(...).name` really is
    `".."`. The environment form reaches the path join as readily as the flag does."""
    assert agent_for_project("/home/ted/.claude/projects/..") == ""


def test_the_cli_refuses_a_traversing_agent_and_emits_nothing(tmp_path, capsys) -> None:
    assert journal_main(["--digests", str(tmp_path), "--agent", "../../.claude"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a usable agent name" in captured.err


def test_the_real_hook_refuses_a_traversing_project_directory(tmp_path) -> None:
    """End to end: a hook handed a traversing `CLAUDE_PROJECT_DIR` injects nothing and says
    so, rather than injecting whatever `YYYY-MM-DD.md` files sit above the digest root."""
    (tmp_path / "2026-09-15.md").write_text(
        "## S\n- content from above the root\n", encoding="utf-8"
    )
    write_digest(tmp_path / "digests", "developer", datetime(2026, 9, 15, 14, 2))
    proc = run_hook(
        "--digests",
        str(tmp_path / "digests"),
        env={"CLAUDE_PROJECT_DIR": "/home/ted/.claude/projects/.."},
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert "hookSpecificOutput" not in doc
    assert "no agent resolved" in doc["systemMessage"]


def test_a_symlinked_agent_directory_is_refused(tmp_path) -> None:
    """The containment check is not redundant with the character allowlist, and this is the
    case that shows it: `developer` passes every name test there is, and the directory it
    names is a link out of the tree.

    `recent_journals`' `follow_symlinks=False` does not cover this — it guards the journal
    *files*, and `os.scandir` follows a symlinked directory happily. Without the containment
    check the listing below returns the file outside the root and its content is injected.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "2026-09-15.md").write_text("## S\n- content from outside\n", encoding="utf-8")
    root = tmp_path / "digests"
    root.mkdir()
    (root / "developer").symlink_to(outside)

    assert [p.name for p in recent_journals(root / "developer")] == ["2026-09-15.md"], (
        "precondition: without the guard, the listing reaches outside the root"
    )
    with pytest.raises(ValueError, match="escapes the digest root"):
        agent_dir(root, "developer")


def test_the_cli_refuses_a_symlinked_agent_directory(tmp_path, capsys) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "2026-09-15.md").write_text("## S\n- content from outside\n", encoding="utf-8")
    root = tmp_path / "digests"
    root.mkdir()
    (root / "developer").symlink_to(outside)

    assert journal_main(["--digests", str(root), "--agent", "developer"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "escapes the digest root" in captured.err


# --------------------------------------------------------------------------------------
# The writer and the reader must resolve an agent name identically
# --------------------------------------------------------------------------------------
#
# The audit's Low finding: `_agent_from_project_dir` truncated a hyphenated agent name at the
# first hyphen. Its live consequence was not the cross-contamination the finding described --
# no colliding directory exists -- but a DISAGREEMENT. The extractor wrote `doc-health`'s
# digests to `digests/doc/`; a hook resolving `$CLAUDE_PROJECT_DIR` looked in
# `digests/doc-health/` and found nothing. Silent, permanent, and aimed squarely at the
# failure mode this component was built to remove.


HYPHENATED = ["doc-health", "helm-build", "memory-sync"]


@pytest.mark.parametrize("agent", [*HYPHENATED, "developer", "sysadmin"])
def test_reader_and_writer_agree_on_the_agent_name(agent, monkeypatch, tmp_path) -> None:
    """The property, stated directly: one name, both sides. Parametrised over the three
    hyphenated agents on forge because that is the set the old behaviour renamed."""
    projects = Path(os.path.expanduser("~/.claude/projects"))
    (projects / agent).mkdir(parents=True)
    flattened = f"-home-ted--claude-projects-{agent}"
    (projects / flattened).mkdir()

    writer_side = agent_for_path(flattened)
    reader_side = agent_for_project(str(projects / agent))
    assert writer_side == reader_side == agent


@pytest.mark.parametrize("agent", HYPHENATED)
def test_cwd_resolves_a_hyphenated_agent_exactly(agent) -> None:
    """A session's `cwd` keeps its separators, so there is nothing to disambiguate. The
    parser already captured this field; it simply was not used to name the agent."""
    assert agent_from_cwd(f"/home/ted/.claude/projects/{agent}") == agent


@pytest.mark.parametrize("agent", HYPHENATED)
def test_the_flattened_form_resolves_against_the_directory_the_names_live_in(agent) -> None:
    """The encoding is lossy: a separator and a hyphen inside a name both become `-`, so
    `-...-projects-doc-health` is equally `projects/doc-health` and `projects/doc/health`.
    The longest candidate that exists on disk wins."""
    projects = Path(os.path.expanduser("~/.claude/projects"))
    (projects / agent).mkdir(parents=True)
    assert agent_for_path(f"-home-ted--claude-projects-{agent}") == agent


def test_the_flattened_form_falls_back_to_the_first_segment(tmp_path) -> None:
    """With nothing on disk to resolve against there is no better answer than the old one,
    and inventing one would be worse than the documented ambiguity."""
    assert agent_for_path("-home-ted--claude-projects-doc-health") == "doc"


def test_a_flattened_directory_inside_projects_is_not_read_as_an_agent_name() -> None:
    """Flattened transcript directories really do live under `~/.claude/projects`, so they
    satisfy the working-directory shape exactly. Resolving `cwd` first would answer
    `-home-ted--claude-projects-sysadmin` — well-formed, and wrong."""
    path = "/home/ted/.claude/projects/-home-ted--claude-projects-sysadmin"
    assert agent_for_path(path) == "sysadmin"


def test_the_digest_lands_where_the_hook_looks_for_it(tmp_path) -> None:
    """End to end over the seam, for the agent name that exposed it: write through the real
    pipeline path, read through the real hook path, and require the file to be found."""
    projects = Path(os.path.expanduser("~/.claude/projects"))
    (projects / "doc-health").mkdir(parents=True)
    agent = agent_for_path("-home-ted--claude-projects-doc-health")

    digests = tmp_path / "digests"
    write_digest(digests, agent, datetime(2026, 9, 15, 14, 2))

    read_agent = agent_for_project(str(projects / "doc-health"))
    found = recent_journals(agent_dir(digests, read_agent))
    assert [p.name for p in found] == ["2026-09-15.md"]
    assert "- Listed the runs" in build_context(found)
