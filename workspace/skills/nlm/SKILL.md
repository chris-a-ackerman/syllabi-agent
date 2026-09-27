---
name: nlm
description: Start and check NotebookLM audio overviews (podcasts) for a class session via notebooklm-py.
---

# nlm: NotebookLM podcasts

> **Stub (V0).** Interfaces only. The implementation uses `notebooklm-py` (unofficial, cookie
> auth; there is no consumer API) and lands in a later ticket as `{baseDir}/scripts/nlm-prep` and
> `{baseDir}/scripts/nlm-status`.

Podcast generation takes minutes, far more than the 30-second reply budget. So it is split in two:
`nlm-prep` **starts** the job and returns at once, and a later `poll` trigger calls `nlm-status`.

**Never call `nlm-prep` for a session whose prep-log record already has a `notebook_id`.**

Cookies: `/data/secrets/notebooklm-cookies.json` (`$NLM_COOKIES_PATH`). Never print their contents.

## Commands

### `{baseDir}/scripts/nlm-prep <course> <date> <pdf>...`

Creates notebook `"<course> — <date>"`, adds each PDF (paths under `/data/readings/`) as a source,
**starts** the audio overview, and returns immediately.

```json
{"ok": true, "notebook_id": "…", "sources_added": 3, "audio": "started"}
```

### `{baseDir}/scripts/nlm-status <notebook_id> [--course <course> --date <date>]`

Checks the audio overview.

```json
{"ok": true, "status": "pending"}
{"ok": true, "status": "ready", "audio_url": "…", "local_path": "/data/podcasts/<course>/<date>.mp3", "drive_link": "https://drive.google.com/…"}
{"ok": true, "status": "failed", "reason": "…"}
```

When the status is `ready`, the command has already downloaded the mp3 to
`/data/podcasts/<course>/<date>.mp3` and pushed it to Drive (via the same logic as `drive-put`).
Running it again is idempotent.

## Errors

| code | meaning | what to do |
| --- | --- | --- |
| `NLM_AUTH` | cookies expired/invalid | retry **once**. If it fails again, mark the session `partial`, ask Chris for fresh cookies, and still send the brief and Drive links on schedule |
| `NLM_NOT_FOUND` | notebook id unknown (deleted?) | clear `notebook_id` only after asking Chris. Never silently start a second podcast |
| `NLM_SOURCE_REJECTED` | a PDF couldn't be added (size/format) | continue with the rest and note it in the brief |
| `NLM_RATE_LIMIT` | daily audio quota / throttled | leave it `podcast-pending` and retry on the next poll |
| `NLM_UNAVAILABLE` | network / unexpected response (the unofficial API changed?) | retryable once, then `partial` |
