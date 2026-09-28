# HW2: Engineer a Reliable Agent (class-prep-agent)

MIT AI Studio (MAS.665). Plan of record: [`README.md`](../README.md).

## Overview

<!-- 1 paragraph: what the agent does, where it runs (OpenClaw on Maritime), and the timing rule. -->

## 1. Tools

<!-- canvas, syllabi, nlm-prep / nlm-status, drive-put, Telegram. Why each exists, and the error
     contract (docs/tool-contract.md). Point to a 401 vs 403 example. -->

## 2. Memory

<!-- /data/memory/prep-log.json (schema, state machine, never-redo guarantees) and course-notes.md.
     Show a before/after record from a real run. -->

## 3. Agent loop

<!-- Three phases (prep / poll / notify) as OpenClaw cron jobs in ET, woken by one Maritime */30
     trigger: why two layers (sleeping container; Maritime doesn't mirror OpenClaw's SQLite job store;
     CLI triggers carry no prompt/tz). The 30 s budget and start/poll split, stopping conditions,
     the four ask-a-human cases, run logs. Evidence: docs/deploy-maritime.md verification log. -->

## 4. Subagent

<!-- brief-writer: bounded context (~40k tokens), no tools, strict JSON, schema validation,
     the fuzzy ≥ 0.9 check against Canvas text, re-prompt once then partial. Hallucination log examples.
     Implementation: workspace/skills/brief (SYL-96): `bundle` (cap, even split, truncation notes,
     sidecar with the Canvas text), `validate` (JSON extraction, schema subset, difflib ratio vs whole
     Canvas sentences/lines with exact numbers/negations/content words, HALLUCINATION lines, reprompt
     on attempt 1 / partial on attempt 2), `format` (Telegram text, drafts marked); workspace/skills/
     pdf-text (PDF → text for the bundle). Evidence so far: tests/test_brief.py (the forged
     "### CANVAS:" / "---" tests are the injection cases; FuzzyMatchTests are planted near-miss
     questions). A brief-validation.json from a live run is still to be captured. -->

## 5. Failure recovery

<!-- Walk through the expired-NotebookLM-cookie case end to end, with log and Telegram screenshots
     (evidence/failures/). Canvas 403 fallback.
     Deploy-time failures worth a paragraph (docs/deploy-maritime.md):
     - the model asked for /approve with approvals off, which would have stalled every unattended run;
       fixed by the pre-authorization section in AGENTS.md;
     - a scheduled job silently never fired because Maritime didn't see it; found by sleeping the
       agent and testing; fixed with an explicit wake trigger. -->

## 6. Evaluation

<!-- Baseline vs improved on the 5 cases (evidence/eval/cases.md). Table + discussion.
     Brief-quality rubric (1–5): 5 = accurate, complete, actionable, questions verbatim; 1 = wrong/unusable. -->

## What's still manual

- **Goodnotes import:** one tap per reading. Goodnotes has no server API; the agent files PDFs in
  Drive and the iPad Files app does the rest.
- **Readings behind logins** (HBS cases, library proxy): Chris downloads them and drops them in the
  Drive folder when asked.
- **Submitting pre-class questions:** Chris reviews the drafts and submits them. The agent never
  writes to Canvas.
- **Refreshing NotebookLM cookies** when they expire (unofficial API, no refresh token).
- **Re-authorizing rclone / Canvas tokens** when they expire or are revoked.

## Limitations and future work

<!-- -->
