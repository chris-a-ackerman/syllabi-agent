#!/usr/bin/env python3
"""nlm: NotebookLM podcasts for class-prep-agent (SYL-94), via notebooklm-py.

    nlm.py prep   <course> <date> <pdf>...            (or: nlm-prep <course> <date> <pdf>...)
    nlm.py status <notebook_id> [--course C --date D] (or: nlm-status <notebook_id> ...)
    nlm.py check                                      (auth smoke test: can we list notebooks?)

Prints exactly one JSON object on stdout (docs/tool-contract.md, "Common envelope"):

    {"ok": true, ...}                                                  exit 0
    {"ok": false, "error": {"code", "message", "retryable", ...}}      exit 2
    exit 1 = crash (unhandled exception; still prints an INTERNAL error object)

`prep` creates the notebook "<course> — <date>" (or reuses one with that title), adds each PDF
as a source (skipping ones already there), starts the audio overview if the sources are ready,
and returns at once. `status` reports the audio overview; when it is ready it downloads the
audio to $DATA_DIR/podcasts/<course>/<date>.<ext> and hands it to the drive skill's `drive-put`.
Both are idempotent, and `prep` refuses to start a second podcast for a session whose prep-log
record already has a `notebook_id`.

Environment:
    NLM_COOKIES_PATH   Playwright storage_state.json from `notebooklm login`
                       (default $DATA_DIR/secrets/notebooklm-cookies.json)
    DATA_DIR           persistent volume root, default /data. PDFs must be under
                       $DATA_DIR/readings/; audio lands under $DATA_DIR/podcasts/
    NLM_SOURCE_WAIT    seconds `prep` waits for sources to process before starting the audio
                       (default 20; if they are still processing, `status` starts it later)
    NLM_AUDIO_PROMPT   instructions for the audio overview (a sensible default is built in)
    NLM_DRIVE_PUT      path to the drive skill's drive-put (default ../../drive/scripts/drive-put)
    NLM_VERBOSE=1      same as --verbose: log "<op> <id>" lines to stderr

Security properties (requirements carried over from SYL-93's review pass):
    * Cookie values never appear in stdout or stderr: output is scrubbed against the cookie file.
    * Every path read or written is under $DATA_DIR (readings in, podcasts out).
    * Course/date/notebook ids are validated before they become path components.
    * The library's own state (NOTEBOOKLM_HOME) is kept under $DATA_DIR/secrets/.
    * Reading text inside the PDFs is untrusted content: it goes to NotebookLM as data, and
      nothing NotebookLM returns (titles, statuses, urls) is treated as an instruction.

Standard library only for this wrapper (Python 3.8+); `notebooklm-py` (Python 3.10+) is imported
lazily, so usage errors and the prep-log guard work even where it is not installed.
"""
import argparse
import asyncio
import datetime
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

API_TIMEOUT = 20                 # seconds per NotebookLM RPC; the agent has a 30 s reply budget
COMMAND_DEADLINE = 50            # seconds for the whole command; Maritime caps a command at 60 s
DEFAULT_SOURCE_WAIT = 20         # seconds `prep` waits for sources to process (poll every 2 s)
SOURCE_POLL_INTERVAL = 2
DRIVE_PUT_TIMEOUT = 40           # seconds for the drive-put subprocess
DEFAULT_COOKIES = "secrets/notebooklm-cookies.json"
DEFAULT_AUDIO_PROMPT = ("A pre-class briefing for a graduate student. Explain the main argument of "
                        "each reading, how the readings relate, and what to think about before class.")

_COURSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NOTEBOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# --------------------------------------------------------------------------- errors


