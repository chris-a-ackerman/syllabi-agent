"""Tests for workspace/skills/preplog/scripts/preplog.py (SYL-100, memory half): the memory tool.

Everything runs against a temp DATA_DIR. No network, no Maritime. The clock is fixed by replacing
the module's `now_utc` hook (and `read_stdin` for `-` inputs).

    python3 -m unittest discover -s tests -v
"""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "workspace", "skills", "preplog", "scripts")
TEMPLATES = os.path.join(REPO, "workspace", "memory-templates")
sys.path.insert(0, SCRIPTS)

import preplog  # noqa: E402

NOW = "2026-09-27T19:00:00-04:00"        # Sunday 19:00 ET: the prep job's slot
KEY = "MAS.665@2026-09-29"
CLASS_START = "2026-09-29T13:00:00-04:00"

try:
    import zoneinfo
    zoneinfo.ZoneInfo("America/New_York")
    HAVE_TZDATA = True
except Exception:   # pragma: no cover - depends on the machine
    HAVE_TZDATA = False

try:
    import jsonschema   # noqa: F401
    HAVE_JSONSCHEMA = True
except ImportError:     # pragma: no cover
    HAVE_JSONSCHEMA = False


def brief(**over):
    b = {
        "topic": "Agents that plan",
        "why_it_matters": "It frames the rest of the course.",
        "key_arguments": ["Planning beats reacting (Paper 1)", "Memory is state (Paper 2)"],
        "prep_checklist": ["Read §2 of Paper 1", "Skim Paper 2"],
        "pre_class_questions": [
            {"question": "What is an agent?", "source": "Pre-class questions 4", "draft_answer": "A system that acts."},
        ],
    }
    b.update(over)
    return b


class PrepLogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="preplog-test-")
        self.env = {"DATA_DIR": self.tmp}
        self._orig = (preplog.now_utc, preplog.read_stdin, preplog._EASTERN)
        self.set_now(NOW)

    def tearDown(self):
        preplog.now_utc, preplog.read_stdin, preplog._EASTERN = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers

    def set_now(self, iso):
        instant = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        preplog.now_utc = lambda: instant

    def run_cli(self, *argv, env=None):
        out = io.StringIO()
        code = preplog.main(list(argv), env=self.env if env is None else env, out=out)
        raw = out.getvalue()
        self.assertEqual(raw.count("\n"), 1, "exactly one JSON line expected, got: %r" % raw)
        return code, json.loads(raw)

    def ok(self, *argv, **kw):
        code, body = self.run_cli(*argv, **kw)
        self.assertEqual(code, 0, body)
        self.assertTrue(body["ok"])
        return body

    def assertError(self, result, code):
        rc, body = result
        self.assertEqual(rc, 2, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], code, body)
        self.assertIn("retryable", body["error"])
        return body["error"]

    @property
    def prep_log(self):
        return os.path.join(self.tmp, "memory", "prep-log.json")

    @property
    def notes(self):
        return os.path.join(self.tmp, "memory", "course-notes.md")

    def doc(self):
        with open(self.prep_log, encoding="utf-8") as fh:
            return json.load(fh)

    def raw(self):
        return self.read_bytes(self.prep_log)

    @staticmethod
    def read_bytes(path):
        with open(path, "rb") as fh:
            return fh.read()

    def write_doc(self, doc):
        os.makedirs(os.path.dirname(self.prep_log), exist_ok=True)
        with open(self.prep_log, "w", encoding="utf-8") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))

    def session(self, key=KEY):
        return self.doc()["sessions"][key]

    def make(self, key=KEY, *extra, due=False, class_start=None):
        if class_start is None:   # 13:00 ET on the key's date (upsert refuses a start on another day)
            class_start = "%sT13:00:00-04:00" % key.split("@", 1)[1]
        argv = ["--trigger", "prep", "upsert", key, "--class-start", class_start, "--canvas-course-id", "40577"]
        if due:
            argv += ["--has-due-before-class", "true"]
        return self.ok(*(argv + list(extra)))

    def write_json(self, name, obj):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path


# --------------------------------------------------------------------------- init / validate


class InitTests(PrepLogTestCase):
    def test_init_creates_prep_log_notes_and_logs_dir(self):
        body = self.ok("init")
        self.assertTrue(body["created_prep_log"])
        self.assertTrue(body["created_course_notes"])
        doc = self.doc()
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["sessions"], {})
        self.assertEqual(doc["updated_at"], NOW)
        with open(os.path.join(TEMPLATES, "course-notes.md"), encoding="utf-8") as fh:
            template = fh.read()
        with open(self.notes, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), template)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "logs")))
        self.assertTrue(self.raw().endswith(b"\n"))

    def test_init_never_overwrites(self):
        self.ok("init")
        self.make()
        with open(self.notes, "a", encoding="utf-8") as fh:
            fh.write("\n## MAS.665: AI Studio\n\n- **Canvas course id:** 40577\n")
        before_log, before_notes = self.raw(), self.read_bytes(self.notes)
        body = self.ok("init")
        self.assertFalse(body["created_prep_log"])
        self.assertFalse(body["created_course_notes"])
        self.assertEqual(self.raw(), before_log)
        self.assertEqual(self.read_bytes(self.notes), before_notes)

    def test_reads_treat_a_missing_file_as_empty_and_do_not_create_it(self):
        body = self.ok("list")
        self.assertEqual(body["count"], 0)
        self.assertFalse(os.path.exists(self.prep_log))
        body = self.ok("due")
        self.assertTrue(body["nothing_to_do"])
        self.assertFalse(os.path.exists(self.prep_log))

    def test_data_dir_must_be_absolute(self):
        self.assertError(self.run_cli("init", env={"DATA_DIR": "relative/dir"}), "USAGE")


class ValidateTests(PrepLogTestCase):
    def test_valid_file(self):
        self.make()
        body = self.ok("validate")
        self.assertEqual(body["sessions"], 1)

    def test_corrupt_json_is_refused_and_never_overwritten(self):
        self.write_doc("{not json")
        err = self.assertError(self.run_cli("validate"), "PREPLOG_CORRUPT")
        self.assertIn(self.prep_log, err["message"])
        self.assertError(self.run_cli("upsert", KEY, "--class-start", CLASS_START), "PREPLOG_CORRUPT")
        self.assertEqual(self.raw(), b"{not json")

    def test_wrong_version_is_corrupt(self):
        self.write_doc({"version": 2, "sessions": {}})
        self.assertError(self.run_cli("list"), "PREPLOG_CORRUPT")
        self.write_doc({"version": 1, "sessions": []})
        self.assertError(self.run_cli("list"), "PREPLOG_CORRUPT")

    def test_hand_edited_bad_record_is_reported_with_its_path(self):
        self.make()
        doc = self.doc()
        doc["sessions"][KEY]["status"] = "sleeping"
        doc["sessions"][KEY]["bogus"] = 1
        self.write_doc(doc)
        err = self.assertError(self.run_cli("validate"), "PREPLOG_INVALID")
        joined = "\n".join(err["detail"]["errors"])
        self.assertIn("sessions.%s.status" % KEY, joined)
        self.assertIn("bogus", joined)
        # Reads still work on a bad record; only writes to it are blocked.
        self.assertEqual(self.ok("get", KEY)["session"]["status"], "sleeping")
        self.assertError(self.run_cli("log", KEY, "--action", "x"), "PREPLOG_INVALID")


# --------------------------------------------------------------------------- upsert


