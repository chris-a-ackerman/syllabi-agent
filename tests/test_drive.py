"""Tests for workspace/skills/drive/scripts/drive.py (SYL-95).

No network and no rclone: the module's `run_rclone` hook is replaced with a fake that keeps an
in-memory Drive (folders of lsjson-shaped entries) and answers lsjson / copyto / about the
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
REMOTE_DIR = "Readings/%s/%s" % (COURSE, DATE)
FOLDER = "ClassPrep/" + REMOTE_DIR          # the folder as the fake Drive (and drive_path) sees it
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
        self.folders = {}            # "ClassPrep/Readings/MAS.665/2026-09-29" -> [entry, ...]
        self.fail = {}               # subcommand -> (rc, stderr) | Exception
        self.about = {"total": 100, "used": 40, "free": 60}
        self.with_ids = True         # False: a backend without file ids
        self.with_md5 = True         # False: entries carry no hashes
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
            fh.write("[gdrive]\ntype = drive\nscope = drive.file\nclient_secret = %s\ntoken = %s\nteam_drive = \n"
                     % (CLIENT_SECRET, json.dumps({"access_token": TOKEN, "token_type": "Bearer",
                                                   "refresh_token": REFRESH, "expiry": "2026-09-28T00:00:00Z"})))
        os.chmod(self.conf, 0o600)
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
        for remote_dir in ("../x", "Readings/MAS.665/..", "Readings/../x", "a:b", "a\\b", "", "/", "/Readings/x/2026-09-29",
                           "Readings/.hidden/2026-09-29", "Readings/-flag/2026-09-29", "Readings/a/b c",
                           "ClassPrep/../x"):
            err = self.assertError(self.run_cli("put", self.pdf, remote_dir), "USAGE")
            self.assertIn("remote_dir", err["message"] + json.dumps(err.get("detail", {})))
        self.assertEqual(self.fake.calls, [])

    def test_remote_dir_is_normalised(self):
        for spelled in ("Readings/MAS.665/2026-09-29/", "ClassPrep/Readings/MAS.665/2026-09-29"):
            self.fake.calls = []
            body = self.assertOk(self.run_cli("put", self.pdf, spelled))
            self.assertEqual(body["drive_path"], "%s/week4.pdf" % FOLDER, spelled)
            self.assertEqual(self.fake.ops("lsjson")[0][1][-1], "gdrive:" + FOLDER, spelled)

    def test_remote_dir_must_follow_the_layout(self):
        """SYL-95: ClassPrep/Readings/<course>/<date>/ and ClassPrep/Podcasts/, nothing else."""
        for remote_dir in ("MAS.665/2026-09-29", "Readings/MAS.665", "Readings/MAS.665/sept-29",
                           "Readings/MAS.665/2026-09-29/extra", "Podcasts/MAS.665", "Other/x"):
            err = self.assertError(self.run_cli("put", self.pdf, remote_dir), "USAGE")
            self.assertIn("remote_dir", json.dumps(err))
        self.assertEqual(self.fake.calls, [])

    def test_bad_environment(self):
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_ROOT="ClassPrep")), "USAGE")
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_ROOT="gdrive:../x")), "USAGE")
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="soon")), "USAGE")
        self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="0")), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_missing_rclone_config_is_auth_not_a_crash(self):
        missing = os.path.join(self.data, "rclone", "nope.conf")
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, RCLONE_CONFIG=missing)), "DRIVE_AUTH")
        self.assertFalse(err["retryable"])
        self.assertIn(missing, err["message"])
        self.assertIn("drive.file", err["message"])
        self.assertEqual(self.fake.calls, [])

    def test_world_readable_rclone_config_is_refused(self):
        """SYL-95 Security: rclone.conf holds a refresh token and must be chmod 600."""
        for mode in (0o644, 0o640, 0o604):
            os.chmod(self.conf, mode)
            err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_AUTH")
            self.assertIn("chmod 600", err["message"])
            self.assertEqual(err["detail"]["mode"], "%03o" % mode)
        self.assertEqual(self.fake.calls, [])
        os.chmod(self.conf, 0o600)
        self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))

    def test_root_without_folder_and_custom_root(self):
        env = dict(self.env, DRIVE_ROOT="box:")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=env))
        self.assertEqual(body["drive_path"], "%s/week4.pdf" % REMOTE_DIR)
        self.assertEqual(self.fake.ops("copyto")[0][1][-1], "box:%s/week4.pdf" % REMOTE_DIR)
        env = dict(self.env, DRIVE_ROOT="gdrive:School/ClassPrep/")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=env))
        self.assertEqual(body["drive_path"], "School/ClassPrep/%s/week4.pdf" % REMOTE_DIR)


# ----------------------------------------------------------------------------- put


class PutTests(DriveTestCase):
    def test_first_upload(self):
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertTrue(body["uploaded"])
        self.assertEqual(body["drive_path"], "%s/week4.pdf" % FOLDER)
        self.assertEqual(body["bytes"], len(self.pdf_bytes))
        self.assertEqual(body["md5"], md5(self.pdf_bytes))
        self.assertEqual(body["drive_path"], "ClassPrep/Readings/%s/%s/week4.pdf" % (COURSE, DATE))
        self.assertEqual(body["web_url"], "https://drive.google.com/file/d/%s/view" % body["file_id"])
        self.assertEqual(body["local_path"], self.pdf)
        self.assertNotIn("share", body)
        self.assertEqual([c[0] for c in self.fake.calls], ["lsjson", "copyto", "lsjson"],
                         "list, upload, list again for the id; never rclone link")
        copyto = self.fake.ops("copyto")[0][1]
        self.assertEqual(copyto[-2:], [self.pdf, "gdrive:%s/week4.pdf" % FOLDER])
        self.assertIn("--ignore-times", copyto)
        lsjson = self.fake.ops("lsjson")[0][1]
        for flag in ("--files-only", "--hash", "--no-modtime"):
            self.assertIn(flag, lsjson)

    def test_second_run_is_idempotent(self):
        first = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.fake.calls = []
        second = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertFalse(second["uploaded"])
        self.assertEqual(second["web_url"], first["web_url"])
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
        self.fake.seed(FOLDER, "week4.pdf", other)
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertTrue(body["uploaded"])

    def test_same_size_and_no_remote_hash_counts_as_same(self):
        self.fake.with_md5 = False
        self.fake.seed(FOLDER, "week4.pdf", b"x" * len(self.pdf_bytes), file_id="abc123")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertFalse(body["uploaded"])
        self.assertEqual(body["file_id"], "abc123")
        self.assertEqual(self.fake.ops("copyto"), [])

    def test_drive_duplicates_prefer_the_matching_hash(self):
        self.fake.seed(FOLDER, "week4.pdf", b"an older copy", file_id="old")
        self.fake.seed(FOLDER, "week4.pdf", self.pdf_bytes, file_id="good")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertFalse(body["uploaded"])
        self.assertEqual(body["file_id"], "good")

    def test_other_files_in_the_folder_are_ignored(self):
        self.fake.seed(FOLDER, "week3.pdf", b"another reading")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertTrue(body["uploaded"])
        self.assertEqual(len(self.fake.folders[FOLDER]), 2)

    def test_aliases_for_the_old_output_keys(self):
        """remote_path / share_link stay until nlm (PR #3) reads drive_path / web_url."""
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertEqual(body["remote_path"], body["drive_path"])
        self.assertEqual(body["share_link"], body["web_url"])

    def test_rclone_link_is_never_invoked(self):
        """SYL-95: no anyone-with-the-link sharing, whatever the environment says."""
        env = dict(self.env, DRIVE_SHARE="anyone")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=env))
        self.assertTrue(body["web_url"].startswith("https://drive.google.com/file/d/"))
        self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=env))
        self.assertOk(self.run_cli("ls", REMOTE_DIR, env=env))
        self.assertOk(self.run_cli("check", env=env))
        self.assertEqual(self.fake.ops("link"), [])
        self.assertTrue({c[0] for c in self.fake.calls} <= {"lsjson", "copyto", "about"})
        with open(os.path.join(SCRIPTS, "drive.py")) as fh:
            source = fh.read()
        self.assertNotIn('"link"', source, "no code path may build an `rclone link` command")
        self.assertNotIn("DRIVE_SHARE", source)

    def test_missing_file_id_is_drive_net(self):
        self.fake.with_ids = False
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_NET")
        self.assertTrue(err["retryable"])
        self.assertEqual(len(self.fake.ops("copyto")), 1, "the upload itself happened; rerunning is idempotent")
        self.fake.with_ids = True
        for entry in self.fake.folders[FOLDER]:
            entry["ID"] = "late-id"
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertFalse(body["uploaded"])
        self.assertEqual(body["web_url"], "https://drive.google.com/file/d/late-id/view")

    def test_shortcut_id_uses_the_target(self):
        self.fake.seed(FOLDER, "week4.pdf", self.pdf_bytes, file_id="target-id\tshortcut-id")
        body = self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertEqual(body["file_id"], "target-id")
        self.assertEqual(body["web_url"], "https://drive.google.com/file/d/target-id/view")

    def test_unsafe_id_from_drive_is_not_put_in_a_url(self):
        self.fake.seed(FOLDER, "week4.pdf", self.pdf_bytes, file_id="abc/../../evil?x=1")
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_NET")
        self.assertNotIn("evil", json.dumps(err))

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
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_NET")
        self.assertTrue(err["retryable"])
        self.assertIn("not listed", err["message"])

    def test_size_mismatch_after_upload_is_retryable(self):
        real = self.fake.__call__

        def truncating(cfg, args, timeout):
            rc, out, err = real(cfg, args, timeout)
            if args[0] == "copyto":
                self.fake.folders[FOLDER][0]["Size"] = 3
            return rc, out, err

        drive.run_rclone = truncating
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_NET")
        self.assertTrue(err["retryable"])
        self.assertIn("size mismatch", err["message"])

    def test_hash_mismatch_after_upload_is_retryable(self):
        real = self.fake.__call__

        def corrupting(cfg, args, timeout):
            rc, out, err = real(cfg, args, timeout)
            if args[0] == "copyto":
                self.fake.folders[FOLDER][0]["Hashes"] = {"md5": "0" * 32}
            return rc, out, err

        drive.run_rclone = corrupting
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_NET")
        self.assertIn("hash mismatch", err["message"])

    def test_lsjson_garbage_is_unavailable(self):
        real = self.fake.__call__

        def garbage(cfg, args, timeout):
            if args[0] == "lsjson":
                return 0, "<html>login</html>", ""
            return real(cfg, args, timeout)

        drive.run_rclone = garbage
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR), "DRIVE_NET")
        self.assertTrue(err["retryable"])
        self.assertIn("not JSON", err["message"])

    def test_podcast_with_a_remote_name(self):
        """nlm-status: /data/podcasts/<course>-<date>.mp3 → ClassPrep/Podcasts/<course>-<date>.mp3."""
        folder = os.path.join(self.data, "podcasts")
        os.makedirs(folder)
        audio = self._file("audio-overview.mp3", b"ID3\x04" + b"\x00" * 32, folder=folder)
        body = self.assertOk(self.run_cli("put", audio, "Podcasts", "%s-%s.mp3" % (COURSE, DATE), prog="drive-put"))
        self.assertEqual(body["drive_path"], "ClassPrep/Podcasts/%s-%s.mp3" % (COURSE, DATE))
        self.assertEqual(self.fake.ops("copyto")[0][1][-2:], [audio, "gdrive:ClassPrep/Podcasts/%s-%s.mp3" % (COURSE, DATE)])
        self.assertTrue(body["web_url"].startswith("https://drive.google.com/file/d/"))
        self.fake.calls = []
        again = self.assertOk(self.run_cli("put", audio, "Podcasts", "%s-%s.mp3" % (COURSE, DATE), prog="drive-put"))
        self.assertFalse(again["uploaded"])
        self.assertEqual(again["web_url"], body["web_url"])

    def test_podcast_named_by_its_local_file(self):
        folder = os.path.join(self.data, "podcasts")
        os.makedirs(folder)
        audio = self._file("%s-%s.mp3" % (COURSE, DATE), b"ID3\x04" + b"\x00" * 32, folder=folder)
        body = self.assertOk(self.run_cli("put", audio, "Podcasts"))
        self.assertEqual(body["drive_path"], "ClassPrep/Podcasts/%s-%s.mp3" % (COURSE, DATE))

    def test_unsafe_remote_names(self):
        for name in ("../x.mp3", "a/b.mp3", ".hidden.mp3", "we;rd.mp3", "a:b.mp3", " x.mp3"):
            err = self.assertError(self.run_cli("put", self.pdf, "Podcasts", name), "USAGE")
            self.assertIn("remote_name", err["message"] + json.dumps(err.get("detail", {})), name)
        self.assertError(self.run_cli("put", self.pdf, "Podcasts", "-x.mp3"), "USAGE")   # argparse: not a flag we know
        self.assertEqual(self.fake.calls, [])


