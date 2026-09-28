#!/usr/bin/env python3
"""nlm: NotebookLM podcasts for class-prep-agent (SYL-94).

A thin wrapper over notebooklm-py's own `notebooklm` CLI: every NotebookLM call is one
`notebooklm ... --json` subprocess run from an argv list (never a shell), and only the fields
named below are read back from its JSON.

    nlm.py prep   <course_code> <date> <pdf>... --topic "<session topic>"   (or: nlm-prep ...)
    nlm.py status <notebook_id> <task_id> [--course C --date D]            (or: nlm-status ...)
    nlm.py check                                                           (auth smoke test)

Prints exactly one JSON object on stdout:

    prep    {"notebook_id": "...", "task_id": "..."}                                   exit 0
    status  {"status": "pending"|"ready"|"failed", "local_path": ..., "drive_url": ...} exit 0
    error   {"error": "<CODE>", "message": "...", "retryable": bool}                   exit 2
    auth    {"error": "NLM_AUTH"}   (nothing else: never any part of the auth payload)  exit 2
    crash   {"error": "INTERNAL", ...}                                                 exit 1

`prep`:
    1. `notebooklm auth check --test --json`; AUTH_REQUIRED / status "error" -> {"error": "NLM_AUTH"}
    2. `notebooklm create "<course_code> — <date>" --use --json` -> notebook id
    3. per PDF: `notebooklm source add <pdf> --title "<name>" -n <id> --json`
    4. `notebooklm generate audio "<prompt>" -n <id> --no-wait --json` -> task id
       (the prompt template lives in SKILL.md; the session topic goes in as data, one argv element)
    5. returns {notebook_id, task_id} at once.
`status`:
    `notebooklm artifact poll <task_id> -n <id> --json`; when completed:
    `notebooklm download audio /data/podcasts/<course>-<date>.mp3 -n <id> --latest --json`, then the
    drive skill's `drive-put`, reading `web_url` -> {status: "ready", local_path, drive_url}.
    `status` never calls `generate`.

Environment:
    DATA_DIR               persistent volume root (default /data). PDFs must be under
                           $DATA_DIR/readings/; podcasts land in $DATA_DIR/podcasts/
    NOTEBOOKLM_HOME        notebooklm-py's auth/config dir (default $DATA_DIR/notebooklm). Passed to
                           the CLI so `~/.notebooklm` resolves there. Credential files in it are
                           kept chmod 600 (tightened, with a warning, when looser).
    NOTEBOOKLM_AUTH_JSON   optional inline auth (a Maritime secret); passed through untouched
    NLM_BIN                the notebooklm executable (default: `notebooklm` on PATH)
    NLM_DRIVE_PUT          the drive skill's drive-put (default ../../drive/scripts/drive-put)
    NLM_DRIVE_DIR          Drive folder for podcasts, relative to the drive root (default Podcasts)
    NLM_DEADLINE           seconds for the whole command (default 50; Maritime caps a command at 60)
    NLM_VERBOSE=1          same as -v: log "<op>" lines to stderr (never payloads)

Standard library only (Python 3.8+).
"""
import argparse
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import time

DEFAULT_DEADLINE = 50.0          # seconds; Maritime caps a shell command at 60 s
DRIVE_MIN_BUDGET = 15.0          # below this, leave the upload for the next poll
DEFAULT_DRIVE_DIR = "Podcasts"
TOPIC_MAX = 300
PROMPT_START = "<!-- nlm:prompt:start -->"
PROMPT_END = "<!-- nlm:prompt:end -->"
TOPIC_SLOT = "<session topic>"
FALLBACK_PROMPT = ("A class-prep overview for an MBA student: cover each reading's core argument "
                   "and how they relate to <session topic>.")
# notebooklm-py's credential files. They are equivalent to a logged-in Google session.
SECRET_FILES = ("storage_state.json", "master_token.json")

_COURSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

HERE = os.path.dirname(os.path.realpath(__file__))
SKILL_MD = os.path.join(HERE, "..", "SKILL.md")

# --------------------------------------------------------------------------- errors


class NlmError(Exception):
    """A tool error: printed as {"error": code, ...}, exit code 2."""

    def __init__(self, code, message="", retryable=False):
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_json(self):
        if self.code == "NLM_AUTH":
            return {"error": "NLM_AUTH"}          # SYL-94 Security: nothing else, ever
        return {"error": self.code, "message": self.message, "retryable": self.retryable}


