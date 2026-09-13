### 22:34

**Asked:** Check the queue entry for id 417 and summarise the upstream PRs.

**Done:**
- Ran `set -euo pipefail` before the fetch and read `/srv/app/src/tools/queue.py`
- Saw `submit_task` take `on_behalf_of` and `max_retries=2`, returning `popen(["claude","-p"], env=child_env)`

**Found:**
- The entry is `parked`, and its status history reads `approved → in-progress → completed`
- The stage runs `mistral-small-latest` with `credentials: source: env`

**Tickets:**
- Upstream #655 and #639 are dependency bumps
- The entry's own identifier is #417
