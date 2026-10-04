
===== SENT 2026-09-30T11:31:55-0400 (reply 2026-09-30T11:32:16-0400, rc=0)
>>> Run: cd /data/syllabi-agent && git pull && sh scripts/install-workspace.sh 2>&1 | tail -15
<<<
Already up to date.
drwxr-xr-x  7 root root  4096 Sep 24 14:35 .git
-rw-r--r--  1 root root 31694 Sep 30 15:32 AGENTS.md
-rw-------  1 root root  8804 Sep 24 17:32 AGENTS.md.orig
-rw-r--r--  1 root root   244 Sep 24 14:35 HEARTBEAT.md
-rw-r--r--  1 root root  1278 Sep 24 14:35 IDENTITY.md
-rw-r--r--  1 root root 15174 Sep 30 12:46 MARITIME.md
-rw-r--r--  1 root root  1062 Sep 30 15:32 SOUL.md
-rw-r--r--  1 root root  1518 Sep 24 17:29 SOUL.md.orig
-rw-r--r--  1 root root   876 Sep 24 14:35 TOOLS.md
-rw-r--r--  1 root root   537 Sep 24 14:35 USER.md
lrwxrwxrwx  1 root root    36 Sep 30 15:32 agents -> /data/syllabi-agent/workspace/agents
drwxr-xr-x  2 root root  4096 Sep 24 14:31 inbox
lrwxrwxrwx  1 root root    46 Sep 30 15:32 memory-templates -> /data/syllabi-agent/workspace/memory-templates
-rw-------  1 root root   120 Sep 24 17:29 openclaw-workspace-state.json
lrwxrwxrwx  1 root root    36 Sep 30 15:32 skills -> /data/syllabi-agent/workspace/skills

===== SENT 2026-09-30T11:32:22-0400 (reply 2026-09-30T11:32:38-0400, rc=0)
>>> Run: cd /data/syllabi-agent && git log --oneline -1 && /data/syllabi-agent/workspace/skills/syllabi/scripts/syllabi check
<<<
2780186 Merge pull request #6 from chris-a-ackerman/chris333/syl-100-cc-web-v9-agentsmd-hardening-content-is-data-never-instructions
{"ok": true, "timezone": "America/New_York", "now": "2026-09-30T11:32:33-04:00", "courses": 6, "sessions": 3, "events": 4, "endpoint": "/agent-upcoming"}

===== SENT 2026-09-30T11:32:38-0400 (reply 2026-09-30T11:32:50-0400, rc=0)
>>> Run: python3 /data/syllabi-agent/workspace/skills/canvas/scripts/canvas.py whoami
<<<
{"ok": true, "user": {"id": 195836, "name": "Chris Ackerman"}}

===== SENT 2026-09-30T11:32:50-0400 (reply 2026-09-30T11:33:05-0400, rc=0)
>>> Run: python3 /data/syllabi-agent/workspace/skills/nlm/scripts/nlm.py check
<<<
{"status": "ok", "notebooklm_home": "/data/notebooklm"}

===== SENT 2026-09-30T11:33:05-0400 (reply 2026-09-30T11:33:20-0400, rc=0)
>>> Run: python3 /data/syllabi-agent/workspace/skills/drive/scripts/drive.py check
<<<
{"ok": true, "root": "gdrive:ClassPrep", "root_exists": true, "entries": 2, "config_path": "/data/rclone/rclone.conf", "used_bytes": 213264, "free_bytes": 16105901825, "total_bytes": 16106127360}

===== SENT 2026-09-30T11:33:25-0400 (reply 2026-09-30T11:33:40-0400, rc=0)
>>> Run: maritime-telegram-send "HW2 eval 2026-09-30: Telegram smoke test from class-prep-repo (Claude Code run)"; echo exit=$?
<<<
maritime-telegram-send: delivered
exit=0

