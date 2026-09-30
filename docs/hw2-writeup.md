# HW2: Engineer a Reliable Agent (class-prep-agent)

MIT AI Studio (MAS.665) · Chris Ackerman · Repo: `chris-a-ackerman/syllabi-agent`
Plan of record and full design: [`README.md`](../README.md). Deploy runbook and verification
log: [`docs/deploy-maritime.md`](deploy-maritime.md).

> **Status of the live eval (2026-09-30, written under deadline).** The full planned run (case 1
> through a podcast, case 5 with NotebookLM auth broken) did **not** fit in the time left and was
> not run. What is reported below as *observed* comes from the agent's real scheduled runs on
> 9/29–9/30 and from smoke tests on 9/30. Everything marked *designed* is built and unit-tested
> but was not seen working end to end live. Raw evidence: [`evidence/run-2026-09-30/`](../evidence/run-2026-09-30/).

## Overview

Before each class I used to do five things by hand: check what's being read, download the
readings, put them on my iPad, make a NotebookLM podcast to listen to on the way in, and draft
answers to any pre-class questions. class-prep-agent does all of that except the Goodnotes import.
For each upcoming class it pulls the schedule from my syllabi app, finds the readings on Canvas,
files them in a Google Drive folder my iPad can see, starts a NotebookLM audio overview, has a
subagent draft a brief and the pre-class answers, and sends me one Telegram message with the
brief, the Drive links and the podcast link.

It is an OpenClaw agent hosted on Maritime: a container that sleeps between runs and keeps its
state on a persistent `/data` volume. The work is split into three phases (**prep**, **poll**,
**notify**) because NotebookLM takes minutes to generate audio and Maritime gives a chat reply
about 30 seconds. The three phases run as OpenClaw cron jobs whose prompts are `triggers/*.md`.
They were installed and firing unattended on 9/29–9/30 (§3), and those real runs are most of
the evidence below.

**Timing rule:** the brief goes out 24 hours before class if something is due before class,
otherwise at 06:30 on class day.

## Configuration at a glance

| Choice | Setting |
| --- | --- |
| Harness | OpenClaw (`ghcr.io/openclaw/openclaw:2026.7.1`) on Maritime, agent `class-prep-repo`; persistent `/data` volume |
| Model | GPT-5.4 through Maritime's LLM proxy (the template's default) |
| Instructions | `workspace/AGENTS.md` (phases, hard rules, stop and ask rules; ~31k chars, so `agents.defaults.bootstrapMaxChars` = 40000), `SOUL.md`, and one prompt per phase in `triggers/` |
| Skills / tools | `syllabi`, `canvas` (read-only), `nlm`, `drive`, `fetch-reading`, `pdf-text`, `brief`, `preplog`, and Maritime's `maritime-telegram-send` (§1) |
| Memory / state | `/data/memory/prep-log.json` (per-class state machine) + `course-notes.md`, only through `preplog`; run logs in `/data/logs/` (§2) |
| Permissions | exec approvals off (`security=full, ask=off`) with an explicit allow-list in AGENTS.md; no Canvas writes; `drive.file` scope on a dedicated Google account; subagent `deny: ["*"]` |
| Stopping | status `done`/`ready`/`podcast-pending`/`needs-human`; 3 attempts; 25 tool calls per session per run |
| Escalation | one Telegram question, then `needs-human`, for 4 cases: gated reading, syllabus/Canvas conflict, `NLM_AUTH` after retry, a deliverable needing Chris's own answer |
| Scheduling | Maritime wake trigger every 30 min + 5 OpenClaw cron jobs (ET) |

## Architecture

