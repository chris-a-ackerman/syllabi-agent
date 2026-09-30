# Live evidence, 2026-09-30

Collected by Claude Code over `maritime chat class-prep-repo` (operator chat, `Run:` lines)
between 11:31 and ~11:45 ET on 9/30. The planned full eval run (case 1 through a podcast, case 5
with NotebookLM auth broken) was **not** run: it didn't fit before the deadline.

- [`operator-transcript.md`](operator-transcript.md): every message sent and every reply, with
  ET timestamps. It contains:
  - the Step 0 smoke tests (`syllabi check`, `canvas whoami`, `nlm.py check`, `drive.py check`,
    a Telegram test send): all ok;
  - `syllabi upcoming` for real now (15.662 not listed: class started 10:00) and with
    `--now 2026-09-29T19:00:00-04:00` (listed as `15.662-/-11.383@2026-09-30`);
  - `preplog get 15.662-/-11.383@2026-09-30` and `preplog list` (the record the 9/29 scheduled
    runs left behind);
  - every run log from `/data/logs/2026-09-29-*.md` and `/data/logs/2026-09-30-*.md`
    (the 9/29 prep run left none);
  - `openclaw cron list` (all five jobs installed) and `bootstrapMaxChars` = 40000;
  - the agent's "Please confirm…" reply to a maintenance `Run:` line.
- A backup of `/data/memory` as it was before this session is on the volume at
  `/data/work/eval/memory-before/`. Nothing in `/data/memory` was changed.
- The baseline answer is in [`../eval/baseline/`](../eval/baseline/).