class UpsertTests(PrepLogTestCase):
    def test_creates_a_pending_record_with_defaults(self):
        body = self.make()
        self.assertTrue(body["created"])
        s = body["session"]
        self.assertEqual(s["course"], "MAS.665")
        self.assertEqual(s["class_date"], "2026-09-29")
        self.assertEqual(s["class_start"], CLASS_START)
        self.assertEqual(s["canvas_course_id"], 40577)
        self.assertEqual(s["status"], "pending")
        self.assertEqual(s["attempts"], 0)
        self.assertEqual(s["readings"], [])
        self.assertEqual(s["drive_paths"], [])
        self.assertEqual([h["action"] for h in s["history"]], ["record created"])
        self.assertEqual(s["history"][0]["trigger"], "prep")
        self.assertEqual(s["history"][0]["ts"], NOW)
        self.assertEqual(body["plan"]["steps"], ["find_readings", "podcast", "brief"])
        self.assertEqual(self.doc()["updated_at"], NOW)
        self.assertEqual(self.ok("validate")["sessions"], 1)

    def test_notify_at_defaults_to_0630_on_class_day(self):
        self.assertEqual(self.make()["session"]["notify_at"], "2026-09-29T06:30:00-04:00")

    def test_notify_at_is_class_start_minus_24h_when_something_is_due(self):
        self.assertEqual(self.make(due=True)["session"]["notify_at"], "2026-09-28T13:00:00-04:00")

    def test_notify_at_in_the_past_becomes_now(self):
        body = self.make("MAS.665@2026-09-27", class_start="2026-09-27T20:00:00-04:00")
        self.assertEqual(body["session"]["notify_at"], NOW)

    def test_explicit_notify_at_is_normalized_to_eastern(self):
        body = self.make(KEY, "--notify-at", "2026-09-28T10:30:00Z")
        self.assertEqual(body["session"]["notify_at"], "2026-09-28T06:30:00-04:00")

    def test_has_due_needs_class_start(self):
        self.assertError(self.run_cli("upsert", KEY, "--has-due-before-class", "true"), "USAGE")
        self.assertFalse(os.path.exists(self.prep_log))

    def test_without_class_start_the_morning_default_still_works(self):
        body = self.ok("upsert", KEY)
        self.assertEqual(body["session"]["notify_at"], "2026-09-29T06:30:00-04:00")
        self.assertNotIn("class_start", body["session"])

    def test_bad_keys(self):
        for key in ("MAS.665", "MAS.665@2026-13-01", "MAS 665@2026-09-29", "@2026-09-29", "MAS@665@2026-09-29"):
            self.assertError(self.run_cli("upsert", key, "--class-start", CLASS_START), "USAGE")

    def test_course_and_date_must_match_the_key(self):
        self.assertError(self.run_cli("upsert", KEY, "--course", "15.286"), "USAGE")
        self.assertError(self.run_cli("upsert", KEY, "--class-date", "2026-09-30"), "USAGE")
        self.assertFalse(os.path.exists(self.prep_log))

    def test_from_json_merges_and_is_validated(self):
        path = self.write_json("patch.json", {"topic": "Planning", "canvas_course_id": "abc", "has_due_before_class": False})
        s = self.ok("upsert", KEY, "--class-start", CLASS_START, "--from-json", path)["session"]
        self.assertEqual(s["topic"], "Planning")
        self.assertEqual(s["canvas_course_id"], "abc")
        bad = self.write_json("bad.json", {"unknown_field": 1})
        before = self.raw()
        err = self.assertError(self.run_cli("upsert", KEY, "--from-json", bad), "PREPLOG_INVALID")
        self.assertIn("unknown_field", "\n".join(err["detail"]["errors"]))
        self.assertEqual(self.raw(), before)

    def test_from_json_protects_history_and_attempts(self):
        for patch in ({"history": []}, {"attempts": 0}):
            path = self.write_json("p.json", patch)
            self.assertError(self.run_cli("upsert", KEY, "--from-json", path), "USAGE")
        self.assertError(self.run_cli("upsert", KEY, "--from-json", self.write_json("l.json", [1])), "USAGE")
        with open(os.path.join(self.tmp, "notjson"), "w") as fh:
            fh.write("{")
        self.assertError(self.run_cli("upsert", KEY, "--from-json", os.path.join(self.tmp, "notjson")), "USAGE")

    def test_from_json_reads_stdin(self):
        preplog.read_stdin = lambda: json.dumps({"topic": "From stdin"})
        s = self.make(KEY, "--from-json", "-")["session"]
        self.assertEqual(s["topic"], "From stdin")

    def test_second_upsert_updates_facts_but_keeps_state(self):
        self.make()
        self.ok("begin", KEY)
        self.ok("add-reading", KEY, "--title", "P", "--source", "canvas_file", "--id-or-url", "1")
        body = self.make(KEY, "--topic", "New topic")
        self.assertFalse(body["created"])
        s = body["session"]
        self.assertEqual(s["topic"], "New topic")
        self.assertEqual(s["attempts"], 1)
        self.assertEqual(len(s["readings"]), 1)
        self.assertEqual([h["action"] for h in s["history"]], ["record created", "run started (attempt 1)"])

    def test_bool_parsing(self):
        self.assertTrue(self.make(KEY, "--has-due-before-class", "yes")["session"]["has_due_before_class"])
        self.assertError(self.run_cli("upsert", KEY, "--has-due-before-class", "maybe"), "USAGE")


# --------------------------------------------------------------------------- begin / plan


class BeginTests(PrepLogTestCase):
    def test_first_begin_counts_an_attempt_and_returns_the_plan(self):
        self.make()
        body = self.ok("--trigger", "prep", "begin", KEY)
        self.assertFalse(body["skip"])
        self.assertEqual(body["attempts"], 1)
        self.assertEqual(body["plan"], {"steps": ["find_readings", "podcast", "brief"], "recorded": []})
        s = self.session()
        self.assertEqual(s["attempts"], 1)
        self.assertEqual(s["history"][-1]["action"], "run started (attempt 1)")
        self.assertEqual(s["history"][-1]["trigger"], "prep")

    def test_skips_finished_or_waiting_records_without_counting(self):
        for status in ("podcast-pending", "ready", "done", "needs-human"):
            key = "C%s@2026-09-29" % status.replace("-", "")
            self.make(key)
            self.ok("set-status", key, status)
            body = self.ok("begin", key)
            self.assertTrue(body["skip"], body)
            self.assertEqual(body["reason"], "status:%s" % status)
            self.assertEqual(self.session(key)["attempts"], 0)

    def test_partial_is_retried(self):
        self.make()
        self.ok("set-status", KEY, "partial", "--error-code", "NLM_AUTH", "--step", "podcast")
        body = self.ok("begin", KEY)
        self.assertFalse(body["skip"])
        self.assertEqual(body["plan"]["podcast_retry_after"], "NLM_AUTH")
        self.assertEqual(body["last_error"]["code"], "NLM_AUTH")

    def test_third_attempt_is_the_last(self):
        self.make()
        for n in (1, 2, 3):
            self.assertEqual(self.ok("begin", KEY)["attempts"], n)
        body = self.ok("begin", KEY)
        self.assertTrue(body["skip"])
        self.assertEqual(body["reason"], "MAX_ATTEMPTS")
        self.assertTrue(body["notify_chris"])
        s = self.session()
        self.assertEqual(s["status"], "needs-human")
        self.assertEqual(s["last_error"]["code"], "MAX_ATTEMPTS")
        self.assertEqual(s["attempts"], 3)
        again = self.ok("begin", KEY)
        self.assertTrue(again["skip"])
        self.assertEqual(again["reason"], "status:needs-human")
        self.assertEqual(sum(1 for h in s["history"] if h["action"].startswith("max attempts")), 1)

    def test_max_attempts_on_a_record_reset_to_pending_asks_only_once(self):
        self.make()
        for _ in range(3):
            self.ok("begin", KEY)
        self.ok("begin", KEY)                       # -> needs-human, MAX_ATTEMPTS
        self.ok("set-status", KEY, "pending")       # Chris "replied" but attempts stay at 3
        body = self.ok("begin", KEY)
        self.assertEqual(body["reason"], "MAX_ATTEMPTS")
        self.assertTrue(body["notify_chris"])       # last_error was still MAX_ATTEMPTS but status wasn't needs-human
        self.assertEqual(self.session()["status"], "needs-human")

    def test_unknown_key(self):
        err = self.assertError(self.run_cli("begin", KEY), "NOT_FOUND")
        self.assertEqual(err["detail"]["known_keys"], [])


