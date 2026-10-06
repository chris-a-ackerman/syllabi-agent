#!/bin/sh
# Pull the HW3 forum evidence from the agent into evidence/run-<date>/ (run on the laptop).
#
#   sh scripts/collect-forum-evidence.sh [since-ISO]     # default since 2026-10-05T00:00:00-04:00
#
# Asks the agent over `maritime chat` (operator chat, `Run:` lines) for the redacted
# `forum report`, `forum status` and the forum.jsonl lines, and writes them to
# forum-report.md, forum-status.txt and forum-log-excerpts.txt. Redaction runs on the agent,
# where the token is in the environment. Read-only: nothing is posted, no state changes.
set -eu

AGENT="${AGENT:-class-prep-repo}"
SINCE="${1:-2026-10-05T00:00:00-04:00}"
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
OUT="$ROOT/evidence/run-2026-10-05"
FORUM="python3 /data/syllabi-agent/workspace/skills/forum/scripts/forum.py"
REDACT="python3 /data/syllabi-agent/scripts/redact-evidence.py"

mkdir -p "$OUT"

ask() { # file command
  echo "-> $1"
  maritime chat "$AGENT" "Run: $2" > "$OUT/$1"
}

ask forum-report.md        "$FORUM report --since $SINCE | jq -r .markdown | $REDACT"
ask forum-status.txt       "$FORUM status | $REDACT"
ask forum-log-excerpts.txt "$REDACT < /data/logs/forum.jsonl"

echo "Saved to $OUT. Read each file for names, emails and tokens before committing."
