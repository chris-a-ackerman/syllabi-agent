"""sandbox: run delivered unit tests against one implementation, isolated (SYL-115, HW4-4).

    run_unittest(files, workdir, env=None) -> {"exit": int, "output": str, "timed_out": bool, "mode": str}

`files` maps a file name (no directories) to its text, e.g. {"test_delivered.py": ..., "control_state.py":
...}. They are written to a fresh directory under `workdir`, and `python -m unittest -v` runs there:

    docker (default): docker run --rm --network none --memory 512m --pids-limit 128 --cpus 1 --read-only
        --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --user nobody:nogroup
        -v <dir>:/work:ro -w /work python:3.12-slim timeout 60 python -m unittest -v
    subprocess (only when HW4_SANDBOX=subprocess is set explicitly): the same command with this
        Python, timeout=60, cwd=<dir>, no network isolation. For local runs without docker.

The child process never inherits os.environ: its env is built from scratch ({"PATH": SAFE_PATH} plus
PYTHONDONTWRITEBYTECODE), so CANVAS_FORUM_TOKEN and ANTHROPIC_API_KEY, which the cycle step has, never
reach the tests. The docker CLI gets the same env and passes nothing into the container.

Standard library only, Python 3.10+.
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

IMAGE = "python:3.12-slim"
TEST_TIMEOUT = 60               # seconds, inside the container (coreutils `timeout`)
DOCKER_TIMEOUT = 240            # the whole `docker run`, image start included (the workflow pulls first)
OUTPUT_CAP = 64 * 1024
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\.py$")


class SandboxError(Exception):
    pass


def child_env(path=SAFE_PATH):
    """The only environment a sandbox child gets: built from scratch, never from os.environ."""
    return {"PATH": path, "PYTHONDONTWRITEBYTECODE": "1"}


def mode(env=None):
    env = os.environ if env is None else env
    return "subprocess" if (env.get("HW4_SANDBOX") or "").strip() == "subprocess" else "docker"


def docker_argv(directory, name):
    return ["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "512m", "--pids-limit", "128",
            "--cpus", "1", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "nobody:nogroup", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", "%s:/work:ro" % directory, "-w", "/work", IMAGE,
            "timeout", str(TEST_TIMEOUT), "python", "-m", "unittest", "-v"]


def _write(directory, files):
    for name, text in files.items():
        if not _NAME.match(name):
            raise SandboxError("bad sandbox file name: %r" % name[:40])
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o644)
    os.chmod(directory, 0o755)     # the container runs as nobody


def _text(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return (raw or "")[-OUTPUT_CAP:]


def run_unittest(files, workdir, env=None):
    """Run `python -m unittest -v` over `files` in a fresh directory; see the module docstring."""
    how = mode(env)
    os.makedirs(workdir, exist_ok=True)
    directory = tempfile.mkdtemp(prefix="sandbox-", dir=workdir)
    try:
        _write(directory, files)
        if how == "docker":
            docker = shutil.which("docker", path=SAFE_PATH)
            if docker is None:
                raise SandboxError("docker is not installed (set HW4_SANDBOX=subprocess for a local run)")
            name = "hw4-sandbox-" + secrets.token_hex(6)
            argv = [docker] + docker_argv(os.path.realpath(directory), name)[1:]
            timeout = DOCKER_TIMEOUT
        else:
            argv = [sys.executable, "-E", "-s", "-m", "unittest", "-v"]
            timeout = TEST_TIMEOUT
        try:
            proc = subprocess.run(argv, cwd=directory, env=child_env(), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            if how == "docker":
                subprocess.run([argv[0], "kill", name], env=child_env(), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30, check=False)
            return {"exit": 124, "output": _text(e.output) + "\n[timed out after %d s]" % timeout,
                    "timed_out": True, "mode": how}
        out = _text(proc.stdout)
        # `timeout` exits 124 when the tests ran past TEST_TIMEOUT inside the container.
        return {"exit": proc.returncode, "output": out, "timed_out": proc.returncode == 124, "mode": how}
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def passed(result):
    """Every test passed: exit 0 and unittest's closing OK."""
    return result.get("exit") == 0 and re.search(r"^OK\b", result.get("output") or "", re.M) is not None


def failed(result):
    """At least one test failed or errored (a crash or a time-out is not a failing test)."""
    out = result.get("output") or ""
    return (result.get("exit") not in (0, None) and not result.get("timed_out")
            and re.search(r"^(FAIL|ERROR): ", out, re.M) is not None)


def counts(result):
    """(tests run, failures + errors) from unittest's summary lines."""
    out = result.get("output") or ""
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    bad = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", out))
    return (int(ran.group(1)) if ran else 0), bad
