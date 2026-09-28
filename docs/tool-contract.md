# Tool contract

Every tool the agent can call: name, inputs, outputs, error shape. The skill stubs in
`workspace/skills/*/SKILL.md` restate their part of this for the agent. **This file is the
source of truth.** If the two disagree, fix the skill.

## Common envelope

Every skill command prints exactly **one JSON object** to stdout.

```jsonc
// success: exit code 0
{ "ok": true, /* tool-specific fields */ }

// failure: exit code 2 (1 is reserved for crashes/unhandled exceptions)
{
  "ok": false,
  "error": {
    "code": "CANVAS_403",          // stable, UPPER_SNAKE; the agent branches on this
    "message": "human-readable",   // for logs; never contains secrets
    "retryable": false,            // may the agent retry once within this run?
    "status": 403,                 // optional: upstream HTTP status
    "detail": {}                   // optional: tool-specific context (ids, urls)
  }
}
```

Rules for every tool:
- Every path it takes or returns is under `/data/`.
- It never prints tokens, cookies or config contents, including in `message`.
- Write operations (download, upload) are idempotent: rerunning one returns the same result
  and does not duplicate the work.
- A timeout counts as a retryable network error, never a hang: `CANVAS_NET` for canvas; other
  tools use the network code in their own table below. Every API call has a 20 s timeout, inside
  the 30-second reply budget. File transfers are capped by Maritime's 60 s per-command exec limit:
  `canvas download` has a 50 s **total** deadline (all redirect hops plus the body), not a
  per-socket timeout, and returns `CANVAS_NET` (`retryable: true`) when it runs out. The only long
  job (audio generation) runs asynchronously.

---

## 1. `syllabi` skill (the syllabi app's agent endpoint, read-only)

Implemented as `{baseDir}/scripts/syllabi <command> …` (`syllabi.py`), SYL-105. The app side is the
Supabase edge function `agent-upcoming` (syllabi repo, SYL-92/SYL-104): it accepts a scoped,
revocable agent token minted in the app's **Settings → Agent access** card and returns the
caller's active semester as `{timezone, courses[], sessions[], events[]}` for the next `days`
days (0–14). There is no separate course endpoint; `syllabi course` filters the same payload.

Auth: `Authorization: Bearer $SYLLABI_AGENT_TOKEN` (`syl_agent_…`, scope `read:upcoming`).
Base: `$SYLLABI_BASE_URL` = `https://<ref>.supabase.co/functions/v1`; the request is
`GET <base>/agent-upcoming?days=N` (`SYLLABI_UPCOMING_PATH` overrides the path; the plan's
`/agent/upcoming` spelling is not what Supabase serves). `SYLLABI_ANON_KEY`, if set, is sent as
`apikey`. Optional `SYLLABI_TIMEOUT` (default 20 s).

| Command | Inputs | Output (`ok: true` plus) |
| --- | --- | --- |
| `upcoming` | `--days N` (0–14, default 3), `--within-hours H` (prep: 48), `--now ISO` (eval runs only) | `timezone, now, days, within_hours, sessions: [{key, course, course_code, course_id, course_name, canvas_course_id, class_date, class_start, class_end, start_time_known, hours_until_class, topic, readings: [{title, url}], due_before_class: [{title, type, due_at, date, time, time_known, canvas_url, source}], has_due_before_class, notify_at}]`, `events: [{course_id, code, date, time, due_at, title, type, category, confidence, source, canvas_url}]`, `courses: [{id, code, name, canvas_course_id}]`, `timezone_fallback?` |
| `course` | `<id_or_code>` | `timezone, course: {id, code, name, canvas_course_id, schedule, grading_rules, policies}, next_sessions: [{date, start, end}]` (14-day window) |
| `check` | | `timezone, now, courses, sessions, events` (counts), `endpoint`, `warnings?` |

Semantics the agent relies on:

