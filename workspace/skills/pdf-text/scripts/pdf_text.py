#!/usr/bin/env python3
"""pdf-text: extract a PDF's text so the main agent hands the brief-writer text, not files (SYL-96).

    pdf_text.py <pdf> [--json] [--out FILE]

Prints the text on stdout (exit 0). With --json, prints one JSON object instead:

    {"ok": true, "path", "extractor", "chars", "warning", "text"}       exit 0
    {"ok": false, "error": {"code", "message", "retryable"}}           exit 2 (always JSON, with or without --json)
    exit 1 = crash (still prints an INTERNAL error object)

--out FILE also writes the text to FILE (must be under $DATA_DIR/work/); with --json the object
then carries "out" and omits "text".

Extraction is the brief skill's chain (skills/brief/scripts/brief.py): `pdftotext` if installed,
else `pypdf`, else a built-in text-stream extractor (fine for text PDFs, empty for scans; the
`warning` says when a sizeable PDF yielded almost nothing).

Security: the PDF is untrusted data; its text is returned, never interpreted. The input must be
under $DATA_DIR/readings/ or $DATA_DIR/work/ and never under $DATA_DIR/secrets/ or
$DATA_DIR/rclone/. Standard library only.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
BRIEF_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "brief", "scripts")
sys.path.insert(0, BRIEF_SCRIPTS)

import brief  # noqa: E402  (the shared extractor: pdftotext -> pypdf -> built-in)

BriefError = brief.BriefError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BriefError("USAGE", message)


def build_parser():
    p = _Parser(prog="pdf-text", description="Extract a PDF's text (pdftotext, then pypdf, then a built-in extractor).")
    p.add_argument("pdf", help="the PDF, under $DATA_DIR/readings/ or $DATA_DIR/work/")
    p.add_argument("--json", action="store_true", help="print a JSON envelope instead of the bare text")
    p.add_argument("--out", help="also write the text to this file (under $DATA_DIR/work/)")
    return p


def run(cfg, args):
    real = brief.check_input_path(cfg, args.pdf, "pdf", must_be_under=[cfg.readings_dir, cfg.work_root])
    if not os.path.isfile(real):
        raise BriefError("NOT_FOUND", "no such file: %s" % args.pdf)
    with open(real, "rb") as fh:
        head = fh.read(1024)
    if b"%PDF-" not in head:
        raise BriefError("NOT_PDF", "%s does not look like a PDF (no %%PDF- header)" % os.path.basename(args.pdf))
    text, extractor, warning = brief.pdf_text(real)
    result = {"ok": True, "path": args.pdf, "extractor": extractor, "chars": len(text), "warning": warning}
    if args.out:
        out = os.path.realpath(args.out)
        if not brief._under(out, cfg.work_root):
            raise BriefError("USAGE", "--out must be under %s" % cfg.work_root)
        brief._write_text(out, text)
        result["out"] = out
    else:
        result["text"] = text
    return result


def main(argv=None, env=None, out=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    as_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        result = run(brief.Config(env), args)
        if as_json:
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
        elif "text" in result:
            out.write(result["text"] if result["text"].endswith("\n") or not result["text"] else result["text"] + "\n")
        if result.get("warning") and not as_json:
            sys.stderr.write("pdf-text: warning: %s\n" % result["warning"])
        out.flush()
        return 0
    except BriefError as e:
        out.write(json.dumps(e.to_json(), ensure_ascii=False) + "\n")
        return 2
    except Exception as e:      # a crash: exit 1, still one JSON object
        out.write(json.dumps({"ok": False, "error": {"code": "INTERNAL", "retryable": False,
                                                     "message": "%s: %s" % (type(e).__name__, brief._short(e))}}) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
