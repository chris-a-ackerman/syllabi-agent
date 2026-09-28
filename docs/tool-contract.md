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
- A timeout counts as an error (`*_UNAVAILABLE`, `retryable: true`), never a hang. Each call must
  finish well within the 30-second reply budget. The only long job (audio generation) runs
  asynchronously.

---

## 1. syllabi endpoint (HTTP)

Auth: `Authorization: Bearer $SYLLABI_AGENT_TOKEN`. Base: `$SYLLABI_BASE_URL`.

### `GET /agent/upcoming?days=3`

Output (expected shape; confirm against the syllabi app):

```json
{
  "sessions": [
    {
      "course": "MAS.665",
      "course_id": "…",
      "canvas_course_id": 12345,
      "class_date": "2026-09-29",
      "class_start": "2026-09-29T13:00:00-04:00",
      "topic": "…",
      "readings": [{ "title": "…", "url": "…", "canvas_file_id": 678 }],
      "due": [{ "title": "Pre-class questions 4", "due_at": "2026-09-28T23:59:00-04:00", "canvas_assignment_id": 901 }]
    }
  ]
}
```

### `GET /agent/course/:id`

Output: course metadata (`course`, `title`, `canvas_course_id`, `schedule[]`, `reading_policy?`).

| code | when | retryable |
| --- | --- | --- |
| `SYLLABI_401` | token missing/invalid | no: ask Chris |
| `SYLLABI_UNAVAILABLE` | 5xx / timeout / network | yes (once) |
| `SYLLABI_BAD_RESPONSE` | body doesn't match the expected shape | no: log it and skip the run |

(Called over HTTP rather than through a skill script. The agent maps HTTP failures to these codes
in its log and prep-log.)

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
| `download` | `course_id`, `file_id`, `dest_dir` (under `$DATA_DIR/readings/`) | `GET /courses/{id}/files/{file_id}`, then its pre-signed `url` | `path, bytes, sha256, skipped, file_id, display_name, content_type`. Same name and size already present → `skipped: true`, nothing fetched |
| `assignments` | `course_id`, `--upcoming?` | `GET /courses/{id}/assignments?include[]=submission[&bucket=upcoming]` | `assignments: [{id, name, due_at, html_url, description_text, links: [{text, href}], submission_types[], submitted}]`. `description_text` is full length, HTML stripped |
| `page` | `course_id`, `page_url_or_id` | `GET /courses/{id}/pages/{url}` | `page: {url, title, body_text, links, updated_at, html_url}` |
| `whoami` | | `GET /users/self` | `user: {id, name}` |

V0 aliases: `list_modules`, `list_files`, `download_file`, `upcoming_assignments` (= `assignments --upcoming`), `get_page`.

No write ops exist, by design. Security rules the implementation enforces (SYL-93):
`display_name` is sanitized to `[A-Za-z0-9._ -]` (leading dots stripped, empty rejected) before
anything is written under `dest_dir`; the token goes only to `CANVAS_BASE_URL`'s host and never on
a redirect; redirects must be https and must not resolve to private/loopback/link-local addresses;
`-v` logs `<method> <path>` only.

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `CANVAS_401` | token missing/expired/revoked | no | stop Canvas calls this run; ask Chris to refresh the token |
| `CANVAS_403` | authenticated but forbidden, or the file is locked | no | fall back to the syllabus link, else `needs-human` (ask-a-human condition 1) |
| `CANVAS_404` | not found | no | log it, note it in course-notes |
| `CANVAS_RATE` | 429 (or Canvas's 403 "Rate Limit Exceeded") twice; the tool already waited `Retry-After` (≤ 5 s) and retried once | yes (next run) | leave it |
| `CANVAS_NET` | 5xx / timeout / non-JSON body (`retryable: true`); refused redirect or unexpected 4xx (`retryable: false`) | see `retryable` | retry once if retryable |
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

---

## 7. Agent-level codes (in `last_error`, not emitted by tools)

| code | meaning |
| --- | --- |
| `LOGIN_REQUIRED` | external reading behind a login (ask-a-human 1) |
| `READING_LIST_CONFLICT` | syllabus and Canvas disagree (ask-a-human 2) |
| `NEEDS_OWN_ANSWER` | deliverable needs Chris's own answer (ask-a-human 4) |
| `TOOL_BUDGET` | 25 tool calls for this session in this run |
| `MAX_ATTEMPTS` | 3 attempts reached; session → `needs-human` |
