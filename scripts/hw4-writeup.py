#!/usr/bin/env python3
"""hw4-writeup: the HW4 technical write-up, rebuilt from the agent's own memory after every run.

    python3 scripts/hw4-writeup.py [--state DIR] [--out DIR]

Reads state/memory/hw4-state.json, state/memory/forum-state.json and state/logs/hw4.jsonl (DIR
defaults to $DATA_DIR, then ./state) and writes <out>/hw4-writeup.md (out defaults to ./docs).
The workflow commits it with the state, so whatever the exchange's outcome (accepted result, open request, or
only documented attempts) the submission is ready to download from main.

No model call and no network: every sentence below is either fixed text about how the agent is
built or a fact read from the state files, so the write-up cannot claim something the logs do not
show. Standard library only.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_URL = "https://github.com/chris-a-ackerman/syllabi-agent"
FORUM_URL = "https://canvas.mit.edu/courses/40577/discussion_topics/449948"
TITLE = "HW4: Make Your Agent Hire Another Agent"
OPEN = ("sent", "result_received", "correction_requested")
DISCOVERY_ROWS = 5
CYCLE_ROWS = 6

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - no tz database: print UTC
    ET = dt.timezone.utc


# --------------------------------------------------------------------------- reading the state


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _time(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def et(ts):
    t = _time(ts)
    return t.astimezone(ET).strftime("%b %-d, %H:%M ET") if t else "?"


def plain(text):
    """Text that came from peers (offer summaries, ranking reasons, authors): no Markdown syntax
    survives, so it cannot add links or formatting to the write-up."""
    return re.sub(r"[`*]", "", str(text if text is not None else "")).replace("[", "(").replace("]", ")")


def _clip(text, n):
    text = plain(text)
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def entry_url(eid):
    return "%s#entry-%s" % (FORUM_URL, eid)


def link(eid, label=None):
    return "[%s](%s)" % (label or "entry %s" % eid, entry_url(eid)) if eid is not None else "-"


# --------------------------------------------------------------------------- a tiny document model


class Doc:
    """Blocks rendered to Markdown."""

    def __init__(self):
        self.blocks = []

    def h(self, level, text):
        self.blocks.append(("h", level, text))

    def p(self, text):
        self.blocks.append(("p", text))

    def ul(self, items):
        self.blocks.append(("ul", list(items)))

    def table(self, head, rows):
        self.blocks.append(("table", head, [list(r) for r in rows]))

    def markdown(self):
        out = []
        for b in self.blocks:
            if b[0] == "h":
                out.append("#" * b[1] + " " + b[2])
            elif b[0] == "p":
                out.append(b[1])
            elif b[0] == "ul":
                out.append("\n".join("- " + i for i in b[1]))
            else:
                cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
                rows = ["| " + " | ".join(cell(c) for c in b[1]) + " |",
                        "| " + " | ".join("---" for _ in b[1]) + " |"]
                rows += ["| " + " | ".join(cell(c) for c in r) + " |" for r in b[2]]
                out.append("\n".join(rows))
        return "\n\n".join(out) + "\n"


# --------------------------------------------------------------------------- the write-up


def checks_cell(c):
    if not c:
        return "-"
    return ", ".join("%s %s" % (k, c[k]) for k in ("static", "correct", "m1", "m2") if c.get(k))


def provider_fit(st, req):
    """The ranking that chose this provider: (fit, reason, number of candidates)."""
    pid = req.get("provider_entry_id")
    for d in reversed(st.get("discovery") or []):
        if d.get("chosen") == pid:
            fit = next((r.get("fit") for r in d.get("ranking") or [] if r.get("entry_id") == pid), None)
            return fit, d.get("reason") or "", len(d.get("candidates") or [])
    return None, "", 0


def capability(st, eid):
    o = next((o for o in st.get("offers_seen") or [] if o.get("entry_id") == eid), None)
    return (o or {}).get("capability") or ""


def build(st, fs, log, model, halted=False):
    requests = list(st.get("past_requests") or []) + ([st["request"]] if st.get("request") else [])
    accepted = [r for r in requests if r.get("status") == "accepted"]
    open_req = next((r for r in requests if r.get("status") in OPEN), None)
    jobs = st.get("jobs") or []
    cycles = [x for x in log if "event" not in x]
    offer = st.get("offer") or {}
    d = Doc()

    d.h(1, TITLE)
    d.p("MIT AI Studio (MAS.665) · Chris Ackerman · agent: class-prep-agent · code: [%s](%s)" % (
        REPO_URL.split("github.com/")[1], REPO_URL))
    if accepted:
        r = accepted[-1]
        status = ("**Status: exchange complete.** The tests delivered in %s by %s passed every agreed check "
                  "and were accepted on %s; they now run in this project's test suite." % (
                      link(r.get("result_entry_id")), plain(r.get("provider_author")), et(r.get("accepted_at"))))
    elif open_req:
        status = ("**Status: request open.** %s was sent to %s and is open until %s. If no passing result "
                  "arrives by then, the agent marks it timed out and asks the next suitable provider on a "
                  "later cycle." % (link(open_req.get("request_entry_id"), "The request"),
                                    plain(open_req.get("provider_author")), open_req.get("deadline_et") or et(open_req.get("deadline"))))
    else:
        status = ("**Status: documented attempts.** No peer delivered a result that passed the checks (%d "
                  "request(s) sent; see §3 and §5). The agent keeps reading offers each cycle and will ask "
                  "any new suitable provider." % len(requests))
    d.p(status + " This document is regenerated from the agent's state after every scheduled run; "
        "last cycle %s, %d cycles in all." % (et(st.get("last_cycle_at")), len(cycles)))

    # 1. Task and service
    d.h(2, "1. Task and service")
    d.p("**Service offered** (%s, posted %s): syllabus to calendar and workload estimate. A classmate's "
        "agent replies to the offer with 500 to 12,000 characters of syllabus text. The agent replies with a "
        "table of dated events, an iCalendar block generated by code, and an hours-per-week estimate. Every "
        "event quotes the syllabus line it came from, and code drops any event whose quote is not in the "
        "text verbatim, so the requester can check each date." % (link(offer.get("entry_id"), "offer"),
                                                                     et(offer.get("posted_at"))))
    d.p("**Task delegated:** unit tests for `control_state()`, the function (a 15-line module) that reads the course "
        "team's control line and decides whether the agent may write at all. It is small, self-contained and "
        "safety-critical, so independent tests are useful and can be checked mechanically: they must pass on "
        "the real function and fail on two deliberately broken versions.")
    if requests:
        rows = []
        for r in requests:
            fit, reason, n = provider_fit(st, r)
            rows.append([link(r.get("provider_entry_id")), plain(r.get("provider_author") or "?"),
                         plain(capability(st, r.get("provider_entry_id")) or "-"),
                         "%s/3 of %d offers" % (fit, n) if fit is not None else "-", plain(reason or "-")])
        d.p("**Provider choice.** Each cycle a model call ranks every untried offer the agent has read "
            "(fit 0 to 3 against the need \"can write Python unit tests, or review/test code; stdlib Python; "
            "will return a file I can run\") and the agent replies to the best one with fit 2 or more (1 or more after 3 cycles with no fit). "
            "No partner is hard-coded; a provider that times out, declines or fails is never asked again.")
        d.table(["Offer", "Author", "What it offers", "Fit", "Why chosen"], rows)
    else:
        d.p("**Provider choice:** no offer has met the bar yet (see §5).")

    # 2. Technical setup
    d.h(2, "2. Technical setup")
    d.ul([
        "**Harness:** a Python driver, `scripts/hw4-cycle.py`, on GitHub Actions (`.github/workflows/"
        "hw4-forum.yml`). One run is one cycle: read the forum, classify new entries, post the offer once, "
        "serve one job, check one reply to our request, choose a provider and send a request.",
        "**Model:** `%s` through the Anthropic Messages API, with structured JSON output, for classification, "
        "provider ranking, answering a provider's question and the syllabus extraction. Every check and every "
        "decision to post is code, not the model." % model,
        "**Canvas:** the HW3 forum tool (`workspace/skills/forum/scripts/forum.py`) with my own Canvas token, "
        "kept as a GitHub Actions secret. It reads the HW4 topic and posts replies through the Discussions API.",
        "**Schedule:** every 2 hours. GitHub's cron fires unreliably, so a scheduled Claude Code cloud routine "
        "also starts the workflow every 2 hours unless a run started in the last 45 minutes.",
        "**Memory:** `state/` is committed back to `main` after every run: entries already read, own posts, "
        "the open request and its deadline, providers tried, every ranking and every verdict. The next "
        "run starts from it, so replies are handled without me relaying messages and a request is never sent "
        "twice.",
    ])

    # 3. Results and verification
    d.h(2, "3. Results and verification")
    d.p("The agent checks a delivery itself, in a Docker sandbox with no network, a read-only file "
        "system, 512 MB, 60 s and no secrets in its environment: **(static)** the file parses, has a "
        "`unittest.TestCase` with 3+ tests and imports only the standard library, with no file, network or "
        "subprocess use; **(correct)** every test passes against the real function; **(m1)** at least one "
        "fails against a mutant that reports PAUSED as RUNNING; **(m2)** at least one fails against a mutant "
        "that skips whitespace stripping and upper-casing. All pass: accept and tip. A failure: one "
        "correction request quoting the failing output (+12 h), then reject.")
    if requests:
        d.table(["Request", "Provider", "Sent", "Deadline", "Outcome", "Result", "Checks", "Tip"],
                [[link(r.get("request_entry_id")), link(r.get("provider_entry_id")), et(r.get("sent_at")),
                  r.get("deadline_et") or et(r.get("deadline")), (r.get("status") or "?").replace("_", " ") +
                  (" (%s)" % plain(r["reason"]) if r.get("reason") else ""), link(r.get("result_entry_id")),
                  checks_cell(r.get("checks")), "-" if r.get("tip") is None else r.get("tip")]
                 for r in requests])
    if accepted:
        r = accepted[-1]
        adopted = r.get("adopted")
        d.p("**Used in the final output:** the accepted tests were committed as %s and run with the rest of "
            "the suite (`python3 -m unittest discover -s tests`). Verdict and tip: %s." % (
                "[`%s`](%s/blob/main/%s)" % (adopted, REPO_URL, adopted) if adopted else "a test file",
                link(r.get("verdict_entry_id"))))
    else:
        d.p("**Unfinished:** no delivered tests have passed the checks yet, so nothing has been adopted. "
            "Accepted tests would be committed as `tests/test_hw4_delegated_control_state.py` and run with "
            "the project's suite; the checks above are already implemented and unit-tested with stub "
            "deliveries (`tests/test_hw4_verify.py`).")
    if jobs:
        d.p("**Requests the agent served for others:**")
        d.table(["Request", "From", "Received", "Outcome", "Reply", "Checks"],
                [[link(j.get("request_entry_id")), plain(j.get("author") or "?"), et(j.get("received_at")),
                  j.get("status") or "?", link(j.get("result_entry_id")),
                  ", ".join("%s %s" % kv for kv in (j.get("checks") or {}).items()) or "-"] for j in jobs])
    else:
        d.p("No classmate has replied to the offer yet (receiving a request is not required).")

    # 4. Links
    d.h(2, "4. Canvas discussion links")
    labels = {offer.get("entry_id"): "service offer (current)"}
    for o in st.get("past_offers") or []:
        labels[o.get("entry_id")] = "earlier service offer, retired when the service changed"
    for r in requests:
        labels[r.get("request_entry_id")] = "request to %s" % link(r.get("provider_entry_id"), "offer %s" % r.get("provider_entry_id"))
        for k, what in (("verdict_entry_id", "verdict"), ("correction_entry_id", "correction request"),
                        ("answer_entry_id", "answer to the provider's question")):
            if r.get(k):
                labels[r[k]] = "%s on %s" % (what, link(r.get("result_entry_id"), "result %s" % r.get("result_entry_id")))
    for j in jobs:
        for k in ("result_entry_id", "part2_entry_id"):
            if j.get(k):
                labels[j[k]] = "reply to %s's request %s" % (plain(j.get("author")), j.get("request_entry_id"))
    items = ["Forum: [Homework 4: Agent Services Forum](%s)" % FORUM_URL]
    for p in sorted(fs.get("posts") or [], key=lambda p: p.get("posted_at") or ""):
        eid = p.get("entry_id")
        items.append("%s, %s: %s" % (link(eid), et(p.get("posted_at")), labels.get(eid, "own post")))
    for r in requests:
        for res in (r.get("past_results") or []) + ([{"entry_id": r["result_entry_id"]}] if r.get("result_entry_id") else []):
            items.append("%s: returned work from the provider of %s (checked: %s)" % (
                link(res["entry_id"]), link(r.get("provider_entry_id"), "offer %s" % r.get("provider_entry_id")),
                checks_cell(res.get("checks") or (r.get("checks") if res["entry_id"] == r.get("result_entry_id") else {}))))
    d.ul(items)

    # 5. Discovery log
    d.h(2, "5. Discovery log")
    disc = [x for x in st.get("discovery") or [] if x.get("reason") != "deadline passed" or x.get("candidates")]
    timeouts = [x for x in st.get("discovery") or [] if x.get("reason") == "deadline passed"]
    d.p("%d ranking(s) over %d offers read; %d request(s) timed out. %s" % (
        len(disc), len(st.get("offers_seen") or []), len(timeouts),
        "Most recent %d shown." % DISCOVERY_ROWS if len(disc) > DISCOVERY_ROWS else ""))
    rows = []
    for x in disc[-DISCOVERY_ROWS:]:
        top = sorted(x.get("ranking") or [], key=lambda r: -(r.get("fit") or 0))[:2]
        rows.append([et(x.get("ts")), len(x.get("candidates") or []),
                     link(x["chosen"]) if x.get("chosen") else "none",
                     "; ".join("%s fit %s" % (r.get("entry_id"), r.get("fit")) for r in top) or "-",
                     _clip(x.get("reason") or "-", 160)])
    if rows:
        d.table(["Time", "Offers", "Chosen", "Top fits", "Reason"], rows)

    # 6. Safety
    d.h(2, "6. Safety controls")
    posts = sum(int(x.get("posts") or 0) for x in cycles)
    errors = [x for x in cycles if x.get("decision") == "error"]
    d.ul([
        "**Credentials:** the Canvas token and API key are GitHub secrets, never printed (no `set -x`, no "
        "environment dumps); `forum.py` scrubs their values from everything it writes or posts. Delivered "
        "tests run in a child process with a fresh environment, so secrets never reach peer code.",
        "**Untrusted input:** peer text is capped and wrapped in random-nonce delimiters, and every prompt "
        "says it is data; the model's answers are schema-constrained and re-checked by code (an offer number "
        "must name a real candidate). The agent never runs peer instructions; peer code runs only in the "
        "sandbox in §3.",
        "**Bounded tools and spending:** at most 2 posts per cycle, small model calls at low effort, a "
        "20-minute job timeout, and runs never overlap.",
        "**Forum control and write limit:** before every write the agent reads the control line and writes "
        "only on `COURSE-TEAM CONTROL: RUNNING`; at most 3 write attempts per rolling hour, retries included.",
        "**Verified, de-duplicated posts:** every post is read back; an uncertain write is reconciled against "
        "the thread before any retry, and a text hash blocks duplicates.",
        "**Stopping:** 3 consecutive failures or a rejected token writes a halt file that blocks all posting "
        "until a human removes it.",
        "**This run so far:** %d cycles, %d posts, %d cycle errors (%s), halt file %s." % (
            len(cycles), posts, len(errors),
            ", ".join(sorted({x.get("error_code") or "?" for x in errors})) or "none",
            "present" if halted else "absent"),
    ])
    d.p("**Recent cycles** (from `state/logs/hw4.jsonl`):")
    d.table(["Time", "New entries", "Decision", "Reason"],
            [[et(x.get("ts")), x.get("new_count"), x.get("decision"), _clip(x.get("reason") or "", 120)]
             for x in cycles[-CYCLE_ROWS:]])
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", default=os.environ.get("DATA_DIR") or os.path.join(REPO, "state"))
    ap.add_argument("--out", default=os.path.join(REPO, "docs"))
    a = ap.parse_args(argv)
    st = _load(os.path.join(a.state, "memory", "hw4-state.json"), {})
    fs = _load(os.path.join(a.state, "memory", "forum-state.json"), {})
    log = _jsonl(os.path.join(a.state, "logs", "hw4.jsonl"))
    model = (os.environ.get("HW4_MODEL") or "claude-sonnet-5-5").strip()
    doc = build(st, fs, log, model, os.path.exists(os.path.join(a.state, "memory", "forum-halt")))
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "hw4-writeup.md"), "w", encoding="utf-8") as fh:
        fh.write(doc.markdown())
    print(json.dumps({"ok": True, "out": a.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
