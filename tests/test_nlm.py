"""Tests for workspace/skills/nlm/scripts/nlm.py (SYL-94).

No network and no notebooklm-py: the module's `open_client`, `sleep` and `drive_put` hooks are
replaced with fakes that mimic the notebooklm-py client surface the wrapper uses.

    python3 -m unittest discover -s tests -v
"""
import asyncio
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "workspace", "skills", "nlm", "scripts")
sys.path.insert(0, SCRIPTS)

import nlm  # noqa: E402

COURSE = "MAS.665"
DATE = "2026-09-29"
TITLE = "MAS.665 — 2026-09-29"
COOKIE = "sekrit-cookie-value-1234567890abcdef"
M4A = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x00" * 64
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" + b"\x00" * 64


# ----------------------------------------------------------------------------- fake notebooklm-py

class NotebookLMError(Exception):
    pass


class RPCError(NotebookLMError):
    pass


class AuthError(RPCError):
    pass


class RateLimitError(RPCError):
    pass


class NotFoundError(NotebookLMError):
    pass


class NotebookNotFoundError(NotFoundError, RPCError):
    pass


class SourceAddError(NotebookLMError):
    pass


class NetworkError(NotebookLMError):
    pass


class ServerError(RPCError):
    pass


class ArtifactType:
    def __init__(self, name):
        self.name = name
        self.value = name.lower()


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeNotebooks:
    def __init__(self, client):
        self.c = client

    async def list(self):
        self.c.record("notebooks.list")
        return [Obj(id=i, title=n["title"]) for i, n in self.c.state.items()]

    async def create(self, title):
        self.c.record("notebooks.create", title)
        nid = "nb-%d" % (len(self.c.state) + 1)
        self.c.state[nid] = {"title": title, "sources": [], "artifacts": []}
        return Obj(id=nid, title=title)

    async def get(self, notebook_id):
        self.c.record("notebooks.get", notebook_id)
        if notebook_id not in self.c.state:
            raise NotebookNotFoundError("Notebook not found: " + notebook_id)
        return Obj(id=notebook_id, title=self.c.state[notebook_id]["title"])


class FakeSources:
    def __init__(self, client):
        self.c = client

    async def list(self, notebook_id):
        self.c.record("sources.list", notebook_id)
        self.c.list_calls += 1
        nb = self.c._nb(notebook_id)
        if self.c.list_calls > self.c.ready_after_lists:
            for s in nb["sources"]:
                if s.status == "processing":
                    s.status = "ready"
        if self.c.list_calls <= self.c.list_lag:
            return []                      # NotebookLM hasn't caught up with the upload yet
        return list(nb["sources"])

    async def add_file(self, notebook_id, file_path, **kw):
        self.c.record("sources.add_file", notebook_id, file_path, kw)
        nb = self.c._nb(notebook_id)
        base = os.path.basename(file_path)
        if base in self.c.reject_files:
            raise SourceAddError("Failed to add a source: unsupported file %s" % base)
        src = Obj(id="src-%d" % (len(nb["sources"]) + 1), title=os.path.splitext(base)[0],
                  status=self.c.new_source_status, kind="pdf")
        nb["sources"].append(src)
        return src


class FakeArtifacts:
    def __init__(self, client):
        self.c = client

    async def list(self, notebook_id):
        self.c.record("artifacts.list", notebook_id)
        return list(self.c._nb(notebook_id)["artifacts"])

    async def generate_audio(self, notebook_id, **kw):
        self.c.record("artifacts.generate_audio", notebook_id, kw)
        if self.c.generate_kwargs_supported is not None:
            for key in kw:
                if key not in self.c.generate_kwargs_supported:
                    raise TypeError("generate_audio() got an unexpected keyword argument '%s'" % key)
        nb = self.c._nb(notebook_id)
        art = Obj(id="art-%d" % (len(nb["artifacts"]) + 1), artifact_type=ArtifactType("AUDIO"),
                  status="pending", title=kw.get("title"), url=None)
        nb["artifacts"].append(art)
        return Obj(task_id="task-" + art.id, artifact_id=art.id, status="pending")

    async def download_audio(self, notebook_id, output_path, **kw):
        self.c.record("artifacts.download_audio", notebook_id, output_path, kw)
        with open(output_path, "wb") as fh:
            fh.write(self.c.audio_bytes)
        return output_path