def auth_error():
    return NlmError("NLM_AUTH")


# notebooklm CLI `--json` error codes (docs/cli-exit-codes.md in notebooklm-py) -> ours.
_CLI_CODES = {
    "AUTH_REQUIRED": ("NLM_AUTH", True),
    "AUTH_ERROR": ("NLM_AUTH", True),
    "RATE_LIMITED": ("NLM_RATE_LIMIT", True),
    "NOTEBOOK_LIMIT": ("NLM_RATE_LIMIT", True),
    "NOT_FOUND": ("NLM_NOT_FOUND", False),
    "VALIDATION_ERROR": ("NLM_INVALID", False),
    "GENERATION_FAILED": ("NLM_GENERATION_FAILED", False),
    "NETWORK_ERROR": ("NLM_UNAVAILABLE", True),
    "ARTIFACT_TIMEOUT": ("NLM_UNAVAILABLE", True),
    "UNCONFIRMED_WRITE": ("NLM_UNAVAILABLE", True),
    "CONFIG_ERROR": ("NLM_UNAVAILABLE", False),
    "NOTEBOOKLM_ERROR": ("NLM_UNAVAILABLE", False),
    "UNEXPECTED_ERROR": ("NLM_UNAVAILABLE", False),
}


# --------------------------------------------------------------------------- config


class Config:
    def __init__(self, env, verbose=False):
        self.env = env
        self.data_dir = os.path.realpath(env.get("DATA_DIR") or "/data")
        self.nlm_home = env.get("NOTEBOOKLM_HOME") or os.path.join(self.data_dir, "notebooklm")
        self.nlm_bin = env.get("NLM_BIN") or "notebooklm"
        self.drive_put = env.get("NLM_DRIVE_PUT") or os.path.join(HERE, "..", "..", "drive", "scripts", "drive-put")
        self.drive_dir = env.get("NLM_DRIVE_DIR") or DEFAULT_DRIVE_DIR
        self.verbose = verbose or env.get("NLM_VERBOSE") == "1"
        try:
            self.deadline = float(env.get("NLM_DEADLINE") or DEFAULT_DEADLINE)
        except ValueError:
            raise NlmError("USAGE", "NLM_DEADLINE must be a number of seconds")
        self._t0 = time.monotonic()

    readings_root = property(lambda self: os.path.join(self.data_dir, "readings"))
    podcasts_root = property(lambda self: os.path.join(self.data_dir, "podcasts"))
    jobs_root = property(lambda self: os.path.join(self.data_dir, "work", "nlm"))
    prep_log_path = property(lambda self: os.path.join(self.data_dir, "memory", "prep-log.json"))

    def remaining(self):
        return self.deadline - (time.monotonic() - self._t0)

    def child_env(self):
        """The CLI's environment: ours, with NOTEBOOKLM_HOME on the persistent volume.
        NOTEBOOKLM_AUTH_JSON (if set) passes through untouched."""
        env = dict(self.env)
        env["NOTEBOOKLM_HOME"] = self.nlm_home
        if not (env.get("NOTEBOOKLM_AUTH_JSON") or "").strip():
            env.pop("NOTEBOOKLM_AUTH_JSON", None)   # empty would shadow the file-backed login
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def log(self, message):
        if self.verbose:
            sys.stderr.write("nlm: %s\n" % message)


# --------------------------------------------------------------------------- validation


def check_course(course):
    if not _COURSE_RE.match(course or ""):
        raise NlmError("USAGE", "course_code must be like MAS.665 (letters, digits, . _ -)")
    return course