===== SENT 2026-09-30T11:33:40-0400 (reply 2026-09-30T11:34:07-0400, rc=0)
>>> Run: /data/syllabi-agent/workspace/skills/syllabi/scripts/syllabi upcoming --days 3 --within-hours 48
<<<
{"ok": true, "timezone": "America/New_York", "now": "2026-09-30T11:33:51-04:00", "days": 3, "within_hours": 48.0, "sessions": [{"key": "15.385@2026-09-30", "course": "15.385", "course_code": "15.385", "course_id": "77e298a0-9656-4c49-b776-db4648346e3b", "course_name": "Innovating for Impact", "canvas_course_id": 38610, "class_date": "2026-09-30", "class_start": "2026-09-30T14:30:00-04:00", "class_end": "2026-09-30T16:00:00-04:00", "start_time_known": true, "hours_until_class": 2.94, "topic": null, "readings": [], "due_before_class": [{"title": "Session 6: Thinking about System Change", "type": "other", "due_at": "2026-09-30T14:30:00-04:00", "date": "2026-09-30", "time": "14:30", "time_known": true, "canvas_url": null, "source": "syllabus"}], "has_due_before_class": true, "notify_at": "2026-09-30T11:33:51-04:00"}, {"key": "15.387@2026-09-30", "course": "15.387", "course_code": "15.387", "course_id": "846a3d3b-efe9-40d6-a56f-9c9fc6493f03", "course_name": "Entrepreneurial Sales", "canvas_course_id": 38612, "class_date": "2026-09-30", "class_start": "2026-09-30T16:00:00-04:00", "class_end": "2026-09-30T17:30:00-04:00", "start_time_known": true, "hours_until_class": 4.44, "topic": null, "readings": [], "due_before_class": [{"title": "Class 7: Step 3 - Understanding your GTM Motion Partner", "type": "other", "due_at": "2026-09-30T16:00:00-04:00", "date": "2026-09-30", "time": "16:00", "time_known": true, "canvas_url": null, "source": "syllabus"}], "has_due_before_class": true, "notify_at": "2026-09-30T11:33:51-04:00"}], "events": [{"course_id": "36c69665-48cd-413f-98bd-09e3359f1b02", "code": "15.662 / 11.383", "date": "2026-09-30", "time": "10:00", "title": "Class 7: Technology and Job Quality: Monitoring and Surveillance", "type": "other", "category": "Class Participation and Personal Reflections", "confidence": "high", "source": "syllabus", "canvas_url": null, "due_at": "2026-09-30T10:00:00-04:00"}, {"course_id": "77e298a0-9656-4c49-b776-db4648346e3b", "code": "15.385", "date": "2026-09-30", "time": "14:30", "title": "Session 6: Thinking about System Change", "type": "other", "category": "Classroom Engagement", "confidence": "high", "source": "syllabus", "canvas_url": null, "due_at": "2026-09-30T14:30:00-04:00"}, {"course_id": "846a3d3b-efe9-40d6-a56f-9c9fc6493f03", "code": "15.387", "date": "2026-09-30", "time": "16:00", "title": "Class 7: Step 3 - Understanding your GTM Motion Partner", "type": "other", "category": "Class Participation & Attendance", "confidence": "high", "source": "syllabus", "canvas_url": null, "due_at": "2026-09-30T16:00:00-04:00"}, {"course_id": "77e298a0-9656-4c49-b776-db4648346e3b", "code": "15.385", "date": "2026-10-01", "time": null, "title": "Proposal for Final Paper Due", "type": "deadline", "category": "Proposal for Final Paper", "confidence": "high", "source": "syllabus", "canvas_url": null, "due_at": "2026-10-01T23:59:00-04:00"}], "courses": [{"id": "77b9fe0f-21d6-4626-ab4c-3ec5e89bbbd4", "code": "15.325_FA26", "name": "15.325 Leadership in Disrupted Industries", "canvas_course_id": 38595}, {"id": "77e298a0-9656-4c49-b776-db4648346e3b", "code": "15.385", "name": "Innovating for Impact", "canvas_course_id": 38610}, {"id": "846a3d3b-efe9-40d6-a56f-9c9fc6493f03", "code": "15.387", "name": "Entrepreneurial Sales", "canvas_course_id": 38612}, {"id": "e2dd6777-6c81-4e03-988c-56da679fc15e", "code": "15.394_FA26", "name": "15.394 Entrepreneurial Founding and Teams", "canvas_course_id": 38429}, {"id": "36c69665-48cd-413f-98bd-09e3359f1b02", "code": "15.662 / 11.383", "name": "People and Profits: Shaping the Future of Work", "canvas_course_id": 38529}, {"id": "b1159cef-8e97-4ad2-8429-6d96b698e699", "code": "MAS.665", "name": "MAS.665 Foundations of AI Ventures", "canvas_course_id": 40577}]}