class FakeClient:
    """Mimics the parts of notebooklm-py's NotebookLMClient the wrapper touches."""

    def __init__(self):
        self.state = {}
        self.calls = []
        self.opened = 0
        self.fail = {}                     # "notebooks.list" -> exception to raise
        self.reject_files = set()
        self.new_source_status = "processing"
        self.ready_after_lists = 0         # sources flip to ready once sources.list was called this often
        self.list_lag = 0                  # the first N sources.list calls return [] (eventual consistency)
        self.list_calls = 0
        self.audio_bytes = M4A
        self.generate_kwargs_supported = None
        self.notebooks = FakeNotebooks(self)
        self.sources = FakeSources(self)
        self.artifacts = FakeArtifacts(self)

    def _nb(self, notebook_id):
        if notebook_id not in self.state:
            raise NotebookNotFoundError("Notebook not found: " + notebook_id)
        return self.state[notebook_id]

    def record(self, op, *args):
        self.calls.append((op,) + args)
        if op in self.fail:
            raise self.fail[op]

    def seed(self, notebook_id="nb-9", title=TITLE, sources=(), artifacts=()):
        self.state[notebook_id] = {"title": title,
                                   "sources": [Obj(id="s%d" % i, title=t, status=s, kind="pdf")
                                               for i, (t, s) in enumerate(sources)],
                                   "artifacts": [Obj(id="a%d" % i, artifact_type=ArtifactType("AUDIO"),
                                                     status=s, title=title, url=u)
                                                 for i, (s, u) in enumerate(artifacts)]}
        return notebook_id

    def ops(self, name=None):
        return [c for c in self.calls if name is None or c[0] == name]

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *exc):
        return False


class NlmTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.realpath(self.tmp.name)
        self.readings = os.path.join(self.data, "readings", COURSE, DATE)
        os.makedirs(self.readings)
        os.makedirs(os.path.join(self.data, "secrets"))
        os.makedirs(os.path.join(self.data, "memory"))
        self.cookies = os.path.join(self.data, "secrets", "notebooklm-cookies.json")
        with open(self.cookies, "w") as fh:
            json.dump({"cookies": [{"name": "SID", "value": COOKIE, "domain": ".google.com"}], "origins": []}, fh)
        self.pdf1 = self._pdf("week4.pdf")
        self.pdf2 = self._pdf("Case Study.pdf")
        self.fake = FakeClient()
        self.slept = []
        self.drive_calls = []
        self.drive_result = {"ok": True, "remote_path": "Readings/%s/%s/%s.m4a" % (COURSE, DATE, DATE),
                             "share_link": "https://drive.google.com/file/d/abc/view", "uploaded": True}
        self._orig = (nlm.open_client, nlm.sleep, nlm.drive_put)
        nlm.open_client = lambda cfg: self.fake

        async def fake_sleep(seconds):
            self.slept.append(seconds)

        def fake_drive_put(cfg, local_path, remote_dir):
            self.drive_calls.append((local_path, remote_dir))
            return self.drive_result

        nlm.sleep = fake_sleep
        nlm.drive_put = fake_drive_put
        self.env = {"DATA_DIR": self.data, "NLM_COOKIES_PATH": self.cookies}

    def tearDown(self):
        nlm.open_client, nlm.sleep, nlm.drive_put = self._orig
        self.tmp.cleanup()

    def _pdf(self, name, content=b"%PDF-1.4 hello"):
        path = os.path.join(self.readings, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def run_cli(self, *argv, env=None, prog="nlm"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = nlm.main(list(argv), env=self.env if env is None else env, out=out, prog=prog)
        raw = out.getvalue()
        self.assertEqual(raw.count("\n"), 1, "exactly one JSON line expected, got: %r" % raw)
        return code, json.loads(raw), err.getvalue()

    def assertOk(self, result):
        rc, body, _ = result
        self.assertEqual(rc, 0, body)
        self.assertTrue(body["ok"])
        return body

    def assertError(self, result, code):
        rc, body, _ = result
        self.assertEqual(rc, 2, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], code, body)
        return body["error"]

    def write_prep_log(self, sessions):
        with open(os.path.join(self.data, "memory", "prep-log.json"), "w") as fh:
            json.dump({"version": 1, "sessions": sessions}, fh)


# ----------------------------------------------------------------------------- usage


class UsageTests(NlmTestCase):
    def test_missing_command(self):
        self.assertError(self.run_cli(), "USAGE")
        self.assertError(self.run_cli("bogus"), "USAGE")
        self.assertEqual(self.fake.opened, 0)

    def test_bad_course_and_date(self):
        self.assertError(self.run_cli("prep", "../x", DATE, self.pdf1), "USAGE")
        self.assertError(self.run_cli("prep", COURSE, "29-09-2026", self.pdf1), "USAGE")
        self.assertError(self.run_cli("prep", COURSE, "2026-02-30", self.pdf1), "USAGE")
        self.assertEqual(self.fake.opened, 0)

    def test_source_outside_readings_is_refused_before_any_call(self):
        outside = os.path.join(self.data, "elsewhere.pdf")
        with open(outside, "wb") as fh:
            fh.write(b"%PDF")
        err = self.assertError(self.run_cli("prep", COURSE, DATE, outside), "USAGE")
        self.assertIn("readings", err["message"])
        # a symlink inside readings/ that points outside is refused too
        link = os.path.join(self.readings, "sneaky.pdf")
        os.symlink(outside, link)
        self.assertError(self.run_cli("prep", COURSE, DATE, link), "USAGE")
        self.assertEqual(self.fake.opened, 0)

    def test_missing_or_empty_source(self):
        self.assertError(self.run_cli("prep", COURSE, DATE, os.path.join(self.readings, "nope.pdf")), "USAGE")
        empty = self._pdf("empty.pdf", b"")
        self.assertError(self.run_cli("prep", COURSE, DATE, empty), "USAGE")
        self.assertEqual(self.fake.opened, 0)

    def test_status_argument_checks(self):
        self.assertError(self.run_cli("status", "bad id!"), "USAGE")
        self.assertError(self.run_cli("status", "nb-1", "--course", COURSE), "USAGE")
        self.assertEqual(self.fake.opened, 0)

    def test_source_wait_must_be_numeric(self):
        env = dict(self.env, NLM_SOURCE_WAIT="soon")
        self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1, env=env), "USAGE")

    def test_symlink_names_imply_the_command(self):
        self.assertEqual(nlm.implied_argv("nlm-prep", ["-v", COURSE, DATE, "a.pdf"]), ["-v", "prep", COURSE, DATE, "a.pdf"])
        self.assertEqual(nlm.implied_argv("nlm-status", ["nb-1"]), ["status", "nb-1"])
        self.assertEqual(nlm.implied_argv("nlm-status", ["status", "nb-1"]), ["status", "nb-1"])
        self.assertEqual(nlm.implied_argv("nlm.py", ["status", "nb-1"]), ["status", "nb-1"])
        self.fake.seed("nb-9", sources=[("week4", "ready")], artifacts=[("in_progress", None)])
        body = self.assertOk(self.run_cli("nb-9", prog="nlm-status"))
        self.assertEqual(body["status"], "pending")