class PlanTests(PrepLogTestCase):
    def add(self, *extra, ident="1", title="Paper"):
        return self.ok("add-reading", KEY, "--title", title, "--source", "canvas_file", "--id-or-url", ident, *extra)

    def test_download_then_drive_then_recorded(self):
        self.make()
        plan = self.add()["plan"]
        self.assertEqual(plan["steps"], ["download", "podcast", "brief"])
        self.assertEqual(plan["recorded"], ["find_readings"])
        plan = self.add("--local-path", "/data/readings/MAS.665/2026-09-29/paper.pdf")["plan"]
        self.assertEqual(plan["steps"], ["drive", "podcast", "brief"])
        self.assertEqual(plan["recorded"], ["find_readings", "download"])
        plan = self.add("--drive-path", "Readings/MAS.665/2026-09-29/paper.pdf")["plan"]
        self.assertEqual(plan["steps"], ["podcast", "brief"])
        self.assertEqual(plan["recorded"], ["find_readings", "download", "drive"])

    def test_login_walled_readings_are_not_download_steps(self):
        self.make()
        plan = self.add("--requires-login", "true", ident="https://hbsp.harvard.edu/x")["plan"]
        self.assertNotIn("download", plan["steps"])
        self.assertEqual(plan["readings_waiting_on_chris"], 1)
        plan = self.add(ident="2", title="Public")["plan"]
        self.assertIn("download", plan["steps"])

    def test_podcast_and_brief_become_recorded(self):
        self.make()
        self.add("--local-path", "/data/readings/MAS.665/2026-09-29/p.pdf", "--drive-path", "Readings/MAS.665/2026-09-29/p.pdf")
        self.assertEqual(self.ok("set-notebook", KEY, "nb1")["status"], "podcast-pending")
        plan = self.ok("get", KEY)["plan"]
        self.assertEqual(plan["steps"], ["brief"])
        path = self.write_json("brief.json", brief())
        plan = self.ok("set-brief", KEY, "--from", path)["plan"]
        self.assertEqual(plan["steps"], [])
        self.assertEqual(plan["recorded"], ["find_readings", "download", "drive", "podcast", "brief"])

    def test_podcast_url_alone_counts_as_recorded(self):
        self.make()
        self.ok("set-podcast", KEY, "--url", "https://drive.google.com/x")
        self.assertNotIn("podcast", self.ok("get", KEY)["plan"]["steps"])


# --------------------------------------------------------------------------- readings / drive


class ReadingTests(PrepLogTestCase):
    def test_add_is_idempotent_on_source_and_id(self):
        self.make()
        first = self.ok("add-reading", KEY, "--title", "Paper", "--source", "canvas_file", "--id-or-url", "123")
        self.assertTrue(first["created"])
        second = self.ok("add-reading", KEY, "--title", "Paper (v2)", "--source", "canvas_file", "--id-or-url", "123",
                         "--local-path", "/data/readings/MAS.665/2026-09-29/paper.pdf")
        self.assertFalse(second["created"])
        self.assertEqual(second["readings"], 1)
        r = self.session()["readings"][0]
        self.assertEqual(r["title"], "Paper (v2)")
        self.assertEqual(r["local_path"], "/data/readings/MAS.665/2026-09-29/paper.pdf")
        other = self.ok("add-reading", KEY, "--title", "Same id, external", "--source", "external", "--id-or-url", "123")
        self.assertTrue(other["created"])
        self.assertEqual(other["readings"], 2)

    def test_drive_path_goes_into_drive_paths_once(self):
        self.make()
        for _ in range(2):
            self.ok("add-reading", KEY, "--title", "P", "--source", "canvas_file", "--id-or-url", "1",
                    "--drive-path", "Readings/MAS.665/2026-09-29/p.pdf")
        self.assertEqual(self.session()["drive_paths"], ["Readings/MAS.665/2026-09-29/p.pdf"])

    def test_local_path_outside_readings_is_refused(self):
        self.make()
        before = self.raw()
        err = self.assertError(self.run_cli("add-reading", KEY, "--title", "P", "--source", "canvas_file",
                                            "--id-or-url", "1", "--local-path", "/tmp/evil.pdf"), "PREPLOG_INVALID")
        self.assertIn("local_path", "\n".join(err["detail"]["errors"]))
        self.assertEqual(self.raw(), before)

    def test_flags_and_sources(self):
        self.make()
        self.assertError(self.run_cli("add-reading", KEY, "--title", "P", "--source", "dropbox", "--id-or-url", "1"), "USAGE")
        body = self.ok("add-reading", KEY, "--title", "P", "--source", "external", "--id-or-url", "https://x.y/p.pdf",
                       "--requires-login", "true", "--truncated-for-brief", "false")
        self.assertTrue(body["reading"]["requires_login"])
        self.assertFalse(body["reading"]["truncated_for_brief"])

    def test_add_drive_path(self):
        self.make()
        self.assertTrue(self.ok("add-drive-path", KEY, "https://drive.google.com/folder/abc")["added"])
        self.assertFalse(self.ok("add-drive-path", KEY, "https://drive.google.com/folder/abc")["added"])
        self.assertEqual(self.session()["drive_paths"], ["https://drive.google.com/folder/abc"])
        self.assertError(self.run_cli("add-drive-path", KEY, "  "), "USAGE")


# --------------------------------------------------------------------------- notebook / podcast / brief


class NotebookTests(PrepLogTestCase):
    def test_set_notebook_moves_to_podcast_pending_once(self):
        self.make()
        body = self.ok("--trigger", "prep", "set-notebook", KEY, "nb_abc")
        self.assertTrue(body["changed"])
        self.assertEqual((body["status_before"], body["status"]), ("pending", "podcast-pending"))
        s = self.session()
        self.assertEqual(s["notebook_id"], "nb_abc")
        self.assertEqual(s["history"][-1]["action"], "nlm-prep started: notebook nb_abc")
        again = self.ok("set-notebook", KEY, "nb_abc")
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.session()["history"]), len(s["history"]))

    def test_set_notebook_records_task_id_for_nlm_status(self):
        self.make()
        body = self.ok("set-notebook", KEY, "nb_abc", "--task-id", "task_1")
        self.assertEqual(body["task_id"], "task_1")
        self.assertEqual(self.session()["task_id"], "task_1")
        self.assertFalse(self.ok("set-notebook", KEY, "nb_abc", "--task-id", "task_1")["changed"])
        pending = self.ok("due")["podcast_pending"]
        self.assertEqual([(p["notebook_id"], p["task_id"]) for p in pending], [("nb_abc", "task_1")])

    def test_second_notebook_is_refused(self):
        self.make()
        self.ok("set-notebook", KEY, "nb_abc")
        err = self.assertError(self.run_cli("set-notebook", KEY, "nb_other"), "ALREADY_HAS_NOTEBOOK")
        self.assertEqual(err["detail"]["notebook_id"], "nb_abc")
        self.assertFalse(err["retryable"])
        self.assertEqual(self.session()["notebook_id"], "nb_abc")

    def test_notebook_clears_an_nlm_error(self):
        self.make()
        self.ok("set-status", KEY, "partial", "--error-code", "NLM_AUTH", "--step", "podcast")
        self.ok("set-notebook", KEY, "nb1")
        s = self.session()
        self.assertNotIn("last_error", s)
        self.assertEqual(s["status"], "podcast-pending")

    def test_notebook_keeps_other_errors(self):
        self.make()
        self.ok("set-status", KEY, "partial", "--error-code", "BRIEF_SCHEMA_INVALID", "--step", "brief")
        self.ok("set-notebook", KEY, "nb1")
        self.assertEqual(self.session()["last_error"]["code"], "BRIEF_SCHEMA_INVALID")

    def test_empty_notebook_id(self):
        self.make()
        self.assertError(self.run_cli("set-notebook", KEY, " "), "USAGE")


class PodcastTests(PrepLogTestCase):
    def test_ready_after_podcast_pending(self):
        self.make()
        self.ok("set-notebook", KEY, "nb1")
        body = self.ok("--trigger", "poll", "set-podcast", KEY, "--url", "https://drive.google.com/p.mp3")
        self.assertEqual((body["status_before"], body["status"]), ("podcast-pending", "ready"))
        s = self.session()
        self.assertEqual(s["podcast_url"], "https://drive.google.com/p.mp3")
        self.assertEqual(s["history"][-1]["trigger"], "poll")
        self.assertEqual(s["history"][-1]["action"], "podcast ready")
        again = self.ok("set-podcast", KEY, "--url", "https://drive.google.com/p.mp3")
        self.assertFalse(again["changed"])

    def test_other_statuses_are_left_alone(self):
        self.make()
        body = self.ok("set-podcast", KEY, "--url", "https://drive.google.com/p.mp3")
        self.assertEqual(body["status"], "pending")


