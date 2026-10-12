"""Tests for the HW4 GitHub Actions runner (SYL-112): the workflow file, state/ and hw4-log-status.py.

No network, no YAML library: the workflow is checked as text.

    python3 -m unittest discover -s tests -v
"""
import datetime as dt
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "hw4-forum.yml")
LOG_STATUS = os.path.join(REPO, "scripts", "hw4-log-status.py")

_spec = importlib.util.spec_from_file_location("hw4_log_status", LOG_STATUS)
hw4_log_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hw4_log_status)

SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "COOKIE")


class WorkflowFileTests(unittest.TestCase):
    def setUp(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            self.text = fh.read()
        self.lines = self.text.splitlines()

    def test_required_settings(self):
        for needle in ("concurrency", "group: hw4-forum", "cancel-in-progress: false", "timeout-minutes: 10",
                       "permissions:", "contents: write", "DATA_DIR", "[skip ci]", 'cron: "17 */2 * * *"',
                       "workflow_dispatch", "actions/checkout@v4", "ref: main", "fetch-depth: 0",
                       "retention-days: 14", 'CANVAS_FORUM_COURSE_ID: "40577"', "TZ: America/New_York",
                       "secrets.CANVAS_FORUM_TOKEN", "secrets.ANTHROPIC_API_KEY", "vars.CANVAS_BASE_URL",
                       "vars.CANVAS_FORUM_TOPIC_ID", 'user.name "syllabi-agent[bot]"', "git add state/",
                       "git pull --rebase origin main"):
            self.assertIn(needle, self.text)

    def test_no_tracing_or_echoed_secrets(self):
        secret_var = r"\$\{?[A-Z0-9_]*(?:%s)[A-Z0-9_]*" % "|".join(SECRET_WORDS)
        for n, line in enumerate(self.lines, 1):
            code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
            self.assertNotRegex(code, r"set\s+-[a-z]*x", "line %d" % n)
            self.assertNotRegex(code, r"\b(?:echo|printf)\b[^\n]*" + secret_var, "line %d" % n)
            self.assertNotRegex(code, r"\b(?:env|printenv)\s*(?:$|\||>)", "line %d" % n)

    def test_secrets_only_in_the_cycle_step(self):
        steps = re.split(r"\n      - name: ", self.text)
        with_secrets = [s.split("\n", 1)[0] for s in steps if "secrets." in s]
        self.assertEqual(with_secrets, ["cycle"])

    def test_inputs_never_interpolated_into_shell(self):
        # `${{ inputs.* }}` inside `run:` would be script injection; they go through env only.
        for block in re.findall(r"run: \|\n((?:          .*\n|\n)+)", self.text):
            self.assertNotIn("inputs.", block)

    def test_commit_step_always_runs(self):
        commit = self.text.split("- name: commit state", 1)[1].split("- name:", 1)[0]
        self.assertIn("if: always()", commit)
        self.assertIn("git diff --cached --quiet", commit)


class StateDirTests(unittest.TestCase):
    def test_state_skeleton_is_committed(self):
        for rel in ("state/README.md", "state/memory/.gitkeep", "state/logs/.gitkeep", "state/work/forum/.gitkeep"):
            self.assertTrue(os.path.exists(os.path.join(REPO, rel)), rel)

    def test_gitignore_keeps_state_and_drops_scratch(self):
        try:
            subprocess.run(["git", "--version"], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git not available")
        cases = {
            "state/memory/forum-state.json": False, "state/memory/forum-halt": False,
            "state/memory/hw4-state.json": False, "state/logs/forum.jsonl": False, "state/logs/hw4.jsonl": False,
            "state/memory/hw4-state.json.lock": True,
            "state/memory/forum-state.json.lock": True, "state/memory/.forum-state.json.x.tmp": True,
            "state/work/forum/post-1.txt": True, "memory/forum-state.json": True, "logs/forum.jsonl": True,
            "data/memory/forum-state.json": True,
        }
        for rel, ignored in cases.items():
            rc = subprocess.run(["git", "-C", REPO, "check-ignore", "-q", "--no-index", rel]).returncode
            self.assertEqual(rc == 0, ignored, rel)


class LogStatusTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 12, 17, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"DATA_DIR": self.tmp.name, "GITHUB_RUN_ID": "987"}

    def lines(self):
        with open(os.path.join(self.tmp.name, "logs", "forum.jsonl"), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh]

    def test_ok_status_becomes_one_line_of_counts(self):
        status = {"ok": True, "control": "RUNNING", "halted": False, "self_user_id": 42, "posts_last_hour": 1,
                  "attempts_last_hour": 2, "rate_limit": 3, "halt": {"note": "free text"}, "extra": "x"}
        self.assertEqual(hw4_log_status.main([], stdin=io.StringIO(json.dumps(status)), env=self.env,
                                             now=self.NOW), 0)
        [line] = self.lines()
        self.assertEqual((line["command"], line["run_id"], line["ts"]), ("status", "987", "2026-10-12T17:00:00Z"))
        self.assertEqual((line["control"], line["attempts_last_hour"]), ("RUNNING", 2))
        self.assertNotIn("halt", line)
        self.assertNotIn("extra", line)

    def test_error_status_logs_only_the_code(self):
        status = {"ok": False, "error": {"code": "USAGE", "message": "CANVAS_FORUM_TOPIC_ID is not set"}}
        hw4_log_status.main([], stdin=io.StringIO(json.dumps(status)), env=self.env, now=self.NOW)
        [line] = self.lines()
        self.assertEqual((line["decision"], line["error_code"]), ("error", "USAGE"))
        self.assertNotIn("CANVAS_FORUM_TOPIC_ID", json.dumps(line))

    def test_garbage_writes_nothing(self):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hw4_log_status.main([], stdin=io.StringIO("not json"), env=self.env), 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "logs")))


if __name__ == "__main__":
    unittest.main()
