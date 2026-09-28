"""Tests for workspace/skills/drive/scripts/drive.py (SYL-95).

No network and no rclone: the module's `run_rclone` hook is replaced with a fake that keeps an
in-memory Drive (folders of lsjson-shaped entries) and answers lsjson / copyto / link / about the
way rclone does. A few tests run the real subprocess path against a fake `rclone` shell script.

    python3 -m unittest discover -s tests -v
"""
import hashlib
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
SCRIPTS = os.path.join(REPO, "workspace", "skills", "drive", "scripts")
sys.path.insert(0, SCRIPTS)

import drive  # noqa: E402

COURSE = "MAS.665"
DATE = "2026-09-29"
REMOTE_DIR = "%s/%s" % (COURSE, DATE)
TOKEN = "ya29.a0AfH6SMB-secret-access-token-value-0123456789"
REFRESH = "1//0gSecretRefreshTokenValue-abcdefghijklmnop"
CLIENT_SECRET = "GOCSPX-client-secret-value-xyz"
NOT_FOUND = "2026/09/27 22:00:00 ERROR : Failed to lsjson: directory not found\n"


def md5(data):
    return hashlib.md5(data).hexdigest()


# ----------------------------------------------------------------------------- fake rclone


class FakeRclone:
    """Stands in for the rclone binary. `folders` maps '<root>/<dir>' to lsjson-shaped entries."""

    def __init__(self):
        self.calls = []              # (subcommand, args, timeout)
        self.folders = {}            # "Readings/MAS.665/2026-09-29" -> [entry, ...]
        self.fail = {}               # subcommand -> (rc, stderr) | Exception
        self.about = {"total": 100, "used": 40, "free": 60}
        self.with_ids = True         # False: a backend without file ids
        self.with_md5 = True         # False: entries carry no hashes
        self.link_stdout = None      # override what `link` prints
        self.next_id = 100

    @staticmethod
    def _split(target):
        remote, _, path = target.partition(":")
        return remote, path.strip("/")

    def ops(self, name=None):
        return [c for c in self.calls if name is None or c[0] == name]

    def seed(self, folder, name, data, file_id=None):
        entry = {"Path": folder + "/" + name, "Name": name, "Size": len(data), "MimeType": "application/pdf",
                 "IsDir": False, "Hashes": {"md5": md5(data)}}
        if self.with_ids:
            entry["ID"] = file_id or "id-%d" % self._new_id()
        self.folders.setdefault(folder, []).append(entry)
        return entry

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def __call__(self, cfg, args, timeout):
        op = args[0]
        self.calls.append((op, list(args), timeout))
        if op in self.fail:
            failure = self.fail[op]
            if isinstance(failure, Exception):
                raise failure
            return failure[0], "", failure[1]
        if op == "lsjson":
            _, path = self._split(args[-1])
            if path not in self.folders:
                return 3, "", NOT_FOUND
            entries = []
            for e in self.folders[path]:
                e = dict(e)
                if "--hash" not in args or not self.with_md5:
                    e.pop("Hashes", None)
                entries.append(e)
            return 0, json.dumps(entries) + "\n", ""
        if op == "copyto":
            local, target = args[-2], args[-1]
            _, path = self._split(target)
            folder, _, name = path.rpartition("/")
            with open(local, "rb") as fh:
                data = fh.read()
            entries = self.folders.setdefault(folder, [])
            for e in entries:
                if e["Name"] == name:            # rclone updates the existing object in place
                    e["Size"], e["Hashes"] = len(data), {"md5": md5(data)}
                    break
            else:
                self.seed(folder, name, data)
            return 0, "", "Transferred: 1 / 1, 100%\n"
        if op == "link":
            _, path = self._split(args[-1])
            folder, _, name = path.rpartition("/")
            for e in self.folders.get(folder, []):
                if e["Name"] == name:
                    out = self.link_stdout
                    if out is None:
                        out = "https://drive.google.com/open?id=%s\n" % e.get("ID", "x")
                    return 0, out, ""
            return 4, "", "Failed to link: object not found\n"
        if op == "about":
            return 0, json.dumps(self.about) + "\n", ""
        raise AssertionError("unexpected rclone subcommand %r" % op)


class DriveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.realpath(self.tmp.name)
        self.readings = os.path.join(self.data, "readings", COURSE, DATE)
        os.makedirs(self.readings)
        os.makedirs(os.path.join(self.data, "rclone"))
        self.conf = os.path.join(self.data, "rclone", "rclone.conf")
        with open(self.conf, "w") as fh:
            fh.write("[gdrive]\ntype = drive\nscope = drive\nclient_secret = %s\ntoken = %s\nteam_drive = \n"
                     % (CLIENT_SECRET, json.dumps({"access_token": TOKEN, "token_type": "Bearer",
                                                   "refresh_token": REFRESH, "expiry": "2026-09-28T00:00:00Z"})))
        self.pdf_bytes = b"%PDF-1.4 week four reading"
        self.pdf = self._file("week4.pdf", self.pdf_bytes)
        self.fake = FakeRclone()
        self._orig = drive.run_rclone
        drive.run_rclone = self.fake
        self.env = {"DATA_DIR": self.data}

    def tearDown(self):
        drive.run_rclone = self._orig
        self.tmp.cleanup()

    def _file(self, name, content=b"%PDF-1.4 hello", folder=None):
        path = os.path.join(folder or self.readings, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def run_cli(self, *argv, env=None, prog="drive"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = drive.main(list(argv), env=self.env if env is None else env, out=out, prog=prog)
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


# ----------------------------------------------------------------------------- usage


class UsageTests(DriveTestCase):
    def test_missing_or_bogus_command(self):
        self.assertError(self.run_cli(), "USAGE")
        self.assertError(self.run_cli("bogus"), "USAGE")
        self.assertError(self.run_cli("put", self.pdf), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_local_outside_data_dir_is_refused_before_any_call(self):
        outside = os.path.join(os.path.realpath(tempfile.gettempdir()), "drive-test-outside.pdf")
        with open(outside, "wb") as fh:
            fh.write(b"%PDF")
        try:
            err = self.assertError(self.run_cli("put", outside, REMOTE_DIR), "USAGE")
            self.assertIn(self.data, err["message"])
            link = os.path.join(self.readings, "sneaky.pdf")
            os.symlink(outside, link)
            self.assertError(self.run_cli("put", link, REMOTE_DIR), "USAGE")
        finally:
            os.remove(outside)
        self.assertEqual(self.fake.calls, [])

    def test_missing_local_file(self):
        err = self.assertError(self.run_cli("put", os.path.join(self.readings, "nope.pdf"), REMOTE_DIR), "LOCAL_NOT_FOUND")
        self.assertFalse(err["retryable"])
        self.assertEqual(self.fake.calls, [])

    def test_empty_file(self):
        empty = self._file("empty.pdf", b"")
        self.assertError(self.run_cli("put", empty, REMOTE_DIR), "USAGE")

    def test_unsafe_file_names(self):
        for name in (".hidden.pdf", "we;rd.pdf", "café.pdf", "a|b.pdf"):
            path = self._file(name)
            err = self.assertError(self.run_cli("put", path, REMOTE_DIR), "BAD_FILENAME")
            self.assertEqual(err["detail"]["name"], name)
        self.assertEqual(self.fake.calls, [])

    def test_unsafe_remote_dirs(self):
        for remote_dir in ("../x", "MAS.665/..", "a:b", "a\\b", "", "/", ".hidden/x", "-flag/x", "a/b c"):
            err = self.assertError(self.run_cli("put", self.pdf, remote_dir), "USAGE")
            self.assertIn("remote_dir", err["message"] + json.dumps(err.get("detail", {})))
        self.assertEqual(self.fake.calls, [])

    def test_remote_dir_is_normalised(self):
        for spelled in ("/MAS.665/2026-09-29/", "Readings/MAS.665/2026-09-29", "Readings/MAS.665/2026-09-29/"):
            self.fake.calls = []
            body = self.assertOk(self.run_cli("put", self.pdf, spelled))
            self.assertEqual(body["remote_path"], "Readings/%s/week4.pdf" % REMOTE_DIR, spelled)
            self.assertEqual(self.fake.ops("lsjson")[0][1][-1], "gdrive:Readings/" + REMOTE_DIR, spelled)

    def test_bad_environment(self):
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_READINGS_ROOT="Readings")), "USAGE")
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_READINGS_ROOT="gdrive:../x")), "USAGE")
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_SHARE="public")), "USAGE")
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="soon")), "USAGE")
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="0")), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_missing_rclone_config_is_auth_not_a_crash(self):
        missing = os.path.join(self.data, "rclone", "nope.conf")
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, RCLONE_CONFIG=missing)), "DRIVE_AUTH")
        self.assertFalse(err["retryable"])
        self.assertIn(missing, err["message"])
        self.assertIn("rclone config", err["message"])
        self.assertEqual(self.fake.calls, [])

    def test_root_without_folder_and_custom_root(self):
        env = dict(self.env, DRIVE_READINGS_ROOT="box:")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=env))
        self.assertEqual(body["remote_path"], "%s/week4.pdf" % REMOTE_DIR)
        self.assertEqual(self.fake.ops("copyto")[0][1][-1], "box:%s/week4.pdf" % REMOTE_DIR)
        env = dict(self.env, DRIVE_READINGS_ROOT="gdrive:School/Readings/")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=env))
        self.assertEqual(body["remote_path"], "School/Readings/%s/week4.pdf" % REMOTE_DIR)


