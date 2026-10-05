#!/bin/sh
# Build the HW3 submission ZIP from the committed tree, and refuse it if it holds a secret.
#
#   sh scripts/make-submission-zip.sh          # -> dist/syllabi-agent-hw3.zip
#
# The ZIP is `git archive HEAD`: tracked, committed files only. Uncommitted edits, .env files,
# /data runtime output and anything .gitignore keeps out never reach it. Commit first.
#
# Then every file in the archive is checked, and the ZIP is deleted (exit 1) if it contains:
#   - a file named .env or .env.* (other than .env.example), forum-state.json, forum.jsonl,
#     prep-log.json, rclone.conf, storage_state*.json or master_token.json;
#   - text matching syl_agent_[A-Za-z0-9_-]{20,}, mk_[A-Za-z0-9]{20,} or
#     Bearer [A-Za-z0-9~_-]{20,} (a syllabi agent token, a Maritime key, a bearer header).
# A refusal names the file, line and pattern, never the matched value.
set -eu

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
NAME="syllabi-agent-hw3"
OUT_DIR="$ROOT/dist"
OUT="$OUT_DIR/$NAME.zip"

cd "$ROOT"
mkdir -p "$OUT_DIR"
rm -f "$OUT"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "note: uncommitted changes are NOT in the ZIP (it is built from HEAD)." >&2
fi

git archive --format=zip --prefix="$NAME/" -o "$OUT" HEAD

if ! python3 - "$OUT" <<'PY'
import os
import re
import sys
import zipfile

path = sys.argv[1]
FORBIDDEN_NAMES = {"forum-state.json", "forum.jsonl", "prep-log.json", "rclone.conf",
                   "storage_state.json", "master_token.json"}
PATTERNS = [
    ("syllabi agent token", re.compile(rb"syl_agent_[A-Za-z0-9_-]{20,}")),
    ("mk_ key", re.compile(rb"mk_[A-Za-z0-9]{20,}")),
    ("bearer token", re.compile(rb"Bearer [A-Za-z0-9~_-]{20,}")),
]


def bad_name(base):
    if base == ".env" or (base.startswith(".env.") and base != ".env.example"):
        return True
    if base.startswith("storage_state") and base.endswith(".json"):
        return True
    return base in FORBIDDEN_NAMES


problems, files, size = [], 0, 0
with zipfile.ZipFile(path) as zf:
    for info in zf.infolist():
        if info.is_dir():
            continue
        files += 1
        size += info.file_size
        name = info.filename.split("/", 1)[-1]
        if bad_name(os.path.basename(name)):
            problems.append("%s: forbidden file" % name)
        data = zf.read(info)
        for label, rx in PATTERNS:
            for m in rx.finditer(data):
                line = data.count(b"\n", 0, m.start()) + 1
                problems.append("%s:%d: looks like a %s" % (name, line, label))

if problems:
    print("REFUSED: the archive contains secrets or runtime state:", file=sys.stderr)
    for p in problems:
        print("  " + p, file=sys.stderr)
    sys.exit(1)
print("%d files, %.1f KB uncompressed" % (files, size / 1024.0))
PY
then
  rm -f "$OUT"
  echo "No ZIP written. Remove those from git (and rotate any real token), commit, and re-run." >&2
  exit 1
fi

bytes="$(wc -c < "$OUT" | tr -d ' ')"
echo "wrote ${OUT#"$ROOT"/}: $bytes bytes ($(git rev-parse --short HEAD))"
