---
name: drive
description: Upload readings and podcasts to Google Drive (Readings/<course>/<date>/) via rclone and return share links.
metadata: { "openclaw": { "requires": { "bins": ["rclone"] } } }
---

# drive: Google Drive via rclone

> **Stub (V0).** Interfaces only. The implementation lands in a later ticket as
> `{baseDir}/scripts/drive-put`.

Drive is where the iPad reaches readings: the Files app mounts Drive, and Goodnotes imports from
it. The Goodnotes import itself is a manual tap. Don't claim to have done it.

rclone config: `/data/rclone/rclone.conf` (`$RCLONE_CONFIG`). Root: `$DRIVE_READINGS_ROOT`
(e.g. `gdrive:Readings`).

## Command

### `{baseDir}/scripts/drive-put <local_path> <remote_dir>`

- `local_path` must be under `/data/`.
- `remote_dir` is relative to the Drive root, e.g. `MAS.665/2026-09-29`.
- **Idempotent:** if a file with the same name and size/hash already exists in `remote_dir`, the
  command skips the upload and returns the existing link.

```json
{"ok": true, "remote_path": "Readings/MAS.665/2026-09-29/week4.pdf", "share_link": "https://drive.google.com/…", "uploaded": false}
```

(`uploaded: false` means it was already there.)

## Errors

| code | meaning | what to do |
| --- | --- | --- |
| `DRIVE_AUTH` | rclone token expired/invalid | ask Chris to re-run `rclone config reconnect` and upload the new conf. Keep the local files |
| `DRIVE_QUOTA` | storage/API quota exceeded | retryable on the next run |
| `FILE_TOO_LARGE` | > 100 MB | skip it and tell Chris |
| `LOCAL_NOT_FOUND` | `local_path` missing | a bug: log it and don't retry |
| `DRIVE_UNAVAILABLE` | network / 5xx | retryable once |
