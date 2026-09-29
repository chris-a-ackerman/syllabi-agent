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

Use this chat, not `maritime chat`: a reply that doesn't come back normally (here, the agent was
waiting for `/approve`) shows up over `maritime chat` as junk text, and the clone never ran. The repo is public, so no token is needed.
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

## 6. Subagent config (agent chat)

```
Run: cp ~/.openclaw/openclaw.json /data/openclaw.json.pre-subagent
Run: cd /data/syllabi-agent && openclaw config patch --file config/openclaw.example.json5 --dry-run
Run: cd /data/syllabi-agent && openclaw config patch --file config/openclaw.example.json5 && openclaw config validate
```
Then `maritime restart class-prep-repo`. The change survives the restart.

**Check** (from the terminal; the file trick avoids a junk reply while the child runs):
```
maritime chat class-prep-repo "Use sessions_spawn with agentId \"brief-writer\", mode \"run\", context \"isolated\", and this task: \"Reply with a JSON array of the exact names of every tool you can call, and nothing else. If you have none, reply [].\" Write the child's reply verbatim to /data/spawn-test.txt." --json
maritime chat class-prep-repo "Run: cat /data/spawn-test.txt" --json
```
Expected: `[]`. To undo: `cp /data/openclaw.json.pre-subagent ~/.openclaw/openclaw.json`, then restart.

## 7. Still to do

- Install the nlm skill's CLI (SYL-94; verified 2026-09-29 with notebooklm-py 0.8.3). The
  container has Python 3.11 but no `pip`, so it goes in a venv on the volume. The install takes
  longer than the 60 s command cap, so run it in the background and read the log:
  ```
  Run: nohup sh -c 'python3 -m venv --without-pip /data/venvs/nlm && curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && /data/venvs/nlm/bin/python /tmp/get-pip.py && /data/venvs/nlm/bin/pip install "notebooklm-py[headless]" && /data/venvs/nlm/bin/notebooklm --version && echo INSTALL-DONE' > /data/nlm-install.txt 2>&1 &
  Run: tail -20 /data/nlm-install.txt
  ```
  Then, in the terminal: `maritime env set class-prep-repo NLM_BIN=/data/venvs/nlm/bin/notebooklm --no-secret --reload`.
- NotebookLM auth (SYL-94 Security): use a **dedicated Google account for the agent** (e.g.
  `syllabi382@gmail.com`), never your main Gmail. A master token can mint cookies for any
  Google service on that account. Use the same account for Drive (V4).
  1. As the agent account, open `https://notebooklm.google.com` in a browser once and accept the
     terms. Until then `create` fails with `UNCONFIRMED_WRITE`.
  2. In an incognito window, open `https://accounts.google.com/EmbeddedSetup`, sign in as the
     agent account, and copy the `oauth_token` cookie (DevTools → Application → Cookies).
  3. Terminal, straight away (the token is single-use and short-lived; the agent refuses a
     command with a live token in the chat text, so it goes in as a secret):
     ```
     maritime env set class-prep-repo NLM_OAUTH_TOKEN='<token>' --reload
     maritime chat class-prep-repo "Run: NOTEBOOKLM_HOME=/data/notebooklm /data/venvs/nlm/bin/notebooklm login --master-token --account syllabi382@gmail.com --oauth-token \"\$NLM_OAUTH_TOKEN\" > /data/nlm-login.txt 2>&1; echo exit=\$?" --json
     maritime env rm class-prep-repo NLM_OAUTH_TOKEN
     ```
  Then:
  ```
  Run: python3 /data/syllabi-agent/workspace/skills/nlm/scripts/nlm.py check
  ```
  `check` must print `{"status": "ok", ...}`. `{"error": "NLM_AUTH"}` means the login is missing or
  stale. `NLM_NOT_INSTALLED` means `NLM_BIN` doesn't point at the CLI. `nlm.py` keeps
  `/data/notebooklm` at mode 700 and its credential files at 600.
