#!/usr/bin/env python3
"""redact-evidence: clean forum output and log excerpts before they go into the HW3 writeup (SYL-109).

    python3 scripts/redact-evidence.py [--self-id N] [--env-file PATH] < raw.txt > clean.txt

Reads text on stdin and writes it to stdout with:

    * the value of every environment variable whose name contains TOKEN, KEY, SECRET, PASSWORD or
      COOKIE (values of 8+ characters, the same rule as forum.py) replaced by [redacted], plus
      anything shaped like a syllabi agent token, an mk_ key or a Bearer header;
    * email addresses replaced by [email];
    * Canvas user ids other than the agent's own replaced by user-N, numbered in order of first
      appearance, so the same person keeps the same number within one run. Recognised forms:
      "user 123" (forum read's author field), "user_id": 123 / "123", user_id=123, /users/123.

The agent's own id comes from --self-id, else from $DATA_DIR/memory/forum-state.json
(self_user_id), else from a "self_user_id": N in the input itself (forum status prints it). With
none of those, every user id is replaced, and a note says so on stderr.

--env-file reads KEY=VALUE lines (a local .env) as well as the real environment, for running
this on a laptop where the agent's secrets are not exported.

Standard library only. Typical use, on Maritime:

    forum report | python3 scripts/redact-evidence.py > /data/work/forum-report.md
"""
import argparse
import json
import os
import re
import sys

SECRET_NAME_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "COOKIE")
SECRET_MIN_LEN = 8
REDACTED = "[redacted]"
EMAIL_MARK = "[email]"

TOKEN_SHAPES = [
    re.compile(r"syl_agent_[A-Za-z0-9_-]{20,}"),
    re.compile(r"mk_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<=Bearer )[A-Za-z0-9~_.+/=-]{8,}"),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SELF_ID_IN_TEXT = re.compile(r'"self_user_id"\s*:\s*"?(\d+)')
# Each alternative captures (prefix)(id)(suffix); the prefix and suffix are kept as written.
USER_ID = re.compile(
    r'(?P<json>"user_id"\s*:\s*)(?P<q>"?)(?P<jid>\d+)(?P=q)'
    r'|(?P<kv>\buser_id=)(?P<kid>\d+)'
    r'|(?P<path>/users/)(?P<pid>\d+)\b'
    r'|(?P<word>\buser )(?P<wid>\d+)\b'
)


def secret_values(env):
    """Values (8+ characters) of every variable whose name looks like a credential, longest first."""
    values = set()
    for name, value in env.items():
        if not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) >= SECRET_MIN_LEN and any(w in name.upper() for w in SECRET_NAME_WORDS):
            values.add(value)
    return sorted(values, key=len, reverse=True)


def read_env_file(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name.startswith("export "):
                name = name[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[name] = value
    return env


def self_id_from_state(data_dir):
    try:
        with open(os.path.join(data_dir, "memory", "forum-state.json"), encoding="utf-8") as fh:
            uid = json.load(fh).get("self_user_id")
    except (OSError, ValueError, AttributeError):
        return None
    return str(uid) if uid is not None and str(uid).isdigit() else None


class UserNumbers:
    """Maps Canvas user ids to user-1, user-2, ... in order of first appearance."""

    def __init__(self, self_id=None):
        self.self_id = str(self_id) if self_id is not None else None
        self.labels = {}

    def label(self, uid):
        if uid == self.self_id:
            return None
        if uid not in self.labels:
            self.labels[uid] = "user-%d" % (len(self.labels) + 1)
        return self.labels[uid]

    def sub(self, m):
        if m.group("json") is not None:
            label = self.label(m.group("jid"))
            return m.group(0) if label is None else '%s"%s"' % (m.group("json"), label)
        for prefix, key in (("kv", "kid"), ("path", "pid")):
            if m.group(prefix) is not None:
                label = self.label(m.group(key))
                return m.group(0) if label is None else m.group(prefix) + label
        label = self.label(m.group("wid"))
        return m.group(0) if label is None else label


def redact(text, secrets=(), self_id=None, numbers=None):
    """The whole transformation, as a pure function (the tests call this)."""
    for value in secrets:
        text = text.replace(value, REDACTED)
    for rx in TOKEN_SHAPES:
        text = rx.sub(REDACTED, text)
    text = EMAIL.sub(EMAIL_MARK, text)
    numbers = numbers or UserNumbers(self_id)
    return USER_ID.sub(numbers.sub, text)


def main(argv=None, stdin=None, stdout=None, stderr=None, env=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(description="Redact secrets, emails and other users' ids from evidence text.")
    ap.add_argument("--self-id", help="the agent's own Canvas user id (kept as is)")
    ap.add_argument("--env-file", action="append", default=[], help="also treat secret-named values in this .env file as secrets")
    args = ap.parse_args(argv)
    if args.self_id is not None and not args.self_id.isdigit():
        ap.error("--self-id must be a numeric Canvas user id")

    secrets = set(secret_values(env))
    for path in args.env_file:
        try:
            file_env = read_env_file(path)
        except OSError as e:
            stderr.write("redact-evidence: cannot read %s: %s\n" % (path, e.strerror))
            return 2
        secrets.update(secret_values(file_env))
    secrets = sorted(secrets, key=len, reverse=True)
    text = stdin.read()

    self_id = args.self_id or self_id_from_state(env.get("DATA_DIR") or "/data")
    if self_id is None:
        m = SELF_ID_IN_TEXT.search(text)
        self_id = m.group(1) if m else None
    if self_id is None:
        stderr.write("redact-evidence: agent's own user id unknown; every user id is replaced\n")

    stdout.write(redact(text, secrets, self_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