# ----------------------------------------------------------------------------- put


class PutTests(DriveTestCase):
    def test_first_upload(self):
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertTrue(body["uploaded"])
        self.assertEqual(body["remote_path"], "Readings/%s/week4.pdf" % REMOTE_DIR)
        self.assertEqual(body["bytes"], len(self.pdf_bytes))
        self.assertEqual(body["md5"], md5(self.pdf_bytes))
        self.assertEqual(body["share"], "private")
        self.assertEqual(body["share_link"], "https://drive.google.com/file/d/%s/view" % body["file_id"])
        self.assertEqual(body["local_path"], self.pdf)
        self.assertEqual([c[0] for c in self.fake.calls], ["lsjson", "copyto", "lsjson"],
                         "list, upload, list again for the id; no rclone link in private mode")
        copyto = self.fake.ops("copyto")[0][1]
        self.assertEqual(copyto[-2:], [self.pdf, "gdrive:Readings/%s/week4.pdf" % REMOTE_DIR])
        self.assertIn("--ignore-times", copyto)
        lsjson = self.fake.ops("lsjson")[0][1]
        for flag in ("--files-only", "--hash", "--no-modtime"):
            self.assertIn(flag, lsjson)

    def test_second_run_is_idempotent(self):
        first = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.fake.calls = []
        second = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertFalse(second["uploaded"])
        self.assertEqual(second["share_link"], first["share_link"])
        self.assertEqual(second["file_id"], first["file_id"])
        self.assertEqual([c[0] for c in self.fake.calls], ["lsjson"], "no transfer at all")

    def test_changed_content_is_re_uploaded_in_place(self):
        first = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self._file("week4.pdf", b"%PDF-1.4 a corrected version")
        self.fake.calls = []
        second = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertTrue(second["uploaded"])
        self.assertEqual(second["file_id"], first["file_id"], "rclone updates the object, the link stays valid")
        self.assertEqual(second["md5"], md5(b"%PDF-1.4 a corrected version"))
        self.assertEqual(len(self.fake.ops("copyto")), 1)

    def test_same_size_different_hash_is_re_uploaded(self):
        other = b"%PDF-1.4 week four rEading"      # same length as self.pdf_bytes
        self.assertEqual(len(other), len(self.pdf_bytes))
        self.fake.seed("Readings/" + REMOTE_DIR, "week4.pdf", other)
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertTrue(body["uploaded"])

    def test_same_size_and_no_remote_hash_counts_as_same(self):
        self.fake.with_md5 = False
        self.fake.seed("Readings/" + REMOTE_DIR, "week4.pdf", b"x" * len(self.pdf_bytes), file_id="abc123")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertFalse(body["uploaded"])
        self.assertEqual(body["file_id"], "abc123")
        self.assertEqual(self.fake.ops("copyto"), [])

    def test_drive_duplicates_prefer_the_matching_hash(self):
        self.fake.seed("Readings/" + REMOTE_DIR, "week4.pdf", b"an older copy", file_id="old")
        self.fake.seed("Readings/" + REMOTE_DIR, "week4.pdf", self.pdf_bytes, file_id="good")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertFalse(body["uploaded"])
        self.assertEqual(body["file_id"], "good")

    def test_other_files_in_the_folder_are_ignored(self):
        self.fake.seed("Readings/" + REMOTE_DIR, "week3.pdf", b"another reading")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertTrue(body["uploaded"])
        self.assertEqual(len(self.fake.folders["Readings/" + REMOTE_DIR]), 2)

    def test_share_anyone_uses_rclone_link(self):
        env = dict(self.env, DRIVE_SHARE="anyone")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=env))
        self.assertEqual(body["share"], "anyone")
        self.assertEqual(body["share_link"], "https://drive.google.com/open?id=%s" % body["file_id"])
        self.assertEqual([c[0] for c in self.fake.calls], ["lsjson", "copyto", "lsjson", "link"])
        self.assertEqual(self.fake.ops("link")[0][1][-1], "gdrive:Readings/%s/week4.pdf" % REMOTE_DIR)

    def test_link_takes_the_last_line(self):
        self.fake.link_stdout = "NOTICE: creating public link\nhttps://drive.google.com/open?id=zzz\n\n"
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_SHARE="anyone")))
        self.assertEqual(body["share_link"], "https://drive.google.com/open?id=zzz")

    def test_link_without_a_url_is_unavailable(self):
        self.fake.link_stdout = "something odd\n"
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_SHARE="anyone")),
                               "DRIVE_UNAVAILABLE")
        self.assertTrue(err["retryable"])

    def test_private_mode_needs_a_file_id(self):
        self.fake.with_ids = False
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "USAGE")
        self.assertIn("DRIVE_SHARE=anyone", err["message"])
        self.assertEqual(len(self.fake.ops("copyto")), 1, "the upload itself happened; rerunning is idempotent")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_SHARE="anyone")))
        self.assertFalse(body["uploaded"])
        self.assertIsNone(body["file_id"])

    def test_shortcut_id_uses_the_target(self):
        self.fake.seed("Readings/" + REMOTE_DIR, "week4.pdf", self.pdf_bytes, file_id="target-id\tshortcut-id")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertEqual(body["file_id"], "target-id")
        self.assertEqual(body["share_link"], "https://drive.google.com/file/d/target-id/view")

    def test_unsafe_id_from_drive_is_not_put_in_a_url(self):
        self.fake.seed("Readings/" + REMOTE_DIR, "week4.pdf", self.pdf_bytes, file_id="abc/../../evil?x=1")
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "USAGE")
        self.assertNotIn("evil", err["message"])

    def test_file_too_large_is_checked_before_any_call(self):
        orig = drive.MAX_FILE_BYTES
        drive.MAX_FILE_BYTES = 10
        try:
            err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "FILE_TOO_LARGE")
        finally:
            drive.MAX_FILE_BYTES = orig
        self.assertFalse(err["retryable"])
        self.assertEqual(err["detail"]["limit"], 10)
        self.assertEqual(self.fake.calls, [])

    def test_upload_that_does_not_land_is_retryable(self):
        real = self.fake.__call__

        def dropping(cfg, args, timeout):
            if args[0] == "copyto":
                self.fake.calls.append(("copyto", list(args), timeout))
                return 0, "", ""          # says ok, stores nothing
            return real(cfg, args, timeout)

        drive.run_rclone = dropping
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_UNAVAILABLE")
        self.assertTrue(err["retryable"])
        self.assertIn("not listed", err["message"])

    def test_size_mismatch_after_upload_is_retryable(self):
        real = self.fake.__call__

        def truncating(cfg, args, timeout):
            rc, out, err = real(cfg, args, timeout)
            if args[0] == "copyto":
                self.fake.folders["Readings/" + REMOTE_DIR][0]["Size"] = 3
            return rc, out, err

        drive.run_rclone = truncating
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_UNAVAILABLE")
        self.assertTrue(err["retryable"])
        self.assertIn("size mismatch", err["message"])

    def test_hash_mismatch_after_upload_is_retryable(self):
        real = self.fake.__call__

        def corrupting(cfg, args, timeout):
            rc, out, err = real(cfg, args, timeout)
            if args[0] == "copyto":
                self.fake.folders["Readings/" + REMOTE_DIR][0]["Hashes"] = {"md5": "0" * 32}
            return rc, out, err

        drive.run_rclone = corrupting
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_UNAVAILABLE")
        self.assertIn("hash mismatch", err["message"])

    def test_lsjson_garbage_is_unavailable(self):
        real = self.fake.__call__

        def garbage(cfg, args, timeout):
            if args[0] == "lsjson":
                return 0, "<html>login</html>", ""
            return real(cfg, args, timeout)

        drive.run_rclone = garbage
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_UNAVAILABLE")
        self.assertTrue(err["retryable"])
        self.assertIn("not JSON", err["message"])

    def test_podcast_from_nlm_status(self):
        """nlm-status runs `drive-put /data/podcasts/<course>/<date>.m4a <course>/<date>`."""
        folder = os.path.join(self.data, "podcasts", COURSE)
        os.makedirs(folder)
        audio = self._file(DATE + ".m4a", b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 32, folder=folder)
        body = self.assertOk(self.run_cli("put", audio, REMOTE_DIR, prog="drive-put"))
        self.assertEqual(body["remote_path"], "Readings/%s/%s.m4a" % (REMOTE_DIR, DATE))
        self.assertTrue(body["share_link"].startswith("https://drive.google.com/"))


