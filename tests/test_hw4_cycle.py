"""Tests for scripts/hw4-cycle.py, the HW4 driver (SYL-113, HW4-2).

No network: forum.py's transport is the fake Canvas from tests/test_forum.py, and the model is a fake
that returns canned JSON per call and records every prompt.

    python3 -m unittest discover -s tests -v
"""
import datetime as dt
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)

import test_forum as tf  # noqa: E402  (the fake Canvas; its TestCases are not re-run from here)

_spec = importlib.util.spec_from_file_location("hw4_cycle", os.path.join(REPO, "scripts", "hw4-cycle.py"))
hw4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hw4)
forum = hw4.forum
assert forum is tf.forum, "the driver and the fake must share one forum module"

NOW = tf.NOW
OTHER_A, OTHER_B = 7, 8


class FakeModel:
    """Canned answers in order: a JSON-able value, a raw string, or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.schemas = []

    def __call__(self, system, user, **kw):
        self.calls.append((system, user))
        self.schemas.append(kw.get("schema"))
        if not self.answers:
            raise AssertionError("unexpected model call:\n" + user[:500])
        a = self.answers.pop(0)
        if callable(a):
            a = a(user)
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps(a)


def classify(*items):
    return {"entries": [{"id": i, "kind": k, "capability": "cap %s" % i, "confidence": 0.9} for i, k in items]}


def rank(chosen, *fits, reason="best fit"):
    """A ranking answer written with entry ids; turned into the prompt's offer numbers when the fake
    model is called (an id that is not in the prompt becomes number 99)."""
    def answer(prompt):
        number = {int(e): int(n) for n, e in re.findall(r"offer #(\d+) \(forum entry (\d+)", prompt)}
        return {"chosen_offer": None if chosen is None else number.get(chosen, 99), "reason": reason,
                "ranking": [{"offer": number.get(i, 99), "fit": f, "why": "w"} for i, f in fits]}
    return answer


def offered(prompt):
    return re.findall(r"offer #\d+ \(forum entry (\d+)", prompt)


class Hw4TestCase(unittest.TestCase):
    def setUp(self):
        self.fake = tf.FakeCanvas()
        self._orig = (forum.transport, forum.sleep, forum.clock, hw4.ask_model)
        forum.transport = self.fake
        forum.sleep = lambda s: None
        forum.clock = lambda: 1000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.env = {"CANVAS_BASE_URL": tf.BASE, "CANVAS_FORUM_TOKEN": tf.TOKEN, "CANVAS_FORUM_TOPIC_ID": tf.TOPIC,
                    "DATA_DIR": self.data, "ANTHROPIC_API_KEY": "sk-ant-fake-key-0123456789"}

    def tearDown(self):
        forum.transport, forum.sleep, forum.clock, hw4.ask_model = self._orig
        self.tmp.cleanup()

    def model(self, *answers):
        hw4.ask_model = FakeModel(*answers)
        return hw4.ask_model

    def cycle(self, now=NOW, env=None, expect=0):
        out = io.StringIO()
        code = hw4.main(["--now", now, "cycle"], env=env or self.env, out=out)
        raw = out.getvalue()
        self.assertEqual(raw.count("\n"), 1, raw)
        body = json.loads(raw)
        self.assertEqual(code, expect, body)
        return body

    def state_path(self):
        return os.path.join(self.data, "memory", "hw4-state.json")

    def state(self):
        with open(self.state_path(), encoding="utf-8") as fh:
            return json.load(fh)

    def forum_log(self):
        path = os.path.join(self.data, "logs", "forum.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh]

    def hw4_log(self):
        with open(os.path.join(self.data, "logs", "hw4.jsonl"), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh]

    def posted(self):
        """[(parent_id or None, plain text)] for every POST the fake Canvas received."""
        out = []
        for _, url, _, data in self.fake.posts():
            m = re.search(r"/entries/(\d+)/replies$", urllib.parse.urlparse(url).path)
            msg = urllib.parse.parse_qs(data.decode())["message"][0]
            out.append((int(m.group(1)) if m else None, forum.html_to_text(msg)))
        return out

    def two_offers(self):
        self.fake.add(101, OTHER_A, "I offer: slide decks from bullet points.")
        self.fake.add(102, OTHER_B, "I offer: Python unit tests and code review, stdlib only.")


class FirstCycleTests(Hw4TestCase):
    def test_first_cycle_posts_the_offer_then_requests_the_better_ranked_provider(self):
        self.two_offers()
        m = self.model(classify((101, "offer"), (102, "offer")), rank(102, (101, 0), (102, 3)))
        body = self.cycle()
        posts = self.posted()
        self.assertEqual([p[0] for p in posts], [None, 102])
        self.assertIn("Service: syllabus to calendar", posts[0][1])
        self.assertIn("REQUEST", posts[1][1])
        self.assertEqual(body["posts"], 2)
        st = self.state()
        self.assertIsNotNone(st["offer"]["entry_id"])
        self.assertEqual((st["request"]["provider_entry_id"], st["request"]["status"]), (102, "sent"))
        self.assertEqual(st["request"]["provider_author"], "user %d" % OTHER_B)
        self.assertEqual(st["request"]["deadline"], "2026-10-06T14:00:00Z")
        self.assertEqual([o["entry_id"] for o in st["offers_seen"]], [101, 102])
        self.assertTrue(all(len(o["excerpt"]) <= 300 for o in st["offers_seen"]))
        [disc] = st["discovery"]
        self.assertEqual((disc["candidates"], disc["chosen"]), ([101, 102], 102))
        self.assertEqual(len(m.calls), 2)
        self.assertEqual(self.hw4_log()[-1]["posts"], 2)
        self.assertNotIn("skip", [x["command"] for x in self.forum_log()])

    def test_both_calls_ask_for_structured_output(self):
        self.two_offers()
        m = self.model(classify((101, "offer"), (102, "offer")), rank(102, (102, 3)))
        self.cycle()
        self.assertEqual(m.schemas, [hw4.CLASSIFY_SCHEMA, hw4.rank_schema(2)])
        self.assertEqual(m.schemas[1]["properties"]["chosen_offer"]["anyOf"][0]["enum"], [1, 2])

    def test_a_bare_array_answer_still_works(self):
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer"))["entries"], rank(102, (102, 3)))
        self.cycle()
        self.assertEqual(self.state()["request"]["provider_entry_id"], 102)

    def test_second_cycle_with_nothing_new_posts_nothing_and_skips(self):
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer")), rank(102, (102, 3)))
        self.cycle()
        before = len(self.fake.posts())
        m = self.model()                                  # no new entries: no model call
        body = self.cycle(now="2026-10-05T16:00:00Z")
        self.assertEqual(len(self.fake.posts()), before)
        self.assertEqual(body["posts"], 0)
        self.assertEqual(m.calls, [])
        self.assertEqual(self.forum_log()[-1]["command"], "skip")
        self.assertIn("waiting for entry 102", self.forum_log()[-1]["reason"])
        self.assertEqual(self.hw4_log()[-1]["decision"], "skip")

    def test_offer_only_when_nobody_else_has_posted(self):
        self.model()
        self.cycle()
        st = self.state()
        self.assertIsNotNone(st["offer"])
        self.assertIsNone(st["request"])
        self.assertEqual(len(self.fake.posts()), 1)

    def test_duplicate_offer_stores_the_existing_entry(self):
        self.model()
        self.cycle()
        first = self.state()["offer"]["entry_id"]
        os.unlink(self.state_path())                      # the driver forgot; forum.py remembers
        self.model()
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(self.state()["offer"]["entry_id"], first)
        self.assertTrue(self.state()["offer"]["duplicate"])

    def test_model_ids_not_in_the_read_set_are_dropped(self):
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer"), (999, "result_for_me")), rank(None, (102, 1)))
        body = self.cycle()
        self.assertEqual(sum(body["classified"].values()), 2)
        self.assertEqual(self.state()["inbox"], [])

    def test_offers_are_numbered_and_numbers_map_back_to_entry_ids(self):
        self.two_offers()
        m = self.model(classify((101, "offer"), (102, "offer")),
                       {"chosen_offer": 2, "reason": "tests", "ranking": [{"offer": 2, "fit": 3, "why": "w"},
                                                                          {"offer": 1, "fit": 0, "why": "w"}]})
        self.cycle()
        self.assertIn("offer #1 (forum entry 101, author user 7)", m.calls[1][1])
        [disc] = self.state()["discovery"]
        self.assertEqual((disc["chosen"], [r["entry_id"] for r in disc["ranking"]]), (102, [102, 101]))
        self.assertEqual(disc["model_answer"], {"chosen_offer": 2, "ranked_offers": 2})
        self.assertEqual(self.posted()[-1][0], 102)

    def test_chosen_id_outside_the_candidates_is_refused(self):
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer")), rank(555, (102, 3)))
        self.cycle()
        self.assertEqual(len(self.fake.posts()), 1)       # the offer only
        self.assertIsNone(self.state()["discovery"][0]["chosen"])


class StatusTests(Hw4TestCase):
    def test_status_is_read_only_and_reports_the_request(self):
        out = io.StringIO()
        self.assertEqual(hw4.main(["status"], env=self.env, out=out), 0)
        self.assertIsNone(json.loads(out.getvalue())["offer"])
        self.assertFalse(os.path.exists(os.path.join(self.data, "memory")))
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer")), rank(102, (102, 3)))
        self.cycle()
        out = io.StringIO()
        hw4.main(["status"], env=self.env, out=out)
        body = json.loads(out.getvalue())
        self.assertEqual((body["offers_seen"], body["request"]["status"]), (2, "sent"))

    def test_retired_offer_means_the_next_cycle_posts_the_new_one(self):
        # What SYL-113's offer change did by hand: the old offer moved to past_offers, offer reset.
        os.makedirs(os.path.join(self.data, "memory"))
        old = dict(hw4.EMPTY_STATE, past_offers=[{"entry_id": 99, "retired": "service changed"}])
        del old["inbox"]                                   # an older file without newer keys still loads
        with open(self.state_path(), "w") as fh:
            json.dump(old, fh)
        self.model()
        self.cycle()
        [(parent, text)] = self.posted()
        self.assertIsNone(parent)
        self.assertIn("syllabus to calendar", text)
        st = self.state()
        self.assertEqual(st["past_offers"][0]["entry_id"], 99)
        self.assertIsNotNone(st["offer"]["entry_id"])
        self.assertEqual(st["inbox"], [])

    def test_corrupt_state_is_an_error_and_never_replaced(self):
        os.makedirs(os.path.join(self.data, "memory"))
        with open(self.state_path(), "w") as fh:
            fh.write("{not json")
        self.model()
        body = self.cycle(expect=2)
        self.assertEqual(body["error"]["code"], "HW4_STATE")
        with open(self.state_path()) as fh:
            self.assertEqual(fh.read(), "{not json")
        self.assertEqual(self.fake.posts(), [])


class LaterCycleTests(Hw4TestCase):
    def setUp(self):
        super().setUp()
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer")), rank(102, (101, 1), (102, 3)))
        self.cycle()

    def test_timed_out_provider_is_never_asked_again(self):
        self.model()
        self.cycle(now="2026-10-06T15:00:00Z")            # past the 24 h deadline
        st = self.state()
        self.assertEqual(st["request"]["status"], "timed_out")
        self.assertEqual(st["tried_providers"], [102])
        self.assertEqual(st["discovery"][-1]["reason"], "deadline passed")
        m = self.model(rank(101, (101, 2)))
        self.cycle(now="2026-10-06T17:00:00Z")
        self.assertEqual(offered(m.calls[0][1]), ["101"])
        st = self.state()
        self.assertEqual((st["request"]["provider_entry_id"], st["request"]["status"]), (101, "sent"))
        self.assertEqual(st["past_requests"][0]["status"], "timed_out")
        self.assertEqual(self.posted()[-1][0], 101)

    def test_reply_in_the_offer_thread_is_a_request_candidate_whatever_the_model_says(self):
        offer = self.state()["offer"]["entry_id"]
        self.fake.add(300, OTHER_A, "please brief this text", parent_id=offer)
        self.model(classify((300, "other")))
        self.cycle(now="2026-10-05T16:00:00Z")
        [item] = self.state()["inbox"]
        self.assertEqual((item["entry_id"], item["candidate"], item["kind"]), (300, "request_to_me", "other"))

    def test_provider_reply_in_the_request_thread_is_a_result_candidate(self):
        self.fake.add(301, OTHER_B, "here are your tests", parent_id=self.state()["request"]["request_entry_id"])
        self.fake.entries[301]["parent_id"] = self.state()["request"]["request_entry_id"]
        m = self.model(classify((301, "result_for_me")))
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertIn("in_my_request_thread=yes", m.calls[0][1])
        [item] = self.state()["inbox"]
        self.assertEqual(item["candidate"], "result_for_me")


class BudgetAndSafetyTests(Hw4TestCase):
    def test_post_budget_of_two_holds(self):
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer")), rank(102, (102, 3)))
        hw4.POST_BUDGET, orig = 1, hw4.POST_BUDGET
        try:
            body = self.cycle()
        finally:
            hw4.POST_BUDGET = orig
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertIn({"action": "request", "result": "deferred", "reason": "post budget used"}, body["actions"])
        # with the real budget, a cycle never makes more than 2 post calls
        self.assertEqual(hw4.POST_BUDGET, 2)
        with open(os.path.join(REPO, "scripts", "hw4-cycle.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count('self.forum(*argv)'), 1)   # every post goes through Cycle.post

    def test_model_exception_exits_2_and_keeps_only_mechanical_facts(self):
        self.model()
        self.cycle()                                      # offer posted, state written
        before = self.state()
        offer = before["offer"]["entry_id"]
        self.two_offers()
        self.fake.add(103, OTHER_A, "please brief this", parent_id=offer)
        self.model(hw4.model.ModelError("HTTP 529"))
        body = self.cycle(now="2026-10-05T16:00:00Z", expect=2)
        self.assertEqual((body["decision"], body["reason"]), ("error", "model"))
        after = self.state()
        # forum.py has marked 101-103 as seen, so what needs no model is kept ...
        self.assertEqual([o["entry_id"] for o in after["offers_seen"]], [101, 102])
        self.assertNotIn("kind", after["offers_seen"][0])
        [item] = after["inbox"]
        self.assertEqual((item["entry_id"], item["kind"], item["candidate"]), (103, "unclassified", "request_to_me"))
        # ... and no decision is made or recorded
        for key in ("offer", "request", "discovery", "tried_providers", "past_requests"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(self.hw4_log()[-1]["reason"], "model")
        self.assertEqual(self.hw4_log()[-1]["error_message"], "model: HTTP 529")

    def test_offers_saved_after_a_model_failure_are_ranked_next_cycle(self):
        self.two_offers()
        self.model(hw4.model.ModelError("max_tokens"))
        self.cycle(expect=2)
        self.assertEqual(self.fake.posts(), [])             # the offer comes after classification
        m = self.model(rank(102, (101, 0), (102, 3)))      # nothing new: no classify call
        self.cycle(now="2026-10-05T16:00:00Z")
        self.assertEqual(offered(m.calls[0][1]), ["101", "102"])
        self.assertEqual([p[0] for p in self.posted()], [None, 102])

    def test_many_new_entries_are_classified_in_batches(self):
        for i in range(60):
            self.fake.add(200 + i, OTHER_A, "offer number %d" % i)
        answers = [classify(*[(200 + i, "offer") for i in range(start, min(start + 25, 60))])
                   for start in range(0, 60, 25)]
        m = self.model(*answers, rank(None))
        body = self.cycle()
        self.assertEqual(len(m.calls), 4)                  # 25 + 25 + 10, then the ranking
        self.assertEqual(body["classified"], {"offer": 60})
        self.assertEqual([len(re.findall(r"^entry id=", c[1], re.M)) for c in m.calls[:3]], [25, 25, 10])

    def test_dry_run_posts_nothing_and_prints_the_offer_and_request(self):
        self.two_offers()
        self.model(classify((101, "offer"), (102, "offer")), rank(102, (102, 3)))
        body = self.cycle(env=dict(self.env, HW4_DRY_RUN="1"))
        self.assertEqual(self.fake.posts(), [])
        self.assertEqual([w["what"] for w in body["would_post"]], ["offer", "request"])
        self.assertEqual(body["would_post"][1]["reply_to"], 102)
        self.assertFalse(os.path.exists(self.state_path()))
        self.assertEqual(self.forum_log(), [])             # forum.py ran on a throwaway copy
        self.assertFalse(os.path.exists(os.path.join(self.data, "memory", "forum-state.json")))

    def test_paused_control_skips_without_a_model_call(self):
        self.fake.topic_message = "<p>COURSE-TEAM CONTROL: PAUSED</p>"
        self.two_offers()
        m = self.model()
        body = self.cycle()
        self.assertEqual(self.fake.posts(), [])
        self.assertEqual(m.calls, [])
        self.assertEqual(body["reason"], "control PAUSED")
        self.assertEqual(self.forum_log()[-1]["command"], "skip")

    def test_halted_forum_exits_2(self):
        os.makedirs(os.path.join(self.data, "memory"))
        open(os.path.join(self.data, "memory", "forum-halt"), "w").close()
        self.model()
        body = self.cycle(expect=2)
        self.assertEqual(body["error"]["code"], "FORUM_HALTED")
        self.assertEqual(self.fake.posts(), [])

    def test_no_prompt_contains_a_secret_env_value(self):
        self.fake.add(101, OTHER_A, "I offer Python unit tests.")
        self.fake.add(102, OTHER_B, "Code review offer")
        env = dict(self.env, CANVAS_FORUM_TOKEN="canvas-secret-token-XYZ987654", GITHUB_TOKEN="ghp_fakefakefake12345")
        m = self.model(classify((101, "offer"), (102, "offer")), rank(101, (101, 3)))
        self.cycle(env=env)
        self.assertEqual(len(m.calls), 2)
        for system, user in m.calls:
            for value in forum.secret_values(env):
                self.assertNotIn(value, system + user)
        self.assertEqual(len(self.fake.posts()), 2)

    def test_peer_text_is_capped_and_cannot_forge_the_delimiter(self):
        self.fake.add(101, OTHER_A, "A" * 5000)
        m = self.model(classify((101, "offer")), rank(None, (101, 0)))
        self.cycle()
        nonce = re.search(r"<<<UNTRUSTED-([0-9a-f]+)", m.calls[0][1]).group(1)
        self.assertLessEqual(m.calls[0][1].count("A" * 100) * 100, hw4.CLASSIFY_TEXT_CAP)
        self.assertEqual(m.calls[0][1].count("UNTRUSTED-%s>>>" % nonce), 2)    # the instructions + the entry

    def test_three_no_fit_cycles_lower_the_bar(self):
        self.model()
        self.cycle()                                       # offer
        for i, hour in enumerate(("16", "18", "20", "22")):
            self.fake.add(110 + i, OTHER_A, "offer %d" % i)
            m = self.model(classify((110 + i, "offer")), rank(None))
            self.cycle(now="2026-10-05T%s:00:00Z" % hour)
        self.assertIn("so accept", m.calls[1][1])
        self.assertEqual([d["lowered_bar"] for d in self.state()["discovery"]], [False, False, False, True])


class TextTests(unittest.TestCase):
    def test_request_renders_under_2000_chars_with_source_and_deadline(self):
        deadline = dt.datetime(2026, 10, 13, 22, 17, tzinfo=dt.timezone.utc)
        text = hw4.render_request(deadline)
        self.assertLess(len(text.strip()), forum.TEXT_MAX)
        self.assertIn(hw4._fmt_et(deadline), text)
        self.assertIn("Oct 13, 2026", text)
        for line in hw4.task_source().splitlines():
            if line.strip():
                self.assertIn("    " + line, text)
        self.assertIn("TestControlState", text)
        self.assertNotRegex(text, r"\{[a-z_]+\}")
        # the posted HTML keeps the indentation
        html = forum.render_message(text)
        self.assertIn("&nbsp;" * 8 + "for line in", html)

    def test_offer_is_plain_prose_under_2000_chars(self):
        text = hw4.render("offer.md")
        self.assertLess(len(text.strip()), forum.TEXT_MAX)
        for line in text.splitlines():
            self.assertNotRegex(line, r"^\s*(#|[-*] |\d+\. )")

    def test_offer_is_the_syllabus_to_calendar_service(self):
        text = hw4.render("offer.md")
        for phrase in ("syllabus to calendar", "500 to 12,000 characters", "BEGIN:VCALENDAR",
                       "hours per week", "quote verbatim", "within 6 hours", "fewer than three dates"):
            self.assertIn(phrase, text)
        self.assertLessEqual(len([p for p in text.split("\n\n") if p.strip()]), 2)
        self.assertNotIn("brief", text.lower())

    def test_no_hard_coded_provider_or_entry_id(self):
        files = [os.path.join(REPO, "scripts", "hw4-cycle.py")]
        for root, _, names in os.walk(os.path.join(REPO, "workspace", "hw4")):
            files += [os.path.join(root, n) for n in names if n.endswith((".py", ".md"))]
        for path in files:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotRegex(src, r"(?<![\d.-])\d{5,}(?![\d-])", path)
            self.assertNotRegex(src, r"reply.to\W+\d", path)


class TaskTests(unittest.TestCase):
    def load(self, rel):
        spec = importlib.util.spec_from_file_location("cs_" + os.path.basename(rel)[:-3],
                                                      os.path.join(REPO, "workspace", "hw4", "task", "control_state_tests", rel))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.control_state

    def test_correct_function_matches_forum_py(self):
        f = self.load(os.path.join("files", "control_state.py"))
        cases = {"COURSE-TEAM CONTROL: RUNNING\nhello": "RUNNING", "\n  course-team control: paused  \n": "PAUSED",
                 "": "UNKNOWN", "\n\n": "UNKNOWN", "hello\nCOURSE-TEAM CONTROL: RUNNING": "UNKNOWN",
                 "COURSE-TEAM CONTROL: RUNNING!": "UNKNOWN"}
        for text, want in cases.items():
            self.assertEqual(f(text), want, repr(text))
            self.assertEqual(forum.control_state({"message": text.replace("\n", "<br>")}), want, repr(text))

    def test_each_mutant_differs_from_the_correct_function(self):
        good = self.load(os.path.join("files", "control_state.py"))
        m1 = self.load(os.path.join("mutants", "m1_paused_is_running.py"))
        m2 = self.load(os.path.join("mutants", "m2_no_normalization.py"))
        self.assertNotEqual(m1("COURSE-TEAM CONTROL: PAUSED"), good("COURSE-TEAM CONTROL: PAUSED"))
        self.assertEqual(m1("COURSE-TEAM CONTROL: RUNNING"), "RUNNING")
        self.assertNotEqual(m2("  course-team control: running "), good("  course-team control: running "))
        self.assertEqual(m2("COURSE-TEAM CONTROL: PAUSED"), "PAUSED")


class ModelClientTests(unittest.TestCase):
    def setUp(self):
        self._orig = hw4.model._post

    def tearDown(self):
        hw4.model._post = self._orig

    def test_request_shape_and_text(self):
        seen = {}

        def fake_post(url, body, headers, timeout):
            seen.update(url=url, body=json.loads(body), headers=headers, timeout=timeout)
            return 200, json.dumps({"content": [{"type": "text", "text": '```json\n{"a": 1}\n```'}]}).encode()

        hw4.model._post = fake_post
        text = hw4.model.complete("sys", "usr", env={"ANTHROPIC_API_KEY": "k" * 20})
        self.assertEqual(hw4.model.parse_json(text), {"a": 1})
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(seen["body"]["model"], "claude-sonnet-5-5")
        self.assertEqual(seen["body"]["system"], "sys")
        self.assertEqual(seen["body"]["messages"], [{"role": "user", "content": "usr"}])

    def test_low_effort_and_refusal_fallback_are_requested(self):
        seen = {}

        def fake_post(url, body, headers, timeout):
            seen.update(body=json.loads(body), headers=headers)
            return 200, json.dumps({"content": [{"type": "thinking", "thinking": ""},
                                                {"type": "text", "text": "[]"}], "stop_reason": "end_turn"}).encode()

        hw4.model._post = fake_post
        self.assertEqual(hw4.model.complete("s", "u", env={"ANTHROPIC_API_KEY": "k" * 20}), "[]")
        self.assertEqual(seen["body"]["output_config"], {"effort": "low"})
        self.assertEqual(seen["body"]["max_tokens"], 16_000)
        self.assertEqual(seen["body"]["fallbacks"], "default")
        self.assertEqual(seen["headers"]["anthropic-beta"], "server-side-fallback-2026-07-01")
        hw4.model.complete("s", "u", env={"ANTHROPIC_API_KEY": "k" * 20, "HW4_MODEL": "claude-haiku-5-5",
                                          "HW4_EFFORT": "medium"})
        self.assertNotIn("fallbacks", seen["body"])
        self.assertNotIn("anthropic-beta", seen["headers"])
        self.assertEqual(seen["body"]["output_config"], {"effort": "medium"})

    def test_schema_goes_into_output_config_format(self):
        seen = {}

        def fake_post(url, body, headers, timeout):
            seen.update(body=json.loads(body))
            return 200, json.dumps({"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"}).encode()

        hw4.model._post = fake_post
        hw4.model.complete("s", "u", schema=hw4.rank_schema(3), env={"ANTHROPIC_API_KEY": "k" * 20})
        self.assertEqual(seen["body"]["output_config"],
                         {"effort": "low", "format": {"type": "json_schema", "schema": hw4.rank_schema(3)}})
        hw4.model.complete("s", "u", env={"ANTHROPIC_API_KEY": "k" * 20})
        self.assertNotIn("format", seen["body"]["output_config"])

    def test_schemas_use_only_supported_features(self):
        # additionalProperties: false on every object; no numeric or length constraints.
        def walk(node, path):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]), path)
                for bad in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
                    self.assertNotIn(bad, node, path)
                for k, v in node.items():
                    walk(v, path + "/" + k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, "%s/%d" % (path, i))
        walk(hw4.CLASSIFY_SCHEMA, "classify")
        walk(hw4.rank_schema(25), "rank")
        json.dumps(hw4.CLASSIFY_SCHEMA)

    def test_thinking_only_answer_cut_at_max_tokens_is_an_error(self):
        # The first live run: Sonnet 5.5 thinks by default and used the whole budget on thinking.
        hw4.model._post = lambda *a: (200, json.dumps({"content": [{"type": "thinking", "thinking": ""}],
                                                       "stop_reason": "max_tokens"}).encode())
        with self.assertRaisesRegex(hw4.model.ModelError, "max_tokens"):
            hw4.model.complete("s", "u", env={"ANTHROPIC_API_KEY": "k" * 20})
        hw4.model._post = lambda *a: (200, json.dumps({"content": [], "stop_reason": "refusal",
                                                       "stop_details": {"category": "cyber"}}).encode())
        with self.assertRaisesRegex(hw4.model.ModelError, "refusal.*cyber"):
            hw4.model.complete("s", "u", env={"ANTHROPIC_API_KEY": "k" * 20})

    def test_http_error_names_the_api_error(self):
        hw4.model._post = lambda *a: (400, json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "message": "output_config.effort: bad value"}}).encode())
        with self.assertRaisesRegex(hw4.model.ModelError, "HTTP 400: invalid_request_error: output_config"):
            hw4.model.complete("s", "u", env={"ANTHROPIC_API_KEY": "k" * 20})

    def test_errors_raise_model_error(self):
        hw4.model._post = lambda *a: (529, b'{"error": "overloaded"}')
        with self.assertRaises(hw4.model.ModelError):
            hw4.model.complete("s", "u", env={"ANTHROPIC_API_KEY": "k" * 20})
        with self.assertRaises(hw4.model.ModelError):
            hw4.model.complete("s", "u", env={})
        with self.assertRaises(hw4.model.ModelError):
            hw4.model.parse_json("no json here")
        self.assertEqual(hw4.model.parse_json('Sure: [{"id": 1}] done'), [{"id": 1}])


class WorkflowTests(unittest.TestCase):
    def test_workflow_runs_the_driver(self):
        with open(os.path.join(REPO, ".github", "workflows", "hw4-forum.yml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("python3 scripts/hw4-cycle.py cycle", text)


if __name__ == "__main__":
    unittest.main()
