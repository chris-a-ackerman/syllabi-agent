# Live evidence, HW3 forum agent, from 2026-10-05

Working notes for the live run of `class-prep-forum` (SYL-110). The final pieces go into the three
`EVIDENCE` placeholders in [`docs/hw3-writeup.md`](../../docs/hw3-writeup.md).

Before committing anything in this folder: no tokens, no emails, no other students' names
(`python3 scripts/redact-evidence.py < file` for log lines). Never name a file `forum.jsonl` or
`forum-state.json`: they are gitignored and `make-submission-zip.sh` refuses them.

## Setup (2026-10-05, ET)

- `CANVAS_FORUM_TOKEN` and `CANVAS_FORUM_TOPIC_ID` set as Maritime secrets; `forum status` and
  `forum read` return `ok: true`, `control: RUNNING`.
- Maritime wake trigger recreated: `*/30 * * * *`.
- `install-jobs.sh` reinstalled all six jobs; the five HW2 jobs (`prep`, `notify`, `poll-a/b/c`)
  were then removed. Only `class-prep-forum` (`0 */3 * * *`, America/New_York) remains.
- First cycle: a one-time job `class-prep-forum-test` (`30 19 5 10 *`, same prompt, same flags)
  so the first post came from a scheduled run, not a manual one. Removed after it ran.

## Thread links

- 
<!-- one per post: date/time ET, link, reply or new thread -->

## Cycles

| Time (ET) | Job | Decision | Reason / entry id | Checked in Canvas |
| --- | --- | --- | --- | --- |
| 10-05 19:30 | `class-prep-forum-test` (one-time) | post (reply) | | |
| 10-05 21:00 | `class-prep-forum` | | | |

## Lost-ack test

- `forum status` before:
- Cycle with `FORUM_FAULT=lost_ack`, `FORUM_FAULT_OK=1` (time, error line):
- Fault env removed at:
- Next cycle, `reconciled` line (entry id):
- `forum status` after:
- Thread link showing one copy:

## Issues / changes during the run

- 