# ----------------------------------------------------------------------------- auth / install


class OpenClientTests(NlmTestCase):
    def setUp(self):
        super().setUp()
        nlm.open_client = nlm._open_client     # the real one

    def test_missing_cookie_file_is_nlm_auth(self):
        os.remove(self.cookies)
        err = self.assertError(self.run_cli("check"), "NLM_AUTH")
        self.assertIn(self.cookies, err["message"])
        self.assertIn("notebooklm login", err["message"])
        self.assertFalse(err["retryable"])

    def test_default_cookie_path_is_under_data_dir(self):
        env = {"DATA_DIR": self.data}
        os.remove(self.cookies)
        err = self.assertError(self.run_cli("check", env=env), "NLM_AUTH")
        self.assertIn(os.path.join(self.data, "secrets", "notebooklm-cookies.json"), err["message"])

    @unittest.skipIf(__import__("importlib").util.find_spec("notebooklm") is not None, "notebooklm-py is installed")
    def test_library_missing_is_nlm_not_installed(self):
        err = self.assertError(self.run_cli("check"), "NLM_NOT_INSTALLED")
        self.assertIn("pip install notebooklm-py", err["message"])
        self.assertFalse(err["retryable"])


# ----------------------------------------------------------------------------- prep


class PrepTests(NlmTestCase):
    def test_creates_notebook_adds_sources_starts_audio(self):
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1, self.pdf2))
        self.assertEqual(body["notebook_id"], "nb-1")
        self.assertEqual(body["notebook_title"], TITLE)
        self.assertTrue(body["created"])
        self.assertEqual((body["sources_added"], body["sources_reused"], body["sources_rejected"]), (2, 0, []))
        self.assertEqual(body["audio"], "started")
        self.assertEqual(body["task_id"], "task-art-1")
        self.assertEqual(body["artifact_id"], "art-1")
        self.assertEqual(self.fake.ops("notebooks.create"), [("notebooks.create", TITLE)])
        adds = self.fake.ops("sources.add_file")
        self.assertEqual([a[2] for a in adds], [self.pdf1, self.pdf2])
        self.assertTrue(all(a[3] == {"wait": False} for a in adds), "prep must not block on processing")
        gen = self.fake.ops("artifacts.generate_audio")
        self.assertEqual(len(gen), 1)
        self.assertEqual(gen[0][2]["prompt"], nlm.DEFAULT_AUDIO_PROMPT)
        self.assertEqual(gen[0][2]["title"], TITLE)
        self.assertEqual(self.slept, [], "sources were ready on the first poll")

    def test_waits_for_sources_then_starts(self):
        self.fake.ready_after_lists = 1
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1))
        self.assertEqual(body["audio"], "started")
        self.assertEqual(self.slept, [nlm.SOURCE_POLL_INTERVAL])

    def test_lagging_source_list_is_treated_as_pending(self):
        self.fake.list_lag = 1
        self.fake.ready_after_lists = 1
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1, self.pdf2))
        self.assertEqual(body["audio"], "started")
        self.assertEqual(self.slept, [nlm.SOURCE_POLL_INTERVAL])

    def test_source_list_never_catching_up_defers(self):
        self.fake.list_lag = 100
        env = dict(self.env, NLM_SOURCE_WAIT="2")
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1, self.pdf2, env=env))
        self.assertEqual((body["audio"], body["sources_pending"]), ("deferred", 2))
        self.assertEqual(self.fake.ops("artifacts.generate_audio"), [])

    def test_defers_audio_when_sources_still_processing(self):
        self.fake.ready_after_lists = 100
        env = dict(self.env, NLM_SOURCE_WAIT="5")
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1, env=env))
        self.assertEqual(body["audio"], "deferred")
        self.assertEqual(body["sources_pending"], 1)
        self.assertEqual(body["notebook_id"], "nb-1")
        self.assertEqual(self.slept, [2, 2, 1])
        self.assertEqual(self.fake.ops("artifacts.generate_audio"), [])

    def test_reuses_notebook_and_existing_sources(self):
        nid = self.fake.seed("nb-9", sources=[("week4", "ready")])
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1, self.pdf2))
        self.assertEqual(body["notebook_id"], nid)
        self.assertFalse(body["created"])
        self.assertEqual((body["sources_added"], body["sources_reused"]), (1, 1))
        self.assertEqual([a[2] for a in self.fake.ops("sources.add_file")], [self.pdf2])
        self.assertEqual(self.fake.ops("notebooks.create"), [])
        self.assertEqual(body["audio"], "started")

    def test_never_starts_a_second_podcast_when_one_is_in_flight(self):
        self.fake.seed("nb-9", sources=[("week4", "ready")], artifacts=[("in_progress", None)])
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1))
        self.assertEqual(body["audio"], "already-started")
        self.assertEqual(body["artifact_id"], "a0")
        self.assertEqual(self.fake.ops("artifacts.generate_audio"), [])

    def test_restarts_audio_after_a_failed_one(self):
        self.fake.seed("nb-9", sources=[("week4", "ready")], artifacts=[("failed", None)])
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1))
        self.assertEqual(body["audio"], "started")
        self.assertEqual(len(self.fake.ops("artifacts.generate_audio")), 1)

    def test_prep_log_guard_refuses_a_second_podcast_without_opening_the_client(self):
        self.write_prep_log({"%s@%s" % (COURSE, DATE): {"notebook_id": "nb-recorded", "status": "podcast-pending"}})
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1))
        self.assertEqual(body["notebook_id"], "nb-recorded")
        self.assertEqual(body["audio"], "already-started")
        self.assertTrue(body["skipped"])
        self.assertEqual(self.fake.opened, 0)

    def test_prep_log_without_notebook_id_does_not_block(self):
        self.write_prep_log({"%s@%s" % (COURSE, DATE): {"status": "pending"},
                             "%s@2026-10-06" % COURSE: {"notebook_id": "other"}})
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1))
        self.assertEqual(body["notebook_id"], "nb-1")
        self.assertEqual(self.fake.opened, 1)

    def test_rejected_source_is_reported_and_the_rest_continue(self):
        self.fake.reject_files = {"Case Study.pdf"}
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1, self.pdf2))
        self.assertEqual(body["sources_added"], 1)
        self.assertEqual(len(body["sources_rejected"]), 1)
        self.assertEqual(body["sources_rejected"][0]["path"], self.pdf2)
        self.assertEqual(body["sources_rejected"][0]["code"], "NLM_SOURCE_REJECTED")
        self.assertEqual(body["audio"], "started")

    def test_all_sources_rejected_is_an_error(self):
        self.fake.reject_files = {"week4.pdf"}
        err = self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1), "NLM_SOURCE_REJECTED")
        self.assertFalse(err["retryable"])
        self.assertEqual(len(err["detail"]["rejected"]), 1)
        self.assertEqual(self.fake.ops("artifacts.generate_audio"), [])

    def test_every_source_failing_to_process_is_source_rejected(self):
        self.fake.new_source_status = "error"
        self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1), "NLM_SOURCE_REJECTED")
        self.assertEqual(self.fake.ops("artifacts.generate_audio"), [])

    def test_auth_error_maps_to_nlm_auth_retryable_once(self):
        self.fake.fail["notebooks.list"] = AuthError("401 for /rpc")
        err = self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1), "NLM_AUTH")
        self.assertTrue(err["retryable"])
        self.assertIn("notebooklm login", err["message"])

    def test_rate_limit_maps(self):
        self.fake.fail["notebooks.create"] = RateLimitError("429")
        err = self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1), "NLM_RATE_LIMIT")
        self.assertTrue(err["retryable"])

    def test_network_and_server_errors_are_unavailable_and_retryable(self):
        self.fake.fail["notebooks.list"] = NetworkError("DNS failed")
        self.assertTrue(self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1), "NLM_UNAVAILABLE")["retryable"])
        self.fake.fail["notebooks.list"] = ServerError("500")
        self.assertTrue(self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1), "NLM_UNAVAILABLE")["retryable"])

    def test_unknown_library_error_is_unavailable_not_retryable(self):
        self.fake.fail["notebooks.list"] = NotebookLMError("something new")
        err = self.assertError(self.run_cli("prep", COURSE, DATE, self.pdf1), "NLM_UNAVAILABLE")
        self.assertFalse(err["retryable"])
        self.assertIn("NotebookLMError", err["message"])

    def test_custom_audio_prompt(self):
        env = dict(self.env, NLM_AUDIO_PROMPT="  keep it short  ")
        self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1, env=env))
        self.assertEqual(self.fake.ops("artifacts.generate_audio")[0][2]["prompt"], "keep it short")

    def test_older_library_without_prompt_keyword_still_works(self):
        self.fake.generate_kwargs_supported = {"title"}
        body = self.assertOk(self.run_cli("prep", COURSE, DATE, self.pdf1))
        self.assertEqual(body["audio"], "started")
        calls = self.fake.ops("artifacts.generate_audio")
        self.assertEqual(calls[-1][2], {"title": TITLE})


