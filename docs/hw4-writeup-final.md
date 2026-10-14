# HW4: Make Your Agent Hire Another Agent

MIT AI Studio · Chris Ackerman · agent: class-prep-agent · code:
[chris-a-ackerman/syllabi-agent](https://github.com/chris-a-ackerman/syllabi-agent) ·
evidence: [`evidence/hw4-run-2026-10-14/`](https://github.com/chris-a-ackerman/syllabi-agent/tree/main/evidence/hw4-run-2026-10-14)

> **Status (state as of Oct 14, 08:48 ET, commit `786fe88`).** The agent posted its offer, chose
> four providers in turn by capability, sent four requests, checked two deliveries, posted a
> correction request and a rejection, and recorded one timeout and one decline. **No delivered work
> has passed the checks yet**, so nothing has been adopted. The fourth request
> ([entry 233001](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-233001)) is
> open until Wed Oct 14, 10:47 PM ET. No agent sent a request to our offer.
> *Observed* = seen in the live run (state/, logs, forum). *Designed* = in code and offline
> tests, not seen live.

## 1. Task and service

**Offer** ([entry 232462](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232462)):
*syllabus to calendar and workload estimate*. A requester pastes 500 to 12,000 characters of
syllabus text; the agent replies with a table of dated events, an iCalendar block, and hours per
week with the heaviest week flagged. The model only extracts events; each event must quote the
line its date came from, and code
([`syllabus_calendar.py`](https://github.com/chris-a-ackerman/syllabi-agent/blob/main/workspace/hw4/services/syllabus_calendar.py))
drops any event whose quote is not in the input verbatim or does not name that month and day,
then builds and re-parses the `.ics`. (An earlier offer, entry 232454, was retired 18 minutes
after posting when the service changed; it is the only other offer post.)

**Delegated task:** unit tests for `control_state()`, the 15-line function that reads the course
team's control line and decides whether the agent may write at all. The request
([template](https://github.com/chris-a-ackerman/syllabi-agent/blob/main/workspace/hw4/task/control_state_tests/request.md))
is self-contained: the function's source, the expected result (one stdlib `unittest` file, class
`TestControlState`, `from control_state import control_state`), the four checks below, and a
24-hour deadline.

**Choosing a provider (observed).** Each choice is one model call that scores every untried offer
0 to 3 against the need ("Python unit tests, stdlib, a file I can run"); code takes the best with
fit ≥ 2 and rejects an answer that names a non-candidate (this happened once, Oct 12 14:25). No
provider is hard-coded; one that times out, declines or fails is added to `tried_providers` and
never asked again. The `reason` recorded in state for each choice, verbatim:

| Time (ET) | Offers | Chosen | Recorded reason |
| --- | --- | --- | --- |
| Oct 12, 14:41 | 26 | [231986](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=231986) | "Offer 3 is the only one about designing Python unit-test cases for a short pure-Python function, though it focuses on text-normalization and proposes cases rather than clearly returning a runnable file." |
| Oct 13, 14:44 | 37 | [232490](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232490) | "Offer 27 writes and runs a pytest suite for a user's Python function and returns it, matching the need for runnable Python unit tests; it uses pytest rather than stdlib unittest, which is a minor mismatch." |
| Oct 13, 20:48 | 43 | [232883](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232883) | "Offer 39 is the only one that produces test vectors with expected results for code, but it is limited to interval-merging functions and does not say it returns a runnable Python file, so it is a partial fit." |
| Oct 13, 22:47 | 50 | [232960](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232960) | "Offer #45 is the only one that tests Python code: it mutation-tests a Python function against a pytest suite, which is the closest match to reviewing and testing code, though it may not write the unit tests or stay in stdlib-only Python." |

Every other offer scored 0 or 1 (full rankings in the evidence report). No offer was a clean fit.

## 2. Technical setup

```
 GitHub Actions hw4-forum.yml: cron every 2 h + on-demand runs, 20-min timeout, no overlap
   | checkout main (state/ = memory); docker pull python:3.12-slim (step has no secrets)
   v
 scripts/hw4-cycle.py cycle --- model.py (Anthropic Messages API, structured JSON output)
   |  read > classify > offer once > serve one job > check one result
   |       > verdict (accept / correct / reject) > rank offers > request
   |  Canvas reads/writes                   delivered tests
   v                                       v
 forum.py: control line, rate limit,      sandbox.py: docker --network none,
 dedupe, read-back, halt -> canvas.mit.edu   read-only, 512 MB, 60 s, empty env
   v
 git commit state/ to main ("hw4: state after run <id>")
```

- **Harness:** [`hw4-forum.yml`](https://github.com/chris-a-ackerman/syllabi-agent/blob/main/.github/workflows/hw4-forum.yml)
  runs one cycle of [`hw4-cycle.py`](https://github.com/chris-a-ackerman/syllabi-agent/blob/main/scripts/hw4-cycle.py)
  on a fresh runner. 25 runs so far (6 from GitHub's cron, 19 started on demand), 23 green.
- **Model:** the Anthropic Messages API through `workspace/hw4/model.py`, configured default
  `claude-sonnet-5-5` at low effort. The model classifies entries, ranks offers and extracts
  syllabus events; every check and every decision to post is code.
- **Canvas:** only through `forum.py`, with a Canvas token stored as a repo secret.
- **Memory:** `state/` is [committed back to main](https://github.com/chris-a-ackerman/syllabi-agent/commits/main/state)
  after every run: entries seen, own posts, the open request and its deadline, `tried_providers`,
  every ranking and verdict. Replies are handled with no human relay: each new entry is classified
  (result, question, decline, request for our service, chatter) and drives a small state machine
  (offer → discovery → request → result → verdict, with a deadline at each step). *Observed:*
  the timeout (Oct 13 14:43), the decline (232963, Oct 13 22:47) and both verdicts were picked
  up by later runs on their own, and no provider was asked twice.

**Why not Maritime/OpenClaw.** In HW3, 8 of 21 runs hung inside the model call; the job's 300 s
limit was not enforced and runs were killed after 23-30 minutes, and a run that posted then hung
was retried and posted again. HW4 runs as a plain script with a hard 20-minute job timeout and
per-call limits. The cost: rebuilding the harness and memory as committed files, losing
OpenClaw's agent loop (the driver is a fixed state machine with model calls inside), and two
failed first runs (Oct 12, model-call errors: empty answers, then unparsed output), fixed the
same day; GitHub's cron also
stopped firing while state commits carried `[skip ci]` until that was removed.

## 3. Results and verification

**Checks (observed in code; ran live on two deliveries).** A delivery is extracted from the reply
and checked mechanically: (static) it parses, has one `TestCase` with ≥ 3 tests, imports only the
stdlib allow-list, no file/network/subprocess/`eval`; (correct) all tests pass on the real
function; (m1) at least one fails on a mutant that reports the PAUSED line as RUNNING; (m2) at
least one fails on a mutant that skips stripping and upper-casing. Runs happen in docker with
`--network none`, a read-only filesystem, and an env built from scratch, so neither secret reaches
peer code ([tool contract §10](https://github.com/chris-a-ackerman/syllabi-agent/blob/main/docs/tool-contract.md#10-hw4-4-checking-delivered-work-sandboxpy-checkspy-a-tasks-verifypy)).
All pass → accept, tip 5, adopt as `tests/test_hw4_delegated_control_state.py`; first failure →
one correction request (+12 h); second failure → reject, no tip.

| Request | Provider | Outcome | Checks (from state) |
| --- | --- | --- | --- |
| [232463](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232463) | 231986 | **timed out** Oct 13 14:43, no reply | - |
| [232849](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232849) | 232490 | result [232899](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232899) → **correction requested**; result [232946](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232946) → **rejected**, tip 0 | static **fail** both times: "imports pytest, which is not on the allow-list"; correct/m1/m2 skipped |
| [232951](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232951) | 232883 | **declined** by provider (232963) | - |
| [233001](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-233001) | 232960 | **open** until Oct 14, 10:47 PM ET | - |

The saved deliveries ([first](https://github.com/chris-a-ackerman/syllabi-agent/blob/main/state/work/hw4/result-232899.py),
[second](https://github.com/chris-a-ackerman/syllabi-agent/blob/main/state/work/hw4/result-232946.py))
show why: the first tested a different function (`merge_intervals`), the second tested
`control_state` but with pytest and `from solution import …`. The static check stops at the first
failure, so only the pytest import was reported.

**Unfinished.** No delivery has passed, so `tests/test_hw4_delegated_control_state.py` does not
exist and nothing was adopted or tipped. The accept path (sandbox run, tip, adoption) is
*designed*: covered offline by `tests/test_hw4_verify.py`, including one real docker run on a
known-good fixture, but not seen live. If request 233001 is answered before its deadline, later
runs check it with no human involved.

**Provider side.** No agent replied to our offer with a syllabus, so no job was served live. The
date-quote check is *designed* and covered offline (`tests/test_hw4_provider.py`).

## 4. Canvas discussion links

Forum: [Homework 4 topic 449948](https://canvas.mit.edu/courses/40577/discussion_topics/449948). All posts were made by scheduled runs.

| What | Time (ET) | Entry |
| --- | --- | --- |
| Our service offer | Oct 12, 14:41 | [232462](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232462) |
| Request 1 (to offer 231986), timed out | Oct 12, 14:41 | [232463](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232463) |
| Request 2 (to offer 232490) | Oct 13, 14:44 | [232849](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232849) |
| Returned work, attempt 1 | read Oct 13, 18:42 | [232899](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232899) |
| Our verification reply: correction requested | Oct 13, 18:43 | [232923](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232923) |
| Returned work, attempt 2 | read Oct 13, 20:48 | [232946](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232946) |
| Our verification reply: rejected, no tip | Oct 13, 20:48 | [232950](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232950) |
| Request 3 (to offer 232883) | Oct 13, 20:48 | [232951](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-232951) |
| Provider's decline | read Oct 13, 22:47 | [232963](https://canvas.mit.edu/courses/40577/discussion_topics/449948?entry_id=232963) |
| Request 4 (to offer 232960), open | Oct 13, 22:48 | [233001](https://canvas.mit.edu/courses/40577/discussion_topics/449948#entry-233001) |

No tip was posted, since nothing was accepted.

## 5. Safety controls

- **Credentials:** the Canvas token and API key are repo secrets, passed only to the one `cycle`
  step; the image pull, write-up and commit steps get none. No `set -x` or env dumps; `forum.py`
  refuses any post containing a secret's value and scrubs logs. Peer tests run with an empty env.
- **Untrusted peer text:** capped, wrapped in random-nonce delimiters and labelled data; model
  outputs are schema-constrained and re-checked (the non-candidate answer above was rejected). Peer
  code runs only in the sandbox, after static checks; nothing else a peer sends is executed.
- **Control line and limits:** `COURSE-TEAM CONTROL: RUNNING` is read fresh before every write
  (PAUSED or unreadable blocks it); at most 3 write attempts per rolling hour, retries included,
  and 2 posts per cycle. *Observed:* 8 posts in 2 days, at most 2 in any run, the control
  line read `RUNNING` in all 25 runs.
- **Verified, no duplicates:** every post is read back (author and text hash) before it counts;
  ambiguous posts are reconciled, never blindly retried; a hash and one-reply-per-parent guard
  block duplicates. *Observed:* all 8 posts `verified: true`, 0 pending, 0 abandoned, no retries.
- **Stopping:** 3 consecutive failures or a rejected token write a halt file that blocks all
  posting until a human deletes it; 20-minute job timeout. *Observed:* consecutive failures never
  above 0; no halt.
- **Not tested live:** PAUSED line, rate-limit refusal, lost-ack reconcile, halt, and the accept
  path. These are covered only by offline tests (`tests/test_forum.py`, `tests/test_hw4_*.py`).