# ----------------------------------------------------------------------------- error mapping


class ErrorMappingTests(DriveTestCase):
    def _fail_copy(self, rc, stderr):
        self.fake.fail["copyto"] = (rc, stderr)
        return self.run_cli("put", self.pdf, REMOTE_DIR)

    def test_token_refresh_timeout_is_network_not_auth(self):
        _, body, _ = self._fail_copy(1, "Failed to copyto: couldn't fetch token: Post \"https://oauth2.googleapis.com/token\": "
                                        "dial tcp: i/o timeout\n")
        self.assertEqual(body["error"]["code"], "DRIVE_UNAVAILABLE")
        self.assertTrue(body["error"]["retryable"])

    def test_expired_token_is_auth(self):
        rc, body, _ = self._fail_copy(1, '2026/09/27 22:00:00 Failed to copyto: couldn\'t fetch token: invalid_grant: '
                                         'oauth2: "invalid_grant" "Token has been expired or revoked."\n')
        self.assertEqual(rc, 2)
        err = body["error"]
        self.assertEqual(err["code"], "DRIVE_AUTH")
        self.assertFalse(err["retryable"])
        self.assertIn("rclone config reconnect gdrive:", err["message"])
        self.assertIn(self.conf, err["message"])
        self.assertIn("invalid_grant", err["message"])
        self.assertNotIn("2026/09/27 22:00:00", err["message"], "timestamps are stripped")

    def test_401_is_auth_with_status(self):
        _, body, _ = self._fail_copy(1, "Failed to copyto: googleapi: Error 401: Invalid Credentials, authError\n")
        self.assertEqual(body["error"]["code"], "DRIVE_AUTH")
        self.assertEqual(body["error"]["status"], 401)

    def test_missing_remote_section_is_auth(self):
        _, body, _ = self._fail_copy(1, "Failed to create file system for \"gdrive:Readings\": didn't find section in config file (\"gdrive\")\n")
        self.assertEqual(body["error"]["code"], "DRIVE_AUTH")

    def test_storage_quota(self):
        _, body, _ = self._fail_copy(1, "ERROR : week4.pdf: Failed to copy: googleapi: Error 403: The user's Drive "
                                        "storage quota has been exceeded., storageQuotaExceeded\n")
        self.assertEqual(body["error"]["code"], "DRIVE_QUOTA")
        self.assertTrue(body["error"]["retryable"])
        self.assertEqual(body["error"]["status"], 403)

    def test_rate_limit_is_quota(self):
        _, body, _ = self._fail_copy(1, "googleapi: Error 403: User Rate Limit Exceeded., userRateLimitExceeded\n")
        self.assertEqual(body["error"]["code"], "DRIVE_QUOTA")
        _, body, _ = self._fail_copy(1, "googleapi: Error 429: Too Many Requests, rateLimitExceeded\n")
        self.assertEqual(body["error"]["code"], "DRIVE_QUOTA")

    def test_network_errors_are_retryable(self):
        for rc, stderr in ((5, "Failed to copyto: Post https://www.googleapis.com/...: dial tcp: lookup "
                               "www.googleapis.com: no such host\n"),
                           (1, "Failed to copyto: googleapi: Error 503: Service Unavailable\n"),
                           (1, "Failed to copyto: read tcp: i/o timeout\n"),
                           (5, "")):
            _, body, _ = self._fail_copy(rc, stderr)
            self.assertEqual(body["error"]["code"], "DRIVE_UNAVAILABLE", stderr)
            self.assertTrue(body["error"]["retryable"], stderr)

    def test_fatal_and_usage_exits_are_not_retryable(self):
        _, body, _ = self._fail_copy(7, "Fatal error: account suspended\n")
        self.assertEqual(body["error"]["code"], "DRIVE_UNAVAILABLE")
        self.assertFalse(body["error"]["retryable"])
        _, body, _ = self._fail_copy(1, "Error: unknown flag: --ignore-times\n")
        self.assertFalse(body["error"]["retryable"])
        self.assertIn("exit 1", body["error"]["message"])
        _, body, _ = self._fail_copy(2, "some uncategorised problem\n")
        self.assertTrue(body["error"]["retryable"])

    def test_listing_failure_stops_before_the_upload(self):
        self.fake.fail["lsjson"] = (1, "Failed to lsjson: couldn't fetch token: invalid_grant\n")
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_AUTH")
        self.assertIn("listing", err["message"])
        self.assertEqual(self.fake.ops("copyto"), [])

    def test_missing_folder_is_not_an_error(self):
        self.fake.fail["lsjson"] = (3, "2026/09/27 22:00:00 ERROR : : error listing: directory not found\nFailed to lsjson: directory not found\n")
        self.assertEqual(drive.list_remote_dir(drive.Config(self.env), REMOTE_DIR), [])
        self.fake.fail["lsjson"] = (4, "")
        self.assertEqual(drive.list_remote_dir(drive.Config(self.env), REMOTE_DIR), [])

    def test_link_failure_maps_too(self):
        self.fake.fail["link"] = (1, "Failed to link: googleapi: Error 403: insufficientPermissions\n")
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_SHARE="anyone")), "DRIVE_AUTH")
        self.assertIn("share link", err["message"])

    def test_out_of_time_before_a_call(self):
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="0.5")), "DRIVE_UNAVAILABLE")
        self.assertTrue(err["retryable"])
        self.assertIn("out of time", err["message"])
        self.assertEqual(self.fake.calls, [])

    def test_each_call_gets_at_most_the_remaining_time(self):
        self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="20")))
        for op, _, timeout in self.fake.calls:
            self.assertLessEqual(timeout, 20, op)
            self.assertGreater(timeout, 0, op)
        self.assertLessEqual(self.fake.ops("lsjson")[0][2], drive.LIST_TIMEOUT)


