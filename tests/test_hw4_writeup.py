"""Tests for scripts/hw4-writeup.py: the HW4 write-up rebuilt from the agent's state after every run."""
import importlib.util
import json
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("hw4_writeup", os.path.join(REPO, "scripts", "hw4-writeup.py"))
hw4_writeup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hw4_writeup)

OFFER = {"entry_id": 900, "posted_at": "2026-10-12T18:41:24Z",
         "html_url": hw4_writeup.entry_url(900)}
CYCLE = {"actions": [], "control": "RUNNING", "decision": "skip", "new_count": 3, "posts": 0,
         "reason": "3 new", "ts": "2026-10-13T16:48:15Z"}


def request(status, **kw):
    r = {"provider_entry_id": 100, "provider_author": "user 7", "request_entry_id": 901,
         "sent_at": "2026-10-12T18:41:24Z", "deadline": "2026-10-13T18:41:24Z",
         "deadline_et": "Tue Oct 13, 2026, 02:41 PM EDT", "status": status, "result_entry_id": None,
         "checks": {}, "tip": None}
    r.update(kw)
    return r


def state(**kw):
    st = {"offer": OFFER, "past_offers": [], "offers_seen": [
              {"entry_id": 100, "author": "user 7", "kind": "offer", "capability": "writes Python unit tests"},
              {"entry_id": 101, "author": "user 8", "kind": "offer", "capability": "pytest suites"}],
          "request": None, "past_requests": [], "tried_providers": [], "jobs": [], "inbox": [],
          "discovery": [{"ts": "2026-10-12T18:41:24Z", "candidates": [100, 101], "chosen": 100,
                         "reason": "best fit", "ranking": [{"entry_id": 100, "fit": 3, "why": "tests"}]}],
          "last_cycle_at": "2026-10-13T16:48:15Z"}
    st.update(kw)
    return st


FORUM = {"posts": [{"entry_id": 900, "parent_id": None, "posted_at": "2026-10-12T18:41:24Z"},
                   {"entry_id": 901, "parent_id": 100, "posted_at": "2026-10-12T18:41:30Z"}]}


def render(st, fs=FORUM, log=(CYCLE,)):
    return hw4_writeup.build(st, fs, list(log), "claude-sonnet-5-5")


class WriteupTest(unittest.TestCase):
    def test_open_request(self):
        md = render(state(request=request("sent"))).markdown()
        self.assertIn("**Status: request open.**", md)
        self.assertIn(hw4_writeup.entry_url(901), md)
        self.assertIn("service offer (current)", md)
        self.assertIn("request to [offer 100]", md)
        self.assertIn("**Unfinished:**", md)

    def test_accepted_result_is_used(self):
        r = request("accepted", result_entry_id=950, verdict_entry_id=951, tip=5,
                    accepted_at="2026-10-13T20:00:00Z", adopted="tests/test_hw4_delegated_control_state.py",
                    checks={"static": "pass", "correct": "pass", "m1": "pass", "m2": "pass"})
        fs = {"posts": FORUM["posts"] + [{"entry_id": 951, "parent_id": 950, "posted_at": "2026-10-13T20:00:00Z"}]}
        md = render(state(request=r), fs=fs).markdown()
        self.assertIn("**Status: exchange complete.**", md)
        self.assertIn("returned work from the provider of [offer 100]", md)
        self.assertIn("blob/main/tests/test_hw4_delegated_control_state.py", md)
        self.assertIn("static pass, correct pass, m1 pass, m2 pass", md)
        self.assertIn("verdict on [result 950]", md)
        self.assertNotIn("**Unfinished:**", md)

    def test_documented_attempts_after_timeout(self):
        st = state(past_requests=[request("timed_out")], tried_providers=[100])
        st["discovery"].append({"ts": "2026-10-13T18:47:00Z", "candidates": [], "chosen": None, "provider": 100,
                                "reason": "deadline passed"})
        st["discovery"].append({"ts": "2026-10-13T20:47:00Z", "candidates": [101], "chosen": None,
                                "reason": "no offer fits", "ranking": [{"entry_id": 101, "fit": 1}]})
        md = render(st).markdown()
        self.assertIn("**Status: documented attempts.**", md)
        self.assertIn("timed out", md)
        self.assertIn("2 ranking(s) over 2 offers read; 1 request(s) timed out.", md)
        self.assertIn("no offer fits", md)

    def test_jobs_served_are_listed(self):
        job = {"request_entry_id": 960, "author": "user 9", "received_at": "2026-10-13T10:00:00Z",
               "status": "delivered", "result_entry_id": 961, "checks": {"events_ok": 12}}
        fs = {"posts": FORUM["posts"] + [{"entry_id": 961, "parent_id": 960, "posted_at": "2026-10-13T12:00:00Z"}]}
        md = render(state(request=request("sent"), jobs=[job]), fs=fs).markdown()
        self.assertIn("events_ok 12", md)
        self.assertIn("reply to user 9's request 960", md)

    def test_peer_text_cannot_inject_links(self):
        st = state(request=request("sent"))
        st["offers_seen"][0]["capability"] = "tests [click](https://evil.example) <script>x</script> **x**"
        st["discovery"][0]["reason"] = "pick me `now` [here](javascript:alert(1))"
        md = render(st).markdown()
        self.assertNotIn("](https://evil.example)", md)
        self.assertNotIn("](javascript:", md)

    def test_table_cells_escape_pipes(self):
        st = state(request=request("sent"))
        st["offers_seen"][0]["capability"] = "a | b"
        self.assertIn("a \\| b", render(st).markdown())

    def test_main_writes_both_files_from_an_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            self.assertEqual(hw4_writeup.main(["--state", os.path.join(tmp, "missing"), "--out", out]), 0)
            with open(os.path.join(out, "hw4-writeup.md"), encoding="utf-8") as fh:
                md = fh.read()
            self.assertIn("**Status: documented attempts.**", md)

    def test_main_reads_the_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "memory"))
            os.makedirs(os.path.join(tmp, "logs"))
            with open(os.path.join(tmp, "memory", "hw4-state.json"), "w") as fh:
                json.dump(state(request=request("sent")), fh)
            with open(os.path.join(tmp, "memory", "forum-state.json"), "w") as fh:
                json.dump(FORUM, fh)
            with open(os.path.join(tmp, "logs", "hw4.jsonl"), "w") as fh:
                fh.write(json.dumps(CYCLE) + "\nnot json\n")
            open(os.path.join(tmp, "memory", "forum-halt"), "w").close()
            hw4_writeup.main(["--state", tmp, "--out", tmp])
            with open(os.path.join(tmp, "hw4-writeup.md"), encoding="utf-8") as fh:
                md = fh.read()
            self.assertIn("1 cycles, 0 posts, 0 cycle errors (none), halt file present", md)


if __name__ == "__main__":
    unittest.main()
