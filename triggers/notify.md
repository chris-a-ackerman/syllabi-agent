<!-- OpenClaw cron job (America/New_York, see README.md): notify — 06:30 America/New_York daily. Everything below the line is the exact prompt text. -->
---
[trigger: notify]

Run the guaranteed morning **notify** pass from AGENTS.md now. Do not start new prep work.

1. Get the current time in America/New_York. The trigger clock may be UTC.
2. Read memory with `preplog --trigger notify due` (the `preplog` skill; never edit the prep-log by hand). The run log is `preplog --trigger notify runlog`.
3. Send pass: for each session with `notify_at` ≤ now and no `brief_sent_at`, including `partial` sessions, format and send the Telegram brief from the stored brief, Drive links, and the podcast link. If the podcast is not ready, say "🎧 podcast pending — link to follow" and let a later poll send the link. Right after each send, run `preplog mark-sent <key> brief` (add `--podcast-included` if the link was in it: `done`; otherwise the record becomes `notified-partial`).
4. For each `podcast_links` entry from `due` (brief sent, `podcast_url` set, `podcast_sent_at` unset), send only the podcast link, then run `preplog mark-sent <key> podcast` (sets `done`).
5. For a `needs-human` session due today whose question has gone unanswered, send one short reminder, and only if `history` shows no reminder yet.
6. Never send anything twice. Never write to Canvas.
7. Write the prep-log, then append the run log to `/data/logs/<YYYY-MM-DD>-notify.md`.

If nothing is due, write only the run log and send nothing.
