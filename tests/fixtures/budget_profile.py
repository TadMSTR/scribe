"""The size profile behind `test_budget`'s default-budget guard, and the transcript built from it.

#966 (id 1049). The guard used to read one real transcript off the host, and Claude Code deletes
a transcript `cleanupPeriodDays` after its last write. Once it went, the test would skip, and
a skipped regression guard reads as green. So the guard now runs on a SYNTHETIC transcript
built from `budget-profile.json`, which records only the reference session's shape: per turn,
the length of the user and assistant text, and per tool event its kind and the length of its
target, argument digest and result digest. **No content.** Every character `synthesize`
writes is filler.

The event log is deliberately NOT the input. The budget ladder runs inside `extract()` on the
transcript, and the persisted event log stores the ladder's RESULT, so a test reading it back
would re-assert a past outcome and pass whatever the ladder now does.

Regenerate the profile, on a host that still has the reference transcript, with:

    python tests/fixtures/budget_profile.py PATH/TO/2dcbe653-....jsonl

Lengths are of the JSON-encoded string, not the raw one. The ladder budgets serialized
characters, and the reference session's digests are escape-heavy: measured by raw length, the
first rung frees 72,649 characters; serialized, 76,025.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROFILE = Path(__file__).with_name("budget-profile.json")

#: Tool names the synthetic transcript uses, one per kind the profile records. Each maps its
#: lengths onto the input keys `extract.parser` reads them from.
_BASH, _MCP, _OTHER = "bash", "mcp", "other"


def _jlen(text: str | None) -> int:
    return len(json.dumps(text or "", ensure_ascii=False)) - 2


def derive(transcript: str | Path) -> dict:
    """The reference session's size profile. Lengths and counts only."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from scribe.extract import extract

    log = extract(transcript, max_session_chars=0)
    commands: dict[str, int] = {}
    turns = []
    for turn in log.turns:
        events = []
        for ev in turn.events:
            kind = ev.kind if ev.kind in (_BASH, _MCP) else _OTHER
            row = [kind, _jlen(ev.target), _jlen(ev.args_digest), _jlen(ev.result_digest)]
            if kind == _BASH:
                # Which earlier command this one repeats, as an index. The command rollup
                # de-duplicates, so the count of DISTINCT commands is part of the size.
                row.append(commands.setdefault(ev.target, len(commands)))
            events.append(row)
        turns.append(
            {
                "user_text": _jlen(turn.user_text),
                "assistant_text": [_jlen(a) for a in turn.assistant_text],
                "events": events,
            }
        )
    return {
        "source": "reference session 2dcbe653 (research), sizes only",
        "undegraded_chars": log.stats.extracted_chars,
        "turns": turns,
    }


def _filler(n: int, seed: int) -> str:
    """`n` characters of dictionary words. Nothing secret-shaped, so the redactor stays at 0
    and cannot change a size behind the test's back."""
    base = f"lorem ipsum dolor sit amet {seed} "
    return (base * (n // len(base) + 1))[:n]


def _arg(key: str, n: int, seed: int) -> dict:
    """An input whose `k=v` argument digest is about `n` characters long."""
    return {key: _filler(n - len(key) - 1, seed)} if n > len(key) + 1 else {}


def synthesize(profile: dict, path: Path) -> Path:
    """Write a transcript with the profile's shape, and return its path."""
    lines = []
    seq = 0
    stamp = "2026-09-13T11:00:00Z"
    for t, turn in enumerate(profile["turns"]):
        lines.append(
            {
                "type": "user",
                "uuid": f"u{t}",
                "sessionId": "budget-synthetic",
                "timestamp": stamp,
                "message": {"role": "user", "content": _filler(turn["user_text"], t)},
            }
        )
        uses, results = [], []
        for row in turn["events"]:
            kind, target, args, result = row[:4]
            if kind == _BASH:
                cid = row[4]
                tag = f"echo {cid} "
                inp = {"command": tag + _filler(max(target - len(tag), 0), cid)}
                inp.update(_arg("description", args, cid))
                name = "Bash"
            elif kind == _MCP:
                inp = {"task_id": _filler(target, seq)} if target else {}
                inp.update(_arg("note", args, seq))
                name = "mcp__synthetic__tool"
            else:
                inp = {"query": _filler(max(target, 1), seq)}
                inp.update(_arg("note", args, seq))
                name = "ToolSearch"
            uses.append({"type": "tool_use", "id": f"t{seq}", "name": name, "input": inp})
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": f"t{seq}",
                    "is_error": False,
                    "content": _filler(result, seq),
                }
            )
            seq += 1
        content = [{"type": "text", "text": _filler(n, t)} for n in turn["assistant_text"]]
        lines.append(
            {
                "type": "assistant",
                "uuid": f"a{t}",
                "timestamp": stamp,
                "message": {"role": "assistant", "content": content + uses},
            }
        )
        if results:
            lines.append(
                {
                    "type": "user",
                    "uuid": f"r{t}",
                    "timestamp": stamp,
                    "message": {"role": "user", "content": results},
                }
            )
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def load() -> dict:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover -- needs the reference transcript
    PROFILE.write_text(json.dumps(derive(sys.argv[1]), indent=None) + "\n", encoding="utf-8")
    print(f"wrote {PROFILE}")