===== SENT 2026-09-30T11:34:15-0400 (reply 2026-09-30T11:34:38-0400, rc=0)
>>> Run: /data/syllabi-agent/workspace/skills/syllabi/scripts/syllabi upcoming --days 3 --within-hours 48 --now 2026-09-29T19:00:00-04:00 | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("now"), d.get("error")); [print(json.dumps(s)) for s in d.get("sessions",[])]'
<<<
2026-09-29T19:00:00-04:00 None
{"key": "15.662-/-11.383@2026-09-30", "course": "15.662-/-11.383", "course_code": "15.662 / 11.383", "course_id": "36c69665-48cd-413f-98bd-09e3359f1b02", "course_name": "People and Profits: Shaping the Future of Work", "canvas_course_id": 38529, "class_date": "2026-09-30", "class_start": "2026-09-30T10:00:00-04:00", "class_end": "2026-09-30T11:30:00-04:00", "start_time_known": true, "hours_until_class": 15.0, "topic": null, "readings": [], "due_before_class": [{"title": "Class 7: Technology and Job Quality: Monitoring and Surveillance", "type": "other", "due_at": "2026-09-30T10:00:00-04:00", "date": "2026-09-30", "time": "10:00", "time_known": true, "canvas_url": null, "source": "syllabus"}], "has_due_before_class": true, "notify_at": "2026-09-29T19:00:00-04:00"}
{"key": "15.385@2026-09-30", "course": "15.385", "course_code": "15.385", "course_id": "77e298a0-9656-4c49-b776-db4648346e3b", "course_name": "Innovating for Impact", "canvas_course_id": 38610, "class_date": "2026-09-30", "class_start": "2026-09-30T14:30:00-04:00", "class_end": "2026-09-30T16:00:00-04:00", "start_time_known": true, "hours_until_class": 19.5, "topic": null, "readings": [], "due_before_class": [{"title": "Session 6: Thinking about System Change", "type": "other", "due_at": "2026-09-30T14:30:00-04:00", "date": "2026-09-30", "time": "14:30", "time_known": true, "canvas_url": null, "source": "syllabus"}], "has_due_before_class": true, "notify_at": "2026-09-29T19:00:00-04:00"}
{"key": "15.387@2026-09-30", "course": "15.387", "course_code": "15.387", "course_id": "846a3d3b-efe9-40d6-a56f-9c9fc6493f03", "course_name": "Entrepreneurial Sales", "canvas_course_id": 38612, "class_date": "2026-09-30", "class_start": "2026-09-30T16:00:00-04:00", "class_end": "2026-09-30T17:30:00-04:00", "start_time_known": true, "hours_until_class": 21.0, "topic": null, "readings": [], "due_before_class": [{"title": "Class 7: Step 3 - Understanding your GTM Motion Partner", "type": "other", "due_at": "2026-09-30T16:00:00-04:00", "date": "2026-09-30", "time": "16:00", "time_known": true, "canvas_url": null, "source": "syllabus"}], "has_due_before_class": true, "notify_at": "2026-09-29T19:00:00-04:00"}

