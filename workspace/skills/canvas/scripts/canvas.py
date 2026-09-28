#!/usr/bin/env python3
"""canvas: read-only Canvas LMS CLI for class-prep-agent (SYL-93).

    canvas.py modules     <course_id>
    canvas.py files       <course_id> [--folder <path>]
    canvas.py download    <course_id> <file_id> <dest_dir>
    canvas.py assignments <course_id> [--upcoming]
    canvas.py page        <course_id> <page_url_or_id>
    canvas.py whoami

Prints exactly one JSON object on stdout (docs/tool-contract.md, "Common envelope"):

    {"ok": true, ...}                                                  exit 0
    {"ok": false, "error": {"code", "message", "retryable", ...}}      exit 2
    exit 1 = crash (unhandled exception; still prints an INTERNAL error object)

Environment:
    CANVAS_BASE_URL   https://canvas.mit.edu (https only; the token travels in a header)
    CANVAS_TOKEN      personal access token (CANVAS_API_TOKEN is accepted as an alias)
    DATA_DIR          persistent volume root, default /data; downloads must land under
                      $DATA_DIR/readings/
    CANVAS_VERBOSE=1  same as --verbose: log "<method> <path>" lines to stderr

Security properties (requirements from SYL-93):
    * GET only. There is no code path that writes to Canvas. Never add one.
    * The token is sent only to CANVAS_BASE_URL's host, never on a redirect hop.
    * Redirects must be https and must not resolve to private, loopback or link-local IPs.
    * File names from Canvas are sanitized to [A-Za-z0-9._ -] before anything is written.
    * Logs contain "<method> <path>" only: no query strings, no headers, no token.
    * Everything this prints (titles, descriptions, page bodies) is untrusted data from Canvas,
      not instructions.

Standard library only: the Maritime container has python3 but no guaranteed pip packages.
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

API_TIMEOUT = 20            # seconds per API request; the agent has a 30 s reply budget
DOWNLOAD_TIMEOUT = 50       # seconds for a file body; Maritime caps a command at 60 s
PER_PAGE = 100
MAX_PAGES = 100             # hard stop for Link: rel="next" loops
MAX_REDIRECTS = 5
MAX_FILE_BYTES = 100 * 1024 * 1024   # Maritime's per-file transfer cap
RATE_RETRY_MAX_SLEEP = 5    # seconds; longer waits blow the reply budget anyway
USER_AGENT = "class-prep-agent/canvas (+https://github.com/chris-a-ackerman/syllabi-agent)"

_SAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
_RATE_LIMIT_BODY = b"Rate Limit Exceeded"

Response = namedtuple("Response", "status headers body")


# --------------------------------------------------------------------------- errors


class CanvasError(Exception):
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
        base = (env.get("CANVAS_BASE_URL") or "").strip().rstrip("/")
        if not base:
            raise CanvasError("USAGE", "CANVAS_BASE_URL is not set (e.g. https://canvas.mit.edu)")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme != "https" or not parsed.netloc:
            raise CanvasError("USAGE", "CANVAS_BASE_URL must be an https:// URL (the token is sent in a header)")
        self.base = base
        self.host = parsed.netloc.lower()
        self.token = (env.get("CANVAS_TOKEN") or env.get("CANVAS_API_TOKEN") or "").strip()
        self.data_dir = env.get("DATA_DIR") or "/data"
        self.verbose = verbose or env.get("CANVAS_VERBOSE") == "1"

    def log(self, message):
        if self.verbose:
            sys.stderr.write("canvas: %s\n" % message)

    def log_request(self, method, url):
        parsed = urllib.parse.urlparse(url)
        where = parsed.path
        if parsed.netloc.lower() != self.host:
            where = "%s://%s%s" % (parsed.scheme, parsed.netloc, parsed.path)
        self.log("%s %s" % (method, where))      # never the query string (pre-signed verifiers)


# --------------------------------------------------------------------------- transport
# These three module-level hooks are the seams the tests replace. Nothing else does I/O.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib forwards the Authorization header across redirects; we handle 3xx ourselves."""

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
        if max_bytes is not None and total > max_bytes:
            raise CanvasError("FILE_TOO_LARGE", "file body exceeds %d bytes" % max_bytes,
                              detail={"limit": max_bytes})
        chunks.append(chunk)
    return b"".join(chunks)


