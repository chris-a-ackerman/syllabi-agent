<!-- OpenClaw cron job (America/New_York, see README.md): notify — 06:30 America/New_York daily. Everything below the line is the exact prompt text. -->
---
[trigger: notify]

Run the guaranteed morning **notify** pass from AGENTS.md now. Do not start new prep work.

1. Get the current time in America/New_York. The trigger clock may be UTC.
2. Read `/data/memory/prep-log.json`.
3. Send pass: for each session with `notify_at` ≤ now and no `brief_sent_at`, including `partial` sessions, format and send the Telegram brief from the stored brief, Drive links, and the podcast link. If the podcast is not ready, say "🎧 podcast pending — link to follow" and let a later poll send the link. Set `brief_sent_at`, and set `done` if the podcast link was included.
4. For each session where the brief was sent, `podcast_sent_at` is unset and `podcast_url` is set, send the podcast link, set `podcast_sent_at` and set `done`.
5. For a `needs-human` session due today whose question has gone unanswered, send one short reminder, and only if `history` shows no reminder yet.
6. Never send anything twice. Never write to Canvas.
7. Write the prep-log, then append the run log to `/data/logs/<YYYY-MM-DD>-notify.md`.

If nothing is due, write only the run log and send nothing.