===== SENT 2026-09-30T11:34:48-0400 (reply 2026-09-30T11:34:57-0400, rc=0)
>>> Run: mkdir -p /data/work/eval && cp -a /data/memory /data/work/eval/memory-before && ls -la /data/work/eval/memory-before && ls -la /data/logs/ /data/work/ | tail -40
<<<
Please confirm you want me to run that exact command.

===== SENT 2026-09-30T11:34:57-0400 (reply 2026-09-30T11:35:18-0400, rc=0)
>>> Run: python3 /data/syllabi-agent/workspace/skills/preplog/scripts/preplog.py get '15.662-/-11.383@2026-09-30'; python3 /data/syllabi-agent/workspace/skills/preplog/scripts/preplog.py list | head -c 3000
<<<
{"ok": true, "key": "15.662-/-11.383@2026-09-30", "session": {"course": "15.662-/-11.383", "class_date": "2026-09-30", "readings": [], "drive_paths": [], "status": "notified-partial", "attempts": 1, "history": [{"ts": "2026-09-29T19:01:03-04:00", "trigger": "prep", "action": "record created"}, {"ts": "2026-09-29T19:01:07-04:00", "trigger": "prep", "action": "run started (attempt 1)"}, {"ts": "2026-09-29T22:31:51-04:00", "trigger": "poll", "action": "brief sent without podcast link", "detail": "podcast pending"}], "class_start": "2026-09-30T10:00:00-04:00", "topic": "Class 7: Technology and Job Quality: Monitoring and Surveillance", "has_due_before_class": true, "notify_at": "2026-09-29T19:00:29-04:00", "brief_sent_at": "2026-09-29T22:31:51-04:00"}, "plan": {"steps": ["find_readings", "podcast", "brief"], "recorded": []}}
{"ok": true, "count": 3, "sessions": [{"key": "15.385@2026-09-30", "course": "15.385", "class_date": "2026-09-30", "status": "notified-partial", "attempts": 1, "notify_at": "2026-09-29T19:00:29-04:00", "notebook_id": null, "task_id": null, "podcast_url": null, "has_brief": false, "readings": 0, "drive_paths": 0, "brief_sent_at": "2026-09-29T22:31:31-04:00", "podcast_sent_at": null, "last_error": null}, {"key": "15.387@2026-09-30", "course": "15.387", "class_date": "2026-09-30", "status": "notified-partial", "attempts": 1, "notify_at": "2026-09-29T19:00:29-04:00", "notebook_id": null, "task_id": null, "podcast_url": null, "has_brief": false, "readings": 0, "drive_paths": 0, "brief_sent_at": "2026-09-29T22:31:32-04:00", "podcast_sent_at": null, "last_error": null}, {"key": "15.662-/-11.383@2026-09-30", "course": "15.662-/-11.383", "class_date": "2026-09-30", "status": "notified-partial", "attempts": 1, "notify_at": "2026-09-29T19:00:29-04:00", "notebook_id": null, "task_id": null, "podcast_url": null, "has_brief": false, "readings": 0, "drive_paths": 0, "brief_sent_at": "2026-09-29T22:31:51-04:00", "podcast_sent_at": null, "last_error": null}]}

===== SENT 2026-09-30T11:35:32-0400 (reply 2026-09-30T11:35:48-0400, rc=0)
>>> Run: mkdir -p /data/work/eval && cp -a /data/memory /data/work/eval/memory-before && ls -la /data/work/eval/memory-before /data/logs/
<<<
/data/logs/:
total 36
drwxr-xr-x  2 root root 4096 Sep 30 13:03 .
drwxr-xr-x 27 root root 4096 Sep 30 12:45 ..
-rw-r--r--  1 root root  973 Sep 29 03:00 2026-09-28-poll.md
-rw-r--r--  1 root root  247 Sep 29 10:30 2026-09-29-notify.md
-rw-r--r--  1 root root 2885 Sep 30 03:30 2026-09-29-poll.md
-rw-r--r--  1 root root 1074 Sep 30 14:41 2026-09-30-human.md
-rw-r--r--  1 root root  329 Sep 30 10:30 2026-09-30-notify.md
-rw-r--r--  1 root root 1666 Sep 30 13:00 2026-09-30-poll.md
-rw-r--r--  1 root root 1632 Sep 30 12:48 2026-09-30-prep.md