# ----------------------------------------------------------------------------- check


class CheckTests(DriveTestCase):
    def test_check_reports_root_and_quota(self):
        self.fake.folders["Readings"] = [{"Name": COURSE, "IsDir": True, "Size": -1}]
        body = self.assertOk(self.run_cli("check"))
        self.assertEqual(body["root"], "gdrive:Readings")
        self.assertTrue(body["root_exists"])
        self.assertEqual(body["entries"], 1)
        self.assertEqual(body["used_bytes"], 40)
        self.assertEqual(body["free_bytes"], 60)
        self.assertEqual(body["config_path"], self.conf)
        self.assertEqual(body["share"], "private")
        self.assertEqual([c[0] for c in self.fake.calls], ["lsjson", "about"])
        self.assertEqual(self.fake.ops("about")[0][1][-1], "gdrive:")

    def test_check_with_missing_root(self):
        body = self.assertOk(self.run_cli("check"))
        self.assertFalse(body["root_exists"])
        self.assertIsNone(body["entries"])

    def test_check_without_about_support(self):
        self.fake.folders["Readings"] = []
        self.fake.fail["about"] = (1, "Failed to about: about not supported\n")
        body = self.assertOk(self.run_cli("check"))
        self.assertTrue(body["root_exists"])
        self.assertNotIn("used_bytes", body)

    def test_check_auth_failure(self):
        self.fake.fail["lsjson"] = (1, "Failed to lsjson: couldn't fetch token: invalid_grant\n")
        self.assertError(self.run_cli("check"), "DRIVE_AUTH")

    def test_check_symlink_name_implies_command(self):
        self.fake.folders["Readings"] = []
        self.assertOk(self.run_cli(prog="drive-check"))


