"""The environment scribe hands to a child process: named variables only, never its own.

**A sweep's environment is not a child's.** A sweep runs with the summarizer's credential in
it (`MISTRAL_API_KEY` on forge), and no child it starts has a use for that key. A subprocess
started without `env=` inherits all of it -- SC-06 in the fleet's pattern base, where the
recurring form is exactly a new subprocess added without one. Every child scribe starts gets
`BASE_ENV` plus the names its caller lists, by exact name, and nothing else.

A leaf module so that both callers can import it: `pipeline` builds the post-write hook's
environment from it, and `summarize.providers` builds `claude -p`'s. `pipeline` already
imports `providers`, so the set cannot live in either without a cycle.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

#: What every child sees, whatever it is for: enough to find its binary and run in the
#: operator's locale, and nothing that authenticates anything. `HOME` is load-bearing as well
#: as harmless: `claude` reads its credentials file and its settings (including the `env`
#: block, where an operator switches off its auto-updater) from under it.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")


def allowlisted_env(names: Iterable[str]) -> dict[str, str]:
    """`{name: value}` for each of `names` that is set. An unset name is omitted, not blanked."""
    return {k: os.environ[k] for k in dict.fromkeys(names) if k in os.environ}
