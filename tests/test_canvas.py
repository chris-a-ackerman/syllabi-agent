"""Tests for workspace/skills/canvas/scripts/canvas.py (SYL-93).

No network: the module's `transport`, `resolve_host` and `sleep` hooks are replaced with fakes.

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "workspace", "skills", "canvas", "scripts")
sys.path.insert(0, SCRIPTS)

import canvas  # noqa: E402

BASE = "https://canvas.example.edu"
HOST = "canvas.example.edu"
TOKEN = "sekrit-token-1234567890"


def _key(url):
    p = urllib.parse.urlparse(url)
    return (p.netloc.lower(), p.path, tuple(sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True))))


class FakeTransport:
    """Routes (host, path, sorted query) -> a queue of responses. Records every call."""

    def __init__(self):
        self.routes = {}
        self.calls = []   # (method, url, headers)

    def add(self, url, status=200, body=None, json_body=None, headers=None, repeat=False):
        payload = json.dumps(json_body).encode() if json_body is not None else (body or b"")
        entry = (status, {k.lower(): v for k, v in (headers or {}).items()}, payload)
        self.routes.setdefault(_key(url), []).append((entry, repeat))
        return self

    def __call__(self, method, url, headers, timeout, max_bytes=None):
        self.calls.append((method, url, dict(headers)))
        queue = self.routes.get(_key(url))
        if not queue:
            raise AssertionError("unexpected request: %s %s" % (method, url))
        (status, hdrs, payload), repeat = queue[0]
        if not repeat and len(queue) > 1:
            queue.pop(0)
        elif not repeat:
            queue[0] = ((status, hdrs, payload), True)   # last response sticks
        if max_bytes is not None and len(payload) > max_bytes:
            raise canvas.CanvasError("FILE_TOO_LARGE", "file body exceeds %d bytes" % max_bytes)
        return canvas.Response(status, hdrs, payload)

    def urls(self):
        return [u for _, u, _ in self.calls]


def api(path, **params):
    url = BASE + "/api/v1" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return url


class CanvasTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransport()
        self.slept = []
        self.resolved = {}    # host -> [ip]
        self._orig = (canvas.transport, canvas.resolve_host, canvas.sleep)
        canvas.transport = self.fake
        canvas.sleep = self.slept.append
        canvas.resolve_host = self._resolve
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"CANVAS_BASE_URL": BASE, "CANVAS_TOKEN": TOKEN, "DATA_DIR": self.tmp.name}

    def tearDown(self):
        canvas.transport, canvas.resolve_host, canvas.sleep = self._orig
        self.tmp.cleanup()

    def _resolve(self, host):
        if host in self.resolved:
            return self.resolved[host]
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            return ["93.184.216.34"]   # a public address by default

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = canvas.main(list(argv), env=self.env if env is None else env, out=out)
        raw = out.getvalue()
        self.assertEqual(raw.count("\n"), 1, "exactly one JSON line expected, got: %r" % raw)
        return code, json.loads(raw), err.getvalue()

    def assertError(self, result, code):
        rc, body, _ = result
        self.assertEqual(rc, 2, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], code, body)
        return body["error"]


# ----------------------------------------------------------------------------- config


class ConfigTests(CanvasTestCase):
    def test_missing_base_url_is_usage_error(self):
        err = self.assertError(self.run_cli("whoami", env={"CANVAS_TOKEN": TOKEN}), "USAGE")
        self.assertIn("CANVAS_BASE_URL", err["message"])
        self.assertEqual(self.fake.calls, [])

    def test_http_base_url_is_refused(self):
        env = dict(self.env, CANVAS_BASE_URL="http://canvas.example.edu")
        self.assertError(self.run_cli("whoami", env=env), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_missing_token_is_canvas_401_without_network(self):
        env = {"CANVAS_BASE_URL": BASE, "DATA_DIR": self.tmp.name}
        self.assertError(self.run_cli("whoami", env=env), "CANVAS_401")
        self.assertEqual(self.fake.calls, [])

    def test_canvas_api_token_alias_is_accepted(self):
        self.fake.add(api("/users/self"), json_body={"id": 7, "name": "Chris"})
        env = {"CANVAS_BASE_URL": BASE + "/", "CANVAS_API_TOKEN": TOKEN}
        rc, body, _ = self.run_cli("whoami", env=env)
        self.assertEqual(rc, 0)
        self.assertEqual(body, {"ok": True, "user": {"id": 7, "name": "Chris"}})
        self.assertEqual(self.fake.calls[0][2]["Authorization"], "Bearer " + TOKEN)

    def test_unknown_command_is_usage_error(self):
        self.assertError(self.run_cli("bogus"), "USAGE")
        self.assertError(self.run_cli(), "USAGE")


# ----------------------------------------------------------------------------- whoami / errors


class ErrorMappingTests(CanvasTestCase):
    def _whoami_status(self, status, body=b'{"errors":[{"message":"x"}]}', headers=None):
        self.fake.add(api("/users/self"), status=status, body=body, headers=headers)
        return self.run_cli("whoami")

    def test_fake_token_yields_canvas_401(self):
        err = self.assertError(self._whoami_status(401), "CANVAS_401")
        self.assertEqual(err["status"], 401)
        self.assertFalse(err["retryable"])

    def test_403_and_404_are_distinct(self):
        self.assertError(self._whoami_status(403), "CANVAS_403")
        self.fake = FakeTransport()
        canvas.transport = self.fake
        self.assertError(self._whoami_status(404), "CANVAS_404")

    def test_5xx_is_retryable_net_error(self):
        err = self.assertError(self._whoami_status(502, body=b"bad gateway"), "CANVAS_NET")
        self.assertTrue(err["retryable"])
        self.assertEqual(err["status"], 502)

    def test_non_json_body_is_net_error(self):
        err = self.assertError(self._whoami_status(200, body=b"<html>login</html>"), "CANVAS_NET")
        self.assertTrue(err["retryable"])

    def test_while1_prefix_is_stripped(self):
        self.fake.add(api("/users/self"), body=b'while(1);{"id": 1, "name": "n"}')
        rc, body, _ = self.run_cli("whoami")
        self.assertEqual((rc, body["user"]["id"]), (0, 1))

    def test_429_retries_once_after_retry_after(self):
        self.fake.add(api("/users/self"), status=429, body=b"", headers={"Retry-After": "2"})
        self.fake.add(api("/users/self"), json_body={"id": 1, "name": "n"})
        rc, body, _ = self.run_cli("whoami")
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.slept, [2.0])
        self.assertEqual(len(self.fake.calls), 2)

    def test_retry_after_is_capped(self):
        self.fake.add(api("/users/self"), status=429, body=b"", headers={"Retry-After": "600"})
        self.fake.add(api("/users/self"), json_body={"id": 1, "name": "n"})
        self.run_cli("whoami")
        self.assertEqual(self.slept, [canvas.RATE_RETRY_MAX_SLEEP])

    def test_429_twice_is_canvas_rate(self):
        self.fake.add(api("/users/self"), status=429, body=b"", repeat=True)
        err = self.assertError(self.run_cli("whoami"), "CANVAS_RATE")
        self.assertTrue(err["retryable"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_403_rate_limit_body_is_canvas_rate(self):
        self.fake.add(api("/users/self"), status=403, body=b"403 Forbidden (Rate Limit Exceeded)", repeat=True)
        self.assertError(self.run_cli("whoami"), "CANVAS_RATE")

    def test_network_failure_is_canvas_net(self):
        def boom(method, url, headers, timeout, max_bytes=None):
            raise canvas.CanvasError("CANVAS_NET", "network error: timed out", retryable=True)
        canvas.transport = boom
        err = self.assertError(self.run_cli("whoami"), "CANVAS_NET")
        self.assertTrue(err["retryable"])

    def test_output_never_contains_the_token(self):
        self.fake.add(api("/users/self"), status=401, body=b"")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            canvas.main(["--verbose", "whoami"], env=self.env, out=out)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertIn("GET /api/v1/users/self", err.getvalue())

    def test_crash_is_exit_1_with_redacted_message(self):
        def boom(method, url, headers, timeout, max_bytes=None):
            raise RuntimeError("leaked header Bearer " + TOKEN)
        canvas.transport = boom
        out = io.StringIO()
        rc = canvas.main(["whoami"], env=self.env, out=out)
        body = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(body["error"]["code"], "INTERNAL")
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn("<redacted>", body["error"]["message"])

    def test_verbose_log_omits_query_strings(self):
        self.fake.add(api("/courses/1/modules", **{"include[]": ["items", "content_details"], "per_page": 100}),
                      json_body=[])
        _, _, err = self.run_cli("-v", "modules", "1")
        self.assertIn("GET /api/v1/courses/1/modules\n", err)
        self.assertNotIn("per_page", err)


# ----------------------------------------------------------------------------- modules


MODULES_Q = {"include[]": ["items", "content_details"], "per_page": 100}


class ModulesTests(CanvasTestCase):
    def test_flattens_items_and_follows_link_next(self):
        page2 = api("/courses/40577/modules", page=2, per_page=100, **{"include[]": ["items", "content_details"]})
        self.fake.add(api("/courses/40577/modules", **MODULES_Q), json_body=[
            {"id": 10, "name": "Week 4: Reliable agents", "position": 4, "items": [
                {"id": 100, "type": "SubHeader", "title": "Readings", "position": 1},
                {"id": 101, "type": "File", "title": "week4.pdf", "position": 2, "content_id": 555,
                 "url": BASE + "/api/v1/courses/40577/files/555", "html_url": BASE + "/courses/40577/modules/items/101",
                 "content_details": {"size": 1234, "content_type": "application/pdf", "locked_for_user": False}},
                {"id": 102, "type": "Page", "title": "Pre-class", "position": 3, "page_url": "pre-class-4",
                 "html_url": BASE + "/courses/40577/pages/pre-class-4"},
                {"id": 103, "type": "ExternalUrl", "title": "HBS case", "position": 4,
                 "external_url": "https://hbsp.harvard.edu/x"},
            ]},
        ], headers={"Link": '<%s>; rel="next", <%s>; rel="last"' % (page2, page2)})
        self.fake.add(page2, json_body=[
            {"id": 11, "name": "Week 5", "position": 5, "items": [
                {"id": 110, "type": "Assignment", "title": "Pre-class questions 5", "position": 1, "content_id": 900},
            ]},
        ])
        rc, body, _ = self.run_cli("modules", "40577")
        self.assertEqual(rc, 0, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["modules"], 2)
        items = body["items"]
        self.assertEqual([i["item_type"] for i in items], ["SubHeader", "File", "Page", "ExternalUrl", "Assignment"])
        pdf = items[1]
        self.assertEqual(pdf["module"], "Week 4: Reliable agents")
        self.assertEqual(pdf["position"], 4)
        self.assertEqual(pdf["content_id"], 555)
        self.assertEqual(pdf["content_type"], "application/pdf")
        self.assertEqual(pdf["size"], 1234)
        self.assertEqual(items[2]["page_url"], "pre-class-4")
        self.assertEqual(items[3]["external_url"], "https://hbsp.harvard.edu/x")
        self.assertEqual(items[4]["module"], "Week 5")
        # Both pages requested with the token, per_page=100 and the include params.
        self.assertEqual(len(self.fake.calls), 2)
        for _, url, headers in self.fake.calls:
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
            self.assertIn("per_page=100", url)
            self.assertIn("include%5B%5D=items", url)

    def test_fetches_items_when_canvas_omits_them(self):
        self.fake.add(api("/courses/1/modules", **MODULES_Q), json_body=[{"id": 5, "name": "Big", "position": 1}])
        self.fake.add(api("/courses/1/modules/5/items", **{"include[]": ["content_details"], "per_page": 100}),
                      json_body=[{"id": 50, "type": "File", "title": "a.pdf", "content_id": 1}])
        rc, body, _ = self.run_cli("modules", "1")
        self.assertEqual(rc, 0, body)
        self.assertEqual([i["title"] for i in body["items"]], ["a.pdf"])

    def test_next_link_on_another_host_is_not_followed(self):
        evil = "https://evil.example.net/api/v1/courses/1/modules?page=2"
        self.fake.add(api("/courses/1/modules", **MODULES_Q), json_body=[], headers={"Link": '<%s>; rel="next"' % evil})
        rc, body, _ = self.run_cli("modules", "1")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.fake.calls), 1)

    def test_v0_alias_list_modules(self):
        self.fake.add(api("/courses/1/modules", **MODULES_Q), json_body=[])
        rc, body, _ = self.run_cli("list_modules", "1")
        self.assertEqual((rc, body["items"]), (0, []))

    def test_module_404_maps_to_canvas_404(self):
        self.fake.add(api("/courses/999/modules", **MODULES_Q), status=404, body=b'{"errors":[]}')
        self.assertError(self.run_cli("modules", "999"), "CANVAS_404")


# ----------------------------------------------------------------------------- files


class FilesTests(CanvasTestCase):
    def _seed(self):
        self.fake.add(api("/courses/1/files", sort="updated_at", order="desc", per_page=100), json_body=[
            {"id": 1, "display_name": "Week 4 - Reading.pdf", "filename": "week4.pdf", "content-type": "application/pdf",
             "size": 10, "updated_at": "2026-09-20T00:00:00Z", "folder_id": 21, "url": BASE + "/files/1/download?verifier=v"},
            {"id": 2, "display_name": "syllabus.pdf", "filename": "syllabus.pdf", "content-type": "application/pdf",
             "size": 20, "updated_at": "2026-09-01T00:00:00Z", "folder_id": 20},
        ])
        self.fake.add(api("/courses/1/folders", per_page=100), json_body=[
            {"id": 20, "full_name": "course files"},
            {"id": 21, "full_name": "course files/Readings/Week 4"},
        ])

    def test_lists_files_with_folder_paths(self):
        self._seed()
        rc, body, _ = self.run_cli("files", "1")
        self.assertEqual(rc, 0, body)
        files = body["files"]
        self.assertEqual([f["id"] for f in files], [1, 2])
        self.assertEqual(files[0]["folder"], "Readings/Week 4")
        self.assertEqual(files[1]["folder"], "")
        self.assertEqual(files[0]["content_type"], "application/pdf")
        self.assertNotIn("url", files[0], "pre-signed urls must not be echoed")

    def test_folder_filter_is_case_insensitive_substring(self):
        self._seed()
        rc, body, _ = self.run_cli("files", "1", "--folder", "week 4")
        self.assertEqual([f["id"] for f in body["files"]], [1])

    def test_folder_lookup_failure_degrades(self):
        self.fake.add(api("/courses/1/files", sort="updated_at", order="desc", per_page=100),
                      json_body=[{"id": 1, "display_name": "a", "folder_id": 3}])
        self.fake.add(api("/courses/1/folders", per_page=100), status=403, body=b"")
        rc, body, _ = self.run_cli("files", "1")
        self.assertEqual(rc, 0, body)
        self.assertIsNone(body["files"][0]["folder"])
        self.assertEqual(body["folders_error"], "CANVAS_403")


# ----------------------------------------------------------------------------- download


class DownloadTests(CanvasTestCase):
    META = api("/courses/40577/files/555")
    SIGNED = BASE + "/files/555/download?download_frd=1&verifier=abc123"
    STORAGE = "https://inst-fs-iad-prod.inscloudgate.net/files/555/pdf?token=xyz"
    PDF = b"%PDF-1.4 hello"

    def setUp(self):
        super().setUp()
        self.dest = os.path.join(self.tmp.name, "readings", "40577", "2026-09-29")

    def _meta(self, **overrides):
        meta = {"id": 555, "display_name": "Week 4 Reading.pdf", "filename": "week4.pdf", "size": len(self.PDF),
                "content-type": "application/pdf", "url": self.SIGNED, "locked_for_user": False}
        meta.update(overrides)
        self.fake.add(self.META, json_body=meta)

    def test_downloads_through_redirect_without_forwarding_the_token(self):
        self._meta()
        self.fake.add(self.SIGNED, status=302, body=b"", headers={"Location": self.STORAGE})
        self.fake.add(self.STORAGE, body=self.PDF, headers={"Content-Type": "application/pdf"})
        rc, body, _ = self.run_cli("download", "40577", "555", self.dest)
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["path"], os.path.join(os.path.realpath(self.dest), "Week 4 Reading.pdf"))
        self.assertEqual(body["bytes"], len(self.PDF))
        self.assertFalse(body["skipped"])
        self.assertEqual(len(body["sha256"]), 64)
        with open(body["path"], "rb") as fh:
            self.assertEqual(fh.read(), self.PDF)
        self.assertFalse(os.path.exists(body["path"] + ".part"))
        by_url = {u: h for _, u, h in self.fake.calls}
        self.assertIn("Authorization", by_url[self.META])
        self.assertIn("Authorization", by_url[self.SIGNED], "same-host pre-signed url may carry the token")
        self.assertNotIn("Authorization", by_url[self.STORAGE], "token must never cross a redirect")

    def test_second_download_is_skipped(self):
        self._meta()
        self.fake.add(self.SIGNED, body=self.PDF)
        self.run_cli("download", "40577", "555", self.dest)
        calls_before = len(self.fake.calls)
        rc, body, _ = self.run_cli("download", "40577", "555", self.dest)
        self.assertEqual(rc, 0)
        self.assertTrue(body["skipped"])
        self.assertEqual(body["bytes"], len(self.PDF))
        self.assertEqual(len(self.fake.calls), calls_before + 1, "only the metadata call")

    def test_display_name_is_sanitized_against_traversal(self):
        self._meta(display_name="../../../etc/passwd")
        self.fake.add(self.SIGNED, body=self.PDF)
        rc, body, _ = self.run_cli("download", "40577", "555", self.dest)
        self.assertEqual(rc, 0, body)
        self.assertEqual(os.path.dirname(body["path"]), os.path.realpath(self.dest))
        name = os.path.basename(body["path"])
        self.assertEqual(name, body["display_name"])
        self.assertFalse(name.startswith("."))
        self.assertNotIn("/", name)
        self.assertTrue(os.path.isfile(body["path"]))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "etc", "passwd")))

    def test_unsafe_name_falls_back_to_filename_then_rejects(self):
        self._meta(display_name="....", filename="week4.pdf")
        self.fake.add(self.SIGNED, body=self.PDF)
        rc, body, _ = self.run_cli("download", "40577", "555", self.dest)
        self.assertEqual(os.path.basename(body["path"]), "week4.pdf")
        self.fake = FakeTransport()
        canvas.transport = self.fake
        self._meta(display_name="..", filename="​")
        self.assertError(self.run_cli("download", "40577", "555", self.dest), "BAD_FILENAME")

    def test_dest_dir_outside_readings_is_refused(self):
        outside = os.path.join(self.tmp.name, "elsewhere")
        self.assertError(self.run_cli("download", "40577", "555", outside), "USAGE")
        self.assertError(self.run_cli("download", "40577", "555", os.path.join(self.tmp.name, "readings", "..", "x")), "USAGE")
        self.assertEqual(self.fake.calls, [], "refused before any request")

    def test_oversized_file_is_refused_before_fetching(self):
        self._meta(size=canvas.MAX_FILE_BYTES + 1)
        err = self.assertError(self.run_cli("download", "40577", "555", self.dest), "FILE_TOO_LARGE")
        self.assertEqual(err["detail"]["canvas_url"], BASE + "/courses/40577/files/555")
        self.assertEqual(len(self.fake.calls), 1)

    def test_locked_file_is_canvas_403(self):
        self._meta(locked_for_user=True, lock_explanation="This file is locked until Oct 1.", url=None)
        self.assertError(self.run_cli("download", "40577", "555", self.dest), "CANVAS_403")

    def test_file_404_is_canvas_404(self):
        self.fake.add(self.META, status=404, body=b"")
        self.assertError(self.run_cli("download", "40577", "555", self.dest), "CANVAS_404")

    def test_http_redirect_is_refused(self):
        self._meta()
        self.fake.add(self.SIGNED, status=302, body=b"", headers={"Location": "http://files.example.net/x"})
        err = self.assertError(self.run_cli("download", "40577", "555", self.dest), "CANVAS_NET")
        self.assertIn("non-https", err["message"])
        self.assertFalse(os.path.exists(self.dest))

    def test_redirect_to_private_or_loopback_is_refused(self):
        for target, ips in (("https://internal.example.net/x", ["10.0.0.5"]),
                            ("https://localhost/x", ["127.0.0.1"]),
                            ("https://169.254.169.254/latest/meta-data", None),
                            ("https://v6.example.net/x", ["fe80::1"])):
            self.fake = FakeTransport()
            canvas.transport = self.fake
            if ips:
                self.resolved[urllib.parse.urlparse(target).hostname] = ips
            self._meta()
            self.fake.add(self.SIGNED, status=302, body=b"", headers={"Location": target})
            err = self.assertError(self.run_cli("download", "40577", "555", self.dest), "CANVAS_NET")
            self.assertIn("private or local", err["message"], target)
            self.assertEqual([u for _, u, _ in self.fake.calls], [self.META, self.SIGNED], target)

    def test_too_many_redirects(self):
        self._meta()
        loop = self.STORAGE
        self.fake.add(self.SIGNED, status=302, body=b"", headers={"Location": loop})
        self.fake.add(loop, status=302, body=b"", headers={"Location": loop}, repeat=True)
        err = self.assertError(self.run_cli("download", "40577", "555", self.dest), "CANVAS_NET")
        self.assertIn("too many redirects", err["message"])

    def test_expired_signed_url_maps_to_403(self):
        self._meta()
        self.fake.add(self.SIGNED, status=403, body=b"expired")
        self.assertError(self.run_cli("download", "40577", "555", self.dest), "CANVAS_403")

    def test_v0_alias_download_file(self):
        self._meta()
        self.fake.add(self.SIGNED, body=self.PDF)
        rc, body, _ = self.run_cli("download_file", "40577", "555", self.dest)
        self.assertEqual(rc, 0, body)


# ----------------------------------------------------------------------------- assignments / page


class AssignmentsTests(CanvasTestCase):
    def test_upcoming_bucket_full_description_links_and_submitted(self):
        long_html = "<p>Answer these before class:</p><ol>" + "".join(
            "<li>Question %d: what does the reading say about %s?</li>" % (i, "x" * 400) for i in range(1, 6)
        ) + "</ol><p>Reading: <a href='/courses/1/files/9?wrap=1'>week4.pdf</a> and "\
            "<a href=\"https://hbsp.harvard.edu/import/123\">the case</a>.</p><script>alert(1)</script>"
        self.fake.add(api("/courses/1/assignments", bucket="upcoming", per_page=100, **{"include[]": ["submission"]}),
                      json_body=[
                          {"id": 900, "name": "Pre-class questions 4", "due_at": "2026-09-28T23:59:00Z",
                           "html_url": BASE + "/courses/1/assignments/900", "description": long_html,
                           "submission_types": ["online_text_entry"],
                           "submission": {"workflow_state": "unsubmitted", "submitted_at": None}},
                          {"id": 901, "name": "Reflection", "due_at": None, "html_url": BASE + "/courses/1/assignments/901",
                           "description": None, "submission_types": ["online_upload"],
                           "submission": {"workflow_state": "submitted", "submitted_at": "2026-09-20T00:00:00Z"}},
                      ])
        rc, body, _ = self.run_cli("assignments", "1", "--upcoming")
        self.assertEqual(rc, 0, body)
        a, b = body["assignments"]
        self.assertEqual(a["id"], 900)
        self.assertFalse(a["submitted"])
        self.assertTrue(b["submitted"])
        self.assertEqual(b["description_text"], "")
        text = a["description_text"]
        self.assertTrue(text.startswith("Answer these before class:\n\n1. Question 1:"))
        self.assertIn("5. Question 5:", text)
        self.assertGreater(len(text), 2000, "description must not be truncated")
        self.assertNotIn("<", text)
        self.assertNotIn("alert(1)", text)
        self.assertEqual(a["links"], [
            {"text": "week4.pdf", "href": BASE + "/courses/1/files/9?wrap=1"},
            {"text": "the case", "href": "https://hbsp.harvard.edu/import/123"},
        ])
        self.assertIn("bucket=upcoming", self.fake.calls[0][1])

    def test_without_upcoming_flag_no_bucket(self):
        self.fake.add(api("/courses/1/assignments", per_page=100, **{"include[]": ["submission"]}), json_body=[])
        rc, body, _ = self.run_cli("assignments", "1")
        self.assertEqual((rc, body["assignments"]), (0, []))
        self.assertNotIn("bucket", self.fake.calls[0][1])

    def test_v0_alias_upcoming_assignments_uses_bucket(self):
        self.fake.add(api("/courses/1/assignments", bucket="upcoming", per_page=100, **{"include[]": ["submission"]}),
                      json_body=[])
        rc, body, _ = self.run_cli("upcoming_assignments", "1")
        self.assertEqual(rc, 0, body)


class PageTests(CanvasTestCase):
    def test_page_by_slug_is_stripped_to_text(self):
        self.fake.add(api("/courses/1/pages/pre-class-4"), json_body={
            "url": "pre-class-4", "title": "Pre-class 4", "updated_at": "2026-09-20T00:00:00Z",
            "html_url": BASE + "/courses/1/pages/pre-class-4",
            "body": "<h2>Prep</h2><p>Read&nbsp;the <b>two</b> papers.<br>Bring questions &amp; a laptop.</p>"
                    "<ul><li>One</li><li>Two</li></ul>",
        })
        rc, body, _ = self.run_cli("page", "1", "pre-class-4")
        self.assertEqual(rc, 0, body)
        page = body["page"]
        self.assertEqual(page["title"], "Pre-class 4")
        self.assertEqual(page["body_text"], "Prep\n\nRead the two papers.\nBring questions & a laptop.\n\n- One\n- Two")
        self.assertEqual(page["links"], [])

    def test_page_id_is_quoted_and_404_maps(self):
        self.fake.add(api("/courses/1/pages/week%204"), status=404, body=b"")
        self.assertError(self.run_cli("get_page", "1", "week 4"), "CANVAS_404")
        self.assertTrue(self.fake.calls[0][1].endswith("/pages/week%204"))


# ----------------------------------------------------------------------------- units


class HtmlToTextTests(unittest.TestCase):
    def test_lists_entities_and_script(self):
        text, links = canvas.html_to_text(
            '<div>Intro</div><ol><li>Q1 &amp; more</li><li>Q2 <a href="#top">top</a></li></ol>'
            '<style>p{}</style><p>End</p>', BASE)
        self.assertEqual(text, "Intro\n\n1. Q1 & more\n2. Q2 top\n\nEnd")
        self.assertEqual(links, [])

    def test_relative_links_resolve_and_dedupe(self):
        _, links = canvas.html_to_text('<a href="/a">A</a> <a href="/a">A again</a> <a href="javascript:x">j</a>'
                                       '<a href="mailto:x@y">m</a>', BASE + "/courses/1")
        self.assertEqual(links, [{"text": "A", "href": BASE + "/a"}])

    def test_empty(self):
        self.assertEqual(canvas.html_to_text(None), ("", []))
        self.assertEqual(canvas.html_to_text(""), ("", []))


class SafeBasenameTests(unittest.TestCase):
    def test_cases(self):
        sb = canvas.safe_basename
        self.assertEqual(sb("Week 4 - Reading.pdf"), "Week 4 - Reading.pdf")
        self.assertEqual(sb("../../etc/passwd"), "_.._etc_passwd")
        self.assertEqual(sb("..\\..\\win.ini"), "_.._win.ini")
        self.assertEqual(sb(".hidden.pdf"), "hidden.pdf")
        self.assertEqual(sb("Séance 4 – été.pdf"), "Seance 4 ete.pdf")   # accents folded, dash dropped
        self.assertEqual(sb("a\x00b.pdf"), "a_b.pdf")
        self.assertEqual(sb("  spaced   name.pdf  "), "spaced name.pdf")
        self.assertEqual(sb("trailing dots..."), "trailing dots")
        self.assertEqual(sb(".."), "")
        self.assertEqual(sb(""), "")
        self.assertEqual(sb(None), "")
        long_name = "x" * 300 + ".pdf"
        self.assertLessEqual(len(sb(long_name)), 200)
        self.assertTrue(sb(long_name).endswith(".pdf"))


class LinkHeaderTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(canvas._next_link('<https://c/x?page=2>; rel="next", <https://c/x?page=9>; rel="last"'),
                         "https://c/x?page=2")
        self.assertEqual(canvas._next_link('<https://c/x?page=1>; rel="current", <https://c/x?page=9>; rel="last"'), None)
        self.assertEqual(canvas._next_link(None), None)


class CliProcessTests(unittest.TestCase):
    """One real subprocess through the `canvas` symlink: shebang, exit code and single-line JSON."""

    def test_symlink_runs_and_reports_usage(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CANVAS_")}
        env["PATH"] = os.environ.get("PATH", "")
        proc = subprocess.run([os.path.join(SCRIPTS, "canvas"), "whoami"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc)
        body = json.loads(proc.stdout)
        self.assertEqual(body["error"]["code"], "USAGE")


if __name__ == "__main__":
    unittest.main()