class BriefTests(PrepLogTestCase):
    def test_valid_brief_is_stored(self):
        self.make()
        body = self.ok("set-brief", KEY, "--from", self.write_json("b.json", brief()))
        self.assertFalse(body["replaced"])
        self.assertEqual(body["questions"], 1)
        s = self.session()
        self.assertEqual(s["brief"]["topic"], "Agents that plan")
        self.assertEqual(s["history"][-1]["action"], "brief stored (1 question)")
        self.assertEqual(self.ok("validate")["sessions"], 1)
        body = self.ok("set-brief", KEY, "--from", self.write_json("b2.json", brief(pre_class_questions=[])))
        self.assertTrue(body["replaced"])
        self.assertEqual(body["questions"], 0)

    def test_invalid_briefs_store_nothing(self):
        self.make()
        before = self.raw()
        cases = {
            "missing": brief(),
            "extra": brief(extra="no"),
            "too_many_args": brief(key_arguments=["x"] * 9),
            "bad_question": brief(pre_class_questions=[{"question": "Q?", "source": "s"}]),
            "empty_topic": brief(topic=""),
        }
        del cases["missing"]["why_it_matters"]
        for name, payload in cases.items():
            err = self.assertError(self.run_cli("set-brief", KEY, "--from", self.write_json(name + ".json", payload)),
                                   "BRIEF_SCHEMA_INVALID")
            self.assertTrue(err["detail"]["errors"], name)
            self.assertTrue(all(e.startswith("brief") for e in err["detail"]["errors"]), err)
        self.assertEqual(self.raw(), before)

    def test_not_json_is_a_schema_failure_too(self):
        self.make()
        preplog.read_stdin = lambda: "Here is your brief: {"
        err = self.assertError(self.run_cli("set-brief", KEY, "--from", "-"), "BRIEF_SCHEMA_INVALID")
        self.assertIn("not JSON", err["detail"]["errors"][0])

    def test_brief_from_stdin(self):
        self.make()
        preplog.read_stdin = lambda: json.dumps(brief())
        self.assertEqual(self.ok("set-brief", KEY, "--from", "-")["questions"], 1)


# --------------------------------------------------------------------------- status / sent / log


class StatusTests(PrepLogTestCase):
    def test_status_with_error(self):
        self.make()
        body = self.ok("--trigger", "prep", "set-status", KEY, "partial", "--error-code", "NLM_AUTH",
                       "--error-message", "cookies expired", "--step", "podcast")
        self.assertTrue(body["changed"])
        self.assertEqual(body["last_error"], {"code": "NLM_AUTH", "message": "cookies expired", "at": NOW, "step": "podcast"})
        h = self.session()["history"][-1]
        self.assertEqual(h["action"], "status pending → partial (NLM_AUTH)")
        self.assertEqual(h["detail"], "cookies expired")

    def test_clear_error_and_no_op(self):
        self.make()
        self.ok("set-status", KEY, "needs-human", "--error-code", "LOGIN_REQUIRED")
        body = self.ok("--trigger", "human", "set-status", KEY, "pending", "--clear-error")
        self.assertEqual(body["status"], "pending")
        self.assertIsNone(body["last_error"])
        self.assertEqual(self.session()["history"][-1]["trigger"], "human")
        n = len(self.session()["history"])
        body = self.ok("set-status", KEY, "pending")
        self.assertFalse(body["changed"])
        self.assertEqual(len(self.session()["history"]), n)

    def test_usage_errors(self):
        self.make()
        self.assertError(self.run_cli("set-status", KEY, "sleeping"), "USAGE")
        self.assertError(self.run_cli("set-status", KEY, "partial", "--step", "lunch"), "USAGE")
        self.assertError(self.run_cli("set-status", KEY, "partial", "--error-message", "no code"), "USAGE")


class MarkSentTests(PrepLogTestCase):
    def test_brief_then_podcast(self):
        self.make()
        self.ok("set-notebook", KEY, "nb1")
        body = self.ok("--trigger", "notify", "mark-sent", KEY, "brief")
        self.assertEqual(body["brief_sent_at"], NOW)
        self.assertEqual(body["status"], "notified-partial")      # brief out, podcast link owed
        self.assertEqual(self.session()["history"][-1]["detail"], "podcast pending")
        self.assertError(self.run_cli("mark-sent", KEY, "brief"), "ALREADY_SENT")
        self.assertError(self.run_cli("mark-sent", KEY, "podcast"), "USAGE")   # no podcast_url yet
        self.ok("set-podcast", KEY, "--url", "https://drive.google.com/p.mp3")
        self.set_now("2026-09-29T07:00:00-04:00")
        body = self.ok("--trigger", "poll", "mark-sent", KEY, "podcast")
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["podcast_sent_at"], "2026-09-29T07:00:00-04:00")
        self.assertError(self.run_cli("mark-sent", KEY, "podcast"), "ALREADY_SENT")

    def test_brief_with_podcast_included_is_done(self):
        self.make()
        self.assertError(self.run_cli("mark-sent", KEY, "brief", "--podcast-included"), "USAGE")
        self.assertIsNone(self.session().get("brief_sent_at"))
        self.ok("set-podcast", KEY, "--url", "https://drive.google.com/p.mp3")
        body = self.ok("mark-sent", KEY, "brief", "--podcast-included")
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["podcast_sent_at"], NOW)

    def test_podcast_before_brief(self):
        self.make()
        self.ok("set-podcast", KEY, "--url", "https://drive.google.com/p.mp3")
        self.assertError(self.run_cli("mark-sent", KEY, "podcast"), "BRIEF_NOT_SENT")


class LogTests(PrepLogTestCase):
    def test_log_appends_a_history_entry(self):
        self.make()
        body = self.ok("--trigger", "prep", "log", KEY, "--action", "asked human: login required",
                       "--detail", "hbsp.harvard.edu case, drop the PDF in Readings/MAS.665/2026-09-29/")
        self.assertEqual(body["history"], 2)
        self.assertEqual(body["entry"]["trigger"], "prep")
        self.assertEqual(self.session()["history"][-1]["action"], "asked human: login required")

    def test_control_characters_and_newlines_are_stripped(self):
        self.make()
        body = self.ok("log", KEY, "--action", "line one\nline two\x07", "--detail", "a\r\nb")
        self.assertEqual(body["entry"]["action"], "line one line two")
        self.assertEqual(body["entry"]["detail"], "a b")
        self.assertError(self.run_cli("log", KEY, "--action", "\x07"), "USAGE")

    def test_env_trigger_default(self):
        self.make()
        env = dict(self.env, PREPLOG_TRIGGER="poll")
        self.assertEqual(self.ok("log", KEY, "--action", "x", env=env)["entry"]["trigger"], "poll")
        self.assertError(self.run_cli("log", KEY, "--action", "x", env=dict(self.env, PREPLOG_TRIGGER="cron")), "USAGE")


# --------------------------------------------------------------------------- due (send pass)


