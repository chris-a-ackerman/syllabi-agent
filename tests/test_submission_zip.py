"""Tests for scripts/make-submission-zip.sh (SYL-109): the ZIP is HEAD only and never holds a secret.

Each test builds a throwaway git repo with a copy of the script, so the real repo and its dist/
are never touched. Needs git and sh; no network.

    python3 -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "make-submission-zip.sh")
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "needs git and sh")
class SubmissionZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.zip = os.path.join(self.root, "dist", "syllabi-agent-hw3.zip")
        subprocess.run(GIT + ["init", "-q", self.root], check=True)
        os.makedirs(os.path.join(self.root, "scripts"))
        shutil.copy(SCRIPT, os.path.join(self.root, "scripts"))
        self.write("README.md", "# test\n")
        self.write(".env.example", "CANVAS_FORUM_TOKEN=\nSYLLABI_AGENT_TOKEN=  # syl_agent_ + 43 characters\n")
        self.write(".gitignore", "dist/\n")
        self.commit()

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def commit(self, *force_add):
        if force_add:
            subprocess.run(GIT + ["-C", self.root, "add", "-f"] + list(force_add), check=True)
        subprocess.run(GIT + ["-C", self.root, "add", "-A"], check=True)
        subprocess.run(GIT + ["-C", self.root, "commit", "-q", "-m", "x"], check=True)

    def build(self):
        return subprocess.run(["sh", os.path.join(self.root, "scripts", "make-submission-zip.sh")],
                              cwd=self.root, capture_output=True, text=True, timeout=60)

    def test_clean_repo_builds_a_zip_of_tracked_files(self):
        self.write("notes.txt", "untracked, not in the zip\n")
        res = self.build()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("4 files", res.stdout)
        self.assertIn("dist/syllabi-agent-hw3.zip", res.stdout)
        with zipfile.ZipFile(self.zip) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith("/"))
        self.assertEqual(names, ["syllabi-agent-hw3/.env.example", "syllabi-agent-hw3/.gitignore",
                                 "syllabi-agent-hw3/README.md",
                                 "syllabi-agent-hw3/scripts/make-submission-zip.sh"])

    def test_planted_tokens_are_refused_and_not_echoed(self):
        for planted in ("syl_agent_" + "Q" * 43, "mk_" + "Z" * 30, "Bearer " + "7867~abcdefghijklmnopqrstu"):
            with self.subTest(planted=planted[:12]):
                self.write("docs/evidence.md", "line one\nAuthorization: %s\n" % planted)
                self.commit()
                res = self.build()
                self.assertEqual(res.returncode, 1)
                self.assertIn("REFUSED", res.stderr)
                self.assertIn("docs/evidence.md:2", res.stderr)
                self.assertNotIn(planted, res.stdout + res.stderr)
                self.assertFalse(os.path.exists(self.zip))
                os.remove(os.path.join(self.root, "docs", "evidence.md"))
                self.commit()

    def test_forbidden_files_are_refused(self):
        for rel in (".env", "config/.env.local", "memory/forum-state.json", "logs/forum.jsonl",
                    "memory/prep-log.json", "rclone/rclone.conf", "storage_state.json",
                    "notebooklm/master_token.json"):
            with self.subTest(rel=rel):
                self.write(rel, "{}\n")
                self.commit(rel)
                res = self.build()
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertIn("%s: forbidden file" % rel, res.stderr)
                self.assertFalse(os.path.exists(self.zip))
                subprocess.run(GIT + ["-C", self.root, "rm", "-q", rel], check=True)
                self.commit()

    def test_uncommitted_token_is_not_in_the_zip(self):
        self.write("README.md", "# test\nBearer %s\n" % ("x" * 30))
        res = self.build()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("uncommitted", res.stderr)
        with zipfile.ZipFile(self.zip) as zf:
            self.assertEqual(zf.read("syllabi-agent-hw3/README.md"), b"# test\n")

    def test_the_real_repo_head_passes_the_scan(self):
        """This repo's tracked files must not trip the guard (e.g. a test fixture shaped like a token)."""
        res = subprocess.run(GIT + ["-C", REPO, "grep", "-nIE",
                                    r"syl_agent_[A-Za-z0-9_-]{20,}|mk_[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9~_-]{20,}"],
                             capture_output=True, text=True)
        self.assertEqual(res.stdout, "")


if __name__ == "__main__":
    unittest.main()