# ----------------------------------------------------------------------------- secrets / crashes


class SafetyTests(DriveTestCase):
    def test_config_values_never_reach_stdout_or_stderr(self):
        self.fake.fail["copyto"] = (1, "Failed to copyto: oauth2: token %s rejected (client %s, refresh %s)\n"
                                    % (TOKEN, CLIENT_SECRET, REFRESH))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            rc = drive.main(["-v", "put", self.pdf, REMOTE_DIR], env=self.env, out=out, prog="drive")
        self.assertEqual(rc, 2)
        for secret in (TOKEN, CLIENT_SECRET, REFRESH):
            self.assertNotIn(secret, out.getvalue())
            self.assertNotIn(secret, err.getvalue())
        self.assertIn("<redacted>", out.getvalue())
        self.assertIn("drive: ", err.getvalue(), "verbose log lines are written")
        self.assertIn("lsjson Readings/%s" % REMOTE_DIR, err.getvalue())

    def test_rclone_stderr_noise_is_scrubbed(self):
        real = self.fake.__call__

        def noisy(cfg, args, timeout):
            sys.stderr.write("rclone: Authorization: Bearer %s\n" % TOKEN)
            return real(cfg, args, timeout)

        drive.run_rclone = noisy
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            drive.main(["check"], env=self.env, out=out)
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertIn("<redacted>", err.getvalue())

    def test_crash_is_exit_1_internal_and_redacted(self):
        def broken(cfg, args, timeout):
            raise RuntimeError("leaked " + TOKEN)

        drive.run_rclone = broken
        out = io.StringIO()
        rc = drive.main(["check"], env=self.env, out=out)
        body = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(body["error"]["code"], "INTERNAL")
        self.assertIn("RuntimeError", body["error"]["message"])
        self.assertIn("<redacted>", body["error"]["message"])
        self.assertNotIn(TOKEN, out.getvalue())

    def test_only_read_and_copy_subcommands_are_used(self):
        self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_SHARE="anyone")))
        self.assertOk(self.run_cli("check"))
        self.assertTrue({c[0] for c in self.fake.calls} <= {"lsjson", "copyto", "link", "about"})

    def test_load_config_secrets(self):
        values = drive.load_config_secrets(self.conf)
        self.assertIn(TOKEN, values)
        self.assertIn(REFRESH, values)
        self.assertIn(CLIENT_SECRET, values)
        self.assertNotIn("drive", values, "short values like the type are not scrubbed")
        self.assertEqual(drive.load_config_secrets(os.path.join(self.data, "missing.conf")), set())


