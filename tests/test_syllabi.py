"""Tests for workspace/skills/syllabi/scripts/syllabi.py (SYL-105).

No network: the module's `transport`, `sleep` and `now_utc` hooks are replaced with fakes, and the
urllib layer is exercised against a fake opener.

    python3 -m unittest discover -s tests -v
"""
import datetime as dt
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "workspace", "skills", "syllabi", "scripts")
sys.path.insert(0, SCRIPTS)

import syllabi  # noqa: E402

BASE = "https://abc123.supabase.co/functions/v1"
HOST = "abc123.supabase.co"
TOKEN = "syl_agent_" + "A" * 43            # the app's format: prefix + 43 base64url chars
ANON = "eyJ.anon.key.sekrit"
NOW = "2026-09-27T19:00:00-04:00"          # Sunday 19:00 ET: the prep job's slot

try:
    import zoneinfo
    zoneinfo.ZoneInfo("America/New_York")
    HAVE_TZDATA = True
except Exception:   # pragma: no cover - depends on the machine
    HAVE_TZDATA = False


def _key(url):
    p = urllib.parse.urlparse(url)
    return (p.netloc.lower(), p.path, tuple(sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True))))


class FakeTransport:
    """Routes (host, path, sorted query) -> a queue of responses. Records every call."""

    def __init__(self):
        self.routes = {}
        self.calls = []   # (method, url, headers, timeout)

    def add(self, url, status=200, body=None, json_body=None, headers=None, repeat=False):
        payload = json.dumps(json_body).encode() if json_body is not None else (body or b"")
        entry = (status, {k.lower(): v for k, v in (headers or {}).items()}, payload)
        self.routes.setdefault(_key(url), []).append((entry, repeat))
        return self

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        queue = self.routes.get(_key(url))
        if not queue:
            raise AssertionError("unexpected request: %s %s" % (method, url))
        (status, hdrs, payload), repeat = queue[0]
        if not repeat and len(queue) > 1:
            queue.pop(0)
        elif not repeat:
            queue[0] = ((status, hdrs, payload), True)   # last response sticks
        return syllabi.Response(status, hdrs, payload)

    def urls(self):
        return [u for _, u, _, _ in self.calls]


def upcoming_url(days=3, path="/agent-upcoming"):
    return BASE + path + "?" + urllib.parse.urlencode({"days": days})


def payload(**overrides):
    """The shape agent-upcoming returns (SYL-92): two courses, three sessions, four events."""
    data = {
        "timezone": "America/New_York",
        "courses": [
            {"id": "c-mas", "name": "AI Studio", "code": "MAS.665", "canvas_course_id": 40577,
             "schedule": {"meeting_days": ["Tue"], "meeting_times": {"start": "13:00", "end": "16:00"}},
             "grading_rules": {"components": [{"name": "HW", "weight": 40}]},
             "policies": {"late": "one day grace"}},
            {"id": "c-pwd", "name": "Persuading with Data", "code": "15.286", "canvas_course_id": "51234",
             "schedule": {"meeting_days": ["Mon", "Wed"], "meeting_times": {"start": "10:00", "end": "11:30"}},
             "grading_rules": None, "policies": None},
        ],
        "sessions": [
            {"course_id": "c-pwd", "code": "15.286", "date": "2026-09-28", "start": "10:00", "end": "11:30"},
            {"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29", "start": "13:00", "end": "16:00"},
            {"course_id": "c-pwd", "code": "15.286", "date": "2026-09-30", "start": "10:00", "end": "11:30"},
        ],
        "events": [
            {"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-28", "time": "23:59:00",
             "title": "Pre-class questions 4", "type": "deadline", "category": "HW", "confidence": "high",
             "source": "canvas_matched", "canvas_url": "https://canvas.mit.edu/courses/40577/assignments/9"},
            {"course_id": "c-pwd", "code": "15.286", "date": "2026-09-30", "time": None,
             "title": "Reading response 3", "type": "deadline", "category": None, "confidence": None,
             "source": "syllabus", "canvas_url": None},
            {"course_id": "c-pwd", "code": "15.286", "date": "2026-09-29", "time": None,
             "title": "No class (holiday make-up)", "type": "no_class", "category": None, "confidence": None,
             "source": "syllabus", "canvas_url": None},
            {"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-27", "time": "12:00:00",
             "title": "Already past", "type": "deadline", "category": None, "confidence": None,
             "source": "syllabus", "canvas_url": None},
        ],
    }
    data.update(overrides)
    return data


class SyllabiTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransport()
        self.slept = []
        self._orig = (syllabi.transport, syllabi.sleep, syllabi.now_utc)
        syllabi.transport = self.fake
        syllabi.sleep = self.slept.append
        syllabi.now_utc = lambda: dt.datetime.fromisoformat(NOW).astimezone(dt.timezone.utc)
        self.env = {"SYLLABI_BASE_URL": BASE, "SYLLABI_AGENT_TOKEN": TOKEN}

    def tearDown(self):
        syllabi.transport, syllabi.sleep, syllabi.now_utc = self._orig

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = syllabi.main(list(argv), env=self.env if env is None else env, out=out)
        raw = out.getvalue()
        self.assertEqual(raw.count("\n"), 1, "exactly one JSON line expected, got: %r" % raw)
        return code, json.loads(raw), err.getvalue()

    def ok(self, *argv, **kw):
        rc, body, err = self.run_cli(*argv, **kw)
        self.assertEqual(rc, 0, body)
        self.assertTrue(body["ok"])
        return body

    def assertError(self, result, code, retryable=None):
        rc, body, _ = result
        self.assertEqual(rc, 2, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], code, body)
        if retryable is not None:
            self.assertEqual(body["error"]["retryable"], retryable, body)
        return body["error"]

    def sessions_by_key(self, body):
        return {s["key"]: s for s in body["sessions"]}


# ----------------------------------------------------------------------------- config