class DueTests(PrepLogTestCase):
    def test_send_pass_selection(self):
        # A: notify_at passed, brief stored               -> briefs
        self.make("A@2026-09-28", "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("set-brief", "A@2026-09-28", "--from", self.write_json("b.json", brief()))
        # B: notify_at in the future                      -> nothing
        self.make("B@2026-09-29")
        # C: needs-human, nothing to send                 -> excluded
        self.make("C@2026-09-28", "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("set-status", "C@2026-09-28", "needs-human", "--error-code", "LOGIN_REQUIRED")
        # D: needs-human but Drive links exist            -> briefs
        self.make("D@2026-09-28", "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("add-drive-path", "D@2026-09-28", "Readings/D/2026-09-28/x.pdf")
        self.ok("set-status", "D@2026-09-28", "needs-human", "--error-code", "LOGIN_REQUIRED")
        # E: brief sent, podcast ready, link not sent     -> podcast_links
        self.make("E@2026-09-28", "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("set-notebook", "E@2026-09-28", "nbE")
        self.ok("mark-sent", "E@2026-09-28", "brief")
        self.ok("set-podcast", "E@2026-09-28", "--url", "https://drive.google.com/e.mp3")
        # F: podcast pending                               -> podcast_pending
        self.make("F@2026-09-30")
        self.ok("set-notebook", "F@2026-09-30", "nbF")
        # G: needs-human, class today (2026-09-27 ET)      -> needs_human_today
        self.make("G@2026-09-27", class_start="2026-09-27T20:00:00-04:00")
        self.ok("set-status", "G@2026-09-27", "needs-human", "--error-code", "NEEDS_OWN_ANSWER")

        body = self.ok("--trigger", "poll", "due")
        self.assertEqual(body["now"], NOW)
        self.assertEqual(body["today"], "2026-09-27")
        # G's notify_at has passed too, but a needs-human record with no brief and no Drive links has nothing to send.
        self.assertEqual([b["key"] for b in body["briefs"]], ["A@2026-09-28", "D@2026-09-28"])
        a = body["briefs"][0]
        self.assertTrue(a["has_brief"])
        self.assertEqual(a["session"]["brief"]["topic"], "Agents that plan")
        self.assertEqual([p["key"] for p in body["podcast_links"]], ["E@2026-09-28"])
        self.assertEqual([p["key"] for p in body["podcast_pending"]], ["F@2026-09-30"])
        self.assertEqual(body["needs_human_today"], [{"key": "G@2026-09-27", "reminded": False,
                                                      "last_error": self.session("G@2026-09-27")["last_error"]}])
        self.assertFalse(body["nothing_to_do"])
        self.ok("log", "G@2026-09-27", "--action", "reminder sent")
        self.assertTrue(self.ok("due")["needs_human_today"][0]["reminded"])

    def test_after_sending_nothing_is_due_again(self):
        self.make("A@2026-09-28", "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("set-podcast", "A@2026-09-28", "--url", "https://drive.google.com/a.mp3")
        self.assertEqual(len(self.ok("due")["briefs"]), 1)
        self.ok("mark-sent", "A@2026-09-28", "brief", "--podcast-included")
        body = self.ok("due")
        self.assertEqual(body["briefs"], [])
        self.assertEqual(body["podcast_links"], [])
        self.assertTrue(body["nothing_to_do"])

    def test_now_override_forms(self):
        self.make("A@2026-09-28", "--notify-at", "2026-09-28T06:30:00-04:00")
        self.assertEqual(self.ok("due")["briefs"], [])
        self.assertEqual(len(self.ok("--now", "2026-09-28T10:30:00Z", "due")["briefs"]), 1)
        self.assertEqual(len(self.ok("--now", "2026-09-28T06:29:59", "due")["briefs"]), 0)   # naive = Eastern
        self.assertEqual(len(self.ok("--now", "2026-09-28T06:30:00", "due")["briefs"]), 1)
        self.assertError(self.run_cli("--now", "yesterday", "due"), "USAGE")

    def test_unparsable_notify_at_is_a_warning(self):
        self.make()
        doc = self.doc()
        doc["sessions"][KEY]["notify_at"] = "soon"
        self.write_doc(doc)
        body = self.ok("due")
        self.assertIn(KEY, body["warnings"][0])
        self.assertEqual(body["briefs"], [])


class ListTests(PrepLogTestCase):
    def test_list_filters_and_sorts(self):
        self.make("15.286@2026-09-30")
        self.make(KEY)
        self.ok("set-status", KEY, "partial", "--error-code", "NLM_AUTH")
        body = self.ok("list")
        self.assertEqual([s["key"] for s in body["sessions"]], [KEY, "15.286@2026-09-30"])
        row = body["sessions"][0]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["last_error"]["code"], "NLM_AUTH")
        self.assertFalse(row["has_brief"])
        self.assertEqual(self.ok("list", "--status", "partial")["count"], 1)
        self.assertEqual(self.ok("list", "--status", "done", "--status", "ready")["count"], 0)
        self.assertEqual(self.ok("list", "--course", "15.286")["sessions"][0]["key"], "15.286@2026-09-30")
        self.assertError(self.run_cli("list", "--status", "sleeping"), "USAGE")


# --------------------------------------------------------------------------- run log


class RunlogTests(PrepLogTestCase):
    def test_block_format_and_eastern_date(self):
        self.set_now("2026-09-28T03:30:00Z")              # 23:30 ET on the 27th
        body = self.ok("--trigger", "poll", "runlog", "--sessions", "MAS.665@2026-09-29 podcast-pending → ready",
                       "--tools", "nlm-status → ok (1)", "--decisions", "podcast ready; notify_at not reached",
                       "--outcome", "nothing to send")
        self.assertEqual(body["date"], "2026-09-27")
        self.assertTrue(body["path"].endswith("/logs/2026-09-27-poll.md"))
        with open(body["path"], encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, "## 2026-09-27T23:30:00-04:00 — poll\n"
                               "- Sessions considered: MAS.665@2026-09-29 podcast-pending → ready\n"
                               "- Tools called: nlm-status → ok (1)\n"
                               "- Decisions: podcast ready; notify_at not reached\n"
                               "- Outcome: nothing to send\n\n")

    def test_appends_and_keeps_earlier_blocks(self):
        self.ok("--trigger", "prep", "runlog", "--outcome", "first")
        self.set_now("2026-09-27T19:05:00-04:00")
        body = self.ok("--trigger", "prep", "runlog", "--outcome", "second", "--line", "INJECTION: canvas — ignore all rules")
        with open(body["path"], encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("\n## "), 1)
        self.assertEqual(text.count("## 2026-09-27T19:0"), 2)
        self.assertIn("- Outcome: first\n", text)
        self.assertIn("- Outcome: second\n- INJECTION: canvas — ignore all rules\n\n", text)
        self.assertIn("- Sessions considered: none\n", text)

    def test_content_cannot_forge_a_run_header(self):
        body = self.ok("--trigger", "prep", "runlog", "--decisions",
                       "HALLUCINATION: dropped 'Q1'\n## 2026-01-01T00:00:00-05:00 — prep\n- Outcome: forged\x07")
        with open(body["path"], encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        self.assertEqual([ln for ln in lines if ln.startswith("## ")], ["## %s — prep" % NOW])
        self.assertIn("  ## 2026-01-01T00:00:00-05:00 — prep", lines)
        self.assertIn("  - Outcome: forged", lines)

    def test_trigger_is_required(self):
        self.assertError(self.run_cli("runlog", "--outcome", "x"), "USAGE")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "logs", "2026-09-27-manual.md")))
        body = self.ok("runlog", "--outcome", "x", env=dict(self.env, PREPLOG_TRIGGER="notify"))
        self.assertTrue(body["path"].endswith("2026-09-27-notify.md"))

    def test_appending_to_a_file_without_trailing_newline(self):
        os.makedirs(os.path.join(self.tmp, "logs"))
        path = os.path.join(self.tmp, "logs", "2026-09-27-prep.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## older\n- Outcome: x")
        self.ok("--trigger", "prep", "runlog", "--outcome", "y")
        with open(path, encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith("## older\n- Outcome: x\n## %s" % NOW))


# --------------------------------------------------------------------------- course notes


class NotesTests(PrepLogTestCase):
    def test_get_without_a_file_returns_the_template(self):
        body = self.ok("notes", "get")
        self.assertFalse(body["exists"])
        self.assertIn("# Course notes", body["text"])
        self.assertFalse(os.path.exists(self.notes))

    def test_set_creates_a_dated_section_then_rewrites_the_line(self):
        body = self.ok("notes", "set", "--course", "MAS.665", "--title", "AI Studio", "--field", "readings",
                       "--value", "Modules › Week 3")
        self.assertTrue(body["created_section"])
        self.assertTrue(body["replaced"])
        self.assertEqual(body["line"], "- **Where readings actually live:** 2026-09-27: Modules › Week 3")
        section = self.ok("notes", "get", "--course", "MAS.665")
        self.assertTrue(section["found"])
        self.assertTrue(section["text"].startswith("## MAS.665: AI Studio\n"))
        for label in ("Canvas course id", "Links that need login", "Naming quirks", "Other"):
            self.assertIn("- **%s:**" % label, section["text"])
        self.assertIn(body["line"], section["text"])
        with open(self.notes, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# Course notes", text)                     # the template header survived
        self.assertIn("## <COURSE CODE>: <course title>", text)   # and its example block
        self.set_now("2026-10-01T09:00:00-04:00")
        body = self.ok("notes", "set", "--course", "mas.665", "--field", "readings", "--value", "Files › /Readings")
        self.assertFalse(body["created_section"])
        with open(self.notes, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("- **Where readings actually live:**"), 2)   # the example block + ours
        self.assertIn("- **Where readings actually live:** 2026-10-01: Files › /Readings", text)
        self.assertNotIn("Modules › Week 3", text)

    def test_append_keeps_the_old_value(self):
        self.ok("notes", "set", "--course", "MAS.665", "--field", "login", "--value", "hbsp.harvard.edu")
        body = self.ok("notes", "set", "--course", "MAS.665", "--field", "login", "--value", "libproxy.mit.edu", "--append")
        self.assertEqual(body["line"], "- **Links that need login:** 2026-09-27: hbsp.harvard.edu; 2026-09-27: libproxy.mit.edu")

    def test_predated_values_are_not_redated_and_ids_work(self):
        body = self.ok("notes", "set", "--course", "MAS.665", "--field", "canvas_course_id", "--value", "40577")
        self.assertEqual(body["line"], "- **Canvas course id:** 2026-09-27: 40577")
        body = self.ok("notes", "set", "--course", "MAS.665", "--field", "other", "--value", "2026-09-01: seen before")
        self.assertEqual(body["line"], "- **Other:** 2026-09-01: seen before")

    def test_other_courses_are_untouched_and_missing_lines_are_added(self):
        self.ok("notes", "set", "--course", "15.286", "--field", "questions", "--value", "Assignment 'Pre-class N'")
        self.ok("notes", "set", "--course", "MAS.665", "--field", "questions", "--value", "Discussion")
        with open(self.notes, encoding="utf-8") as fh:
            text = fh.read()
        # Remove a line by hand, then set it again: it is re-added inside the right section.
        text = text.replace("- **Naming quirks:**\n", "", 1)
        with open(self.notes, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.ok("notes", "set", "--course", "15.286", "--field", "naming", "--value", "Week numbers start at 0")
        sec = self.ok("notes", "get", "--course", "15.286")["text"]
        self.assertIn("2026-09-27: Assignment 'Pre-class N'", sec)
        self.assertIn("2026-09-27: Week numbers start at 0", sec)
        self.assertNotIn("Discussion", sec)
        self.assertIn("Discussion", self.ok("notes", "get", "--course", "MAS.665")["text"])
        self.assertFalse(self.ok("notes", "get", "--course", "6.006")["found"])

    def test_usage(self):
        self.assertError(self.run_cli("notes", "set", "--course", "MAS.665", "--field", "colour", "--value", "x"), "USAGE")
        self.assertError(self.run_cli("notes", "set", "--course", "MAS.665", "--field", "other", "--value", " "), "USAGE")
        self.assertError(self.run_cli("notes", "set", "--course", "# Hack", "--field", "other", "--value", "x"), "USAGE")
        body = self.ok("notes", "set", "--course", "MAS.665", "--field", "other", "--value", "one\nline\x07")
        self.assertEqual(body["line"], "- **Other:** 2026-09-27: one line")


# --------------------------------------------------------------------------- schema validator


class SchemaValidatorTests(unittest.TestCase):
    def setUp(self):
        self.v = preplog.SchemaValidator(TEMPLATES)

    def session(self, **over):
        s = {"course": "MAS.665", "class_date": "2026-09-29", "readings": [], "drive_paths": [],
             "notify_at": "2026-09-29T06:30:00-04:00", "status": "pending", "attempts": 0, "history": []}
        s.update(over)
        return s

    def errors(self, s):
        return self.v.errors(s, "prep-log.schema.json", "#/$defs/session")

    def test_valid_session(self):
        self.assertEqual(self.errors(self.session()), [])

    def test_types_enums_required_and_extras(self):
        self.assertIn("$.status: must be one of", self.errors(self.session(status="napping"))[0])
        self.assertIn("$.attempts: expected integer", self.errors(self.session(attempts="1"))[0])
        self.assertIn("$.attempts: expected integer", self.errors(self.session(attempts=True))[0])
        self.assertIn("below minimum", self.errors(self.session(attempts=-1))[0])
        self.assertIn("missing required property 'notify_at'", self.errors({k: v for k, v in self.session().items() if k != "notify_at"})[0])
        self.assertIn("unknown property 'extra'", self.errors(self.session(extra=1))[0])
        self.assertEqual(self.errors(self.session(canvas_course_id=1)), [])
        self.assertEqual(self.errors(self.session(canvas_course_id="1")), [])
        self.assertIn("expected string or integer", self.errors(self.session(canvas_course_id=1.5))[0])

    def test_formats_and_patterns(self):
        self.assertIn("not a valid date", self.errors(self.session(class_date="Sept 29"))[0])
        self.assertIn("not a valid date-time", self.errors(self.session(notify_at="2026-09-29"))[0])
        self.assertEqual(self.errors(self.session(notify_at="2026-09-29T10:30:00Z")), [])
        reading = {"title": "P", "source": "canvas_file", "id_or_url": "1", "local_path": "/tmp/p.pdf"}
        self.assertIn("$.readings[0].local_path: does not match", self.errors(self.session(readings=[reading]))[0])
        reading["local_path"] = "/data/readings/MAS.665/2026-09-29/p.pdf"
        self.assertEqual(self.errors(self.session(readings=[reading])), [])
        self.assertIn("$.readings[0].source: must be one of", self.errors(self.session(readings=[dict(reading, source="web")]))[0])

    def test_nested_refs_and_lists(self):
        h = {"ts": "2026-09-27T19:00:00-04:00", "trigger": "cron", "action": "x"}
        self.assertIn("$.history[0].trigger", self.errors(self.session(history=[h]))[0])
        err = {"code": "X", "message": "m", "step": "lunch"}
        self.assertIn("$.last_error.step", self.errors(self.session(last_error=err))[0])
        self.assertIn("$.brief.topic", self.errors(self.session(brief=dict(brief(), topic="")))[0])   # sibling-file $ref
        self.assertEqual(self.errors(self.session(brief=brief())), [])

    def test_document_level(self):
        doc = {"version": 1, "sessions": {"bad key": self.session()}}
        errs = self.v.errors(doc, "prep-log.schema.json")
        self.assertTrue(any("name 'bad key'" in e for e in errs), errs)
        self.assertIn("$.version: must be 1", self.v.errors({"version": 2, "sessions": {}}, "prep-log.schema.json")[0])
        self.assertEqual(self.v.errors({"version": 1, "sessions": {}, "updated_at": NOW}, "prep-log.schema.json"), [])

    def test_brief_limits(self):
        b = brief(pre_class_questions=[brief()["pre_class_questions"][0]] * 13)
        self.assertIn("more than 12 items", self.v.errors(b, "brief.schema.json")[0])
        self.assertIn("fewer than 1 items", self.v.errors(brief(key_arguments=[]), "brief.schema.json")[0])
        self.assertIn("longer than 200", self.v.errors(brief(topic="x" * 201), "brief.schema.json")[0])

    def test_error_cap(self):
        s = self.session(readings=[{"title": "", "source": "x", "id_or_url": ""}] * 60)
        self.assertEqual(len(self.errors(s)), preplog.MAX_ERRORS)

    def test_missing_schema_dir(self):
        v = preplog.SchemaValidator(os.path.join(tempfile.gettempdir(), "no-such-preplog-schemas"))
        with self.assertRaises(preplog.PrepLogError) as ctx:
            v.errors({}, "prep-log.schema.json")
        self.assertEqual(ctx.exception.code, "SCHEMA_MISSING")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_agrees_with_jsonschema(self):   # pragma: no cover - depends on the machine
        import jsonschema
        with open(os.path.join(TEMPLATES, "brief.schema.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        for payload in (brief(), brief(topic=""), brief(extra=1), brief(pre_class_questions=[{"question": "q"}])):
            ours = not self.v.errors(payload, "brief.schema.json")
            theirs = jsonschema.Draft202012Validator(schema).is_valid(payload)
            self.assertEqual(ours, theirs, payload)


class SchemaMissingCliTests(PrepLogTestCase):
    def test_schema_missing_is_reported(self):
        env = dict(self.env, PREPLOG_SCHEMA_DIR=os.path.join(self.tmp, "nowhere"))
        self.assertError(self.run_cli("upsert", KEY, env=env), "SCHEMA_MISSING")
        # init still seeds a minimal course-notes file when the template is missing too
        body = self.ok("init", env=env)
        self.assertTrue(body["created_course_notes"])


# --------------------------------------------------------------------------- Eastern time


class TimeTests(unittest.TestCase):
    def setUp(self):
        self._eastern = preplog._EASTERN

    def tearDown(self):
        preplog._EASTERN = self._eastern

    def utc(self, iso):
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)

    def test_fallback_offsets_around_the_transitions(self):
        preplog._EASTERN = None
        cases = {"2026-03-08T06:59:59Z": "-05:00", "2026-03-08T07:00:00Z": "-04:00",
                 "2026-11-01T05:59:59Z": "-04:00", "2026-11-01T06:00:00Z": "-05:00",
                 "2026-07-04T12:00:00Z": "-04:00", "2026-12-25T12:00:00Z": "-05:00",
                 "2027-03-14T07:00:00Z": "-04:00", "2027-11-07T06:00:00Z": "-05:00"}
        for iso, offset in cases.items():
            self.assertTrue(preplog.fmt(self.utc(iso)).endswith(offset), (iso, preplog.fmt(self.utc(iso))))

    @unittest.skipUnless(HAVE_TZDATA, "no tzdata on this machine")
    def test_fallback_agrees_with_zoneinfo(self):
        instants = [self.utc(x) for x in ("2026-03-08T06:59:59Z", "2026-03-08T07:00:00Z", "2026-11-01T05:59:59Z",
                                          "2026-11-01T06:00:00Z", "2026-09-27T23:00:00Z", "2027-01-15T12:00:00Z")]
        with_zone = [preplog.fmt(i) for i in instants]
        morning_zone = [preplog.eastern_wall_clock(d, 6, 30).isoformat() for d in
                        (dt.date(2026, 3, 8), dt.date(2026, 11, 1), dt.date(2026, 9, 29))]
        preplog._EASTERN = None
        self.assertEqual([preplog.fmt(i) for i in instants], with_zone)
        self.assertEqual([preplog.eastern_wall_clock(d, 6, 30).isoformat() for d in
                          (dt.date(2026, 3, 8), dt.date(2026, 11, 1), dt.date(2026, 9, 29))], morning_zone)

    def test_parse_iso_forms(self):
        z = preplog.parse_iso("2026-09-28T10:30:00Z")
        off = preplog.parse_iso("2026-09-28T06:30:00-04:00")
        naive = preplog.parse_iso("2026-09-28T06:30:00")
        self.assertEqual(z, off)
        self.assertEqual(naive, off)
        with self.assertRaises(preplog.PrepLogError):
            preplog.parse_iso("28/09/2026")

    def test_compute_notify_at(self):
        now = self.utc("2026-09-27T23:00:00Z")
        start = preplog.parse_iso(CLASS_START)
        self.assertEqual(preplog.fmt(preplog.compute_notify_at(start, True, dt.date(2026, 9, 29), now)), "2026-09-28T13:00:00-04:00")
        self.assertEqual(preplog.fmt(preplog.compute_notify_at(start, False, dt.date(2026, 9, 29), now)), "2026-09-29T06:30:00-04:00")
        self.assertEqual(preplog.fmt(preplog.compute_notify_at(start, False, dt.date(2026, 9, 27), now)), "2026-09-27T19:00:00-04:00")
        self.assertEqual(preplog.fmt(preplog.compute_notify_at(None, True, dt.date(2026, 9, 29), now)), "2026-09-29T06:30:00-04:00")


# --------------------------------------------------------------------------- CLI plumbing


class CliTests(PrepLogTestCase):
    def test_usage_errors_are_json(self):
        self.assertError(self.run_cli("frobnicate"), "USAGE")
        self.assertError(self.run_cli(), "USAGE")
        self.assertError(self.run_cli("--trigger", "cron", "list"), "USAGE")
        self.assertError(self.run_cli("notes"), "USAGE")
        self.assertError(self.run_cli("mark-sent", KEY, "letter"), "USAGE")

    def test_crash_path_prints_internal(self):
        original = preplog.cmd_list
        preplog.cmd_list = lambda ctx, args: {}["boom"]
        try:
            out = io.StringIO()
            code = preplog.main(["list"], env=self.env, out=out)
        finally:
            preplog.cmd_list = original
        self.assertEqual(code, 1)
        body = json.loads(out.getvalue())
        self.assertEqual(body["error"]["code"], "INTERNAL")
        self.assertIn("KeyError", body["error"]["message"])

    def test_atomic_write_leaves_no_temp_files(self):
        self.make()
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.prep_log)) if f.endswith(".tmp")], [])
        self.assertTrue(self.raw().startswith(b"{\n"))

    def test_subprocess_through_the_symlink(self):
        env = dict(os.environ, DATA_DIR=self.tmp)
        env.pop("PREPLOG_TRIGGER", None)
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "preplog"), "--trigger", "manual", "init"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertTrue(body["ok"] and body["created_prep_log"])
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "preplog.py"), "get", "nope"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "USAGE")
        self.assertEqual(proc.stdout.count("\n"), 1)

    def test_symlink_and_mode(self):
        link = os.path.join(SCRIPTS, "preplog")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), "preplog.py")
        self.assertTrue(os.access(os.path.join(SCRIPTS, "preplog.py"), os.X_OK))