def check_date(date):
    if not _DATE_RE.match(date or ""):
        raise NlmError("USAGE", "date must be YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise NlmError("USAGE", "date is not a real calendar date")
    return date


def check_id(value, what):
    if not _ID_RE.match(value or ""):
        raise NlmError("USAGE", "%s must be a NotebookLM id (letters, digits, - _)" % what)
    return value


def clean_topic(topic):
    """The session topic is data from syllabi/Canvas going into a prompt. It travels as one argv
    element (no shell), so quotes and `$()` are inert; control characters are flattened so it
    can't fake extra prompt lines, and it is length-capped."""
    topic = " ".join(_CONTROL_RE.sub(" ", topic or "").split())
    if not topic:
        raise NlmError("USAGE", "--topic (the session topic) is required")
    return topic[:TOPIC_MAX]


def _under(root, path):
    real, root = os.path.realpath(path), os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def check_pdfs(cfg, paths):
    out = []
    for path in paths:
        if not _under(cfg.readings_root, path):
            raise NlmError("USAGE", "source files must be under %s/" % cfg.readings_root)
        real = os.path.realpath(path)
        if not os.path.isfile(real) or os.path.getsize(real) == 0:
            raise NlmError("USAGE", "source file missing or empty: %s" % os.path.basename(path))
        out.append(real)
    return out


def notebook_title(course, date):
    return "%s — %s" % (course, date)


def podcast_path(cfg, course, date):
    return os.path.join(cfg.podcasts_root, "%s-%s.mp3" % (course, date))


def load_prompt_template(path=None):
    """The podcast prompt template from SKILL.md (between the nlm:prompt markers)."""
    try:
        with open(path or SKILL_MD, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return FALLBACK_PROMPT
    start, end = text.find(PROMPT_START), text.find(PROMPT_END)
    if start < 0 or end < start:
        return FALLBACK_PROMPT
    template = " ".join(text[start + len(PROMPT_START):end].split())
    return template if TOPIC_SLOT in template else FALLBACK_PROMPT


def build_prompt(topic, template=None):
    # str.replace, not str.format: braces in the topic stay literal.
    return (template or load_prompt_template()).replace(TOPIC_SLOT, clean_topic(topic))


# --------------------------------------------------------------------------- auth-file permissions


def secure_auth_home(cfg):
    """Keep NOTEBOOKLM_HOME 700 and its credential files 600. Looser modes are tightened with a
    warning on stderr (path only); if they can't be tightened, refuse to run."""
    home = cfg.nlm_home
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
    except OSError as e:
        raise NlmError("NLM_AUTH_PERMS", "cannot create NOTEBOOKLM_HOME %s: %s" % (home, e.strerror))
    tightened = []

    def tighten(path, mode):
        try:
            current = stat.S_IMODE(os.lstat(path).st_mode)
        except OSError:
            return
        if current & 0o077:
            try:
                os.chmod(path, mode)
            except OSError as e:
                raise NlmError("NLM_AUTH_PERMS", "%s is mode %o and cannot be tightened to %o (%s); fix it "
                               "before running nlm" % (path, current, mode, e.strerror))
            tightened.append(path)

    tighten(home, 0o700)
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            if name in SECRET_FILES:
                tighten(os.path.join(root, name), 0o600)
    for path in tightened:
        sys.stderr.write("nlm: warning: %s had loose permissions; tightened\n" % path)
    return tightened


# --------------------------------------------------------------------------- the notebooklm CLI


def run_notebooklm(cfg, argv, what):
    """Run `notebooklm <argv>` (argv list, no shell) and return its JSON object.

    Returns (exit_code, obj). Raises NlmError for a missing binary, a timeout, or output that
    isn't a JSON object. The CLI's stderr is captured and dropped: it can carry cookie detail."""
    budget = cfg.remaining()
    if budget <= 1:
        raise NlmError("NLM_TIMEOUT", "no time left for %s; try again on the next poll" % what, retryable=True)
    cfg.log(what)
    try:
        proc = subprocess.run([cfg.nlm_bin] + list(argv), capture_output=True, text=True,
                              env=cfg.child_env(), timeout=budget, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise NlmError("NLM_NOT_INSTALLED", "the notebooklm CLI was not found (%s): "
                       "pip install \"notebooklm-py[headless]\"" % cfg.nlm_bin)
    except subprocess.TimeoutExpired:
        raise NlmError("NLM_TIMEOUT", "%s did not finish within the %.0f s budget; try again on the "
                       "next poll" % (what, cfg.deadline), retryable=True)
    except OSError as e:
        raise NlmError("NLM_NOT_INSTALLED", "could not run %s: %s" % (cfg.nlm_bin, e.strerror))
    try:
        obj = json.loads(proc.stdout or "")
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        raise NlmError("NLM_UNAVAILABLE", "%s: notebooklm printed no JSON (exit %d)" % (what, proc.returncode),
                       retryable=proc.returncode != 2)
    return proc.returncode, obj


def raise_for_cli_error(rc, obj, what, invalid_code=None):
    """Turn a CLI `--json` error envelope (or a non-zero exit) into an NlmError."""
    # Error envelopes exit non-zero. `artifact poll` carries an "error" *field* (the generation
    # error text) on success, so on exit 0 only the envelope marker `"error": true` counts.
    if rc == 0 and obj.get("error") is not True:
        return
    code = obj.get("code") if isinstance(obj.get("code"), str) else None
    ours, retryable = _CLI_CODES.get(code or "", ("NLM_UNAVAILABLE", rc != 2))
    if ours == "NLM_AUTH":
        raise auth_error()
    if ours == "NLM_INVALID" and invalid_code:
        ours = invalid_code
    # Only the CLI's code goes out, never its message text (it can quote cookies or file content).
    raise NlmError(ours, "%s failed (notebooklm %s, exit %d)" % (what, code or "error", rc), retryable=retryable)


def auth_check(cfg):
    """`notebooklm auth check --test --json`: stale or missing auth -> NLM_AUTH."""
    rc, obj = run_notebooklm(cfg, ["auth", "check", "--test", "--json"], "auth check")
    if obj.get("code") in ("AUTH_REQUIRED", "AUTH_ERROR"):
        raise auth_error()
    if rc != 0 or obj.get("status") != "ok":
        raise auth_error()        # the check itself failed: storage/cookies/token fetch


def _str_field(obj, *keys):
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj if isinstance(obj, str) and obj else None


def nlm_create(cfg, title):
    rc, obj = run_notebooklm(cfg, ["create", title, "--use", "--json"], "create notebook")
    raise_for_cli_error(rc, obj, "create notebook")
    nb_id = _str_field(obj, "notebook", "id") or _str_field(obj, "active_notebook_id")
    if not nb_id or not _ID_RE.match(nb_id):
        raise NlmError("NLM_UNAVAILABLE", "create notebook: no notebook id in the CLI output", retryable=True)
    return nb_id


def nlm_source_add(cfg, notebook_id, pdf):
    name = os.path.splitext(os.path.basename(pdf))[0]
    rc, obj = run_notebooklm(cfg, ["source", "add", pdf, "--title", name, "-n", notebook_id, "--json"],
                             "source add %s" % os.path.basename(pdf))
    raise_for_cli_error(rc, obj, "source add", invalid_code="NLM_SOURCE_REJECTED")
    return _str_field(obj, "source", "id")


def nlm_generate_audio(cfg, notebook_id, prompt):
    rc, obj = run_notebooklm(cfg, ["generate", "audio", prompt, "-n", notebook_id, "--no-wait", "--json"],
                             "generate audio")
    raise_for_cli_error(rc, obj, "generate audio")
    task_id = _str_field(obj, "task_id")
    if not task_id or not _ID_RE.match(task_id):
        raise NlmError("NLM_UNAVAILABLE", "generate audio: no task id in the CLI output", retryable=True)
    return task_id


def nlm_poll(cfg, notebook_id, task_id):
    """-> ("pending"|"ready"|"failed", error_code)."""
    rc, obj = run_notebooklm(cfg, ["artifact", "poll", task_id, "-n", notebook_id, "--json"], "artifact poll")
    raise_for_cli_error(rc, obj, "artifact poll")
    state = obj.get("status")
    if state == "completed":
        return "ready", None
    if state == "failed":
        code = obj.get("error_code")
        return "failed", code if isinstance(code, (str, int)) else None
    return "pending", None        # pending | in_progress | not_found (just started) | unknown


def nlm_download_audio(cfg, notebook_id, final_path):
    """Download the latest audio to a temp name next to final_path, then rename (atomic)."""
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    part = final_path + ".part"
    rc, obj = run_notebooklm(cfg, ["download", "audio", part, "-n", notebook_id, "--latest", "--force", "--json"],
                             "download audio")
    raise_for_cli_error(rc, obj, "download audio")
    if not os.path.isfile(part) or os.path.getsize(part) == 0:
        raise NlmError("NLM_UNAVAILABLE", "download audio produced no file", retryable=True)
    os.replace(part, final_path)
    return final_path


# --------------------------------------------------------------------------- drive-put (SYL-95 / PR #4)


def _drive_put(cfg, local_path, remote_dir, timeout):
    """Run the drive skill's `drive-put <local_path> <remote_dir>` and return its JSON envelope."""
    script = cfg.drive_put
    if not (os.path.isfile(script) and os.access(script, os.X_OK)):
        return {"ok": False, "error": {"code": "DRIVE_NOT_INSTALLED"}}
    try:
        proc = subprocess.run([script, local_path, remote_dir], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "DRIVE_NET"}}
    except OSError:
        return {"ok": False, "error": {"code": "DRIVE_NOT_INSTALLED"}}
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    try:
        envelope = json.loads(lines[-1]) if lines else None
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict):
        return {"ok": False, "error": {"code": "DRIVE_NET"}}
    return envelope


drive_put = _drive_put          # seam for tests


# --------------------------------------------------------------------------- job record


def _job_path(cfg, course, date):
    return os.path.join(cfg.jobs_root, "%s-%s.json" % (course, date))


def load_job(cfg, course, date):
    try:
        with open(_job_path(cfg, course, date), "r", encoding="utf-8") as fh:
            job = json.load(fh)
    except (OSError, ValueError):
        return {}
    return job if isinstance(job, dict) else {}


def save_job(cfg, course, date, job):
    os.makedirs(cfg.jobs_root, exist_ok=True)
    path = _job_path(cfg, course, date)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    os.replace(path + ".tmp", path)


def find_job_by_notebook(cfg, notebook_id):
    """(course, date) of the prep job (or prep-log record) that owns notebook_id."""
    try:
        names = sorted(os.listdir(cfg.jobs_root))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(cfg.jobs_root, name), "r", encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("notebook_id") == notebook_id:
            return job.get("course"), job.get("date")
    for key, record in _prep_log_sessions(cfg).items():
        if isinstance(record, dict) and record.get("notebook_id") == notebook_id and "@" in key:
            return tuple(key.split("@", 1))
    return None, None


def _prep_log_sessions(cfg):
    try:
        with open(cfg.prep_log_path, "r", encoding="utf-8") as fh:
            log = json.load(fh)
    except (OSError, ValueError):
        return {}
    sessions = log.get("sessions") if isinstance(log, dict) else None
    return sessions if isinstance(sessions, dict) else {}


def prep_log_notebook_id(cfg, course, date):
    record = _prep_log_sessions(cfg).get("%s@%s" % (course, date))
    nb = record.get("notebook_id") if isinstance(record, dict) else None
    return nb if isinstance(nb, str) and nb else None


# --------------------------------------------------------------------------- commands


def cmd_prep(cfg, args):
    course, date = check_course(args.course), check_date(args.date)
    prompt = build_prompt(args.topic)
    pdfs = check_pdfs(cfg, args.pdfs)

    # Quota guard: never a second notebook/podcast for a session that already has one.
    job = load_job(cfg, course, date)
    recorded = prep_log_notebook_id(cfg, course, date)
    if recorded or job.get("task_id"):
        cfg.log("prep: %s@%s already has a notebook; not starting another" % (course, date))
        notebook_id = recorded or job.get("notebook_id")
        task_id = job.get("task_id") if job.get("notebook_id") == notebook_id else None
        return {"notebook_id": notebook_id, "task_id": task_id, "skipped": True}

    secure_auth_home(cfg)
    auth_check(cfg)

    job.update({"course": course, "date": date})
    if not job.get("notebook_id"):
        job["notebook_id"] = nlm_create(cfg, notebook_title(course, date))
        job["sources"] = []
        save_job(cfg, course, date, job)       # a timeout from here on resumes, never re-creates
    notebook_id = job["notebook_id"]

    rejected = []
    for pdf in pdfs:
        if pdf in job.get("sources", []):
            continue
        try:
            nlm_source_add(cfg, notebook_id, pdf)
        except NlmError as e:
            if e.code != "NLM_SOURCE_REJECTED":
                raise
            rejected.append(os.path.basename(pdf))
            continue
        job.setdefault("sources", []).append(pdf)
        save_job(cfg, course, date, job)
    if not job.get("sources"):
        raise NlmError("NLM_SOURCE_REJECTED", "NotebookLM rejected every source; nothing to make a podcast from")

    job["task_id"] = nlm_generate_audio(cfg, notebook_id, prompt)
    save_job(cfg, course, date, job)
    result = {"notebook_id": notebook_id, "task_id": job["task_id"]}
    if rejected:
        result["sources_rejected"] = rejected
    return result


def cmd_status(cfg, args):
    notebook_id = check_id(args.notebook_id, "notebook_id")
    task_id = check_id(args.task_id, "task_id")
    if bool(args.course) != bool(args.date):
        raise NlmError("USAGE", "pass both --course and --date, or neither")
    course, date = (args.course, args.date) if args.course else find_job_by_notebook(cfg, notebook_id)
    if not (course and date):
        raise NlmError("USAGE", "unknown notebook %s: pass --course and --date" % notebook_id)
    course, date = check_course(course), check_date(date)
    local = podcast_path(cfg, course, date)

    if not (os.path.isfile(local) and os.path.getsize(local) > 0):
        secure_auth_home(cfg)
        state, error_code = nlm_poll(cfg, notebook_id, task_id)
        if state == "pending":
            return {"status": "pending", "local_path": None, "drive_url": None}
        if state == "failed":
            out = {"status": "failed", "local_path": None, "drive_url": None}
            if error_code is not None:
                out["error_code"] = error_code
            return out
        nlm_download_audio(cfg, notebook_id, local)

    # Upload. If the budget is nearly spent, keep the mp3 and upload on the next poll.
    budget = cfg.remaining()
    if budget < DRIVE_MIN_BUDGET:
        return {"status": "pending", "local_path": local, "drive_url": None, "drive_error": "DRIVE_DEFERRED"}
    cfg.log("drive-put %s %s" % (os.path.basename(local), cfg.drive_dir))
    envelope = drive_put(cfg, local, cfg.drive_dir, budget - 2)
    url = envelope.get("web_url") if envelope.get("ok") else None
    if not isinstance(url, str) or not url:
        err = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        code = err.get("code") if isinstance(err.get("code"), str) else "DRIVE_NO_LINK"
        return {"status": "pending", "local_path": local, "drive_url": None, "drive_error": code}
    return {"status": "ready", "local_path": local, "drive_url": url}


def cmd_check(cfg, args):
    secure_auth_home(cfg)
    auth_check(cfg)
    return {"status": "ok", "notebooklm_home": cfg.nlm_home}


COMMANDS = {"prep": cmd_prep, "status": cmd_status, "check": cmd_check}
IMPLIED = {"nlm-prep": "prep", "nlm-status": "status", "nlm-check": "check"}


# --------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise NlmError("USAGE", message)


def build_parser(prog="nlm"):
    p = _Parser(prog=prog, description="NotebookLM podcasts for class-prep-agent. Prints one JSON object.")
    p.add_argument("-v", "--verbose", action="store_true", help="log '<op>' lines to stderr")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("prep", help="create the notebook, add the PDFs, start the audio; returns at once")
    s.add_argument("course")
    s.add_argument("date", metavar="YYYY-MM-DD")
    s.add_argument("pdfs", nargs="+", metavar="pdf", help="files under $DATA_DIR/readings/")
    s.add_argument("--topic", required=True, help="the session topic (goes into the podcast prompt as data)")

    s = sub.add_parser("status", help="poll the audio task; when ready download it and drive-put it")
    s.add_argument("notebook_id")
    s.add_argument("task_id")
    s.add_argument("--course")
    s.add_argument("--date", metavar="YYYY-MM-DD")

    sub.add_parser("check", help="auth smoke test: notebooklm auth check --test")
    return p


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


def _emit(out, obj):
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def main(argv=None, env=None, out=None, prog=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    prog = os.path.basename(sys.argv[0] or "nlm") if prog is None else prog
    try:
        args = build_parser(prog if prog in IMPLIED else "nlm").parse_args(implied_argv(prog, argv))
        if not args.command:
            raise NlmError("USAGE", "missing command: one of %s" % ", ".join(COMMANDS))
        cfg = Config(env, verbose=args.verbose)
        _emit(out, COMMANDS[args.command](cfg, args))
        return 0
    except NlmError as e:
        _emit(out, e.to_json())
        return 2
    except Exception as e:      # a bug in this wrapper: exit 1, one JSON object, no payloads
        _emit(out, {"error": "INTERNAL", "message": type(e).__name__, "retryable": False})
        return 1


if __name__ == "__main__":
    sys.exit(main())
