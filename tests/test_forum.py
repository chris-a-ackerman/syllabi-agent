"""Tests for workspace/skills/forum/scripts/forum.py (SYL-107, HW3-1).

No network: the module's `transport`, `sleep` and `clock` hooks are replaced with a stateful fake
Canvas forum (one discussion topic), the same way tests/test_canvas.py fakes canvas.py.

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "workspace", "skills", "forum", "scripts")
sys.path.insert(0, SCRIPTS)

import forum  # noqa: E402

BASE = "https://canvas.example.edu"
HOST = "canvas.example.edu"
TOKEN = "forum-token-ABCDEFGHIJ1234567890"
COURSE = "40577"
TOPIC = "9001"
TOPIC_PATH = "/api/v1/courses/%s/discussion_topics/%s" % (COURSE, TOPIC)
SELF_ID = 42
NOW = "2026-10-05T14:00:00Z"
RUNNING = "<p><strong>COURSE-TEAM CONTROL: RUNNING</strong></p><p>Welcome, agents. Be kind.</p>"


class FakeCanvas:
    """One discussion topic. Entries live in `self.entries`; POSTs create entries owned by SELF_ID.

    post_plan: a queue of behaviours for successive POSTs (default "ok"):
        ok            save and answer 201 with the entry
        save_timeout  save, then raise a network timeout (the acknowledgement is lost)
        timeout       raise a timeout without saving
        500 / 503     answer that status without saving
        save_500      save, then answer 500
        malformed     save, then answer 200 with a non-JSON body
        429           answer 429 (Retry-After: 3) without saving
        403           answer 403 without saving
    """

    def __init__(self):
        self.entries = {}          # id -> {id, user_id, parent_id, created_at, message, deleted}
        self.order = []
        self.next_id = 5000
        self.topic_message = RUNNING
        self.topic_status = 200
        self.view_mode = "ok"      # ok | 503 | empty
        self.self_status = 200
        self.self_headers = {}
        self.post_plan = []
        self.post_time = NOW
        self.page_size = 2         # for the /entries fallback, so pagination is exercised
        self.calls = []            # (method, url, headers, data)
        self.names = {}            # user_id -> display name (only in /view participants)

    # -- setup helpers

    def add(self, eid, user_id, text, parent_id=None, created_at="2026-10-05T12:00:00Z", deleted=False):
        self.entries[eid] = {"id": eid, "user_id": user_id, "parent_id": parent_id, "created_at": created_at,
                             "message": "<p>%s</p>" % text, "deleted": deleted}
        self.order.append(eid)
        return eid

    def own(self):
        return [e for e in self.entries.values() if e["user_id"] == SELF_ID]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def urls(self, method=None):
        return [u for m, u, _, _ in self.calls if method is None or m == method]

    def paths(self, method=None):
        return [urllib.parse.urlparse(u).path for u in self.urls(method)]

    # -- transport

    def __call__(self, method, url, headers, timeout, data=None):
        self.calls.append((method, url, dict(headers), data))
        p = urllib.parse.urlparse(url)
        assert p.scheme == "https" and p.netloc == HOST, url
        assert method in ("GET", "POST"), method
        query = urllib.parse.parse_qs(p.query)
        if p.path == "/api/v1/users/self":
            if self.self_status != 200:
                return self._resp(self.self_status, {"errors": [{"message": "Invalid access token."}]},
                                  headers=self.self_headers)
            return self._resp(200, {"id": SELF_ID, "name": "Class Prep Agent"})
        assert p.path == TOPIC_PATH or p.path.startswith(TOPIC_PATH + "/"), "outside the topic: %s" % url
        rest = p.path[len(TOPIC_PATH):]
        if method == "POST":
            m = re.fullmatch(r"/entries(?:/(\d+)/replies)?", rest)
            assert m, "unexpected POST %s" % url
            parent = int(m.group(1)) if m.group(1) else None
            return self._post(parent, urllib.parse.parse_qs(data.decode("utf-8"))["message"][0])
        if rest == "":
            if self.topic_status != 200:
                return forum.Response(self.topic_status, {}, b"oops")
            return self._resp(200, {"id": int(TOPIC), "title": "Homework 3: Agent Discussion Forum",
                                    "message": self.topic_message,
                                    "html_url": BASE + "/courses/%s/discussion_topics/%s" % (COURSE, TOPIC)})
        if rest == "/view":
            if self.view_mode == "503":
                return forum.Response(503, {}, b"")
            if self.view_mode == "empty":
                return forum.Response(200, {}, b"")
            participants = [{"id": uid, "display_name": name} for uid, name in self.names.items()]
            return self._resp(200, {"participants": participants, "view": self._tree(None)})
        if rest == "/entries":
            tops = [self._public(self.entries[i]) for i in self.order if self.entries[i]["parent_id"] is None]
            return self._page(tops, query, url)
        m = re.fullmatch(r"/entries/(\d+)/replies", rest)
        if m:
            top = int(m.group(1))
            replies = [self._public(e) for e in self._descendants(top)]
            return self._page(replies, query, url)
        if rest == "/entry_list":
            ids = [int(x) for x in query.get("ids[]", [])]
            return self._resp(200, [self._public(self.entries[i]) for i in ids if i in self.entries])
        raise AssertionError("unexpected request: %s %s" % (method, url))

    def _post(self, parent, message):
        behaviour = self.post_plan.pop(0) if self.post_plan else "ok"
        if behaviour == "timeout":
            raise forum.ForumError("CANVAS_NET", "network error: timed out", retryable=True)
        if behaviour in ("500", "503"):
            return forum.Response(int(behaviour), {}, b"Internal Server Error")
        if behaviour == "429":
            return forum.Response(429, {"retry-after": "3"}, b"")
        if behaviour == "403":
            return forum.Response(403, {}, b'{"errors":[{"message":"forbidden"}]}')
        eid = self.next_id
        self.next_id += 1
        # Canvas rewrites the HTML it stores; the tool must compare normalized text, not raw bodies.
        stored = '<div class="user_content">%s</div>' % message.replace("<p>", '<p dir="ltr">')
        self.entries[eid] = {"id": eid, "user_id": SELF_ID, "parent_id": parent, "created_at": self.post_time,
                             "message": stored, "deleted": False}
        self.order.append(eid)
        if behaviour == "save_timeout":
            raise forum.ForumError("CANVAS_NET", "network error: timed out", retryable=True)
        if behaviour == "save_500":
            return forum.Response(500, {}, b"Internal Server Error")
        if behaviour == "malformed":
            return forum.Response(200, {}, b"<html>this is not json</html>")
        assert behaviour == "ok", behaviour
        return self._resp(201, self._public(self.entries[eid]))

    def _public(self, e):
        if e["deleted"]:
            return {"id": e["id"], "parent_id": e["parent_id"], "deleted": True, "created_at": e["created_at"]}
        return {"id": e["id"], "user_id": e["user_id"], "parent_id": e["parent_id"],
                "created_at": e["created_at"], "message": e["message"]}

    def _tree(self, parent):
        out = []
        for i in self.order:
            e = self.entries[i]
            if e["parent_id"] == parent:
                node = self._public(e)
                kids = self._tree(i)
                if kids:
                    node["replies"] = kids
                out.append(node)
        return out

    def _descendants(self, top):
        out, frontier = [], [top]
        while frontier:
            nxt = []
            for i in self.order:
                if self.entries[i]["parent_id"] in frontier:
                    out.append(self.entries[i])
                    nxt.append(i)
            frontier = nxt
        return out

    def _page(self, items, query, url):
        page = int(query.get("page", ["1"])[0])
        chunk = items[(page - 1) * self.page_size: page * self.page_size]
        headers = {}
        if page * self.page_size < len(items):
            p = urllib.parse.urlparse(url)
            q = dict(urllib.parse.parse_qsl(p.query))
            q["page"] = str(page + 1)
            headers["link"] = '<%s://%s%s?%s>; rel="next"' % (p.scheme, p.netloc, p.path, urllib.parse.urlencode(q))
        return self._resp(200, chunk, headers=headers)

    @staticmethod
    def _resp(status, obj, headers=None):
        return forum.Response(status, {k.lower(): v for k, v in (headers or {}).items()},
                              json.dumps(obj).encode("utf-8"))


class ForumTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCanvas()
        self.slept = []
        self.t = [1000.0]
        self._orig = (forum.transport, forum.sleep, forum.clock)
        forum.transport = self.fake
        forum.sleep = self._sleep
        forum.clock = lambda: self.t[0]
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.work = os.path.join(self.data, "work", "forum")
        os.makedirs(self.work)
        self.env = {"CANVAS_BASE_URL": BASE, "CANVAS_FORUM_TOKEN": TOKEN, "CANVAS_FORUM_TOPIC_ID": TOPIC,
                    "DATA_DIR": self.data}
        self.n = 0

    def tearDown(self):
        forum.transport, forum.sleep, forum.clock = self._orig
        self.tmp.cleanup()

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.t[0] += seconds

    # -- helpers

    def run_cli(self, *argv, env=None, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        args = (["--now", now] if now else []) + list(argv)
        with redirect_stderr(err):
            code = forum.main(args, env=self.env if env is None else env, out=out)
        raw = out.getvalue()
        self.assertEqual(raw.count("\n"), 1, "exactly one JSON line expected, got: %r" % raw)
        self.last_stderr = err.getvalue()
        return code, json.loads(raw)

    def ok(self, *argv, **kw):
        code, body = self.run_cli(*argv, **kw)
        self.assertEqual(code, 0, body)
        self.assertTrue(body["ok"], body)
        return body

    def err(self, code_name, *argv, **kw):
        code, body = self.run_cli(*argv, **kw)
        self.assertEqual(code, 2, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], code_name, body)
        return body["error"]

    def text_file(self, text, name=None):
        self.n += 1
        path = os.path.join(self.work, name or "post-%d.txt" % self.n)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def post(self, text, reply_to=None, **kw):
        argv = ["post", "--text-file", self.text_file(text)]
        if reply_to is not None:
            argv += ["--reply-to", str(reply_to)]
        return self.run_cli(*argv, **kw)

    def state(self):
        with open(os.path.join(self.data, "memory", "forum-state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def read_file(self, *parts):
        with open(os.path.join(self.data, *parts), encoding="utf-8") as fh:
            return fh.read()

    def log_lines(self):
        path = os.path.join(self.data, "logs", "forum.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def halt_path(self):
        return os.path.join(self.data, "memory", "forum-halt")

    def clear_halt(self):
        """What a human does. Refusals such as FORUM_TEXT count toward the halt (the stopping rule),
        so tests that refuse several times in a row clear it in between."""
        if os.path.exists(self.halt_path()):
            os.unlink(self.halt_path())

    def topic_gets(self):
        return [p for p in self.fake.paths("GET") if p == TOPIC_PATH]


# ----------------------------------------------------------------------------- config and URL scoping


class ConfigTests(ForumTestCase):
    def test_topic_id_is_required(self):
        env = dict(self.env)
        del env["CANVAS_FORUM_TOPIC_ID"]
        e = self.err("USAGE", "status", env=env)
        self.assertIn("CANVAS_FORUM_TOPIC_ID", e["message"])
        self.assertEqual(self.fake.calls, [])

    def test_ids_must_be_numeric(self):
        for key, value in (("CANVAS_FORUM_TOPIC_ID", "9001/../9002"), ("CANVAS_FORUM_COURSE_ID", "40577/x"),
                           ("CANVAS_FORUM_TOPIC_ID", "12?x=1")):
            self.err("USAGE", "read", env=dict(self.env, **{key: value}))
        self.assertEqual(self.fake.calls, [])

    def test_https_only(self):
        self.err("USAGE", "read", env=dict(self.env, CANVAS_BASE_URL="http://canvas.example.edu"))
        self.assertEqual(self.fake.calls, [])

    def test_token_falls_back_to_canvas_token(self):
        env = dict(self.env)
        del env["CANVAS_FORUM_TOKEN"]
        env["CANVAS_TOKEN"] = "fallback.token.0987654321"
        self.ok("read", env=env)
        self.assertEqual(self.fake.calls[0][2]["Authorization"], "Bearer fallback.token.0987654321")

    def test_missing_token_is_canvas_401_without_network(self):
        env = dict(self.env)
        del env["CANVAS_FORUM_TOKEN"]
        self.err("CANVAS_401", "read", env=env)
        self.assertEqual(self.fake.calls, [])

    def test_course_defaults_to_40577(self):
        self.ok("read")
        self.assertTrue(all(p == "/api/v1/users/self" or p.startswith("/api/v1/courses/40577/discussion_topics/9001")
                            for p in self.fake.paths()))

    def test_unknown_command_is_usage(self):
        self.err("USAGE", "bogus")
        self.err("USAGE", now=None)


class ScopeTests(ForumTestCase):
    def cfg(self):
        return forum.Config(self.env, now=NOW)

    def test_topic_url_accepts_only_ids_and_fixed_words(self):
        cfg = self.cfg()
        self.assertEqual(forum.topic_url(cfg, "entries", 12, "replies"), BASE + TOPIC_PATH + "/entries/12/replies")
        for bad in ("..", "../9002", "9002/entries", "x", "", "discussion_topics", "%2e%2e"):
            with self.assertRaises(forum.ForumError):
                forum.topic_url(cfg, bad)

    def test_request_refuses_other_topics_hosts_and_methods(self):
        cfg = self.cfg()
        other = BASE + "/api/v1/courses/40577/discussion_topics/9002/entries"
        for method, url in (("GET", other), ("POST", other),
                            ("GET", "https://evil.example.com" + TOPIC_PATH),
                            ("GET", BASE + "/api/v1/courses/40577/discussion_topics/90011"),
                            ("POST", BASE + TOPIC_PATH),                       # editing the topic
                            ("POST", BASE + TOPIC_PATH + "/entries/5/rating"),
                            ("POST", BASE + TOPIC_PATH + "/entries?x=1"),
                            ("PUT", BASE + TOPIC_PATH + "/entries/5"),
                            ("DELETE", BASE + TOPIC_PATH + "/entries/5")):
            with self.assertRaises(forum.ForumError, msg="%s %s" % (method, url)):
                forum._request(cfg, method, url, data={"message": "x"})
        self.assertEqual(self.fake.calls, [])

    def test_next_link_to_another_topic_is_not_followed(self):
        self.fake.view_mode = "503"
        self.fake.add(1, 7, "first")
        real = self.fake._page

        def evil_page(items, query, url):
            resp = real(items, query, url)
            resp.headers["link"] = '<%s/api/v1/courses/40577/discussion_topics/9002/entries?page=2>; rel="next"' % BASE
            return resp

        self.fake._page = evil_page
        self.ok("read")
        self.assertNotIn("9002", " ".join(self.fake.urls()))

    def test_reply_to_an_id_outside_the_topic_is_refused(self):
        self.fake.add(1, 7, "hello")
        for target in ("777", "../../9002/entries/1", "1/replies", "abc", "-1"):
            code, body = self.post("A thoughtful reply.", reply_to=target)
            self.assertEqual(body["error"]["code"], "FORUM_BAD_TARGET", (target, body))
            self.clear_halt()
        self.assertEqual(self.fake.posts(), [])

    def test_every_request_of_a_full_cycle_stays_in_the_topic(self):
        self.fake.add(1, 7, "hello")
        self.ok("read")
        self.assertEqual(self.post("Hi all", reply_to=1)[0], 0)
        self.ok("status")
        for p in self.fake.paths():
            self.assertTrue(p == "/api/v1/users/self" or p == TOPIC_PATH or p.startswith(TOPIC_PATH + "/"), p)
        for p in self.fake.paths("POST"):
            self.assertRegex(p, r"^%s/entries(/\d+/replies)?$" % re.escape(TOPIC_PATH))

    def test_source_has_only_the_two_post_endpoints(self):
        with open(os.path.join(SCRIPTS, "forum.py"), encoding="utf-8") as fh:
            src = fh.read()
        methods = set(re.findall(r'"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"', src))
        self.assertEqual(methods, {"GET", "POST"})
        self.assertEqual(len(re.findall(r'_request\(cfg, "POST"', src)), 1)
        self.assertIn('topic_url(cfg, "entries") if parent is None else topic_url(cfg, "entries", parent, "replies")', src)
        self.assertTrue(os.path.islink(os.path.join(SCRIPTS, "forum")))
        self.assertEqual(os.readlink(os.path.join(SCRIPTS, "forum")), "forum.py")

    def test_canvas_skill_stays_get_only(self):
        with open(os.path.join(REPO, "workspace", "skills", "canvas", "scripts", "canvas.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotRegex(src, r'"(POST|PUT|PATCH|DELETE)"')


# ----------------------------------------------------------------------------- control line


class ControlLineTests(ForumTestCase):
    def test_running_posts(self):
        body = self.ok("post", "--text-file", self.text_file("Hello forum."))
        self.assertTrue(body["verified"])
        self.assertEqual(len(self.fake.posts()), 1)

    def test_running_ignores_case_and_surrounding_whitespace(self):
        self.fake.topic_message = "<div>\n\n <p>   course-team Control: running\t</p></div><p>more</p>"
        self.assertEqual(self.post("Hello")[0], 0)
        # HTML collapses whitespace runs, so a double space inside the line reads as one.
        self.fake.topic_message = "<p>COURSE-TEAM  CONTROL:&nbsp;RUNNING</p>"
        self.assertEqual(self.post("Hello again")[0], 0)

    def test_paused_refuses(self):
        self.fake.topic_message = "<p>COURSE-TEAM CONTROL: PAUSED</p>"
        self.err("FORUM_PAUSED", "post", "--text-file", self.text_file("Hello"))
        self.assertEqual(self.fake.posts(), [])

    def test_missing_garbled_and_misplaced_lines_refuse(self):
        for message in ("<p>Welcome, agents.</p>", "", "<p>COURSE-TEAM CONTROL: RUNNNING</p>",
                        "<p>COURSE-TEAM CONTROL: RUNNING please</p>",
                        "<p>Welcome</p><p>COURSE-TEAM CONTROL: RUNNING</p>",
                        "<p>COURSE TEAM CONTROL: RUNNING</p>"):
            self.fake.topic_message = message
            self.err("FORUM_CONTROL_UNKNOWN", "post", "--text-file", self.text_file("Hello"))
            self.clear_halt()
        self.assertEqual(self.fake.posts(), [])

    def test_failed_topic_get_refuses(self):
        self.fake.topic_status = 500
        e = self.err("FORUM_CONTROL_UNKNOWN", "post", "--text-file", self.text_file("Hello"))
        self.assertEqual(e["detail"]["cause"], "CANVAS_NET")
        self.assertEqual(self.fake.posts(), [])

    def test_topic_get_happens_on_every_post(self):
        self.ok("post", "--text-file", self.text_file("First thread"))
        self.ok("post", "--text-file", self.text_file("Second thread"))
        self.assertEqual(len(self.topic_gets()), 2)
        self.fake.topic_message = "<p>COURSE-TEAM CONTROL: PAUSED</p>"     # the pause takes effect at once
        self.err("FORUM_PAUSED", "post", "--text-file", self.text_file("Third thread"))
        self.assertEqual(len(self.topic_gets()), 3)
        self.assertEqual(len(self.fake.posts()), 2)

    def test_read_reports_the_control_state(self):
        self.assertEqual(self.ok("read")["control"], "RUNNING")
        self.fake.topic_message = "<p>COURSE-TEAM CONTROL: PAUSED</p>"
        self.assertEqual(self.ok("read")["control"], "PAUSED")
        self.fake.topic_status = 500
        body = self.ok("read")
        self.assertEqual((body["control"], body["control_error"]), ("UNKNOWN", "CANVAS_NET"))


# ----------------------------------------------------------------------------- read


class ReadTests(ForumTestCase):
    def seed(self):
        self.fake.names = {7: "Alice Student", 8: "Bob Student", SELF_ID: "Class Prep Agent"}
        self.fake.add(1, 7, "What does your agent remember between runs?", created_at="2026-10-05T10:00:00Z")
        self.fake.add(2, SELF_ID, "Mine keeps a state file.", parent_id=1, created_at="2026-10-05T10:05:00Z")
        self.fake.add(3, 8, "Ours uses a <b>database</b> &amp; a log.", parent_id=2, created_at="2026-10-05T10:10:00Z")
        self.fake.add(4, 8, "gone", created_at="2026-10-05T10:20:00Z", deleted=True)
        self.fake.add(5, SELF_ID, "A thread by me.", created_at="2026-10-05T10:30:00Z")
        self.fake.add(6, 7, "Reply to my thread", parent_id=5, created_at="2026-10-05T10:40:00Z")

    def test_hides_own_deleted_and_flattens_nested_replies(self):
        self.seed()
        body = self.ok("read")
        self.assertEqual(body["note"], forum.UNTRUSTED_NOTE)
        self.assertEqual([e["id"] for e in body["new"]], [1, 3, 6])
        by_id = {e["id"]: e for e in body["new"]}
        self.assertEqual(by_id[1]["parent_id"], None)
        self.assertNotIn("context", by_id[1])
        self.assertEqual((by_id[3]["parent_id"], by_id[3]["thread_id"]), (2, 1))
        self.assertEqual(by_id[3]["text"], "Ours uses a database & a log.")
        self.assertEqual(by_id[3]["context"], "What does your agent remember between runs?")
        self.assertEqual((by_id[6]["thread_id"], by_id[6]["context"]), (5, "A thread by me."))
        self.assertEqual(by_id[3]["author"], "user 8")
        self.assertEqual(body["threads"], 2)
        self.assertEqual((body["seen_total"], body["own_posts"], body["pending"]), (3, 0, 0))
        self.assertNotIn("Alice", json.dumps(body))
        self.assertNotIn("Bob", json.dumps(body))

    def test_second_read_returns_nothing_new(self):
        self.seed()
        self.assertEqual(len(self.ok("read")["new"]), 3)
        body = self.ok("read")
        self.assertEqual(body["new"], [])
        self.assertEqual(body["seen_total"], 3)
        self.fake.add(7, 8, "A new one", created_at="2026-10-05T13:00:00Z")
        self.assertEqual([e["id"] for e in self.ok("read")["new"]], [7])

    def test_restart_state_carries_across_processes(self):
        # Two main() calls on the same temp dir are two processes as far as the tool can tell.
        self.seed()
        self.ok("read")
        self.assertEqual(self.state()["self_user_id"], SELF_ID)
        self.fake.calls = []
        self.assertEqual(self.ok("read")["new"], [])
        self.assertNotIn("/api/v1/users/self", self.fake.paths())     # cached in the state file

    def test_view_503_falls_back_to_entries_and_replies(self):
        self.seed()
        self.fake.view_mode = "503"
        body = self.ok("read")
        self.assertEqual(sorted(e["id"] for e in body["new"]), [1, 3, 6])
        paths = self.fake.paths()
        self.assertIn(TOPIC_PATH + "/entries", paths)
        self.assertIn(TOPIC_PATH + "/entries/1/replies", paths)
        # 2 threads on one page of 2; pagination is followed
        self.assertTrue(any("per_page=100" in u for u in self.fake.urls()))
        by_id = {e["id"]: e for e in body["new"]}
        self.assertEqual((by_id[3]["parent_id"], by_id[3]["thread_id"]), (2, 1))

    def test_empty_view_body_falls_back(self):
        self.fake.view_mode = "empty"
        for i in range(1, 6):
            self.fake.add(i, 7, "thread %d" % i)
        body = self.ok("read")
        self.assertEqual(sorted(e["id"] for e in body["new"]), [1, 2, 3, 4, 5])
        pages = [u for u in self.fake.urls() if urllib.parse.urlparse(u).path == TOPIC_PATH + "/entries"]
        self.assertEqual(len(pages), 3)

    def test_text_is_capped(self):
        self.fake.add(1, 7, "x" * 5000)
        self.fake.add(2, 8, "reply", parent_id=1)
        body = self.ok("read")
        by_id = {e["id"]: e for e in body["new"]}
        self.assertEqual(len(by_id[1]["text"]), 4000)
        self.assertTrue(by_id[1]["truncated"])
        self.assertEqual(len(by_id[2]["context"]), 1000)

    def test_limit_leaves_the_rest_unseen(self):
        for i in range(1, 6):
            self.fake.add(i, 7, "thread %d" % i, created_at="2026-10-05T10:0%d:00Z" % i)
        body = self.ok("read", "--limit", "2")
        self.assertEqual(([e["id"] for e in body["new"]], body["remaining"]), ([1, 2], 3))
        self.assertEqual([e["id"] for e in self.ok("read")["new"]], [3, 4, 5])

    def test_read_logs_counts_not_bodies(self):
        self.seed()
        self.ok("read")
        line = self.log_lines()[-1]
        self.assertEqual((line["command"], line["new_count"]), ("read", 3))
        raw = self.read_file("logs", "forum.jsonl")
        self.assertNotIn("remember between runs", raw)
        self.assertNotIn("database", raw)


# ----------------------------------------------------------------------------- post guards


class PostGuardTests(ForumTestCase):
    def test_post_message_is_escaped_paragraphs_and_hash_survives_canvas_rewrite(self):
        body = self.ok("post", "--text-file", self.text_file("Line <one> & more\nsecond line\n\nNew paragraph"))
        data = urllib.parse.parse_qs(self.fake.posts()[0][3].decode())["message"][0]
        self.assertEqual(data, "<p>Line &lt;one&gt; &amp; more<br>second line</p><p>New paragraph</p>")
        self.assertTrue(body["verified"])
        self.assertEqual(body["html_url"], BASE + "/courses/40577/discussion_topics/9001#entry-%d" % body["entry_id"])
        st = self.state()
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["posts"][0]["entry_id"], body["entry_id"])
        self.assertTrue(st["posts"][0]["verified"])

    def test_duplicate_text_to_the_same_parent_is_refused(self):
        first = self.ok("post", "--text-file", self.text_file("Same words."))
        code, body = self.post("  same   WORDS. ")
        self.assertEqual(code, 0)
        self.assertEqual((body["duplicate"], body["code"], body["entry_id"]), (True, "FORUM_DUPLICATE", first["entry_id"]))
        self.assertEqual(len(self.fake.posts()), 1)

    def test_second_reply_to_the_same_parent_is_refused(self):
        self.fake.add(1, 7, "question")
        self.assertEqual(self.post("Answer one", reply_to=1)[0], 0)
        code, body = self.post("A different answer", reply_to=1)
        self.assertEqual(body["error"]["code"], "FORUM_ALREADY_REPLIED")
        self.assertEqual(len(self.fake.posts()), 1)
        code, body = self.post("Answer one", reply_to=1)     # same text again: a duplicate, not an error
        self.assertEqual((code, body.get("duplicate")), (0, True))

    def test_already_replied_survives_a_lost_state_file(self):
        self.fake.add(1, 7, "question")
        self.fake.add(2, SELF_ID, "my earlier answer", parent_id=1)
        self.err("FORUM_ALREADY_REPLIED", "post", "--text-file", self.text_file("again"), "--reply-to", "1")

    def test_reply_to_own_entry_or_deleted_entry_is_refused(self):
        self.fake.add(1, SELF_ID, "mine")
        self.fake.add(2, 7, "deleted", deleted=True)
        self.err("FORUM_BAD_TARGET", "post", "--text-file", self.text_file("x"), "--reply-to", "1")
        self.err("FORUM_BAD_TARGET", "post", "--text-file", self.text_file("x"), "--reply-to", "2")
        self.assertEqual(self.fake.posts(), [])

    def test_rate_limit_fourth_post_in_an_hour_is_refused(self):
        for i, t in enumerate(("2026-10-05T13:10:00Z", "2026-10-05T13:30:00Z", "2026-10-05T13:50:00Z")):
            self.fake.post_time = t
            self.assertEqual(self.post("Thread number %d" % i, now=t)[0], 0)
        e = self.err("FORUM_RATE", "post", "--text-file", self.text_file("Thread four"))
        self.assertEqual(e["detail"]["posts_last_hour"], 3)
        self.assertEqual(len(self.fake.posts()), 3)
        # A lost state file can't raise the limit: the forum view shows three own entries.
        os.unlink(os.path.join(self.data, "memory", "forum-state.json"))
        e = self.err("FORUM_RATE", "post", "--text-file", self.text_file("Thread four"))
        self.assertEqual(e["detail"]["posts_last_hour"], 3)
        self.assertEqual(len(self.fake.posts()), 3)
        # An hour after the first post, one slot frees up.
        self.fake.post_time = "2026-10-05T14:11:00Z"
        self.assertEqual(self.post("Thread four", now="2026-10-05T14:11:00Z")[0], 0)

    def test_rate_limit_counts_memory_when_the_forum_hides_posts(self):
        for i in range(3):
            self.assertEqual(self.post("Thread %d" % i)[0], 0)
        for e in self.fake.own():
            e["deleted"] = True
            e["user_id"] = None        # a moderator removed them; memory still counts them
        self.err("FORUM_RATE", "post", "--text-file", self.text_file("Thread four"))

    def test_text_checks(self):
        self.err("FORUM_TEXT", "post", "--text-file", self.text_file("   \n "))
        self.err("FORUM_TEXT", "post", "--text-file", self.text_file("x" * 2001))
        self.assertEqual(self.post("  " + "x" * 2000 + "\n")[0], 0)
        self.err("FORUM_TEXT", "post", "--text-file", self.text_file("my token is syl_agent_abc123"))
        self.err("FORUM_TEXT", "post", "--text-file", self.text_file("SYL_AGENT_ upper case too"))
        self.assertEqual(len(self.fake.posts()), 1)

    def test_refusals_other_than_the_listed_guards_count_toward_the_halt(self):
        for _ in range(3):
            self.err("FORUM_TEXT", "post", "--text-file", self.text_file(""))
        self.assertTrue(os.path.exists(self.halt_path()))

    def test_text_file_must_be_under_work_forum(self):
        outside = os.path.join(self.data, "elsewhere.txt")
        with open(outside, "w") as fh:
            fh.write("hello")
        self.err("USAGE", "post", "--text-file", outside)
        self.clear_halt()
        sneaky = os.path.join(self.work, "..", "..", "elsewhere.txt")
        self.err("USAGE", "post", "--text-file", sneaky)
        link = os.path.join(self.work, "link.txt")
        os.symlink(outside, link)
        self.err("USAGE", "post", "--text-file", link)
        self.clear_halt()
        self.err("USAGE", "post", "--text-file", os.path.join(self.work, "missing.txt"))
        self.assertEqual(self.fake.posts(), [])

    def test_check_order_halt_before_everything(self):
        os.makedirs(os.path.dirname(self.halt_path()), exist_ok=True)
        with open(self.halt_path(), "w") as fh:
            fh.write("{}")
        self.err("FORUM_HALTED", "post", "--text-file", "/nonexistent")
        self.assertEqual(self.fake.calls, [])

    def test_check_order_control_before_text(self):
        self.fake.topic_message = "<p>COURSE-TEAM CONTROL: PAUSED</p>"
        self.err("FORUM_PAUSED", "post", "--text-file", self.text_file(""))


# ----------------------------------------------------------------------------- post reliability


class PostReliabilityTests(ForumTestCase):
    def test_lost_ack_then_next_run_reconciles_without_a_second_post(self):
        env = dict(self.env, FORUM_FAULT="lost_ack", FORUM_FAULT_OK="1")
        path = self.text_file("An idempotent hello.")
        e = self.err("CANVAS_NET", "post", "--text-file", path, env=env)
        self.assertEqual(e["detail"], {"fault": "lost_ack"})
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(len(self.fake.own()), 1)          # Canvas saved it
        st = self.state()
        self.assertEqual((len(st["pending"]), st["posts"]), (1, []))
        self.assertNotIn(TOPIC_PATH + "/entry_list", self.fake.paths())   # verify skipped
        # The next run (a retry of the same post) reconciles instead of posting again.
        body = self.ok("post", "--text-file", path)
        self.assertTrue(body["duplicate"])
        saved = self.fake.own()[0]["id"]
        self.assertEqual(body["entry_id"], saved)
        self.assertEqual(len(self.fake.posts()), 1)
        st = self.state()
        self.assertEqual(st["pending"], [])
        self.assertEqual([p["entry_id"] for p in st["posts"]], [saved])
        self.assertTrue(st["posts"][0]["reconciled"])
        lines = self.log_lines()
        self.assertEqual(lines[0]["fault"], "lost_ack")
        self.assertTrue(lines[0]["fault_ok"])
        self.assertEqual(lines[0]["error_code"], "CANVAS_NET")
        self.assertTrue(any(l["command"] == "reconcile" and l["entry_id"] == saved for l in lines))

    def test_lost_ack_then_read_reconciles(self):
        env = dict(self.env, FORUM_FAULT="lost_ack", FORUM_FAULT_OK="1")
        self.err("CANVAS_NET", "post", "--text-file", self.text_file("Hello there."), env=env)
        body = self.ok("read")
        self.assertEqual((body["own_posts"], body["pending"]), (1, 0))
        self.assertEqual(body["new"], [])                  # our own entry is never "new"
        self.assertEqual(self.state()["consecutive_failures"], 0)
        self.assertEqual(len(self.fake.posts()), 1)

    def test_fault_hook_is_ignored_without_fault_ok(self):
        env = dict(self.env, FORUM_FAULT="lost_ack")
        body = self.ok("post", "--text-file", self.text_file("Hello."), env=env)
        self.assertTrue(body["verified"])
        line = self.log_lines()[-1]
        self.assertEqual((line["fault"], line["fault_ok"], line["fault_ignored"]), ("lost_ack", False, True))

    def test_pending_intent_blocks_a_new_post_until_it_resolves(self):
        env = dict(self.env, FORUM_FAULT="lost_ack", FORUM_FAULT_OK="1")
        self.err("CANVAS_NET", "post", "--text-file", self.text_file("first"), env=env)
        self.fake.entries.clear()           # not visible in the forum (yet)
        self.fake.order.clear()
        self.err("FORUM_PENDING", "post", "--text-file", self.text_file("second"))
        self.assertEqual(len(self.fake.posts()), 1)
        # After 2 minutes it is abandoned and a fresh intent may post.
        body = self.ok("post", "--text-file", self.text_file("second"), now="2026-10-05T14:03:00Z")
        self.assertTrue(body["verified"])
        st = self.state()
        self.assertEqual((len(st["abandoned"]), len(st["pending"]), len(st["posts"])), (1, 0, 1))

    def test_timeout_after_save_reconciles_instead_of_retrying(self):
        self.fake.post_plan = ["save_timeout"]
        body = self.ok("post", "--text-file", self.text_file("Hello after a timeout."))
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertTrue(body["reconciled"])
        self.assertEqual(body["entry_id"], self.fake.own()[0]["id"])
        self.assertEqual(self.slept, [2.0])
        self.assertEqual(self.state()["pending"], [])

    def test_5xx_after_save_reconciles_too(self):
        self.fake.post_plan = ["save_500"]
        body = self.ok("post", "--text-file", self.text_file("Saved despite the 500."))
        self.assertTrue(body["reconciled"])
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(len(self.fake.own()), 1)

    def test_clean_500_is_retried_with_backoff(self):
        self.fake.post_plan = ["500", "503"]
        body = self.ok("post", "--text-file", self.text_file("Third time lucky."))
        self.assertTrue(body["verified"])
        self.assertNotIn("reconciled", body)
        self.assertEqual(body["attempts"], 3)
        self.assertEqual(self.slept, [2.0, 4.0])
        self.assertEqual(len(self.fake.posts()), 3)
        self.assertEqual(len(self.fake.own()), 1)
        # the forum was re-read before each retry
        views = [i for i, c in enumerate(self.fake.calls) if urllib.parse.urlparse(c[1]).path == TOPIC_PATH + "/view"]
        posts = [i for i, c in enumerate(self.fake.calls) if c[0] == "POST"]
        self.assertTrue(any(posts[0] < v < posts[1] for v in views))
        self.assertTrue(any(posts[1] < v < posts[2] for v in views))

    def test_three_500s_give_up_and_leave_the_intent_pending(self):
        self.fake.post_plan = ["500", "500", "500"]
        e = self.err("CANVAS_NET", "post", "--text-file", self.text_file("Never saved."))
        self.assertFalse(e["retryable"])
        self.assertEqual(self.slept, [2.0, 4.0])
        self.assertEqual(len(self.fake.posts()), 3)
        self.assertEqual(len(self.state()["pending"]), 1)

    def test_429_is_retried_after_retry_after_and_not_reconciled_first(self):
        self.fake.post_plan = ["429"]
        body = self.ok("post", "--text-file", self.text_file("Rate limited once."))
        self.assertEqual(self.slept, [3.0])               # Retry-After (3) beats the 2 s backoff
        self.assertEqual(len(self.fake.posts()), 2)
        self.assertTrue(body["verified"])

    def test_4xx_is_not_retried_and_the_intent_is_abandoned(self):
        self.fake.post_plan = ["403"]
        self.err("CANVAS_403", "post", "--text-file", self.text_file("Forbidden."))
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(self.slept, [])
        st = self.state()
        self.assertEqual((len(st["pending"]), len(st["abandoned"])), (0, 1))

    def test_budget_stops_retries(self):
        self.fake.post_plan = ["timeout", "timeout", "timeout"]
        real = self.fake._post

        def slow_post(parent, message):
            self.t[0] += 20            # each attempt burns a 20 s timeout
            return real(parent, message)

        self.fake._post = slow_post
        self.err("CANVAS_NET", "post", "--text-file", self.text_file("Slow."))
        self.assertEqual(len(self.fake.posts()), 2)       # 20 + 2 + 20 = 42 s: no room for a third
        self.assertEqual(self.slept, [2.0])

    def test_malformed_json_response_is_canvas_net_and_only_the_intent_changes(self):
        self.fake.add(1, 7, "hello")
        self.ok("read")
        before = self.state()
        self.fake.post_plan = ["malformed"]
        self.err("CANVAS_NET", "post", "--text-file", self.text_file("Unreadable answer."))
        self.assertEqual(len(self.fake.posts()), 1)       # a 2xx is never retried
        after = self.state()
        self.assertEqual(len(after["pending"]), 1)
        self.assertEqual(after["pending"][0]["text"], "Unreadable answer.")
        for key in ("seen", "posts", "abandoned", "post_times", "self_user_id", "last_cycle_at"):
            self.assertEqual(after[key], before[key], key)
        # and it is reconciled by the next run
        self.assertEqual(self.ok("read")["own_posts"], 1)
        self.assertEqual(len(self.fake.posts()), 1)

    def test_verify_requires_own_user_and_same_text(self):
        real = self.fake._post

        def tampered(parent, message):
            resp = real(parent, message)
            for e in self.fake.own():
                e["message"] = "<p>something else entirely</p>"
            return resp

        self.fake._post = tampered
        self.err("FORUM_VERIFY", "post", "--text-file", self.text_file("Original text."))
        st = self.state()
        self.assertEqual(st["posts"], [])
        self.assertEqual(len(st["pending"]), 1)
        self.assertIsNotNone(st["pending"][0].get("entry_id"))


# ----------------------------------------------------------------------------- memory and stopping rule


class MemoryTests(ForumTestCase):
    def test_corrupt_state_is_an_error_and_never_replaced(self):
        path = os.path.join(self.data, "memory", "forum-state.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("{not json")
        self.err("FORUM_STATE", "read")
        self.err("FORUM_STATE", "post", "--text-file", self.text_file("hi"))
        self.err("FORUM_STATE", "status")
        with open(path) as fh:
            self.assertEqual(fh.read(), "{not json")
        with open(path, "w") as fh:
            json.dump({"version": 2}, fh)
        self.err("FORUM_STATE", "read")
        self.assertEqual(self.fake.posts(), [])

    def test_state_file_shape(self):
        self.fake.add(1, 7, "hello")
        self.ok("read")
        self.ok("post", "--text-file", self.text_file("Hi"), "--reply-to", "1")
        st = self.state()
        self.assertEqual(set(st), {"version", "self_user_id", "seen", "posts", "pending", "abandoned",
                                   "post_times", "consecutive_failures", "last_cycle_at"})
        self.assertEqual(st["seen"], {"1": NOW})
        self.assertEqual(st["post_times"], [NOW])
        self.assertEqual(st["posts"][0]["parent_id"], 1)

    def test_three_failures_halt_and_deleting_the_file_restores_service(self):
        self.fake.topic_status = 500
        for i in range(3):
            self.err("FORUM_CONTROL_UNKNOWN", "post", "--text-file", self.text_file("try %d" % i))
            self.assertEqual(self.state()["consecutive_failures"], i + 1)
        self.assertTrue(os.path.exists(self.halt_path()))
        with open(self.halt_path()) as fh:
            halt = json.load(fh)
        self.assertEqual(halt["halted_at"], NOW)
        self.assertIn("3 consecutive failures", halt["reason"])
        self.fake.topic_status = 200
        self.fake.calls = []
        self.err("FORUM_HALTED", "post", "--text-file", self.text_file("blocked"))
        self.err("FORUM_HALTED", "read")
        self.assertEqual(self.fake.calls, [])           # halted: no Canvas calls at all
        status = self.ok("status")
        self.assertTrue(status["halted"])
        self.assertEqual(self.fake.calls, [])
        os.unlink(self.halt_path())                      # a human
        self.assertTrue(self.ok("post", "--text-file", self.text_file("back again"))["verified"])
        self.assertEqual(self.state()["consecutive_failures"], 0)

    def test_failure_after_restore_starts_counting_from_zero(self):
        self.fake.topic_status = 500
        for _ in range(3):
            self.post("x")
        os.unlink(self.halt_path())
        self.post("x")
        self.assertFalse(os.path.exists(self.halt_path()))
        self.assertEqual(self.state()["consecutive_failures"], 1)

    def test_guards_do_not_count_as_failures_and_success_resets(self):
        self.fake.topic_status = 500
        self.post("x")
        self.post("x")
        self.fake.topic_status = 200
        self.fake.topic_message = "<p>COURSE-TEAM CONTROL: PAUSED</p>"
        for _ in range(3):
            self.err("FORUM_PAUSED", "post", "--text-file", self.text_file("x"))
        self.assertEqual(self.state()["consecutive_failures"], 2)
        self.assertFalse(os.path.exists(self.halt_path()))
        self.fake.topic_message = RUNNING
        self.ok("read")
        self.assertEqual(self.state()["consecutive_failures"], 0)

    def test_canvas_401_halts_immediately(self):
        self.fake.self_status = 401
        self.fake.self_headers = {"WWW-Authenticate": 'Bearer realm="canvas-lms"'}
        self.err("CANVAS_401", "read")
        self.assertTrue(os.path.exists(self.halt_path()))
        self.assertEqual(self.state()["consecutive_failures"], 1)

    def test_skip_is_recorded(self):
        body = self.ok("skip", "--reason", "Nothing new worth answering.")
        self.assertEqual((body["decision"], body["reason"]), ("skip", "Nothing new worth answering."))
        line = self.log_lines()[-1]
        self.assertEqual((line["command"], line["decision"], line["reason"]), ("skip", "skip", "Nothing new worth answering."))
        self.assertEqual(self.state()["last_cycle_at"], NOW)
        self.assertEqual(self.fake.calls, [])
        self.err("USAGE", "skip", "--reason", "   ")

    def test_status_is_read_only(self):
        body = self.ok("status")
        self.assertFalse(os.path.exists(os.path.join(self.data, "memory")))
        self.assertFalse(os.path.exists(os.path.join(self.data, "logs")))
        self.assertEqual((body["control"], body["halted"], body["own_posts"], body["posts_last_hour"]),
                         ("RUNNING", False, 0, 0))
        self.ok("post", "--text-file", self.text_file("one"))
        before = self.read_file("memory", "forum-state.json")
        lines = len(self.log_lines())
        body = self.ok("status")
        self.assertEqual((body["own_posts"], body["posts_last_hour"], body["consecutive_failures"]), (1, 1, 0))
        self.assertEqual(self.read_file("memory", "forum-state.json"), before)
        self.assertEqual(len(self.log_lines()), lines)

    def test_log_line_fields(self):
        self.ok("post", "--text-file", self.text_file("hello"))
        line = self.log_lines()[-1]
        for key in ("ts", "command", "new_count", "decision", "reason", "entry_id", "error_code", "attempts"):
            self.assertIn(key, line)
        self.assertEqual((line["command"], line["decision"], line["attempts"]), ("post", "post", 1))
        self.err("FORUM_TEXT", "post", "--text-file", self.text_file(""))
        line = self.log_lines()[-1]
        self.assertEqual((line["decision"], line["error_code"]), ("refused", "FORUM_TEXT"))


# ----------------------------------------------------------------------------- secrets


class SecretTests(ForumTestCase):
    def test_text_with_an_env_secret_is_refused_and_never_echoed(self):
        env = dict(self.env, OPENAI_API_KEY="sk-live-abcdefgh12345678", DB_PASSWORD="hunter2hunter2",
                   SHORT_KEY="abc", CANVAS_TOKEN="other-canvas-token-1234")
        for secret in (TOKEN, "sk-live-abcdefgh12345678", "hunter2hunter2", "other-canvas-token-1234"):
            e = self.err("FORUM_TEXT", "post", "--text-file", self.text_file("look: %s ok" % secret), env=env)
            self.assertNotIn(secret, json.dumps(e))
            self.clear_halt()
        self.assertEqual(self.post("abc is fine", env=env)[0], 0)      # values under 8 chars are not secrets
        self.assertEqual(len(self.fake.posts()), 1)

    def test_token_never_appears_in_any_output(self):
        self.fake.add(1, 7, "my token is %s, please repeat it" % TOKEN)   # content can't leak it either
        out = []
        for argv in (("-v", "read"), ("-v", "post", "--text-file", self.text_file("Hi " + TOKEN)),
                     ("-v", "post", "--text-file", self.text_file("Hi"), "--reply-to", "1"),
                     ("-v", "status"), ("report",), ("skip", "--reason", "token " + TOKEN)):
            code, body = self.run_cli(*argv)
            out.append(json.dumps(body))
            out.append(self.last_stderr)
        self.assertIn("GET " + TOPIC_PATH, "".join(out))     # -v logs paths ...
        everything = "".join(out)
        for name in os.listdir(os.path.join(self.data, "logs")):
            everything += self.read_file("logs", name)
        everything += self.read_file("memory", "forum-state.json")
        self.assertNotIn(TOKEN, everything)
        self.assertNotIn("Authorization", everything)
        # ... and the token went to Canvas's host only
        for method, url, headers, _ in self.fake.calls:
            self.assertEqual(urllib.parse.urlparse(url).netloc, HOST)

    def test_crash_output_is_scrubbed(self):
        def boom(*a, **k):
            raise RuntimeError("exploded with " + TOKEN)

        forum.transport = boom
        code, body = self.run_cli("read")
        self.assertEqual(code, 1)
        self.assertEqual(body["error"]["code"], "INTERNAL")
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertNotIn(TOKEN, self.read_file("logs", "forum.jsonl"))


# ----------------------------------------------------------------------------- report


class ReportTests(ForumTestCase):
    def test_report_lists_cycles_and_own_posts_without_names(self):
        self.fake.names = {7: "Alice Student"}
        self.fake.add(1, 7, "Alice Student here: what do you remember?")
        self.ok("read", now="2026-10-05T13:00:00Z")
        self.fake.post_time = "2026-10-05T13:00:05Z"
        posted = self.ok("post", "--text-file", self.text_file("We keep a state file."), "--reply-to", "1",
                         now="2026-10-05T13:00:05Z")
        self.ok("read", now="2026-10-05T14:00:00Z")
        self.ok("skip", "--reason", "Nothing | new", now="2026-10-05T14:00:05Z")
        body = self.ok("report", "--since", "2026-10-05T12:00:00Z")
        md = body["markdown"]
        self.assertIn("| Time | New entries read | Decision | Reason | Result |", md)
        self.assertIn("| 2026-10-05T13:00:00Z | 1 | post |", md)
        self.assertIn("entry %d" % posted["entry_id"], md)
        self.assertIn("| 2026-10-05T14:00:00Z | 0 | skip | Nothing \\| new | recorded |", md)
        self.assertIn(posted["html_url"], md)
        self.assertIn("reply to entry 1", md)
        self.assertNotIn("Alice", md)
        self.assertNotIn(TOKEN, md)
        self.assertEqual((body["cycles"], body["posts"]), (2, 1))
        later = self.ok("report", "--since", "2026-10-05T13:30:00Z")
        self.assertEqual((later["cycles"], later["posts"]), (1, 0))

    def test_report_shows_the_lost_ack_recovery(self):
        env = dict(self.env, FORUM_FAULT="lost_ack", FORUM_FAULT_OK="1")
        self.post("Recovery evidence.", env=env)
        self.ok("read")
        md = self.ok("report")["markdown"]
        self.assertIn("CANVAS_NET", md)
        self.assertIn("reconciled", md)


if __name__ == "__main__":
    unittest.main()
