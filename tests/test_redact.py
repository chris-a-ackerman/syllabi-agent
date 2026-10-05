"""Tests for scripts/redact-evidence.py (SYL-109): secrets, emails and other users' ids out of evidence.

No network. DATA_DIR always points at a temp dir, so a real /data/memory/forum-state.json is never read.

    python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "redact-evidence.py")

_spec = importlib.util.spec_from_file_location("redact_evidence", SCRIPT)
redact_evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redact_evidence)

TOKEN = "7867~FakeCanvasTokenValue0123456789abcdef"
AGENT_TOKEN = "syl_agent_" + "A" * 43


class RedactTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"DATA_DIR": self.tmp.name, "CANVAS_FORUM_TOKEN": TOKEN, "CANVAS_FORUM_TOPIC_ID": "123456789"}

    def run_main(self, text, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = redact_evidence.main(list(argv), stdin=io.StringIO(text), stdout=out, stderr=err,
                                    env=self.env if env is None else env)
        return code, out.getvalue(), err.getvalue()


class SecretTests(RedactTestCase):
    def test_secret_named_env_values_are_redacted(self):
        code, out, _ = self.run_main("auth failed for %s at topic 123456789\n" % TOKEN, "--self-id", "1")
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out)
        self.assertIn("[redacted]", out)
        self.assertIn("123456789", out)   # TOPIC_ID is not a secret name

    def test_short_values_and_other_names_are_left_alone(self):
        env = dict(self.env, API_KEY="short", CANVAS_BASE_URL="https://canvas.mit.edu")
        _, out, _ = self.run_main("short https://canvas.mit.edu\n", "--self-id", "1", env=env)
        self.assertEqual(out, "short https://canvas.mit.edu\n")

    def test_token_shapes_are_redacted_without_an_env_value(self):
        text = "a %s b Authorization: Bearer abcdefghijklmnop c mk_%s\n" % (AGENT_TOKEN, "x" * 24)
        out = redact_evidence.redact(text)
        for leaked in (AGENT_TOKEN, "abcdefghijklmnop", "mk_xxxx"):
            self.assertNotIn(leaked, out)
        self.assertEqual(out.count("[redacted]"), 3)

    def test_env_file_values_count_as_secrets(self):
        path = os.path.join(self.tmp.name, ".env")
        with open(path, "w") as fh:
            fh.write("# local\nexport SYLLABI_ANON_KEY='anon-key-value-123'\nCANVAS_FORUM_COURSE_ID=40577\n")
        _, out, _ = self.run_main("key anon-key-value-123 course 40577\n", "--self-id", "1", "--env-file", path,
                                  env={"DATA_DIR": self.tmp.name})
        self.assertEqual(out, "key [redacted] course 40577\n")

    def test_missing_env_file_is_an_error(self):
        code, out, err = self.run_main("x", "--env-file", os.path.join(self.tmp.name, "nope"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read", err)


class EmailTests(RedactTestCase):
    def test_emails_are_replaced(self):
        out = redact_evidence.redact("from chris333@mit.edu and a.b+c@sub.example.co.uk, not @handle\n")
        self.assertEqual(out, "from [email] and [email], not @handle\n")


class UserIdTests(RedactTestCase):
    def test_other_users_get_stable_numbers_and_the_agent_keeps_its_id(self):
        text = ('{"author": "user 555"} {"author": "user 777"} {"author": "user 555"} '
                '{"author": "user 195836"}\n')
        out = redact_evidence.redact(text, self_id="195836")
        self.assertEqual(out, '{"author": "user-1"} {"author": "user-2"} {"author": "user-1"} '
                              '{"author": "user 195836"}\n')

    def test_all_recognised_forms_and_json_stays_valid(self):
        text = json.dumps({"user_id": 555, "other": {"user_id": "777"}, "self_user_id": 1,
                           "url": "https://canvas.mit.edu/courses/40577/users/555?user_id=888",
                           "entry_id": 555}) + "\n"
        out = redact_evidence.redact(text, self_id="1")
        doc = json.loads(out)
        self.assertEqual(doc["user_id"], "user-1")
        self.assertEqual(doc["other"]["user_id"], "user-2")
        self.assertEqual(doc["url"], "https://canvas.mit.edu/courses/40577/users/user-1?user_id=user-3")
        self.assertEqual(doc["self_user_id"], 1)
        self.assertEqual(doc["entry_id"], 555)      # entry ids are not user ids

    def test_self_id_from_the_input(self):
        text = '{"self_user_id": 42, "author": "user 42"}\n{"author": "user 9"}\n'
        code, out, err = self.run_main(text)
        self.assertEqual(code, 0)
        self.assertIn('"author": "user 42"', out)
        self.assertIn('"author": "user-1"', out)
        self.assertEqual(err, "")

    def test_self_id_from_the_state_file(self):
        os.makedirs(os.path.join(self.tmp.name, "memory"))
        with open(os.path.join(self.tmp.name, "memory", "forum-state.json"), "w") as fh:
            json.dump({"version": 1, "self_user_id": 42}, fh)
        _, out, _ = self.run_main("user 42 replied to user 9\n")
        self.assertEqual(out, "user 42 replied to user-1\n")

    def test_unknown_self_id_replaces_everything_and_says_so(self):
        _, out, err = self.run_main("user 42 replied to user 9\n")
        self.assertEqual(out, "user-1 replied to user-2\n")
        self.assertIn("unknown", err)

    def test_bad_self_id_is_a_usage_error(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_main("x", "--self-id", "me")


class CliTests(RedactTestCase):
    def test_stdin_to_stdout(self):
        env = dict(os.environ, **self.env)
        raw = "| 2026-10-06T15:00:00Z | 2 | skip | user 555 asked chris333@mit.edu for %s | recorded |\n" % TOKEN
        res = subprocess.run([sys.executable, SCRIPT, "--self-id", "1"], input=raw, env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "| 2026-10-06T15:00:00Z | 2 | skip | user-1 asked [email] for [redacted] | recorded |\n")


if __name__ == "__main__":
    unittest.main()