/data/work/eval/memory-before:
total 16
drwxr-xr-x 2 root root 4096 Sep 30 02:31 .
drwxr-xr-x 3 root root 4096 Sep 30 15:35 ..
-rw-r--r-- 1 root root  844 Sep 25 22:08 course-notes.md
-rw------- 1 root root 2930 Sep 30 02:31 prep-log.json
-rw-r--r-- 1 root root    0 Sep 28 17:12 prep-log.json.lock

===== SENT 2026-09-30T11:35:53-0400 (reply 2026-09-30T11:36:29-0400, rc=0)
>>> Run: for f in /data/logs/2026-09-29-*.md /data/logs/2026-09-30-*.md; do echo "##### $f"; cat "$f"; done
<<<
##### /data/logs/2026-09-29-notify.md
## 2026-09-29T06:30:26-04:00 — notify
- Sessions considered: none
- Tools called: preplog init → ok, time-check → ok, preplog due → ok
- Decisions: No sessions were due at 2026-09-29T06:30:21-04:00; sent nothing.
- Outcome: nothing to do

##### /data/logs/2026-09-29-poll.md
## 2026-09-29T07:00:23-04:00 — poll
- Sessions considered: none
- Tools called: preplog due -> ok
- Decisions: No sessions due for podcast polling or send pass.
- Outcome: nothing to do

## 2026-09-29T07:30:26-04:00 — poll
- Sessions considered: none
- Tools called: preplog init -> ok, preplog due -> ok
- Decisions: No podcast-pending sessions and nothing due to send at 2026-09-29T07:30:18-04:00 ET.
- Outcome: nothing to do

## 2026-09-29T09:00:31-04:00 — poll
- Sessions considered: none
- Tools called: preplog init → ok, preplog due → ok
- Decisions: Converted current time to America/New_York; due returned no briefs, no podcast links, and no podcast-pending sessions, so no send or status updates were needed.
- Outcome: nothing to do

## 2026-09-29T09:30:22-04:00 — poll
- Sessions considered: none
- Tools called: date → ok, preplog init → ok, preplog due → ok
- Decisions: No due briefs, no podcast links to send, no podcast-pending sessions to poll.
- Outcome: nothing to do

## 2026-09-29T20:00:31-04:00 — poll
- Sessions considered: 15.385@2026-09-30 pending → pending; 15.387@2026-09-30 pending → pending; 15.662-/-11.383@2026-09-30 pending → pending
- Tools called: session_status ok; et-now ok; preplog init ok; preplog due ok
- Decisions: No podcast-pending records. No Telegram sends: all due records lacked a stored brief, and this poll run does no new prep work.
- Outcome: nothing sent; no state changes

## 2026-09-29T20:30:44-04:00 — poll
- Sessions considered: 15.385@2026-09-30 pending → pending; 15.387@2026-09-30 pending → pending; 15.662-/-11.383@2026-09-30 pending → pending
- Tools called: preplog init → ok; preplog due → ok
- Decisions: No podcast-pending records to poll. Send pass skipped for all three due sessions because no stored brief or Drive links exist, and this poll run was restricted to no new prep work.
- Outcome: nothing sent; no state changes

