"""Tests for HW4-4 (SYL-115): checking and using the delivered tests.

workspace/hw4/checks.py (extraction, static checks), workspace/hw4/sandbox.py (the isolated run),
workspace/hw4/task/control_state_tests/verify.py (the job's checks) and the accept / correct /
reject flow in scripts/hw4-cycle.py. No network: the fake Canvas and fake model from
tests/test_hw4_cycle.py, and a stub sandbox runner. One integration test runs the real docker
sandbox when docker is available.

    python3 -m unittest discover -s tests -v
"""
import ast
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)

import test_hw4_cycle as base  # noqa: E402  (shared fakes; its TestCases are not re-run from here)

hw4, forum, tf = base.hw4, base.forum, base.tf
checks, sandbox = hw4.checks, hw4.sandbox
VERIFY = hw4.Task().verify

with open(os.path.join(TESTS, "fixtures", "hw4_good_tests.py"), encoding="utf-8") as _fh:
    GOOD = _fh.read()
PROVIDER, OTHER = base.OTHER_B, base.OTHER_A


def fenced(code, before="Here are your tests:", after="Thanks for the job."):
    return "%s\n\n```python\n%s```\n\n%s" % (before, code, after)


class StubRunner:
    """The sandbox, faked: the outcome per implementation (correct / m1 / m2) is given; every call
    is recorded."""

    def __init__(self, correct="pass", m1="fail", m2="fail"):
        self.outcome = {"correct": correct, "m1": m1, "m2": m2}
        self.calls = []

    def __call__(self, files, workdir):
        impl = files["control_state.py"]
        which = "m1" if "Mutant 1" in impl else "m2" if "Mutant 2" in impl else "correct"
        self.calls.append((which, sorted(files)))
        if self.outcome[which] == "pass":
            return {"exit": 0, "timed_out": False, "mode": "stub",
                    "output": "test_a ... ok\ntest_b ... ok\ntest_c ... ok\n\n" + "-" * 70 +
                              "\nRan 3 tests in 0.001s\n\nOK\n"}
        return {"exit": 1, "timed_out": False, "mode": "stub",
                "output": "test_a ... FAIL\n\n" + "=" * 70 + "\nFAIL: test_a (test_delivered.TestControlState."
                          "test_a)\nAssertionError: 'RUNNING' != 'PAUSED'\n\n" + "-" * 70 +
                          "\nRan 3 tests in 0.001s\n\nFAILED (failures=1)\n"}


# --------------------------------------------------------------------------- extraction


class ExtractTests(unittest.TestCase):
    def test_fenced_block(self):
        code = checks.extract_code(fenced(GOOD))
        self.assertEqual(code, GOOD)

    def test_several_fenced_blocks_are_joined_when_they_parse_together(self):
        text = "Imports:\n```\nimport unittest\n```\nThe tests:\n```py\nclass T(unittest.TestCase):\n    pass\n```"
        self.assertEqual(checks.extract_code(text), "import unittest\n\nclass T(unittest.TestCase):\n    pass\n")

    def test_a_non_python_fence_is_ignored(self):
        text = "```bash\npython -m unittest\n```\n\n```python\n%s```" % GOOD
        self.assertEqual(checks.extract_code(text), GOOD)

    def test_indented_block(self):
        body = "\n".join("    " + line if line else "" for line in GOOD.splitlines())
        code = checks.extract_code("My tests below.\n\n%s\n\nRun them with unittest." % body)
        self.assertEqual(code, GOOD)

    def test_bare_code_between_prose(self):
        code = checks.extract_code("Hello! Here is the file.\n%s\nLet me know if it works." % GOOD)
        self.assertEqual(code.strip(), GOOD.split("\n", 2)[2].strip())     # from `import unittest` on
        ast.parse(code)

    def test_prose_only_is_none(self):
        self.assertIsNone(checks.extract_code("Thanks, I am working on it and will reply tomorrow."))
        self.assertIsNone(checks.extract_code(""))

    def test_canvas_html_keeps_indentation_through_entry_keep_indent(self):
        html = forum.render_message(fenced(GOOD))
        code = checks.extract_code(forum.html_to_code_text(html))
        self.assertEqual(ast.dump(ast.parse(code)), ast.dump(ast.parse(GOOD)))   # blank-line runs collapse
        flat = forum.html_to_text(html)                    # the plain read loses the indentation
        self.assertNotEqual(checks.extract_code(flat), GOOD)

    def test_pre_block(self):
        html = "<p>Tests:</p><pre>%s</pre>" % GOOD.replace("<", "&lt;")
        code = checks.extract_code(forum.html_to_code_text(html))
        self.assertEqual(ast.dump(ast.parse(code)), ast.dump(ast.parse(GOOD)))


