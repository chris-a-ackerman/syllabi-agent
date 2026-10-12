"""model: the HW4 driver's only model call (SYL-113, HW4-2).

    complete(system, user, *, max_tokens=1500, timeout=60) -> str
    parse_json(text) -> the JSON value in a model answer (a ```json fenced block is fine)

One function over the Anthropic Messages API with urllib, so the provider can be swapped by
changing this file. Environment: ANTHROPIC_API_KEY (required), HW4_MODEL (default
claude-sonnet-5-5). Any failure raises ModelError; the caller records `decision: error,
reason: model` and stops the cycle.

Callers never put environment values in `system` or `user`: the prompts carry forum entry ids
and capped, delimited peer text only. Standard library only, Python 3.10+.
"""
import json
import os
import re
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_BODY = 1024 * 1024

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.S)


class ModelError(Exception):
    pass


def _post(url, body, headers, timeout):
    """One HTTPS POST; the seam the tests replace. Returns (status, bytes)."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096)


def complete(system, user, *, max_tokens=1500, timeout=60, env=None):
    env = os.environ if env is None else env
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ModelError("ANTHROPIC_API_KEY is not set")
    payload = {"model": (env.get("HW4_MODEL") or DEFAULT_MODEL).strip(), "max_tokens": max_tokens,
               "system": system, "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    try:
        status, raw = _post(API_URL, json.dumps(payload).encode("utf-8"), headers, timeout)
    except (urllib.error.URLError, OSError) as e:
        raise ModelError("network error: %s" % type(e).__name__)
    if status != 200:
        # The error body names the problem (model, overload); it never echoes the key.
        raise ModelError("Messages API returned HTTP %d" % status)
    try:
        data = json.loads(raw.decode("utf-8"))
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ModelError("unreadable Messages API response")
    if not text.strip():
        raise ModelError("empty model answer (stop_reason %s)" % data.get("stop_reason"))
    return text


def parse_json(text):
    """The JSON in a model answer: the whole text, else the first fenced block, else the span from
    the first { or [ to the last } or ]. Raises ModelError when none of them parses."""
    candidates = [text.strip()]
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        end = max(text.rfind("}"), text.rfind("]"))
        if end > min(starts):
            candidates.append(text[min(starts):end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            continue
    raise ModelError("the model did not answer with JSON")