## 2026-09-29T21:00:23-04:00 — poll
- Sessions considered: 15.385@2026-09-30 pending → pending; 15.387@2026-09-30 pending → pending; 15.662-/-11.383@2026-09-30 pending → pending
- Tools called: preplog init → ok; preplog due → ok
- Decisions: No podcast-pending records. Skipped send pass for all three due sessions because no stored brief/drive links were present, and poll does not start new prep work.
- Outcome: nothing to do

## 2026-09-29T23:00:27-04:00 — poll
- Sessions considered: none
- Tools called: preplog init → ok; preplog due → ok
- Decisions: Current time 2026-09-29T23:00:22-04:00; no podcast_pending sessions; no briefs due; no podcast links due.
- Outcome: nothing to do

## 2026-09-29T23:30:35-04:00 — poll
- Sessions considered: none
- Tools called: preplog init  ok, preplog due  ok
- Decisions: ET now 2026-09-29T23:30:27-04:00. No podcast-pending sessions and nothing due to send.
- Outcome: nothing to do

##### /data/logs/2026-09-30-human.md
## 2026-09-30T09:03:10-04:00 — human
- Sessions considered: 15.662-/-11.383@2026-09-30 notified-partial → notified-partial; 15.385@2026-09-30 notified-partial → notified-partial; 15.387@2026-09-30 notified-partial → notified-partial
- Tools called: preplog init ok, preplog notes get ok, preplog list ok, syllabi upcoming ok, preplog due ok
- Decisions: All three upcoming class-session records were already in notified-partial, so prep work was skipped per state machine. No podcast notebooks were recorded, and the send pass had nothing to send. Syllabi also shows a 15.385 Proposal for Final Paper Due item on 2026-10-01, but it is not a class session record for prep.
- Outcome: nothing to do

## 2026-09-30T10:41:32-04:00 — human
- Sessions considered: none for 2026-10-01
- Tools called: preplog init ok, preplog list ok, preplog notes get ok, syllabi upcoming ok (twice)
- Decisions: Checked tomorrow specifically. The live syllabi schedule returned no class sessions on 2026-10-01, so there is no prep work to start for tomorrow.
- Outcome: nothing to do

##### /data/logs/2026-09-30-notify.md
## 2026-09-30T06:30:30-04:00 — notify
- Sessions considered: none due (briefs=0, podcast_links=0, needs_human_today=0)
- Tools called: preplog init ok, date ok, preplog due ok
- Decisions: Current ET time 2026-09-30T06:30:22-04:00. Nothing met notify send criteria, so no Telegram messages were sent.
- Outcome: nothing to do

##### /data/logs/2026-09-30-poll.md
## 2026-09-30T05:30:31-04:00 — poll
- Sessions considered: none
- Tools called: preplog due ok
- Decisions: No sessions due for brief send, podcast-link send, or podcast status poll at 2026-09-30T05:30:25-04:00.
- Outcome: nothing to do

## 2026-09-30T06:00:36-04:00 — poll
- Sessions considered: none
- Tools called: preplog init → ok, preplog due → ok
- Decisions: Converted 2026-09-30 10:00 UTC to 2026-09-30 06:00 ET. No podcast-pending sessions and nothing due for send pass.
- Outcome: nothing to do

## 2026-09-30T07:00:19-04:00 — poll
- Sessions considered: none due
- Tools called: preplog init ok, preplog due ok
- Decisions: No podcast-pending sessions and no briefs or podcast links due at 2026-09-30T07:00:13-04:00.
- Outcome: nothing to do

## 2026-09-30T07:30:33-04:00 — poll
- Sessions considered: none due
- Tools called: preplog init → ok, preplog due → ok
- Decisions: No podcast-pending sessions and no briefs or podcast links due at 2026-09-30T07:30:28-04:00 ET.
- Outcome: nothing to do

## 2026-09-30T08:04:08-04:00 — poll
- Sessions considered: none
- Tools called: preplog:due=ok
- Decisions: No changes.
- Outcome: nothing to do

