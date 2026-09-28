"""Tests for workspace/skills/pdf-text/scripts/pdf_text.py (SYL-96).

The pdftotext / pypdf hooks in brief.py are stubbed so the built-in extractor runs regardless of
what is installed. No network.

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_SCRIPTS = os.path.join(REPO, "workspace", "skills", "pdf-text", "scripts")
sys.path.insert(0, os.path.join(REPO, "workspace", "skills", "brief", "scripts"))
sys.path.insert(0, PDF_SCRIPTS)

import brief  # noqa: E402
import pdf_text  # noqa: E402


def make_pdf(content=b"BT /F1 12 Tf 72 700 Td (Hello from a PDF) Tj T* [(Sec) -300 (ond line)] TJ ET"):
    comp = zlib.compress(content)
    return (b"%PDF-1.4\n1 0 obj << /Length " + str(len(comp)).encode() + b" /Filter /FlateDecode >>\nstream\n"
            + comp + b"\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF\n")


class PdfTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.realpath(self.tmp.name)
        self.env = {"DATA_DIR": self.data}
        self._orig = (brief._pdftotext, brief._pypdf)
        brief._pdftotext = lambda path: None
        brief._pypdf = lambda path: None
        self.pdf = self.write("readings/MAS.665/2026-09-29/paper.pdf", make_pdf())

    def tearDown(self):
        brief._pdftotext, brief._pypdf = self._orig
        self.tmp.cleanup()

    def write(self, relpath, data):
        path = os.path.join(self.data, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def run_cli(self, *argv):
        out = io.StringIO()
        code = pdf_text.main(list(argv), env=self.env, out=out)
        return code, out.getvalue()

    def err(self, code_expected, *argv):
        code, raw = self.run_cli(*argv)
        self.assertEqual(code, 2, raw)
        body = json.loads(raw)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], code_expected, body)
        return body["error"]

    def test_plain_text_on_stdout(self):
        code, raw = self.run_cli(self.pdf)
        self.assertEqual(code, 0)
        self.assertEqual(raw, "Hello from a PDF\nSec ond line\n")

    def test_json_envelope(self):
        code, raw = self.run_cli(self.pdf, "--json")
        self.assertEqual(code, 0)
        body = json.loads(raw)
        self.assertEqual((body["ok"], body["extractor"], body["warning"]), (True, "builtin", None))
        self.assertEqual(body["text"], "Hello from a PDF\nSec ond line")
        self.assertEqual(body["chars"], len(body["text"]))

    def test_uses_the_brief_extractor_chain(self):
        brief._pdftotext = lambda path: "from pdftotext\n"
        body = json.loads(self.run_cli(self.pdf, "--json")[1])
        self.assertEqual((body["extractor"], body["text"]), ("pdftotext", "from pdftotext\n"))
        brief._pdftotext = lambda path: None
        brief._pypdf = lambda path: "from pypdf"
        self.assertEqual(json.loads(self.run_cli(self.pdf, "--json")[1])["extractor"], "pypdf")

    def test_out_file_under_work(self):
        out = os.path.join(self.data, "work", "MAS.665", "2026-09-29", "paper.txt")
        body = json.loads(self.run_cli(self.pdf, "--json", "--out", out)[1])
        self.assertEqual(body["out"], out)
        self.assertNotIn("text", body)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "Hello from a PDF\nSec ond line")
        self.err("USAGE", self.pdf, "--out", os.path.join(self.data, "memory", "x.txt"))

    def test_scanned_pdf_gets_a_warning(self):
        scan = self.write("readings/MAS.665/2026-09-29/scan.pdf", b"%PDF-1.4\n" + b"\x00" * 30000)
        body = json.loads(self.run_cli(scan, "--json")[1])
        self.assertTrue(body["ok"])
        self.assertIn("very little text", body["warning"])

    def test_errors(self):
        secret = self.write("secrets/notebooklm-cookies.pdf", make_pdf())
        self.err("USAGE", secret)
        outside = self.write("elsewhere/paper.pdf", make_pdf())
        self.err("USAGE", outside, "--json")
        self.err("NOT_FOUND", os.path.join(self.data, "readings", "missing.pdf"))
        login_page = self.write("readings/MAS.665/2026-09-29/login.pdf", b"<html>Please sign in</html>")
        self.err("NOT_PDF", login_page)
        self.err("USAGE")

    def test_symlink_runs_as_a_subprocess(self):
        env = dict(os.environ, DATA_DIR=self.data)
        proc = subprocess.run([sys.executable, os.path.join(PDF_SCRIPTS, "pdf-text"), self.pdf, "--json"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertTrue(body["ok"])
        self.assertIn("Hello from a PDF", body["text"])


if __name__ == "__main__":
    unittest.main()
