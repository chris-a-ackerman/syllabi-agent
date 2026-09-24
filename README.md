# class-prep-agent

An OpenClaw agent, hosted on [Maritime](https://maritime.sh), that gets me ready for each class.
Homework 2 for MIT AI Studio (MAS.665): *Engineer a Reliable Agent*.

Deploying means pointing Maritime's OpenClaw template at this repo, loading `workspace/` as the
agent workspace, and adding the three cron triggers in [`triggers/`](triggers/README.md).

> **Status: V0 scaffold.** The instructions, schemas, trigger prompts and tool contracts are in
> place. The skill scripts (`canvas`, `nlm`, `drive`) are stubs, to be filled in by V2–V4.

---

## Repo map

```
.
├── README.md                     ← this file: the plan (source of truth) + repo map
├── .env.example                  ← every secret/setting, with where it comes from
├── .gitignore                    ← keeps secrets and /data runtime output out of git
├── config/
│   └── openclaw.example.json5    ← sketch of the openclaw.json bits this repo assumes (verify on deploy)
├── workspace/                    ← the OpenClaw agent workspace (Maritime loads this)
│   ├── AGENTS.md                 ← operating instructions: phases, stop rules, ask-a-human, hard rules
│   ├── SOUL.md                   ← persona / tone / boundaries
│   ├── agents/
│   │   └── brief-writer.md       ← subagent definition: role, bounded context, output contract
│   ├── skills/
│   │   ├── canvas/SKILL.md       ← Canvas LMS reads (stub)
│   │   ├── nlm/SKILL.md          ← nlm-prep / nlm-status for NotebookLM (stub)
│   │   └── drive/SKILL.md        ← drive-put via rclone (stub)
│   └── memory/
│       ├── prep-log.schema.json  ← JSON Schema for /data/memory/prep-log.json
│       ├── brief.schema.json     ← JSON Schema for brief-writer output
│       └── course-notes.md       ← template seeded to /data/memory/course-notes.md
├── triggers/
│   ├── README.md                 ← the three Maritime cron triggers, ET → UTC
│   ├── prep.md                   ← exact prompt delivered by the 19:00 trigger
│   ├── poll.md                   ← exact prompt delivered by the 30-min poll triggers
│   └── notify.md                 ← exact prompt delivered by the 06:30 trigger
├── docs/
│   ├── tool-contract.md          ← every tool: name, inputs, outputs, error shape
│   └── hw2-writeup.md            ← HW2 writeup skeleton (one heading per rubric item)
└── evidence/
    ├── eval/cases.md             ← the 5 eval cases, baseline vs improved (results blank)
    └── failures/                 ← screenshots/logs of failures and recoveries
```

At runtime (on the Maritime volume, never in git):

```
/data/
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

- Containers sleep when idle and wake in ~1s on a message/trigger. In-process timers don't fire
  while asleep, so **all scheduling is Maritime cron triggers**, and each one delivers a prompt.
- **Only `/data` persists** across sleep/wake/redeploy. All memory files, logs, secrets
  (NotebookLM cookies, rclone config), downloaded readings and podcasts go under `/data`.
- **30-second chat reply budget.** Long work runs in the background, so podcast generation is
  *started* in one trigger and *polled* by a later one.
- 2 GB RAM / 5 GB SSD base; 100 MB per file transfer.

### Tools (rubric 1)

Full contracts in [`docs/tool-contract.md`](docs/tool-contract.md).

- **`canvas` skill** (or `vishalsachdev/canvas-mcp` as an MCP server if the template supports it):
  `list_modules`, `list_files`, `download_file` → `/data/readings/`, `upcoming_assignments`,
  `get_page`. Reports 401 and 403 as separate errors.
- **syllabi endpoint:** `GET /agent/upcoming?days=3` (sessions, topics, reading links, anything
  due, `canvas_course_id`), `GET /agent/course/:id`. Bearer agent token.
- **`nlm-prep <course> <date> <pdf...>`** creates a notebook, adds sources, *starts* the audio
  overview, and returns `{notebook_id}` immediately.
  **`nlm-status <notebook_id>`** returns `{status: pending|ready|failed, audio_url?}`. When the
  audio is ready, it downloads the mp3 to `/data/podcasts/` and pushes it to Drive.
  Cookies are at `/data/secrets/notebooklm-cookies.json`. Auth failure returns error code `NLM_AUTH`.
- **`drive-put <local_path> <remote_dir>`** returns a share link, via rclone. Config is at
  `/data/rclone/rclone.conf`. Idempotent.
- **Telegram** (Maritime channel) for briefs and questions.

### Memory (rubric 2): read at the start of every run, written at the end

- **`/data/memory/prep-log.json`** (schema: [`workspace/memory/prep-log.schema.json`](workspace/memory/prep-log.schema.json)):
  one record per (course, class_date):
  `readings[] {title, source: canvas_file|external, id_or_url, local_path?}`, `drive_paths[]`,
  `notebook_id?`, `podcast_url?`, `brief?` (JSON from subagent), `notify_at`,
  `status ∈ {pending, podcast-pending, ready, done, partial, needs-human}`, `attempts`,
  `last_error?`, `history[] of {ts, trigger, action}`.
  **Never redo a recorded step. Never start a second podcast for a session that has a `notebook_id`.**
- **`/data/memory/course-notes.md`** (template: [`workspace/memory/course-notes.md`](workspace/memory/course-notes.md)):
  per-course learned quirks (where readings actually live, which links need login, where pre-class
  questions are posted). Read before planning, written after each run.

### Agent loop (rubric 3): three Maritime cron triggers (America/New_York)

Prompts in [`triggers/`](triggers/); UTC cron expressions in [`triggers/README.md`](triggers/README.md).

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
The main agent validates it against [`brief.schema.json`](workspace/memory/brief.schema.json).
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

## Deploy notes and open questions

These points could not be confirmed while writing the scaffold, because docs.openclaw.ai and
maritime.sh were blocked from the build environment. OpenClaw's docs were read from their GitHub
source instead. Maritime was only visible through search snippets. Check each point on first deploy:

1. **Persistent volume path.** The plan assumes `/data`. Maritime search snippets say `/data` is
   the volume root "for most templates", but the OpenClaw guide mentions `/root/.openclaw` as the
   persisted location. If the OpenClaw template does not mount `/data`, symlink `/data` to the
   persisted directory, or change `DATA_DIR` and every `/data/...` path in `workspace/`,
   `triggers/` and `docs/`.
2. **Where Maritime puts the workspace.** OpenClaw defaults to `~/.openclaw/workspace`
   (`OPENCLAW_WORKSPACE_DIR` overrides it). Confirm that Maritime can load this repo's
   `workspace/` there, or copy it there on boot.
3. **Trigger timezone.** It is unknown whether Maritime cron triggers accept a timezone or are
   UTC-only. [`triggers/README.md`](triggers/README.md) gives both, including DST-shifted UTC sets.
4. **Cron minimum interval and trigger count.** The 30-minute poll needs sub-hourly cron and
   several trigger entries.
5. **Concurrency.** Can two triggers (e.g. poll and notify) run at the same time on one agent?
   The poll schedule skips 06:30 to avoid that, and prep-log status checks make sends idempotent.
6. **Subagents.** OpenClaw has no file-based subagent registry. `brief-writer` is a
   `sessions_spawn` target: an `agents.entries.brief-writer` entry whose tools are all denied
   (see `config/openclaw.example.json5`). The fallback is the bundled `llm-task` tool with
   `brief.schema.json`.
7. **Name clash.** OpenClaw has a built-in tool called `canvas`, which is a UI canvas unrelated to
   Canvas LMS. The skill here is `canvas`. If the model confuses the two, rename it to `canvas-lms`.
