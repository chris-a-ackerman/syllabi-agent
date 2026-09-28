#!/usr/bin/env python3
"""syllabi: read-only client for the syllabi app's agent endpoint (SYL-96, V5).

    syllabi.py upcoming [--days N] [--within-hours H] [--now ISO]
    syllabi.py course   <course_id_or_code>
    syllabi.py check

Prints exactly one JSON object on stdout (docs/tool-contract.md, "Common envelope"):

    {"ok": true, ...}                                                  exit 0
    {"ok": false, "error": {"code", "message", "retryable", ...}}      exit 2
    exit 1 = crash (unhandled exception; still prints an INTERNAL error object)

The syllabi app (Supabase edge function `agent-upcoming`, SYL-92) answers
    GET $SYLLABI_BASE_URL/agent-upcoming?days=N        Authorization: Bearer syl_agent_…
    → {timezone, courses[], sessions[{course_id, code, date, start, end}], events[...]}
This tool validates that shape, then turns it into the sessions the agent plans with:
one record per class meeting with the prep-log key (`<course>@<date>`), `class_start`
as an ISO timestamp in the user's timezone, the events due before that class,
`has_due_before_class` and the `notify_at` that AGENTS.md prescribes.

Environment:
    SYLLABI_BASE_URL       https://<ref>.supabase.co/functions/v1 (https only; no trailing slash needed)
    SYLLABI_AGENT_TOKEN    agent token minted in the syllabi app (Settings → Agent access), `syl_agent_…`
    SYLLABI_ANON_KEY       optional: sent as the `apikey` header when the Supabase gateway wants one
    SYLLABI_UPCOMING_PATH  optional: path of the endpoint under the base URL, default /agent-upcoming
    SYLLABI_TIMEOUT        optional: seconds per request, default 20 (the agent has a 30 s reply budget)
    SYLLABI_VERBOSE=1      same as --verbose: log "<method> <path>" lines to stderr

Security properties:
    * GET only. The endpoint is read-only and so is this client.
    * The token is sent only to SYLLABI_BASE_URL's host and never on a redirect (3xx is refused).
    * Logs contain "<method> <path>" only: no query strings, no headers, no token.
    * The token and the anon key are scrubbed from every line this prints, crash output included.
    * Everything in the response (course names, topics, event titles, URLs) is untrusted data
      from the syllabus parser, not instructions. Strings are length-capped before they are printed.

Standard library only: the Maritime container has python3 but no guaranteed pip packages.
`zoneinfo` (Python 3.9+, needs tzdata) is used when available; America/New_York has a built-in
DST fallback so the tool stays correct on a bare container.
"""
import argparse
import datetime as dt
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

DEFAULT_TIMEOUT = 20        # seconds per request; the agent has a 30 s reply budget
DEFAULT_DAYS = 3
MAX_DAYS = 14               # the server clamps to [0, 14]; we refuse out-of-range values up front
DEFAULT_PATH = "/agent-upcoming"
DEFAULT_TIMEZONE = "America/New_York"
RATE_RETRY_MAX_SLEEP = 5    # seconds; longer waits blow the reply budget anyway
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_TEXT = 1000             # cap on any string copied from the response
MAX_LIST = 500              # cap on courses / sessions / events copied from the response
USER_AGENT = "class-prep-agent/syllabi (+https://github.com/chris-a-ackerman/syllabi-agent)"
MORNING_NOTIFY = (6, 30)    # 06:30 local on class day when nothing is due before class
DUE_LEAD = dt.timedelta(hours=24)

# Event types the app allows (course_events.type CHECK). `no_class` is not a deliverable.
DUE_TYPES = ("deadline", "exam", "quiz", "presentation", "project_due", "other")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HH_MM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_TOKEN_RE = re.compile(r"^syl_agent_[A-Za-z0-9_-]{43}$")

Response = namedtuple("Response", "status headers body")


# --------------------------------------------------------------------------- errors