- Done when (V3, live): `nlm.py prep MAS.665 <date> /data/readings/MAS.665/<date>/<file>.pdf --topic "<topic>"`
  returns `{notebook_id, task_id}`. A few minutes later, `nlm.py status <notebook_id> <task_id>`
  goes from `pending` to `ready`, with an mp3 in `/data/podcasts/` and a Drive link. Swapping in a
  stale cookie-mode `storage_state.json` must yield `{"error": "NLM_AUTH"}`. Restart the agent
  afterwards and re-run `check` to confirm the install survived.
- The canvas skill (SYL-93) needs only `python3` (standard library). Check it
  with, in the agent chat:
  ```
  Run: python3 /data/syllabi-agent/workspace/skills/canvas/scripts/canvas.py whoami
  Run: python3 /data/syllabi-agent/workspace/skills/canvas/scripts/canvas.py modules 40577
  ```
- Install rclone for the drive skill (SYL-95). In the agent chat:
  ```
  Run: rclone version
  ```
  If it is missing, put a static build on the volume (pick `arm64` if `uname -m` says `aarch64`).
  The download can take longer than the 60 s command cap, and a cut-off download leaves a binary
  that dies with `Segmentation fault` (exit 139; `drive.py check` then reports `DRIVE_NET`,
  "rclone exit -11"). So run it in the background, and only copy the binary into `/data/bin/`
  once it has started (checked 2026-09-29 with rclone v1.75.1 on x86_64):
  ```
  Run: rm -rf /data/rc && mkdir -p /data/rc /data/bin && cd /data/rc && nohup sh -c 'curl -fsSL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o rclone.zip && python3 -m zipfile -e rclone.zip x && chmod +x x/*/rclone && x/*/rclone version && cp x/*/rclone /data/bin/rclone && /data/bin/rclone version && echo INSTALL-DONE' > /data/rclone-install.txt 2>&1 &
  Run: tail -5 /data/rclone-install.txt
  ```
  Wait for `INSTALL-DONE` (re-run the `tail`), then `Run: rm -rf /data/rc`. If the log shows
  `Segmentation fault`, re-run the install. Then, in the terminal:
  `maritime env set class-prep-repo DRIVE_RCLONE_BIN=/data/bin/rclone --no-secret --reload`. Then, with
  `/data/rclone/rclone.conf` uploaded (next bullet):
  ```
  Run: python3 /data/syllabi-agent/workspace/skills/drive/scripts/drive.py check
  ```
  `check` must print `{"ok": true, "root": "gdrive:ClassPrep", ...}`. `DRIVE_AUTH` means the
  config file is missing, not `chmod 600`, or its token is stale; `DRIVE_NOT_INSTALLED` means
  rclone was not found. Then one real upload, twice:
  `Run: /data/syllabi-agent/workspace/skills/drive/scripts/drive-put /data/readings/MAS.665/<date>/<file>.pdf Readings/MAS.665/<date>`.
  The first run says `uploaded: true`; open its `web_url` on the iPad; the second run must say
  `uploaded: false` with the same URL and transfer nothing. Restart the agent and re-run `check`
  to confirm the install survived.
- Upload secrets to `/data/secrets/` and `/data/rclone/`, and set the environment variables with
  `maritime env set` (see `.env.example`).
