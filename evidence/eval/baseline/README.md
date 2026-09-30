# Baseline (eval case 1), 2026-09-30

Same Maritime agent, sent over `maritime chat` at 11:38:02 ET; the reply arrived at 11:41:22 ET.
Prompt:

> BASELINE EVAL. Ignore your prep/poll/notify workflow, memory files and every skill except the canvas skill. Do not use preplog, nlm, drive, brief, the brief-writer subagent or Telegram. Prepare me for 15.662 (Canvas course 38529) on 2026-09-30. Write your full answer to /data/work/eval/baseline-case1.md, and at the end of that file list every tool call you made and how many.

Answer: [`baseline-case1.md`](baseline-case1.md). Tool calls are the agent's own count (it says
10; its itemised list adds up to 15). Wall-clock is the chat round-trip. Quality 3/5 (**graded by
Claude Code; Chris to confirm**): the readings and the discussion questions match what it says
Canvas shows, but it couldn't read the slides, it paraphrases the questions into bullets rather
than quoting them, and I didn't have time to check it against Canvas myself. Case 4 (same prompt
again) was not run.