# --------------------------------------------------------------------------- static checks


class StaticCheckTests(unittest.TestCase):
    def check(self, code):
        return checks.static_check(code, VERIFY.ALLOWED, min_tests=3)

    def test_the_good_fixture_passes(self):
        self.assertIsNone(self.check(GOOD))

    def test_import_os_is_rejected(self):
        self.assertIn("imports os", self.check("import os\n" + GOOD))
        self.assertIn("imports os.path", self.check("import os.path\n" + GOOD))
        self.assertIn("imports subprocess", self.check("from subprocess import run\n" + GOOD))

    def test_open_eval_exec_and_dunder_import_are_rejected(self):
        for bad in ("open('x')", "eval('1')", "exec('1')", "__import__('os')", "getattr(1, '__subclasses__')",
                    "().__class__.__bases__"):
            src = GOOD.replace("    def test_running_line(self):\n",
                               "    def test_running_line(self):\n        %s\n" % bad)
            self.assertIsNotNone(self.check(src), bad)

    def test_unlisted_stdlib_module_is_rejected(self):
        self.assertIn("not on the allow-list", self.check("import json\n" + GOOD))

    def test_relative_import_is_rejected(self):
        self.assertIn("relative import", self.check("from . import control_state\n" + GOOD))

    def test_fewer_than_three_test_methods_is_rejected(self):
        src = ("import unittest\nfrom control_state import control_state\n\nclass T(unittest.TestCase):\n"
               "    def test_a(self):\n        pass\n\n    def test_b(self):\n        pass\n")
        self.assertIn("at least 3 test methods", self.check(src))

    def test_from_unittest_import_testcase_counts(self):
        src = ("from unittest import TestCase\nfrom control_state import control_state\n\nclass T(TestCase):\n"
               + "".join("    def test_%d(self):\n        pass\n" % i for i in range(3)))
        self.assertIsNone(self.check(src))

    def test_syntax_error(self):
        self.assertIn("does not parse", self.check("def (:\n"))


# --------------------------------------------------------------------------- verify.py


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_pass_fail_fail_is_ok_with_three_outcome_lines(self):
        runner = StubRunner()
        v = VERIFY.verify(GOOD, self.tmp.name, runner=runner)
        self.assertTrue(v["ok"])
        self.assertEqual({k: v["checks"][k] for k in ("static", "correct", "m1", "m2")},
                         {"static": "pass", "correct": "pass", "m1": "pass", "m2": "pass"})
        self.assertEqual(len(v["lines"]), 3)
        self.assertTrue(v["lines"][0].startswith("Passed on the real function"))
        self.assertTrue(v["lines"][1].startswith("Failed on mutant 1"))
        self.assertTrue(v["lines"][2].startswith("Failed on mutant 2"))
        self.assertEqual([c[0] for c in runner.calls], ["correct", "m1", "m2"])
        self.assertEqual(runner.calls[0][1], ["control_state.py", "test_delivered.py"])

    def test_a_weak_suite_names_the_mutant_it_missed(self):
        v = VERIFY.verify(GOOD, self.tmp.name, runner=StubRunner(m2="pass"))
        self.assertFalse(v["ok"])
        self.assertEqual(v["checks"]["m2"], "fail")
        self.assertIn("mutant 2", v["summary"])
        self.assertNotIn("mutant 1", v["summary"])

    def test_failing_on_the_real_function_quotes_the_failure(self):
        v = VERIFY.verify(GOOD, self.tmp.name, runner=StubRunner(correct="fail"))
        self.assertFalse(v["ok"])
        self.assertTrue(v["checks"]["output_excerpt"].startswith("FAIL: test_a"))
        self.assertLessEqual(len(v["checks"]["output_excerpt"]), 600)

    def test_static_failure_runs_nothing(self):
        runner = StubRunner()
        v = VERIFY.verify("import os\n" + GOOD, self.tmp.name, runner=runner)
        self.assertFalse(v["ok"])
        self.assertEqual(v["checks"]["static"], "fail")
        self.assertEqual(runner.calls, [])

    def test_real_subprocess_sandbox_on_the_good_fixture(self):
        with mock.patch.dict(os.environ, {"HW4_SANDBOX": "subprocess"}):
            v = VERIFY.verify(GOOD, self.tmp.name)
        self.assertTrue(v["ok"], v)
        self.assertEqual(os.listdir(self.tmp.name), [])          # the sandbox directories are removed

    def test_adopt_header_and_import_path(self):
        rel, text = VERIFY.adopt(GOOD, ["one", "two", "three"])
        self.assertEqual(rel, os.path.join("tests", "test_hw4_delegated_control_state.py"))
        self.assertTrue(text.startswith("# one\n# two\n# three\n"))
        ast.parse(text)
        self.assertIn('"control_state_tests", "files"', text)