- **Drive identity and rclone config (SYL-95 Security).** rclone runs as the **dedicated agent
  Google account** (the one from V3), never Chris's main account, with `scope = drive.file`, so
  the agent can only see and change files it created itself. The container has no browser, so:
  1. On the laptop, signed in to the agent account in the browser: `rclone authorize "drive"`.
     It prints a token JSON.
  2. Write `/data/rclone/rclone.conf` (do not commit it, do not paste it into chat or logs):
     ```
     [gdrive]
     type = drive
     scope = drive.file
     token = {"access_token":"…","token_type":"Bearer","refresh_token":"…","expiry":"…"}
     ```
  3. `Run: chmod 600 /data/rclone/rclone.conf`. The drive skill refuses to run (`DRIVE_AUTH`,
     "chmod 600") while the file is readable by group or others: it holds a refresh token.
  4. Let the agent create the folder: the first `drive-put` makes `ClassPrep/` (with `drive.file`,
     a folder you make by hand is invisible to the agent and uploads into it fail with
     `DRIVE_AUTH` / `insufficientFilePermissions`).
  5. From the **agent account** in drive.google.com, share `ClassPrep` with Chris's main account
     by email (Viewer or Editor), **not** "anyone with the link". It shows up under *Shared with
     me* in the iPad Files app and in Goodnotes' "Import from Google Drive"; `web_url` links open
     on the phone because it is signed in to the main account.
  Layout: `ClassPrep/Readings/<course>/<YYYY-MM-DD>/*.pdf` and
  `ClassPrep/Podcasts/<course>-<YYYY-MM-DD>.mp3`. The skill never runs `rclone link`, so no file is
  ever reachable without a Google login. Fallback if Drive is down: send PDFs as Telegram
  attachments (100 MB cap).
- Smoke-test the brief skill (agent chat):
  `Run: python3 /data/syllabi-agent/workspace/skills/brief/scripts/brief.py prompt` (expect the
  brief-writer prompt, read from the workspace's `agents/brief-writer.md`), then
  `Run: which pdftotext; python3 -c "import pypdf"` to learn which PDF extractor the container
  has (the built-in fallback handles text PDFs, not scans). Then ask the agent *"How do you check
  the brief-writer's reply before you store it?"* (expect `brief validate`, the schema and the
  ≥ 0.9 fuzzy check).
- Smoke-test the memory tool (agent chat):
  `Run: python3 /data/syllabi-agent/workspace/skills/preplog/scripts/preplog.py init` then
  `Run: python3 /data/syllabi-agent/workspace/skills/preplog/scripts/preplog.py validate`.
  Expect `created_prep_log: false` when `install-workspace.sh` already seeded the file, and
  `sessions: 0`. Then ask the agent *"How do you record that a podcast was started, and what stops
  you from starting a second one?"* (expect `preplog set-notebook` and `ALREADY_HAS_NOTEBOOK`).
- The job prompts in `triggers/*.md` now name the `preplog` commands. After pulling that change,
  re-install the jobs so OpenClaw picks up the new text:
  `Run: cd /data/syllabi-agent && git pull && sh scripts/install-jobs.sh --replace`.

---

## Verification log (2026-09-24, 2026-09-27 and 2026-09-29)

These are the evidence for the writeup. Each row is something we tested, not something we assumed.