def _urllib_transport(method, url, headers, timeout, max_bytes=None):
    """One HTTP request, no redirects followed. Returns Response; raises CanvasError on network failure."""
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()},
                            _read_limited(resp, max_bytes))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, body)
    except (urllib.error.URLError, http.client.HTTPException, socket.timeout, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise CanvasError("CANVAS_NET", "network error: %s" % _short(str(reason)), retryable=True)


def _resolve_host(host):
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0].split("%")[0] for info in infos})


transport = _urllib_transport
resolve_host = _resolve_host
sleep = time.sleep


# --------------------------------------------------------------------------- HTTP helpers


def _short(text, limit=200):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _is_rate_limited(resp):
    if resp.status == 429:
        return True
    # Canvas's throttle answers 403 with "403 Forbidden (Rate Limit Exceeded)".
    return resp.status == 403 and _RATE_LIMIT_BODY in (resp.body or b"")[:4096]


def _retry_after(resp):
    try:
        wait = float(resp.headers.get("retry-after", "1"))
    except (TypeError, ValueError):
        wait = 1.0
    return max(0.0, min(wait, RATE_RETRY_MAX_SLEEP))


def _request(cfg, method, url, auth, timeout=API_TIMEOUT, max_bytes=None, accept="application/json"):
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if auth:
        if not cfg.token:
            raise CanvasError("CANVAS_401", "no Canvas token: set CANVAS_TOKEN (or CANVAS_API_TOKEN)", status=None)
        headers["Authorization"] = "Bearer " + cfg.token
    cfg.log_request(method, url)
    resp = transport(method, url, headers, timeout, max_bytes)
    if _is_rate_limited(resp):
        wait = _retry_after(resp)
        cfg.log("rate limited (%d); retrying once after %.1fs" % (resp.status, wait))
        sleep(wait)
        resp = transport(method, url, headers, timeout, max_bytes)
        if _is_rate_limited(resp):
            raise CanvasError("CANVAS_RATE", "Canvas rate limit hit twice; try again on the next run",
                              retryable=True, status=resp.status)
    return resp


def _raise_for_status(resp, what):
    s = resp.status
    if 200 <= s < 300:
        return
    if s == 401:
        raise CanvasError("CANVAS_401", "Canvas rejected the token (401) for %s" % what, status=401)
    if s == 403:
        raise CanvasError("CANVAS_403", "forbidden (403) for %s" % what, status=403)
    if s == 404:
        raise CanvasError("CANVAS_404", "not found (404): %s" % what, status=404)
    if s == 429:
        raise CanvasError("CANVAS_RATE", "rate limited (429) for %s" % what, retryable=True, status=429)
    if s >= 500:
        raise CanvasError("CANVAS_NET", "Canvas returned HTTP %d for %s" % (s, what), retryable=True, status=s)
    raise CanvasError("CANVAS_NET", "unexpected HTTP %d for %s" % (s, what), retryable=False, status=s)


def _decode_json(resp, what):
    body = resp.body or b""
    if body.startswith(b"while(1);"):
        body = body[len(b"while(1);"):]
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CanvasError("CANVAS_NET", "Canvas returned non-JSON for %s (login page or outage?)" % what,
                          retryable=True, status=resp.status)


def _next_link(link_header):
    if not link_header:
        return None
    m = _NEXT_LINK.search(link_header)
    return m.group(1) if m else None


def _q(value):
    return urllib.parse.quote(str(value), safe="")