# ----------------------------------------------------------------------------- status


class StatusTests(NlmTestCase):
    def test_unknown_notebook_is_nlm_not_found(self):
        err = self.assertError(self.run_cli("status", "nb-404", "--course", COURSE, "--date", DATE), "NLM_NOT_FOUND")
        self.assertFalse(err["retryable"])

    def test_no_audio_yet_and_sources_ready_starts_it(self):
        nid = self.fake.seed(sources=[("week4", "ready")])
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["audio"], "started")
        self.assertEqual(body["artifact_id"], "art-1")
        self.assertEqual(len(self.fake.ops("artifacts.generate_audio")), 1)

    def test_no_audio_yet_and_sources_processing_waits(self):
        nid = self.fake.seed(sources=[("week4", "processing")])
        self.fake.ready_after_lists = 100
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual((body["status"], body["audio"], body["sources_pending"]), ("pending", "waiting-for-sources", 1))
        self.assertEqual(self.fake.ops("artifacts.generate_audio"), [])

    def test_no_sources_is_failed(self):
        nid = self.fake.seed()
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual(body["status"], "failed")
        self.assertIn("no sources", body["reason"])

    def test_in_progress_is_pending(self):
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("in_progress", None)])
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual((body["status"], body["artifact_status"], body["artifact_id"]), ("pending", "in_progress", "a0"))
        self.assertEqual(self.fake.ops("artifacts.download_audio"), [])

    def test_failed_generation_is_failed_with_reason(self):
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("failed", None)])
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual(body["status"], "failed")
        self.assertIn("failed", body["reason"])
        self.assertEqual(self.fake.ops("artifacts.generate_audio"), [], "status never restarts a failed podcast")

    def test_completed_downloads_and_pushes_to_drive(self):
        nid = self.fake.seed(sources=[("week4", "ready")],
                             artifacts=[("failed", None), ("completed", "https://notebooklm.google.com/audio/xyz")])
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["artifact_id"], "a1", "the ready artifact wins over the failed one")
        self.assertEqual(body["audio_url"], "https://notebooklm.google.com/audio/xyz")
        expected = os.path.join(self.data, "podcasts", COURSE, DATE + ".m4a")
        self.assertEqual(body["local_path"], expected)
        self.assertTrue(body["downloaded"])
        self.assertEqual(body["bytes"], len(M4A))
        self.assertEqual(body["drive_link"], self.drive_result["share_link"])
        self.assertEqual(body["remote_path"], self.drive_result["remote_path"])
        with open(expected, "rb") as fh:
            self.assertEqual(fh.read(), M4A)
        self.assertFalse(os.path.exists(os.path.join(self.data, "podcasts", COURSE, DATE + ".part")))
        dl = self.fake.ops("artifacts.download_audio")
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0][3], {"artifact_id": "a1"})
        self.assertEqual(self.drive_calls, [(expected, "%s/%s" % (COURSE, DATE))])

    def test_second_status_run_is_idempotent(self):
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("completed", None)])
        self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual(body["status"], "ready")
        self.assertFalse(body["downloaded"])
        self.assertEqual(len(self.fake.ops("artifacts.download_audio")), 1, "no second download")
        self.assertEqual(len(self.drive_calls), 2, "drive-put is idempotent and re-run for the link")

    def test_mp3_container_gets_mp3_extension(self):
        self.fake.audio_bytes = MP3
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("completed", None)])
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertTrue(body["local_path"].endswith(os.path.join(COURSE, DATE + ".mp3")))

    def test_course_and_date_come_from_the_notebook_title(self):
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("completed", None)])
        body = self.assertOk(self.run_cli("status", nid))
        self.assertEqual((body["course"], body["date"]), (COURSE, DATE))
        self.assertEqual(self.fake.ops("notebooks.get"), [("notebooks.get", nid)])
        self.assertTrue(body["local_path"].endswith(os.path.join("podcasts", COURSE, DATE + ".m4a")))

    def test_unparseable_title_needs_flags(self):
        nid = self.fake.seed(title="My research", sources=[("week4", "ready")])
        err = self.assertError(self.run_cli("status", nid), "USAGE")
        self.assertIn("--course", err["message"])

    def test_drive_failure_keeps_ready_but_no_link(self):
        self.drive_result = {"ok": False, "error": {"code": "DRIVE_AUTH", "message": "token expired", "retryable": False}}
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("completed", None)])
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE))
        self.assertEqual(body["status"], "ready")
        self.assertIsNone(body["drive_link"])
        self.assertEqual(body["drive_error"]["code"], "DRIVE_AUTH")
        self.assertFalse(body["drive_error"]["retryable"])
        self.assertTrue(os.path.isfile(body["local_path"]), "the download is kept for the next poll")

    def test_empty_download_is_unavailable(self):
        self.fake.audio_bytes = b""
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("completed", None)])
        err = self.assertError(self.run_cli("status", nid, "--course", COURSE, "--date", DATE), "NLM_UNAVAILABLE")
        self.assertTrue(err["retryable"])
        self.assertEqual(self.drive_calls, [])

    def test_auth_error_on_status(self):
        self.fake.fail["artifacts.list"] = AuthError("cookies expired")
        nid = self.fake.seed(sources=[("week4", "ready")])
        self.assertError(self.run_cli("status", nid, "--course", COURSE, "--date", DATE), "NLM_AUTH")