| Test | Result |
| --- | --- |
| `ls /data` | `inbox  lost+found  outbox`, so it's a real volume |
| File in `/data` across `maritime sleep` and `maritime restart` | survived |
| `echo $HOME` | `/data`, so `~/.openclaw` = `/data/.openclaw` |
| `openclaw approvals get` | `security=full, ask=off`, no allowlist file |
| `curl`/`git` over `maritime chat`, before the `AGENTS.md` fix | the model asked for `/approve` anyway |
| Same, after adding "Command execution (pre-authorized)" | ran; a scheduled job sent `HTTP/2 200` to Telegram |
| `python3 --version`; `python3 -m pip` (09-29) | `Python 3.11.2`; `No module named pip`, so the nlm CLI goes in a venv at `/data/venvs/nlm` |
| notebooklm-py 0.8.3 in the venv, `NLM_BIN` set (09-29) | installed; every CLI command and flag `nlm.py` uses exists |
| `notebooklm login --master-token --oauth-token "$NLM_OAUTH_TOKEN"` (09-29) | logged in; `nlm.py check` → `{"status": "ok"}`. A live token in the chat text is refused by the agent, so it goes in as a Maritime secret |
| `nlm.py check` with an empty `NOTEBOOKLM_HOME` (09-29) | exactly `{"error": "NLM_AUTH"}` |
| `create` before the agent account had opened NotebookLM (09-29) | `UNCONFIRMED_WRITE` twice; works once the terms are accepted in a browser |
| `generate audio` right after `source add` (09-29) | `NOTEBOOKLM_ERROR` / `UNCONFIRMED_WRITE` while the PDF processed; the same call worked minutes later. `prep` now runs `source wait` first |
| `generate audio` that answered `RATE_LIMITED` (09-29) | NotebookLM started the podcast anyway. `prep` now re-checks `artifact list` after a failed generate |
| `nlm.py status` on a finished podcast (09-29) | downloaded `/data/podcasts/TEST-2026-10-01.mp3` (44.5 MB); `drive_error: DRIVE_NOT_INSTALLED` until V4 |
| `preplog set-notebook … --task-id` then `due` (09-29) | `podcast_pending` lists `notebook_id` and `task_id` for `nlm-status` |
| `maritime restart`, then `notebooklm --version`, `$NLM_BIN`, `nlm.py check` (09-29) | 0.8.3, `/data/venvs/nlm/bin/notebooklm`, `{"status": "ok"}`: the venv, the env var and the login survive |
| `maritime-telegram-send` | delivered to Telegram |
| One-time OpenClaw job, agent asleep, no Maritime trigger | did **not** fire; ran late when the dashboard woke the agent |
| `find /data/.openclaw -ipath '*cron*'` | nothing; jobs are in `state/openclaw.sqlite` |
| Maritime trigger `8 18 * * *` (UTC) + OpenClaw job at 14:08 ET, agent asleep | logs show a bare wake at 18:08 UTC; the job ran; the message arrived |
| Maritime trigger `*/5 * * * *` | accepted; woke the agent every 5 minutes |
| `maritime triggers create --help` | only `--type` and `--cron`: no prompt, no timezone |
| Two one-time jobs at 16:55 ET, each running a 45 s script (09-27) | serialized: A 20:55:04–20:55:49, B 20:55:57–20:56:42 UTC |
| `config patch` with `agents.entries` (09-27) | rejected: "agents: Unrecognized key: entries" (2026.7.1 uses `agents.list`) |
| `brief-writer` with a hand-written tool deny list (09-27) | child still had tools |
| `brief-writer` with `profile: "minimal", deny: ["*"]` (09-27) | child reported `[]` |
| `openclaw.json` after `maritime restart` (09-27) | patch kept; `last-good` updated to the new file |
| `CANVAS_*` set, "what readings are on Canvas for MAS.665?" (09-27) | used the canvas skill, reported missing `scripts/canvas`; did not call OpenClaw's `canvas` tool |
| Restart log (09-27) | "Captured derived image v4 (restart). Edits will survive restart" |

### Gotchas we hit

- `maritime triggers list|delete` take the subcommand first:
  `maritime triggers list <agent>`, `maritime triggers delete <agent> <trigger-id>`.
- A `maritime link` in a local clone writes `.maritime/project.json`, which only holds the agent id.
  It's in `.gitignore`.
- `maritime deploy --source github` builds from a Dockerfile in the repo. This repo has none, so
  don't use it.
- Over `maritime chat`, a reply that doesn't come back normally shows up as unrelated text
  ("node-inspect-debugger") or "No output". Seen while the agent waited for `/approve` and during a
  `sessions_spawn` run. Have the agent write results to a file and read it with a second message.
- Current OpenClaw docs are newer than the 2026.7.1 the template runs; read them at tag `v2026.7.1`.
- `openclaw cron add --at ...` rejects `--exact` (cron schedules only).
- Don't deny subagent tools by listing them: sub-agents also get session, memory, goal, plugin and
  MCP tools. Use `deny: ["*"]`.
