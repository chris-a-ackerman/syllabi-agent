<!-- OpenClaw cron job (America/New_York, see README.md): poll — every 30 min 19:30–23:00 and 05:30–09:00 America/New_York (06:30 left to notify). Everything below the line is the exact prompt text. -->
---
[trigger: poll]

Run the **poll** phase from AGENTS.md now. Keep it short: no new prep work.

1. Get the current time in America/New_York. The trigger clock may be UTC.
2. Read memory with `preplog --trigger poll due` (the `preplog` skill; never edit the prep-log by hand). Record each result with `preplog set-podcast` / `set-status`, and each Telegram send with `preplog mark-sent` right after it succeeds. The run log is `preplog --trigger poll runlog`.
3. For each `podcast_pending` entry from `due` (`podcast-pending`, or `notified-partial` whose audio is still in flight): run `nlm-status <notebook_id>`. If the result is `ready`, run `preplog set-podcast <key> --url <link>` (`podcast-pending` → `ready`; a `notified-partial` record stays and now owes only the link). If it is `failed`, set `partial` with `last_error`. If it is `pending`, leave it. On `NLM_AUTH`, retry once, then set `partial` and ask Chris for fresh cookies, unless `history` shows you already asked.
4. Send pass: for each session with `notify_at` ≤ now and no `brief_sent_at`, format and send the Telegram brief from the stored brief, Drive links and the podcast link, or "🎧 podcast pending — link to follow". Set `brief_sent_at`, and set `done` if the podcast link was included. For each session where the brief was sent, `podcast_sent_at` is unset and `podcast_url` is now set, send the podcast link, set `podcast_sent_at` and set `done`.
5. Never send anything twice: the `*_sent_at` fields are the guard. Never write to Canvas.
6. Write the prep-log, then append the run log to `/data/logs/<YYYY-MM-DD>-poll.md`.

If nothing changed, write only the run log and send nothing.