# ----------------------------------------------------------------------------- error mapping


class ErrorMappingTests(DriveTestCase):
    def _fail_copy(self, rc, stderr):
        self.fake.fail["copyto"] = (rc, stderr)
        return self.run_cli("put", self.pdf, REMOTE_DIR)

    def test_token_refresh_timeout_is_network_not_auth(self):
        _, body, _ = self._fail_copy(1, "Failed to copyto: couldn't fetch token: Post \"https://oauth2.googleapis.com/token\": "
                                        "dial tcp: i/o timeout\n")
        self.assertEqual(body["error"]["code"], "DRIVE_NET")
        self.assertTrue(body["error"]["retryable"])

    def test_invalid_grant_on_a_digit_heavy_path_is_auth(self):
        """Course numbers and dates are full of 3-digit runs (15.515, 15.401); they are not HTTP statuses."""
        remote_dir = "Readings/15.515/2026-05-15"
        stderr = ('2026/09/27 22:00:00 ERROR : Readings/15.515/2026-05-15/week4.pdf: Failed to copy: '
                  'couldn\'t fetch token: invalid_grant: oauth2: "invalid_grant" "Token has been expired or revoked."\n')
        self.fake.fail["copyto"] = (1, stderr)
        err = self.assertError(self.run_cli("put", self.pdf, remote_dir), "DRIVE_AUTH")
        self.assertFalse(err["retryable"])
        self.assertNotIn("status", err, "no HTTP status in that text")
        self.fake.fail["copyto"] = (1, "Failed to copy 15.401/2026-05-15/lecture 500.pdf: some odd failure\n")
        err = self.assertError(self.run_cli("put", self.pdf, "Readings/15.401/2026-05-15"), "DRIVE_NET")
        self.assertNotIn("status", err, "500 in a file name is not a 5xx")
        self.fake.fail["copyto"] = (1, "Failed to copy 15.401/week4.pdf: oauth2: cannot fetch token: 400 Bad Request\n")
        self.assertError(self.run_cli("put", self.pdf, "Readings/15.401/2026-05-15"), "DRIVE_AUTH")

    def test_insufficient_file_permissions_is_auth(self):
        """drive.file scope: a folder the agent did not create gives 403 insufficientFilePermissions."""
        _, body, _ = self._fail_copy(1, "ERROR : week4.pdf: Failed to copy: googleapi: Error 403: The user does not "
                                        "have sufficient permissions for this file., insufficientFilePermissions\n")
        err = body["error"]
        self.assertEqual(err["code"], "DRIVE_AUTH")
        self.assertFalse(err["retryable"])
        self.assertEqual(err["status"], 403)
        self.assertEqual(err["detail"]["reason"], "insufficientFilePermissions")
        self.assertIn("drive.file", err["message"])
        self.fake.fail["lsjson"] = (1, "Failed to lsjson: googleapi: Error 403: Insufficient Permission: Request had "
                                       "insufficient authentication scopes., insufficientPermissions\n")
        self.assertError(self.run_cli("ls", REMOTE_DIR), "DRIVE_AUTH")

    def test_expired_token_is_auth(self):
        rc, body, _ = self._fail_copy(1, '2026/09/27 22:00:00 Failed to copyto: couldn\'t fetch token: invalid_grant: '
                                         'oauth2: "invalid_grant" "Token has been expired or revoked."\n')
        self.assertEqual(rc, 2)
        err = body["error"]
        self.assertEqual(err["code"], "DRIVE_AUTH")
        self.assertFalse(err["retryable"])
        self.assertIn('rclone authorize "drive"', err["message"])
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
                           (1, "Failed to copyto: googleapi: Error 502: Bad Gateway, badGateway\n"),
                           (1, "Failed to copyto: read tcp: i/o timeout\n"),
                           (5, "")):
            _, body, _ = self._fail_copy(rc, stderr)
            self.assertEqual(body["error"]["code"], "DRIVE_NET", stderr)
            self.assertTrue(body["error"]["retryable"], stderr)

    def test_fatal_and_usage_exits_are_not_retryable(self):
        _, body, _ = self._fail_copy(7, "Fatal error: account suspended\n")
        self.assertEqual(body["error"]["code"], "DRIVE_NET")
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

    def test_out_of_time_before_a_call(self):
        err = self.assertError(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="0.5")), "DRIVE_NET")
        self.assertTrue(err["retryable"])
        self.assertIn("out of time", err["message"])
        self.assertEqual(self.fake.calls, [])

    def test_each_call_gets_at_most_the_remaining_time(self):
        self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR, env=dict(self.env, DRIVE_TIMEOUT="20")))
        for op, _, timeout in self.fake.calls:
            self.assertLessEqual(timeout, 20, op)
            self.assertGreater(timeout, 0, op)
        self.assertLessEqual(self.fake.ops("lsjson")[0][2], drive.LIST_TIMEOUT)


