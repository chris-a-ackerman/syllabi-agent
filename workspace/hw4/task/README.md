# workspace/hw4/task/: the job the agent delegates

The HW4 driver (`scripts/hw4-cycle.py`) hires another agent for one job. The job is a package in
this directory, chosen with the env variable `HW4_TASK` (default `control_state_tests`). The
driver never knows what the job is: it reads the request and the need from the package, and
calls the package's `verify.py` to check what comes back.

    workspace/hw4/task/<name>/
        request.md   the request text. {files} becomes the files/ below (indented four spaces so
                     it reads as code), {deadline} the deadline in Eastern time. Keep it under
                     2,000 characters after rendering, and say how you will check the result.
        need.txt     one line: the capability a provider must have. The classifier and the
                     ranking prompt read it ("I need an agent that <need.txt>").
        files/       what goes into the request (one file: just its code; several: each after a
                     "# file: <name>" line).
        verify.py    the checks, plus any private data they need (control_state_tests keeps its
                     two mutants in mutants/). It must define
                         verify(result_code: str, workdir: str, runner=None) ->
                             {"ok": bool, "checks": {...}, "summary": str, "lines": [str]}
                     and may define
                         adopt(result_code: str, header_lines: [str]) -> (path, text)
                     to turn an accepted result into tests/test_hw4_delegated_<something>.py (the
                     only path the driver will write). `runner(files, workdir)` runs
                     `python -m unittest -v` over {file name: text}; it defaults to
                     sandbox.run_unittest, and tests pass a stub. verify.py may import the
                     generic helpers next to the driver: `checks` (extract_code, static_check
                     with your allow-list) and `sandbox` (run_unittest, passed, failed, counts).

The driver does the rest, whatever the job: it finds the result in the request thread, extracts
the code, saves it to `state/work/hw4/result-<entry_id>.py`, calls `verify()`, and posts the
verdict: accepted (with `lines` and the tip), one correction request (with `summary` and
`checks.output_excerpt`, at most 600 characters), or rejected.

## Adding a job

1. Copy `control_state_tests/` to a new directory with a lower-case name (`[a-z0-9_]`).
2. Write `request.md`, `need.txt` and `files/`, and change `verify.py`'s checks and `adopt()`.
3. Add tests for the new `verify()` (see `tests/test_hw4_verify.py`), with a stub runner.
4. In GitHub, set the repository variable `HW4_TASK` to the directory name (Settings → Secrets
   and variables → Actions → Variables). The next cycle that has no open request sends it.

Switching `HW4_TASK` is safe at any time: the open request records its own `task` in
`hw4-state.json` and its result is checked by that package; the new job goes out with the next
request.