class NlmError(Exception):
    """A tool error: printed as the {"ok": false, "error": {...}} envelope, exit code 2."""

    def __init__(self, code, message, retryable=False, status=None, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.detail = detail

    def to_json(self):
        err = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.status is not None:
            err["status"] = self.status
        if self.detail:
            err["detail"] = self.detail
        return {"ok": False, "error": err}


# Mapping from notebooklm-py exception classes to tool codes, matched by class *name* anywhere in
# the MRO so the wrapper (and its tests) never import the library at module load. Order matters:
# AuthError and RateLimitError are subclasses of RPCError, so they come first.
_EXCEPTION_MAP = [
    ({"MissingDependencyError"}, "NLM_NOT_INSTALLED", False),
    ({"AuthError", "AuthExtractionError", "HeadlessReauthError", "HeadlessLoginRequiredError",
      "ConfigurationError"}, "NLM_AUTH", True),
    ({"RateLimitError", "NotebookLimitError"}, "NLM_RATE_LIMIT", True),
    ({"NotebookNotFoundError", "NotFoundError"}, "NLM_NOT_FOUND", False),
    ({"SourceAddError", "SourceProcessingError", "PlayBookNotExportableError", "SourceTimeoutError",
      "ValidationError"}, "NLM_SOURCE_REJECTED", False),
    ({"ClientError", "ArtifactFeatureUnavailableError", "UnsupportedOperationError"}, "NLM_UNAVAILABLE", False),
    ({"NetworkError", "RPCTimeoutError", "ServerError", "DecodingError", "RPCError", "WaitTimeoutError",
      "LockUnavailableError", "ArtifactDownloadError", "ArtifactNotReadyError", "TimeoutError",
      "ConnectionError", "OSError"}, "NLM_UNAVAILABLE", True),
]


def classify(exc):
    """Map a library (or OS) exception to (code, retryable). Unknown exceptions are NLM_UNAVAILABLE."""
    names = [klass.__name__ for klass in type(exc).__mro__]
    for wanted, code, retryable in _EXCEPTION_MAP:
        if any(name in wanted for name in names):
            return code, retryable
    return "NLM_UNAVAILABLE", False


def _short(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _wrap(exc, what):
    """Turn any exception raised while talking to NotebookLM into an NlmError."""
    if isinstance(exc, NlmError):
        return exc
    code, retryable = classify(exc)
    message = "%s: %s: %s" % (what, type(exc).__name__, _short(exc) or "no detail")
    if code == "NLM_AUTH":
        message = "%s: NotebookLM rejected the cookies (%s). Refresh $NLM_COOKIES_PATH with `notebooklm login`" % (
            what, type(exc).__name__)
    elif code == "NLM_NOT_INSTALLED":
        message = "%s: notebooklm-py is missing a dependency: %s" % (what, _short(exc))
    return NlmError(code, message, retryable=retryable)


# --------------------------------------------------------------------------- config


class Config:
    def __init__(self, env, verbose=False):
        self.data_dir = os.path.realpath(env.get("DATA_DIR") or "/data")
        self.cookies_path = env.get("NLM_COOKIES_PATH") or os.path.join(self.data_dir, DEFAULT_COOKIES)
        self.verbose = verbose or env.get("NLM_VERBOSE") == "1"
        self.audio_prompt = (env.get("NLM_AUDIO_PROMPT") or DEFAULT_AUDIO_PROMPT).strip()
        try:
            self.source_wait = max(0.0, float(env.get("NLM_SOURCE_WAIT") or DEFAULT_SOURCE_WAIT))
        except ValueError:
            raise NlmError("USAGE", "NLM_SOURCE_WAIT must be a number of seconds")
        here = os.path.dirname(os.path.abspath(__file__))
        self.drive_put = env.get("NLM_DRIVE_PUT") or os.path.join(here, "..", "..", "drive", "scripts", "drive-put")
        # Keep notebooklm-py's own files (profiles, locks) on the persistent volume, never in the workspace.
        self.nlm_home = env.get("NOTEBOOKLM_HOME") or os.path.join(self.data_dir, "secrets", "notebooklm")

    @property
    def readings_root(self):
        return os.path.join(self.data_dir, "readings")

    @property
    def podcasts_root(self):
        return os.path.join(self.data_dir, "podcasts")

    @property
    def prep_log_path(self):
        return os.path.join(self.data_dir, "memory", "prep-log.json")

    def log(self, message):
        if self.verbose:
            sys.stderr.write("nlm: %s\n" % message)


def notebook_title(course, date):
    return "%s — %s" % (course, date)


_TITLE_RE = re.compile(r"^(?P<course>[A-Za-z0-9][A-Za-z0-9._-]{0,63})\s+[—–-]+\s+(?P<date>\d{4}-\d{2}-\d{2})$")


def parse_notebook_title(title):
    """Inverse of notebook_title(); returns (course, date) or None."""
    m = _TITLE_RE.match((title or "").strip())
    return (m.group("course"), m.group("date")) if m else None


def check_course(course):
    if not _COURSE_RE.match(course or ""):
        raise NlmError("USAGE", "course must be like MAS.665 (letters, digits, . _ -)", detail={"course": course})
    return course


def check_date(date):
    if not _DATE_RE.match(date or ""):
        raise NlmError("USAGE", "date must be YYYY-MM-DD", detail={"date": date})
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise NlmError("USAGE", "date is not a real calendar date", detail={"date": date})
    return date


def check_notebook_id(notebook_id):
    if not _NOTEBOOK_ID_RE.match(notebook_id or ""):
        raise NlmError("USAGE", "notebook_id must be a NotebookLM id (letters, digits, - _)")
    return notebook_id


def _under(root, path):
    real = os.path.realpath(path)
    root = os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def check_source_paths(cfg, paths):
    """Every source must be an existing regular file under $DATA_DIR/readings/ (realpath, so
    symlinks can't escape). Returns the resolved paths."""
    out = []
    for path in paths:
        if not _under(cfg.readings_root, path):
            raise NlmError("USAGE", "source files must be under %s/" % cfg.readings_root, detail={"path": path})
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            raise NlmError("USAGE", "source file not found: %s" % path, detail={"path": path})
        if os.path.getsize(real) == 0:
            raise NlmError("USAGE", "source file is empty: %s" % path, detail={"path": path})
        out.append(real)
    return out


# --------------------------------------------------------------------------- secrets

def load_cookie_values(path):
    """Every secret-looking string in the Playwright storage_state file, so output can be scrubbed.
    Best effort: a missing or malformed file yields nothing (and NLM_AUTH later)."""
    values = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return values

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key in ("value", "token", "master_token", "oauth_token") and isinstance(val, str):
                    if len(val) >= 8:
                        values.add(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return values


def read_prep_log_notebook_id(cfg, course, date):
    """The prep-log guard: the notebook_id already recorded for <course>@<date>, if any."""
    try:
        with open(cfg.prep_log_path, "r", encoding="utf-8") as fh:
            log = json.load(fh)
    except (OSError, ValueError):
        return None
    sessions = log.get("sessions") if isinstance(log, dict) else None
    record = sessions.get("%s@%s" % (course, date)) if isinstance(sessions, dict) else None
    notebook_id = record.get("notebook_id") if isinstance(record, dict) else None
    return notebook_id if isinstance(notebook_id, str) and notebook_id else None


# --------------------------------------------------------------------------- NotebookLM client
# `open_client`, `sleep` and `drive_put` are the seams the tests replace. Nothing else does I/O
# with NotebookLM or the drive skill.


def _open_client(cfg):
    """Async context manager yielding a notebooklm-py client bound to the cookie file."""
    if not os.path.isfile(cfg.cookies_path):
        raise NlmError("NLM_AUTH", "no NotebookLM cookie file at %s: run `notebooklm login` on your laptop and "
                       "upload its storage_state.json there" % cfg.cookies_path, retryable=False)
    os.environ.setdefault("NOTEBOOKLM_HOME", cfg.nlm_home)
    try:
        from notebooklm import NotebookLMClient  # noqa: WPS433 (lazy: optional dependency)
    except ImportError as e:
        raise NlmError("NLM_NOT_INSTALLED", "notebooklm-py is not installed for %s (%s): "
                       "pip install notebooklm-py" % (sys.executable, _short(e)))
    return NotebookLMClient.from_storage(path=cfg.cookies_path, timeout=API_TIMEOUT)


async def _sleep(seconds):
    await asyncio.sleep(seconds)


def _drive_put(cfg, local_path, remote_dir):
    """Run the drive skill's drive-put and return its JSON envelope (or a synthetic error one)."""
    script = cfg.drive_put
    if not (os.path.isfile(script) and os.access(script, os.X_OK)):
        return {"ok": False, "error": {"code": "DRIVE_NOT_INSTALLED", "retryable": True,
                                       "message": "drive-put not found at %s" % script}}
    try:
        proc = subprocess.run([script, local_path, remote_dir], capture_output=True, text=True,
                              timeout=DRIVE_PUT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "DRIVE_UNAVAILABLE", "retryable": True,
                                       "message": "drive-put did not finish within %d s" % DRIVE_PUT_TIMEOUT}}
    except OSError as e:
        return {"ok": False, "error": {"code": "DRIVE_UNAVAILABLE", "retryable": True,
                                       "message": "could not run drive-put: %s" % _short(e)}}
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    try:
        envelope = json.loads(lines[-1]) if lines else None
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or "ok" not in envelope:
        return {"ok": False, "error": {"code": "DRIVE_UNAVAILABLE", "retryable": True,
                                       "message": "drive-put printed no JSON (exit %d)" % proc.returncode}}
    return envelope


open_client = _open_client
sleep = _sleep
drive_put = _drive_put


# --------------------------------------------------------------------------- helpers over the library


def _enum_str(value):
    value = getattr(value, "value", value)
    value = getattr(value, "name", value) if not isinstance(value, str) else value
    return str(value if value is not None else "").lower()


def artifact_state(artifact):
    """'ready' | 'failed' | 'pending' from an Artifact/GenerationStatus status of any shape."""
    s = _enum_str(getattr(artifact, "status", artifact))
    if s in ("completed", "complete", "ready", "done", "succeeded", "success"):
        return "ready"
    if s in ("failed", "failure", "error", "errored", "cancelled", "canceled"):
        return "failed"
    return "pending"


def source_state(source):
    s = _enum_str(getattr(source, "status", source))
    if s in ("ready", "completed", "complete", "done", "enabled"):
        return "ready"
    if s in ("error", "failed", "failure", "rejected", "disabled"):
        return "error"
    return "pending"


def is_audio(artifact):
    kind = _enum_str(getattr(artifact, "artifact_type", getattr(artifact, "type", "")))
    return "audio" in kind


def _names_for(path):
    base = os.path.basename(path)
    root, _ = os.path.splitext(base)
    return {base.lower(), root.lower()}


async def _call(fn, *args, **kwargs):
    """Call a library method, dropping keyword arguments the installed version doesn't know.
    notebooklm-py is unofficial and renames keywords between releases (instructions → prompt)."""
    while True:
        try:
            return await fn(*args, **kwargs)
        except TypeError as e:
            m = re.search(r"unexpected keyword argument '(\w+)'", str(e))
            if not m or m.group(1) not in kwargs:
                raise
            kwargs = dict(kwargs)
            del kwargs[m.group(1)]


async def find_notebook(client, title):
    try:
        notebooks = await client.notebooks.list()
    except Exception as e:
        raise _wrap(e, "listing notebooks")
    for nb in notebooks or []:
        if (getattr(nb, "title", None) or "").strip() == title:
            return nb
    return None


async def list_audio_artifacts(client, notebook_id):
    try:
        artifacts = await client.artifacts.list(notebook_id)
    except Exception as e:
        raise _wrap(e, "listing artifacts of notebook %s" % notebook_id)
    return [a for a in artifacts or [] if is_audio(a)]


async def list_sources(client, notebook_id):
    try:
        return list(await client.sources.list(notebook_id) or [])
    except Exception as e:
        raise _wrap(e, "listing sources of notebook %s" % notebook_id)


async def start_audio(cfg, client, notebook_id, title):
    cfg.log("generate_audio %s" % notebook_id)
    try:
        status = await _call(client.artifacts.generate_audio, notebook_id, prompt=cfg.audio_prompt, title=title)
    except Exception as e:
        raise _wrap(e, "starting the audio overview")
    return {"task_id": getattr(status, "task_id", None), "artifact_id": getattr(status, "artifact_id", None)}


def _pick_audio(artifacts):
    """The audio artifact that matters: a ready one first, then one in flight, else the last failed."""
    by_state = {"ready": [], "pending": [], "failed": []}
    for a in artifacts:
        by_state[artifact_state(a)].append(a)
    for state in ("ready", "pending", "failed"):
        if by_state[state]:
            return by_state[state][-1], state
    return None, None


# --------------------------------------------------------------------------- commands


async def cmd_prep(cfg, args):
    course = check_course(args.course)
    date = check_date(args.date)
    paths = check_source_paths(cfg, args.pdfs)
    title = notebook_title(course, date)

    recorded = read_prep_log_notebook_id(cfg, course, date)
    if recorded:
        cfg.log("prep-log already has notebook_id %s for %s@%s; not starting a second podcast" % (recorded, course, date))
        return {"ok": True, "notebook_id": recorded, "notebook_title": title, "created": False,
                "sources_added": 0, "sources_reused": 0, "sources_rejected": [],
                "audio": "already-started", "skipped": True,
                "note": "prep-log record %s@%s already has a notebook_id" % (course, date)}

    async with open_client(cfg) as client:
        nb = await find_notebook(client, title)
        created = nb is None
        if created:
            cfg.log("create notebook %r" % title)
            try:
                nb = await client.notebooks.create(title)
            except Exception as e:
                raise _wrap(e, "creating the notebook")
        notebook_id = getattr(nb, "id", None)
        if not notebook_id:
            raise NlmError("NLM_UNAVAILABLE", "NotebookLM returned a notebook without an id", retryable=True)

        existing = [] if created else await list_sources(client, notebook_id)
        existing_names = set()
        for src in existing:
            existing_names.update(_names_for(getattr(src, "title", "") or ""))

        added, reused, rejected = [], [], []
        for path in paths:
            if _names_for(path) & existing_names:
                reused.append(path)
                continue
            cfg.log("add_file %s -> %s" % (os.path.basename(path), notebook_id))
            try:
                src = await _call(client.sources.add_file, notebook_id, path, wait=False)
                added.append(getattr(src, "id", None))
            except Exception as e:
                err = _wrap(e, "adding %s" % os.path.basename(path))
                if err.code == "NLM_SOURCE_REJECTED":
                    rejected.append({"path": path, "code": err.code, "message": err.message})
                    continue
                raise err
        if not added and not reused:
            raise NlmError("NLM_SOURCE_REJECTED", "NotebookLM rejected every source; nothing to make a podcast from",
                           detail={"rejected": rejected})

        result = {"ok": True, "notebook_id": notebook_id, "notebook_title": title, "created": created,
                  "sources_added": len(added), "sources_reused": len(reused), "sources_rejected": rejected}

        audio, state = _pick_audio(await list_audio_artifacts(client, notebook_id))
        if audio is not None and state != "failed":
            result.update({"audio": "already-started", "artifact_id": getattr(audio, "id", None)})
            return result

        pending = await wait_for_sources(cfg, client, notebook_id, expected=len(added) + len(reused))
        if pending:
            result.update({"audio": "deferred", "sources_pending": pending,
                           "note": "sources still processing after %.0f s; nlm-status starts the audio" % cfg.source_wait})
            return result
        result.update(await start_audio(cfg, client, notebook_id, title))
        result["audio"] = "started"
        return result


async def wait_for_sources(cfg, client, notebook_id, expected):
    """Poll until no source is still processing, or the budget is spent. Returns how many are pending.
    `expected` sources are known to exist; a shorter list means NotebookLM hasn't caught up yet."""
    deadline = cfg.source_wait
    waited = 0.0
    while True:
        sources = await list_sources(client, notebook_id)
        states = [source_state(s) for s in sources]
        pending = states.count("pending") + max(0, expected - len(sources))
        if pending == 0:
            if "ready" not in states:
                raise NlmError("NLM_SOURCE_REJECTED", "every source failed to process", retryable=False)
            return 0
        if waited >= deadline:
            return pending
        step = min(SOURCE_POLL_INTERVAL, deadline - waited)
        await sleep(step)
        waited += step


def _podcast_paths(cfg, course, date):
    folder = os.path.join(cfg.podcasts_root, course)
    return folder, [os.path.join(folder, date + ext) for ext in (".m4a", ".mp3", ".wav", ".bin")]


def sniff_audio_ext(path):
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ".mp3"
    if head[:4] == b"RIFF":
        return ".wav"
    return ".bin"


async def download_audio(cfg, client, notebook_id, artifact, course, date):
    folder, candidates = _podcast_paths(cfg, course, date)
    for existing in candidates:
        if os.path.isfile(existing) and os.path.getsize(existing) > 0:
            return existing, True
    os.makedirs(folder, exist_ok=True)
    part = os.path.join(folder, date + ".part")
    cfg.log("download_audio %s/%s" % (notebook_id, getattr(artifact, "id", "?")))
    try:
        await _call(client.artifacts.download_audio, notebook_id, part, artifact_id=getattr(artifact, "id", None))
    except Exception as e:
        raise _wrap(e, "downloading the audio overview")
    if not os.path.isfile(part) or os.path.getsize(part) == 0:
        raise NlmError("NLM_UNAVAILABLE", "the audio download produced no file", retryable=True)
    final = os.path.join(folder, date + sniff_audio_ext(part))
    os.replace(part, final)
    return final, False


async def cmd_status(cfg, args):
    notebook_id = check_notebook_id(args.notebook_id)
    course = check_course(args.course) if args.course else None
    date = check_date(args.date) if args.date else None
    if bool(course) != bool(date):
        raise NlmError("USAGE", "pass both --course and --date, or neither")

    async with open_client(cfg) as client:
        if not course:
            try:
                nb = await client.notebooks.get(notebook_id)
            except Exception as e:
                raise _wrap(e, "looking up notebook %s" % notebook_id)
            parsed = parse_notebook_title(getattr(nb, "title", None))
            if not parsed:
                raise NlmError("USAGE", "the notebook title is not '<course> — <date>'; pass --course and --date",
                               detail={"title": _short(getattr(nb, "title", ""))})
            course, date = parsed

        artifacts = await list_audio_artifacts(client, notebook_id)
        audio, state = _pick_audio(artifacts)
        base = {"ok": True, "notebook_id": notebook_id, "course": course, "date": date}

        if audio is None:
            # Nothing started yet (prep deferred it, or it was never called). Start it if we can.
            sources = await list_sources(client, notebook_id)
            states = [source_state(s) for s in sources]
            if not sources:
                return dict(base, status="failed", reason="the notebook has no sources")
            if "pending" in states:
                return dict(base, status="pending", audio="waiting-for-sources", sources_pending=states.count("pending"))
            if "ready" not in states:
                return dict(base, status="failed", reason="every source failed to process")
            started = await start_audio(cfg, client, notebook_id, notebook_title(course, date))
            return dict(base, status="pending", audio="started", **started)

        if state == "failed":
            return dict(base, status="failed", artifact_id=getattr(audio, "id", None),
                        reason="NotebookLM reports the audio overview failed (%s)" % _enum_str(getattr(audio, "status", "")))
        if state == "pending":
            return dict(base, status="pending", artifact_id=getattr(audio, "id", None),
                        artifact_status=_enum_str(getattr(audio, "status", "")) or "pending")

        local_path, reused = await download_audio(cfg, client, notebook_id, audio, course, date)
        remote_dir = "%s/%s" % (course, date)
        cfg.log("drive-put %s %s" % (os.path.basename(local_path), remote_dir))
        drive = drive_put(cfg, local_path, remote_dir)
        result = dict(base, status="ready", artifact_id=getattr(audio, "id", None),
                      audio_url=getattr(audio, "url", None), local_path=local_path, downloaded=not reused,
                      bytes=os.path.getsize(local_path))
        if drive.get("ok"):
            result["drive_link"] = drive.get("share_link")
            result["remote_path"] = drive.get("remote_path")
        else:
            err = drive.get("error") or {}
            result["drive_link"] = None
            result["drive_error"] = {"code": err.get("code", "DRIVE_UNAVAILABLE"),
                                     "message": _short(err.get("message", "")),
                                     "retryable": bool(err.get("retryable", True))}
        return result


async def cmd_check(cfg, args):
    async with open_client(cfg) as client:
        try:
            notebooks = await client.notebooks.list()
        except Exception as e:
            raise _wrap(e, "listing notebooks")
    return {"ok": True, "notebooks": len(notebooks or []), "cookies_path": cfg.cookies_path}


COMMANDS = {"prep": cmd_prep, "status": cmd_status, "check": cmd_check}
# When invoked through the nlm-prep / nlm-status symlinks the subcommand is implied by argv[0].
IMPLIED = {"nlm-prep": "prep", "nlm-status": "status", "nlm-check": "check"}


# --------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise NlmError("USAGE", message)


def build_parser(prog="nlm"):
    p = _Parser(prog=prog, description="NotebookLM audio overviews for class-prep-agent. Prints one JSON object.")
    p.add_argument("-v", "--verbose", action="store_true", help="log '<op> <id>' lines to stderr (never cookies)")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("prep", help="create/reuse the notebook, add the PDFs, start the audio overview")
    s.add_argument("course")
    s.add_argument("date", metavar="YYYY-MM-DD")
    s.add_argument("pdfs", nargs="+", metavar="pdf", help="files under $DATA_DIR/readings/")

    s = sub.add_parser("status", help="check the audio overview; download + drive-put when ready")
    s.add_argument("notebook_id")
    s.add_argument("--course")
    s.add_argument("--date", metavar="YYYY-MM-DD")

    sub.add_parser("check", help="auth smoke test: list notebooks")
    return p


def _emit(out, obj, secrets=()):
    text = json.dumps(obj, ensure_ascii=False)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    out.write(text + "\n")
    out.flush()


class _Scrubbed:
    """stderr wrapper that redacts cookie values from anything logged (ours or the library's)."""

    def __init__(self, stream, secrets):
        self._stream = stream
        self._secrets = secrets

    def write(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return self._stream.write(text)

    def flush(self):
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


async def _run(cfg, args):
    return await asyncio.wait_for(COMMANDS[args.command](cfg, args), timeout=COMMAND_DEADLINE)


def implied_argv(prog, argv):
    """The nlm-prep / nlm-status symlinks imply the subcommand. Leading -v/--verbose stay in front."""
    command = IMPLIED.get(prog)
    argv = list(argv)
    if not command:
        return argv
    flags = []
    while argv and argv[0] in ("-v", "--verbose"):
        flags.append(argv.pop(0))
    if argv[:1] == [command]:
        argv = argv[1:]
    return flags + [command] + argv


def main(argv=None, env=None, out=None, prog=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    prog = os.path.basename(sys.argv[0] or "nlm") if prog is None else prog
    argv = implied_argv(prog, argv)
    cookies_path = env.get("NLM_COOKIES_PATH") or os.path.join(env.get("DATA_DIR") or "/data", DEFAULT_COOKIES)
    secrets = sorted(load_cookie_values(cookies_path), key=len, reverse=True)
    real_stderr = sys.stderr
    sys.stderr = _Scrubbed(real_stderr, secrets)
    try:
        args = build_parser(prog if prog in IMPLIED else "nlm").parse_args(argv)
        if not args.command:
            raise NlmError("USAGE", "missing command: one of %s" % ", ".join(COMMANDS))
        cfg = Config(env, verbose=args.verbose)
        result = asyncio.run(_run(cfg, args))
        _emit(out, result, secrets)
        return 0
    except NlmError as e:
        _emit(out, e.to_json(), secrets)
        return 2
    except asyncio.TimeoutError:
        _emit(out, NlmError("NLM_UNAVAILABLE", "NotebookLM did not answer within %d s; try again on the next poll"
                            % COMMAND_DEADLINE, retryable=True).to_json(), secrets)
        return 2
    except Exception as e:      # a crash: exit 1, still one JSON object, never a cookie
        _emit(out, {"ok": False, "error": {"code": "INTERNAL", "retryable": False,
                                           "message": "%s: %s" % (type(e).__name__, _short(e))}}, secrets)
        return 1
    finally:
        sys.stderr = real_stderr


if __name__ == "__main__":
    sys.exit(main())