# ----------------------------------------------------------------------------- ls


class LsTests(DriveTestCase):
    def test_ls_lists_a_folder(self):
        self.fake.seed(FOLDER, "week4.pdf", self.pdf_bytes, file_id="f1")
        self.fake.folders[FOLDER].append({"Path": "notes", "Name": "notes", "Size": -1, "IsDir": True, "ID": "d1",
                                          "MimeType": "inode/directory"})
        body = self.assertOk(self.run_cli("ls", REMOTE_DIR))
        self.assertEqual(body["drive_path"], FOLDER)
        self.assertTrue(body["exists"])
        files = {e["name"]: e for e in body["entries"]}
        self.assertEqual(files["week4.pdf"]["web_url"], "https://drive.google.com/file/d/f1/view")
        self.assertEqual(files["week4.pdf"]["bytes"], len(self.pdf_bytes))
        self.assertEqual(files["week4.pdf"]["drive_path"], FOLDER + "/week4.pdf")
        self.assertEqual(files["notes"]["web_url"], "https://drive.google.com/drive/folders/d1")
        self.assertTrue(files["notes"]["is_dir"])
        self.assertEqual(self.fake.ops("lsjson")[0][1][-1], "gdrive:" + FOLDER)

    def test_ls_partial_paths_and_root(self):
        self.fake.folders["ClassPrep/Readings"] = [{"Name": COURSE, "IsDir": True, "ID": "c1"}]
        self.assertEqual(self.assertOk(self.run_cli("ls", "Readings"))["entries"][0]["name"], COURSE)
        self.fake.folders["ClassPrep"] = [{"Name": "Readings", "IsDir": True, "ID": "r1"}]
        self.assertEqual(self.assertOk(self.run_cli("ls"))["drive_path"], "ClassPrep")
        self.assertOk(self.run_cli("ls", "Podcasts"))

    def test_ls_missing_folder(self):
        body = self.assertOk(self.run_cli("ls", REMOTE_DIR))
        self.assertFalse(body["exists"])
        self.assertEqual(body["entries"], [])

    def test_ls_is_sanitized(self):
        for remote_dir in ("../x", "Readings/..", "/etc", "a:b", "Other", "Readings/.hidden"):
            self.assertError(self.run_cli("ls", remote_dir), "USAGE")
        self.assertEqual(self.fake.calls, [])

    def test_ls_unsafe_ids_get_no_url(self):
        self.fake.folders[FOLDER] = [{"Name": "x.pdf", "IsDir": False, "Size": 1, "ID": "../evil?x"}]
        entry = self.assertOk(self.run_cli("ls", REMOTE_DIR))["entries"][0]
        self.assertIsNone(entry["web_url"])
        self.assertIsNone(entry["file_id"])

    def test_ls_auth_failure(self):
        self.fake.fail["lsjson"] = (1, "Failed to lsjson: couldn't fetch token: invalid_grant\n")
        self.assertError(self.run_cli("ls", REMOTE_DIR), "DRIVE_AUTH")


