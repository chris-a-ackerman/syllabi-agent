# forum-notes.md: what I learned building this agent

First-hand lessons from building and running class-prep-agent (MAS.665, HW2 and HW3), taken from
this repo's writeup, deploy log and READMEs. This is the only source for claims about what I
built. If a point is not here, do not claim it. One paragraph each: what happened, what fixed it.

**A sleeping container runs no cron.** The agent lives in a container that sleeps when idle. The
platform's docs said it mirrored the harness's scheduled jobs into wake triggers, but the harness
had moved its jobs into a SQLite database, so nothing was mirrored. A test job silently did not
fire while the agent slept and ran late the next time something woke it. The fix is two layers:
one platform wake trigger every 30 minutes, plus the harness's own cron jobs in Eastern time that
hold the prompts.

**The instruction file was cut in half without warning.** The harness loads at most 20,000
characters of each workspace file by default. The instructions were about 31,000, so isolated
cron sessions silently lost the later sections: the phases, the state machine and the stop rules.
A prep run then died partway and wrote no log. A config check showed about 19,000 of 31,000
characters injected. Raising `bootstrapMaxChars` to 40,000 fixed it, and a test now fails if the
file outgrows the limit.

**Long work split into start, poll and notify.** A chat reply gets about 30 seconds and a shell
command 60, while a NotebookLM podcast takes minutes. So prep only starts the podcast, a poll job
every 30 minutes checks it, and a notify pass sends the brief at a fixed time. Because any phase
can die or run twice, each one has to be safe to re-run: it reads memory first and skips every
step already recorded.

**Memory through a tool, not hand-edited JSON.** All memory reads and writes go through one
command that validates against a schema and writes atomically. It enforces the rules in code:
never redo a recorded step, never start a second podcast, never send a message twice. The catch:
one poll marked an empty brief as sent, and from then on "never redo a recorded step" also meant
"never repair a bad record". Rules the tool enforced held every time; the same rule written only
in the prompt was followed by some runs and not others. Guards belong in the tool.

**A deny-list for the subagent missed tools.** The brief-writer subagent should have no tools,
since it reads untrusted text. A hand-written deny list left some behind, because subagents also
get session, memory, plugin and MCP tools. `deny: ["*"]` fixed it; the spawned subagent then
reported an empty tool list.

**Stable error codes instead of error text.** Every tool returns one JSON object with `ok` and,
on failure, a stable `error.code`. A Canvas 401 means the token is dead, so ask a human once; a
403 means one file is locked, so fall back to another link or ask for the PDF. The agent branches
on the code instead of guessing from a message.

**Canvas tokens can't be scoped.** A personal Canvas token can do anything the account can,
including submit. So the read skill is GET-only by construction, the rules forbid raw HTTP to
Canvas, and the only write path is one command with its own guards, pinned to one discussion
topic.

**Files written just before a restart came back empty.** After a `git pull`, a platform restart
a few minutes later left the changed files and some git objects as 0-byte files, and the same
happened to a backup. Running `sync` before every restart fixed it, and the install script now
ends with `sync`.

**How this forum agent is built.** One cycle every 3 hours decides whether to post or skip. The
tool re-reads the course team's control line before every write and refuses unless it says
RUNNING. Each post is recorded as an intent before the POST; after a timeout or an unclear answer
it re-reads the forum and reconciles before any retry, so a lost acknowledgement never becomes a
duplicate. It allows at most 3 posts per hour, and after 3 failures in a row it writes a halt file
that only a human removes.
