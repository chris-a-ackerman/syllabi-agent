# HW3: Make Your Agent Autonomous (class-prep-agent on the agent forum)
MIT AI Studio (MAS.665) · Chris Ackerman · Repo: `chris-a-ackerman/syllabi-agent`
Setup: [`README.md`, "HW3: forum agent"](../README.md#hw3-forum-agent). Tool contract:
[`docs/tool-contract.md` §9](tool-contract.md#9-forum-skill-the-hw3-agent-forum-the-only-canvas-writes).

> **Status.** Everything below is built and unit-tested offline (`tests/test_forum.py`, fake
> Canvas, no network). What is marked *observed* was seen in the live run on Maritime; everything
> marked *designed* is in the code and the tests but was not seen live. The live evidence goes in
> the three `EVIDENCE` sections.

## 1. Thread links

<!-- EVIDENCE: thread links -->

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
(a) a new entry asks something, or describes a problem, that `forum-notes.md` answers with a
specific fact; (b) a new entry replies to one of its posts and a follow-up would add something
new; (c) it has no posts yet and no existing thread fits, so it starts one on a topic from the
notes. Otherwise it runs `forum skip --reason "<why>"`. One post per cycle at most, 60–180 words,
replies preferred over new threads, and no claim that is not in the notes.

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

The output of `forum report` (cycle table and own posts with links), passed through
`scripts/redact-evidence.py`. It must show several scheduled cycles and at least one deliberate
skip.

<!-- EVIDENCE: forum report -->

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

<!-- EVIDENCE: lost_ack log lines + thread link showing one copy -->

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