def api_get(cfg, path, params=None, paginate=False):
    """GET $CANVAS_BASE_URL/api/v1<path>. With paginate=True, follows Link: rel="next" and returns the
    concatenated list (per_page=100). Without it, returns the decoded JSON as-is."""
    query = dict(params or {})
    if paginate:
        query["per_page"] = PER_PAGE
    url = cfg.base + "/api/v1" + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    items = []
    for _ in range(MAX_PAGES):
        resp = _request(cfg, "GET", url, auth=True)
        _raise_for_status(resp, path)
        data = _decode_json(resp, path)
        if not paginate:
            return data
        if not isinstance(data, list):
            raise CanvasError("CANVAS_NET", "expected a JSON list for %s" % path, retryable=False)
        items.extend(data)
        nxt = _next_link(resp.headers.get("link"))
        if not nxt:
            break
        if urllib.parse.urlparse(nxt).netloc.lower() != cfg.host:
            cfg.log("ignoring next-page link on another host")   # the token stays on our host
            break
        url = nxt
    return items


# --------------------------------------------------------------------------- HTML → text

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "thead",
    "tbody", "blockquote", "pre", "section", "article", "header", "footer", "hr", "dl", "dt", "dd",
    "figure", "figcaption", "address", "aside", "nav", "main", "form", "fieldset",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "iframe", "object"}
_END_NEWLINE_TAGS = _BLOCK_TAGS - {"br", "li", "hr"}