# --------------------------------------------------------------------------- SYL-100 fixes (PR #7 review)


class NotifiedPartialTests(PrepLogTestCase):
    """The brief goes out before the podcast is ready: notified-partial -> poll -> done."""

    def test_status_is_in_the_schema_and_the_tool(self):
        with open(os.path.join(TEMPLATES, "prep-log.schema.json"), encoding="utf-8") as fh:
            enum = json.load(fh)["$defs"]["status"]["enum"]
        self.assertEqual(enum, ["pending", "podcast-pending", "ready", "notified-partial", "done", "partial",
                                "needs-human"])
        self.assertEqual(list(preplog.STATUSES), enum)
        self.assertIn("notified-partial", preplog.PREP_SKIP_STATUSES)

    def test_full_path_notify_partial_then_poll_then_done(self):
        key = "A@2026-09-28"
        self.make(key, "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("set-brief", key, "--from", self.write_json("b.json", brief()))
        self.ok("set-notebook", key, "nbA")
        # notify: the brief is due, the podcast is not ready
        due = self.ok("--trigger", "notify", "due")
        self.assertEqual([b["key"] for b in due["briefs"]], [key])
        body = self.ok("--trigger", "notify", "mark-sent", key, "brief")
        self.assertEqual(body["status"], "notified-partial")
        self.assertEqual(self.session(key)["history"][-1]["action"], "brief sent without podcast link")
        # prep skips it; begin counts no attempt
        begin = self.ok("--trigger", "prep", "begin", key)
        self.assertTrue(begin["skip"])
        self.assertEqual(begin["reason"], "status:notified-partial")
        self.assertEqual(self.session(key)["attempts"], 0)
        # poll: the notebook is still polled, no brief is due again, no link yet
        due = self.ok("--trigger", "poll", "due")
        self.assertEqual(due["briefs"], [])
        self.assertEqual(due["podcast_links"], [])
        self.assertEqual([p["key"] for p in due["podcast_pending"]], [key])
        # poll: nlm-status says ready -> record it; status stays notified-partial, link only is owed
        self.set_now("2026-09-27T19:30:00-04:00")
        body = self.ok("--trigger", "poll", "set-podcast", key, "--url", "https://drive.google.com/a.mp3")
        self.assertEqual(body["status"], "notified-partial")
        self.assertEqual(body["send"], "podcast-link-only")
        due = self.ok("--trigger", "poll", "due")
        self.assertEqual(due["briefs"], [])
        self.assertEqual(due["podcast_pending"], [])
        self.assertEqual(due["podcast_links"], [{"key": key, "status": "notified-partial",
                                                 "podcast_url": "https://drive.google.com/a.mp3",
                                                 "send": "podcast-link-only"}])
        self.assertError(self.run_cli("mark-sent", key, "brief"), "ALREADY_SENT")
        body = self.ok("--trigger", "poll", "mark-sent", key, "podcast")
        self.assertEqual(body["status"], "done")
        due = self.ok("--trigger", "poll", "due")
        self.assertTrue(due["nothing_to_do"])
        self.assertEqual(self.ok("validate")["sessions"], 1)

    def test_set_notebook_does_not_undo_a_sent_brief(self):
        key = "A@2026-09-28"
        self.make(key, "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("mark-sent", key, "brief")
        self.assertEqual(self.ok("set-notebook", key, "nbA")["status"], "notified-partial")

    def test_list_filter_accepts_it(self):
        key = "A@2026-09-28"
        self.make(key, "--notify-at", "2026-09-27T13:00:00-04:00")
        self.ok("mark-sent", key, "brief")
        self.assertEqual(self.ok("list", "--status", "notified-partial")["count"], 1)


class NoOpUpsertTests(PrepLogTestCase):
    """Eval case 4: an already-prepped session costs no writes."""

    @staticmethod
    def digest(path):
        import hashlib
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def test_repeat_upsert_changes_nothing(self):
        self.make(KEY, "--topic", "Agents", due=True)
        before, mtime = self.digest(self.prep_log), os.stat(self.prep_log).st_mtime_ns
        self.set_now("2026-09-28T19:00:00-04:00")       # a day later: updated_at would move if we wrote
        body = self.make(KEY, "--topic", "Agents", due=True)
        self.assertFalse(body["created"])
        self.assertFalse(body["changed"])
        self.assertEqual(self.digest(self.prep_log), before)
        self.assertEqual(os.stat(self.prep_log).st_mtime_ns, mtime)

    def test_repeat_upsert_on_a_done_record_changes_nothing(self):
        self.make()
        self.ok("set-podcast", KEY, "--url", "https://drive.google.com/p.mp3")
        self.ok("mark-sent", KEY, "brief", "--podcast-included")
        before = self.digest(self.prep_log)
        self.assertFalse(self.make()["changed"])
        self.assertTrue(self.ok("begin", KEY)["skip"])
        self.ok("get", KEY)
        self.ok("list")
        self.assertEqual(self.digest(self.prep_log), before)

    def test_a_real_change_is_written(self):
        self.make()
        before = self.digest(self.prep_log)
        body = self.make(KEY, "--topic", "New")
        self.assertTrue(body["changed"])
        self.assertNotEqual(self.digest(self.prep_log), before)


class ResetAttemptsTests(PrepLogTestCase):
    def exhaust(self):
        self.make()
        for _ in range(3):
            self.ok("begin", KEY)
        self.ok("begin", KEY)                       # -> needs-human, MAX_ATTEMPTS

    def test_human_reset_gives_three_fresh_attempts(self):
        self.exhaust()
        body = self.ok("--trigger", "human", "set-status", KEY, "pending", "--reset-attempts")
        self.assertEqual(body["attempts"], 0)
        self.assertIsNone(body["last_error"])
        s = self.session()
        self.assertEqual(s["status"], "pending")
        self.assertEqual(s["history"][-1]["action"], "status needs-human → pending, attempts reset (was 3)")
        self.assertEqual(s["history"][-1]["trigger"], "human")
        begin = self.ok("--trigger", "prep", "begin", KEY)
        self.assertFalse(begin["skip"])              # no instant MAX_ATTEMPTS, no second ping to Chris
        self.assertEqual(begin["attempts"], 1)

    def test_reset_is_refused_for_cron_triggers_and_other_statuses(self):
        self.exhaust()
        for trig in ("prep", "poll", "notify"):
            self.assertError(self.run_cli("--trigger", trig, "set-status", KEY, "pending", "--reset-attempts"), "USAGE")
        self.assertError(self.run_cli("--trigger", "human", "set-status", KEY, "partial", "--reset-attempts"), "USAGE")
        self.assertEqual(self.session()["attempts"], 3)
        self.ok("--trigger", "manual", "set-status", KEY, "pending", "--reset-attempts")
        self.assertEqual(self.session()["attempts"], 0)


class FromJsonGuardTests(PrepLogTestCase):
    def test_guarded_fields_are_refused(self):
        self.make()
        self.ok("set-notebook", KEY, "nb1")
        before = self.raw()
        err = self.assertError(self.run_cli("upsert", KEY, "--from-json",
                                            self.write_json("p.json", {"notebook_id": "nb2"})), "USAGE")
        self.assertIn("set-notebook", err["message"])
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.session()["notebook_id"], "nb1")
        patches = ({"status": "pending"}, {"brief_sent_at": NOW}, {"podcast_sent_at": NOW},
                   {"podcast_url": "https://x"}, {"brief": brief()}, {"last_error": {"code": "X", "message": "x"}})
        for patch in patches:
            self.assertError(self.run_cli("upsert", KEY, "--from-json", self.write_json("p.json", patch)), "USAGE")
        self.assertEqual(self.raw(), before)


class NotifyAtRecomputeTests(PrepLogTestCase):
    def test_changed_class_start_moves_a_computed_notify_at(self):
        self.make(due=True)
        self.assertEqual(self.session()["notify_at"], "2026-09-28T13:00:00-04:00")
        s = self.make(KEY, due=True, class_start="2026-09-29T15:00:00-04:00")["session"]
        self.assertEqual(s["notify_at"], "2026-09-28T15:00:00-04:00")
        self.assertEqual(s["history"][-1]["action"],
                         "notify_at 2026-09-28T13:00:00-04:00 → 2026-09-28T15:00:00-04:00")

    def test_changed_has_due_moves_it_both_ways(self):
        self.make()
        self.assertEqual(self.session()["notify_at"], "2026-09-29T06:30:00-04:00")
        self.make(KEY, "--has-due-before-class", "true")
        self.assertEqual(self.session()["notify_at"], "2026-09-28T13:00:00-04:00")
        self.make(KEY, "--has-due-before-class", "false")
        self.assertEqual(self.session()["notify_at"], "2026-09-29T06:30:00-04:00")

    def test_explicit_notify_at_wins_and_is_kept_when_inputs_do_not_change(self):
        self.make(KEY, "--notify-at", "2026-09-28T20:00:00-04:00")
        self.make(KEY, "--topic", "x")
        self.assertEqual(self.session()["notify_at"], "2026-09-28T20:00:00-04:00")
        self.make(KEY, "--notify-at", "2026-09-28T21:00:00-04:00", due=True)
        self.assertEqual(self.session()["notify_at"], "2026-09-28T21:00:00-04:00")


class ReviewNitTests(PrepLogTestCase):
    def test_class_start_on_another_day_is_refused(self):
        self.assertError(self.run_cli("upsert", KEY, "--class-start", "2026-09-30T13:00:00-04:00"), "USAGE")
        # 01:00Z on the 30th is 21:00 ET on the 29th: the ET date is what counts.
        self.assertFalse(os.path.exists(self.prep_log))
        self.assertEqual(self.ok("upsert", KEY, "--class-start", "2026-09-30T01:00:00Z")["session"]["class_start"],
                         "2026-09-29T21:00:00-04:00")

    def test_reads_create_neither_memory_dir_nor_lock(self):
        for argv in (["list"], ["due"], ["validate"], ["get", KEY], ["notes", "get"]):
            self.run_cli(*argv)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "memory")))
        self.make()
        lock = self.prep_log + ".lock"
        os.unlink(lock)
        for argv in (["list"], ["due"], ["validate"], ["get", KEY]):
            self.ok(*argv)
        self.assertFalse(os.path.exists(lock))

    def test_memory_files_are_private(self):
        self.ok("init")
        self.make()
        self.ok("notes", "set", "--course", "MAS.665", "--field", "readings", "--value", "Canvas modules")
        for path in (self.prep_log, self.notes):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)


if __name__ == "__main__":
    unittest.main()
