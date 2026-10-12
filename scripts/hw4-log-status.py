#!/usr/bin/env python3
"""hw4-log-status: record one `forum status` result in forum.jsonl (SYL-112, HW4-1).

    python3 forum.py status | python3 scripts/hw4-log-status.py [--run-id ID]

`forum status` is read-only and writes no log, so on GitHub Actions a run that only checks status
would leave no trace in the committed state. This appends one line to $DATA_DIR/logs/forum.jsonl
with the same fields forum.py logs (ts, command, decision, reason, entry_id, error_code, attempts,
new_count) plus the run id and the status counts. Only whitelisted fields are copied: ids,
counts, codes, never text. `forum report` ignores `status` lines.

Exit 0 on success, 2 when stdin is not one forum.py JSON object (nothing is written).
Standard library only.
"""
import argparse
import datetime as dt
import json
import os
import sys

STATUS_FIELDS = ("control", "control_checked", "halted", "self_user_id", "own_posts", "pending",
                 "abandoned", "posts_last_hour", "attempts_last_hour", "rate_limit",
                 "consecutive_failures", "last_cycle_at")


def build_line(result, run_id, now):
    line = {"ts": now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "command": "status", "new_count": None, "decision": None, "reason": None, "entry_id": None,
            "error_code": None, "attempts": None}
    if run_id:
        line["run_id"] = str(run_id)
    if result.get("ok"):
        for key in STATUS_FIELDS:
            if key in result and isinstance(result[key], (str, int, bool, type(None))):
                line[key] = result[key]
        if isinstance(result.get("control_error"), str):
            line["error_code"] = result["control_error"]
    else:
        err = result.get("error") if isinstance(result.get("error"), dict) else {}
        line["decision"] = "error"
        line["error_code"] = str(err.get("code") or "UNKNOWN")[:40]
    return line


def main(argv=None, stdin=None, env=None, now=None):
    p = argparse.ArgumentParser(prog="hw4-log-status")
    p.add_argument("--run-id", help="the workflow run id (default $GITHUB_RUN_ID)")
    args = p.parse_args(argv)
    stdin = sys.stdin if stdin is None else stdin
    env = os.environ if env is None else env
    try:
        result = json.loads(stdin.read())
    except ValueError:
        sys.stderr.write("hw4-log-status: stdin is not JSON; nothing logged\n")
        return 2
    if not isinstance(result, dict):
        sys.stderr.write("hw4-log-status: stdin is not a JSON object; nothing logged\n")
        return 2
    data_dir = (env.get("DATA_DIR") or "/data").rstrip("/") or "/"
    logs = os.path.join(data_dir, "logs")
    os.makedirs(logs, exist_ok=True)
    line = build_line(result, args.run_id or env.get("GITHUB_RUN_ID"), now or dt.datetime.now(dt.timezone.utc))
    with open(os.path.join(logs, "forum.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