```
 Maritime wake (*/30)          operator chat (Run: …)       Telegram (Chris)
          │                            │                          ▲
          ▼                            ▼                          │ maritime-telegram-send
 OpenClaw cron: prep 19:00 · poll every 30 min · notify 06:30     │
          │                                                        │
          ▼                                                        │
 ┌──────────────── main agent (GPT-5.4, AGENTS.md) ───────────────┴──┐
 │ 1 read memory ─► 2 plan per session ─► 3 tools ─► 4 write memory+log│
 └───┬───────────────┬──────────────────────┬───────────────────┬──────┘
     │               │                      │                   │
     ▼               ▼                      ▼                   ▼
 preplog ⇄ /data/memory   syllabi · canvas · fetch-reading   nlm-prep / nlm-status   brief bundle
 (prep-log.json,          pdf-text · drive-put ─► Google     ─► NotebookLM            │ sessions_spawn
  course-notes.md)        Drive ClassPrep/                                            ▼
 /data/logs/*.md                                                        brief-writer subagent
                                                                        (no tools, JSON out)
                                                                              │
                                                              brief validate (schema + fuzzy ≥ 0.9)
                                                                              ▼
                                                               preplog set-brief ─► send pass
```

## 1. Tools

The agent uses five tools, each a small command-line script in `workspace/skills/` with a
`SKILL.md` telling the agent when and how to call it. Every one returns a single JSON object,
either `{"ok": true, ...}` or `{"ok": false, "error": {code, message, retryable}}`, so the agent
branches on a stable error code instead of guessing from error text. Full contracts:
[`docs/tool-contract.md`](tool-contract.md).

| Tool | What it does | Why it exists |
| --- | --- | --- |
| `syllabi` | Calls my syllabi app's `agent-upcoming` endpoint with a read-only, revocable agent token. Returns each class meeting in the next 48 h with its key (`MAS.665@2026-09-29`), start time, Canvas course id, what's due before class, and when to notify. | The schedule of record. I built the endpoint and the token card in the app for this (SYL-92, SYL-104). |
| `canvas` | Read-only Canvas REST: modules, files, download, assignments, pages. | Where the readings and pre-class questions actually live. GET only: the Canvas token can't be scoped, so the tool itself cannot submit. |
| `nlm` (`nlm-prep`, `nlm-status`) | Wraps the unofficial `notebooklm-py` CLI: create notebook, add PDFs, *start* audio, and later check status and download the mp3. | There is no NotebookLM API. Start and check are separate calls so nothing blocks for minutes. |
| `drive` (`drive-put`) | Uploads a file to `ClassPrep/Readings/<course>/<date>/` with rclone and returns its Drive URL. Re-uploading the same file is a no-op. | Replaces iCloud, which has no server API. The iPad Files app and Goodnotes both read Drive. |
| Telegram (`maritime-telegram-send`) | Sends me the brief and any questions. | How I actually get the output. |

Supporting helpers: `pdf-text` (PDF → text for the subagent), `fetch-reading` (the only way to
download an external reading link, with size cap, no credentials, and every host logged),
`preplog` (memory, §2) and `brief` (subagent plumbing, §4).

The error codes are specific enough to act on. For example, Canvas 401 (`CANVAS_401`, the token
is dead, so ask me once) and Canvas 403 (`CANVAS_403`, this one file is locked, so fall back to
the syllabus link or ask for the PDF) lead to different actions. Real calls from the 9/30 smoke test (operator chat, `Run:` lines):

```
syllabi check      → {"ok": true, "timezone": "America/New_York", "courses": 6, "sessions": 3, "events": 4, "endpoint": "/agent-upcoming"}
canvas.py whoami   → {"ok": true, "user": {"id": 195836, "name": "Chris Ackerman"}}
nlm.py check       → {"status": "ok", "notebooklm_home": "/data/notebooklm"}
drive.py check     → {"ok": true, "root": "gdrive:ClassPrep", "root_exists": true, "entries": 2, ...}
```

Tests: 523 unit tests across the skills, no network (`python3 -m unittest discover -s tests`).

## 2. Memory

The agent keeps nothing in its head between runs. What's in `/data/memory/` is what it knows,
and it reads that before planning and writes it before finishing, even when the run fails.

- **`prep-log.json`**: one record per class (`<course>@<date>`): readings found, Drive paths,
  NotebookLM notebook id, podcast URL, the brief, when to notify, status, attempt count, last
  error, and a history of every action. Status moves through a small state machine:
  `pending → podcast-pending → ready → done`, with `partial`, `notified-partial` and
  `needs-human` for the cases where something is missing.
- **`course-notes.md`**: what the agent has learned per course (where readings actually live,
  which links need a login, where pre-class questions are posted). Read before looking anything
  up, updated after each run.

