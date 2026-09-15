#!/usr/bin/env bash
# SessionStart hook — inject this agent's recent scribe digests.
#
# Register in ~/.claude/settings.json:
#
#   "hooks": { "SessionStart": [ { "matcher": "", "hooks": [
#     { "type": "command", "command": "/path/to/scribe/hooks/session-start.sh" } ] } ] }
#
# Set SCRIBE_PYTHON to an interpreter that can import scribe (a venv, or an install).
# Deliberately no PYTHONPATH fallback to the adjacent src/: a hook that silently preferred a
# checkout over the deployed package would run whatever branch happened to be checked out.
#
# stdin is discarded before anything else. common.sh in the plugin this replaces learned that
# the hard way -- a `$(cat)` with no EOF blocks a session start indefinitely -- and this hook
# reads no input at all, so there is nothing to lose by closing it.
exec < /dev/null
# `set -e` is safe alongside the explicit `if out=$(...)` below: a command substitution
# in an `if` condition is exempt, so the failure path stays reachable rather than
# exiting the hook before it can report anything.
set -euo pipefail

PY="${SCRIBE_PYTHON:-python3}"

# stderr is passed through, not captured: a non-zero hook surfaces it to the operator, which
# is the whole diagnostic. stdout is captured so a failed run emits no partial JSON.
if out=$("$PY" -m scribe journal "$@"); then
  printf '%s\n' "$out"
  exit 0
fi

# Reached when scribe cannot run at all -- not installed, or a malformed config. Say so in
# the one field a session will render rather than emitting nothing: an injection that is
# merely absent looks exactly like a quiet day, and that ambiguity is what this component
# exists to remove.
printf '%s\n' '{"systemMessage": "[scribe] journal hook failed — scribe unavailable or misconfigured; see hook stderr"}'
exit 0
