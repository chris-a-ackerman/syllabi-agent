<!-- OpenClaw cron job (America/New_York, see README.md): prep — 19:00 America/New_York daily. Everything below the line is the exact prompt text. -->
---
[trigger: prep]

Run the **prep** phase from AGENTS.md now.

1. Get the current time in America/New_York. The trigger clock may be UTC.
2. Read `/data/memory/prep-log.json` and `/data/memory/course-notes.md`. Create or seed them if missing.
3. Call the syllabi endpoint `GET /agent/upcoming?days=3`. Consider every class starting in the next 48 hours.
4. For each class whose record is not `podcast-pending`, `ready` or `done`: find readings (syllabus + Canvas) → download to `/data/readings/<course>/<date>/` → `drive-put` to `Readings/<course>/<date>/` → `nlm-prep <course> <date> <pdf...> --topic "<session topic>"` if there is no `notebook_id` (never a second podcast; record `notebook_id` and `task_id`) → spawn `brief-writer` if there is no brief, then validate it → set `notify_at`. For a `partial` record whose only failure is `NLM_AUTH`, do only the podcast step.
5. Skip any step the prep-log already records. Respect the limits: 3 attempts per session, 25 tool calls per session this run.
6. For a login-walled or 403 reading, a syllabus/Canvas reading-list conflict, `NLM_AUTH` after one retry, or a deliverable that needs Chris's own answer: send one Telegram question, mark the session `needs-human`, and move on.
7. Do not send briefs in this phase. Only questions to Chris. Never write to Canvas.
8. Write the prep-log and course-notes, then append the run log to `/data/logs/<YYYY-MM-DD>-prep.md`.

If there is nothing to do, write only the run log and send nothing.