class DrivePutSubprocessTests(NlmTestCase):
    """The real drive_put hook: runs the drive skill's script and parses its envelope."""

    def setUp(self):
        super().setUp()
        nlm.drive_put = nlm._drive_put
        self.audio = os.path.join(self.data, "podcasts", COURSE, DATE + ".m4a")
        os.makedirs(os.path.dirname(self.audio))
        with open(self.audio, "wb") as fh:
            fh.write(M4A)

    def _script(self, body):
        path = os.path.join(self.data, "drive-put")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_missing_script_is_reported_not_fatal(self):
        cfg = nlm.Config(dict(self.env, NLM_DRIVE_PUT=os.path.join(self.data, "nope")))
        res = nlm._drive_put(cfg, self.audio, "x/y")
        self.assertEqual(res["error"]["code"], "DRIVE_NOT_INSTALLED")

    def test_default_location_is_the_drive_skill(self):
        cfg = nlm.Config(self.env)
        self.assertEqual(os.path.normpath(cfg.drive_put),
                         os.path.normpath(os.path.join(REPO, "workspace", "skills", "drive", "scripts", "drive-put")))

    def test_parses_the_last_json_line(self):
        script = self._script('echo "progress 50%"\nprintf \'{"ok": true, "remote_path": "Readings/%s", "share_link": "https://d/x", "uploaded": true}\\n\' "$2"')
        cfg = nlm.Config(dict(self.env, NLM_DRIVE_PUT=script))
        res = nlm._drive_put(cfg, self.audio, "%s/%s" % (COURSE, DATE))
        self.assertEqual(res, {"ok": True, "remote_path": "Readings/%s/%s" % (COURSE, DATE),
                               "share_link": "https://d/x", "uploaded": True})

    def test_garbage_output_is_unavailable(self):
        script = self._script("echo not json; exit 3")
        cfg = nlm.Config(dict(self.env, NLM_DRIVE_PUT=script))
        res = nlm._drive_put(cfg, self.audio, "x/y")
        self.assertEqual(res["error"]["code"], "DRIVE_UNAVAILABLE")
        self.assertTrue(res["error"]["retryable"])
        self.assertIn("exit 3", res["error"]["message"])

    def test_status_end_to_end_through_the_script(self):
        script = self._script('printf \'{"ok": true, "remote_path": "Readings/%s/%s", "share_link": "https://d/e2e", "uploaded": false}\\n\' "%s" "%s"' % (COURSE, DATE, COURSE, DATE))
        nid = self.fake.seed(sources=[("week4", "ready")], artifacts=[("completed", None)])
        env = dict(self.env, NLM_DRIVE_PUT=script)
        body = self.assertOk(self.run_cli("status", nid, "--course", COURSE, "--date", DATE, env=env))
        self.assertEqual(body["drive_link"], "https://d/e2e")
        self.assertFalse(body["downloaded"])


