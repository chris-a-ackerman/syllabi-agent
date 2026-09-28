#!/usr/bin/env python3
"""drive: Google Drive uploads for class-prep-agent (SYL-95), via rclone.

    drive.py put <local_path> <remote_dir>     (or: drive-put <local_path> <remote_dir>)
    drive.py check                             (auth smoke test: list the Drive root, read the quota)

Prints exactly one JSON object on stdout (docs/tool-contract.md, "Common envelope"):

    {"ok": true, ...}                                                  exit 0
    {"ok": false, "error": {"code", "message", "retryable", ...}}      exit 2
    exit 1 = crash (unhandled exception; still prints an INTERNAL error object)

`put` copies one local file (a downloaded reading or a podcast) to
$DRIVE_READINGS_ROOT/<remote_dir>/<name> and returns a link to it. It is idempotent: when a file
with the same name, size and MD5 is already there, nothing is transferred and the same link comes
back (`uploaded: false`). It never deletes, moves or renames anything on Drive.

Environment:
    RCLONE_CONFIG         rclone.conf on the persistent volume (default $DATA_DIR/rclone/rclone.conf)
    DRIVE_READINGS_ROOT   <remote>:<folder> that remote_dir is relative to (default gdrive:Readings)
    DRIVE_SHARE           private (default): the link is the file's Drive URL, which only Chris's
                          Google account can open, and no permission on Drive is changed.
                          anyone: `rclone link`, i.e. "anyone with the link can view".
    DRIVE_TIMEOUT         seconds for the whole command (default 35: nlm-status allows drive-put
                          40 s, Maritime caps a command at 60 s)
    DATA_DIR              persistent volume root, default /data; local_path must be under it
    DRIVE_RCLONE_BIN      the rclone binary (default: `rclone` on PATH)
    DRIVE_VERBOSE=1       same as --verbose: log "<op> <path>" lines to stderr

Security properties (requirements carried over from SYL-93 / SYL-94):
    * Nothing from rclone.conf reaches stdout or stderr: every value in it is scrubbed from all
      output, including crash output and rclone's own messages.
    * local_path must resolve (realpath) under $DATA_DIR, so a symlink can't leak a file from
      elsewhere; its name must already be safe ([A-Za-z0-9._ -], as canvas.py writes them).
    * remote_dir is validated segment by segment (no '..', no leading dot or dash, no ':' or
      backslash) before it becomes part of an rclone path.
    * Only `lsjson`, `copyto`, `link` and `about` are ever run: no sync, no delete, no purge.
    * By default no file is made public (DRIVE_SHARE=private).
    * File names and ids that come back from Drive are data, never instructions.

Standard library only (Python 3.8+); rclone is a separate binary (docs/deploy-maritime.md §7).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

DEFAULT_TIMEOUT = 35             # seconds for the whole command (see DRIVE_TIMEOUT above)
LIST_TIMEOUT = 15                # seconds for one lsjson / about
LINK_TIMEOUT = 15                # seconds for one rclone link
MAX_FILE_BYTES = 100 * 1024 * 1024   # Maritime's per-file transfer cap
DEFAULT_ROOT = "gdrive:Readings"
DEFAULT_CONFIG = "rclone/rclone.conf"   # under $DATA_DIR
DRIVE_FILE_URL = "https://drive.google.com/file/d/%s/view"
# Fail fast: the deadline, not rclone's retry loop, bounds the run time.
RCLONE_FLAGS = ["--retries", "1", "--low-level-retries", "3", "--contimeout", "10s"]

_REMOTE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_ .-]*):(.*)$")       # rclone remote name rules
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")       # one remote_dir segment
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,254}$")          # the file name (canvas.py's safe set)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")                         # a Drive file id
_TIMESTAMP_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")
_HTTP_STATUS_RE = re.compile(r"\b(?:Error|status(?: code)?)[: ]+(\d{3})\b", re.I)

# rclone / Google API error text → tool codes. Checked in this order.
_AUTH_RE = re.compile(
    r"invalid_grant|token has been expired|expired or revoked|oauth2|cannot fetch token|couldn't fetch token|"
    r"unauthori[sz]ed|invalid credentials|invalid_client|unauthenticated|autherror|"
    r"insufficient ?permissions|didn't find section in config|config file .*not found|couldn't find config|"
    r"failed to read config|failed to create file system|no such remote|empty token found|token expired|"
    r"\b401\b", re.I)
_QUOTA_RE = re.compile(
    r"storageQuotaExceeded|quotaExceeded|quota exceeded|teamDriveFileLimitExceeded|userRateLimitExceeded|"
    r"rateLimitExceeded|dailyLimitExceeded|too many requests|\b429\b|insufficient storage|storage quota", re.I)
_NETWORK_RE = re.compile(
    r"dial tcp|no such host|connection (?:refused|reset)|timed? ?out|i/o timeout|tls handshake|unexpected eof|"
    r"network is unreachable|temporary failure|\b5\d\d\b|internal error|backend error|service unavailable|"
    r"context deadline|broken pipe", re.I)
_NOT_FOUND_RE = re.compile(r"(?:directory|file|object) not found", re.I)


# --------------------------------------------------------------------------- errors


class DriveError(Exception):
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


def _short(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------- config


class Config:
    def __init__(self, env, verbose=False):
        self.data_dir = os.path.realpath(env.get("DATA_DIR") or "/data")
        self.config_path = env.get("RCLONE_CONFIG") or os.path.join(self.data_dir, DEFAULT_CONFIG)
        root = (env.get("DRIVE_READINGS_ROOT") or DEFAULT_ROOT).strip()
        m = _REMOTE_RE.match(root)
        if not m:
            raise DriveError("USAGE", "DRIVE_READINGS_ROOT must be <remote>:<folder>, e.g. gdrive:Readings",
                             detail={"root": root})
        self.remote = m.group(1)
        self.root_path = m.group(2).strip().strip("/")
        for segment in self.root_path.split("/") if self.root_path else []:
            if not _SEGMENT_RE.match(segment):
                raise DriveError("USAGE", "DRIVE_READINGS_ROOT has an unsafe folder segment", detail={"segment": segment})
        self.share = (env.get("DRIVE_SHARE") or "private").strip().lower()
        if self.share not in ("private", "anyone"):
            raise DriveError("USAGE", "DRIVE_SHARE must be 'private' (default) or 'anyone'")
        try:
            self.timeout = float(env.get("DRIVE_TIMEOUT") or DEFAULT_TIMEOUT)
        except ValueError:
            raise DriveError("USAGE", "DRIVE_TIMEOUT must be a number of seconds")
        if self.timeout <= 0:
            raise DriveError("USAGE", "DRIVE_TIMEOUT must be positive")
        self.rclone = env.get("DRIVE_RCLONE_BIN") or "rclone"
        self.verbose = verbose or env.get("DRIVE_VERBOSE") == "1"
        self.started = time.monotonic()

    @property
    def root_display(self):
        return "%s:%s" % (self.remote, self.root_path)

    def remaining(self):
        return self.timeout - (time.monotonic() - self.started)

    def remote_path(self, *parts):
        """'<remote>:<root>/<parts...>', the form rclone takes."""
        return "%s:%s" % (self.remote, self.display_path(*parts))

    def display_path(self, *parts):
        """'<root>/<parts...>' without the remote name: what goes in the prep-log and the brief."""
        return "/".join(p for p in (self.root_path,) + tuple(parts) if p)

    def log(self, message):
        if self.verbose:
            sys.stderr.write("drive: %s\n" % message)


# --------------------------------------------------------------------------- secrets


def load_config_secrets(path):
    """Every value in rclone.conf (OAuth tokens, client secrets, folder ids), so output can be
    scrubbed. Best effort: a missing or unreadable file yields nothing (and DRIVE_AUTH later)."""
    values = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line[0] in "#;[" or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 8:
            values.add(value)
        if value.startswith("{"):           # token = {"access_token": "...", "refresh_token": "..."}
            try:
                data = json.loads(value)
            except ValueError:
                continue
            for item in (data.values() if isinstance(data, dict) else []):
                if isinstance(item, str) and len(item) >= 8:
                    values.add(item)
    return values


# --------------------------------------------------------------------------- argument checks


def _under(root, path):
    real = os.path.realpath(path)
    root = os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def check_local_path(cfg, path):
    """local_path must be an existing, non-empty regular file under $DATA_DIR (realpath, so
    symlinks can't escape), with a safe name and within the transfer cap. Returns (real, name, size)."""
    if not _under(cfg.data_dir, path):
        raise DriveError("USAGE", "local_path must be under %s/" % cfg.data_dir, detail={"local_path": path})
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise DriveError("LOCAL_NOT_FOUND", "no such file: %s" % path, detail={"local_path": path})
    name = os.path.basename(real)
    if not _NAME_RE.match(name):
        raise DriveError("BAD_FILENAME", "file name must use [A-Za-z0-9._ -] and not start with a dot; rename it first",
                         detail={"name": name})
    size = os.path.getsize(real)
    if size == 0:
        raise DriveError("USAGE", "file is empty: %s" % path, detail={"local_path": path})
    if size > MAX_FILE_BYTES:
        raise DriveError("FILE_TOO_LARGE", "file is %d bytes; the transfer cap is %d" % (size, MAX_FILE_BYTES),
                         detail={"size": size, "limit": MAX_FILE_BYTES, "local_path": real})
    return real, name, size


def check_remote_dir(cfg, remote_dir):
    """remote_dir is '<course>/<date>' relative to the root. 'Readings/<course>/<date>' (the root
    folder spelled out, as AGENTS.md's Drive layout does) is accepted and means the same folder."""
    raw = (remote_dir or "").strip().strip("/")
    if cfg.root_path and (raw == cfg.root_path or raw.startswith(cfg.root_path + "/")):
        raw = raw[len(cfg.root_path):].strip("/")
    if not raw:
        raise DriveError("USAGE", "remote_dir must be like <course>/<date>, relative to %s" % cfg.root_display,
                         detail={"remote_dir": remote_dir})
    if ":" in raw or "\\" in raw:
        raise DriveError("USAGE", "remote_dir must not contain ':' or '\\'", detail={"remote_dir": remote_dir})
    for segment in raw.split("/"):
        if not _SEGMENT_RE.match(segment):
            raise DriveError("USAGE", "remote_dir has an unsafe segment (use letters, digits, . _ - and spaces; "
                             "no leading dot or dash)", detail={"segment": segment})
    return raw


def _md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- rclone
# `run_rclone` is the seam the tests replace. Nothing else starts a process.


def _run_rclone(cfg, args, timeout):
    """One rclone invocation. Returns (returncode, stdout, stderr)."""
    cmd = [cfg.rclone, "--config", cfg.config_path] + RCLONE_FLAGS + list(args)
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DriveError("DRIVE_NOT_INSTALLED", "rclone is not installed (looked for %r); see docs/deploy-maritime.md §7"
                         % cfg.rclone)
    except subprocess.TimeoutExpired:
        raise DriveError("DRIVE_UNAVAILABLE", "rclone %s did not finish within %.0f s; for a big file run drive-put in "
                         "the background with DRIVE_TIMEOUT=300" % (args[0], timeout), retryable=True)
    except OSError as e:
        raise DriveError("DRIVE_UNAVAILABLE", "could not run rclone: %s" % _short(e), retryable=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


run_rclone = _run_rclone


def rclone(cfg, args, cap, what):
    """Run one rclone subcommand inside what is left of the command deadline."""
    remaining = cfg.remaining()
    if remaining < 1:
        raise DriveError("DRIVE_UNAVAILABLE", "out of time before %s (DRIVE_TIMEOUT=%.0f s)" % (what, cfg.timeout),
                         retryable=True)
    cfg.log(what)
    return run_rclone(cfg, list(args), min(cap, remaining))


def _stderr_summary(err):
    """The line of rclone's stderr that names the failure, without its timestamp, shortened."""
    lines = [line.strip() for line in (err or "").splitlines() if line.strip()]
    if not lines:
        return ""
    chosen = lines[-1]
    for line in reversed(lines):
        if "Failed" in line or "ERROR" in line or "error" in line.lower():
            chosen = line
            break
    return _short(_TIMESTAMP_RE.sub("", chosen))


def is_not_found(rc, err):
    return rc in (3, 4) or bool(_NOT_FOUND_RE.search(err or ""))


def classify(cfg, rc, err, what):
    """Map a failed rclone run to a DriveError (docs/tool-contract.md §4)."""
    summary = _stderr_summary(err)
    m = _HTTP_STATUS_RE.search(err or "")
    status = int(m.group(1)) if m else None
    # Network first: a token refresh that times out mentions oauth2 but is not an auth failure.
    if rc == 5 or _NETWORK_RE.search(err or ""):
        return DriveError("DRIVE_UNAVAILABLE", "%s: network or Drive error (%s)" % (what, summary or "rclone exit %d" % rc),
                          retryable=True, status=status)
    if _AUTH_RE.search(err or ""):
        return DriveError("DRIVE_AUTH", "%s: rclone could not use the Drive remote (%s). Reconnect it on your laptop "
                          "(`rclone config reconnect %s:`) and upload the new rclone.conf to %s"
                          % (what, summary or "no detail", cfg.remote, cfg.config_path), retryable=False, status=status)
    if _QUOTA_RE.search(err or ""):
        return DriveError("DRIVE_QUOTA", "%s: Drive quota or rate limit hit (%s); try again on the next run"
                          % (what, summary), retryable=True, status=status)
    # 1 = usage (our bug or an unknown flag), 7 = fatal (retries won't help); anything else may pass next time.
    return DriveError("DRIVE_UNAVAILABLE", "%s: rclone exit %d (%s)" % (what, rc, summary or "no output"),
                      retryable=rc not in (1, 7), status=status)


# --------------------------------------------------------------------------- Drive listings


def list_remote_dir(cfg, remote_dir):
    """Files in <root>/<remote_dir> as rclone lsjson dicts; [] when the folder doesn't exist yet."""
    display = cfg.display_path(remote_dir)
    rc, out, err = rclone(cfg, ["lsjson", "--files-only", "--hash", "--no-modtime", cfg.remote_path(remote_dir)],
                          LIST_TIMEOUT, "lsjson %s" % display)
    if rc != 0:
        if is_not_found(rc, err):
            return []
        raise classify(cfg, rc, err, "listing %s" % display)
    try:
        entries = json.loads(out or "[]")
    except ValueError:
        raise DriveError("DRIVE_UNAVAILABLE", "rclone lsjson printed something that is not JSON for %s" % display,
                         retryable=True)
    if not isinstance(entries, list):
        raise DriveError("DRIVE_UNAVAILABLE", "rclone lsjson printed no list for %s" % display, retryable=True)
    return [e for e in entries if isinstance(e, dict) and not e.get("IsDir")]


def _md5_of(entry):
    hashes = entry.get("Hashes")
    if isinstance(hashes, dict):
        for key, value in hashes.items():
            if str(key).lower() == "md5" and isinstance(value, str) and value:
                return value.lower()
    return None


def _file_id(entry):
    """The Drive file id from an lsjson entry, or None. A shortcut lists '<target id>\\t<shortcut id>'."""
    fid = entry.get("ID") if isinstance(entry, dict) else None
    if not isinstance(fid, str):
        return None
    fid = fid.split("\t")[0].strip()
    return fid if _ID_RE.match(fid) else None


def find_remote_file(entries, name, md5):
    """The entry called `name`; with Drive duplicates, the one whose MD5 matches wins."""
    matches = [e for e in entries if e.get("Name") == name]
    for entry in matches:
        if _md5_of(entry) == md5:
            return entry
    return matches[0] if matches else None


def same_file(entry, size, md5):
    """Same size and (when Drive reports one) the same MD5: nothing to upload."""
    if entry.get("Size") != size:
        return False
    remote_md5 = _md5_of(entry)
    return remote_md5 is None or remote_md5 == md5


def share_link(cfg, entry, remote_dir, name):
    """(link, mode). private: the file's Drive URL, no permission change. anyone: rclone link."""
    display = cfg.display_path(remote_dir, name)
    if cfg.share == "private":
        fid = _file_id(entry)
        if not fid:
            raise DriveError("USAGE", "Drive returned no file id for %s (not a Google Drive remote?); set "
                             "DRIVE_SHARE=anyone to use `rclone link` instead" % display)
        return DRIVE_FILE_URL % fid, "private"
    rc, out, err = rclone(cfg, ["link", cfg.remote_path(remote_dir, name)], LINK_TIMEOUT, "link %s" % display)
    if rc != 0:
        raise classify(cfg, rc, err, "creating a share link for %s" % display)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    link = lines[-1] if lines else ""
    if not link.startswith(("https://", "http://")):
        raise DriveError("DRIVE_UNAVAILABLE", "rclone link printed no URL for %s" % display, retryable=True)
    return link, "anyone"


# --------------------------------------------------------------------------- commands


def cmd_put(cfg, args):
    local, name, size = check_local_path(cfg, args.local_path)
    remote_dir = check_remote_dir(cfg, args.remote_dir)
    md5 = _md5_file(local)
    display = cfg.display_path(remote_dir, name)

    existing = find_remote_file(list_remote_dir(cfg, remote_dir), name, md5)
    uploaded = False
    if existing is not None and same_file(existing, size, md5):
        cfg.log("already there: %s" % display)
    else:
        # --ignore-times: we have already decided it differs; never let a size+mtime match skip it.
        rc, _, err = rclone(cfg, ["copyto", "--ignore-times", local, cfg.remote_path(remote_dir, name)],
                            cfg.remaining(), "copyto %s" % display)
        if rc != 0:
            raise classify(cfg, rc, err, "uploading %s" % display)
        uploaded = True
        existing = find_remote_file(list_remote_dir(cfg, remote_dir), name, md5)
        if existing is None:
            raise DriveError("DRIVE_UNAVAILABLE", "upload of %s reported success but the file is not listed" % display,
                             retryable=True)
        if existing.get("Size") != size:
            raise DriveError("DRIVE_UNAVAILABLE", "size mismatch after uploading %s (%s bytes on Drive, %d local)"
                             % (display, existing.get("Size"), size), retryable=True)
        remote_md5 = _md5_of(existing)
        if remote_md5 and remote_md5 != md5:
            raise DriveError("DRIVE_UNAVAILABLE", "hash mismatch after uploading %s" % display, retryable=True)

    link, mode = share_link(cfg, existing, remote_dir, name)
    return {"ok": True, "remote_path": display, "share_link": link, "uploaded": uploaded, "bytes": size,
            "md5": md5, "file_id": _file_id(existing), "share": mode, "local_path": local}


def cmd_check(cfg, args):
    rc, out, err = rclone(cfg, ["lsjson", "--max-depth", "1", "--no-modtime", cfg.remote_path()],
                          LIST_TIMEOUT, "lsjson %s" % cfg.root_display)
    root_exists, entries = True, None
    if rc == 0:
        try:
            listed = json.loads(out or "[]")
        except ValueError:
            raise DriveError("DRIVE_UNAVAILABLE", "rclone lsjson printed something that is not JSON", retryable=True)
        entries = len(listed) if isinstance(listed, list) else None
    elif is_not_found(rc, err):
        root_exists = False          # the first upload creates it
    else:
        raise classify(cfg, rc, err, "listing %s" % cfg.root_display)
    result = {"ok": True, "root": cfg.root_display, "root_exists": root_exists, "entries": entries,
              "config_path": cfg.config_path, "share": cfg.share}
    rc, out, err = rclone(cfg, ["about", "--json", cfg.remote + ":"], LIST_TIMEOUT, "about %s:" % cfg.remote)
    if rc == 0:                      # quota is a nicety: a backend without `about` still passes the check
        try:
            about = json.loads(out or "{}")
        except ValueError:
            about = {}
        for key in ("used", "free", "total"):
            if isinstance(about, dict) and isinstance(about.get(key), int):
                result[key + "_bytes"] = about[key]
    return result


COMMANDS = {"put": cmd_put, "check": cmd_check}
# When invoked through the drive-put symlink the subcommand is implied by argv[0].
IMPLIED = {"drive-put": "put", "drive-check": "check"}


# --------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DriveError("USAGE", message)


def build_parser(prog="drive"):
    p = _Parser(prog=prog, description="Google Drive uploads for class-prep-agent, via rclone. Prints one JSON object.")
    p.add_argument("-v", "--verbose", action="store_true", help="log '<op> <path>' lines to stderr (never the config)")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("put", help="upload one file under $DATA_DIR to <root>/<remote_dir>/ and return its link")
    s.add_argument("local_path")
    s.add_argument("remote_dir", help="e.g. MAS.665/2026-09-29 (relative to $DRIVE_READINGS_ROOT)")

    sub.add_parser("check", help="auth smoke test: list the Drive root and read the quota")
    return p


def _emit(out, obj, secrets=()):
    text = json.dumps(obj, ensure_ascii=False)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    out.write(text + "\n")
    out.flush()


class _Scrubbed:
    """stderr wrapper that redacts rclone.conf values from anything logged (ours or rclone's)."""

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


def implied_argv(prog, argv):
    """The drive-put symlink implies the subcommand. Leading -v/--verbose stay in front."""
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
    prog = os.path.basename(sys.argv[0] or "drive") if prog is None else prog
    argv = implied_argv(prog, argv)
    config_path = env.get("RCLONE_CONFIG") or os.path.join(env.get("DATA_DIR") or "/data", DEFAULT_CONFIG)
    secrets = sorted(load_config_secrets(config_path), key=len, reverse=True)
    real_stderr = sys.stderr
    sys.stderr = _Scrubbed(real_stderr, secrets)
    try:
        args = build_parser(prog if prog in IMPLIED else "drive").parse_args(argv)
        if not args.command:
            raise DriveError("USAGE", "missing command: one of %s" % ", ".join(COMMANDS))
        cfg = Config(env, verbose=args.verbose)
        if not os.path.isfile(cfg.config_path):
            raise DriveError("DRIVE_AUTH", "no rclone config at %s: run `rclone config` on your laptop (Google Drive "
                             "remote, OAuth) and upload the resulting rclone.conf there" % cfg.config_path)
        result = COMMANDS[args.command](cfg, args)
        _emit(out, result, secrets)
        return 0
    except DriveError as e:
        _emit(out, e.to_json(), secrets)
        return 2
    except Exception as e:      # a crash: exit 1, still one JSON object, never a token
        _emit(out, {"ok": False, "error": {"code": "INTERNAL", "retryable": False,
                                           "message": "%s: %s" % (type(e).__name__, _short(e))}}, secrets)
        return 1
    finally:
        sys.stderr = real_stderr


if __name__ == "__main__":
    sys.exit(main())