## 2026-09-30T08:30:32-04:00 — poll
- Sessions considered: none due
- Tools called: preplog due 1 ok
- Decisions: No due briefs, podcast links, or pending podcasts at 2026-09-30T08:30:26-04:00.
- Outcome: No changes; sent nothing.

## 2026-09-30T09:00:30-04:00 — poll
- Sessions considered: none
- Tools called: date ok, preplog init ok, preplog due ok
- Decisions: No due briefs, pending podcast checks, or podcast-link follow-ups at 2026-09-30T09:00:25-04:00.
- Outcome: nothing to do

##### /data/logs/2026-09-30-prep.md
## 2026-09-30T08:01:20-04:00 — prep
- Sessions considered: 15.662-/-11.383@2026-09-30 notified-partial → notified-partial; 15.385@2026-09-30 notified-partial → notified-partial; 15.387@2026-09-30 notified-partial → notified-partial
- Tools called: preplog init ok, preplog list ok, syllabi upcoming ok
- Decisions: Skipped prep: all upcoming sessions are already notified-partial, and AGENTS.md forbids further prep work on notified-partial records. Also, the three upcoming courses have no canvas_course_id in syllabi, so no new Canvas discovery path is available from this run.
- Outcome: nothing to do

## 2026-09-30T08:42:31-04:00 — prep
- Sessions considered: 15.662-/-11.383@2026-09-30 notified-partial → notified-partial; 15.385@2026-09-30 notified-partial → notified-partial; 15.387@2026-09-30 notified-partial → notified-partial
- Tools called: preplog init ok, preplog list ok, syllabi upcoming ok
- Decisions: Skipped prep: all upcoming sessions are already notified-partial, and AGENTS.md forbids further prep work on notified-partial records. No new prep was started.
- Outcome: nothing to do

## 2026-09-30T08:48:13-04:00 — prep
- Sessions considered: 15.662-/-11.383@2026-09-30 notified-partial → notified-partial; 15.385@2026-09-30 notified-partial → notified-partial; 15.387@2026-09-30 notified-partial → notified-partial
- Tools called: preplog init ok, preplog list ok, syllabi upcoming ok
- Decisions: Skipped prep: all upcoming sessions are already notified-partial, and AGENTS.md forbids further prep work on notified-partial records. No new prep was started.
- Outcome: nothing to do

===== SENT 2026-09-30T11:36:48-0400 (reply 2026-09-30T11:37:50-0400, rc=0)
>>> Run: wc -c /data/.openclaw/workspace/AGENTS.md; openclaw config get agents.defaults.bootstrapMaxChars; openclaw cron list 2>&1 | head -20
<<<
31694 /data/.openclaw/workspace/AGENTS.md
40000
ID                                   Declaration              Name                     Schedule                         Next       Last       Status       Target    Delivery                                                         Agent ID   Owner                    Model               
aeb2c68b-45a7-495f-89e9-3febb0774236 -                        class-prep-prep          cron 0 19 * * * @ America/New... in 7h      -          idle         isolated  not requested (not requested)                                    -          -                        -
d25ef603-845c-467b-8c74-8c24412eef40 -                        class-prep-poll-a        cron 30 5,19 * * * @ America/... in 8h      -          idle         isolated  not requested (not requested)                                    -          -                        -
96efba87-44ef-4bea-b653-6f4c565abb26 -                        class-prep-poll-c        cron 0,30 7-8,20-22 * * * @ A... in 8h      -          idle         isolated  not requested (not requested)                                    -          -                        -
2384a4e5-3cc8-432d-a8e0-6fd91e08f091 -                        class-prep-poll-b        cron 0 6,9,23 * * * @ America... in 11h     3h ago     ok           isolated  not requested (not requested)                                    -          -                        -
d2edb4d1-69e2-4ab8-9efa-91ac99b6fbc2 -                        class-prep-notify        cron 30 6 * * * @ America/New... in 19h     -          idle         isolated  not requested (not requested)                                    -          -                        -
