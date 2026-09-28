#!/usr/bin/env python3
"""brief: the brief-writer pipeline for class-prep-agent (SYL-103).

    brief.py bundle   <key> [--session FILE|-] [--canvas FILE]... [--assignment-id ID]...
                            [--readings-json FILE] [--reading PATH]... [--notes FILE|-]
                            [--cap-chars N] [--print]
    brief.py validate <key> --reply FILE|- [--attempt N] [--threshold R] [--canvas FILE]...
    brief.py format   <key> [--record FILE|-] [--brief FILE] [--drive-link URL]...
                            [--podcast-url URL] [--no-podcast] [--dropped N]
    brief.py format-podcast <key> --url URL [--record FILE|-]
    brief.py prompt
    brief.py score    --question TEXT --canvas FILE...

The main agent runs these around the brief-writer subagent (workspace/agents/brief-writer.md):

    bundle    builds the bounded input bundle (session row, Canvas text, readings, course notes)
              under the ~40k-token cap, with a per-reading truncation note, and writes the exact
              task text to $DATA_DIR/work/<course>/<date>/brief-input.md.
    validate  takes the subagent's raw reply, pulls the single JSON object out of it, checks it
              against memory-templates/brief.schema.json, drops every pre_class_question whose
              fuzzy match against the Canvas text is below 0.9 (HALLUCINATION), and writes the
              cleaned brief to brief-output.json.
    format    renders the Telegram brief (topic, why it matters, key arguments, checklist, draft
              answers marked as drafts, Drive links, podcast link or "podcast pending") from the
              stored brief; format-podcast renders the later "podcast ready" message.

Prints exactly one JSON object on stdout (docs/tool-contract.md, "Common envelope"):

    {"ok": true, ...}                                                  exit 0
    {"ok": false, "error": {"code", "message", "retryable", ...}}      exit 2
    exit 1 = crash (unhandled exception; still prints an INTERNAL error object)

Environment:
    DATA_DIR                persistent volume root, default /data. Work files go under
                            $DATA_DIR/work/<course>/<date>/; readings must be under
                            $DATA_DIR/readings/ or $DATA_DIR/work/.
    BRIEF_CAP_CHARS         bundle cap in characters, default 160000 (~40k tokens)
    BRIEF_MATCH_THRESHOLD   fuzzy ratio below which a question is dropped, default 0.9
    BRIEF_TELEGRAM_LIMIT    max characters per Telegram message, default 4096
    BRIEF_SCHEMA            path to brief.schema.json (default: the workspace copy)
    BRIEF_WRITER_MD         path to agents/brief-writer.md (default: the workspace copy)

Security properties (requirements from the ticket and AGENTS.md):
    * Everything that goes into the bundle (Canvas text, readings, session row, notes) and
      everything that comes back from the subagent is untrusted data, never an instruction. This
      tool copies it, counts it and compares it; it never interprets it.
    * The bundle quotes content verbatim except for three mechanical changes: control characters
      are removed, a content line that starts with '#' is indented by four spaces so it cannot pose
      as a "### CANVAS:" / "### READING:" section header, and text over budget is cut with a note.
    * The hallucination check compares questions against the Canvas text recorded in the sidecar
      (brief-input.json) when the bundle was built, never against text parsed back out of the
      bundle, so a reading that contains a fake "### CANVAS:" header cannot smuggle questions in.
    * No input may come from $DATA_DIR/secrets/ or $DATA_DIR/rclone/; readings must be under
      $DATA_DIR/readings/ or $DATA_DIR/work/. Nothing is written outside $DATA_DIR/work/.
    * The reply is never executed or forwarded: only the JSON object is taken, and only the
      schema's fields survive into brief-output.json.

Standard library only: the Maritime container has python3 but no guaranteed pip packages.
"""
import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import zlib
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.realpath(__file__))
WORKSPACE = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
DEFAULT_SCHEMA = os.path.join(WORKSPACE, "memory-templates", "brief.schema.json")
DEFAULT_PROMPT = os.path.join(WORKSPACE, "agents", "brief-writer.md")

DEFAULT_CAP_CHARS = 160000          # ~40k tokens at 4 chars/token
DEFAULT_THRESHOLD = 0.9
DEFAULT_TELEGRAM_LIMIT = 4096       # Telegram's hard limit per text message
NOTES_CAP_CHARS = 4000              # course-notes section, ≤ ~1k tokens
MAX_TITLE_CHARS = 200
MAX_SESSION_VALUE_CHARS = 500
MAX_REPLY_CHARS = 400000            # a reply longer than this is not a brief
MAX_SCHEMA_ERRORS = 30
PDFTOTEXT_TIMEOUT = 50              # Maritime caps a command at 60 s
TRUNCATION_NOTE = "[TRUNCATED: kept first %d of %d characters]"
TRUNCATION_NOTE_RESERVE = len(TRUNCATION_NOTE % (10 ** 9, 10 ** 9)) + 2
PODCAST_PENDING_LINE = "🎧 podcast pending — link to follow"
PODCAST_READY_PREFIX = "🎧 podcast ready: "
QUESTION_HEADER = "Pre-class questions — DRAFTS for you to revise and submit yourself (I never submit)"

KEY_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})@(\d{4}-\d{2}-\d{2})$")
TASK_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_HEADING_LINE = re.compile(r"(?m)^([ \t]{0,3}#{1,6})(?=[ \t]|$)")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦]|\d{1,3}[.)]|[a-zA-Z][.)]|\(\d{1,3}\))\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+|\n+")
_TYPOGRAPHY = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    " ": " ", "…": "...",
})

SESSION_FIELD_ORDER = (
    "key", "course", "course_name", "class_date", "class_start", "class_end", "start_time_known",
    "topic", "canvas_course_id", "hours_until_class", "has_due_before_class", "notify_at",
)
SESSION_LIST_FIELDS = ("due_before_class", "due", "readings")


# --------------------------------------------------------------------------- errors


