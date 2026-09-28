# class-prep-agent

An OpenClaw agent, hosted on [Maritime](https://maritime.sh), that gets me ready for each class.
Homework 2 for MIT AI Studio (MAS.665): *Engineer a Reliable Agent*.

Deploying means creating an agent from Maritime's OpenClaw template, cloning this repo onto its
persistent volume, running two install scripts, and adding one Maritime wake trigger. The steps
were verified on 2026-09-24: see [`docs/deploy-maritime.md`](docs/deploy-maritime.md).

> **Status: V0 scaffold + syllabi skill.** The instructions, schemas, trigger prompts and tool
> contracts are in place. The `syllabi` skill is implemented (SYL-105, tested against a fake
> endpoint, not yet run against the live app). `canvas` (SYL-93, PR #2), `nlm` (SYL-94, PR #3)
> and `drive` (SYL-95, PR #4) are on their own branches.

---

## Repo map

```
.
├── README.md                     ← this file: the plan (source of truth) + repo map
├── .env.example                  ← every secret/setting, with where it comes from
├── .gitignore                    ← keeps secrets and /data runtime output out of git
├── config/
│   └── openclaw.example.json5    ← openclaw.json settings to merge in (subagent still untested)
├── scripts/
│   ├── install-workspace.sh      ← merges workspace/ into Maritime's OpenClaw workspace (re-run after git pull)
│   └── install-jobs.sh           ← creates the 5 OpenClaw cron jobs (ET) from triggers/*.md
├── workspace/                    ← our half of the OpenClaw workspace (installed by install-workspace.sh)
│   ├── AGENTS.md                 ← operating instructions: phases, stop rules, ask-a-human, hard rules
│   ├── SOUL.md                   ← persona / tone / boundaries
│   ├── agents/
│   │   └── brief-writer.md       ← subagent definition: role, bounded context, output contract
│   ├── skills/
│   │   ├── syllabi/
│   │   │   ├── SKILL.md          ← the schedule of record: upcoming / course / check, errors, agent rules
│   │   │   └── scripts/syllabi.py← the CLI over the app's agent-upcoming endpoint; `syllabi` is a symlink
│   │   ├── canvas/SKILL.md       ← Canvas LMS reads (stub)
│   │   ├── nlm/SKILL.md          ← nlm-prep / nlm-status for NotebookLM (stub)
│   │   └── drive/SKILL.md        ← drive-put via rclone (stub)
│   └── memory-templates/
│       ├── prep-log.schema.json  ← JSON Schema for /data/memory/prep-log.json
│       ├── brief.schema.json     ← JSON Schema for brief-writer output
│       └── course-notes.md       ← template seeded to /data/memory/course-notes.md
├── triggers/
│   ├── README.md                 ← how scheduling works: 1 Maritime wake trigger + 5 OpenClaw jobs (ET)
│   ├── prep.md                   ← exact prompt of the 19:00 job
│   ├── poll.md                   ← exact prompt of the 30-min poll jobs
│   └── notify.md                 ← exact prompt of the 06:30 job
├── docs/
│   ├── deploy-maritime.md        ← verified deploy runbook + what we learned about Maritime
│   ├── tool-contract.md          ← every tool: name, inputs, outputs, error shape
│   └── hw2-writeup.md            ← HW2 writeup skeleton (one heading per rubric item)
├── tests/
│   └── test_syllabi.py           ← syllabi skill tests, no network: python3 -m unittest discover -s tests
└── evidence/
    ├── eval/cases.md             ← the 5 eval cases, baseline vs improved (results blank)
    └── failures/                 ← screenshots/logs of failures and recoveries
```

At runtime (on the Maritime volume, never in git). `HOME` is `/data`:

```
/data/
├── .openclaw/                    ← OpenClaw config, state DB (cron jobs) and workspace
├── syllabi-agent/                ← this repo, cloned
├── memory/prep-log.json          ← one record per (course, class_date)
├── memory/course-notes.md        ← learned per-course quirks
├── logs/<YYYY-MM-DD>-<trigger>.md← one run log per trigger firing (appended)
├── readings/<course>/<date>/     ← downloaded PDFs
├── podcasts/<course>/<date>.mp3  ← NotebookLM audio overviews
├── work/<course>/<date>/         ← extracted text, brief-writer input/output
├── secrets/notebooklm-cookies.json
└── rclone/rclone.conf
```

---

## The plan (source of truth)

### Goal

A hosted, always-on agent that, before each class, finds the readings, files them where my iPad
can get them, builds a NotebookLM podcast, drafts pre-class questions, and sends me a Telegram
brief with the podcast link and "what this class is about / how to prepare."
Timing: **24 hours before class** if something is due beforehand, **morning-of** otherwise.

### What's automatable

- **Find readings:** syllabi app endpoint (schedule, topics, reading links) + Canvas
  (Modules/Files/assignments/pages).
- **Store readings:** Google Drive folder `Readings/<course>/<date>/`. iCloud has no server API;
  the iPad Files app mounts Drive, and Goodnotes imports from Drive.
- **Goodnotes import:** *not* automatable. It takes one manual tap, and the writeup says so.
- **NotebookLM podcast:** `notebooklm-py` (unofficial, cookie auth). There is no consumer API.
  Cookie expiry is a real failure mode, and it is the rubric's failure-recovery test.
- **Pre-class questions:** a subagent drafts them and I review them. The agent never submits
  anything to Canvas.
- **External readings behind logins** (HBS cases, library): the agent escalates to me on
  Telegram, asks me to drop the PDF in the Drive folder, and continues.

### Maritime constraints that shape the design

- Containers sleep when idle and wake in well under a second on a message or trigger. Nothing
  runs while asleep, so scheduling takes **one Maritime wake trigger every 30 minutes plus
  OpenClaw cron jobs in America/New_York** that do the work
  ([`triggers/README.md`](triggers/README.md) explains why both are needed).
- **Only `/data` persists** across sleep/wake/restart. `HOME` is `/data`, so `~/.openclaw` (config,
  cron jobs, workspace) persists too. All memory files, logs, secrets (NotebookLM cookies, rclone
  config), downloaded readings and podcasts go under `/data`.
- **30-second chat reply budget, 60 s default command timeout.** Long work runs in the background,
  so podcast generation is *started* in one run and *polled* by a later one. Over `maritime chat`,
  a reply that doesn't finish in time comes back as junk (see Deploy findings).
- **The model is GPT-5.4** through Maritime's LLM proxy by default (no API key needed).
- 2 GB RAM / 5 GB SSD base; 100 MB per file transfer.

### Tools (rubric 1)

Full contracts in [`docs/tool-contract.md`](docs/tool-contract.md).

- **`canvas` skill** (or `vishalsachdev/canvas-mcp` as an MCP server if the template supports it):
  `list_modules`, `list_files`, `download_file` → `/data/readings/`, `upcoming_assignments`,
  `get_page`. Reports 401 and 403 as separate errors.
- **`syllabi` skill** (`workspace/skills/syllabi/scripts/syllabi.py`): `upcoming --days 3
  --within-hours 48` calls the syllabi app's `agent-upcoming` edge function with a scoped,
  revocable agent token (minted in the app's Settings → Agent access) and returns one record per
  upcoming class meeting: the prep-log `key`, `class_start`, `canvas_course_id`, what is
  `due_before_class`, and the `notify_at` the agent must store, all in ET. `course <code>` gives
  one course's schedule and policies; `check` is the auth smoke test. Auth failure is
  `SYLLABI_401`. The app does not send reading links yet, so readings come from Canvas.
- **`nlm-prep <course> <date> <pdf...>`** creates a notebook, adds sources, *starts* the audio
  overview, and returns `{notebook_id}` immediately.
  **`nlm-status <notebook_id>`** returns `{status: pending|ready|failed, audio_url?}`. When the
  audio is ready, it downloads the mp3 to `/data/podcasts/` and pushes it to Drive.
  Cookies are at `/data/secrets/notebooklm-cookies.json`. Auth failure returns error code `NLM_AUTH`.
- **`drive-put <local_path> <remote_dir>`** returns a share link, via rclone. Config is at
  `/data/rclone/rclone.conf`. Idempotent.
- **Telegram** (Maritime channel) for briefs and questions, sent with `maritime-telegram-send`.

### Memory (rubric 2): read at the start of every run, written at the end

- **`/data/memory/prep-log.json`** (schema: [`workspace/memory-templates/prep-log.schema.json`](workspace/memory-templates/prep-log.schema.json)):
  one record per (course, class_date):
  `readings[] {title, source: canvas_file|external, id_or_url, local_path?}`, `drive_paths[]`,
  `notebook_id?`, `podcast_url?`, `brief?` (JSON from subagent), `notify_at`,
  `status ∈ {pending, podcast-pending, ready, done, partial, needs-human}`, `attempts`,
  `last_error?`, `history[] of {ts, trigger, action}`.
  **Never redo a recorded step. Never start a second podcast for a session that has a `notebook_id`.**
- **`/data/memory/course-notes.md`** (template: [`workspace/memory-templates/course-notes.md`](workspace/memory-templates/course-notes.md)):
  per-course learned quirks (where readings actually live, which links need login, where pre-class
  questions are posted). Read before planning, written after each run.

### Agent loop (rubric 3): three phases on OpenClaw cron jobs (America/New_York)

Prompts in [`triggers/`](triggers/); job definitions and the Maritime wake trigger in
[`triggers/README.md`](triggers/README.md).

- **19:00 `prep`:** for every class in the next 48h that is not yet podcast-pending/ready/done:
  fetch readings → Drive → notebook + start audio → delegate brief to subagent → validate →
  write prep-log with `notify_at` (24h before class if something is due, else 06:30 day-of).
- **Every 30 min during 19:00–23:00 and 05:30–09:00, `poll`:** advance podcast-pending → ready.
  If any session's `notify_at` has passed and it isn't done, send the notification and mark it done.
- **06:30 `notify`:** a guaranteed morning pass, same logic as the second half of poll. If the
  podcast still isn't ready, send the brief with "podcast pending" and let a later poll send the link.
- **Stop** when every session in the window is done/ready/podcast-pending/needs-human, or after
  3 attempts per session, or after 25 tool calls per session per run.
- **Ask a human** (Telegram question, mark needs-human, move on) when:
  1. a reading is behind a login or returns 403;
  2. the syllabus and Canvas disagree on the reading list;
  3. `NLM_AUTH` persists after one retry;
  4. a pre-class deliverable needs my own answer.
- Every run appends to **`/data/logs/<date>-<trigger>.md`**: trigger, sessions considered, tools
  called, decisions, outcome.

### Subagent (rubric 4): `brief-writer`

Definition: [`workspace/agents/brief-writer.md`](workspace/agents/brief-writer.md).

Input: extracted reading text (cap ~40k tokens, truncated per reading with a note) + session row +
Canvas assignment/page text. **No tools.** Output: strict JSON
`{topic, why_it_matters, key_arguments[], prep_checklist[], pre_class_questions[{question, source, draft_answer}]}`.
The main agent validates it against [`brief.schema.json`](workspace/memory-templates/brief.schema.json).
Every `pre_class_questions[].question` must appear (fuzzy ≥ 0.9) in the Canvas text; otherwise
it is dropped and logged as a hallucination. On schema failure the main agent re-prompts once,
then marks the session partial. The main agent formats the Telegram message, not the subagent.

### Failure recovery (rubric 5)

- **Expired NotebookLM cookie:** `NLM_AUTH` → one retry → session marked `partial` → Drive links
  + brief still sent on schedule → Telegram asks for fresh cookies → the next prep run does only
  the podcast step.
- **Canvas 403:** fall back to the syllabus link if present, else `needs-human`.

### Eval (rubric 6)

Cases table: [`evidence/eval/cases.md`](evidence/eval/cases.md).

- **Baseline** = OpenClaw with no triggers, no memory files, no skills except Canvas, no subagent,
  prompt "prepare me for &lt;course&gt; on &lt;date&gt;".
- **Improved** = full config.
- **5 cases:** (1) 2 Canvas PDFs, nothing due; (2) external links, one behind login;
  (3) pre-class questions due before class; (4) already prepped yesterday, so the agent must stay
  silent; (5) NotebookLM auth broken.
- **Measures:** success, tool calls, wall-clock, human interventions, brief quality 1–5.

---

## Deploy findings (verified 2026-09-24 and 2026-09-27)

The scaffold was written without access to Maritime's docs. These are the answers from the first
deploy. The full runbook is in [`docs/deploy-maritime.md`](docs/deploy-maritime.md).

| # | Question | Answer |
| --- | --- | --- |
| 1 | Persistent volume path | ✅ `/data` is a real volume (it has `lost+found`), and it survived sleep and restart. `HOME=/data`. |
| 2 | Where the workspace lives | ✅ `/data/.openclaw/workspace`. It's unconfirmed whether the template uses `--repo`/`--branch` (the test pointed at `main`, which doesn't have these files yet), so we clone the repo to `/data/syllabi-agent` and run `scripts/install-workspace.sh`. Maritime writes `MARITIME.md` and a block at the top of `AGENTS.md` there, so we merge rather than repoint the workspace. |
| 3 | Trigger timezone | ✅ OpenClaw jobs take `--tz America/New_York`. Maritime CLI triggers are UTC with no prompt, which is fine for an every-30-minutes wake. |
| 4 | Minimum interval | ✅ `*/5` was accepted and fired, so `*/30` is fine. |
| 5 | Concurrency | ✅ Cron runs are serialized. Two jobs due in the same minute ran one after the other (A 20:55:04–20:55:49, B 20:55:57–20:56:42), so `prep` and `poll` can't clobber `prep-log.json`; a long `prep` only delays the next poll. Not covered: a Telegram reply from Chris during a job runs in its own session. |
| 6 | Subagents | ✅ `brief-writer` has zero tools: spawned via `sessions_spawn`, it reported `[]`. It needed two fixes to `config/openclaw.example.json5`: OpenClaw 2026.7.1 wants an `agents.list` array (with `main` marked `default`), not `agents.entries`, and a hand-written deny list left tools behind, so it now uses `deny: ["*"]`. |
| 7 | `canvas` name clash | ✅ With `CANVAS_*` set, the skill is eligible, and "what readings are on Canvas?" went to the canvas **skill** (which reported the missing `scripts/canvas`), not OpenClaw's `canvas` tool. No rename needed. Re-check once the V2 script lands. |
| new | Schedule sync | ❌ Maritime's docs say it mirrors `~/.openclaw/cron/jobs.json`, but OpenClaw 2026.6.1+ stores jobs in SQLite, so nothing is mirrored. Fixed with an explicit `*/30` wake trigger. |
| new | Exec approvals | The model (GPT-5.4) asked Chris to `/approve` network commands although the policy was `security=full, ask=off`. Fixed with the "Command execution (pre-authorized)" section in `AGENTS.md`. |
| new | Telegram from a cron run | A normal reply goes nowhere. Use `maritime-telegram-send` (verified). |
| new | Junk chat replies | Over `maritime chat`, a reply that doesn't come back normally shows up as unrelated text ("node-inspect-debugger") or "No output". It happened while the agent waited for `/approve` and during a `sessions_spawn` run. Workaround: have the agent write results to a file and read it with a second quick message, or use the dashboard chat. |
| new | OpenClaw version | The template runs OpenClaw 2026.7.1. Current OpenClaw docs describe newer schemas (`agents.entries`, which 2026.7.1 rejects). Check the docs at tag `v2026.7.1`. |
| new | Config persistence | `openclaw config patch` changes survive `maritime restart`; Maritime does not regenerate `openclaw.json`. |
| new | Filesystem persistence | On restart Maritime logs "Captured derived image … Edits will survive restart", so installs outside `/data` should persist too. Confirm with the first V2 install. |

Still to check before V2: whether `python3`, `pip`, `notebooklm-py` and `rclone` are available, and
that an install survives a restart.
