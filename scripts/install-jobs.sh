#!/bin/sh
# Create the OpenClaw cron jobs (America/New_York) from triggers/*.md.
#
#   sh /data/syllabi-agent/scripts/install-jobs.sh            # create missing jobs
#   sh /data/syllabi-agent/scripts/install-jobs.sh --replace  # delete and recreate (after editing a prompt)
#
# The jobs only fire while the container is awake. Maritime wakes it with ONE platform trigger,
# created once from your laptop (see triggers/README.md):
#   maritime triggers create <agent> --type cron --cron "*/30 * * * *"
#
# Flags used, per the OpenClaw automations CLI docs:
#   --tz America/New_York  schedules are ET, so DST is handled for us
#   --exact                no top-of-hour stagger (otherwise up to 5 min late, after the wake)
#   --session isolated     fresh transcript every run; state lives in /data/memory, not in chat
#   --no-deliver           the agent sends Telegram itself with maritime-telegram-send
set -eu

REPO="${REPO:-/data/syllabi-agent}"
TZ_NAME="America/New_York"
REPLACE=0
[ "${1:-}" = "--replace" ] && REPLACE=1

# Prompt = everything below the first '---' line of triggers/<name>.md
prompt() { sed '1,/^---$/d' "$REPO/triggers/$1.md"; }

job_id() {
  openclaw cron show "$1" --json 2>/dev/null | node -e '
    let s = ""; process.stdin.on("data", d => s += d).on("end", () => {
      try { const j = JSON.parse(s); const job = j.job || j; console.log(job.id || job.jobId || ""); }
      catch { console.log(""); }
    });' 2>/dev/null || true
}

add() { # name cron trigger-file timeout-seconds
  name="$1"; cron="$2"; file="$3"; timeout="$4"
  id="$(job_id "$name")"
  if [ -n "$id" ]; then
    if [ "$REPLACE" -eq 1 ]; then
      openclaw cron rm "$id" >/dev/null && echo "removed $name ($id)"
    else
      echo "skip $name: already exists ($id). Use --replace to recreate."; return 0
    fi
  fi
  openclaw cron add --name "$name" --cron "$cron" --tz "$TZ_NAME" --exact \
    --session isolated --no-deliver --timeout-seconds "$timeout" \
    --message "$(prompt "$file")" >/dev/null
  echo "added  $name  [$cron $TZ_NAME]"
}

add class-prep-prep     "0 19 * * *"           prep   1200
add class-prep-notify   "30 6 * * *"           notify 600
add class-prep-poll-a   "30 5,19 * * *"        poll   600
add class-prep-poll-b   "0 6,9,23 * * *"       poll   600
add class-prep-poll-c   "0,30 7-8,20-22 * * *" poll   600

openclaw cron list
