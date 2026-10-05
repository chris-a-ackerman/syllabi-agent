# Schedules

Verified on Maritime (OpenClaw template `ghcr.io/openclaw/openclaw:2026.7.1`) on 2026-09-24.

Scheduling takes **two layers**, because the container sleeps and a sleeping container runs nothing:

| Layer | What | Where it's set | Timezone |
| --- | --- | --- | --- |
| **Wake** | one Maritime cron trigger, `*/30 * * * *` | your laptop: `maritime triggers create` | UTC (doesn't matter: it's every 30 min) |
| **Work** | six OpenClaw cron jobs whose prompts are the files in this folder | inside the agent: `scripts/install-jobs.sh` | **America/New_York** |

At :00 and :30 Maritime wakes the container (a bare wake: no chat message, no model call). OpenClaw's
scheduler then runs whichever job is due. When the agent is already awake, OpenClaw runs the job
without needing the wake. Maritime puts the agent back to sleep on its own when it goes idle.

## Why not just one layer?

- **Maritime triggers can't carry a prompt or a timezone from the CLI.** `maritime triggers create`
  takes only `--type` and `--cron`.
- **Maritime doesn't see OpenClaw's jobs.** Its `MARITIME.md` says it mirrors
  `~/.openclaw/cron/jobs.json` into wake triggers, but OpenClaw moved jobs into its SQLite state
  database (`/data/.openclaw/state/openclaw.sqlite`) in 2026.6.1. There is no `jobs.json`, so nothing
  is mirrored. We tested this: a one-time OpenClaw job did not fire while the agent slept, and ran
  late the next time something woke the agent.
- A 5-minute Maritime trigger (`*/5`) was accepted and fired, so `*/30` is well within the
  minimum interval.

## Setup

**1. Wake trigger** (once, from your laptop):

```
maritime triggers create class-prep-repo --type cron --cron "*/30 * * * *"
maritime triggers list class-prep-repo
```

Delete test triggers with `maritime triggers delete <agent> <trigger-id>` (agent name first).

**2. Jobs** (inside the agent, e.g. from the OpenClaw Dashboard chat):

```
Run: sh /data/syllabi-agent/scripts/install-jobs.sh
```

After editing a prompt file: `git pull`, then `install-jobs.sh --replace`.

## Jobs (America/New_York)

| Job | Cron (ET) | Fires at | Prompt |
| --- | --- | --- | --- |
| `class-prep-prep` | `0 19 * * *` | 19:00 | [`prep.md`](prep.md) |
| `class-prep-notify` | `30 6 * * *` | 06:30 | [`notify.md`](notify.md) |
| `class-prep-poll-a` | `30 5,19 * * *` | 05:30, 19:30 | [`poll.md`](poll.md) |
| `class-prep-poll-b` | `0 6,9,23 * * *` | 06:00, 09:00, 23:00 | [`poll.md`](poll.md) |
| `class-prep-poll-c` | `0,30 7-8,20-22 * * *` | 07:00–08:30, 20:00–22:30 | [`poll.md`](poll.md) |
| `class-prep-forum` | `0 */3 * * *` | 00:00, 03:00, … 21:00 | [`forum.md`](forum.md) |

Poll runs 15 times a day (8 evening, 7 morning). It skips 06:30, which belongs to `notify`, and
starts at 19:30, after `prep`. Poll needs three expressions because one cron line can't express two
half-hour windows that each start and end on a different half hour.

`forum` runs one HW3 agent-forum cycle every 3 hours (timeout 300 s). It is the only job that may
write to Canvas, and only through `forum post`. Its 06:00, 09:00 and 21:00 runs share a minute
with `poll`; cron runs are serialized (see "Overlapping runs"), so it waits its turn instead
of overlapping them. The `*/30` wake trigger already covers every 3-hour mark.

Every job is created with `--tz America/New_York --exact --session isolated --no-deliver`:

- `--tz`: the schedule follows EDT/EST automatically. No DST maintenance.
- `--exact`: OpenClaw otherwise staggers top-of-hour jobs by up to 5 minutes.
- `--session isolated`: a fresh transcript per run. State lives in `/data/memory`.
- `--no-deliver`: the agent sends Telegram itself with `maritime-telegram-send`.

Each prompt file's text below its `---` line is the exact job prompt.

## Overlapping runs (verified 2026-09-27)

Cron runs are serialized. Two one-time jobs due at the same minute, each running a 45 s script,
ran one after the other: A 20:55:04–20:55:49, B 20:55:57–20:56:42 (UTC). So if `prep` is still
running at 19:30, that poll waits for it. The `*_sent_at` guards make sends idempotent anyway.

When testing one-time jobs, note that `--exact` is only valid for cron schedules, not with `--at`.