# ----------------------------------------------------------------------------- secrets / crashes / timeouts


class SafetyTests(NlmTestCase):
    def test_cookie_values_never_reach_stdout_or_stderr(self):
        self.fake.fail["notebooks.create"] = AuthError("rejected cookie SID=" + COOKIE)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            rc = nlm.main(["-v", "prep", COURSE, DATE, self.pdf1], env=self.env, out=out, prog="nlm")
        self.assertEqual(rc, 2)
        self.assertNotIn(COOKIE, out.getvalue())
        self.assertNotIn(COOKIE, err.getvalue())
        self.assertIn("nlm: ", err.getvalue(), "verbose log lines are written")

    def test_library_stderr_noise_is_scrubbed_too(self):
        async def noisy_list():
            sys.stderr.write("httpx: Cookie: SID=%s\n" % COOKIE)
            return []
        self.fake.notebooks.list = noisy_list
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            nlm.main(["check"], env=self.env, out=out)
        self.assertNotIn(COOKIE, err.getvalue())
        self.assertIn("<redacted>", err.getvalue())

    def test_library_bug_is_a_tool_error_not_a_crash(self):
        self.fake.fail["notebooks.list"] = RuntimeError("leaked " + COOKIE)
        err = self.assertError(self.run_cli("check"), "NLM_UNAVAILABLE")
        self.assertFalse(err["retryable"])
        self.assertIn("RuntimeError", err["message"])
        self.assertIn("<redacted>", err["message"])

    def test_crash_is_exit_1_internal_and_redacted(self):
        def broken(cfg):
            raise RuntimeError("leaked " + COOKIE)
        nlm.open_client = broken
        out = io.StringIO()
        rc = nlm.main(["check"], env=self.env, out=out)
        body = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(body["error"]["code"], "INTERNAL")
        self.assertIn("<redacted>", body["error"]["message"])
        self.assertNotIn(COOKIE, out.getvalue())

    def test_slow_notebooklm_is_a_retryable_timeout_not_a_hang(self):
        async def slow_list():
            await asyncio.sleep(0.5)
            return []
        self.fake.notebooks.list = slow_list
        orig = nlm.COMMAND_DEADLINE
        nlm.COMMAND_DEADLINE = 0.05
        try:
            err = self.assertError(self.run_cli("check"), "NLM_UNAVAILABLE")
        finally:
            nlm.COMMAND_DEADLINE = orig
        self.assertTrue(err["retryable"])
        self.assertIn("did not answer", err["message"])

    def test_check_lists_notebooks(self):
        self.fake.seed()
        body = self.assertOk(self.run_cli("check"))
        self.assertEqual(body["notebooks"], 1)
        self.assertEqual(body["cookies_path"], self.cookies)