# ----------------------------------------------------------------------------- units


class UnitTests(unittest.TestCase):
    def test_same_file(self):
        self.assertTrue(drive.same_file({"Size": 3, "Hashes": {"md5": "ABC"}}, 3, "abc"))
        self.assertTrue(drive.same_file({"Size": 3}, 3, "abc"))
        self.assertFalse(drive.same_file({"Size": 4, "Hashes": {"md5": "abc"}}, 3, "abc"))
        self.assertFalse(drive.same_file({"Size": 3, "Hashes": {"md5": "def"}}, 3, "abc"))
        self.assertFalse(drive.same_file({}, 3, "abc"))

    def test_file_id(self):
        self.assertEqual(drive._file_id({"ID": "1AbC_-x"}), "1AbC_-x")
        self.assertEqual(drive._file_id({"ID": "target\tshortcut"}), "target")
        self.assertIsNone(drive._file_id({"ID": ""}))
        self.assertIsNone(drive._file_id({"ID": "has space"}))
        self.assertIsNone(drive._file_id({}))
        self.assertIsNone(drive._file_id({"ID": 12}))

    def test_stderr_summary_prefers_the_failure_line(self):
        err = ("2026/09/27 22:00:00 NOTICE: Config file loaded\n"
               "2026/09/27 22:00:01 ERROR : week4.pdf: Failed to copy: googleapi: Error 403: quota\n"
               "Transferred: 0 / 1\n")
        summary = drive._stderr_summary(err)
        self.assertTrue(summary.startswith("ERROR : week4.pdf"), summary)
        self.assertEqual(drive._stderr_summary(""), "")
        self.assertEqual(drive._stderr_summary("just this\n"), "just this")

    def test_is_not_found(self):
        self.assertTrue(drive.is_not_found(3, ""))
        self.assertTrue(drive.is_not_found(1, "Failed to lsjson: directory not found"))
        self.assertFalse(drive.is_not_found(1, "Failed to lsjson: invalid_grant"))

    def test_implied_argv(self):
        self.assertEqual(drive.implied_argv("drive-put", ["a", "b"]), ["put", "a", "b"])
        self.assertEqual(drive.implied_argv("drive-put", ["-v", "a", "b"]), ["-v", "put", "a", "b"])
        self.assertEqual(drive.implied_argv("drive-put", ["put", "a", "b"]), ["put", "a", "b"])
        self.assertEqual(drive.implied_argv("drive", ["put", "a", "b"]), ["put", "a", "b"])

    def test_config_paths(self):
        cfg = drive.Config({"DATA_DIR": "/tmp", "DRIVE_READINGS_ROOT": "gdrive:Readings"})
        self.assertEqual(cfg.remote_path("MAS.665/2026-09-29", "a.pdf"), "gdrive:Readings/MAS.665/2026-09-29/a.pdf")
        self.assertEqual(cfg.display_path(), "Readings")
        self.assertEqual(cfg.remote_path(), "gdrive:Readings")
        self.assertEqual(cfg.config_path, os.path.join(os.path.realpath("/tmp"), "rclone", "rclone.conf"))
        cfg = drive.Config({"DATA_DIR": "/tmp", "DRIVE_READINGS_ROOT": "box:"})
        self.assertEqual(cfg.remote_path("x", "y"), "box:x/y")
        self.assertEqual(cfg.display_path("x", "y"), "x/y")


# ----------------------------------------------------------------------------- real subprocesses


FAKE_RCLONE = r'''#!/bin/sh
# A stand-in rclone for the tests: one "folder" persisted in $FAKE_STATE.
sub=""
for a in "$@"; do
  case "$a" in lsjson|copyto|link|about) sub="$a";; esac
done
last=""; prev=""
for a in "$@"; do prev="$last"; last="$a"; done
case "$sub" in
  lsjson)
    if [ -f "$FAKE_STATE" ]; then
      size=$(wc -c < "$(cat "$FAKE_STATE")" | tr -d ' ')
      printf '[{"Path":"week4.pdf","Name":"week4.pdf","Size":%s,"IsDir":false,"ID":"fake-id-7"}]\n' "$size"
    else
      echo "Failed to lsjson: directory not found" >&2; exit 3
    fi ;;
  copyto) printf '%s' "$prev" > "$FAKE_STATE"; echo "Transferred: 1 / 1" >&2 ;;
  link) echo "https://drive.google.com/open?id=fake-id-7" ;;
  about) echo '{"total": 10, "used": 1, "free": 9}' ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
'''


