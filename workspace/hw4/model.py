"""model: the HW4 driver's only model call (SYL-113, HW4-2).

    complete(system, user, *, max_tokens=16_000, timeout=120) -> str
    parse_json(text) -> the JSON value in a model answer (a ```json fenced block is fine)

One function over the Anthropic Messages API with urllib, so the provider can be swapped by
changing this file. Environment: ANTHROPIC_API_KEY (required), HW4_MODEL (default
claude-sonnet-5-5), HW4_EFFORT (default low). Any failure raises ModelError; the caller records
`decision: error, reason: model` and stops the cycle.

Current models think by default (adaptive thinking; the thinking blocks come back empty) and that
thinking counts against max_tokens. The first live run hit max_tokens=1500 with no answer text, so
the effort is set to `low` (classification and ranking are simple judgements) and max_tokens is
16,000. A classifier refusal (stop_reason "refusal") is retried server-side on the fallback model
Anthropic recommends (`fallbacks: "default"`) for the models that support it.

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
DEFAULT_EFFORT = "low"
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
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


def complete(system, user, *, max_tokens=16_000, timeout=120, env=None):
    env = os.environ if env is None else env
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ModelError("ANTHROPIC_API_KEY is not set")
    model_id = (env.get("HW4_MODEL") or DEFAULT_MODEL).strip()
    payload = {"model": model_id, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}],
               "output_config": {"effort": (env.get("HW4_EFFORT") or DEFAULT_EFFORT).strip()}}
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    if model_id in FALLBACK_MODELS:
        payload["fallbacks"] = "default"
        headers["anthropic-beta"] = FALLBACK_BETA
    try:
        status, raw = _post(API_URL, json.dumps(payload).encode("utf-8"), headers, timeout)
    except (urllib.error.URLError, OSError) as e:
        raise ModelError("network error: %s" % type(e).__name__)
    if status != 200:
        # The error body names the problem (bad parameter, overload); it never echoes the key.
        raise ModelError("Messages API returned HTTP %d%s" % (status, _api_error(raw)))
    try:
        data = json.loads(raw.decode("utf-8"))
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ModelError("unreadable Messages API response")
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise ModelError("the model declined (refusal, category %s)"
                         % ((data.get("stop_details") or {}).get("category")))
    if stop == "max_tokens":
        raise ModelError("the answer hit max_tokens=%d (%d characters of text)" % (max_tokens, len(text)))
    if not text.strip():
        raise ModelError("empty model answer (stop_reason %s)" % stop)
    return text


def _api_error(raw):
    try:
        err = json.loads(raw.decode("utf-8")).get("error") or {}
        return ": %s: %s" % (err.get("type"), " ".join(str(err.get("message"))[:200].split()))
    except (ValueError, AttributeError, UnicodeDecodeError):
        return ""


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
