#!/bin/sh
# Install this repo's workspace into the Maritime/OpenClaw workspace.
# Safe to re-run after every `git pull`.
#
#   sh /data/syllabi-agent/scripts/install-workspace.sh
#
# What it does (verified layout on Maritime, 2026-09-24):
#   - HOME is /data, so OpenClaw's workspace is /data/.openclaw/workspace (persistent).
#   - Maritime writes its own MARITIME.md and a "maritime-prepend" block at the top of AGENTS.md.
#     We keep both, and put our AGENTS.md below Maritime's block (replacing OpenClaw's default text).
#   - skills/, agents/ and memory-templates/ are symlinked, so `git pull` updates them directly.
#     (memory-templates, not memory: OpenClaw uses workspace/memory/ for its own daily notes.)
#   - SOUL.md is copied over OpenClaw's default.
#   - Runtime dirs under /data are created, and course-notes / prep-log are seeded if missing.
set -eu

REPO="${REPO:-/data/syllabi-agent}"
WS="${WS:-/data/.openclaw/workspace}"
SRC="$REPO/workspace"
BACKUP="/data/workspace-backups/$(date -u +%Y%m%dT%H%M%SZ)"

[ -d "$WS" ]  || { echo "error: no OpenClaw workspace at $WS" >&2; exit 1; }
[ -d "$SRC" ] || { echo "error: no repo workspace at $SRC (clone the repo first)" >&2; exit 1; }

mkdir -p "$BACKUP"
cp -a "$WS/AGENTS.md" "$BACKUP/AGENTS.md"
[ -f "$WS/SOUL.md" ] && cp -a "$WS/SOUL.md" "$BACKUP/SOUL.md"

# 1. Folders: symlinks into the repo.
for d in skills agents memory-templates; do
  if [ -e "$WS/$d" ] && [ ! -L "$WS/$d" ]; then mv "$WS/$d" "$BACKUP/$d"; fi
  ln -sfn "$SRC/$d" "$WS/$d"
done

# 2. SOUL.md: ours replaces OpenClaw's default.
cp "$SRC/SOUL.md" "$WS/SOUL.md"

# 3. AGENTS.md: Maritime's block (everything before the first "# AGENTS.md" heading), then ours.
awk '/^# AGENTS\.md/ { exit } { print }' "$BACKUP/AGENTS.md" > "$WS/AGENTS.md.new"
grep -q 'maritime-prepend' "$WS/AGENTS.md.new" \
  || echo "warning: Maritime's prepend block was not found; AGENTS.md will contain only ours" >&2
cat "$SRC/AGENTS.md" >> "$WS/AGENTS.md.new"
mv "$WS/AGENTS.md.new" "$WS/AGENTS.md"

# 4. Runtime directories and seed files (never overwritten).
mkdir -p /data/memory /data/logs /data/readings /data/podcasts /data/work /data/secrets /data/rclone
[ -f /data/memory/course-notes.md ] || cp "$SRC/memory-templates/course-notes.md" /data/memory/course-notes.md
[ -f /data/memory/prep-log.json ]   || printf '{"version": 1, "sessions": {}}\n' > /data/memory/prep-log.json

echo "installed $SRC into $WS (backup: $BACKUP)"
ls -la "$WS"
