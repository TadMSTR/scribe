"""Whole-document guarantees about the extractor's output.

The redaction tests in `test_redact.py` check the redactor in isolation. These check the
*document*, which is a different claim: that every string reaching the event log actually
went through the redactor. Those come apart exactly when someone adds a field and forgets
to route it — the sanitising call is present, correct, and simply not on the new path — so
this walks the rendered JSON rather than naming the fields it expects to be clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scribe.extract import extract
from scribe.extract.models import SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"

#: Every credential planted in `_planted_transcript`. Each is synthetic; the marker NOTREAL
#: is embedded so a hit in the output is unambiguous rather than a judgement call.
PLANTED = (
    "sk-ant-api03-NOTREALaaaabbbbccccdddd",
    "ghp_NOTREALaaaabbbbccccdddd11",
    "glpat-NOTREALaaaabbbbccccdddd",
    "AKIANOTREAL123456789",
    "NOTREALmistralkey0987654321",
    "NOTREALpassword12345",
    "NOTREALbearervalue123456",
)


def _planted_transcript(tmp_path: Path) -> Path:
    """A transcript carrying a secret on every distinct path a string can take.

    The point is coverage of *routes*, not of secret formats: user text, assistant text, a
    tool argument, a nested tool argument, a tool_result body, a Bash stdout, a Bash stderr,
    and an error string are eight different journeys into the document, and a redactor
    wired into seven of them passes any test that only checks one.
    """
    recs = [
        {
            "type": "user",
            "uuid": "p1",
            "sessionId": "planted",
            "timestamp": "2026-09-13T12:00:00Z",
            "message": {"role": "user", "content": f"deploy with token {PLANTED[0]} please"},
        },
        {
            "type": "assistant",
            "uuid": "p2",
            "timestamp": "2026-09-13T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"Using GITHUB_TOKEN={PLANTED[1]} for the push."},
                    {
                        "type": "tool_use",
                        "id": "p_t1",
                        "name": "Bash",
                        "input": {
                            "command": f"curl -H 'Authorization: Bearer {PLANTED[6]}' https://x",
                            "description": f"call with glpat {PLANTED[2]}",
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "p_t2",
                        "name": "mcp__scoped-mcp__system-ops_run_command",
                        "input": {"command": "env", "nested": {"deep": {"aws": PLANTED[3]}}},
                    },
                    {
                        "type": "tool_use",
                        "id": "p_t3",
                        "name": "Write",
                        "input": {
                            "file_path": "/tmp/out.env",
                            "content": f"MISTRAL_API_KEY={PLANTED[4]}",
                        },
                    },
                ],
            },
        },
        {
            "type": "user",
            "uuid": "p3",
            "timestamp": "2026-09-13T12:00:02Z",
            "toolUseResult": {
                "stdout": f"MISTRAL_API_KEY={PLANTED[4]}",
                "stderr": f"POSTGRES_PASSWORD={PLANTED[5]}",
                "interrupted": False,
                "isImage": False,
            },
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "p_t1",
                        "is_error": False,
                        "content": f"ok, api_key={PLANTED[4]}",
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "p4",
            "timestamp": "2026-09-13T12:00:03Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "p_t2",
                        "is_error": True,
                        "content": f"failed: password={PLANTED[5]} rejected",
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "p5",
            "timestamp": "2026-09-13T12:00:04Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "p_t3",
                        "is_error": False,
                        "content": [{"type": "text", "text": f"wrote token {PLANTED[1]}"}],
                    }
                ],
            },
        },
    ]
    path = tmp_path / "planted.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8",
    )
    return path


def _walk_strings(node):
    """Yield every string anywhere in the document, keys included.

    Keys are walked too: a dict built as `{secret: value}` hides from any value-only scan,
    and `_args_summary` renders argument dicts by key.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_strings(v)


def test_no_planted_secret_survives_anywhere_in_the_document(tmp_path) -> None:
    log = extract(_planted_transcript(tmp_path))
    doc = log.to_dict()
    strings = list(_walk_strings(doc))
    assert strings, "guard against a vacuous pass on an empty document"
    for secret in PLANTED:
        for s in strings:
            assert secret not in s, f"{secret!r} survived extraction in {s[:120]!r}"
    assert "NOTREAL" not in json.dumps(doc, ensure_ascii=False)


def test_the_planted_document_is_not_empty(tmp_path) -> None:
    """The negative test above is satisfied by an extractor that produced nothing.

    This is the reason the redaction control in the build plan has two halves. Asserting
    real content and a positive redaction count is what makes the absence meaningful.
    """
    log = extract(_planted_transcript(tmp_path))
    assert log.stats.tool_events == 3
    assert log.stats.turns >= 1
    assert log.stats.secrets_redacted >= 6
    assert any(t.user_text for t in log.turns)
    assert any(t.assistant_text for t in log.turns)


def test_secrets_redacted_is_zero_on_a_clean_transcript(tmp_path) -> None:
    """The counter must discriminate, not just be non-zero on everything."""
    recs = [
        {
            "type": "user",
            "uuid": "c1",
            "sessionId": "clean",
            "timestamp": "2026-09-13T12:00:00Z",
            "message": {"role": "user", "content": "list the files in src"},
        },
        {
            "type": "assistant",
            "uuid": "c2",
            "timestamp": "2026-09-13T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "c_t1",
                        "name": "Bash",
                        "input": {"command": "ls -la src"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "c3",
            "timestamp": "2026-09-13T12:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c_t1",
                        "is_error": False,
                        "content": "main.py\nutil.py",
                    }
                ],
            },
        },
    ]
    path = tmp_path / "clean.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    log = extract(path)
    assert log.stats.secrets_redacted == 0
    assert log.stats.tool_events == 1


def test_plan_redaction_control_both_halves() -> None:
    """The exact control the build plan specifies, run as a test rather than by hand.

        python -m scribe.extract tests/fixtures/transcript-with-bearer.jsonl --json \\
          | grep -c 'sk-\\|Bearer [A-Za-z0-9]'   -> 0
        ... | jq '.stats.secrets_redacted'       -> > 0
    """
    log = extract(FIXTURES / "transcript-with-bearer.jsonl")
    blob = json.dumps(log.to_dict(), ensure_ascii=False)
    assert "sk-" not in blob
    import re

    assert not re.search(r"Bearer [A-Za-z0-9]", blob)
    assert log.stats.secrets_redacted > 0


def test_schema_version_is_declared(tmp_path) -> None:
    log = extract(_planted_transcript(tmp_path))
    assert log.to_dict()["schema_version"] == SCHEMA_VERSION


@pytest.mark.parametrize("name", ["transcript-with-bearer", "transcript-structural"])
def test_output_round_trips_through_json(name: str) -> None:
    log = extract(FIXTURES / f"{name}.jsonl")
    assert json.loads(json.dumps(log.to_dict(), ensure_ascii=False)) == log.to_dict()
