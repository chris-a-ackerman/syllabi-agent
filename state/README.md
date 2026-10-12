# state/: the HW4 forum agent's memory

On GitHub Actions every run starts on a fresh runner, so the forum agent's memory lives here and
`.github/workflows/hw4-forum.yml` commits it back to `main` after each run
(`hw4: state after run <run id> [skip ci]`). The workflow sets `DATA_DIR` to this directory, so
the paths below are the same ones `forum.py` uses under `/data` on Maritime.

| File | Written by | What it holds |
| --- | --- | --- |
| `memory/forum-state.json` | `forum.py` | Own user id; ids of entries already read (`seen`); own posts (entry id, parent id, text hash, own text, URL); pending and abandoned post intents; `post_times` and `attempt_times` (the rate limit, kept 24 h); consecutive failures; last cycle time. |
| `memory/forum-halt` | `forum.py`, or a human | Present = posting is off. The tool writes it after 3 failures in a row or a rejected token. Only a human deletes it. |
| `memory/hw4-state.json` | `scripts/hw4-cycle.py` (HW4-2) | The driver's decisions: ids, hashes, classifications. |
| `logs/forum.jsonl` | `forum.py`, `scripts/hw4-log-status.py` | One line per command: time, command, counts, decision, reason, entry id, error code, attempts; plus one `status` line per workflow run with its run id. |
| `work/forum/` | the driver | Scratch text files for `forum post --text-file`. Ignored by git; only `.gitkeep` is committed. |

**What never goes in here:** other students' or agents' post text (the files hold their entry ids
and our hashes, nothing they wrote), any token or key, request headers, or environment values.
`forum.py` scrubs secret values from everything it writes, and `.gitignore` keeps lock and temp
files out.

**Un-halt:** delete `memory/forum-halt`, commit and push. The next run starts counting failures
from zero.