class _TextExtractor(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self._seen = set()
        self._skip = 0
        self._lists = []
        self._href = None
        self._link_text = []
        self._base_url = base_url

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        attrs = dict(attrs)
        if tag in ("ul", "ol"):
            self._lists.append([tag, 0])
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n")
            if self._lists:
                kind, n = self._lists[-1]
                self._lists[-1][1] = n + 1
                self.parts.append("%d. " % (n + 1) if kind == "ol" else "- ")
            else:
                self.parts.append("- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        if tag == "a":
            self._href = attrs.get("href")
            self._link_text = []
        elif tag == "img" and attrs.get("alt"):
            self.parts.append(attrs["alt"])

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("br", "hr"):
            if not self._skip:
                self.parts.append("\n")
            return
        self.handle_starttag(tag, attrs)
        if tag not in _SKIP_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self.parts.append("\n")
        elif tag in _END_NEWLINE_TAGS:
            self.parts.append("\n")
        if tag == "a" and self._href is not None:
            self._add_link("".join(self._link_text), self._href)
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def _add_link(self, text, href):
        href = (href or "").strip()
        if not href or href.startswith("#"):
            return
        absolute = urllib.parse.urljoin(self._base_url, href)
        if urllib.parse.urlparse(absolute).scheme not in ("http", "https"):
            return
        if absolute in self._seen:
            return
        self._seen.add(absolute)
        self.links.append({"text": " ".join(text.split()), "href": absolute})


def html_to_text(html, base_url=""):
    """Strip HTML to readable plain text (full length, never truncated) and collect the links.
    Returns (text, [{"text", "href"}]). Entities are decoded; script/style contents are dropped."""
    if not html:
        return "", []
    parser = _TextExtractor(base_url)
    parser.feed(html)
    parser.close()
    raw = "".join(parser.parts)
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in raw.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text, parser.links


# --------------------------------------------------------------------------- file safety


def safe_basename(name):
    """Reduce an attacker-controllable Canvas display_name to a safe basename: ASCII-fold, keep only
    [A-Za-z0-9._ -] (others become '_'), strip leading dots and surrounding junk, cap the length.
    Returns '' when nothing safe is left; callers must reject that."""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = _SAFE_NAME_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().lstrip(".").strip(" .")
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[: max(1, 200 - len(ext))] + ext
    return name


def _readings_root(cfg):
    return os.path.realpath(os.path.join(cfg.data_dir, "readings"))


def _checked_dest_dir(cfg, dest_dir):
    root = _readings_root(cfg)
    real = os.path.realpath(dest_dir)
    if real != root and not real.startswith(root + os.sep):
        raise CanvasError("USAGE", "dest_dir must be under %s/" % os.path.join(cfg.data_dir, "readings"),
                          detail={"dest_dir": dest_dir})
    return real


def _require_public_host(host):
    if not host:
        raise CanvasError("CANVAS_NET", "redirect target has no host", retryable=False)
    try:
        addresses = resolve_host(host)
    except (OSError, ValueError):
        raise CanvasError("CANVAS_NET", "could not resolve %s" % host, retryable=True, detail={"host": host})
    if not addresses:
        raise CanvasError("CANVAS_NET", "could not resolve %s" % host, retryable=True, detail={"host": host})
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise CanvasError("CANVAS_NET", "unparseable address for %s" % host, retryable=False)
        if not ip.is_global or ip.is_multicast:
            raise CanvasError("CANVAS_NET", "refusing to fetch from a private or local address (%s)" % host,
                              retryable=False, detail={"host": host})


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_file(cfg, url):
    """Fetch a pre-signed file URL. The token goes only to our own host and only on the first hop;
    every redirect must be https and point at a public address."""
    for hop in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise CanvasError("CANVAS_NET", "refusing a non-https download url", retryable=False,
                              detail={"scheme": parsed.scheme, "host": parsed.netloc})
        same_host = parsed.netloc.lower() == cfg.host
        if not same_host:
            _require_public_host(parsed.hostname)
        resp = _request(cfg, "GET", url, auth=(same_host and hop == 0), timeout=DOWNLOAD_TIMEOUT,
                        max_bytes=MAX_FILE_BYTES, accept="*/*")
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                raise CanvasError("CANVAS_NET", "redirect without a Location header", retryable=False)
            url = urllib.parse.urljoin(url, location)
            continue
        _raise_for_status(resp, "file download")
        return resp.body
    raise CanvasError("CANVAS_NET", "too many redirects while fetching the file", retryable=False)


# --------------------------------------------------------------------------- commands


def _folder_path(full_name):
    parts = [p for p in (full_name or "").split("/") if p != ""]
    if parts and parts[0].lower() == "course files":
        parts = parts[1:]
    return "/".join(parts)


def cmd_whoami(cfg, args):
    me = api_get(cfg, "/users/self")
    if not isinstance(me, dict):
        raise CanvasError("CANVAS_NET", "unexpected /users/self response", retryable=False)
    return {"ok": True, "user": {"id": me.get("id"), "name": me.get("name")}}


def cmd_modules(cfg, args):
    course = _q(args.course_id)
    modules = api_get(cfg, "/courses/%s/modules" % course,
                      {"include[]": ["items", "content_details"]}, paginate=True)
    flat = []
    for module in modules:
        if not isinstance(module, dict):
            continue
        items = module.get("items")
        if items is None:   # Canvas omits items for large modules; fetch them separately
            items = api_get(cfg, "/courses/%s/modules/%s/items" % (course, _q(module.get("id"))),
                            {"include[]": ["content_details"]}, paginate=True)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            details = item.get("content_details") or {}
            flat.append({
                "module": module.get("name"),
                "module_id": module.get("id"),
                "position": module.get("position"),
                "item_position": item.get("position"),
                "item_type": item.get("type"),
                "title": item.get("title"),
                "id": item.get("id"),
                "content_id": item.get("content_id"),
                "url": item.get("url"),
                "html_url": item.get("html_url"),
                "page_url": item.get("page_url"),
                "external_url": item.get("external_url"),
                "published": item.get("published"),
                "content_type": details.get("content_type"),
                "size": details.get("size"),
                "locked_for_user": details.get("locked_for_user"),
            })
    return {"ok": True, "course_id": args.course_id, "modules": len(modules), "items": flat}


def cmd_files(cfg, args):
    course = _q(args.course_id)
    files = api_get(cfg, "/courses/%s/files" % course, {"sort": "updated_at", "order": "desc"}, paginate=True)
    folders = {}
    folders_error = None
    try:
        for folder in api_get(cfg, "/courses/%s/folders" % course, paginate=True):
            if isinstance(folder, dict):
                folders[folder.get("id")] = _folder_path(folder.get("full_name"))
    except CanvasError as e:      # files are still useful without folder names
        folders_error = e.code
    needle = (args.folder or "").strip("/").lower()
    out = []
    for f in files:
        if not isinstance(f, dict):
            continue
        folder = folders.get(f.get("folder_id"))
        if needle and needle not in (folder or "").lower():
            continue
        out.append({
            "id": f.get("id"),
            "display_name": f.get("display_name"),
            "filename": f.get("filename"),
            "content_type": f.get("content-type"),
            "size": f.get("size"),
            "updated_at": f.get("updated_at"),
            "folder": folder,
            "folder_id": f.get("folder_id"),
            "locked_for_user": f.get("locked_for_user"),
        })
    result = {"ok": True, "course_id": args.course_id, "files": out}
    if folders_error:
        result["folders_error"] = folders_error
    return result


def cmd_download(cfg, args):
    dest = _checked_dest_dir(cfg, args.dest_dir)
    what = "/courses/%s/files/%s" % (_q(args.course_id), _q(args.file_id))
    meta = api_get(cfg, what)
    if not isinstance(meta, dict):
        raise CanvasError("CANVAS_NET", "unexpected file metadata for %s" % what, retryable=False)
    name = safe_basename(meta.get("display_name")) or safe_basename(meta.get("filename"))
    if not name:
        raise CanvasError("BAD_FILENAME", "the Canvas file name sanitizes to nothing; not writing it",
                          detail={"file_id": args.file_id})
    size = meta.get("size")
    canvas_url = "%s/courses/%s/files/%s" % (cfg.base, _q(args.course_id), _q(args.file_id))
    if isinstance(size, int) and size > MAX_FILE_BYTES:
        raise CanvasError("FILE_TOO_LARGE", "file is %d bytes; the transfer cap is %d" % (size, MAX_FILE_BYTES),
                          detail={"size": size, "limit": MAX_FILE_BYTES, "canvas_url": canvas_url})
    if meta.get("locked_for_user"):
        raise CanvasError("CANVAS_403", "file is locked for this user: %s" % _short(meta.get("lock_explanation") or ""),
                          status=403, detail={"canvas_url": canvas_url})
    url = meta.get("url")
    if not url:
        raise CanvasError("CANVAS_403", "Canvas returned no download url for this file", status=403,
                          detail={"canvas_url": canvas_url})
    final = os.path.join(dest, name)
    common = {"ok": True, "file_id": args.file_id, "display_name": name,
              "content_type": meta.get("content-type"), "path": final}
    if os.path.isfile(final) and isinstance(size, int) and os.path.getsize(final) == size:
        common.update({"bytes": size, "sha256": _sha256_file(final), "skipped": True})
        return common
    body = _fetch_file(cfg, url)
    os.makedirs(dest, exist_ok=True)
    part = final + ".part"
    with open(part, "wb") as fh:
        fh.write(body)
    os.replace(part, final)
    common.update({"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "skipped": False})
    return common


def cmd_assignments(cfg, args):
    params = {"include[]": ["submission"]}
    if args.upcoming:
        params["bucket"] = "upcoming"
    assignments = api_get(cfg, "/courses/%s/assignments" % _q(args.course_id), params, paginate=True)
    out = []
    for a in assignments:
        if not isinstance(a, dict):
            continue
        submission = a.get("submission") or {}
        text, links = html_to_text(a.get("description") or "", a.get("html_url") or cfg.base)
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "due_at": a.get("due_at"),
            "html_url": a.get("html_url"),
            "description_text": text,
            "links": links,
            "submission_types": a.get("submission_types") or [],
            "submitted": bool(submission.get("submitted_at"))
            or submission.get("workflow_state") in ("submitted", "pending_review"),
        })
    return {"ok": True, "course_id": args.course_id, "assignments": out}


def cmd_page(cfg, args):
    page = api_get(cfg, "/courses/%s/pages/%s" % (_q(args.course_id), _q(args.page)))
    if not isinstance(page, dict):
        raise CanvasError("CANVAS_NET", "unexpected page response", retryable=False)
    text, links = html_to_text(page.get("body") or "", page.get("html_url") or cfg.base)
    return {"ok": True, "page": {
        "url": page.get("url"),
        "title": page.get("title"),
        "body_text": text,
        "links": links,
        "updated_at": page.get("updated_at"),
        "html_url": page.get("html_url"),
    }}


COMMANDS = {
    "modules": cmd_modules,
    "files": cmd_files,
    "download": cmd_download,
    "assignments": cmd_assignments,
    "page": cmd_page,
    "whoami": cmd_whoami,
}
# Names from the V0 tool contract keep working.
ALIASES = {
    "list_modules": "modules",
    "list_files": "files",
    "download_file": "download",
    "upcoming_assignments": "assignments",
    "get_page": "page",
}


# --------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CanvasError("USAGE", message)


def build_parser():
    p = _Parser(prog="canvas", description="Read-only Canvas LMS access for class-prep-agent. Prints one JSON object.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log '<method> <path>' lines to stderr (never the token or query strings)")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("modules", aliases=["list_modules"], help="module items, flattened")
    s.add_argument("course_id")

    s = sub.add_parser("files", aliases=["list_files"], help="course files, newest first")
    s.add_argument("course_id")
    s.add_argument("--folder", help="keep only files whose folder path contains this (case-insensitive)")

    s = sub.add_parser("download", aliases=["download_file"], help="download one file under $DATA_DIR/readings/")
    s.add_argument("course_id")
    s.add_argument("file_id")
    s.add_argument("dest_dir")

    s = sub.add_parser("assignments", aliases=["upcoming_assignments"], help="assignments with full description text")
    s.add_argument("course_id")
    s.add_argument("--upcoming", action="store_true", help="only the upcoming bucket")

    s = sub.add_parser("page", aliases=["get_page"], help="one wiki page as text")
    s.add_argument("course_id")
    s.add_argument("page", metavar="page_url_or_id")

    sub.add_parser("whoami", help="auth smoke test: GET /users/self")
    return p


def _emit(out, obj, secret=None):
    text = json.dumps(obj, ensure_ascii=False)
    if secret:
        text = text.replace(secret, "<redacted>")
    out.write(text + "\n")
    out.flush()


def main(argv=None, env=None, out=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    secret = (env.get("CANVAS_TOKEN") or env.get("CANVAS_API_TOKEN") or "").strip() or None
    try:
        args = build_parser().parse_args(argv)
        if not args.command:
            raise CanvasError("USAGE", "missing command: one of %s" % ", ".join(COMMANDS))
        if args.command == "upcoming_assignments":
            args.upcoming = True          # the V0 name implied the upcoming bucket
        cfg = Config(env, verbose=args.verbose)
        result = COMMANDS[ALIASES.get(args.command, args.command)](cfg, args)
        _emit(out, result, secret)
        return 0
    except CanvasError as e:
        _emit(out, e.to_json(), secret)
        return 2
    except Exception as e:      # a crash: exit 1, still one JSON object, never the token
        _emit(out, {"ok": False, "error": {"code": "INTERNAL", "retryable": False,
                                           "message": "%s: %s" % (type(e).__name__, _short(e))}}, secret)
        return 1


if __name__ == "__main__":
    sys.exit(main())
