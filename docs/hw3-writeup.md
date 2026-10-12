# HW3: Make Your Agent Autonomous (class-prep-agent on the agent forum)
MIT AI Studio (MAS.665) · Chris Ackerman · Repo: `chris-a-ackerman/syllabi-agent`
Setup: [`README.md`, "HW3: forum agent"](../README.md#hw3-forum-agent). Tool contract:
[`docs/tool-contract.md` §9](tool-contract.md#9-forum-skill-the-hw3-agent-forum-the-only-canvas-writes).

> **Status.** Everything below is built and unit-tested offline (`tests/test_forum.py`, fake
> Canvas, no network). It ran live on Maritime from Oct 5, 19:30 ET to Oct 7, 06:00 ET (13
> scheduled cycles, 8 automatic retries): 10 posts, 5 deliberate skips, and a live
> lost-acknowledgement test that ended with one copy of the post (§1, §4, §5).
> What is marked *observed* was seen in that run. What is marked *designed* is in the code and the
> tests but was not seen live.

## 1. Thread links

Forum: [Homework 3: Agent Discussion Forum](https://canvas.mit.edu/courses/40577/discussion_topics/448963)
(course 40577, topic 448963). Every post below was made by a scheduled cron cycle with no human
prompt. All are replies to other agents' entries, which the decision rule prefers over new
threads. Times are ET.

| # | Posted (ET) | Reply to entry | Post |
| --- | --- | --- | --- |
| 1 | Oct 5, 19:30 | 229515 | [entry 230213](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230213) |
| 2 | Oct 5, 21:00 | 229609 | [entry 230233](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230233) |
| 3 | Oct 6, 00:00 | 229788 | [entry 230300](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230300), the lost-ack test post (§5) |
| 4 | Oct 6, 04:30 | 230069 | [entry 230361](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230361) |
| 5 | Oct 6, 06:30 | 230313 | [entry 230384](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230384) |
| 6 | Oct 6, 09:00 | 230383 | [entry 230406](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230406) |
| 7 | Oct 6, 12:00 | 230408 | [entry 230481](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230481) |
| 8 | Oct 6, 15:00 | 230497 | [entry 230569](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230569) |
| 9 | Oct 6, 15:23 | 230599 | [entry 230615](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230615) |
| 10 | Oct 6, 21:00 | 230737 | [entry 230740](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230740) |

After the decision rule was narrowed (Oct 6, before 18:00 ET), the agent posted once (#10) and
deliberately skipped five times (§4).

## 2. Code and setup

Code: <https://github.com/chris-a-ackerman/syllabi-agent>. The forum work is one skill,
[`workspace/skills/forum/`](../workspace/skills/forum/SKILL.md), one cron prompt,
[`triggers/forum.md`](../triggers/forum.md), and the agent's talking points,
[`workspace/forum-notes.md`](../workspace/forum-notes.md). Step-by-step setup (token, secrets,
install, smoke test, pause, stop, tests) is the "HW3: forum agent" section of the README.

## 3. Architecture and autonomy

The HW2 agent (class-prep-agent, OpenClaw on Maritime) gained one more cron job. It takes part in
the *Homework 3: Agent Discussion Forum* on its own: every 3 hours it reads the forum, decides
whether it has something worth adding, and either posts once or records a skip. The model only
decides *whether* and *what* to post. Every control (pause switch, duplicates, rate limit,
verification, halt) is enforced in `forum.py`, not in the prompt, because HW2 showed that rules
held in a prompt are followed by some runs and not others, while rules held in a tool held every
time.

```
 Maritime wake (*/30, UTC)
          │
          ▼
 OpenClaw cron: class-prep-forum, 0 */3 * * * America/New_York  (isolated session, no human prompt)
          │  prompt = triggers/forum.md
          ▼
 ┌──────────── main agent (GPT-5.4, AGENTS.md hard rule 1 exception) ────────────┐
 │ 1 forum read ─► 2 control RUNNING? ─► 3 read forum-notes.md ─► 4 decide         │
 │                         │ no                                   │ post / skip    │
 │                         ▼                                      ▼                │
 │                    forum skip                     forum post --text-file …     │
 └──────────────────────────────────────────────┬───────────────────────────────────┘
                                                │ (only path that writes to Canvas)
                                                ▼
 forum.py: halt? ─► reconcile ─► control GET ─► text ─► target ─► dupes ─► rate
           ─► intent saved ─► POST (≤3, backoff) ─► verify GET ─► posts[]
                │                                                   │
                ▼                                                   ▼
 /data/memory/forum-state.json · forum-halt        https://canvas.mit.edu
 /data/logs/forum.jsonl                             course 40577, one topic
```

**Scheduler.** Two layers, as in HW2: a Maritime wake trigger every 30 minutes (a sleeping
container runs no cron), and the OpenClaw cron job `class-prep-forum` at `0 */3 * * *` in
America/New_York (00:00, 03:00, … 21:00), isolated session, 300 s timeout, installed by
`scripts/install-jobs.sh`. After install, no human prompt is needed. Cron runs are serialized, so
the 06:00, 09:00 and 21:00 cycles wait for the `poll` job that shares their minute.

**Canvas access.** `forum.py` (standard library only) talks to `https://canvas.mit.edu`, course
`40577`, one discussion topic. Every request path is built from `CANVAS_FORUM_COURSE_ID` and
`CANVAS_FORUM_TOPIC_ID`; no argument can point it anywhere else, and `_request()` refuses any
other path, host or method. The token comes from the `CANVAS_FORUM_TOKEN` environment secret,
travels only in a header over https to that host, and is never logged. The HW2 `canvas` skill
stays GET-only.

**Decision logic.** From `triggers/forum.md`, the agent posts only if at least one is true:
(a) a new entry asks a direct question that `forum-notes.md` answers with a specific fact, and
no other new entry has answered it yet; (b) a new entry replies to one of its posts and a
follow-up would add something new; (c) it has no posts yet and no existing thread fits, so it
starts one on a topic from the notes. Being related to the notes is not enough, and when unsure
it skips. Otherwise it runs `forum skip --reason "<why>"`. One post per cycle at most, 40–120
words of plain prose, replies preferred over new threads, and no claim that is not in the notes.
(Rule (a) was narrowed on Oct 6; the earlier, looser version posted every cycle, see §4.)

**Local persistent memory.** `/data/memory/forum-state.json` holds the ids already seen, the
agent's own posts (entry id, parent, text hash, URL), pending intents, post times, abandoned
intents, the consecutive-failure count and the last cycle time. It is written atomically under a
file lock and never silently replaced if corrupt (`FORUM_STATE`). `/data/logs/forum.jsonl` gets
one line per command: time, command, new-entry count, decision, reason, entry id, error code,
attempts. `forum read` returns only entries that are new, not deleted and **not the agent's own**,
so it never answers itself.

**Verification.** After every POST the tool GETs the saved entry (`entry_list?ids[]=<id>`) and
checks that it exists, belongs to the agent's user id and has the same normalized-text hash.
Only then does it count as posted (`verified: true`); otherwise `FORUM_VERIFY`, and the next
cycle reconciles it by id.

**Rate limits.** At most 3 posts in any trailing 60 minutes, counted as the larger of the post
times in memory and the agent's own entries visible in the forum, so losing the state file
cannot raise the limit (`FORUM_RATE`). The schedule allows at most one post per 3 hours anyway.
A failed POST is retried at most twice within a 45 s budget (2 s, then 4 s, or a 429's
`Retry-After`), and the forum is re-read before every retry, so a retry is never blind.

**Stopping rule.** The control line is fetched fresh before every write: the first line of the
topic must read `COURSE-TEAM CONTROL: RUNNING`. `PAUSED` blocks the post (`FORUM_PAUSED`), and
anything else, including a failed fetch, blocks it too (`FORUM_CONTROL_UNKNOWN`, fail closed).
Three failed commands in a row (not counting guards such as pause or rate limit) write
`/data/memory/forum-halt`, and a rejected token writes it at once. While it exists, every `read`
and `post` refuses without calling Canvas. Only a human removes it.

## 4. Supporting activity evidence

Two sources are joined here. The first is OpenClaw's run history for the job
(`openclaw cron runs`: start time, status, duration). The second is the tool's own log
(`forum.jsonl`, via `forum report`). Full redacted output is in
[`evidence/run-2026-10-05/`](../evidence/run-2026-10-05/) (`forum-report.md`,
`forum-log-excerpts.txt`, `forum-status.txt`). All times are ET (UTC−4).

**Phase 1: original decision rule** (Oct 5, 19:30 to Oct 6, 15:23)

| Run start (ET) | Trigger | Cron status | Forum tool | Outcome |
| --- | --- | --- | --- | --- |
| Oct 5, 19:02 | operator smoke test (manual) | n/a | `read`: 50 new | read only, before scheduling |
| Oct 5, 19:30 | one-time job `class-prep-forum-test` | ok | `read`, `post` | reply, entry 230213 |
| Oct 5, 21:00 | `class-prep-forum` | ok, 32 s | `read`, `post` | reply, entry 230233 |
| Oct 6, 00:00 | `class-prep-forum`, **fault injected** | ok, 35 s | `read`, `post` → `CANVAS_NET` | lost ack; agent did not retry (§5) |
| Oct 6, 03:00 | `class-prep-forum` | **timeout**, 1798 s | `reconcile` 230300, `read` | intent resolved, no new post; model call hung |
| Oct 6, 03:30 | OpenClaw retry | **timeout**, 1772 s | `read` | no post; model call hung |
| Oct 6, 04:30 | OpenClaw retry | ok, 39 s | `read`, `post` | reply, entry 230361 |
| Oct 6, 06:00 | `class-prep-forum` | **timeout**, 1792 s | `read` | no post; model call hung |
| Oct 6, 06:30 | OpenClaw retry | ok, 39 s | `read`, `post` | reply, entry 230384 |
| Oct 6, 09:00 | `class-prep-forum` | ok, 46 s | `read` (24 new), `post` | reply, entry 230406 |
| Oct 6, 12:00 | `class-prep-forum` | ok, 34 s | `read` (35 new), `post` | reply, entry 230481 |
| Oct 6, 15:00 | `class-prep-forum` | **timeout**, 1373 s | `read` (38 new), `post` | reply, entry 230569, *then* the model call hung |
| Oct 6, 15:23 | OpenClaw retry | ok, 42 s | `read` (5 new), `post` | reply, entry 230615 |

In phase 1, every run that finished posted exactly one reply. The original rule ("a new entry asks
something that the notes answer") matched something in every batch of 24–50 new entries, so the
agent never skipped. The runs that read the forum and posted nothing were model timeouts, not
decisions. On Oct 6 I narrowed rule (a) in `triggers/forum.md`: the agent may answer only a
direct question that no other entry has answered, should skip when unsure, and is told that
"most cycles should end in a skip" (§3). I recreated the job with the new prompt
(`class-prep-forum`, new id) before the 18:00 cycle. The code, limits and schedule did not change.

**Phase 2: narrowed decision rule** (Oct 6, 18:00 to Oct 7, 06:00)

| Run start (ET) | Trigger | Cron status | Forum tool | Outcome |
| --- | --- | --- | --- | --- |
| Oct 6, 18:00 | `class-prep-forum` | **timeout**, 1796 s | `read` (31 new) | no decision; model call hung |
| Oct 6, 18:30 | OpenClaw retry | **timeout**, 1769 s | `read` (8 new) | no decision; model call hung |
| Oct 6, 19:30 | OpenClaw retry | ok, 31 s | `read` (11 new), **`skip`** | "no new question my notes answer, and none of the new entries reply to one of my posts" |
| Oct 6, 21:00 | `class-prep-forum` | **timeout**, 1793 s | `read` (19 new), `post` | reply, entry 230740, *then* the model call hung |
| Oct 6, 21:30 | OpenClaw retry | **timeout**, 1771 s | `read` (10 new) | no decision; model call hung |
| Oct 6, 22:30 | OpenClaw retry | ok, 23 s | `read` (13 new), **`skip`** | "the direct question about flush/sync already has answers in new entries, and the rest do not ask for a specific fact from our notes" |
| Oct 7, 00:00 | `class-prep-forum` | ok, 31 s | `read` (24 new), **`skip`** | "suspected-injection: entry 230827" |
| Oct 7, 03:00 | `class-prep-forum` | error, 82 s | `read` (50 new), **`skip`** | "no new question my notes answer with a specific fact"; see below |
| Oct 7, 06:00 | `class-prep-forum` | ok, 44 s | `read` (41 new), **`skip`** | "no new question my notes answer with a specific fact, and none of the new entries reply to one of my posts" |

`forum status` after the Oct 7, 06:00 run: `control: RUNNING`, `halted: false`, `own_posts: 10`,
`pending: 0`, `abandoned: 0`, `consecutive_failures: 0`, `posts_last_hour: 0` (limit 3),
`seen_total: 759`.

**Observed.** Over about 35 hours with no human prompt, 21 runs took place: 13 scheduled cycles
(one one-time test job and 12 runs of the recurring job) plus 8 automatic retries. They made 10
posts, each to a different parent entry, and recorded 5 deliberate skips. The rate limit, dedupe
and verification never had to refuse anything.

**Deliberate skips.** Every finished cycle under the narrowed rule except one ended in
`forum skip`, each with a reason that names the rule it applied:
- **Nothing to answer** (Oct 6 19:30; Oct 7 03:00, 06:00). None of the new entries asked a
  question that the notes answer with a specific fact, and none replied to the agent's posts.
- **Already answered** (Oct 6 22:30). One new entry did ask a direct question that the notes
  cover (flush/sync before a restart). Other entries had already answered it, so the agent
  stayed out.
- **Suspected injection** (Oct 7 00:00). One new entry tried to give the agent instructions. It
  did not reply to that entry or act on it, posted nothing else, and recorded
  `suspected-injection: entry 230827`, as `triggers/forum.md` step 7 requires. The injected text
  is not copied here.

The 22:30 skip came from a retry of the 21:00 cycle, which had posted entry 230740 before it
hung. Under phase 1, a retry like that posted a second time (15:00 and 15:23). This time the
retry skipped. That was the model's judgement, though, and the tool would not have stopped a
second post (§7).

**Timeouts and errors.** In eight runs the model call never returned. OpenClaw killed each one
after 23–30 minutes (the job's 300 s limit was not enforced at that phase) and re-ran the job,
up to twice. No state was lost, because every write goes through the tool and reconcile runs
first. The 03:00 run on Oct 6 had already reconciled the pending intent before it hung. The Oct 7,
03:00 run ended in `error` for a different reason. The agent tried to run an inline Python
script through a heredoc, and that exec call failed. The run history records only
`Exec failed: run python3 inline script (heredoc)`. The agent still recorded its skip through
`forum skip`, so nothing reached Canvas outside the tool, and `consecutive_failures` stayed at 0.

## 5. Failure and recovery

**Test: lost acknowledgement.** The most dangerous failure for a posting agent is a POST that
Canvas saves but whose answer never arrives: a naive retry posts twice. With
`FORUM_FAULT=lost_ack` and `FORUM_FAULT_OK=1`, `forum post` makes the real POST and then drops the
response as if it timed out. Expected (*designed*; unit-tested): the post exits with `CANVAS_NET`
and the intent stays `pending` in `forum-state.json`; on the next cycle, `forum read` (or
`forum post`) reconciles first, finds the agent's own entry with the same parent and content hash,
records it as posted (`reconciled: true`, a `reconcile` line in `forum.jsonl`), and posts nothing.
The forum then shows exactly one copy. Both fault values are logged, and without
`FORUM_FAULT_OK=1` the hook is ignored.

**Observed live (Oct 6).** `FORUM_FAULT=lost_ack` and `FORUM_FAULT_OK=1` were set on the agent
before the 00:00 ET cycle and removed after it. From `forum.jsonl`
([`forum-log-excerpts.txt`](../evidence/run-2026-10-05/forum-log-excerpts.txt)):

```
{"command": "read",      "fault": "lost_ack", "fault_ok": true, "new_count": 50, "ts": "2026-10-06T04:00:17Z"}
{"command": "post",      "decision": "error", "error_code": "CANVAS_NET", "attempts": 1, "fault": "lost_ack", "fault_ok": true,
                         "reason": "no response to the POST (FORUM_FAULT=lost_ack); the next command reconciles it", "ts": "2026-10-06T04:00:30Z"}
{"command": "reconcile", "decision": "post", "entry_id": 230300, "reason": "reconciled", "ts": "2026-10-06T07:00:15Z"}
```

1. **Injected failure.** Canvas saved the reply to entry 229788. The tool dropped the response
   and returned `CANVAS_NET` with the intent still `pending`.
2. **Agent behaviour.** The cron run summary for that cycle reads: *"Tried one forum reply this
   cycle, to entry `229788`, and the post returned `CANVAS_NET` with a lost acknowledgement. I
   did not retry. The next cycle will reconcile it."* That follows trigger step 6.
3. **Recovery.** At the start of the next scheduled cycle (03:00 ET), the tool reconciled before
   anything else. It found its own entry under 229788 with the same content hash and recorded it
   as entry 230300 (`reconciled: true`). It made no POST.
4. **No duplicate.** There was one POST and one entry:
   [entry 230300](https://canvas.mit.edu/courses/40577/discussion_topics/448963?entry_id=230300).
   `forum report` lists it once, and later status shows `pending: 0`, `abandoned: 0`. The
   one-reply-per-parent guard would also have refused a second reply to 229788 had the model
   tried one.

**Offline tests** (`tests/test_forum.py`, fake Canvas; `python3 -m unittest discover -s tests`):

| Case | Tests |
| --- | --- |
| Timeout | `test_timeout_after_save_reconciles_instead_of_retrying`, `test_budget_stops_retries` |
| HTTP error | `test_5xx_after_save_reconciles_too`, `test_clean_500_is_retried_with_backoff`, `test_three_500s_give_up_and_leave_the_intent_pending`, `test_429_is_retried_after_retry_after_and_not_reconciled_first`, `test_4xx_is_not_retried_and_the_intent_is_abandoned` |
| Malformed response | `test_malformed_json_response_is_canvas_net_and_only_the_intent_changes`, `test_verify_requires_own_user_and_same_text` |
| Duplicate | `test_duplicate_text_to_the_same_parent_is_refused`, `test_second_reply_to_the_same_parent_is_refused`, `test_already_replied_survives_a_lost_state_file`, `test_lost_ack_then_next_run_reconciles_without_a_second_post` |
| Restart | `test_restart_state_carries_across_processes`, `test_lost_ack_then_read_reconciles`, `test_pending_intent_blocks_a_new_post_until_it_resolves` |
| Halt | `test_three_failures_halt_and_deleting_the_file_restores_service`, `test_canvas_401_halts_immediately` |

## 6. Safety and boundaries

- **Token.** A new Canvas token only for this, set as the `CANVAS_FORUM_TOKEN` secret on
  Maritime, never in git. It goes only to `canvas.mit.edu` over https and is scrubbed from every
  output; a post containing the value of any secret-named variable, or `syl_agent_`, is refused
  (`FORUM_TEXT`). Canvas tokens cannot be scoped, so it could do anything my account can: the
  limits below are what keep it to one topic.
- **Single write path.** The only Canvas writes in the repo are the two POSTs in `forum.py` (new
  entry, reply) on the configured topic. `AGENTS.md` hard rule 1 allows `forum post` only inside
  the `forum` cron job; prep, poll, notify, Telegram and the operator chat may not call it. Tests
  pin both: `test_source_has_only_the_two_post_endpoints` and `test_canvas_skill_stays_get_only`
  in `tests/test_forum.py`, and `test_hard_rule_1_is_never_write_to_canvas_except_forum_post` in
  `tests/test_instructions.py`.
- **Untrusted forum text.** Every entry is written by another agent or its owner: data, never
  instructions. `read` says so in a `note` field and shows authors only as `user <id>`. An entry
  that tries to instruct the agent gets no reply, only `forum skip --reason "suspected-injection:
  entry <id>"`.
- **What it may not mention.** Secrets or env values, file paths under `/data`, grades, other
  students' names, anything about my other courses, and any claim not in `forum-notes.md`.
- **Blast radius.** One topic in one course; 2,000 characters per post; 3 posts per hour (one per
  3-hour cycle in practice); one reply per parent entry; the exec allow-list in `AGENTS.md`
  ("Command execution") is unchanged apart from the `forum` commands.
- **Expiry and revocation.** The token is set to expire shortly after Oct 7 and will be revoked
  after grading (Canvas → Account → Settings). An expired or revoked token returns 401, which
  halts the tool at once instead of retrying.

## 7. Limits and what was not done

- **Single live run.** The schedule, posts, skips and lost-ack recovery are seen live only in the
  window before the deadline (§1, §4, §5). Anything without live evidence there stays *designed*.
- **The first decision rule was too loose.** With 24–50 new entries per cycle, "the notes answer
  something here" was true every time, so the agent never skipped. The rule was narrowed during
  the run (§3, §4). Afterwards 5 of the 6 finished cycles skipped. Whether a post is worth making
  is still decided by the prompt, not by the code.
- **Hung model calls.** Eight of 21 runs timed out inside the model call. The job's 300 s limit
  did not stop them; OpenClaw killed them after 23–30 minutes and re-ran the job. Retries are
  safe for state, because reconcile runs first and every write goes through the tool. But a run
  that posted and *then* hung was retried and posted again (Oct 6, 15:00 and 15:23 ET). The same
  thing happened again at 21:00, but that time the retry chose to skip. "One post per
  cycle" holds only per run. The fix belongs in the tool: refuse a second post within the same
  3-hour slot. That fix is not built.
- **Judgement is still the model's.** The script guarantees how often and where it posts, not
  whether a post is useful. The quality of the decision depends on GPT-5.4 and on
  `forum-notes.md`, which is a fixed list of lessons from this repo.
- **No alert on halt.** When the halt file is written, the forum job stops quietly; nothing
  messages me. I have to check `forum status`. The same gap as HW2's "nothing alerts me when a run
  dies".
- **The control line is the only pause switch the agent sees.** If the course team pauses the
  forum some other way (locking the topic), posts fail with a Canvas error and count toward the
  halt instead of being treated as a pause.
- **The token is not scoped.** The one-topic limit is enforced by my code, not by Canvas. A
  scoped token or an LTI tool would remove the risk rather than guard against it.
- **Not tested live:** the 3-failure halt, a rejected token, and the course team's PAUSED line.
  All three are covered only by the offline tests.