- `key` = `<course>@<YYYY-MM-DD>`, the prep-log record key, matching
  `^[^@\s]+@\d{4}-\d{2}-\d{2}$`. `course` is the course code (or the app's course id when the
  code is empty) with whitespace and `@` replaced by `-` ("CS 101" → `CS-101`); `course_code` is
  the unmodified code.
- All timestamps carry the user's timezone offset (the app's `timezone`, default
  `America/New_York`). Sessions that already started are dropped. Event and session times arrive
  as `HH:MM` or Postgres `HH:MM:SS` and are normalized to `HH:MM`.
- `due_before_class`: the course's dated events (any `type` except `no_class`) with
  `now ≤ due_at ≤ class_start`. No time → 23:59 that day, or class start when on the class day.
- `notify_at`: `class_start − 24h` (elapsed, computed in UTC so a DST change doesn't shift it) if
  `has_due_before_class`, else 06:30 local on class day; `now` if that has already passed. This
  skill is the source of truth: `preplog upsert --notify-at` stores it as given and only falls
  back to its own rule when the flag is absent. `hours_until_class` is elapsed hours as well.
- No start time (`start_time_known: false`): `class_start`, `class_end` and `hours_until_class`
  are `null`; the due window runs to 23:59 on class day; `notify_at` is 06:30 on class day; the
  session is kept until the day is over.
- Stable fields other skills read: `key`, `notify_at`, `has_due_before_class`, `canvas_course_id`.
- `topic` / `readings` are `null` / `[]` until the app sends them. Readings come from the canvas skill.
- Untrusted data: strings are capped at 1000 characters, lists at 500 items, bodies at 4 MB;
  URLs must be http(s); `schedule`/`grading_rules`/`policies` over 20k characters are replaced
  by `{"truncated": true, "chars"}`.

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `SYLLABI_401` | HTTP 401/403: token missing, malformed, expired, revoked, wrong scope | no | skip planning this run; ask Chris once for a new token (Settings → Agent access) |
| `SYLLABI_NOT_FOUND` | HTTP 404 (wrong base URL / function not deployed); `course` with no match (`detail.known_codes`) | no | configuration: tell Chris |
| `SYLLABI_BAD_REQUEST` | HTTP 400/405/422 | no | a bug: log it |
| `SYLLABI_RATE_LIMIT` | 429 twice (one `Retry-After` wait ≤ 5 s already done) | yes (next run) | |
| `SYLLABI_UNAVAILABLE` | 5xx / timeout / network / non-JSON body / refused redirect | yes (once), except the redirect | then skip planning and log it |
| `SYLLABI_BAD_RESPONSE` | JSON, but not the expected shape; body > 4 MB | no | log it and skip the run; the app changed |
| `USAGE` | bad arguments or configuration | no | fix the call |

Security rules (requirements): GET only; https only; the token goes to `SYLLABI_BASE_URL`'s host
and nowhere else (3xx is refused, never followed); the token and anon key are scrubbed from every
output line, crash output included; `-v` logs `GET <path>` without query strings or headers.

---

## 2. `canvas` skill (read-only)

Implemented as `workspace/skills/canvas/scripts/canvas.py` (SYL-93; `scripts/canvas` is a symlink
to it), a standard-library Python CLI over the Canvas REST API: base `$CANVAS_BASE_URL/api/v1`,
header `Authorization: Bearer $CANVAS_TOKEN` (`CANVAS_API_TOKEN` is an alias), `per_page=100`,
every `Link: rel="next"` page followed. `canvas-mcp` was not used: whether the OpenClaw template
can host a Python MCP server is unverified, and these are six GET endpoints.

| Op | Inputs | Canvas endpoint | Output (`ok: true` plus) |
| --- | --- | --- | --- |
| `modules` | `course_id` | `GET /courses/{id}/modules?include[]=items&include[]=content_details` (+ `/modules/{mid}/items` when Canvas omits items) | `items: [{module, module_id, position, item_position, item_type, title, id, content_id, url, html_url, page_url, external_url, published, content_type, size, locked_for_user}]`, `modules` (count) |
| `files` | `course_id`, `--folder <path>?` | `GET /courses/{id}/files?sort=updated_at&order=desc` + `GET /courses/{id}/folders` | `files: [{id, display_name, filename, content_type, size, updated_at, folder, folder_id, locked_for_user}]`; `folders_error` if the folder lookup failed |
| `download` | `course_id`, `file_id`, `dest_dir` (under `$DATA_DIR/readings/`) | `GET /courses/{id}/files/{file_id}`, then its pre-signed `url` | `path, bytes, sha256, skipped, file_id, display_name, content_type`. Streams to `<name>.part` (deleted on failure), then renames. `dest_dir/.canvas-manifest.json` records `{file_id, updated_at, bytes, sha256}` per name: same `file_id`, same `updated_at` and size → `skipped: true`, nothing fetched. A name held by another file id (or a file the tool didn't write) is never overwritten; the new file becomes `<root>-<file_id><ext>` |
| `assignments` | `course_id`, `--upcoming?` | `GET /courses/{id}/assignments?include[]=submission[&bucket=upcoming]` | `assignments: [{id, name, due_at, html_url, description_text, links: [{text, href}], submission_types[], submitted}]`. `description_text` is full length, HTML stripped |
| `page` | `course_id`, `page_url_or_id` | `GET /courses/{id}/pages/{url}` | `page: {url, title, body_text, links, updated_at, html_url}` |
| `whoami` | | `GET /users/self` | `user: {id, name}` |

V0 aliases: `list_modules`, `list_files`, `download_file`, `upcoming_assignments` (= `assignments --upcoming`), `get_page`.

No write ops exist, by design. Security rules the implementation enforces (SYL-93):
`display_name` is sanitized to `[A-Za-z0-9._ -]` (leading dots stripped, empty rejected) before
anything is written under `dest_dir`; the token goes only to `CANVAS_BASE_URL`'s host and never on
a redirect; `Link: rel="next"` pages are followed only when https on that same host; redirects
must be https and must not resolve to private/loopback/link-local addresses;
`-v` logs `<method> <path>` only.

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `CANVAS_401` | token missing/expired/revoked: a 401 with a `WWW-Authenticate` header or a body naming the token | no | stop Canvas calls this run; ask Chris to refresh the token |
| `CANVAS_403` | authenticated but forbidden, or the file is locked (includes Canvas's permissions 401 "user not authorized", reported with `status: 401`) | no | fall back to the syllabus link, else `needs-human` (ask-a-human condition 1) |
| `CANVAS_404` | not found | no | log it, note it in course-notes |
| `CANVAS_RATE` | 429 (or Canvas's 403 "Rate Limit Exceeded") twice; the tool already waited `Retry-After` (≤ 5 s) and retried once | yes (next run) | leave it |
| `CANVAS_NET` | 5xx / timeout / non-JSON body / 50 s download deadline exceeded (`retryable: true`); refused redirect or unexpected 4xx (`retryable: false`) | see `retryable` | retry once if retryable |
| `FILE_TOO_LARGE` | > 100 MB | no | skip it; include `detail.canvas_url` in the brief |
| `BAD_FILENAME` | the Canvas file name sanitizes to nothing | no | skip it; tell Chris |
| `USAGE` | bad arguments/config, `dest_dir` outside `$DATA_DIR/readings/` | no | a bug in the call |

Tests (no network, fake transport): `python3 -m unittest discover -s tests`.

---

## 3. `nlm` skill (NotebookLM via `notebooklm-py`)

Cookies: `$NLM_COOKIES_PATH` = `/data/secrets/notebooklm-cookies.json`.

### `nlm-prep <course> <date> <pdf...>`

| | |
| --- | --- |
| Inputs | `course` (e.g. `MAS.665`), `date` (`YYYY-MM-DD`), one or more PDF paths under `/data/readings/` |
| Behavior | create notebook `"<course> — <date>"`, add sources, **start** audio overview, return immediately |
| Output | `{ok: true, notebook_id, sources_added, audio: "started"}` |
| Precondition | the agent must not call it if the prep-log record has a `notebook_id` |

### `nlm-status <notebook_id> [--course C --date D]`

| | |
| --- | --- |
| Output | `{ok: true, status: "pending"}` · `{ok: true, status: "ready", audio_url, local_path, drive_link}` · `{ok: true, status: "failed", reason}` |
| Side effect on `ready` | downloads the mp3 to `/data/podcasts/<course>/<date>.mp3` and pushes it to Drive (`drive-put` logic). Idempotent |

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `NLM_AUTH` | cookies expired/invalid | yes (**once**) | after the retry fails: `partial`, ask for fresh cookies (condition 3); brief and Drive links still go out on schedule; the next prep run does only the podcast step |
| `NLM_NOT_FOUND` | notebook id unknown | no | ask Chris before clearing `notebook_id` |
| `NLM_SOURCE_REJECTED` | a source couldn't be added | no | continue with the rest; note it in the brief |
| `NLM_RATE_LIMIT` | quota / throttled | yes (next poll) | stay `podcast-pending` |
| `NLM_UNAVAILABLE` | network / unexpected response | yes (once) | then `partial` |

---

## 4. `drive` skill (rclone)

Config: `$RCLONE_CONFIG` = `/data/rclone/rclone.conf`. Root: `$DRIVE_READINGS_ROOT`.

### `drive-put <local_path> <remote_dir>`

| | |
| --- | --- |
| Inputs | `local_path` under `/data/`; `remote_dir` relative to the root, e.g. `MAS.665/2026-09-29` |
| Output | `{ok: true, remote_path, share_link, uploaded}`, where `uploaded: false` means it was already there |
| Idempotency | same name and same size/hash in `remote_dir`: no upload, same link returned |

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `DRIVE_AUTH` | rclone token invalid | no | ask Chris to reconnect the rclone remote |
| `DRIVE_QUOTA` | quota exceeded | yes (next run) | |
| `FILE_TOO_LARGE` | > 100 MB | no | skip it, tell Chris |
| `LOCAL_NOT_FOUND` | missing local file | no | a bug: log it |
| `DRIVE_UNAVAILABLE` | network / 5xx | yes (once) | |

---

## 5. Telegram (Maritime channel)

Paired in the Maritime dashboard (agent → **Channels** → Telegram). Verified 2026-09-24:

- **Proactive sends** (everything a cron job sends) use Maritime's CLI:
  `maritime-telegram-send "<text>"` or `printf '%s' "$msg" | maritime-telegram-send -`.
  It requires `MARITIME_TELEGRAM_CONNECTED=1` in the environment. A plain chat reply from a cron
  run goes nowhere, because nobody messaged the agent.
- **Replies** to a message Chris sent on Telegram go back on the same channel automatically.
- **Files**: `maritime-share <abs path>`, then paste its fenced output verbatim.

Only the main agent sends. The subagent has `exec` and `message` denied, so it can do neither.

| Message | Content | Guard |
| --- | --- | --- |
| **brief** | topic, why it matters, key arguments, prep checklist, pre-class questions + draft answers (marked DRAFT), Drive links, podcast link or "🎧 podcast pending — link to follow" | `brief_sent_at` |
| **podcast link** | "🎧 podcast ready: <link>" for a session whose brief is already sent | `podcast_sent_at` |
| **question** | `[<course> <date>]` + one blocker + exactly what's needed and where to put it | `history[]` shows no identical ask |

| code | when | retryable |
| --- | --- | --- |
| `TELEGRAM_SEND_FAILED` | `maritime-telegram-send` exits non-zero | yes (once); if the retry fails, leave `*_sent_at` unset so the next poll retries |
| `TELEGRAM_NOT_CONNECTED` | `MARITIME_TELEGRAM_CONNECTED` is not `1` | no; log it, leave `*_sent_at` unset, Chris must re-pair the channel |

---

## 6. `brief-writer` subagent

Spawned via `sessions_spawn({agentId: "brief-writer", mode: "run", context: "isolated", task})`
(fallback: `llm-task` with `schema`). See `workspace/agents/brief-writer.md`.

| | |
| --- | --- |
| Input | inline in `task`: session row, Canvas assignment/page text, extracted reading text (≤ ~40k tokens total, per-reading truncation note), course-notes section |
| Tools | none |
| Output | JSON matching `workspace/memory-templates/brief.schema.json` |

Validation errors (raised by the main agent):

| code | when | action |
| --- | --- | --- |
| `BRIEF_SCHEMA_INVALID` | failed schema validation twice (original + one re-prompt) | `partial`; send Drive links + podcast without a brief |
| `BRIEF_HALLUCINATED_QUESTION` | a question's fuzzy match to the Canvas text is < 0.9 | drop that question, log `HALLUCINATION: …` (not a session failure) |

### `brief` skill (the pipeline around the subagent)

Implemented as `{baseDir}/scripts/brief <command> …` (`workspace/skills/brief/SKILL.md` has the
full command table and a worked prep run). It builds the bundle, checks the reply and formats the
Telegram text; the main agent still spawns the subagent itself. Work files live under
`/data/work/<course>/<date>/`. `<key>` is `<course>@<YYYY-MM-DD>`.

| Command | Inputs | Output (`ok: true` plus) |
| --- | --- | --- |
| `bundle <key>` | `--session FILE\|-` (row or `syllabi upcoming` output), `--canvas FILE`* (`canvas assignments`/`page` JSON, `{title,url,text}`, or text; `--assignment-id ID`* filters), `--readings-json FILE` (record / `preplog get` output / list) or `--reading PATH`*, `--notes FILE\|-`, `--cap-chars N`, `--print` | `task_path` (`brief-input.md`: prompt head from `agents/brief-writer.md` + `## SESSION`, `## CANVAS TEXT` (`### CANVAS: <title> (<url>)`), `## READINGS` (`### READING: <title>`, cut ones end `[TRUNCATED: kept first N of M characters]`), `## COURSE NOTES`), `sidecar_path` (`brief-input.json`), `task_name`, `chars, est_tokens, cap_chars, canvas[], readings[], skipped[], truncated[], warnings[]` |
| `validate <key>` | `--reply FILE\|-`, `--attempt N`, `--threshold R`, `--canvas FILE`* (override) | `attempt, brief_path` (`brief-output.json`, the cleaned brief), `reply_path`, `questions {returned, kept, dropped}`, `kept[] {question, score, source, source_ok, source_original?}`, `dropped[] {question, score, best_match, reason, log_line}`, `log_lines[]`, `warnings[]` |
| `format <key>` | `--record FILE\|-`, `--brief FILE`, `--drive-link URL`*, `--podcast-url URL`, `--no-podcast`, `--dropped N` | `text, parts[], paths[]` (`brief-telegram.txt`, `.2.txt`…), `send_with[]`, `has_brief, brief_source, questions, drive_links[], podcast: ready\|pending\|unavailable, includes_podcast, dropped` |
| `format-podcast <key>` | `--url URL`, `--record FILE\|-` | `text` ("🎧 podcast ready: <link>" + course and class time), `path` |
| `prompt` | | `prompt, source` (the blockquote under "## Prompt" in `agents/brief-writer.md`) |
| `score` | `--question TEXT`, `--canvas FILE`*, `--threshold R` | `score, best_match, kept` |

Rules the tool enforces: the bundle never exceeds the cap (`BRIEF_CAP_CHARS`, default 160000
characters ≈ 40k tokens); Canvas text is kept whole and readings share the rest evenly; the
Canvas text used by `validate` is the sidecar's copy, never text parsed back out of the bundle (a
reading with a fake `### CANVAS:` header is indented, not promoted); inputs may not come from
`/data/secrets/` or `/data/rclone/`, readings only from `/data/readings/` or `/data/work/`; the
cleaned brief holds only the schema's fields and only the questions that scored ≥ the threshold.
A question is scored against whole Canvas sentences/lines (never a fragment), needs ≥ 4 words, and
is rejected at any threshold when a number, a negation or a content word differs. Content lines
starting with `#`, `---` or `[TRUNCATED` are indented so they cannot forge bundle structure.
`bundle` clears the previous run's replies and validation files (`cleared[]`), so attempt
numbering restarts at 1. Telegram length is counted in UTF-16 code units.

### `pdf-text` skill

`{baseDir}/scripts/pdf-text <pdf> [--json] [--out FILE]` (`workspace/skills/pdf-text/SKILL.md`):
the text of a PDF under `/data/readings/` or `/data/work/`, via `pdftotext` → `pypdf` → the
built-in extractor (the same chain `brief bundle` uses). Plain text on stdout by default; with
`--json`: `{ok, path, extractor, chars, warning, text}` (`out` instead of `text` with `--out`,
which must be under `/data/work/`). Errors are always the JSON envelope, exit 2: `USAGE` (bad
arguments or path), `NOT_FOUND`, `NOT_PDF` (no `%PDF-` header, e.g. a saved login page). None
retryable.

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `BRIEF_SCHEMA_INVALID` | no JSON object in the reply, or it fails the schema (`detail.errors`, `detail.attempt`, `detail.reprompt`, `detail.next`); nothing stored | no | attempt 1: spawn again with `detail.reprompt` appended; attempt 2: `partial` (§6 above) |
| `NO_BUNDLE` | `validate` without a prior `bundle` for the key and no `--canvas` | no | run `bundle` |
| `PROMPT_MISSING` / `SCHEMA_MISSING` | `agents/brief-writer.md` / `memory-templates/brief.schema.json` not reachable | no | run `scripts/install-workspace.sh` |
| `USAGE` | bad key, forbidden path, unknown Canvas JSON shape, session file without this key | no | fix the call |

A dropped question is reported in `dropped[]` / `log_lines[]` and is not an error.

---

## 7. Agent-level codes (in `last_error`, not emitted by tools)

| code | meaning |
| --- | --- |
| `LOGIN_REQUIRED` | external reading behind a login (ask-a-human 1) |
| `READING_LIST_CONFLICT` | syllabus and Canvas disagree (ask-a-human 2) |
| `NEEDS_OWN_ANSWER` | deliverable needs Chris's own answer (ask-a-human 4) |
| `TOOL_BUDGET` | 25 tool calls for this session in this run |
| `MAX_ATTEMPTS` | 3 attempts reached; session → `needs-human` (set by `preplog begin`, see the `preplog` section below) |

---

## 8. `preplog` skill (memory)

The agent's durable memory as a tool: `/data/memory/prep-log.json`, the run log under
`/data/logs/` and `/data/memory/course-notes.md`. Implemented as `{baseDir}/scripts/preplog
<command> …` (`workspace/skills/preplog/SKILL.md` has the full command table and a worked prep
run). Every write is validated against `workspace/memory-templates/prep-log.schema.json` (briefs
against `brief.schema.json`), written atomically under a lock, and stamped with the
America/New_York offset. The agent never edits these files any other way.

Global options: `--trigger prep|poll|notify|human|manual` (written into `history[]`, names the
run-log file) and `--now ISO` (evals and tests only). `<key>` is `<course>@<YYYY-MM-DD>`.

| Command | Inputs | Output (`ok: true` plus) |
| --- | --- | --- |
| `init` | | `prep_log, created_prep_log, course_notes, created_course_notes, logs_dir` (never overwrites) |
| `validate` | | `sessions` (count) |
| `get <key>` | | `session`, `plan: {steps[], recorded[]}` |
| `list` | `--status S`*, `--course C` | `count, sessions[]` (summaries) |
| `upsert <key>` | `--class-start`, `--canvas-course-id`, `--topic`, `--has-due-before-class`, `--notify-at`, `--from-json FILE\|-` | `created, changed, session, plan`. No write at all when nothing changed. `--notify-at` (from the syllabi skill) wins; without it `notify_at` is computed by the AGENTS.md rule for a new record and recomputed when `class_start` / `has_due_before_class` change. `class_start` must fall on the key's ET date. `--from-json` may not set `history, attempts, notebook_id, status, *_sent_at, podcast_url, brief, last_error` |
| `begin <key>` | | `skip, reason?, attempts, plan, notify_chris?` (stop rules: skips `podcast-pending, ready, notified-partial, done, needs-human`; counts one attempt) |
| `add-reading <key>` | `--title --source canvas_file\|external --id-or-url` `[--local-path --drive-path --requires-login --truncated-for-brief]` | `created, reading, readings, plan` (idempotent on source + id_or_url) |
| `add-drive-path <key> <path>` | | `added, drive_paths` |
| `set-notebook <key> <id>` | | `changed, status_before, status` (→ `podcast-pending`) |
| `set-podcast <key> --url` | | `changed, status_before, status, send?` (`podcast-pending` → `ready`; `notified-partial` stays, `send: "podcast-link-only"`) |
| `set-brief <key> --from FILE\|-` | brief JSON | `replaced, questions, plan` |
| `set-status <key> <status>` | `--error-code --error-message --step`, `--clear-error`, `--reset-attempts` (with `pending`, `--trigger human\|manual` only) | `changed, status_before, status, attempts, last_error` |
| `mark-sent <key> brief\|podcast` | `--podcast-included` | `status, brief_sent_at, podcast_sent_at` (`brief` alone → `notified-partial`; with the link, or `podcast` → `done`) |
| `log <key> --action A` | `--detail D` | `entry, history` (length) |
| `due` | | `briefs[], podcast_links[], podcast_pending[], needs_human_today[], nothing_to_do` |
| `runlog` | `--sessions --tools --decisions --outcome --line`* (needs `--trigger`) | `path, date, lines` |
| `notes get` / `notes set` | `--course`; `--field --value [--title --append]` | `text, found?` / `line, created_section, replaced` |

`plan.steps` ⊆ `find_readings, download, drive, podcast, brief`: only the work the record does not
already show (hard rule 4). `due` is the send pass: `briefs` = `notify_at ≤ now` and no
`brief_sent_at` (a `needs-human` record with no brief and no Drive links is left out);
`podcast_links` = brief sent, `podcast_url` set, `podcast_sent_at` unset (`send: "podcast-link-only"`);
`podcast_pending` = `podcast-pending`, plus `notified-partial` records with a `notebook_id` and no
`podcast_url` (their audio is still in flight).

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `NOT_FOUND` | no record for the key (`detail.known_keys`) | no | `upsert` it first |
| `ALREADY_HAS_NOTEBOOK` | a different `notebook_id` is already recorded | no | do not call `nlm-prep`; `nlm-status` the existing notebook |
| `ALREADY_SENT` | that `*_sent_at` guard is already set | no | do not send |
| `BRIEF_NOT_SENT` | podcast link before the brief | no | send the brief first |
| `BRIEF_SCHEMA_INVALID` | the brief fails `brief.schema.json` (`detail.errors`); nothing stored | no | re-prompt once with the errors, then `set-status partial` (see `brief-writer` above) |
| `PREPLOG_INVALID` | the write would break the schema (`detail.errors`); nothing written | no | fix the field; for `--from-json`, drop unknown keys |
| `PREPLOG_CORRUPT` | the file is not valid JSON / not version 1; never overwritten | no | say so in the run log; Chris repairs it |
| `SCHEMA_MISSING` | `memory-templates/` not reachable from the skill | no | run `scripts/install-workspace.sh` |
| `USAGE` | bad arguments (key shape, dates, enums, empty values) | no | fix the call |

Guards this tool enforces, so the model does not have to: one `notebook_id` per session, one
brief send and one podcast-link send per session, `attempts` capped at 3 (`needs-human`,
`MAX_ATTEMPTS`, `notify_chris` once; only a `human`/`manual` `set-status pending --reset-attempts`
starts over), a `plan` that never lists a recorded step, and an `upsert` that writes nothing when
nothing changed. It does **not** count tool calls: the 25-per-session budget is the agent's rule.