class ConfigTests(SyllabiTestCase):
    def test_missing_base_url_is_usage_error(self):
        err = self.assertError(self.run_cli("check", env={"SYLLABI_AGENT_TOKEN": TOKEN}), "USAGE")
        self.assertIn("SYLLABI_BASE_URL", err["message"])
        self.assertEqual(self.fake.calls, [])

    def test_http_base_url_is_refused(self):
        env = dict(self.env, SYLLABI_BASE_URL="http://abc123.supabase.co/functions/v1")
        self.assertError(self.run_cli("check", env=env), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_missing_token_is_401_without_network(self):
        err = self.assertError(self.run_cli("check", env={"SYLLABI_BASE_URL": BASE}), "SYLLABI_401", retryable=False)
        self.assertIn("Agent access", err["message"])
        self.assertEqual(self.fake.calls, [])

    def test_token_with_whitespace_is_401_without_network(self):
        env = dict(self.env, SYLLABI_AGENT_TOKEN="syl_agent_abc def")
        self.assertError(self.run_cli("check", env=env), "SYLLABI_401")
        self.assertEqual(self.fake.calls, [])

    def test_bad_timeout_is_usage_error(self):
        self.assertError(self.run_cli("check", env=dict(self.env, SYLLABI_TIMEOUT="soon")), "USAGE")
        self.assertError(self.run_cli("check", env=dict(self.env, SYLLABI_TIMEOUT="0")), "USAGE")
        self.assertError(self.run_cli("check", env=dict(self.env, SYLLABI_TIMEOUT="90")), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_custom_timeout_is_passed_to_the_transport(self):
        self.fake.add(upcoming_url(1), json_body=payload())
        self.ok("check", env=dict(self.env, SYLLABI_TIMEOUT="7.5"))
        self.assertEqual(self.fake.calls[0][3], 7.5)

    def test_bad_upcoming_path_is_usage_error(self):
        for bad in ("agent-upcoming?x=1", "/a//b", "/x#y"):
            self.assertError(self.run_cli("check", env=dict(self.env, SYLLABI_UPCOMING_PATH=bad)), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_missing_command_is_usage_error(self):
        self.assertError(self.run_cli(), "USAGE")

    def test_unknown_command_is_usage_error(self):
        self.assertError(self.run_cli("frobnicate"), "USAGE")


# ----------------------------------------------------------------------------- requests


class RequestTests(SyllabiTestCase):
    def test_get_with_bearer_token_on_our_host_only(self):
        self.fake.add(upcoming_url(3), json_body=payload())
        self.ok("upcoming")
        method, url, headers, timeout = self.fake.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, upcoming_url(3))
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["Accept"], "application/json")
        self.assertIn("class-prep-agent", headers["User-Agent"])
        self.assertNotIn("apikey", headers)
        self.assertEqual(timeout, syllabi.DEFAULT_TIMEOUT)

    def test_anon_key_goes_in_the_apikey_header(self):
        self.fake.add(upcoming_url(3), json_body=payload())
        self.ok("upcoming", env=dict(self.env, SYLLABI_ANON_KEY=ANON))
        self.assertEqual(self.fake.calls[0][2]["apikey"], ANON)

    def test_trailing_slash_and_custom_path(self):
        self.fake.add(upcoming_url(3, "/agent/upcoming"), json_body=payload())
        env = dict(self.env, SYLLABI_BASE_URL=BASE + "/", SYLLABI_UPCOMING_PATH="agent/upcoming")
        self.ok("upcoming", env=env)
        self.assertEqual(self.fake.urls(), [upcoming_url(3, "/agent/upcoming")])

    def test_days_is_sent_and_bounded(self):
        self.fake.add(upcoming_url(14), json_body=payload())
        body = self.ok("upcoming", "--days", "14")
        self.assertEqual(body["days"], 14)
        self.assertError(self.run_cli("upcoming", "--days", "15"), "USAGE")
        self.assertError(self.run_cli("upcoming", "--days", "-1"), "USAGE")
        self.assertError(self.run_cli("upcoming", "--days", "two"), "USAGE")
        self.assertEqual(len(self.fake.calls), 1)

    def test_negative_within_hours_and_bad_now_fail_before_any_request(self):
        self.assertError(self.run_cli("upcoming", "--within-hours", "-2"), "USAGE")
        self.assertError(self.run_cli("upcoming", "--now", "yesterday"), "USAGE")
        self.assertError(self.run_cli("check", "--now", "2026-13-40"), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_verbose_logs_method_and_path_only(self):
        self.fake.add(upcoming_url(3), json_body=payload())
        rc, body, err = self.run_cli("-v", "upcoming")
        self.assertEqual(rc, 0)
        self.assertIn("syllabi: GET /functions/v1/agent-upcoming\n", err)
        self.assertNotIn("days=", err)
        self.assertNotIn(TOKEN, err)
        self.assertNotIn("Bearer", err)

    def test_verbose_via_environment(self):
        self.fake.add(upcoming_url(3), json_body=payload())
        rc, body, err = self.run_cli("upcoming", env=dict(self.env, SYLLABI_VERBOSE="1"))
        self.assertIn("GET /functions/v1/agent-upcoming", err)


# ----------------------------------------------------------------------------- error mapping


class ErrorMappingTests(SyllabiTestCase):
    def test_401_is_syllabi_401(self):
        self.fake.add(upcoming_url(3), status=401, json_body={"error": "Unauthorized"})
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_401", retryable=False)
        self.assertEqual(err["status"], 401)
        self.assertIn("Settings → Agent access", err["message"])

    def test_403_is_also_syllabi_401(self):
        self.fake.add(upcoming_url(3), status=403, body=b"forbidden")
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_401", retryable=False)
        self.assertEqual(err["status"], 403)

    def test_404_is_not_found_with_a_config_hint(self):
        self.fake.add(upcoming_url(3), status=404, body=b"Requested function was not found")
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_NOT_FOUND", retryable=False)
        self.assertIn("SYLLABI_BASE_URL", err["message"])
        self.assertIn("/agent-upcoming", err["message"])

    def test_400_is_bad_request_with_the_servers_text(self):
        self.fake.add(upcoming_url(3), status=400, json_body={"error": "days must be a number"})
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_BAD_REQUEST", retryable=False)
        self.assertIn("days must be a number", err["message"])

    def test_405_is_bad_request(self):
        self.fake.add(upcoming_url(3), status=405, json_body={"error": "Method not allowed"})
        self.assertError(self.run_cli("upcoming"), "SYLLABI_BAD_REQUEST")

    def test_5xx_is_unavailable_and_retryable(self):
        self.fake.add(upcoming_url(3), status=500, json_body={"error": "Internal server error"})
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_UNAVAILABLE", retryable=True)
        self.assertEqual(err["status"], 500)
        self.fake.routes.clear()
        self.fake.add(upcoming_url(3), status=503, body=b"")
        self.assertError(self.run_cli("upcoming"), "SYLLABI_UNAVAILABLE", retryable=True)

    def test_other_status_is_unavailable_not_retryable(self):
        self.fake.add(upcoming_url(3), status=418, body=b"")
        self.assertError(self.run_cli("upcoming"), "SYLLABI_UNAVAILABLE", retryable=False)

    def test_redirect_is_refused_and_not_followed(self):
        self.fake.add(upcoming_url(3), status=302, headers={"Location": "https://evil.example/agent-upcoming"})
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_UNAVAILABLE", retryable=False)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(err["detail"]["location_host"], "evil.example")
        self.assertIn("not following", err["message"])
        self.assertIn(HOST, err["message"])

    def test_429_waits_retry_after_then_succeeds(self):
        self.fake.add(upcoming_url(3), status=429, headers={"Retry-After": "2"})
        self.fake.add(upcoming_url(3), json_body=payload())
        body = self.ok("upcoming")
        self.assertEqual(len(body["sessions"]), 3)
        self.assertEqual(self.slept, [2.0])
        self.assertEqual(len(self.fake.calls), 2)

    def test_429_twice_is_rate_limit_and_wait_is_capped(self):
        self.fake.add(upcoming_url(3), status=429, headers={"Retry-After": "600"}, repeat=True)
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_RATE_LIMIT", retryable=True)
        self.assertEqual(err["status"], 429)
        self.assertEqual(self.slept, [float(syllabi.RATE_RETRY_MAX_SLEEP)])
        self.assertEqual(len(self.fake.calls), 2)

    def test_429_with_garbage_retry_after_waits_one_second(self):
        self.fake.add(upcoming_url(3), status=429, headers={"Retry-After": "soon"})
        self.fake.add(upcoming_url(3), json_body=payload())
        self.ok("upcoming")
        self.assertEqual(self.slept, [1.0])

    def test_non_json_body_is_unavailable(self):
        self.fake.add(upcoming_url(3), body=b"<html>maintenance</html>")
        err = self.assertError(self.run_cli("upcoming"), "SYLLABI_UNAVAILABLE", retryable=True)
        self.assertIn("non-JSON", err["message"])

    def test_bad_shapes_are_bad_response(self):
        cases = [
            [1, 2, 3],
            {"courses": [], "sessions": [], "events": []},                    # no timezone
            {"timezone": "", "courses": [], "sessions": [], "events": []},
            {"timezone": "UTC", "courses": {}, "sessions": [], "events": []},
            {"timezone": "UTC", "courses": [], "sessions": [{"code": "X", "date": "2026-09-29"}], "events": []},
            {"timezone": "UTC", "courses": [], "sessions": [{"course_id": "c", "date": "09/29/2026"}], "events": []},
            {"timezone": "UTC", "courses": [], "sessions": [{"course_id": "c", "date": "2026-02-30"}], "events": []},
            {"timezone": "UTC", "courses": [], "sessions": [], "events": [{"course_id": "c", "date": None}]},
            {"timezone": "UTC", "courses": [{"code": "X"}], "sessions": [], "events": []},
            {"timezone": "UTC", "courses": ["MAS.665"], "sessions": [], "events": []},
            {"error": "Unauthorized"},
        ]
        for bad in cases:
            self.fake.routes.clear()
            self.fake.add(upcoming_url(3), json_body=bad)
            err = self.assertError(self.run_cli("upcoming"), "SYLLABI_BAD_RESPONSE", retryable=False)
            self.assertIn("unexpected response shape", err["message"])

    def test_empty_semester_is_fine(self):
        self.fake.add(upcoming_url(3), json_body={"timezone": "America/New_York", "courses": [], "sessions": [], "events": []})
        body = self.ok("upcoming")
        self.assertEqual(body["sessions"], [])
        self.assertEqual(body["courses"], [])


# ----------------------------------------------------------------------------- the urllib layer


class _FakeHTTPResponse:
    def __init__(self, status, headers, chunks):
        self.status = status
        self.headers = headers
        self._chunks = list(chunks)

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Headers(dict):
    def items(self):
        return list(dict.items(self))


class UrllibTransportTests(unittest.TestCase):
    def setUp(self):
        self._orig = syllabi._opener

    def tearDown(self):
        syllabi._opener = self._orig

    def test_success_returns_status_lowercased_headers_and_body(self):
        syllabi._opener = _FakeOpener(_FakeHTTPResponse(200, _Headers({"Content-Type": "application/json"}), [b'{"a":', b"1}"]))
        resp = syllabi._urllib_transport("GET", BASE + "/agent-upcoming?days=1", {"X": "y"}, 5)
        self.assertEqual(resp, syllabi.Response(200, {"content-type": "application/json"}, b'{"a":1}'))
        req, timeout = syllabi._opener.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(timeout, 5)

    def test_http_error_becomes_a_response(self):
        err = urllib.error.HTTPError(BASE, 401, "Unauthorized", _Headers({"Content-Type": "application/json"}),
                                     io.BytesIO(b'{"error":"Unauthorized"}'))
        syllabi._opener = _FakeOpener(err)
        resp = syllabi._urllib_transport("GET", BASE + "/agent-upcoming", {}, 5)
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.body, b'{"error":"Unauthorized"}')

    def test_network_failures_are_retryable_unavailable(self):
        for exc in (urllib.error.URLError("timed out"), OSError("connection reset"), __import__("socket").timeout()):
            syllabi._opener = _FakeOpener(exc)
            with self.assertRaises(syllabi.SyllabiError) as ctx:
                syllabi._urllib_transport("GET", BASE + "/agent-upcoming", {}, 5)
            self.assertEqual(ctx.exception.code, "SYLLABI_UNAVAILABLE")
            self.assertTrue(ctx.exception.retryable)

    def test_oversized_body_is_bad_response(self):
        big = b"x" * (1024 * 1024)
        syllabi._opener = _FakeOpener(_FakeHTTPResponse(200, _Headers({}), [big] * 5))
        with self.assertRaises(syllabi.SyllabiError) as ctx:
            syllabi._urllib_transport("GET", BASE + "/agent-upcoming", {}, 5)
        self.assertEqual(ctx.exception.code, "SYLLABI_BAD_RESPONSE")

    def test_redirect_handler_does_not_follow(self):
        handler = syllabi._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere/"))


# ----------------------------------------------------------------------------- planning records


class UpcomingTests(SyllabiTestCase):
    def setUp(self):
        super().setUp()
        self.fake.add(upcoming_url(3), json_body=payload())

    def test_sessions_become_planning_records(self):
        body = self.ok("upcoming")
        self.assertEqual(body["timezone"], "America/New_York")
        self.assertEqual(body["now"], NOW)
        self.assertNotIn("timezone_fallback", body)
        keys = [s["key"] for s in body["sessions"]]
        self.assertEqual(keys, ["15.286@2026-09-28", "MAS.665@2026-09-29", "15.286@2026-09-30"])
        mas = self.sessions_by_key(body)["MAS.665@2026-09-29"]
        self.assertEqual(mas["course"], "MAS.665")
        self.assertEqual(mas["course_id"], "c-mas")
        self.assertEqual(mas["course_name"], "AI Studio")
        self.assertEqual(mas["canvas_course_id"], 40577)
        self.assertEqual(mas["class_date"], "2026-09-29")
        self.assertEqual(mas["class_start"], "2026-09-29T13:00:00-04:00")
        self.assertEqual(mas["class_end"], "2026-09-29T16:00:00-04:00")
        self.assertTrue(mas["start_time_known"])
        self.assertEqual(mas["hours_until_class"], 42.0)
        self.assertIsNone(mas["topic"])
        self.assertEqual(mas["readings"], [])

    def test_due_before_class_drives_notify_at(self):
        body = self.ok("upcoming")
        by = self.sessions_by_key(body)
        mas = by["MAS.665@2026-09-29"]
        self.assertTrue(mas["has_due_before_class"])
        self.assertEqual([d["title"] for d in mas["due_before_class"]], ["Pre-class questions 4"])
        self.assertEqual(mas["due_before_class"][0]["due_at"], "2026-09-28T23:59:00-04:00")
        self.assertTrue(mas["due_before_class"][0]["time_known"])
        self.assertEqual(mas["due_before_class"][0]["canvas_url"], "https://canvas.mit.edu/courses/40577/assignments/9")
        self.assertEqual(mas["notify_at"], "2026-09-28T13:00:00-04:00")        # class_start - 24h

    def test_nothing_due_means_0630_on_class_day(self):
        body = self.ok("upcoming")
        mon = self.sessions_by_key(body)["15.286@2026-09-28"]
        self.assertFalse(mon["has_due_before_class"])
        self.assertEqual(mon["due_before_class"], [])
        self.assertEqual(mon["notify_at"], "2026-09-28T06:30:00-04:00")
        self.assertEqual(mon["hours_until_class"], 15.0)

    def test_untimed_event_on_class_day_counts_as_due_at_class_start(self):
        body = self.ok("upcoming")
        wed = self.sessions_by_key(body)["15.286@2026-09-30"]
        self.assertTrue(wed["has_due_before_class"])
        due = wed["due_before_class"][0]
        self.assertEqual(due["title"], "Reading response 3")
        self.assertFalse(due["time_known"])
        self.assertEqual(due["due_at"], "2026-09-30T10:00:00-04:00")
        self.assertEqual(wed["notify_at"], "2026-09-29T10:00:00-04:00")

    def test_past_events_and_no_class_events_are_not_deliverables(self):
        body = self.ok("upcoming")
        titles = [d["title"] for s in body["sessions"] for d in s["due_before_class"]]
        self.assertNotIn("Already past", titles)
        self.assertNotIn("No class (holiday make-up)", titles)

    def test_within_hours_filters_sessions(self):
        body = self.ok("upcoming", "--within-hours", "48")
        self.assertEqual([s["key"] for s in body["sessions"]], ["15.286@2026-09-28", "MAS.665@2026-09-29"])
        self.assertEqual(body["within_hours"], 48.0)

    def test_sessions_that_already_started_are_dropped(self):
        body = self.ok("upcoming", "--now", "2026-09-28T10:30:00-04:00")
        self.assertEqual([s["key"] for s in body["sessions"]], ["MAS.665@2026-09-29", "15.286@2026-09-30"])

    def test_notify_at_in_the_past_becomes_now(self):
        body = self.ok("upcoming", "--now", "2026-09-28T08:00:00-04:00")
        mon = self.sessions_by_key(body)["15.286@2026-09-28"]
        self.assertEqual(mon["notify_at"], "2026-09-28T08:00:00-04:00")
        self.assertEqual(body["now"], "2026-09-28T08:00:00-04:00")

    def test_now_accepts_utc_and_naive_forms(self):
        body = self.ok("upcoming", "--now", "2026-09-28T12:00:00Z")
        self.assertEqual(body["now"], "2026-09-28T08:00:00-04:00")
        self.fake.routes.clear()
        self.fake.add(upcoming_url(3), json_body=payload())
        body = self.ok("upcoming", "--now", "2026-09-28T08:00")            # naive = user's timezone
        self.assertEqual(body["now"], "2026-09-28T08:00:00-04:00")

    def test_events_and_courses_are_echoed_compactly(self):
        body = self.ok("upcoming")
        self.assertEqual(len(body["events"]), 4)
        untimed = [e for e in body["events"] if e["title"] == "Reading response 3"][0]
        self.assertEqual(untimed["due_at"], "2026-09-30T23:59:00-04:00")
        self.assertEqual(body["courses"], [
            {"id": "c-mas", "code": "MAS.665", "name": "AI Studio", "canvas_course_id": 40577},
            {"id": "c-pwd", "code": "15.286", "name": "Persuading with Data", "canvas_course_id": 51234},
        ])
        self.assertNotIn("schedule", body["courses"][0])


class NormalisationTests(SyllabiTestCase):
    def test_missing_code_falls_back_to_course_code_then_id(self):
        data = payload()
        data["sessions"][1]["code"] = None
        data["sessions"].append({"course_id": "c-unknown", "code": None, "date": "2026-09-29", "start": "09:00", "end": None})
        self.fake.add(upcoming_url(3), json_body=data)
        keys = [s["key"] for s in self.ok("upcoming")["sessions"]]
        self.assertIn("MAS.665@2026-09-29", keys)
        self.assertIn("c-unknown@2026-09-29", keys)

    def test_untimed_session_is_sometime_that_day(self):
        data = payload(sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29", "start": None, "end": None}])
        data["events"].append({"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29", "time": "20:00:00",
                               "title": "Evening memo", "type": "deadline", "source": "syllabus"})
        self.fake.add(upcoming_url(3), json_body=data)
        s = self.ok("upcoming")["sessions"][0]
        self.assertIsNone(s["class_start"])                 # not midnight
        self.assertIsNone(s["class_end"])
        self.assertIsNone(s["hours_until_class"])
        self.assertFalse(s["start_time_known"])
        # The due window runs to the end of the class day, and the 06:30 notification stays.
        self.assertEqual([d["title"] for d in s["due_before_class"]], ["Pre-class questions 4", "Evening memo"])
        self.assertTrue(s["has_due_before_class"])
        self.assertEqual(s["notify_at"], "2026-09-29T06:30:00-04:00")

    def test_untimed_session_is_kept_until_the_day_is_over(self):
        data = payload(sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29", "start": None, "end": None}])
        self.fake.add(upcoming_url(3), json_body=data, repeat=True)
        s = self.ok("upcoming", "--now", "2026-09-29T15:00:00-04:00")["sessions"]
        self.assertEqual([x["key"] for x in s], ["MAS.665@2026-09-29"])
        self.assertEqual(s[0]["notify_at"], "2026-09-29T15:00:00-04:00")   # 06:30 has passed: now
        self.assertEqual(self.ok("upcoming", "--now", "2026-09-29T23:59:00-04:00")["sessions"][0]["key"],
                         "MAS.665@2026-09-29")
        self.assertEqual(self.ok("upcoming", "--now", "2026-09-30T00:00:00-04:00")["sessions"], [])

    def test_untimed_session_within_hours_counts_from_the_start_of_the_day(self):
        data = payload(sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29", "start": None, "end": None}])
        self.fake.add(upcoming_url(3), json_body=data, repeat=True)
        self.assertEqual(len(self.ok("upcoming", "--within-hours", "29")["sessions"]), 1)    # 00:00 is 29 h away
        self.assertEqual(self.ok("upcoming", "--within-hours", "28")["sessions"], [])

    def test_end_before_start_rolls_to_next_day(self):
        data = payload(sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29", "start": "23:00", "end": "00:30"}])
        self.fake.add(upcoming_url(3), json_body=data)
        s = self.ok("upcoming")["sessions"][0]
        self.assertEqual(s["class_end"], "2026-09-30T00:30:00-04:00")

    def test_bad_times_are_treated_as_unknown(self):
        data = payload(sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29", "start": "1pm", "end": 42}])
        self.fake.add(upcoming_url(3), json_body=data)
        s = self.ok("upcoming")["sessions"][0]
        self.assertFalse(s["start_time_known"])
        self.assertIsNone(s["class_end"])

    def test_strings_are_capped_and_control_characters_dropped(self):
        data = payload()
        data["events"][0]["title"] = "Q" * 5000 + "\x00"
        data["courses"][0]["name"] = "N\r\n" * 10
        self.fake.add(upcoming_url(3), json_body=data)
        body = self.ok("upcoming")
        title = body["events"][0]["title"]
        self.assertEqual(len(title), 501)
        self.assertTrue(title.endswith("…"))
        self.assertNotIn("\x00", title)
        self.assertNotIn("\r", body["courses"][0]["name"])

    def test_lists_are_capped(self):
        many = [{"course_id": "c-mas", "code": "MAS.665", "date": "2026-10-%02d" % (1 + i % 28), "start": "13:00", "end": None}
                for i in range(600)]
        self.fake.add(upcoming_url(3), json_body=payload(sessions=many))
        body = self.ok("upcoming", "--now", "2026-09-01T00:00:00-04:00")
        self.assertEqual(len(body["sessions"]), syllabi.MAX_LIST)

    def test_canvas_course_id_forms(self):
        self.assertEqual(syllabi._canvas_id(40577), 40577)
        self.assertEqual(syllabi._canvas_id(" 40577 "), 40577)
        self.assertIsNone(syllabi._canvas_id("abc"))
        self.assertIsNone(syllabi._canvas_id(True))
        self.assertIsNone(syllabi._canvas_id(None))

    def test_readings_and_urls_are_validated(self):
        data = payload()
        data["sessions"][1]["topic"] = "Agents"
        data["sessions"][1]["readings"] = ["https://example.edu/a.pdf", {"title": "B", "url": "javascript:alert(1)"},
                                           {"title": "C", "url": "ftp://x/y"}, 42]
        data["events"][0]["canvas_url"] = "javascript:alert(1)"
        self.fake.add(upcoming_url(3), json_body=data)
        body = self.ok("upcoming")
        mas = self.sessions_by_key(body)["MAS.665@2026-09-29"]
        self.assertEqual(mas["topic"], "Agents")
        self.assertEqual(mas["readings"], [
            {"title": "https://example.edu/a.pdf", "url": "https://example.edu/a.pdf"},
            {"title": "B", "url": None},
            {"title": "C", "url": None},
        ])
        self.assertIsNone(mas["due_before_class"][0]["canvas_url"])

    def test_unknown_event_type_defaults_to_other_and_counts_as_due(self):
        data = payload()
        data["events"][0]["type"] = None
        self.fake.add(upcoming_url(3), json_body=data)
        mas = self.sessions_by_key(self.ok("upcoming"))["MAS.665@2026-09-29"]
        self.assertEqual(mas["due_before_class"][0]["type"], "other")


class PostgresTimeTests(SyllabiTestCase):
    def test_time_accepts_seconds_and_normalizes_to_hh_mm(self):
        self.assertEqual(syllabi._time("23:59:00"), "23:59")
        self.assertEqual(syllabi._time("9:05:30.123456"), "09:05")
        self.assertEqual(syllabi._time(" 10:00 "), "10:00")
        for bad in ("24:00:00", "12:60:00", "12:00:61", "12", "1pm", 1200, None):
            self.assertIsNone(syllabi._time(bad), bad)

    def test_event_due_after_class_on_class_day_is_not_due_before_class(self):
        data = payload()
        data["events"].append({"course_id": "c-pwd", "code": "15.286", "date": "2026-09-28", "time": "23:59:00",
                               "title": "Due tonight", "type": "deadline", "source": "canvas_matched"})
        self.fake.add(upcoming_url(3), json_body=data)
        body = self.ok("upcoming")
        mon = self.sessions_by_key(body)["15.286@2026-09-28"]              # class at 10:00
        self.assertEqual(mon["due_before_class"], [])
        self.assertFalse(mon["has_due_before_class"])
        self.assertEqual(mon["notify_at"], "2026-09-28T06:30:00-04:00")
        tonight = [e for e in body["events"] if e["title"] == "Due tonight"][0]
        self.assertEqual(tonight["time"], "23:59")
        self.assertEqual(tonight["due_at"], "2026-09-28T23:59:00-04:00")

    def test_session_times_with_seconds(self):
        data = payload(sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-09-29",
                                  "start": "13:00:00", "end": "16:00:00"}])
        self.fake.add(upcoming_url(3), json_body=data)
        s = self.ok("upcoming")["sessions"][0]
        self.assertTrue(s["start_time_known"])
        self.assertEqual(s["class_start"], "2026-09-29T13:00:00-04:00")
        self.assertEqual(s["class_end"], "2026-09-29T16:00:00-04:00")


KEY_SCHEMA = __import__("re").compile(r"^[^@\s]+@\d{4}-\d{2}-\d{2}$")    # prep-log.schema.json


class SessionKeyTests(SyllabiTestCase):
    def test_course_codes_with_spaces_make_schema_valid_keys(self):
        data = payload()
        data["courses"][0]["code"] = "CS 101"
        data["sessions"][1]["code"] = None                                  # falls back to the course code
        data["sessions"][0]["code"] = "  15 .286\tA "
        data["sessions"].append({"course_id": "c-x", "code": "a@b c", "date": "2026-09-29", "start": "09:00", "end": None})
        self.fake.add(upcoming_url(3), json_body=data)
        body = self.ok("upcoming")
        keys = [s["key"] for s in body["sessions"]]
        self.assertIn("CS-101@2026-09-29", keys)
        self.assertIn("15-.286-A@2026-09-28", keys)
        self.assertIn("a-b-c@2026-09-29", keys)
        for s in body["sessions"]:
            self.assertRegex(s["key"], KEY_SCHEMA)
            self.assertEqual(s["key"], "%s@%s" % (s["course"], s["class_date"]))
        cs = self.sessions_by_key(body)["CS-101@2026-09-29"]
        self.assertEqual(cs["course_code"], "CS 101")
        self.assertEqual(cs["canvas_course_id"], 40577)

    def test_course_slug(self):
        self.assertEqual(syllabi.course_slug("MAS.665"), "MAS.665")
        self.assertEqual(syllabi.course_slug("CS 101"), "CS-101")
        self.assertEqual(syllabi.course_slug(" @ "), "course")


class DSTTests(SyllabiTestCase):
    """DST ends 2026-11-01 02:00 EDT. A class at 10:00 EST that day is 25 wall-clock hours after
    10:00 EDT on Oct 31 but 24 elapsed hours after 11:00 EDT."""

    def _payload(self):
        return payload(
            sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-11-01", "start": "10:00", "end": "11:00"}],
            events=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-11-01", "time": "08:00:00",
                     "title": "Pre-class memo", "type": "deadline", "source": "syllabus"}])

    def _check(self):
        self.fake.add(upcoming_url(3), json_body=self._payload())
        s = self.ok("upcoming", "--now", "2026-10-31T09:00:00-04:00")["sessions"][0]
        self.assertEqual(s["class_start"], "2026-11-01T10:00:00-05:00")
        self.assertTrue(s["has_due_before_class"])
        self.assertEqual(s["notify_at"], "2026-10-31T11:00:00-04:00")        # start - 24 elapsed hours
        self.assertEqual(s["hours_until_class"], 26.0)                       # 13:00Z -> 15:00Z next day
        return s

    def test_notify_at_and_hours_across_nov_1_with_zoneinfo(self):
        if not HAVE_TZDATA:
            self.skipTest("no tzdata on this machine")
        self._check()

    def test_notify_at_and_hours_across_nov_1_with_builtin_rules(self):
        saved = sys.modules.get("zoneinfo")
        sys.modules["zoneinfo"] = None
        try:
            self._check()
        finally:
            if saved is None:
                del sys.modules["zoneinfo"]
            else:
                sys.modules["zoneinfo"] = saved

    def test_spring_forward(self):
        # DST starts 2026-03-08 02:00 EST. Class 09:00 EDT Mar 8 (13:00Z) - 24 h = 08:00 EST Mar 7,
        # not the wall-clock 09:00.
        data = payload(sessions=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-03-08", "start": "09:00", "end": None}],
                       events=[{"course_id": "c-mas", "code": "MAS.665", "date": "2026-03-08", "time": None,
                                "title": "Due at class", "type": "deadline"}])
        self.fake.add(upcoming_url(3), json_body=data)
        s = self.ok("upcoming", "--now", "2026-03-07T07:00:00-05:00")["sessions"][0]
        self.assertEqual(s["class_start"], "2026-03-08T09:00:00-04:00")
        self.assertEqual(s["notify_at"], "2026-03-07T08:00:00-05:00")
        self.assertEqual(s["hours_until_class"], 25.0)


# ----------------------------------------------------------------------------- course / check


class CourseTests(SyllabiTestCase):
    def setUp(self):
        super().setUp()
        self.fake.add(upcoming_url(14), json_body=payload())

    def test_course_by_id(self):
        body = self.ok("course", "c-mas")
        self.assertEqual(self.fake.urls(), [upcoming_url(14)])
        self.assertEqual(body["course"]["code"], "MAS.665")
        self.assertEqual(body["course"]["schedule"]["meeting_days"], ["Tue"])
        self.assertEqual(body["course"]["grading_rules"]["components"][0]["name"], "HW")
        self.assertEqual(body["course"]["policies"], {"late": "one day grace"})
        self.assertEqual(body["next_sessions"], [{"date": "2026-09-29", "start": "13:00", "end": "16:00"}])

    def test_course_by_code_is_case_insensitive(self):
        body = self.ok("course", "mas.665")
        self.assertEqual(body["course"]["id"], "c-mas")

    def test_unknown_course_is_not_found_with_known_codes(self):
        err = self.assertError(self.run_cli("course", "6.006"), "SYLLABI_NOT_FOUND", retryable=False)
        self.assertEqual(err["detail"]["known_codes"], ["MAS.665", "15.286"])

    def test_blank_course_is_usage(self):
        self.assertError(self.run_cli("course", "  "), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_huge_sub_documents_are_replaced_by_a_marker(self):
        data = payload()
        data["courses"][0]["policies"] = {"text": "p" * 30000}
        self.fake.routes.clear()
        self.fake.add(upcoming_url(14), json_body=data)
        body = self.ok("course", "MAS.665")
        self.assertEqual(body["course"]["policies"]["truncated"], True)
        self.assertGreater(body["course"]["policies"]["chars"], 20000)


class CheckTests(SyllabiTestCase):
    def test_check_reports_counts_only(self):
        self.fake.add(upcoming_url(1), json_body=payload())
        body = self.ok("check")
        self.assertEqual(body, {"ok": True, "timezone": "America/New_York", "now": NOW, "courses": 2,
                                "sessions": 3, "events": 4, "endpoint": "/agent-upcoming"})
        self.assertNotIn("warnings", body)

    def test_check_warns_about_an_odd_looking_token(self):
        self.fake.add(upcoming_url(1), json_body=payload())
        body = self.ok("check", env=dict(self.env, SYLLABI_AGENT_TOKEN="not-an-agent-token"))
        self.assertIn("syl_agent_", body["warnings"][0])

    def test_check_surfaces_auth_failure(self):
        self.fake.add(upcoming_url(1), status=401, json_body={"error": "Unauthorized"})
        self.assertError(self.run_cli("check"), "SYLLABI_401")


# ----------------------------------------------------------------------------- secrets


class SecretTests(SyllabiTestCase):
    def test_token_and_anon_key_are_scrubbed_from_output(self):
        data = payload()
        data["courses"][0]["name"] = "leak " + TOKEN + " and " + ANON
        self.fake.add(upcoming_url(3), json_body=data)
        out = io.StringIO()
        with redirect_stderr(io.StringIO()):
            syllabi.main(["upcoming"], env=dict(self.env, SYLLABI_ANON_KEY=ANON), out=out)
        raw = out.getvalue()
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn(ANON, raw)
        self.assertIn("leak <redacted> and <redacted>", raw)

    def test_crash_exits_1_with_one_redacted_json_line(self):
        self.fake.add(upcoming_url(3), json_body=payload())
        orig = syllabi.build_sessions
        syllabi.build_sessions = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom " + TOKEN))
        try:
            out = io.StringIO()
            with redirect_stderr(io.StringIO()):
                rc = syllabi.main(["upcoming"], env=self.env, out=out)
        finally:
            syllabi.build_sessions = orig
        self.assertEqual(rc, 1)
        raw = out.getvalue()
        self.assertEqual(raw.count("\n"), 1)
        body = json.loads(raw)
        self.assertEqual(body["error"]["code"], "INTERNAL")
        self.assertIn("ValueError", body["error"]["message"])
        self.assertNotIn(TOKEN, raw)

    def test_error_messages_never_carry_the_token(self):
        self.fake.add(upcoming_url(3), status=401, json_body={"error": "bad token " + TOKEN})
        rc, body, err = self.run_cli("upcoming")
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertNotIn(TOKEN, err)


# ----------------------------------------------------------------------------- timezones


class TimezoneTests(unittest.TestCase):
    def test_tzinfo_for_uses_zoneinfo_when_present(self):
        if not HAVE_TZDATA:
            self.skipTest("no tzdata on this machine")
        tz, fallback = syllabi.tzinfo_for("America/New_York")
        self.assertFalse(fallback)
        self.assertEqual(tz.key, "America/New_York")

    def test_tzinfo_for_falls_back_without_zoneinfo(self):
        saved = sys.modules.get("zoneinfo")
        sys.modules["zoneinfo"] = None            # makes `import zoneinfo` raise ImportError
        try:
            tz, fallback = syllabi.tzinfo_for("America/New_York")
            self.assertTrue(fallback)
            self.assertIsInstance(tz, syllabi._USEasternTZ)
            tz2, fallback2 = syllabi.tzinfo_for("Europe/Paris")
            self.assertTrue(fallback2)
            self.assertEqual(tz2, dt.timezone.utc)
        finally:
            if saved is None:
                del sys.modules["zoneinfo"]
            else:
                sys.modules["zoneinfo"] = saved

    def test_unknown_zone_falls_back_to_utc(self):
        tz, fallback = syllabi.tzinfo_for("Mars/Olympus_Mons")
        self.assertTrue(fallback)
        self.assertEqual(tz, dt.timezone.utc)

    def test_builtin_eastern_rules_at_the_2026_transitions(self):
        tz = syllabi._USEasternTZ()
        utc = dt.timezone.utc

        def local(iso):
            return dt.datetime.fromisoformat(iso).replace(tzinfo=utc).astimezone(tz).isoformat()

        self.assertEqual(local("2026-01-15T12:00:00"), "2026-01-15T07:00:00-05:00")   # EST
        self.assertEqual(local("2026-07-04T12:00:00"), "2026-07-04T08:00:00-04:00")   # EDT
        self.assertEqual(local("2026-03-08T06:59:00"), "2026-03-08T01:59:00-05:00")   # just before spring forward
        self.assertEqual(local("2026-03-08T07:00:00"), "2026-03-08T03:00:00-04:00")   # 02:00 EST -> 03:00 EDT
        self.assertEqual(local("2026-11-01T05:59:00"), "2026-11-01T01:59:00-04:00")   # first pass of 01:xx
        self.assertEqual(local("2026-11-01T06:00:00"), "2026-11-01T01:00:00-05:00")   # second pass of 01:xx
        self.assertEqual(local("2026-11-01T07:00:00"), "2026-11-01T02:00:00-05:00")

    def test_builtin_eastern_wall_clock_offsets(self):
        tz = syllabi._USEasternTZ()
        self.assertEqual(dt.datetime(2026, 9, 29, 13, 0, tzinfo=tz).utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(dt.datetime(2026, 12, 1, 13, 0, tzinfo=tz).utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(dt.datetime(2026, 9, 29, 13, 0, tzinfo=tz).tzname(), "EDT")
        self.assertEqual(dt.datetime(2026, 12, 1, 13, 0, tzinfo=tz).tzname(), "EST")
        ambiguous = dt.datetime(2026, 11, 1, 1, 30, tzinfo=tz)
        self.assertEqual(ambiguous.utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(ambiguous.replace(fold=1).utcoffset(), dt.timedelta(hours=-5))
        gap = dt.datetime(2026, 3, 8, 2, 30, tzinfo=tz)
        self.assertEqual(gap.utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(gap.replace(fold=1).utcoffset(), dt.timedelta(hours=-4))

    def test_builtin_eastern_matches_zoneinfo_across_two_years(self):
        if not HAVE_TZDATA:
            self.skipTest("no tzdata on this machine")
        ours = syllabi._USEasternTZ()
        theirs = zoneinfo.ZoneInfo("America/New_York")
        t = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2028, 1, 1, tzinfo=dt.timezone.utc)
        step = dt.timedelta(minutes=17)
        while t < end:
            a, b = t.astimezone(ours), t.astimezone(theirs)
            self.assertEqual((a.replace(tzinfo=None), a.utcoffset()), (b.replace(tzinfo=None), b.utcoffset()), t)
            t += step

    def test_planning_records_use_the_fallback_when_tzdata_is_missing(self):
        saved = sys.modules.get("zoneinfo")
        sys.modules["zoneinfo"] = None
        fake = FakeTransport().add(upcoming_url(3), json_body=payload())
        orig = (syllabi.transport, syllabi.now_utc)
        syllabi.transport = fake
        syllabi.now_utc = lambda: dt.datetime.fromisoformat(NOW).astimezone(dt.timezone.utc)
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                rc = syllabi.main(["-v", "upcoming"], env={"SYLLABI_BASE_URL": BASE, "SYLLABI_AGENT_TOKEN": TOKEN}, out=out)
            body = json.loads(out.getvalue())
        finally:
            syllabi.transport, syllabi.now_utc = orig
            if saved is None:
                del sys.modules["zoneinfo"]
            else:
                sys.modules["zoneinfo"] = saved
        self.assertEqual(rc, 0)
        self.assertTrue(body["timezone_fallback"])
        self.assertIn("built-in fallback", err.getvalue())
        self.assertEqual(body["sessions"][1]["class_start"], "2026-09-29T13:00:00-04:00")
        self.assertEqual(body["sessions"][1]["notify_at"], "2026-09-28T13:00:00-04:00")


# ----------------------------------------------------------------------------- the real entry point


class SubprocessTests(unittest.TestCase):
    def test_symlink_runs_and_prints_the_envelope(self):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "syllabi"), "check"],
                              env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(body["error"]["code"], "USAGE")
        self.assertIn("SYLLABI_BASE_URL", body["error"]["message"])

    def test_refused_connection_is_unavailable(self):
        env = {"PATH": os.environ.get("PATH", ""), "SYLLABI_BASE_URL": "https://127.0.0.1:9",
               "SYLLABI_AGENT_TOKEN": TOKEN, "SYLLABI_TIMEOUT": "3"}
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "syllabi.py"), "check"],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(body["error"]["code"], "SYLLABI_UNAVAILABLE")
        self.assertTrue(body["error"]["retryable"])
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
