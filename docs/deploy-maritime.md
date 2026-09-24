# Deploying on Maritime

The runbook below was verified step by step on 2026-09-24, on agent `class-prep-repo`
(OpenClaw template, image `ghcr.io/openclaw/openclaw:2026.7.1`).

There are two places you type commands:

- **Terminal**: your laptop, with the Maritime CLI (`npm install -g maritime-cli`, then `maritime login`).
- **Agent chat**: the **OpenClaw Dashboard** button on the agent's page in the Maritime dashboard.
  Prefix shell commands with `Run:`. Use this chat, not `maritime chat`, for anything that might
  prompt: `maritime chat` starts a new conversation on every call (`conversationId: null`), so a
  follow-up `/approve` has nothing to attach to.

## 1. Create the agent (terminal)

```
maritime create class-prep-repo --template openclaw
maritime status class-prep-repo
```

## 2. Clone the repo onto the volume (agent chat)

```
Run: git clone -b main https://github.com/chris-a-ackerman/syllabi-agent.git /data/syllabi-agent
```

Use this chat because `git clone` can take longer than the 30 s reply budget. Over `maritime chat`
it came back as a garbage reply and the clone never finished. The repo is public, so no token is needed.
Before this PR is merged, use `-b claude/class-prep-agent-scaffold-86anxm`. After it merges, switch with
`Run: cd /data/syllabi-agent && git fetch && git checkout main && git pull`.
To update later: `Run: cd /data/syllabi-agent && git pull`.

## 3. Install the workspace (agent chat)

```
Run: sh /data/syllabi-agent/scripts/install-workspace.sh
```

The script backs up the existing files to `/data/workspace-backups/<timestamp>/`, then:

- keeps Maritime's `MARITIME.md` and its block at the top of `AGENTS.md`, and puts our `AGENTS.md`
  below that block (replacing OpenClaw's default text);
- copies our `SOUL.md`;
- symlinks `skills/`, `agents/` and `memory-templates/` into the repo;
- creates `/data/memory`, `/data/logs` and the other runtime folders, and seeds `course-notes.md`
  and `prep-log.json` if they are missing.

Re-run it after every `git pull` that touches `workspace/AGENTS.md` or `SOUL.md`. The symlinked
folders update on their own.

**Check** (terminal): `maritime restart class-prep-repo`. Then, in the agent chat, ask
*"What are your three trigger phases and your stop rules? How do you send me a file?"*. It should
answer prep/poll/notify, 3 attempts / 25 tool calls, and `maritime-share`.

## 4. Connect Telegram (dashboard)

Agent → **Channels** tab (not the sidebar's Integrations) → Telegram card → one-tap connect.
If the browser blocks the `tg://` link (no Telegram Desktop installed), open
`https://t.me/MaritimeAIBOT?start=<code>` instead, or send `/start <code>` to @MaritimeAIBOT
from your phone.

**Check** (agent chat):
```
Run: env | grep MARITIME_TELEGRAM; maritime-telegram-send "Test from class-prep-repo"
```
Restart the agent if the environment variable isn't there yet.

## 5. Schedules

Wake trigger (terminal), then jobs (agent chat). Details are in [`triggers/README.md`](../triggers/README.md).

```
maritime triggers create class-prep-repo --type cron --cron "*/30 * * * *"
```
```
Run: sh /data/syllabi-agent/scripts/install-jobs.sh
```

**Check**: `maritime triggers list class-prep-repo` shows the cron trigger, and `openclaw cron list`
shows the five `class-prep-*` jobs with `America/New_York`.

## 6. Still to do

- Merge the subagent settings from `config/openclaw.example.json5` into `~/.openclaw/openclaw.json`
  (back it up first; Maritime keeps an `openclaw.json.last-good`), then test `sessions_spawn`.
- Check the V2–V4 tooling: `python3`, `pip`, `notebooklm-py`, `rclone`, and whether installs
  survive a restart.
- Upload secrets to `/data/secrets/` and `/data/rclone/`, and set the environment variables with
  `maritime env set` (see `.env.example`).

---

## Verification log (2026-09-24)

These are the evidence for the writeup. Each row is something we tested, not something we assumed.

| Test | Result |
| --- | --- |
| `ls /data` | `inbox  lost+found  outbox`, so it's a real volume |
| File in `/data` across `maritime sleep` and `maritime restart` | survived |
| `echo $HOME` | `/data`, so `~/.openclaw` = `/data/.openclaw` |
| `openclaw approvals get` | `security=full, ask=off`, no allowlist file |
| `curl`/`git` over `maritime chat`, before the `AGENTS.md` fix | the model asked for `/approve` anyway |
| Same, after adding "Command execution (pre-authorized)" | ran; a scheduled job sent `HTTP/2 200` to Telegram |
| `maritime-telegram-send` | delivered to Telegram |
| One-time OpenClaw job, agent asleep, no Maritime trigger | did **not** fire; ran late when the dashboard woke the agent |
| `find /data/.openclaw -ipath '*cron*'` | nothing; jobs are in `state/openclaw.sqlite` |
| Maritime trigger `8 18 * * *` (UTC) + OpenClaw job at 14:08 ET, agent asleep | logs show a bare wake at 18:08 UTC; the job ran; the message arrived |
| Maritime trigger `*/5 * * * *` | accepted; woke the agent every 5 minutes |
| `maritime triggers create --help` | only `--type` and `--cron`: no prompt, no timezone |

### Gotchas we hit

- `maritime triggers list|delete` take the subcommand first:
  `maritime triggers list <agent>`, `maritime triggers delete <agent> <trigger-id>`.
- A `maritime link` in a local clone writes `.maritime/project.json`, which only holds the agent id.
  It's in `.gitignore`.
- `maritime deploy --source github` builds from a Dockerfile in the repo. This repo has none, so
  don't use it.
- Replies that time out (long commands) can come back as unrelated text or "No output". The
  command may or may not have finished, so check the result with a second, quick command.
