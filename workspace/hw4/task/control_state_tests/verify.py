"""verify: the checks for the control_state_tests job (SYL-115, HW4-4).

The delegated task: unit tests for files/control_state.py. A delivered test file is accepted when
    static   it parses, has a unittest.TestCase subclass with >= 3 test methods, and imports only
             ALLOWED (no file, network, subprocess or import-machinery use);
    correct  every test passes against files/control_state.py;
    m1       at least one test fails against mutants/m1_paused_is_running.py;
    m2       at least one test fails against mutants/m2_no_normalization.py.
Each run puts the tests (as test_delivered.py) next to one implementation (as control_state.py) in
the sandbox (workspace/hw4/sandbox.py). The driver (scripts/hw4-cycle.py) only calls verify() and
adopt(); it knows nothing about control_state.
"""
import os
import re

import checks
import sandbox

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED = {"unittest", "re", "typing", "string", "textwrap", "itertools", "functools", "control_state"}
TEST_FILE = "test_delivered.py"
IMPL = "control_state.py"
RUNS = (  # (check, implementation file, must pass?, how the outcome line names it)
    ("correct", os.path.join("files", "control_state.py"), True, "the real function"),
    ("m1", os.path.join("mutants", "m1_paused_is_running.py"), False, "mutant 1 (the PAUSED line reported as RUNNING)"),
    ("m2", os.path.join("mutants", "m2_no_normalization.py"), False,
     "mutant 2 (no whitespace stripping or upper-casing)"),
)
EXCERPT_CAP = 600
ADOPT_PATH = os.path.join("tests", "test_hw4_delegated_control_state.py")


def _read(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
        return fh.read()


def _excerpt(output):
    """The part of unittest's output that says what failed, at most EXCERPT_CAP characters."""
    out = (output or "").strip()
    m = re.search(r"^(FAIL|ERROR): ", out, re.M)
    return out[m.start():][:EXCERPT_CAP] if m else out[-EXCERPT_CAP:]


def verify(result_code, workdir, runner=None):
    """{"ok", "checks": {static, correct, m1, m2, output_excerpt}, "summary", "lines"}. `runner(files,
    workdir)` defaults to sandbox.run_unittest; tests pass a stub."""
    runner = runner or sandbox.run_unittest
    result = {"static": None, "correct": "skipped", "m1": "skipped", "m2": "skipped", "output_excerpt": ""}
    problem = checks.static_check(result_code, ALLOWED, min_tests=3)
    if problem:
        result.update(static="fail", output_excerpt=("static check: " + problem)[:EXCERPT_CAP])
        return {"ok": False, "checks": result, "lines": [],
                "summary": "check (4) failed before anything ran: %s." % problem}
    result["static"] = "pass"
    lines, failures = [], []
    for name, rel, must_pass, label in RUNS:
        run = runner({TEST_FILE: result_code, IMPL: _read(rel)}, workdir)
        ran, bad = sandbox.counts(run)
        if must_pass:
            ok = sandbox.passed(run)
            line = ("Passed on %s (%d tests)." % (label, ran) if ok else
                    "Did not pass on %s (%s)." % (label, "timed out" if run.get("timed_out")
                                                  else "%d of %d tests failed or errored" % (bad, ran)))
        else:
            ok = sandbox.failed(run)
            line = ("Failed on %s: %d of %d tests caught it." % (label, bad, ran) if ok else
                    "Did not catch %s: %s." % (label, "the run timed out" if run.get("timed_out")
                                               else "every test passed against it"))
        result[name] = "pass" if ok else "fail"
        lines.append(line)
        if not ok:
            failures.append(line)
            if not result["output_excerpt"]:
                result["output_excerpt"] = _excerpt(run.get("output"))
    ok = not failures
    summary = " ".join(lines) if ok else " ".join(failures)
    return {"ok": ok, "checks": result, "lines": lines, "summary": summary}


def adopt(result_code, header_lines):
    """The accepted tests as a file in this repo's suite: (repo-relative path, text). The delivered
    `from control_state import control_state` keeps working because files/ goes on sys.path first."""
    header = "".join("# %s\n" % line for line in header_lines)
    path_fix = (
        "import os as _os\n"
        "import sys as _sys\n"
        "_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), \"..\", \"workspace\", "
        "\"hw4\", \"task\", \"control_state_tests\", \"files\"))\n")
    return ADOPT_PATH, header + path_fix + "\n" + result_code.strip("\n") + "\n"