# --------------------------------------------------------------------------- sandbox.py


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def captured_env(self, how):
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, env=kw.get("env"))
            return subprocess.CompletedProcess(argv, 0, stdout=b"Ran 1 test\n\nOK\n")

        parent = {"CANVAS_FORUM_TOKEN": "tok-SECRET-0123456789", "ANTHROPIC_API_KEY": "sk-ant-SECRET-0123456789",
                  "HOME": "/home/runner", "GITHUB_TOKEN": "ghs_SECRET0123456789", "HW4_SANDBOX": how}
        with mock.patch.dict(os.environ, parent), mock.patch.object(sandbox.subprocess, "run", fake_run), \
                mock.patch.object(sandbox.shutil, "which", lambda name, path=None: "/usr/bin/docker"):
            sandbox.run_unittest({"test_x.py": "", "control_state.py": ""}, self.tmp.name)
        return seen

    def test_child_env_has_no_key_from_the_parent_env(self):
        for how in ("docker", "subprocess"):
            seen = self.captured_env(how)
            self.assertEqual(set(seen["env"]), {"PATH", "PYTHONDONTWRITEBYTECODE"}, how)
            self.assertEqual(seen["env"]["PATH"], sandbox.SAFE_PATH)
            for value in ("tok-SECRET", "sk-ant-SECRET", "ghs_SECRET", "/home/runner"):
                self.assertNotIn(value, json.dumps(seen), how)

    def test_docker_command_isolates_the_run(self):
        argv = self.captured_env("docker")["argv"]
        line = " ".join(argv)
        for part in ("--rm", "--network none", "--memory 512m", "--pids-limit 128", "--cpus 1", "--read-only",
                     "--tmpfs /tmp", ":/work:ro", "-w /work", "python:3.12-slim timeout 60 python -m unittest -v"):
            self.assertIn(part, line)
        self.assertNotIn("--env-file", line)
        self.assertEqual([a for a in argv if a == "-e"], ["-e"])        # only PYTHONDONTWRITEBYTECODE

    def test_docker_is_the_default_and_subprocess_only_when_asked(self):
        self.assertEqual(sandbox.mode({}), "docker")
        self.assertEqual(sandbox.mode({"HW4_SANDBOX": "yes"}), "docker")
        self.assertEqual(sandbox.mode({"HW4_SANDBOX": "subprocess"}), "subprocess")

    def test_missing_docker_is_a_sandbox_error(self):
        with mock.patch.dict(os.environ, {"HW4_SANDBOX": ""}), \
                mock.patch.object(sandbox.shutil, "which", lambda name, path=None: None):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.run_unittest({"test_x.py": ""}, self.tmp.name)

    def test_real_child_sees_no_parent_secret(self):
        probe = ("import unittest\nimport os\n\nclass T(unittest.TestCase):\n    def test_env(self):\n"
                 "        self.assertEqual(sorted(os.environ.get(k, '') for k in ('HW4_PROBE_SECRET',)), [''])\n")
        with mock.patch.dict(os.environ, {"HW4_SANDBOX": "subprocess", "HW4_PROBE_SECRET": "x" * 20}):
            run = sandbox.run_unittest({"test_probe.py": probe}, self.tmp.name)
        self.assertTrue(sandbox.passed(run), run["output"])

    def test_bad_file_name_is_refused(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.run_unittest({"../evil.py": ""}, self.tmp.name, env={"HW4_SANDBOX": "subprocess"})

    def test_outcome_helpers(self):
        ok = StubRunner()({"control_state.py": ""}, None)
        bad = StubRunner(correct="fail")({"control_state.py": ""}, None)
        self.assertTrue(sandbox.passed(ok) and not sandbox.failed(ok))
        self.assertTrue(sandbox.failed(bad) and not sandbox.passed(bad))
        self.assertEqual(sandbox.counts(bad), (3, 1))
        self.assertFalse(sandbox.failed({"exit": 124, "timed_out": True, "output": "FAIL: x"}))


def _docker_ok():
    docker = shutil.which("docker", path=sandbox.SAFE_PATH)
    if not docker:
        return False
    try:
        return subprocess.run([docker, "image", "inspect", sandbox.IMAGE], env=sandbox.child_env(),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_docker_ok(), "docker (with %s pulled) is not available" % sandbox.IMAGE)
class DockerSandboxTests(unittest.TestCase):
    def test_real_docker_sandbox_on_the_good_fixture(self):
        with tempfile.TemporaryDirectory(dir=REPO) as tmp, mock.patch.dict(os.environ, {"HW4_SANDBOX": ""}):
            v = VERIFY.verify(GOOD, tmp)
        self.assertTrue(v["ok"], v)
        self.assertEqual({v["checks"][k] for k in ("correct", "m1", "m2")}, {"pass"})


# --------------------------------------------------------------------------- the cycle


class ResultFlowTests(base.Hw4TestCase):
    """The request goes to entry 102 (PROVIDER) at 14:00; results come back as replies to it."""

    def setUp(self):
        super().setUp()
        self.adopt_root = tempfile.TemporaryDirectory()
        self._orig_seams = (hw4.run_sandbox, hw4.ADOPT_ROOT)
        hw4.ADOPT_ROOT = self.adopt_root.name
        self.runner = self.stub()
        self.two_offers()
        self.model(base.classify((101, "offer"), (102, "offer")), base.rank(102, (101, 1), (102, 3)))
        self.cycle()
        self.req = self.state()["request"]
        self.next_id = 400

    def tearDown(self):
        hw4.run_sandbox, hw4.ADOPT_ROOT = self._orig_seams
        self.adopt_root.cleanup()
        super().tearDown()

    def stub(self, **outcome):
        self.runner = StubRunner(**outcome)
        hw4.run_sandbox = self.runner
        return self.runner

    def reply(self, text, parent=None, user=PROVIDER, kind="result_for_me"):
        """A reply in the request thread, as Canvas would store it (rendered HTML), classified as `kind`."""
        eid = self.next_id
        self.next_id += 1
        parent = parent or self.req["request_entry_id"]
        self.fake.add(eid, user, "x", parent_id=parent)
        self.fake.entries[eid]["message"] = forum.render_message(text)
        self.model(base.classify((eid, kind)))
        return eid

    def adopted(self):
        return os.path.join(self.adopt_root.name, "tests", "test_hw4_delegated_control_state.py")

    def verdicts(self):
        return [x for x in self.hw4_log() if x.get("event") == "verdict"]

    def test_pass_fail_fail_accepts_tips_5_and_adopts_the_tests(self):
        eid = self.reply(fenced(GOOD))
        body = self.cycle(now="2026-10-05T16:00:00Z")
        parent, text = self.posted()[-1]
        self.assertEqual(parent, eid)
        self.assertIn("accepted", text)
        self.assertIn("Passed on the real function", text)
        self.assertIn("Failed on mutant 1", text)
        self.assertIn("Failed on mutant 2", text)
        self.assertIn("Tip: 5 coins for request %d, because" % self.req["request_entry_id"], text)
        req = self.state()["request"]
        self.assertEqual((req["status"], req["tip"], req["attempts"], req["result_entry_id"]), ("accepted", 5, 1, eid))
        self.assertEqual({k: req["checks"][k] for k in ("static", "correct", "m1", "m2")},
                         {"static": "pass", "correct": "pass", "m1": "pass", "m2": "pass"})
        self.assertEqual(req["adopted"], "tests/test_hw4_delegated_control_state.py")
        with open(self.adopted(), encoding="utf-8") as fh:
            adopted = fh.read()
        ast.parse(adopted)
        self.assertTrue(all(line.startswith("# ") for line in adopted.splitlines()[:3]))
        self.assertTrue(adopted.splitlines()[3].startswith("import os as _os"))
        self.assertIn("forum entry %d" % eid, adopted.splitlines()[0])
        self.assertIn("2026-10-05", adopted.splitlines()[1])
        self.assertIn("another student's agent, accepted after the checks in docs/hw4-writeup.md", adopted)
        with open(os.path.join(self.data, "work", "hw4", "result-%d.py" % eid), encoding="utf-8") as fh:
            self.assertEqual(ast.dump(ast.parse(fh.read())), ast.dump(ast.parse(GOOD)))
        [v] = self.verdicts()
        self.assertEqual((v["decision"], v["tip"], v["result_entry_id"]), ("accepted", 5, eid))
        self.assertIn("accept posted to entry %d" % eid, body["reason"])
        # accepted is final: later cycles neither re-check nor send a new request
        self.model()
        self.cycle(now="2026-10-05T18:00:00Z")
        self.assertEqual(len(self.runner.calls), 3)

    def test_the_adopted_file_runs_in_the_suite_layout(self):
        self.reply(fenced(GOOD))
        self.cycle(now="2026-10-05T16:00:00Z")
        os.symlink(os.path.join(REPO, "workspace"), os.path.join(self.adopt_root.name, "workspace"))
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_hw4_delegated_*.py",
                              "-v"], cwd=self.adopt_root.name,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout.decode())
        self.assertIn("Ran 5 tests", run.stdout.decode())

    def test_weak_suite_gets_one_correction_request_naming_the_mutant(self):
        self.stub(m2="pass")
        eid = self.reply(fenced(GOOD))
        self.cycle(now="2026-10-05T16:00:00Z")
        parent, text = self.posted()[-1]
        self.assertEqual(parent, eid)
        self.assertIn("correction requested", text)
        self.assertIn("mutant 2", text)
        self.assertIn("Ran 3 tests", text)                                # the run that should have failed
        message = urllib.parse.parse_qs(self.fake.posts()[-1][3].decode())["message"][0]
        self.assertIn("&nbsp;" * 4 + "Ran 3 tests", message)              # quoted as an indented block
        req = self.state()["request"]
        self.assertEqual((req["status"], req["attempts"]), ("correction_requested", 1))
        self.assertEqual(req["deadline"], "2026-10-06T04:00:00Z")          # now + 12 h
        self.assertIsNotNone(req["correction_entry_id"])
        self.assertIsNone(req["tip"])
        self.assertEqual(self.verdicts()[-1]["decision"], "correction_requested")

    def test_second_failure_rejects_with_tip_0_and_the_provider_is_not_asked_again(self):
        self.stub(m2="pass")
        self.reply(fenced(GOOD))
        self.cycle(now="2026-10-05T16:00:00Z")
        correction = self.state()["request"]["correction_entry_id"]
        second = self.reply(fenced(GOOD), parent=correction)
        self.model(base.classify((second, "result_for_me")), base.rank(None, (101, 1)))
        self.cycle(now="2026-10-05T18:00:00Z")
        parent, text = self.posted()[-1]
        self.assertEqual(parent, second)
        self.assertIn("rejected", text)
        self.assertIn("No tip", text)
        st = self.state()
        req = st["request"]
        self.assertEqual((req["status"], req["tip"], req["attempts"]), ("rejected", 0, 2))
        self.assertEqual(st["tried_providers"], [102])
        self.assertEqual(req["past_results"][0]["checks"]["m2"], "fail")
        self.assertEqual([v["decision"] for v in self.verdicts()], ["correction_requested", "rejected"])
        self.assertEqual(st["discovery"][-1]["candidates"], [101])          # back to choosing, 102 excluded

    def test_corrected_result_that_passes_is_tipped_3(self):
        self.stub(m1="pass")
        self.reply(fenced(GOOD))
        self.cycle(now="2026-10-05T16:00:00Z")
        self.stub()
        second = self.reply(fenced(GOOD), parent=self.state()["request"]["correction_entry_id"])
        self.cycle(now="2026-10-05T18:00:00Z")
        req = self.state()["request"]
        self.assertEqual((req["status"], req["tip"], req["result_entry_id"]), ("accepted", 3, second))
        self.assertIn("Tip: 3 coins for request %d" % req["request_entry_id"], self.posted()[-1][1])

    def test_decline_rejects_without_running_anything(self):
        self.reply("Sorry, I cannot do this one.", kind="decline_for_me")
        self.model(base.classify((400, "decline_for_me")), base.rank(None, (101, 1)))
        before = len(self.fake.posts())
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(len(self.fake.posts()), before)
        st = self.state()
        self.assertEqual((st["request"]["status"], st["request"]["reason"]), ("rejected", "provider declined"))
        self.assertEqual(st["tried_providers"], [102])
        self.assertEqual(self.verdicts()[-1]["decision"], "rejected")

    def test_a_question_is_answered_once_and_pushes_the_deadline_once(self):
        q = self.reply("Should the tests also cover None input?", kind="question_for_me")
        m = self.model(base.classify((q, "question_for_me")), {"answer": "Yes: empty input is UNKNOWN."})
        self.cycle(now="2026-10-05T16:00:00Z")
        parent, text = self.posted()[-1]
        self.assertEqual(parent, q)
        self.assertIn("Yes: empty input is UNKNOWN.", text)
        self.assertEqual(m.schemas[-1], hw4.ANSWER_SCHEMA)
        self.assertIn("UNTRUSTED-", m.calls[-1][1])
        req = self.state()["request"]
        self.assertEqual((req["status"], req["deadline"]), ("sent", "2026-10-07T02:00:00Z"))
        q2 = self.reply("And what about tabs?", kind="question_for_me")
        self.model(base.classify((q2, "question_for_me")))                 # no second answer call
        before = len(self.fake.posts())
        self.cycle(now="2026-10-05T18:00:00Z")
        self.assertEqual(len(self.fake.posts()), before)
        self.assertEqual(self.state()["request"]["deadline"], "2026-10-07T02:00:00Z")

    def test_chatter_without_code_is_not_a_result(self):
        self.reply("Got it, working on it!", kind="other")
        before = len(self.fake.posts())
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertEqual(len(self.fake.posts()), before)
        self.assertEqual(self.state()["request"]["status"], "sent")
        self.assertEqual(self.state()["inbox"][-1]["handled_as"], "no_code")

    def test_a_result_with_no_code_gets_a_correction_request(self):
        self.reply("All done, the tests pass!", kind="result_for_me")
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertIn("no Python code", self.posted()[-1][1])
        self.assertEqual(self.state()["request"]["checks"]["static"], "fail")
        self.assertEqual(self.runner.calls, [])

    def test_static_failure_never_reaches_the_sandbox(self):
        self.reply(fenced("import os\n" + GOOD))
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertEqual(self.runner.calls, [])
        self.assertIn("imports os", self.posted()[-1][1])
        self.assertEqual(self.state()["request"]["checks"]["static"], "fail")

    def test_the_providers_work_for_someone_else_is_ignored(self):
        self.fake.add(390, OTHER, "please test my code", parent_id=102)
        eid = self.reply(fenced(GOOD), parent=390, kind="other")          # provider answering someone else
        self.model(base.classify((390, "other"), (eid, "other")))
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.state()["request"]["status"], "sent")

    def test_correction_deadline_passing_rejects_without_a_post(self):
        self.stub(m2="pass")
        self.reply(fenced(GOOD))
        self.cycle(now="2026-10-05T16:00:00Z")
        before = len(self.fake.posts())
        self.model(base.rank(None, (101, 1)))
        self.cycle(now="2026-10-06T05:00:00Z")
        st = self.state()
        self.assertEqual((st["request"]["status"], st["request"]["tip"]), ("rejected", 0))
        self.assertEqual(st["tried_providers"], [102])
        self.assertEqual(len(self.fake.posts()), before)

    def test_late_failing_result_is_rejected_at_once(self):
        self.stub(m1="pass")
        self.reply(fenced(GOOD))
        self.model(base.classify((400, "result_for_me")), base.rank(None, (101, 1)))
        self.cycle(now="2026-10-06T15:00:00Z")               # past the 24 h deadline
        self.assertIn("rejected", self.posted()[-1][1])
        self.assertEqual(self.state()["request"]["status"], "rejected")

    def test_a_sandbox_failure_is_an_error_and_the_result_is_checked_again(self):
        def broken(files, workdir):
            raise sandbox.SandboxError("docker is not installed")
        hw4.run_sandbox = broken
        before = len(self.fake.posts())
        self.reply(fenced(GOOD))
        body = self.cycle(now="2026-10-05T16:00:00Z", expect=2)
        self.assertEqual(body["error"]["code"], "HW4_SANDBOX")
        self.assertEqual(len(self.fake.posts()), before)
        self.stub()
        self.model()
        self.cycle(now="2026-10-05T18:00:00Z")
        self.assertEqual(self.state()["request"]["status"], "accepted")

    def test_dry_run_shows_the_verdict_it_would_post_and_writes_nothing(self):
        eid = self.reply(fenced(GOOD))
        with open(self.state_path(), encoding="utf-8") as fh:
            before = fh.read()
        body = self.cycle(now="2026-10-05T16:00:00Z", env=dict(self.env, HW4_DRY_RUN="1"))
        [verdict] = [p for p in body["would_post"] if p["what"] == "accept"]
        self.assertEqual(verdict["reply_to"], eid)
        self.assertIn("Tip: 5 coins", verdict["text"])
        self.assertEqual(body["request"]["status"], "accepted")
        with open(self.state_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertFalse(os.path.exists(self.adopted()))
        self.assertFalse(os.path.exists(os.path.join(self.data, "work", "hw4", "result-%d.py" % eid)))

    def test_no_secret_reaches_the_posted_verdict(self):
        leaky = StubRunner(correct="fail")
        orig = leaky.__call__

        def runner(files, workdir):
            out = orig(files, workdir)
            out["output"] = out["output"].replace("AssertionError", "AssertionError %s" % self.env["ANTHROPIC_API_KEY"])
            return out
        hw4.run_sandbox = runner
        self.reply(fenced(GOOD))
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertIn("correction requested", self.posted()[-1][1])
        self.assertNotIn(self.env["ANTHROPIC_API_KEY"], self.posted()[-1][1])
        self.assertNotIn(self.env["ANTHROPIC_API_KEY"], json.dumps(self.state()))


class TaskPackageTests(base.Hw4TestCase):
    def test_need_txt_goes_into_both_prompts(self):
        need = hw4.Task().need
        self.two_offers()
        m = self.model(base.classify((101, "offer"), (102, "offer")), base.rank(102, (102, 3)))
        self.cycle()
        self.assertIn(need, m.calls[0][1])
        self.assertIn("I need an agent that %s." % need, m.calls[1][1])

    def test_unknown_task_is_a_usage_error(self):
        for name in ("nope", "../templates", "Control_State_Tests"):
            body = self.cycle(env=dict(self.env, HW4_TASK=name), expect=2)
            self.assertEqual(body["error"]["code"], "USAGE", name)
        self.assertEqual(self.fake.calls, [])

    def test_the_request_records_its_task(self):
        self.two_offers()
        self.model(base.classify((101, "offer"), (102, "offer")), base.rank(102, (102, 3)))
        self.cycle()
        self.assertEqual(self.state()["request"]["task"], "control_state_tests")

    def test_package_layout(self):
        root = os.path.join(REPO, "workspace", "hw4", "task")
        self.assertTrue(os.path.isfile(os.path.join(root, "README.md")))
        pkg = os.path.join(root, hw4.DEFAULT_TASK)
        for rel in ("request.md", "need.txt", "verify.py", os.path.join("files", "control_state.py"),
                    os.path.join("mutants", "m1_paused_is_running.py"),
                    os.path.join("mutants", "m2_no_normalization.py")):
            self.assertTrue(os.path.isfile(os.path.join(pkg, rel)), rel)
        with open(os.path.join(pkg, "need.txt"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 1)

    def test_driver_knows_nothing_task_specific(self):
        with open(os.path.join(REPO, "scripts", "hw4-cycle.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("control_state.py", "mutant", '"m1"', '"m2"', "COURSE-TEAM", "TestControlState"):
            self.assertNotIn(word, src)


class WorkflowTests(unittest.TestCase):
    def test_workflow_pulls_the_image_passes_the_task_and_commits_the_adopted_tests(self):
        with open(os.path.join(REPO, ".github", "workflows", "hw4-forum.yml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("docker pull -q python:3.12-slim", text)
        self.assertIn("HW4_TASK: ${{ vars.HW4_TASK }}", text)
        self.assertIn("git add tests/test_hw4_delegated_*.py", text)
        self.assertNotIn("HW4_SANDBOX", text)                       # the workflow uses docker
        pull = text[text.index("- name: sandbox image"):text.index("- name: cycle")]
        self.assertNotIn("secrets.", pull)


if __name__ == "__main__":
    unittest.main()