class CliProcessTests(unittest.TestCase):
    """Real subprocesses: the drive-put symlink (shebang, implied command, exit code, one JSON
    line) and the real rclone runner against a fake rclone script."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.data, "readings", COURSE, DATE))
        os.makedirs(os.path.join(self.data, "rclone"))
        self.conf = os.path.join(self.data, "rclone", "rclone.conf")
        with open(self.conf, "w") as fh:
            fh.write("[gdrive]\ntype = drive\ntoken = {\"access_token\": \"%s\"}\n" % TOKEN)
        self.pdf = os.path.join(self.data, "readings", COURSE, DATE, "week4.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 subprocess reading")
        self.rclone = os.path.join(self.data, "rclone-fake")
        with open(self.rclone, "w") as fh:
            fh.write(FAKE_RCLONE)
        os.chmod(self.rclone, os.stat(self.rclone).st_mode | stat.S_IXUSR)
        self.state = os.path.join(self.data, "fake-state")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not (k.startswith("DRIVE_") or k.startswith("RCLONE"))}
        env.update({"DATA_DIR": self.data, "FAKE_STATE": self.state, "DRIVE_RCLONE_BIN": self.rclone})
        env.update(extra)
        return env

    def _run(self, name, *args, **extra):
        proc = subprocess.run([os.path.join(SCRIPTS, name)] + list(args), env=self._env(**extra),
                              capture_output=True, text=True)
        self.assertEqual(proc.stdout.count("\n"), 1, proc)
        return proc.returncode, json.loads(proc.stdout), proc.stderr

    def test_symlink_reports_usage(self):
        rc, body, _ = self._run("drive-put")
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"]["code"], "USAGE")
        self.assertIn("local_path", body["error"]["message"])

    def test_symlink_refuses_a_file_outside_data_dir(self):
        rc, body, _ = self._run("drive-put", "/etc/hosts", REMOTE_DIR)
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"]["code"], "USAGE")

    def test_put_end_to_end_through_the_fake_rclone(self):
        rc, body, err = self._run("drive-put", "-v", self.pdf, REMOTE_DIR)
        self.assertEqual(rc, 0, (body, err))
        self.assertTrue(body["uploaded"])
        self.assertEqual(body["share_link"], "https://drive.google.com/file/d/fake-id-7/view")
        self.assertEqual(body["remote_path"], "Readings/%s/week4.pdf" % REMOTE_DIR)
        self.assertIn("drive: copyto", err)
        rc, body, _ = self._run("drive-put", self.pdf, REMOTE_DIR)
        self.assertEqual(rc, 0, body)
        self.assertFalse(body["uploaded"])
        rc, body, _ = self._run("drive-put", self.pdf, REMOTE_DIR, DRIVE_SHARE="anyone")
        self.assertEqual(body["share_link"], "https://drive.google.com/open?id=fake-id-7")

    def test_check_through_the_fake_rclone(self):
        rc, body, _ = self._run("drive.py", "check")
        self.assertEqual(rc, 0, body)
        self.assertFalse(body["root_exists"])
        self.assertEqual(body["free_bytes"], 9)

    def test_rclone_missing_is_not_installed(self):
        rc, body, _ = self._run("drive-put", self.pdf, REMOTE_DIR, DRIVE_RCLONE_BIN=os.path.join(self.data, "no-rclone"))
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"]["code"], "DRIVE_NOT_INSTALLED")
        self.assertFalse(body["error"]["retryable"])

    def test_slow_rclone_is_a_retryable_timeout_not_a_hang(self):
        slow = os.path.join(self.data, "rclone-slow")
        with open(slow, "w") as fh:
            fh.write("#!/bin/sh\nsleep 5\n")
        os.chmod(slow, os.stat(slow).st_mode | stat.S_IXUSR)
        cfg = drive.Config({"DATA_DIR": self.data, "DRIVE_RCLONE_BIN": slow})
        with self.assertRaises(drive.DriveError) as ctx:
            drive._run_rclone(cfg, ["lsjson", "gdrive:Readings"], 0.2)
        self.assertEqual(ctx.exception.code, "DRIVE_UNAVAILABLE")
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("DRIVE_TIMEOUT", ctx.exception.message)

    def test_runner_passes_config_and_fail_fast_flags(self):
        echo = os.path.join(self.data, "rclone-echo")
        with open(echo, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        os.chmod(echo, os.stat(echo).st_mode | stat.S_IXUSR)
        cfg = drive.Config({"DATA_DIR": self.data, "DRIVE_RCLONE_BIN": echo})
        rc, out, _ = drive._run_rclone(cfg, ["lsjson", "gdrive:Readings"], 5)
        self.assertEqual(rc, 0)
        argv = out.splitlines()
        self.assertEqual(argv[:2], ["--config", self.conf])
        self.assertIn("--retries", argv)
        self.assertEqual(argv[-2:], ["lsjson", "gdrive:Readings"])


if __name__ == "__main__":
    unittest.main()
