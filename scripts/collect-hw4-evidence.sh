#!/bin/sh
# Build the HW4 evidence folder from the committed agent memory in state/ (SYL-116).
#
#   sh scripts/collect-hw4-evidence.sh [YYYY-MM-DD]     # default: today in America/New_York
#
# Reads state/ only: no Canvas call, no secrets. Writes evidence/hw4-run-<date>/:
#   hw4-report.md    cycles (hw4.jsonl + forum.jsonl), discovery rankings, the
#                    request -> result -> verdict timeline, provider-side jobs
#   forum-status.txt `forum.py status` if it runs without a token, else a note
#   workflow-runs.txt `gh run list --workflow hw4-forum.yml` if gh works, else a note
# Everything goes through scripts/redact-evidence.py. Read the files before committing.
set -eu

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
DAY="${1:-$(TZ=America/New_York date +%Y-%m-%d)}"
OUT="$ROOT/evidence/hw4-run-$DAY"
STATE="$ROOT/state"
REDACT="python3 $ROOT/scripts/redact-evidence.py --self-id $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["self_user_id"])' "$STATE/memory/forum-state.json")"

mkdir -p "$OUT"

echo "-> hw4-report.md"
python3 - "$STATE" <<'PY' | $REDACT > "$OUT/hw4-report.md"
import datetime as dt, json, os, sys
from zoneinfo import ZoneInfo

state = sys.argv[1]
ET = ZoneInfo("America/New_York")
FORUM = "https://canvas.mit.edu/courses/40577/discussion_topics/449948"

def et(ts):
    if not ts:
        return "-"
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
    return t.strftime("%b %d, %H:%M ET")

def link(i):
    return f"[{i}]({FORUM}?entry_id={i})" if i else "-"

def jsonl(p):
    out = []
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out

s = json.load(open(os.path.join(state, "memory/hw4-state.json"), encoding="utf-8"))
cycles = jsonl(os.path.join(state, "logs/hw4.jsonl"))
forum = jsonl(os.path.join(state, "logs/forum.jsonl"))
halt = os.path.exists(os.path.join(state, "memory/forum-halt"))

print("# HW4 evidence report (from state/, no Canvas call)\n")
print(f"Generated {dt.datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}. Last cycle: {et(s.get('last_cycle_at'))}. "
      f"Halt file: {'PRESENT' if halt else 'absent'}.\n")

print("## Cycles (state/logs/hw4.jsonl)\n")
print("| Time | New entries | Decision | Actions | Posts | Reason |")
print("| --- | --- | --- | --- | --- | --- |")
for c in cycles:
    if c.get("event") == "verdict":
        continue
    acts = "; ".join(f"{a.get('action')}: {a.get('result')}" + (f" {a['entry_id']}" if a.get("entry_id") else "")
                     for a in c.get("actions", [])) or "-"
    err = f" ({c['error_code']})" if c.get("error_code") else ""
    print(f"| {et(c.get('ts'))} | {c.get('new_count', '-')} | {c.get('decision')}{err} | {acts} | {c.get('posts', 0)} | {c.get('reason') or '-'} |")

print("\n## Forum tool writes and errors (state/logs/forum.jsonl)\n")
print("| Time | Command | Decision | Entry | Attempts | Error |")
print("| --- | --- | --- | --- | --- | --- |")
for f in forum:
    if f.get("command") in ("post", "reconcile") or f.get("error_code"):
        print(f"| {et(f.get('ts'))} | {f.get('command')} | {f.get('decision')} | {link(f.get('entry_id'))} | {f.get('attempts') or '-'} | {f.get('error_code') or '-'} |")
runs = [f for f in forum if f.get("command") == "status"]
print(f"\n{len(runs)} workflow runs logged a `status` line; halted in any: "
      f"{any(f.get('halted') for f in runs)}; max consecutive_failures: {max([f.get('consecutive_failures') or 0 for f in runs] or [0])}; "
      f"max attempts_last_hour: {max([f.get('attempts_last_hour') or 0 for f in runs] or [0])}.")

print("\n## Discovery (every provider ranking)\n")
for d in s.get("discovery", []):
    top = sorted(d.get("ranking", []), key=lambda r: -r.get("fit", 0))[:3]
    print(f"- **{et(d.get('ts'))}**: {len(d.get('candidates', []))} candidates, chosen {link(d.get('chosen'))}. "
          f"Reason: \"{d.get('reason')}\"")
    for r in top:
        print(f"  - {r['entry_id']} fit {r['fit']}: {r.get('why')}")

print("\n## Request -> result -> verdict timeline\n")
print("| Request | Provider | Sent | Deadline | Status | Result | Verdict | Checks | Tip | Reason |")
print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
for r in s.get("past_requests", []) + ([s["request"]] if s.get("request") else []):
    ch = r.get("checks") or {}
    checks = ", ".join(f"{k} {ch[k]}" for k in ("static", "correct", "m1", "m2") if k in ch) or "-"
    print(f"| {link(r.get('request_entry_id'))} | {link(r.get('provider_entry_id'))} ({r.get('provider_author')}) | {et(r.get('sent_at'))} | "
          f"{r.get('deadline_et')} | {r.get('status')} | {link(r.get('result_entry_id'))} | {link(r.get('verdict_entry_id'))} | "
          f"{checks} | {r.get('tip') if r.get('tip') is not None else '-'} | {r.get('reason') or '-'} |")
for v in (c for c in cycles if c.get("event") == "verdict"):
    print(f"\n- verdict {et(v['ts'])}: {v['decision']} on result {v.get('result_entry_id')} (attempt {v.get('attempt')}); "
          f"output: {(v.get('checks') or {}).get('output_excerpt', '-')}")

print("\n## Provider side (jobs other agents sent to us)\n")
jobs = s.get("jobs", [])
if not jobs:
    print("No requests to our offer were received.")
for j in jobs:
    print(f"- request {link(j.get('request_entry_id'))} from {j.get('author')}: {j.get('status')}, result {link(j.get('result_entry_id'))}, "
          f"checks {j.get('checks')}, decline {j.get('decline_code') or '-'}")
PY

echo "-> forum-status.txt"
if [ -n "${CANVAS_FORUM_TOKEN:-}" ] && DATA_DIR="$STATE" python3 "$ROOT/workspace/skills/forum/scripts/forum.py" status > "$OUT/.status.raw" 2>&1; then
  $REDACT < "$OUT/.status.raw" > "$OUT/forum-status.txt"
else
  {
    echo "forum.py status was not run: it needs CANVAS_BASE_URL and a token to read the control line,"
    echo "and this script uses no secrets. The last status line each workflow run logged:"
    grep '"command": "status"' "$STATE/logs/forum.jsonl" | tail -1
  } | $REDACT > "$OUT/forum-status.txt"
fi
rm -f "$OUT/.status.raw"

echo "-> workflow-runs.txt"
if gh run list --workflow hw4-forum.yml --limit 50 > "$OUT/.runs.raw" 2>/dev/null; then
  $REDACT < "$OUT/.runs.raw" > "$OUT/workflow-runs.txt"
elif [ ! -s "$OUT/workflow-runs.txt" ]; then
  echo "gh is not authenticated here; see https://github.com/chris-a-ackerman/syllabi-agent/actions/workflows/hw4-forum.yml" > "$OUT/workflow-runs.txt"
fi
rm -f "$OUT/.runs.raw"

echo "Saved to $OUT. Read each file for names, emails and tokens before committing."
