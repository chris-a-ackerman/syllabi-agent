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

Implemented as `{baseDir}/scripts/canvas <op> …`, or as the equivalent tools from
`vishalsachdev/canvas-mcp` mounted as an MCP server. Either way, the ops, outputs and error codes
are the ones below.

| Op | Inputs | Output (`ok: true` plus) |
| --- | --- | --- |
| `list_modules` | `course_id` | `modules: [{id, name, position, items: [{id, title, type, content_id?, page_url?, external_url?}]}]` |
| `list_files` | `course_id`, `folder?` | `files: [{id, display_name, filename, size, content_type, updated_at, folder}]` |
| `download_file` | `file_id`, `dest_dir` (under `/data/readings/`) | `file_id, local_path, bytes, sha256, skipped` |
| `upcoming_assignments` | `course_id`, `days?` (default 7) | `assignments: [{id, name, due_at, html_url, description_text, submission_types[]}]` |
| `get_page` | `course_id`, `page_url` | `page: {url, title, body_text, updated_at}` |

No write ops exist, by design.

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `CANVAS_401` | token missing/expired/revoked | no | stop Canvas calls this run; ask Chris to refresh the token |
| `CANVAS_403` | authenticated but forbidden | no | fall back to the syllabus link, else `needs-human` (ask-a-human condition 1) |
| `CANVAS_404` | not found | no | log it, note it in course-notes |
| `CANVAS_RATE_LIMIT` | 429 | yes | retry once after a pause |
| `CANVAS_UNAVAILABLE` | 5xx / timeout | yes | retry once |
| `FILE_TOO_LARGE` | > 100 MB | no | skip it; include the Canvas link in the brief |

---

## 3. `nlm` skill (NotebookLM via notebooklm-py's `notebooklm` CLI)

Implemented as `workspace/skills/nlm/scripts/nlm.py` (SYL-94). `scripts/nlm-prep` and
`scripts/nlm-status` are symlinks to it that imply the subcommand. It is a thin wrapper: each
NotebookLM step is one `notebooklm … --json` subprocess run from an argv list (never a shell),
with `NOTEBOOKLM_HOME=/data/notebooklm`, and a timeout equal to the time left in the 50 s budget.
Auth is a master-token login for the dedicated agent account, or `NOTEBOOKLM_AUTH_JSON`.

**Envelope (differs from §0):** success is the SYL-94 shape itself (no `ok` key). An error is
`{"error": "<CODE>", "message", "retryable"}` with exit 2, and a wrapper crash is
`{"error": "INTERNAL"}` with exit 1. `NLM_AUTH` is always exactly `{"error": "NLM_AUTH"}`.

### `nlm-prep <course_code> <date> <pdf...> --topic "<session topic>"` (= `nlm.py prep`)

| | |
| --- | --- |
| Steps | `auth check --test --json` → `create "<course_code> — <date>" --use --json` → per PDF `source add <pdf> --title "<name>" -n <id> --json` → `generate audio "<prompt>" -n <id> --no-wait --json` |
| Prompt | template in the nlm `SKILL.md` ("…how they relate to `<session topic>`"). The topic travels as data inside one argv element: control characters are flattened and it is capped at 300 characters |
| Output | `{notebook_id, task_id}`, plus `sources_rejected: [name]` when some PDFs were refused, plus `skipped: true` when the session already had a notebook (prep-log `notebook_id`, or this tool's job record `/data/work/nlm/<course>-<date>.json`). Nothing new is started in that case |
| Resume | after a timeout or error mid-way, the next `prep` reuses the created notebook and skips PDFs already added. It never creates a second notebook |

### `nlm-status <notebook_id> <task_id> [--course C --date D]` (= `nlm.py status`)

| | |
| --- | --- |
| Steps | `artifact poll <task_id> -n <id> --json`. On `completed`: `download audio /data/podcasts/<course>-<date>.mp3 -n <id> --latest --json` (via `.part` + rename), then `drive-put <mp3> Podcasts` (§4), reading `web_url`. Never `generate` |
| Output | `{status: "pending" \| "ready" \| "failed", local_path, drive_url}`. `failed` may add `error_code`. `pending` with a `local_path` adds `drive_error`: the mp3 is downloaded but the upload failed or was deferred (`DRIVE_DEFERRED` when fewer than 15 s were left), and the next call retries only the upload |
| course/date | from the flags, else from prep's job record, else from the prep-log record that holds `notebook_id` |

### `nlm.py check`

`{status: "ok", notebooklm_home}`, or `{"error": "NLM_AUTH"}`: the auth smoke test (`auth check --test`).

Security rules the implementation enforces (SYL-94 Security):
- Source paths must resolve (realpath) under `$DATA_DIR/readings/`. `course`, `date`, and the ids are validated before use.
- `NOTEBOOKLM_HOME` is kept at mode 700 and its `storage_state.json` / `master_token.json` files at 600. Looser modes are tightened with a warning; if they can't be tightened, the command refuses to run (`NLM_AUTH_PERMS`).
- The CLI's stderr is dropped, and its error text is never copied into this tool's output. Only its `code` is mapped.

| code | when | retryable | agent action |
| --- | --- | --- | --- |
| `NLM_AUTH` | `auth check --test` failed, or any call returned `AUTH_REQUIRED` / `AUTH_ERROR` (stale or expired session) | once | after the retry: `partial`, ask for re-login (condition 3); brief and Drive still go out |
| `NLM_AUTH_PERMS` | credential file permissions can't be tightened | no | tell Chris |
| `NLM_RATE_LIMIT` | `RATE_LIMITED`, `NOTEBOOK_LIMIT` | yes (next poll) | stay `podcast-pending` |
| `NLM_SOURCE_REJECTED` | every PDF refused (`VALIDATION_ERROR` on `source add`) | no | `partial` for the podcast |
| `NLM_NOT_FOUND` | `NOT_FOUND` | no | ask Chris before clearing `notebook_id` |
| `NLM_GENERATION_FAILED` | `GENERATION_FAILED` | no | `partial` |
| `NLM_TIMEOUT` | the budget ran out mid-command | yes | next poll (`prep` resumes) |
| `NLM_UNAVAILABLE` | `NETWORK_ERROR` and similar (retryable), or unexpected output / a CLI crash | see `retryable` | retry once, then `partial` |
| `NLM_NOT_INSTALLED` | no `notebooklm` on `PATH` | no | tell Chris |
| `USAGE` | bad arguments | no | a bug in the call |

Tests (no network; a fake `notebooklm` executable that prints the real `--json` shapes):
`python3 -m unittest discover -s tests`.

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