# ----------------------------------------------------------------------------- units


class ClassifyTests(unittest.TestCase):
    def test_mapping_follows_the_mro(self):
        self.assertEqual(nlm.classify(AuthError("x")), ("NLM_AUTH", True))
        self.assertEqual(nlm.classify(RateLimitError("x")), ("NLM_RATE_LIMIT", True))
        self.assertEqual(nlm.classify(NotebookNotFoundError("x")), ("NLM_NOT_FOUND", False))
        self.assertEqual(nlm.classify(SourceAddError("x")), ("NLM_SOURCE_REJECTED", False))
        self.assertEqual(nlm.classify(NetworkError("x")), ("NLM_UNAVAILABLE", True))
        self.assertEqual(nlm.classify(RPCError("x")), ("NLM_UNAVAILABLE", True))
        self.assertEqual(nlm.classify(NotebookLMError("x")), ("NLM_UNAVAILABLE", False))
        self.assertEqual(nlm.classify(ConnectionResetError("x")), ("NLM_UNAVAILABLE", True))
        self.assertEqual(nlm.classify(ValueError("x")), ("NLM_UNAVAILABLE", False))

        class MissingDependencyError(Exception):
            pass

        class HeadlessLoginRequiredError(Exception):
            pass

        self.assertEqual(nlm.classify(MissingDependencyError("x")), ("NLM_NOT_INSTALLED", False))
        self.assertEqual(nlm.classify(HeadlessLoginRequiredError("x")), ("NLM_AUTH", True))

    def test_notebook_title_round_trip(self):
        self.assertEqual(nlm.notebook_title(COURSE, DATE), TITLE)
        self.assertEqual(nlm.parse_notebook_title(TITLE), (COURSE, DATE))
        self.assertEqual(nlm.parse_notebook_title("MAS.665 - 2026-09-29"), (COURSE, DATE))
        self.assertEqual(nlm.parse_notebook_title("  MAS.665 – 2026-09-29 "), (COURSE, DATE))
        self.assertIsNone(nlm.parse_notebook_title("MAS.665 2026-09-29"))
        self.assertIsNone(nlm.parse_notebook_title("../x — 2026-09-29"))
        self.assertIsNone(nlm.parse_notebook_title(None))

    def test_state_normalisation(self):
        self.assertEqual(nlm.artifact_state(Obj(status="completed")), "ready")
        self.assertEqual(nlm.artifact_state(Obj(status=Obj(value="in_progress"))), "pending")
        self.assertEqual(nlm.artifact_state(Obj(status=Obj(name="FAILED", value="failed"))), "failed")
        self.assertEqual(nlm.artifact_state(Obj(status=None)), "pending")
        self.assertEqual(nlm.source_state(Obj(status="READY")), "ready")
        self.assertEqual(nlm.source_state(Obj(status="error")), "error")
        self.assertEqual(nlm.source_state(Obj(status="preparing")), "pending")
        self.assertTrue(nlm.is_audio(Obj(artifact_type=ArtifactType("AUDIO"))))
        self.assertTrue(nlm.is_audio(Obj(type="audio_overview")))
        self.assertFalse(nlm.is_audio(Obj(artifact_type="quiz")))

    def test_sniff_audio_ext(self):
        with tempfile.TemporaryDirectory() as d:
            for name, blob, ext in (("a", M4A, ".m4a"), ("b", MP3, ".mp3"), ("c", b"\xff\xfb\x90\x00" * 4, ".mp3"),
                                    ("d", b"RIFF....WAVE", ".wav"), ("e", b"<html>", ".bin")):
                p = os.path.join(d, name)
                with open(p, "wb") as fh:
                    fh.write(blob)
                self.assertEqual(nlm.sniff_audio_ext(p), ext, name)

    def test_load_cookie_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w") as fh:
                json.dump({"cookies": [{"name": "a", "value": COOKIE}, {"name": "b", "value": "short"}],
                           "origins": [{"localStorage": [{"name": "t", "value": "local-storage-token-xyz"}]}],
                           "master_token": "aas_et/master-token-value"}, fh)
            self.assertEqual(nlm.load_cookie_values(p), {COOKIE, "local-storage-token-xyz", "aas_et/master-token-value"})
            self.assertEqual(nlm.load_cookie_values(os.path.join(d, "missing.json")), set())


class CliProcessTests(unittest.TestCase):
    """Real subprocesses through the symlinks: shebang, implied command, exit code, single-line JSON."""

    def _run(self, name, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NLM_")}
        env["DATA_DIR"] = tempfile.gettempdir()
        proc = subprocess.run([os.path.join(SCRIPTS, name)] + list(args), env=env, capture_output=True, text=True)
        self.assertEqual(proc.stdout.count("\n"), 1, proc)
        return proc.returncode, json.loads(proc.stdout)

    def test_nlm_prep_symlink_reports_usage(self):
        rc, body = self._run("nlm-prep")
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"]["code"], "USAGE")
        self.assertIn("course", body["error"]["message"])

    def test_nlm_status_symlink_reports_usage(self):
        rc, body = self._run("nlm-status", "not a valid id")
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"]["code"], "USAGE")

    def test_source_outside_readings_through_symlink(self):
        rc, body = self._run("nlm-prep", COURSE, DATE, "/etc/hosts")
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"]["code"], "USAGE")


if __name__ == "__main__":
    unittest.main()