class BriefError(Exception):
    """A tool error: printed as the {"ok": false, "error": {...}} envelope, exit code 2."""

    def __init__(self, code, message, retryable=False, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail

    def to_json(self):
        err = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.detail:
            err["detail"] = self.detail
        return {"ok": False, "error": err}


def _short(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------- config


def _int_env(env, name, default, minimum):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BriefError("USAGE", "%s must be an integer, got %r" % (name, raw))
    if value < minimum:
        raise BriefError("USAGE", "%s must be at least %d" % (name, minimum))
    return value


def _float_env(env, name, default):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise BriefError("USAGE", "%s must be a number, got %r" % (name, raw))
    if not 0.0 <= value <= 1.0:
        raise BriefError("USAGE", "%s must be between 0 and 1" % name)
    return value


class Config:
    def __init__(self, env):
        self.data_dir = os.path.realpath(env.get("DATA_DIR") or "/data")
        self.cap_chars = _int_env(env, "BRIEF_CAP_CHARS", DEFAULT_CAP_CHARS, 2000)
        self.threshold = _float_env(env, "BRIEF_MATCH_THRESHOLD", DEFAULT_THRESHOLD)
        self.telegram_limit = _int_env(env, "BRIEF_TELEGRAM_LIMIT", DEFAULT_TELEGRAM_LIMIT, 200)
        self.schema_path = env.get("BRIEF_SCHEMA") or DEFAULT_SCHEMA
        self.prompt_path = env.get("BRIEF_WRITER_MD") or DEFAULT_PROMPT

    @property
    def readings_dir(self):
        return os.path.join(self.data_dir, "readings")

    @property
    def work_root(self):
        return os.path.join(self.data_dir, "work")

    def work_dir(self, course, date):
        return os.path.join(self.work_root, course, date)

    def forbidden_dirs(self):
        return [os.path.join(self.data_dir, "secrets"), os.path.join(self.data_dir, "rclone")]


# --------------------------------------------------------------------------- keys and paths


def parse_key(key):
    m = KEY_RE.match(key or "")
    if not m:
        raise BriefError("USAGE", "key must look like <course>@<YYYY-MM-DD> (e.g. MAS.665@2026-09-29), got %r" % key)
    course, date = m.group(1), m.group(2)
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise BriefError("USAGE", "key has an invalid date: %r" % date)
    return course, date


def task_name_for(course, date):
    slug = re.sub(r"[^a-z0-9]", "", course.lower()) or "course"
    name = "brief-%s-%s" % (slug, date.replace("-", ""))
    if not TASK_NAME_RE.match(name):
        name = "brief-%s" % date.replace("-", "")
    return name[:64]


def _under(path, root):
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def check_input_path(cfg, path, what, must_be_under=None):
    """Refuse secrets and (for readings) anything outside the readings/work trees."""
    if not path or path == "-":
        raise BriefError("USAGE", "%s needs a file path" % what)
    real = os.path.realpath(path)
    for forbidden in cfg.forbidden_dirs():
        if _under(real, forbidden):
            raise BriefError("USAGE", "%s may not come from %s" % (what, forbidden))
    if must_be_under and not any(_under(real, d) for d in must_be_under):
        raise BriefError("USAGE", "%s must be under %s" % (what, " or ".join(must_be_under)),
                         detail={"path": path})
    return real


def read_text_arg(cfg, value, what, stdin=None):
    """Read a --flag FILE|- argument as text."""
    if value == "-":
        data = (stdin or sys.stdin).read()
        return data
    check_input_path(cfg, value, what)
    if not os.path.isfile(value):
        raise BriefError("USAGE", "%s: no such file: %s" % (what, value))
    with open(value, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def read_json_arg(cfg, value, what, stdin=None):
    text = read_text_arg(cfg, value, what, stdin)
    try:
        return json.loads(text)
    except ValueError as e:
        raise BriefError("USAGE", "%s is not valid JSON: %s" % (what, _short(e)))


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_json(path, obj):
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- text handling


def clean_text(text):
    """Verbatim content minus control characters; newlines normalised, form feeds become newlines."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    return _CONTROL.sub("", text)


def guard_headings(text):
    """Indent content lines that start with '#' so they read as literal text, not as our section headers."""
    return _HEADING_LINE.sub(r"    \1", text)


def one_line(text, limit):
    text = " ".join(clean_text(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def truncate(text, keep):
    """Cut `text` to at most `keep` characters at a whitespace boundary. Returns (kept_text, truncated)."""
    if len(text) <= keep:
        return text, False
    if keep <= 0:
        return "", True
    cut = text[:keep]
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary >= keep - 200 and boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip(), True


def even_split(lengths, budget):
    """Share `budget` characters across items evenly; items that need less give the rest back."""
    n = len(lengths)
    alloc = [0] * n
    remaining = max(0, budget)
    left = n
    for i in sorted(range(n), key=lambda k: lengths[k]):
        share = remaining // left if left else 0
        take = min(lengths[i], max(0, share))
        alloc[i] = take
        remaining -= take
        left -= 1
    return alloc


def est_tokens(chars):
    return (chars + 3) // 4


class _HTMLText(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table",
              "section", "article", "blockquote", "pre", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    p = _HTMLText()
    p.feed(html)
    p.close()
    text = "".join(p.parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- PDF text (fallback chain)


def _pdf_decode_string(raw):
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == 0x5C and i + 1 < n:          # backslash
            i += 1
            e = raw[i]
            mapping = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C,
                       0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
            if e in mapping:
                out.append(mapping[e])
            elif 0x30 <= e <= 0x37:          # octal escape
                oct_digits = bytes([e])
                while i + 1 < n and len(oct_digits) < 3 and 0x30 <= raw[i + 1] <= 0x37:
                    i += 1
                    oct_digits += bytes([raw[i]])
                out.append(int(oct_digits, 8) & 0xFF)
            elif e in (0x0A, 0x0D):          # line continuation
                pass
            else:
                out.append(e)
        else:
            out.append(c)
        i += 1
    return out.decode("latin-1")


def _pdf_content_text(content):
    """Text from one content stream: (..)Tj, [..]TJ, ' and \" operators; T*, Td, TD, Tm and ET break lines."""
    out = []
    i = 0
    n = len(content)
    pending = []                       # string operands waiting for an operator
    while i < n:
        c = content[i]
        if c == 0x28:                  # ( string
            depth = 1
            j = i + 1
            while j < n and depth:
                if content[j] == 0x5C:
                    j += 2
                    continue
                if content[j] == 0x28:
                    depth += 1
                elif content[j] == 0x29:
                    depth -= 1
                j += 1
            pending.append(_pdf_decode_string(content[i + 1:j - 1]))
            i = j
        elif c == 0x3C and i + 1 < n and content[i + 1] != 0x3C:   # <hex string>
            j = content.find(b">", i)
            if j < 0:
                break
            hexdigits = re.sub(rb"\s", b"", content[i + 1:j])
            try:
                raw = bytes.fromhex(hexdigits.decode("ascii") + ("0" if len(hexdigits) % 2 else ""))
                if raw and all(0x20 <= b < 0x7F or b in (0x09, 0x0A) for b in raw):
                    pending.append(raw.decode("latin-1"))
            except ValueError:
                pass
            i = j + 1
        elif c in (0x5B, 0x5D):        # [ ]  array delimiters: keep scanning inside
            i += 1
        elif c == 0x25:                # % comment
            j = content.find(b"\n", i)
            i = n if j < 0 else j + 1
        else:
            m = re.match(rb"[^\s()<>\[\]/%]+", content[i:])
            if not m:
                i += 1
                continue
            tok = m.group(0)
            i += len(tok)
            if tok in (b"Tj", b"TJ", b"'", b'"'):
                if tok in (b"'", b'"'):
                    out.append("\n")
                out.append("".join(pending))
                pending = []
            elif tok in (b"T*", b"Td", b"TD", b"Tm", b"ET"):
                out.append("\n")
                pending = []
            elif re.match(rb"^-?\d+(\.\d+)?$", tok):
                try:
                    if pending and float(tok) < -150:   # a large negative kern inside TJ is a word gap
                        pending.append(" ")
                except ValueError:
                    pass
            else:
                pending = []
    text = "".join(out)
    return re.sub(r"[ \t]+\n", "\n", text)


def pdf_text_builtin(data):
    texts = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = m.group(1)
        try:
            content = zlib.decompress(raw)
        except zlib.error:
            try:
                content = zlib.decompressobj().decompress(raw)
            except zlib.error:
                content = raw
        if b"BT" not in content:
            continue
        piece = _pdf_content_text(content).strip()
        if piece:
            texts.append(piece)
    return "\n\n".join(texts)


def _pdftotext(path):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-enc", "UTF-8", path, "-"], capture_output=True, timeout=PDFTOTEXT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _pypdf(path):
    try:
        import pypdf  # type: ignore
    except ImportError:
        try:
            import PyPDF2 as pypdf  # type: ignore
        except ImportError:
            return None
    try:
        reader = pypdf.PdfReader(path)
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None


def extract_pdf(path):
    """(text, extractor) via pdftotext, then pypdf, then the built-in scanner."""
    text = _pdftotext(path)
    if text is not None:
        return text, "pdftotext"
    text = _pypdf(path)
    if text is not None:
        return text, "pypdf"
    with open(path, "rb") as fh:
        data = fh.read()
    return pdf_text_builtin(data), "builtin"


def extract_reading(path):
    """(text, extractor, warning) for a reading file: .pdf, .html, or text."""
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    warning = None
    if ext == ".pdf":
        text, extractor = extract_pdf(path)
        if size > 20000 and len(text.strip()) < 200:
            warning = "very little text came out of this PDF (%s); it may be scanned or use fonts the %s extractor can't read" % (
                os.path.basename(path), extractor)
    else:
        with open(path, "rb") as fh:
            data = fh.read()
        if ext in (".html", ".htm"):
            text, extractor = html_to_text(data.decode("utf-8", "replace")), "html"
        else:
            text, extractor = data.decode("utf-8", "replace"), "text"
            if data.count(b"\x00") > 0 or (data and sum(1 for b in data[:4096] if b < 9 or 13 < b < 32) > 40):
                warning = "%s does not look like a text file" % os.path.basename(path)
    return clean_text(text), extractor, warning


# --------------------------------------------------------------------------- prompt and schema


def load_prompt(cfg):
    """The blockquoted prompt in agents/brief-writer.md, with the '> ' markers removed."""
    path = cfg.prompt_path
    if not os.path.isfile(path):
        raise BriefError("PROMPT_MISSING", "brief-writer definition not found at %s (is the workspace installed?)" % path)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## Prompt"):
            start = i + 1
            break
    if start is None:
        raise BriefError("PROMPT_MISSING", "no '## Prompt' section in %s" % path)
    quoted = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.startswith(">"):
            quoted.append(line[2:] if line.startswith("> ") else line[1:])
    prompt = "\n".join(quoted).strip()
    if not prompt:
        raise BriefError("PROMPT_MISSING", "the '## Prompt' section in %s has no blockquoted lines" % path)
    return prompt, path


def load_schema(cfg):
    path = cfg.schema_path
    if not os.path.isfile(path):
        raise BriefError("SCHEMA_MISSING", "brief.schema.json not found at %s (is the workspace installed?)" % path)
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except ValueError as e:
            raise BriefError("SCHEMA_MISSING", "brief.schema.json is not valid JSON: %s" % _short(e))


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_schema(instance, schema, path="$", errors=None):
    """A small JSON Schema 2020-12 subset: enough for brief.schema.json. Returns a list of messages."""
    errors = [] if errors is None else errors
    if len(errors) >= MAX_SCHEMA_ERRORS:
        return errors
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_TYPES.get(t, lambda v: False)(instance) for t in allowed):
            errors.append("%s: expected %s, got %s" % (path, "/".join(allowed), type(instance).__name__
                                                       if instance is not None else "null"))
            return errors
    if "const" in schema and instance != schema["const"]:
        errors.append("%s: must equal %r" % (path, schema["const"]))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: must be one of %s" % (path, ", ".join(repr(e) for e in schema["enum"])))
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: must not be empty" % path if schema["minLength"] == 1
                          else "%s: shorter than %d characters" % (path, schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("%s: longer than %d characters (%d)" % (path, schema["maxLength"], len(instance)))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("%s: does not match %s" % (path, schema["pattern"]))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append("%s: needs at least %d item(s), got %d" % (path, schema["minItems"], len(instance)))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append("%s: at most %d items allowed, got %d" % (path, schema["maxItems"], len(instance)))
        if "items" in schema:
            for i, item in enumerate(instance):
                validate_schema(item, schema["items"], "%s[%d]" % (path, i), errors)
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append("%s: missing required field %r" % (path, name))
        for name, value in instance.items():
            if name in props:
                validate_schema(value, props[name], "%s.%s" % (path, name), errors)
            elif schema.get("additionalProperties") is False:
                errors.append("%s: unexpected field %r" % (path, name))
    return errors[:MAX_SCHEMA_ERRORS]


# --------------------------------------------------------------------------- JSON extraction


def extract_json_object(text):
    """The single JSON object in a subagent reply, or None. Code fences and prose around it are ignored."""
    text = text or ""
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS]
    for m in re.finditer(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.S):
        obj = _try_json(m.group(1))
        if isinstance(obj, dict):
            return obj
    obj = _try_json(text)
    if isinstance(obj, dict):
        return obj
    starts = 0
    i = text.find("{")
    while i >= 0 and starts < 50:
        starts += 1
        end = _matching_brace(text, i)
        if end > i:
            obj = _try_json(text[i:end + 1])
            if isinstance(obj, dict):
                return obj
        i = text.find("{", i + 1)
    return None


def _try_json(s):
    try:
        return json.loads(s.strip())
    except ValueError:
        return None


def _matching_brace(text, start):
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


# --------------------------------------------------------------------------- fuzzy matching


def normalize(text):
    text = clean_text(text).translate(_TYPOGRAPHY).lower()
    text = _PUNCT.sub(" ", text)
    return " ".join(text.split())


def candidate_sentences(text):
    """Sentences, lines and bullet items of a Canvas text, normalised."""
    out = set()
    for piece in _SENTENCE_SPLIT.split(clean_text(text)):
        piece = _BULLET.sub("", piece)
        norm = normalize(piece)
        if norm:
            out.add(norm)
    for line in clean_text(text).split("\n"):
        norm = normalize(_BULLET.sub("", line))
        if norm:
            out.add(norm)
    return out


def best_match(question, canvas_texts):
    """(score, best_candidate): the best difflib ratio between the normalised question and the
    normalised Canvas sentences and same-length token windows. 1.0 when it is a verbatim substring."""
    q = normalize(question)
    if not q:
        return 0.0, ""
    q_tokens = q.split()
    q_set = set(q_tokens)
    best_score, best_text = 0.0, ""
    for text in canvas_texts:
        t = normalize(text)
        if not t:
            continue
        if (" " + q + " ") in (" " + t + " "):
            return 1.0, q
        candidates = candidate_sentences(text)
        tokens = t.split()
        n = len(q_tokens)
        for length in range(max(1, n - 2), n + 3):
            for i in range(0, len(tokens) - length + 1):
                window = tokens[i:i + length]
                if len(q_set & set(window)) < 0.6 * len(q_set):
                    continue
                candidates.add(" ".join(window))
        for cand in candidates:
            sm = difflib.SequenceMatcher(None, q, cand, autojunk=False)
            if sm.real_quick_ratio() <= best_score or sm.quick_ratio() <= best_score:
                continue
            score = sm.ratio()
            if score > best_score:
                best_score, best_text = score, cand
                if best_score >= 0.999:
                    return 1.0, cand
    return round(best_score, 4), best_text


# --------------------------------------------------------------------------- inputs: session, canvas, readings, notes


def _clean_url(value):
    if value in (None, ""):
        return None
    return one_line(str(value), 500) or None


def pick_session(obj, key, course, date):
    """Accept one session row, or a `syllabi upcoming` output ({sessions: [...]}) and pick this key's row."""
    if isinstance(obj, dict) and isinstance(obj.get("sessions"), list):
        for row in obj["sessions"]:
            if not isinstance(row, dict):
                continue
            if row.get("key") == key or ("key" not in row and row.get("course") == course and row.get("class_date") == date):
                return row
        raise BriefError("USAGE", "no session for %s in --session (keys: %s)" % (
            key, ", ".join(str(r.get("key")) for r in obj["sessions"] if isinstance(r, dict))[:300] or "none"))
    if isinstance(obj, dict):
        return obj
    raise BriefError("USAGE", "--session must be a JSON object (a session row or a `syllabi upcoming` output)")


def render_session(session, key, course, date):
    lines = []
    seen = set()

    def add(name, value):
        if value is None or name in seen:
            return
        seen.add(name)
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, str):
            text = one_line(value, MAX_SESSION_VALUE_CHARS)
        else:
            text = one_line(json.dumps(value, ensure_ascii=False), MAX_SESSION_VALUE_CHARS)
        lines.append("- %s: %s" % (one_line(name, 60), text))

    add("key", key)
    for name in SESSION_FIELD_ORDER:
        if name in session:
            add(name, session[name])
    if "course" not in seen:
        add("course", course)
    if "class_date" not in seen:
        add("class_date", date)
    for name in sorted(session):
        if name in seen or name in SESSION_LIST_FIELDS:
            continue
        add(name, session[name])
    for name in SESSION_LIST_FIELDS:
        items = session.get(name)
        if not isinstance(items, list) or not items:
            continue
        label = {"due_before_class": "due before class", "due": "due before class", "readings": "syllabus reading"}[name]
        for item in items[:50]:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name") or "(untitled)"
                bits = [one_line(title, MAX_TITLE_CHARS)]
                for k in ("type", "due_at", "date", "time", "url", "canvas_url", "canvas_file_id"):
                    if item.get(k) not in (None, ""):
                        bits.append("%s=%s" % (k, one_line(str(item[k]), 200)))
                lines.append("- %s: %s" % (label, "; ".join(bits)))
            else:
                lines.append("- %s: %s" % (label, one_line(str(item), MAX_SESSION_VALUE_CHARS)))
    return "\n".join(lines)


def canvas_sections_from_json(obj, assignment_ids, origin):
    sections = []
    if isinstance(obj, list):
        for item in obj:
            sections.extend(canvas_sections_from_json(item, assignment_ids, origin))
        return sections
    if not isinstance(obj, dict):
        raise BriefError("USAGE", "unrecognised Canvas JSON in %s (expected an object or a list)" % origin)
    if obj.get("ok") is False:
        raise BriefError("USAGE", "%s holds a tool error (%s), not Canvas text" % (
            origin, (obj.get("error") or {}).get("code", "?")))
    if isinstance(obj.get("assignments"), list):
        for a in obj["assignments"]:
            if not isinstance(a, dict):
                continue
            if assignment_ids and str(a.get("id")) not in assignment_ids:
                continue
            title = a.get("name") or "Assignment %s" % a.get("id", "?")
            text = a.get("description_text")
            if text is None:
                text = html_to_text(a.get("description") or "")
            if a.get("due_at"):
                text = "Due: %s\n\n%s" % (one_line(str(a["due_at"]), 60), text)
            sections.append({"title": one_line(title, MAX_TITLE_CHARS), "url": _clean_url(a.get("html_url")),
                             "text": clean_text(text), "kind": "assignment", "id": a.get("id")})
        return sections
    if isinstance(obj.get("page"), dict):
        p = obj["page"]
        text = p.get("body_text")
        if text is None:
            text = html_to_text(p.get("body") or "")
        sections.append({"title": one_line(p.get("title") or p.get("url") or "Page", MAX_TITLE_CHARS),
                         "url": _clean_url(p.get("html_url") or p.get("url")), "text": clean_text(text), "kind": "page"})
        return sections
    for field in ("text", "body_text", "description_text"):
        if field in obj:
            sections.append({"title": one_line(obj.get("title") or obj.get("name") or os.path.basename(origin), MAX_TITLE_CHARS),
                             "url": _clean_url(obj.get("url") or obj.get("html_url")),
                             "text": clean_text(obj.get(field) or ""), "kind": "text"})
            return sections
    raise BriefError("USAGE", "unrecognised Canvas JSON in %s: expected `assignments[]`, `page` or `text`" % origin)


def load_canvas(cfg, paths, assignment_ids):
    sections = []
    for path in paths or []:
        check_input_path(cfg, path, "--canvas")
        if not os.path.isfile(path):
            raise BriefError("USAGE", "--canvas: no such file: %s" % path)
        with open(path, "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
        stripped = raw.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            if obj is not None:
                sections.extend(canvas_sections_from_json(obj, assignment_ids, path))
                continue
        sections.append({"title": one_line(os.path.splitext(os.path.basename(path))[0], MAX_TITLE_CHARS),
                         "url": None, "text": clean_text(raw), "kind": "text"})
    return sections


def load_readings(cfg, reading_paths, readings_json):
    items = []
    skipped = []
    if readings_json:
        obj = read_json_arg(cfg, readings_json, "--readings-json")
        if isinstance(obj, dict) and isinstance(obj.get("session"), dict):
            obj = obj["session"]
        rows = obj.get("readings") if isinstance(obj, dict) else obj
        if not isinstance(rows, list):
            raise BriefError("USAGE", "--readings-json must hold a readings list (a prep-log record, `preplog get` output, or a list)")
        for r in rows:
            if not isinstance(r, dict):
                continue
            title = r.get("title") or os.path.basename(str(r.get("local_path") or "reading"))
            if not r.get("local_path"):
                skipped.append({"title": one_line(title, MAX_TITLE_CHARS),
                                "reason": "requires_login" if r.get("requires_login") else "not downloaded"})
                continue
            items.append({"title": one_line(title, MAX_TITLE_CHARS), "path": str(r["local_path"])})
    for path in reading_paths or []:
        items.append({"title": one_line(os.path.splitext(os.path.basename(path))[0], MAX_TITLE_CHARS), "path": path})
    readings = []
    for it in items:
        real = check_input_path(cfg, it["path"], "reading %r" % it["title"],
                                must_be_under=[cfg.readings_dir, cfg.work_root])
        if not os.path.isfile(real):
            skipped.append({"title": it["title"], "path": it["path"], "reason": "file not found"})
            continue
        text, extractor, warning = extract_reading(real)
        readings.append({"title": it["title"], "path": it["path"], "text": text,
                         "extractor": extractor, "warning": warning})
    return readings, skipped


def load_notes(cfg, value, stdin=None):
    if not value:
        return ""
    text = read_text_arg(cfg, value, "--notes", stdin)
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("text"), str):
                text = obj["text"]
        except ValueError:
            pass
    return clean_text(text)


# --------------------------------------------------------------------------- bundle


def build_bundle(cfg, key, course, date, prompt, session, canvas, readings, skipped, notes, cap):
    """Assemble the task text under `cap` characters. Returns (task_text, meta)."""
    session_block = "## SESSION\n\n" + render_session(session or {}, key, course, date) + "\n"
    notes_src = guard_headings(notes.strip())
    notes_text, notes_cut = truncate(notes_src, NOTES_CAP_CHARS) if notes_src else ("", False)
    notes_block = ""
    if notes_text:
        notes_block = "## COURSE NOTES\n\n" + notes_text + ("\n" + TRUNCATION_NOTE % (len(notes_text), len(notes_src)) if notes_cut else "") + "\n"

    head = prompt.strip() + "\n\n---\n\n# INPUT BUNDLE: %s, class on %s\n\n" % (course, date)
    canvas_intro = "## CANVAS TEXT\n\n"
    if not canvas:
        canvas_intro += "(No Canvas assignment or page text was found for this session, so there are no pre-class questions: return `\"pre_class_questions\": []`.)\n\n"
    readings_intro = "## READINGS\n\n"
    if not readings:
        readings_intro += "(No reading text is available for this session.)\n\n"
    skipped_lines = ""
    if skipped:
        skipped_lines = "".join("- Not available: %s (%s)\n" % (s["title"], s["reason"]) for s in skipped) + "\n"

    def frame_len(title, url):
        return len("### %s: %s (%s)\n\n" % ("READING", title, url or "")) + 2 + TRUNCATION_NOTE_RESERVE

    fixed = len(head) + len(session_block) + len(notes_block) + len(canvas_intro) + len(readings_intro) + len(skipped_lines) + 8
    canvas_frames = sum(frame_len(c["title"], c["url"]) for c in canvas)
    reading_frames = sum(frame_len(r["title"], None) for r in readings)

    canvas_texts = [guard_headings(c["text"].strip()) for c in canvas]
    canvas_budget = cap - fixed - canvas_frames - reading_frames
    canvas_alloc = even_split([len(t) for t in canvas_texts], canvas_budget)
    if sum(len(t) for t in canvas_texts) <= canvas_budget:
        canvas_alloc = [len(t) for t in canvas_texts]

    canvas_block = canvas_intro
    canvas_meta = []
    canvas_kept = 0
    for c, text, keep in zip(canvas, canvas_texts, canvas_alloc):
        kept, cut = truncate(text, keep)
        canvas_kept += len(kept)
        header = "### CANVAS: %s%s\n\n" % (c["title"], " (%s)" % c["url"] if c.get("url") else "")
        canvas_block += header + kept + ("\n" + TRUNCATION_NOTE % (len(kept), len(text)) if cut else "") + "\n\n"
        canvas_meta.append({"title": c["title"], "url": c.get("url"), "kind": c.get("kind"), "chars": len(text),
                            "kept": len(kept), "truncated": cut})

    reading_texts = [guard_headings(r["text"].strip()) for r in readings]
    readings_budget = cap - fixed - canvas_frames - canvas_kept - reading_frames
    reading_alloc = even_split([len(t) for t in reading_texts], readings_budget)

    readings_block = readings_intro + skipped_lines
    reading_meta = []
    for r, text, keep in zip(readings, reading_texts, reading_alloc):
        kept, cut = truncate(text, keep)
        body = kept if kept else "(no text could be extracted)" if not text else ""
        readings_block += "### READING: %s\n\n" % r["title"] + body
        readings_block += ("\n" + TRUNCATION_NOTE % (len(kept), len(text)) if cut else "") + "\n\n"
        reading_meta.append({"title": r["title"], "path": r["path"], "chars": len(text), "kept": len(kept),
                             "truncated": cut, "extractor": r["extractor"], "warning": r["warning"]})

    task = head + session_block + "\n" + canvas_block + readings_block + notes_block
    task = task.rstrip() + "\n"
    meta = {
        "canvas": canvas_meta,
        "readings": reading_meta,
        "notes_chars": len(notes_text),
        "notes_truncated": notes_cut,
        "chars": len(task),
        "est_tokens": est_tokens(len(task)),
        "cap_chars": cap,
        "readings_budget": max(0, readings_budget),
    }
    return task, meta


def cmd_bundle(cfg, args, stdin=None):
    course, date = parse_key(args.key)
    cap = args.cap_chars or cfg.cap_chars
    if cap < 2000:
        raise BriefError("USAGE", "--cap-chars must be at least 2000")
    prompt, prompt_path = load_prompt(cfg)
    session = None
    if args.session:
        session = pick_session(read_json_arg(cfg, args.session, "--session", stdin), args.key, course, date)
    ids = set(str(i) for i in (args.assignment_id or []))
    canvas = load_canvas(cfg, args.canvas, ids)
    readings, skipped = load_readings(cfg, args.reading, args.readings_json)
    notes = load_notes(cfg, args.notes, stdin)

    task, meta = build_bundle(cfg, args.key, course, date, prompt, session, canvas, readings, skipped, notes, cap)
    if meta["chars"] > cap:
        raise BriefError("INTERNAL", "bundle is %d characters, over the %d cap" % (meta["chars"], cap))

    work = cfg.work_dir(course, date)
    task_path = os.path.join(work, "brief-input.md")
    sidecar_path = os.path.join(work, "brief-input.json")
    task_name = task_name_for(course, date)
    sidecar = {
        "key": args.key, "course": course, "class_date": date, "task_name": task_name, "built_at": _now_iso(),
        "prompt_source": prompt_path, "task_path": task_path, "cap_chars": cap, "chars": meta["chars"],
        "est_tokens": meta["est_tokens"], "session": session,
        "canvas": [dict(c, text=canvas[i]["text"].strip()) for i, c in enumerate(meta["canvas"])],
        "readings": meta["readings"], "skipped": skipped, "notes_chars": meta["notes_chars"],
    }
    _write_text(task_path, task)
    _write_json(sidecar_path, sidecar)

    warnings = []
    if not canvas:
        warnings.append("no Canvas text: the subagent must return pre_class_questions: [] and every question it returns will be dropped")
    if not readings:
        warnings.append("no reading text")
    for r in meta["readings"]:
        if r["warning"]:
            warnings.append(r["warning"])
    if meta["readings"] and meta["readings_budget"] <= 0:
        warnings.append("the cap left no room for reading text")
    result = {
        "ok": True, "key": args.key, "task_path": task_path, "sidecar_path": sidecar_path, "task_name": task_name,
        "chars": meta["chars"], "est_tokens": meta["est_tokens"], "cap_chars": cap,
        "canvas": meta["canvas"], "readings": meta["readings"], "skipped": skipped,
        "notes_chars": meta["notes_chars"], "truncated": [x["title"] for x in meta["canvas"] + meta["readings"] if x["truncated"]],
        "warnings": warnings,
    }
    if args.print:
        result["task"] = task
    return result


# --------------------------------------------------------------------------- validate


def load_sidecar(cfg, course, date):
    path = os.path.join(cfg.work_dir(course, date), "brief-input.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except ValueError:
            raise BriefError("INTERNAL", "sidecar %s is not valid JSON; rebuild the bundle" % path)


def cmd_validate(cfg, args, stdin=None):
    course, date = parse_key(args.key)
    threshold = cfg.threshold if args.threshold is None else args.threshold
    if not 0.0 <= threshold <= 1.0:
        raise BriefError("USAGE", "--threshold must be between 0 and 1")
    schema = load_schema(cfg)
    work = cfg.work_dir(course, date)
    sidecar = load_sidecar(cfg, course, date)
    if args.canvas:
        canvas_sections = load_canvas(cfg, args.canvas, set())
    elif sidecar is not None:
        canvas_sections = sidecar.get("canvas") or []
    else:
        raise BriefError("NO_BUNDLE", "no brief-input.json for %s under %s: run `brief bundle` first (or pass --canvas)" % (
            args.key, work))
    canvas_texts = [c.get("text") or "" for c in canvas_sections]

    reply = read_text_arg(cfg, args.reply, "--reply", stdin)
    attempt = args.attempt
    if attempt is None:
        existing = [n for n in os.listdir(work)] if os.path.isdir(work) else []
        attempt = 1 + sum(1 for n in existing if re.match(r"^brief-reply\.\d+\.txt$", n))
    reply_path = os.path.join(work, "brief-reply.%d.txt" % attempt)
    _write_text(reply_path, reply)

    obj = extract_json_object(reply)
    errors = ["no JSON object found in the reply"] if obj is None else validate_schema(obj, schema)
    if errors:
        reprompt = ("Your previous reply did not validate against the required JSON shape:\n"
                    + "".join("- %s\n" % e for e in errors)
                    + "Reply again with ONLY the JSON object (no prose, no code fences), following the same rules.")
        _write_json(os.path.join(work, "brief-validation.json"), {
            "key": args.key, "attempt": attempt, "ok": False, "errors": errors, "checked_at": _now_iso()})
        raise BriefError("BRIEF_SCHEMA_INVALID", "the reply does not match brief.schema.json (%d error%s)" % (
            len(errors), "" if len(errors) == 1 else "s"), detail={
            "errors": errors, "attempt": attempt, "reply_path": reply_path, "reprompt": reprompt,
            "next": "re-prompt once with detail.reprompt appended to the task" if attempt < 2
                    else "set the session partial with last_error.code BRIEF_SCHEMA_INVALID (step brief)"})

    kept, dropped = [], []
    titles = [(c.get("title") or "").lower() for c in canvas_sections]
    urls = [(c.get("url") or "").lower() for c in canvas_sections if c.get("url")]
    for q in obj.get("pre_class_questions", []):
        score, match = best_match(q["question"], canvas_texts)
        source = (q.get("source") or "").lower()
        source_ok = any(t and (t in source or source in t) for t in titles) or any(u and u in source for u in urls)
        entry = {"question": q["question"], "score": score, "source": q.get("source"), "source_ok": source_ok}
        if score >= threshold:
            kept.append(entry)
        else:
            entry["best_match"] = match
            entry["log_line"] = "HALLUCINATION: " + one_line(q["question"], 300)
            dropped.append(entry)

    cleaned = {
        "topic": obj["topic"], "why_it_matters": obj["why_it_matters"], "key_arguments": obj["key_arguments"],
        "prep_checklist": obj["prep_checklist"],
        "pre_class_questions": [{"question": k["question"], "source": k["source"],
                                 "draft_answer": next(q["draft_answer"] for q in obj["pre_class_questions"]
                                                      if q["question"] == k["question"])}
                                for k in kept],
    }
    brief_path = os.path.join(work, "brief-output.json")
    _write_json(brief_path, cleaned)
    warnings = []
    if not canvas_texts and obj.get("pre_class_questions"):
        warnings.append("no Canvas text was in the bundle: every question was dropped")
    if not 3 <= len(obj["key_arguments"]) <= 6:
        warnings.append("key_arguments has %d items (the prompt asks for 3-6)" % len(obj["key_arguments"]))
    if not 3 <= len(obj["prep_checklist"]) <= 6:
        warnings.append("prep_checklist has %d items (the prompt asks for 3-6)" % len(obj["prep_checklist"]))
    for k in kept:
        if not k["source_ok"]:
            warnings.append("question kept but its source %r names no Canvas section" % one_line(k["source"] or "", 80))
    validation = {"key": args.key, "attempt": attempt, "ok": True, "threshold": threshold, "kept": kept,
                  "dropped": dropped, "warnings": warnings, "checked_at": _now_iso(), "brief_path": brief_path}
    _write_json(os.path.join(work, "brief-validation.json"), validation)
    return {
        "ok": True, "key": args.key, "attempt": attempt, "brief_path": brief_path, "reply_path": reply_path,
        "threshold": threshold,
        "questions": {"returned": len(obj.get("pre_class_questions", [])), "kept": len(kept), "dropped": len(dropped)},
        "kept": kept, "dropped": dropped,
        "log_lines": [d["log_line"] for d in dropped],
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- format


def pick_record(obj, key):
    if isinstance(obj, dict) and isinstance(obj.get("session"), dict):
        return obj["session"]
    if isinstance(obj, dict) and isinstance(obj.get("sessions"), dict):
        rec = obj["sessions"].get(key)
        if not isinstance(rec, dict):
            raise BriefError("USAGE", "no record %s in --record" % key)
        return rec
    if isinstance(obj, dict):
        return obj
    raise BriefError("USAGE", "--record must be a prep-log record, a `preplog get` output, or the prep-log file")


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def when_label(record, date):
    start = _parse_iso((record or {}).get("class_start"))
    if start is not None:
        label = "%s %s %d" % (start.strftime("%a"), start.strftime("%b"), start.day)
        if (record or {}).get("start_time_known") is not False and not (start.hour == 0 and start.minute == 0):
            label += ", %02d:%02d ET" % (start.hour, start.minute)
        return label
    try:
        d = dt.date.fromisoformat(date)
        return "%s %s %d" % (d.strftime("%a"), d.strftime("%b"), d.day)
    except ValueError:
        return date


def _link_label(link, readings):
    for r in readings or []:
        if isinstance(r, dict) and r.get("drive_path") == link and r.get("title"):
            return "%s: %s" % (one_line(r["title"], 120), link)
    return link


def render_brief(course, date, record, brief, drive_links, podcast_url, podcast_state, dropped):
    lines = ["📚 %s — %s" % (course, when_label(record, date))]
    if brief:
        lines.append(one_line(brief["topic"], 200))
        lines += ["", "Why it matters", " ".join(clean_text(brief["why_it_matters"]).split())]
        lines += ["", "Key arguments"] + ["• " + " ".join(clean_text(a).split()) for a in brief["key_arguments"]]
        lines += ["", "Prep checklist"] + ["☐ " + " ".join(clean_text(a).split()) for a in brief["prep_checklist"]]
        questions = brief.get("pre_class_questions") or []
        lines.append("")
        if questions:
            lines.append(QUESTION_HEADER)
            for i, q in enumerate(questions, 1):
                lines.append("%d. %s" % (i, " ".join(clean_text(q["question"]).split())))
                lines.append("   Source: %s" % one_line(q["source"], 200))
                lines.append("   DRAFT: %s" % " ".join(clean_text(q["draft_answer"]).split()))
        else:
            lines.append("Pre-class questions: none found on Canvas.")
    else:
        lines += ["", "No brief this time: the brief-writer's reply failed validation twice. The readings and podcast are below."]
    lines += ["", "Readings on Drive"]
    if drive_links:
        lines += ["• " + _link_label(link, (record or {}).get("readings")) for link in drive_links]
    else:
        lines.append("• none filed yet")
    lines.append("")
    if podcast_state == "ready":
        lines.append("🎧 Podcast: %s" % podcast_url)
    elif podcast_state == "unavailable":
        lines.append("🎧 No podcast this time: the NotebookLM step failed.")
    else:
        lines.append(PODCAST_PENDING_LINE)
    if dropped:
        lines += ["", "⚠️ %d draft question%s dropped: not found in the Canvas text." % (dropped, "" if dropped == 1 else "s")]
    return "\n".join(lines).rstrip() + "\n"


def split_message(text, limit):
    """Split at blank lines (then lines, then hard) so every part fits Telegram's limit.
    Parts are numbered "(i/n) "; the numbering's room is reserved before splitting."""
    text = text.rstrip("\n")
    if len(text) <= limit:
        return [text]
    tag_room = 8                    # "(12/34) "
    parts = _split_parts(text, limit - tag_room)
    total = len(parts)
    return ["(%d/%d) %s" % (i, total, p) for i, p in enumerate(parts, 1)]


def _split_parts(text, limit):
    parts, current = [], ""
    for para in text.split("\n\n"):
        candidate = para if not current else current + "\n\n" + para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(para) <= limit:
            current = para
            continue
        for line in para.split("\n"):
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            candidate = line if not current else current + "\n" + line
            if len(candidate) <= limit:
                current = candidate
            else:
                parts.append(current)
                current = line
    if current:
        parts.append(current)
    return parts


def cmd_format(cfg, args, stdin=None):
    course, date = parse_key(args.key)
    schema = load_schema(cfg)
    work = cfg.work_dir(course, date)
    record = pick_record(read_json_arg(cfg, args.record, "--record", stdin), args.key) if args.record else {}
    brief = None
    brief_source = None
    if args.brief:
        brief, brief_source = read_json_arg(cfg, args.brief, "--brief", stdin), args.brief
    elif isinstance(record.get("brief"), dict):
        brief, brief_source = record["brief"], "record"
    elif os.path.isfile(os.path.join(work, "brief-output.json")):
        with open(os.path.join(work, "brief-output.json"), encoding="utf-8") as fh:
            brief, brief_source = json.load(fh), os.path.join(work, "brief-output.json")
    if brief is not None:
        errors = validate_schema(brief, schema)
        if errors:
            raise BriefError("BRIEF_SCHEMA_INVALID", "the brief to format does not match brief.schema.json",
                             detail={"errors": errors, "source": brief_source})

    links = []
    for link in list(args.drive_link or []) + list(record.get("drive_paths") or []):
        if isinstance(link, str) and link and link not in links:
            links.append(link)
    for r in record.get("readings") or []:
        if isinstance(r, dict) and r.get("drive_path") and r["drive_path"] not in links:
            links.append(r["drive_path"])

    podcast_url = args.podcast_url or record.get("podcast_url")
    if args.no_podcast:
        state = "unavailable"
    elif podcast_url:
        state = "ready"
    else:
        state = "pending"

    dropped = args.dropped
    if dropped is None:
        history = record.get("history") or []
        dropped = sum(1 for h in history if isinstance(h, dict) and str(h.get("action", "")).startswith("HALLUCINATION:"))
        if not dropped and os.path.isfile(os.path.join(work, "brief-validation.json")):
            with open(os.path.join(work, "brief-validation.json"), encoding="utf-8") as fh:
                try:
                    dropped = len(json.load(fh).get("dropped") or [])
                except ValueError:
                    dropped = 0

    text = render_brief(course, date, record, brief, links, podcast_url, state, dropped)
    parts = split_message(text, cfg.telegram_limit)
    paths = []
    for i, part in enumerate(parts, 1):
        path = os.path.join(work, "brief-telegram.txt" if i == 1 else "brief-telegram.%d.txt" % i)
        _write_text(path, part + "\n")
        paths.append(path)
    return {
        "ok": True, "key": args.key, "text": text, "parts": parts, "paths": paths, "chars": len(text),
        "has_brief": brief is not None, "brief_source": brief_source, "questions": len((brief or {}).get("pre_class_questions") or []),
        "drive_links": links, "podcast": state, "includes_podcast": state == "ready", "dropped": dropped,
        "send_with": ["cat %s | maritime-telegram-send -" % p for p in paths],
    }


def cmd_format_podcast(cfg, args, stdin=None):
    course, date = parse_key(args.key)
    url = (args.url or "").strip()
    if not url:
        raise BriefError("USAGE", "--url is required")
    record = pick_record(read_json_arg(cfg, args.record, "--record", stdin), args.key) if args.record else {}
    text = "%s%s\n%s — %s\n" % (PODCAST_READY_PREFIX, one_line(url, 500), course, when_label(record, date))
    path = os.path.join(cfg.work_dir(course, date), "brief-podcast-telegram.txt")
    _write_text(path, text)
    return {"ok": True, "key": args.key, "text": text, "path": path, "chars": len(text)}


def cmd_prompt(cfg, args, stdin=None):
    prompt, path = load_prompt(cfg)
    return {"ok": True, "prompt": prompt, "source": path, "chars": len(prompt)}


def cmd_score(cfg, args, stdin=None):
    threshold = cfg.threshold if args.threshold is None else args.threshold
    sections = load_canvas(cfg, args.canvas, set())
    score, match = best_match(args.question, [s["text"] for s in sections])
    return {"ok": True, "question": args.question, "score": score, "best_match": match,
            "kept": score >= threshold, "threshold": threshold, "canvas_sections": len(sections)}


COMMANDS = {
    "bundle": cmd_bundle,
    "validate": cmd_validate,
    "format": cmd_format,
    "format-podcast": cmd_format_podcast,
    "prompt": cmd_prompt,
    "score": cmd_score,
}


# --------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BriefError("USAGE", message)


def build_parser():
    p = _Parser(prog="brief", description="brief-writer input bundle, reply validation and Telegram formatting. Prints one JSON object.")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("bundle", help="build the bounded input bundle for brief-writer")
    s.add_argument("key", help="<course>@<YYYY-MM-DD>")
    s.add_argument("--session", help="JSON session row, or a `syllabi upcoming` output (this key's row is picked); - for stdin")
    s.add_argument("--canvas", action="append", default=[], help="Canvas text: `canvas assignments`/`canvas page` JSON, {title,url,text} JSON, or a text file; repeatable")
    s.add_argument("--assignment-id", action="append", default=[], help="keep only these assignment ids from --canvas files; repeatable")
    s.add_argument("--readings-json", help="a prep-log record, `preplog get` output, or a list of {title, local_path}")
    s.add_argument("--reading", action="append", default=[], help="a reading file (.pdf, .html or text) under $DATA_DIR/readings/; repeatable")
    s.add_argument("--notes", help="the course's course-notes section (text, or `preplog notes get` JSON); - for stdin")
    s.add_argument("--cap-chars", type=int, help="override the bundle cap (default $BRIEF_CAP_CHARS or 160000)")
    s.add_argument("--print", action="store_true", help="include the task text in the output")

    s = sub.add_parser("validate", help="check a brief-writer reply: JSON, schema, hallucinated questions")
    s.add_argument("key")
    s.add_argument("--reply", required=True, help="the raw reply (file or - for stdin)")
    s.add_argument("--attempt", type=int, help="1 for the first reply, 2 for the re-prompt (default: counted from saved replies)")
    s.add_argument("--threshold", type=float, help="fuzzy ratio below which a question is dropped (default 0.9)")
    s.add_argument("--canvas", action="append", default=[], help="Canvas text to check against instead of the bundle's sidecar; repeatable")

    s = sub.add_parser("format", help="render the Telegram brief from the stored brief")
    s.add_argument("key")
    s.add_argument("--record", help="the prep-log record (`preplog get` output, the record, or the prep-log file); - for stdin")
    s.add_argument("--brief", help="brief JSON to format instead of the record's / brief-output.json")
    s.add_argument("--drive-link", action="append", default=[], help="Drive link or path to list; repeatable")
    s.add_argument("--podcast-url", help="podcast link to include")
    s.add_argument("--no-podcast", action="store_true", help="say the podcast is unavailable instead of pending")
    s.add_argument("--dropped", type=int, help="number of questions dropped as hallucinations (default: from the record's history)")

    s = sub.add_parser("format-podcast", help="render the later 'podcast ready' message")
    s.add_argument("key")
    s.add_argument("--url", required=True)
    s.add_argument("--record", help="the prep-log record, for the class time line")

    sub.add_parser("prompt", help="print the brief-writer prompt head (from agents/brief-writer.md)")

    s = sub.add_parser("score", help="fuzzy-score one question against Canvas text (evals, debugging)")
    s.add_argument("--question", required=True)
    s.add_argument("--canvas", action="append", default=[], required=True)
    s.add_argument("--threshold", type=float)
    return p


def _emit(out, obj):
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def main(argv=None, env=None, out=None, stdin=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    try:
        args = build_parser().parse_args(argv)
        if not args.command:
            raise BriefError("USAGE", "missing command: one of %s" % ", ".join(COMMANDS))
        cfg = Config(env)
        _emit(out, COMMANDS[args.command](cfg, args, stdin))
        return 0
    except BriefError as e:
        _emit(out, e.to_json())
        return 2
    except Exception as e:      # a crash: exit 1, still one JSON object
        _emit(out, {"ok": False, "error": {"code": "INTERNAL", "retryable": False,
                                           "message": "%s: %s" % (type(e).__name__, _short(e))}})
        return 1


if __name__ == "__main__":
    sys.exit(main())