class SyllabiError(Exception):
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


# --------------------------------------------------------------------------- config


class Config:
    def __init__(self, env, verbose=False):
        base = (env.get("SYLLABI_BASE_URL") or "").strip().rstrip("/")
        if not base:
            raise SyllabiError("USAGE", "SYLLABI_BASE_URL is not set (e.g. https://<ref>.supabase.co/functions/v1)")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SyllabiError("USAGE", "SYLLABI_BASE_URL must be an https:// URL (the token is sent in a header)")
        self.base = base
        self.host = parsed.netloc.lower()
        self.token = (env.get("SYLLABI_AGENT_TOKEN") or "").strip()
        self.anon_key = (env.get("SYLLABI_ANON_KEY") or "").strip()
        path = (env.get("SYLLABI_UPCOMING_PATH") or DEFAULT_PATH).strip()
        if not path.startswith("/"):
            path = "/" + path
        if "?" in path or "#" in path or "//" in path:
            raise SyllabiError("USAGE", "SYLLABI_UPCOMING_PATH must be a plain path like /agent-upcoming")
        self.path = path
        try:
            self.timeout = float(env.get("SYLLABI_TIMEOUT") or DEFAULT_TIMEOUT)
        except ValueError:
            raise SyllabiError("USAGE", "SYLLABI_TIMEOUT must be a number of seconds")
        if not 0 < self.timeout <= 55:
            raise SyllabiError("USAGE", "SYLLABI_TIMEOUT must be between 1 and 55 seconds (Maritime's exec cap is 60)")
        self.verbose = verbose or env.get("SYLLABI_VERBOSE") == "1"

    def secrets(self):
        return [s for s in (self.token, self.anon_key) if s]

    def log(self, message):
        if self.verbose:
            sys.stderr.write("syllabi: %s\n" % message)

    def log_request(self, method, url):
        parsed = urllib.parse.urlparse(url)
        where = parsed.path
        if parsed.netloc.lower() != self.host:
            where = "%s://%s%s" % (parsed.scheme, parsed.netloc, parsed.path)
        self.log("%s %s" % (method, where))      # never the query string, never headers


# --------------------------------------------------------------------------- transport
# These module-level hooks are the seams the tests replace. Nothing else does I/O.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib forwards the Authorization header across redirects; a 3xx is refused instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _read_limited(fp, max_bytes):
    chunks = []
    total = 0
    while True:
        chunk = fp.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SyllabiError("SYLLABI_BAD_RESPONSE", "response body exceeds %d bytes" % max_bytes,
                               detail={"limit": max_bytes})
        chunks.append(chunk)
    return b"".join(chunks)