All reads and writes go through the `preplog` tool instead of the model editing JSON by hand. It
validates every write against the schema, writes atomically, and enforces the rules that matter
most:

- **Never redo a recorded step.** `preplog begin` returns only the steps a session still needs.
- **Never start a second podcast** (`ALREADY_HAS_NOTEBOOK`). This matters because NotebookLM has
  a daily audio quota.
- **Never send the same message twice** (`ALREADY_SENT`).
- **Three attempts, then ask me** (`MAX_ATTEMPTS` → `needs-human`).

Evidence (observed, from the real scheduled runs of 9/29–9/30):
- **The prep-log record for 15.662 on 9/30** (key `15.662-/-11.383@2026-09-30`: the syllabi app's
  course code is "15.662 / 11.383", so the key isn't `15.662@…`). It shows memory working as a
  record of what happened, and it also shows a bug (§5):
  ```
  "status": "notified-partial", "attempts": 1, "readings": [], "drive_paths": [],
  "history": [
    {"ts": "2026-09-29T19:01:03-04:00", "trigger": "prep", "action": "record created"},
    {"ts": "2026-09-29T19:01:07-04:00", "trigger": "prep", "action": "run started (attempt 1)"},
    {"ts": "2026-09-29T22:31:51-04:00", "trigger": "poll", "action": "brief sent without podcast link", "detail": "podcast pending"}],
  "notify_at": "2026-09-29T19:00:29-04:00", "brief_sent_at": "2026-09-29T22:31:51-04:00"
  ```
- **course-notes.md:** unchanged since 9/25 (no run got far enough to learn anything). No diff to show.
- **Memory stops re-work (the case 4 behaviour, observed):** three later prep runs on 9/30
  (08:01, 08:42, 08:48 ET) each read memory, saw every session was already `notified-partial`,
  and stopped: 3 tool calls each (`preplog init`, `preplog list`, `syllabi upcoming`), **no
  Canvas/NLM/Drive calls and no Telegram message**:
  ```
  ## 2026-09-30T08:42:31-04:00 — prep
  - Sessions considered: 15.662-/-11.383@2026-09-30 notified-partial → notified-partial; 15.385@… ; 15.387@…
  - Tools called: preplog init ok, preplog list ok, syllabi upcoming ok
  - Decisions: Skipped prep: all upcoming sessions are already notified-partial, and AGENTS.md forbids further prep work on notified-partial records.
  - Outcome: nothing to do
  ```
  Caveat: the state it was protecting was itself incomplete (see §5), so here "never redo" also
  meant "never repair".

## 3. Agent loop

Each phase is a prompt in `triggers/` that tells the agent its goal for that pass. The rules for
all three live in [`workspace/AGENTS.md`](../workspace/AGENTS.md).

1. **prep**: for every class in the next 48 h that isn't already in progress or done: read
   memory → find readings (Canvas + syllabus links) → download → upload to Drive → *start* the
   podcast → have the subagent write the brief → validate it → save the record.
2. **poll**: check any podcast still generating; when it's ready, download it, upload it to
   Drive and record the link. Then run the send pass.
3. **notify / send pass**: for every class whose notify time has passed, format and send the
   brief (with the podcast link, or "podcast pending, link to follow"), then mark it sent. If
   the podcast finishes later, send the link as a short follow-up.

**Why split it this way:** Maritime gives a chat reply about 30 seconds and a shell command
60 seconds, and a NotebookLM podcast takes several minutes. So podcast generation is started in
one pass and checked in a later one, and every pass has to be safe to run again. That is what
the memory rules in §2 guarantee.

**Stopping conditions** (quoted from `AGENTS.md`): stop on a session when its status is `done`,
`ready`, `podcast-pending` or `needs-human`; after 3 attempts; or after 25 tool calls for that
session in one run. When nothing needs doing, the agent does nothing and sends nothing.

**Ask-a-human conditions** (the only four reasons it messages me with a question):
1. a reading is behind a login or returns 403;
2. the syllabus and Canvas disagree on the reading list;
3. NotebookLM auth still fails after one retry;
4. a pre-class deliverable needs my own answer (it drafts; I submit).

It asks once, marks the session `needs-human`, and moves on to the next class instead of waiting.

**Observability:** every run appends a block to `/data/logs/<date>-<phase>.md`: sessions
considered (status before → after), tools called and their result codes, decisions (every skip,
retry, ask, and any dropped hallucinated question), and the outcome.

**Observed run logs** (full text: [`evidence/run-2026-09-30/operator-transcript.md`](../evidence/run-2026-09-30/operator-transcript.md)).
The OpenClaw jobs were in fact installed and firing on 9/29–9/30 (`openclaw cron list` shows
all five `class-prep-*` jobs; poll-b last ran "3h ago"), so the logs below are real unattended
runs, not hand-triggered ones. Poll ran every half hour in both windows and correctly did nothing
when nothing was due, for example:

```
## 2026-09-29T20:30:44-04:00 — poll
- Sessions considered: 15.385@2026-09-30 pending → pending; 15.387@2026-09-30 pending → pending; 15.662-/-11.383@2026-09-30 pending → pending
- Tools called: preplog init → ok; preplog due → ok
- Decisions: No podcast-pending records to poll. Send pass skipped for all three due sessions because no stored brief or Drive links exist, and this poll run was restricted to no new prep work.
- Outcome: nothing sent; no state changes
```

The 9/29 19:00 **prep** run has no run-log block at all: it created the three records and
started attempt 1, then stopped before finding readings or writing its log (§5).

**Clock note:** by the time of this eval (11:30 ET on 9/30) 15.662 had already met at 10:00, so
`syllabi upcoming` no longer listed it. `syllabi upcoming --now 2026-09-29T19:00:00-04:00` (a
simulated clock at the night-before prep) does list it, with `has_due_before_class: true`
(the app counts class participation as "due"), so `notify_at` = now, not 06:30.

**Scheduling (installed; ran unattended 9/29–9/30):** a sleeping Maritime container runs nothing, and
Maritime's CLI triggers can't carry a prompt or a timezone. The design is one Maritime wake
trigger every 30 minutes plus OpenClaw cron jobs in Eastern time that hold the prompts
([`triggers/README.md`](../triggers/README.md)). I tested both halves on 9/24: a job with no wake
trigger silently didn't fire while the agent slept, and with the wake trigger it ran on time.

## 4. Subagent

**`brief-writer`** turns one class's readings and Canvas text into a structured brief:
`{topic, why_it_matters, key_arguments[], prep_checklist[], pre_class_questions[{question,
source, draft_answer}]}`. Definition:
[`workspace/agents/brief-writer.md`](../workspace/agents/brief-writer.md).

The handoff is deliberately narrow:
- **Bounded input.** The main agent builds one bundle capped at ~40k tokens: the session row, the
  Canvas assignment and page text verbatim, and the reading text split evenly, with any cut
  reading marked `[TRUNCATED: kept first N of M characters]`.
- **No tools.** It's spawned as a separate agent with every tool denied (`deny: ["*"]`). I
  verified on Maritime that it reported zero tools; an earlier hand-written deny list left some
  behind.
- **Strict output, checked by the main agent**, not trusted:
  1. It must match `brief.schema.json`. If it doesn't, the agent re-prompts once with the errors;
     a second failure marks the session `partial`.
  2. Every pre-class question must match a sentence in the Canvas text (fuzzy ratio ≥ 0.9, with
     numbers, negations and content words required to match exactly). Anything else is dropped
     and logged as `HALLUCINATION: <question>`. This stops it inventing questions the professor
     never asked.
  3. The main agent, not the subagent, formats and sends the Telegram message.

Why a subagent at all: the readings are long and the main agent's context is full of tool
output and rules. A fresh context with only the source material writes a better brief, and
having no tools means untrusted reading text can't make it do anything.

Evidence:
- **Not observed live.** No live run reached the brief step: the 9/29 prep stopped before
  finding readings, and the planned 9/30 eval run didn't fit before the deadline. So there is no
  live `brief-input.md`, `brief-output.json` or `HALLUCINATION:` line to show. What *is*
  verified live is the isolation: on Maritime the spawned `brief-writer` reported `[]` tools
  (`docs/deploy-maritime.md`, 9/27).
- Unit tests include planted near-miss questions and forged section headers in the bundle
  (`tests/test_brief.py`).

## 5. Failure recovery

**Main test: NotebookLM auth broken.** NotebookLM is the most fragile piece (unofficial API,
session-based login), so it's the failure I tested end to end. I pointed `NOTEBOOKLM_HOME` at an
empty folder, which makes the auth check return exactly `{"error": "NLM_AUTH"}`, and ran prep
for a class.

Expected, per `AGENTS.md`: `NLM_AUTH` → one retry → session marked `partial` with `last_error`
→ readings still uploaded and brief still written → brief and Drive links still sent at the
notify time → one Telegram message asking me to redo the NotebookLM login. After I restore the
login, the next prep does **only** the podcast step.

**Result: not run.** It didn't fit in the time left before the deadline. The pieces are
verified separately (an empty `NOTEBOOKLM_HOME` makes `nlm.py check` return exactly
`{"error": "NLM_AUTH"}`, 9/29), but the end-to-end recovery (`partial`, the Telegram ask, the
podcast-only re-run) is **designed, not observed**.

**Failures observed in the real scheduled runs (9/29–9/30).** These are the honest results of the
live deployment, and they matter more than the planned test:

1. **The 19:00 prep run died partway and left no log.** On 9/29 it created the three records for
   9/30 and started attempt 1 at 19:01, then stopped: no readings, no Drive upload, no podcast,
   no brief, and **no run-log block** (there is no `2026-09-29-prep.md`). The likely cause is that
   `AGENTS.md` (31k chars) was cut at OpenClaw's 20k-char bootstrap limit, so the isolated cron
   session was missing the later phase rules. `agents.defaults.bootstrapMaxChars` is now 40000
   (checked on 9/30), but that fix was applied after this run. *Likely, not proven:* the run's
   transcript wasn't captured.
2. **A poll sent an empty "brief".** At 22:31 on 9/29 a poll's send pass marked all three
   sessions as sent (`"brief sent without podcast link"`) even though none had a brief
   (`has_brief: false`, no Drive links). AGENTS.md says to skip only `needs-human` with no brief
   and no Drive links, so a `pending` record with nothing in it was still "sent". That poll
   also wrote no run-log block (the 9/29 log jumps from 21:00 to 23:00). Earlier polls (20:00–21:00)
   had correctly declined to send in the same state, so the model's behavior differed from run
   to run.
3. **Memory then locked in the bad state.** Once `brief_sent_at` was set, the record was
   `notified-partial`, which prep must never touch. The next morning, three prep runs correctly
   skipped all three sessions, and 15.662 was never prepared. The never-redo rule worked as
   designed and made the gap permanent. *Fix to make:* the send pass should refuse to mark a
   brief sent when the record has no brief and no Drive links (put the guard in `preplog
   mark-sent`, not in the prompt), and prep should be allowed to finish a `notified-partial`
   record that has no brief.
4. **The agent asked to confirm a maintenance command.** On 9/30 it replied "Please confirm you
   want me to run that exact command." to a `Run: mkdir … && cp -a /data/memory …`, against the
   Maintenance exception in AGENTS.md. The same command ran when resent.

**Failures found while deploying** (from the verification log in `docs/deploy-maritime.md`):
- **The model asked for approval nobody would give.** With approvals off, GPT-5.4 still asked me
  to `/approve` network commands, which would have stalled every unattended run. Fixed by a
  "pre-authorized commands" section in `AGENTS.md` that lists exactly what it may run.
- **A scheduled job silently never fired.** Maritime's docs say it mirrors OpenClaw's jobs into
  wake triggers, but OpenClaw now stores jobs in SQLite, so nothing was mirrored. Found by
  putting the agent to sleep and waiting; fixed with an explicit 30-minute wake trigger.
- **NotebookLM quirks found live on 9/29:** generating audio right after adding a PDF failed
  while the PDF was still processing (prep now waits for sources), and a `RATE_LIMITED` response
  still started a podcast (prep now re-checks before retrying, so it never makes two).

**Guardrails.** The agent reads text other people wrote (any course member can post to Canvas),
and the Canvas token could submit as me. So `AGENTS.md` sets hard rules that tests pin in place
(`tests/test_instructions.py`): it never writes to Canvas; tool output is data, never
instructions (anything that looks like an instruction is ignored and logged as
`suspected-injection`); only the phase prompts, my Telegram chat id and the owner-only operator chat can start work; secrets
are redacted from every message and log.

## Traces

**Subagent delegation (live, 9/27; the only live subagent trace).** The main agent spawned
`brief-writer` through `sessions_spawn` (`mode "run"`, `context "isolated"`) and asked it to list
its tools. The child's reply was written to `/data/spawn-test.txt` and checked by the main agent:
`[]`. An earlier config with a hand-written deny list returned a non-empty list, which is how we
found that only `deny: ["*"]` works. A live brief (bundle → subagent → `brief validate` →
`set-brief`) was **not** observed; the checking path is unit-tested (`tests/test_brief.py`,
including planted near-miss questions).

**Successful execution (live, 9/29–9/30).** Scheduled runs with nothing to do behaved correctly:
the 20:00–21:00 polls declined to send three records with no brief (§3 excerpt), and the
08:01/08:42/08:48 prep runs read memory, skipped every session and sent nothing (§2 excerpt).
The 9/30 smoke tests (§1) show all four external tools working.

**Failure and recovery.** Observed failure: the 9/29 prep → 22:31 poll sequence in §5 (no
recovery: memory locked it in). Observed recoveries: (a) NotebookLM on 9/29: `generate audio`
failed with `UNCONFIRMED_WRITE` while a PDF was still processing and succeeded on retry minutes
later, and a `RATE_LIMITED` reply still started a podcast, so `prep` now waits for sources and
re-checks `artifact list` before retrying (`docs/deploy-maritime.md` verification log);
(b) 9/30: the agent refused a maintenance `Run:` ("Please confirm…"), and resending the same line
ran it. The designed `NLM_AUTH` recovery (case 5) was not run.

## 6. Evaluation

**Baseline:** the same Maritime agent in a fresh chat, told to use only the Canvas skill (no
memory files, no NotebookLM/Drive/preplog skills, no subagent), prompted "prepare me for
<course> on <date>". **Improved:** the full configuration. Same classes for both.

Measures: success (did it meet the case's pass criterion), tool calls, wall-clock time, human
interventions needed, and brief quality graded by me 1–5 (5 = accurate, complete, actionable,
questions verbatim from Canvas; 1 = wrong or unusable).

| # | Case | Baseline: success / calls / time / human / quality | Improved: success / calls / time / human / quality |
| --- | --- | --- | --- |
| 1 | 15.662 on 9/30 (real scheduled run, not the planned eval run) | **Partial** / 10 by its own count (its itemised list adds up to 15) / 3 min 20 s (11:38:02 → 11:41:22 ET, chat round-trip) / 0 / **3** | **N** / unknown (prep left no log) / 19:01 → 22:31 ET on 9/29 to a message with no brief / 0 / **1** |
| 4 | Already handled (should stay silent) | Not run (no time). By design it has no memory, so a second prompt would redo all the work | **Y** / 3 per run (`preplog init`, `preplog list`, `syllabi upcoming`) / not measured / 0 / n/a |
| 5 | NotebookLM auth broken | Not applicable: the baseline has no NotebookLM step, so it can't fail or recover | **Not run** (no time before the deadline) |

Quality scores are **graded by Claude Code; Chris to confirm.** Improved case 1 gets 1 because
nothing usable was delivered. Improved case 4 is the three 9/30 morning prep runs; its setup
was `notified-partial`, not `done`, and it passed only in the narrow sense (see §5). Tool calls
are counted from the "Tools called" lines of the run logs.

Full case definitions, including the ones not run: [`evidence/eval/cases.md`](../evidence/eval/cases.md).

**Not run, and why:** case 2 (external reading behind a login), case 3 (pre-class questions due
the night before) and optional case 6 (prompt injection in Canvas text) need specific course
content that wasn't available in the window before the deadline. The logic for each is in
`AGENTS.md` and covered by unit tests, but I don't claim live results for them.

**Discussion:** The results don't match the shape I expected. The improved agent's memory and
stop rules did what they were built to do: runs with nothing to do stayed silent and cheap
(2–3 tool calls, no messages), and nothing was ever done twice. But the one real prep cycle
failed: prep stopped partway without a log, a later poll marked an empty brief as sent, and the
never-redo rule then kept anything from repairing it. So on case 1 it delivered nothing useful.
The baseline, run in a fresh chat with only Canvas, did better on case 1 than the improved
agent's real run. It found the three readings and the four discussion questions in the Canvas
assignment and wrote a sensible plan in 3 min 20 s. But it couldn't extract the slide text, filed
nothing to Drive, made no podcast and sent nothing, and it has no memory for case 4. The lesson is that the guards have to live in the tool, not the prompt:
the prompt-level rule "don't send without a brief" was followed by some polls and not others,
while the rules `preplog` enforces in code held every time.

**Limits of this eval:** one run per case, graded by me, on a baseline that is the same agent
with tools switched off rather than a separately deployed agent. It shows the design choices
matter; it isn't a statistically strong comparison.

## Reproducing the demonstration

1. Deploy: follow [`docs/deploy-maritime.md`](deploy-maritime.md) §1–§7 (create the OpenClaw agent,
   clone this repo to `/data/syllabi-agent`, `install-workspace.sh`, connect Telegram, the wake
   trigger + `install-jobs.sh`, the config patch, then the nlm/rclone/Canvas/syllabi secrets).
2. Smoke-test (terminal): `maritime chat class-prep-repo "Run: python3 /data/syllabi-agent/workspace/skills/<skill>/scripts/<skill>.py check"`
   for `nlm` and `drive`, `canvas.py whoami`, and `skills/syllabi/scripts/syllabi check`.
3. Run a phase: wait for the cron job, or send the text below the `---` line of
   `triggers/prep.md` (then `poll.md`, `notify.md`) with `maritime chat`, telling the agent to
   write its summary to a file under `/data/work/`. Read the file with a second `Run: cat` message,
   because long replies come back as junk.
4. Inspect: `Run: python3 /data/syllabi-agent/workspace/skills/preplog/scripts/preplog.py get <key>`
   and `Run: cat /data/logs/<date>-<phase>.md`.
5. Baseline: send the prompt in [`evidence/eval/baseline/`](../evidence/eval/baseline/) in a fresh chat.
6. Case 5: `maritime env set class-prep-repo NOTEBOOKLM_HOME=/data/notebooklm-broken --reload`,
   run prep, then set it back to `/data/notebooklm`.

## What's still manual

- **Goodnotes import:** one tap per reading. Goodnotes has no server API; the agent files PDFs in
  Drive and the iPad Files app does the rest.
- **Readings behind logins** (HBS cases, library proxy): I download them and drop them in the
  Drive folder when asked.
- **Submitting pre-class questions:** I review the drafts and submit them. The agent never
  writes to Canvas.
- **Refreshing the NotebookLM login** when it expires (unofficial API).
- **Re-authorizing rclone / Canvas tokens** when they expire or are revoked.
- **Watching the runs:** nothing alerts me when a run dies without a log (§5, failure 1).

## Limitations and future work

- **Fix the §5 failures:** a `mark-sent` guard against empty briefs, a way for prep to finish a
  `notified-partial` record with no brief, and an alert when a run ends without a log. Then
  watch a week of real evening → morning cycles.
- **Run the remaining eval cases** (2, 3, 6) against real course content, and repeat each case
  more than once.
- **Readings in syllabi:** the app stores class meetings and deadlines but not reading links, so
  Canvas is the only reading source today. Adding readings to the app would let the agent
  cross-check the two (ask-a-human case 2 exists for that).
- **NotebookLM dependence:** it's an unofficial, cookie/master-token API that can break without
  notice. A fallback (text-to-speech over the brief) would keep the audio step alive.
- **The Canvas token** can't be scoped. The tool is read-only by construction and the rules
  forbid writes, but a scoped token or an LTI integration would remove the risk rather than
  guard against it.
