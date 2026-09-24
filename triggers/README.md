# Maritime triggers

All scheduling is Maritime cron triggers. The container sleeps, so in-process timers never fire.
Each trigger delivers the prompt text from its file: everything below the `---` line, starting at
`[trigger: …]`. Add them with, for example,
`maritime trigger add --type cron --schedule "<expr>"` and the prompt from the file. Check the
flags against `maritime trigger --help`.

Design times are **America/New_York**.

| Trigger | ET schedule | Prompt |
| --- | --- | --- |
| `prep` | 19:00 daily | [`prep.md`](prep.md) |
| `poll` | every 30 min, 19:30–23:00 and 05:30–09:00 (**skips 06:30**) | [`poll.md`](poll.md) |
| `notify` | 06:30 daily | [`notify.md`](notify.md) |

`poll` skips 06:30 so it cannot run at the same time as `notify`. It also starts at 19:30, after
`prep`. Sends are idempotent anyway (`brief_sent_at` / `podcast_sent_at`).

## If Maritime triggers accept a timezone (preferred)

Set the timezone to `America/New_York` and use these, with no DST maintenance:

| Trigger | Cron (ET) |
| --- | --- |
| prep | `0 19 * * *` |
| notify | `30 6 * * *` |
| poll (a) | `30 5,19 * * *` |
| poll (b) | `0 6,9,23 * * *` |
| poll (c) | `0,30 7-8,20-22 * * *` |

## If triggers are UTC-only

New York is **UTC−4 (EDT) until Sun 2026-11-01 02:00**, then **UTC−5 (EST) until Sun 2027-03-14**.
UTC cron can't follow DST, so swap sets on those dates. The agent always reasons in ET, so an
hour of drift only shifts timing and does not break logic.

### EDT: now through 2026-10-31 (and again from 2027-03-14)

| Trigger | Cron (UTC) | Fires at (ET) |
| --- | --- | --- |
| prep | `0 23 * * *` | 19:00 |
| notify | `30 10 * * *` | 06:30 |
| poll (a) | `30 9,23 * * *` | 05:30, 19:30 |
| poll (b) | `0 3,10,13 * * *` | 23:00, 06:00, 09:00 |
| poll (c) | `0,30 0-2,11-12 * * *` | 20:00–22:30, 07:00–08:30 |

### EST: 2026-11-01 through 2027-03-13

| Trigger | Cron (UTC) | Fires at (ET) |
| --- | --- | --- |
| prep | `0 0 * * *` | 19:00 (previous ET day) |
| notify | `30 11 * * *` | 06:30 |
| poll (a) | `30 0,10 * * *` | 19:30, 05:30 |
| poll (b) | `0 4,11,14 * * *` | 23:00, 06:00, 09:00 |
| poll (c) | `0,30 1-3,12-13 * * *` | 20:00–22:30, 07:00–08:30 |

Under EST, UTC midnight is 19:00 ET on the *previous* calendar day. The prompts tell the agent
to derive "today" from ET, never from the trigger's clock.

Poll is split into three expressions because one cron line can't express two half-hour windows
that each start and end on a different half hour. Total poll firings per day: 15
(8 evening + 7 morning; 06:30 belongs to `notify`).