def _urllib_transport(method, url, headers, timeout):
    """One HTTP request, no redirects followed. Returns Response; raises SyllabiError on network failure."""
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()},
                            _read_limited(resp, MAX_BODY_BYTES))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, body)
    except (urllib.error.URLError, http.client.HTTPException, socket.timeout, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise SyllabiError("SYLLABI_UNAVAILABLE", "network error: %s" % _short(str(reason)), retryable=True)


transport = _urllib_transport
sleep = time.sleep
now_utc = lambda: dt.datetime.now(dt.timezone.utc)   # noqa: E731  (replaced by tests)


# --------------------------------------------------------------------------- HTTP helpers


def _short(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _retry_after(resp):
    try:
        wait = float(resp.headers.get("retry-after", "1"))
    except (TypeError, ValueError):
        wait = 1.0
    return max(0.0, min(wait, RATE_RETRY_MAX_SLEEP))


def _headers(cfg):
    if not cfg.token:
        raise SyllabiError("SYLLABI_401", "no agent token: set SYLLABI_AGENT_TOKEN "
                           "(mint one in the syllabi app: Settings → Agent access)")
    if any(ch.isspace() for ch in cfg.token):
        raise SyllabiError("SYLLABI_401", "SYLLABI_AGENT_TOKEN contains whitespace; paste the token exactly")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": "Bearer " + cfg.token}
    if cfg.anon_key:
        headers["apikey"] = cfg.anon_key
    return headers


def _get(cfg, url):
    """GET a URL on our own host with the token. Handles the one rate-limit retry."""
    if urllib.parse.urlparse(url).netloc.lower() != cfg.host:
        raise SyllabiError("USAGE", "refusing to send the token to another host")
    headers = _headers(cfg)
    cfg.log_request("GET", url)
    resp = transport("GET", url, headers, cfg.timeout)
    if resp.status == 429:
        wait = _retry_after(resp)
        cfg.log("rate limited (429); retrying once after %.1fs" % wait)
        sleep(wait)
        resp = transport("GET", url, headers, cfg.timeout)
        if resp.status == 429:
            raise SyllabiError("SYLLABI_RATE_LIMIT", "syllabi rate limit hit twice; try again on the next run",
                               retryable=True, status=429)
    return resp


def _raise_for_status(cfg, resp):
    s = resp.status
    if 200 <= s < 300:
        return
    if s in (301, 302, 303, 307, 308):
        target = urllib.parse.urlparse(resp.headers.get("location") or "")
        raise SyllabiError("SYLLABI_UNAVAILABLE",
                           "the endpoint redirected (HTTP %d) to %s; not following it, the token stays on %s. "
                           "Fix SYLLABI_BASE_URL." % (s, target.netloc or "an unknown host", cfg.host),
                           retryable=False, status=s, detail={"location_host": target.netloc or None})
    if s in (401, 403):
        raise SyllabiError("SYLLABI_401",
                           "the syllabi app rejected the agent token (HTTP %d): missing, malformed, expired, "
                           "revoked or lacking the read:upcoming scope. Mint a new one in Settings → Agent "
                           "access and set SYLLABI_AGENT_TOKEN." % s, status=s)
    if s == 404:
        raise SyllabiError("SYLLABI_NOT_FOUND",
                           "nothing at %s (HTTP 404): check SYLLABI_BASE_URL and that agent-upcoming is "
                           "deployed" % cfg.path, status=404)
    if s in (400, 405, 422):
        raise SyllabiError("SYLLABI_BAD_REQUEST", "the syllabi app refused the request (HTTP %d): %s"
                           % (s, _short(_error_text(resp))), status=s)
    if s >= 500:
        raise SyllabiError("SYLLABI_UNAVAILABLE", "the syllabi app returned HTTP %d" % s, retryable=True, status=s)
    raise SyllabiError("SYLLABI_UNAVAILABLE", "unexpected HTTP %d from the syllabi app" % s,
                       retryable=False, status=s)


def _error_text(resp):
    try:
        data = json.loads((resp.body or b"").decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            return data["error"]
    except (UnicodeDecodeError, ValueError):
        pass
    return (resp.body or b"")[:200].decode("utf-8", "replace")


def _decode_json(resp):
    try:
        return json.loads((resp.body or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SyllabiError("SYLLABI_UNAVAILABLE", "the syllabi app returned a non-JSON body (outage or wrong URL?)",
                           retryable=True, status=resp.status)


def fetch_upcoming(cfg, days):
    """GET <base><path>?days=N, validated. Returns the raw (shape-checked) payload."""
    if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= MAX_DAYS:
        raise SyllabiError("USAGE", "--days must be an integer from 0 to %d" % MAX_DAYS)
    url = cfg.base + cfg.path + "?" + urllib.parse.urlencode({"days": days})
    resp = _get(cfg, url)
    _raise_for_status(cfg, resp)
    return validate_payload(_decode_json(resp))


# --------------------------------------------------------------------------- payload validation


def _bad(reason, **detail):
    return SyllabiError("SYLLABI_BAD_RESPONSE", "unexpected response shape: " + reason, retryable=False,
                        detail=detail or None)


def _text(value, limit=MAX_TEXT):
    """A string field from the response: None stays None, anything else becomes a capped string."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    value = value.replace("\r", " ").replace("\x00", "")
    return value if len(value) <= limit else value[:limit] + "…"


def _time(value):
    if value is None:
        return None
    if isinstance(value, str) and _HH_MM.match(value.strip()):
        h, m = value.strip().split(":")
        return "%02d:%s" % (int(h), m)
    return None


def _date(value, where):
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        raise _bad("%s.date is not YYYY-MM-DD" % where)
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise _bad("%s.date is not a calendar date" % where)
    return value


def _require_str(obj, key, where):
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise _bad("%s.%s missing or not a string" % (where, key))
    return _text(value, 200)


def _list(payload, key):
    value = payload.get(key)
    if not isinstance(value, list):
        raise _bad("'%s' is not a list" % key)
    return value[:MAX_LIST]


def validate_payload(data):
    """Shape-check the agent-upcoming payload and copy out only the fields we use, capped."""
    if not isinstance(data, dict):
        raise _bad("body is not a JSON object")
    if isinstance(data.get("error"), str) and "timezone" not in data:
        raise _bad("the app answered with an error", error=_short(data["error"]))
    tz = data.get("timezone")
    if not isinstance(tz, str) or not tz:
        raise _bad("'timezone' missing")
    courses = []
    for i, c in enumerate(_list(data, "courses")):
        if not isinstance(c, dict):
            raise _bad("courses[%d] is not an object" % i)
        courses.append({
            "id": _require_str(c, "id", "courses[%d]" % i),
            "code": _text(c.get("code"), 100),
            "name": _text(c.get("name"), 300),
            "canvas_course_id": _canvas_id(c.get("canvas_course_id")),
            "schedule": _bounded(c.get("schedule") if isinstance(c.get("schedule"), dict) else None),
            "grading_rules": _bounded(c.get("grading_rules") if isinstance(c.get("grading_rules"), (dict, list)) else None),
            "policies": _bounded(c.get("policies") if isinstance(c.get("policies"), (dict, list)) else None),
        })
    sessions = []
    for i, s in enumerate(_list(data, "sessions")):
        if not isinstance(s, dict):
            raise _bad("sessions[%d] is not an object" % i)
        sessions.append({
            "course_id": _require_str(s, "course_id", "sessions[%d]" % i),
            "code": _text(s.get("code"), 100),
            "date": _date(s.get("date"), "sessions[%d]" % i),
            "start": _time(s.get("start")),
            "end": _time(s.get("end")),
            "topic": _text(s.get("topic"), 500),                  # not sent today; kept for when it is
            "readings": _readings(s.get("readings")),
        })
    events = []
    for i, e in enumerate(_list(data, "events")):
        if not isinstance(e, dict):
            raise _bad("events[%d] is not an object" % i)
        events.append({
            "course_id": _require_str(e, "course_id", "events[%d]" % i),
            "code": _text(e.get("code"), 100),
            "date": _date(e.get("date"), "events[%d]" % i),
            "time": _time(e.get("time")),
            "title": _text(e.get("title"), 500) or "",
            "type": _text(e.get("type"), 50) or "other",
            "category": _text(e.get("category"), 200),
            "confidence": _text(e.get("confidence"), 50),
            "source": _text(e.get("source"), 50),
            "canvas_url": _url(e.get("canvas_url")),
        })
    return {"timezone": _text(tz, 100), "courses": courses, "sessions": sessions, "events": events}


def _canvas_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if urllib.parse.urlparse(value).scheme not in ("http", "https"):
        return None
    return _text(value, 2000)


def _readings(value):
    out = []
    if not isinstance(value, list):
        return out
    for r in value[:50]:
        if isinstance(r, str):
            out.append({"title": _text(r, 300), "url": _url(r)})
        elif isinstance(r, dict):
            out.append({"title": _text(r.get("title"), 300), "url": _url(r.get("url"))})
    return out


# --------------------------------------------------------------------------- timezones


class _USEasternTZ(dt.tzinfo):
    """America/New_York without tzdata: EST/EDT with the post-2007 US rules. DST starts at
    02:00 EST on the second Sunday of March (the clock jumps to 03:00) and ends at 02:00 EDT on
    the first Sunday of November (the clock goes back to 01:00). Same conventions as zoneinfo:
    in the November repeated hour fold=0 is EDT and fold=1 is EST; in the March gap fold=0 is
    read as EST and fold=1 as EDT. Modeled on the USTimeZone example in the datetime docs."""

    _STD = dt.timedelta(hours=-5)
    _HOUR = dt.timedelta(hours=1)

    @staticmethod
    def _nth_sunday(year, month, n):
        first = dt.date(year, month, 1)
        offset = (6 - first.weekday()) % 7        # Monday=0 … Sunday=6
        return first + dt.timedelta(days=offset + 7 * (n - 1))

    def _range(self, year):
        start = dt.datetime.combine(self._nth_sunday(year, 3, 2), dt.time(2, 0))    # wall clock, EST
        end = dt.datetime.combine(self._nth_sunday(year, 11, 1), dt.time(2, 0))     # wall clock, EDT
        return start, end

    def _is_dst(self, local):
        if local is None:
            return False
        start, end = self._range(local.year)
        naive = local.replace(tzinfo=None)
        fold = getattr(local, "fold", 0)
        if start + self._HOUR <= naive < end - self._HOUR:
            return True
        if end - self._HOUR <= naive < end:       # the repeated 01:xx hour in November
            return not fold
        if start <= naive < start + self._HOUR:   # the missing 02:xx hour in March
            return bool(fold)
        return False

    def utcoffset(self, local):
        return self._STD + self._HOUR if self._is_dst(local) else self._STD

    def dst(self, local):
        return self._HOUR if self._is_dst(local) else dt.timedelta(0)

    def tzname(self, local):
        return "EDT" if self._is_dst(local) else "EST"

    def fromutc(self, utc):
        naive = utc.replace(tzinfo=None)
        start, end = self._range(naive.year)
        std_time = naive + self._STD
        dst_time = std_time + self._HOUR
        if end <= dst_time < end + self._HOUR:                 # the repeated hour: second pass, EST
            return std_time.replace(tzinfo=self, fold=1)
        if std_time < start or dst_time >= end:                # standard time
            return std_time.replace(tzinfo=self)
        return dst_time.replace(tzinfo=self)                   # daylight time


def tzinfo_for(name):
    """(tzinfo, fallback_used). zoneinfo when available, the built-in Eastern rules for
    America/New_York otherwise, UTC as the last resort."""
    try:
        import zoneinfo
        try:
            return zoneinfo.ZoneInfo(name), False
        except Exception:
            pass
    except ImportError:
        pass
    if name == DEFAULT_TIMEZONE:
        return _USEasternTZ(), True
    return dt.timezone.utc, True


def _parse_now(value, tz):
    if not value:
        return now_utc().astimezone(tz)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise SyllabiError("USAGE", "--now must be an ISO 8601 timestamp, e.g. 2026-09-28T18:00:00-04:00")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)      # a naive --now is read in the user's timezone
    return parsed.astimezone(tz)


def _local(date_str, time_str, tz):
    d = dt.date.fromisoformat(date_str)
    if time_str:
        h, m = time_str.split(":")
        t = dt.time(int(h), int(m))
    else:
        t = dt.time(0, 0)
    return dt.datetime.combine(d, t).replace(tzinfo=tz)


def _iso(value):
    return value.isoformat(timespec="seconds") if value is not None else None


def _hours_between(later, earlier):
    return round((later - earlier).total_seconds() / 3600.0, 2)


def _due_at(event, tz, class_start=None):
    """When an event is due, as an aware datetime. A dated event with no time counts as due at
    23:59 that day; on the class day itself it counts as due at class start (we would rather
    notify early than late)."""
    if event["time"] is None and class_start is not None and event["date"] == class_start.date().isoformat():
        return class_start
    return _local(event["date"], event["time"] or "23:59", tz)


def _bounded(value, limit=20000):
    """A raw JSON sub-document from the response (schedule, grading rules, policies): passed
    through when small, replaced by a marker when it is not."""
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > limit:
        return {"truncated": True, "chars": len(text)}
    return value


# --------------------------------------------------------------------------- planning view


def build_sessions(payload, now, tz, within_hours=None):
    """Turn the validated payload into planning records, one per class meeting.
    `now` is timezone-aware. Sessions that already started are dropped."""
    courses = {c["id"]: c for c in payload["courses"]}
    events = payload["events"]
    out = []
    for s in payload["sessions"]:
        course = courses.get(s["course_id"], {})
        code = s["code"] or course.get("code") or s["course_id"]
        start = _local(s["date"], s["start"], tz)
        end = _local(s["date"], s["end"], tz) if s["end"] else None
        if end is not None and end < start:
            end = end + dt.timedelta(days=1)
        if start < now:
            continue
        hours_until = _hours_between(start, now)
        if within_hours is not None and hours_until > within_hours:
            continue
        due = []
        for e in events:
            if e["course_id"] != s["course_id"] or e["type"] == "no_class":
                continue
            when = _due_at(e, tz, class_start=start)
            if not (now <= when <= start):        # still ahead of us, and no later than class
                continue
            due.append({
                "title": e["title"],
                "type": e["type"],
                "due_at": _iso(when),
                "date": e["date"],
                "time": e["time"],
                "time_known": e["time"] is not None,
                "canvas_url": e["canvas_url"],
                "source": e["source"],
            })
        has_due = bool(due)
        if has_due:
            notify_at = start - DUE_LEAD
        else:
            notify_at = dt.datetime.combine(start.date(), dt.time(*MORNING_NOTIFY)).replace(tzinfo=tz)
        if notify_at < now:
            notify_at = now
        out.append({
            "key": "%s@%s" % (code, s["date"]),
            "course": code,
            "course_id": s["course_id"],
            "course_name": course.get("name"),
            "canvas_course_id": course.get("canvas_course_id"),
            "class_date": s["date"],
            "class_start": _iso(start),
            "class_end": _iso(end),
            "start_time_known": s["start"] is not None,
            "hours_until_class": hours_until,
            "topic": s["topic"],
            "readings": s["readings"],
            "due_before_class": due,
            "has_due_before_class": has_due,
            "notify_at": _iso(notify_at),
        })
    return out


# --------------------------------------------------------------------------- commands


def _check_now(args):
    """Fail on a malformed --now before any network call is made."""
    if getattr(args, "now", None):
        _parse_now(args.now, dt.timezone.utc)


def _clock(cfg, payload, args):
    tz, fallback = tzinfo_for(payload["timezone"])
    now = _parse_now(getattr(args, "now", None), tz)
    if fallback:
        cfg.log("no tzdata for %s; using the built-in fallback" % payload["timezone"])
    return tz, now, fallback


def cmd_upcoming(cfg, args):
    if args.within_hours is not None and args.within_hours < 0:
        raise SyllabiError("USAGE", "--within-hours must be zero or more")
    _check_now(args)
    payload = fetch_upcoming(cfg, args.days)
    tz, now, fallback = _clock(cfg, payload, args)
    sessions = build_sessions(payload, now, tz, args.within_hours)
    result = {
        "ok": True,
        "timezone": payload["timezone"],
        "now": _iso(now),
        "days": args.days,
        "within_hours": args.within_hours,
        "sessions": sessions,
        "events": [dict(e, due_at=_iso(_due_at(e, tz))) for e in payload["events"]],
        "courses": [{"id": c["id"], "code": c["code"], "name": c["name"], "canvas_course_id": c["canvas_course_id"]}
                    for c in payload["courses"]],
    }
    if fallback:
        result["timezone_fallback"] = True
    return result


def cmd_course(cfg, args):
    needle = args.course.strip()
    if not needle:
        raise SyllabiError("USAGE", "course id or code required")
    _check_now(args)
    payload = fetch_upcoming(cfg, MAX_DAYS)
    for c in payload["courses"]:
        if c["id"] == needle or (c["code"] or "").lower() == needle.lower():
            sessions = [s for s in payload["sessions"] if s["course_id"] == c["id"]]
            return {"ok": True, "timezone": payload["timezone"], "course": c,
                    "next_sessions": [{"date": s["date"], "start": s["start"], "end": s["end"]} for s in sessions]}
    raise SyllabiError("SYLLABI_NOT_FOUND", "no course with id or code %s in the active semester" % _short(needle),
                       status=None, detail={"known_codes": [c["code"] for c in payload["courses"]]})


def cmd_check(cfg, args):
    warnings = []
    if cfg.token and not _TOKEN_RE.match(cfg.token):
        warnings.append("SYLLABI_AGENT_TOKEN does not look like a syl_agent_ token (54 chars); the app may reject it")
    _check_now(args)
    payload = fetch_upcoming(cfg, 1)
    tz, now, fallback = _clock(cfg, payload, args)
    result = {"ok": True, "timezone": payload["timezone"], "now": _iso(now), "courses": len(payload["courses"]),
              "sessions": len(payload["sessions"]), "events": len(payload["events"]), "endpoint": cfg.path}
    if fallback:
        result["timezone_fallback"] = True
    if warnings:
        result["warnings"] = warnings
    return result


COMMANDS = {"upcoming": cmd_upcoming, "course": cmd_course, "check": cmd_check}


# --------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SyllabiError("USAGE", message)


def build_parser():
    p = _Parser(prog="syllabi", description="Read-only client for the syllabi app's agent endpoint. Prints one JSON object.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log '<method> <path>' lines to stderr (never the token or query strings)")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("upcoming", help="class sessions and dated events in the next N days, as planning records")
    s.add_argument("--days", type=int, default=DEFAULT_DAYS, help="0-14 (default %d)" % DEFAULT_DAYS)
    s.add_argument("--within-hours", type=float, default=None, dest="within_hours",
                   help="keep only sessions starting within this many hours (AGENTS.md prep uses 48)")
    s.add_argument("--now", default=None, help="ISO 8601 override of the current time (tests, eval runs)")

    s = sub.add_parser("course", help="one course from the active semester, by id or code")
    s.add_argument("course", metavar="course_id_or_code")
    s.add_argument("--now", default=None, help=argparse.SUPPRESS)

    s = sub.add_parser("check", help="auth smoke test: GET ?days=1 and report counts")
    s.add_argument("--now", default=None, help=argparse.SUPPRESS)
    return p


def _emit(out, obj, secrets=()):
    text = json.dumps(obj, ensure_ascii=False)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    out.write(text + "\n")
    out.flush()


def main(argv=None, env=None, out=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    secrets = [s for s in ((env.get("SYLLABI_AGENT_TOKEN") or "").strip(),
                           (env.get("SYLLABI_ANON_KEY") or "").strip()) if s]
    try:
        args = build_parser().parse_args(argv)
        if not args.command:
            raise SyllabiError("USAGE", "missing command: one of %s" % ", ".join(COMMANDS))
        cfg = Config(env, verbose=args.verbose)
        result = COMMANDS[args.command](cfg, args)
        _emit(out, result, secrets)
        return 0
    except SyllabiError as e:
        _emit(out, e.to_json(), secrets)
        return 2
    except Exception as e:      # a crash: exit 1, still one JSON object, never the token
        _emit(out, {"ok": False, "error": {"code": "INTERNAL", "retryable": False,
                                           "message": "%s: %s" % (type(e).__name__, _short(e))}}, secrets)
        return 1


if __name__ == "__main__":
    sys.exit(main())