# ----------------------------------------------------------------------------- check


class CheckTests(DriveTestCase):
    def test_check_reports_root_and_quota(self):
        self.fake.folders["ClassPrep"] = [{"Name": "Readings", "IsDir": True, "Size": -1}]
        body = self.assertOk(self.run_cli("check"))
        self.assertEqual(body["root"], "gdrive:ClassPrep")
        self.assertTrue(body["root_exists"])
        self.assertEqual(body["entries"], 1)
        self.assertEqual(body["used_bytes"], 40)
        self.assertEqual(body["free_bytes"], 60)
        self.assertEqual(body["config_path"], self.conf)
        self.assertNotIn("share", body)
        self.assertEqual([c[0] for c in self.fake.calls], ["lsjson", "about"])
        self.assertEqual(self.fake.ops("about")[0][1][-1], "gdrive:")

    def test_check_with_missing_root(self):
        body = self.assertOk(self.run_cli("check"))
        self.assertFalse(body["root_exists"])
        self.assertIsNone(body["entries"])

    def test_check_without_about_support(self):
        self.fake.folders["ClassPrep"] = []
        self.fake.fail["about"] = (1, "Failed to about: about not supported\n")
        body = self.assertOk(self.run_cli("check"))
        self.assertTrue(body["root_exists"])
        self.assertNotIn("used_bytes", body)

    def test_check_auth_failure(self):
        self.fake.fail["lsjson"] = (1, "Failed to lsjson: couldn't fetch token: invalid_grant\n")
        self.assertError(self.run_cli("check"), "DRIVE_AUTH")

    def test_check_symlink_name_implies_command(self):
        self.fake.folders["ClassPrep"] = []
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
        self.assertIn("lsjson %s" % FOLDER, err.getvalue())

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
        self.assertOk(self.run_cli("put", self.pdf, REMOTE_DIR))
        self.assertOk(self.run_cli("ls", REMOTE_DIR))
        self.assertOk(self.run_cli("check"))
        self.assertTrue({c[0] for c in self.fake.calls} <= {"lsjson", "copyto", "about"})

    def test_load_config_secrets(self):
        values = drive.load_config_secrets(self.conf)
        self.assertNotIn("drive.file", values, "the scope is not a secret; messages name it")
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
        cfg = drive.Config({"DATA_DIR": "/tmp"})
        self.assertEqual(cfg.remote_path("Readings/MAS.665/2026-09-29", "a.pdf"),
                         "gdrive:ClassPrep/Readings/MAS.665/2026-09-29/a.pdf")
        self.assertEqual(cfg.display_path(), "ClassPrep")
        self.assertEqual(cfg.remote_path(), "gdrive:ClassPrep")
        self.assertEqual(cfg.config_path, os.path.join(os.path.realpath("/tmp"), "rclone", "rclone.conf"))
        cfg = drive.Config({"DATA_DIR": "/tmp", "DRIVE_ROOT": "box:"})
        self.assertEqual(cfg.remote_path("x", "y"), "box:x/y")
        self.assertEqual(cfg.display_path("x", "y"), "x/y")


# ----------------------------------------------------------------------------- real subprocesses


FAKE_RCLONE = r'''#!/bin/sh
# A stand-in rclone for the tests: one "folder" persisted in $FAKE_STATE.
sub=""
for a in "$@"; do
  case "$a" in lsjson|copyto|about) sub="$a";; esac
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
            fh.write("[gdrive]\ntype = drive\nscope = drive.file\ntoken = {\"access_token\": \"%s\"}\n" % TOKEN)
        os.chmod(self.conf, 0o600)
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
        self.assertEqual(body["web_url"], "https://drive.google.com/file/d/fake-id-7/view")
        self.assertEqual(body["drive_path"], "%s/week4.pdf" % FOLDER)
        self.assertIn("drive: copyto", err)
        rc, body, _ = self._run("drive-put", self.pdf, REMOTE_DIR)
        self.assertEqual(rc, 0, body)
        self.assertFalse(body["uploaded"])

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
            drive._run_rclone(cfg, ["lsjson", "gdrive:ClassPrep"], 0.2)
        self.assertEqual(ctx.exception.code, "DRIVE_NET")
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("DRIVE_TIMEOUT", ctx.exception.message)

    def test_runner_passes_config_and_fail_fast_flags(self):
        echo = os.path.join(self.data, "rclone-echo")
        with open(echo, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        os.chmod(echo, os.stat(echo).st_mode | stat.S_IXUSR)
        cfg = drive.Config({"DATA_DIR": self.data, "DRIVE_RCLONE_BIN": echo})
        rc, out, _ = drive._run_rclone(cfg, ["lsjson", "gdrive:ClassPrep"], 5)
        self.assertEqual(rc, 0)
        argv = out.splitlines()
        self.assertEqual(argv[:2], ["--config", self.conf])
        self.assertIn("--retries", argv)
        self.assertEqual(argv[-2:], ["lsjson", "gdrive:ClassPrep"])


if __name__ == "__main__":
    unittest.main()
