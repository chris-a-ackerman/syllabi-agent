"""Generic checks on delivered Python code (SYL-115, HW4-4): extraction and static checks.

Nothing here knows about a particular job: a task package's verify.py (workspace/hw4/task/<name>/)
calls these with its own allow-list. Nothing is executed here; that is sandbox.py's job.

    extract_code(text)                         -> str or None
    static_check(code, allowed, min_tests=3)   -> None when it passes, else the reason (one line)

Standard library only, Python 3.10+.
"""
import ast
import re
import textwrap

# Module roots that are never allowed, whatever a task's allow-list says.
FORBIDDEN_MODULES = {"os", "sys", "subprocess", "socket", "urllib", "http", "pathlib", "shutil", "importlib",
                     "builtins", "ctypes", "multiprocessing", "threading", "signal", "pickle", "marshal", "io",
                     "tempfile", "glob", "asyncio", "requests"}
# Names that open a way out of a test (files, code from strings, the import machinery).
FORBIDDEN_NAMES = {"eval", "exec", "open", "__import__", "compile", "breakpoint", "globals", "locals",
                   "__builtins__", "__loader__", "__spec__"}
# Module names that may not appear even as a bare name (a module smuggled in some other way).
NAMED_MODULES = {"os", "sys", "subprocess", "socket", "urllib", "http", "pathlib", "shutil", "importlib", "builtins"}
FORBIDDEN_ATTRS = {"__subclasses__", "__globals__", "__builtins__", "__code__", "__bases__", "__mro__",
                   "__dict__", "__getattribute__", "__import__", "f_globals", "f_locals", "gi_frame", "tb_frame"}

_FENCE = re.compile(r"^[ \t]*(```+|~~~+)[ \t]*([\w+-]*)[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.M | re.S)
_CODE_START = re.compile(r"^(import |from |class |def |@|if __name__)")


def _parses(code):
    try:
        ast.parse(code)
        return True
    except (SyntaxError, ValueError):
        return False


def _fenced(text):
    blocks = [m.group(3) for m in _FENCE.finditer(text) if m.group(2).lower() in ("", "py", "python", "python3")]
    blocks = [textwrap.dedent(b).strip("\n") for b in blocks if b.strip()]
    if not blocks:
        return None
    joined = "\n\n".join(blocks)
    if _parses(joined):
        return joined
    parsing = [b for b in blocks if _parses(b)]
    return max(parsing or blocks, key=len)


def _indented(text):
    """The longest run of lines indented by 4+ spaces (blank lines allowed inside), dedented."""
    best, run = [], []
    for line in text.splitlines() + ["END"]:
        if line.startswith("    ") or line.startswith("\t") or (run and not line.strip()):
            run.append(line)
            continue
        while run and not run[-1].strip():
            run.pop()
        if len(run) > len(best):
            best = run
        run = []
    code = textwrap.dedent("\n".join(best)).strip("\n")
    return code if len(best) >= 3 and _parses(code) else None


def _bare(text):
    """The longest run of lines that parses, starting at a line that looks like code."""
    lines = text.splitlines()[:600]
    best = ""
    for i, line in enumerate(lines):
        if not _CODE_START.match(line):
            continue
        for j in range(len(lines), i, -1):
            if j - i <= best.count("\n") + 1:
                break
            chunk = "\n".join(lines[i:j]).strip("\n")
            if _parses(chunk):
                best = chunk
                break
    return best or None


def extract_code(text):
    """Python code from a forum entry's text: fenced blocks first, else indented blocks, else the
    longest run of lines that parses. None when there is nothing that looks like code."""
    text = str(text or "").replace("\r\n", "\n")
    code = _fenced(text)
    if not (code and code.strip()):
        # Bare code has indented class bodies too, so the longer of the two wins.
        found = [c for c in (_indented(text), _bare(text)) if c and c.strip()]
        code = max(found, key=lambda c: c.count("\n")) if found else None
    return code.strip("\n") + "\n" if code else None


def _root(name):
    return (name or "").split(".")[0]


def _is_testcase_base(base, testcase_names):
    if isinstance(base, ast.Attribute):
        return base.attr == "TestCase" and isinstance(base.value, ast.Name) and base.value.id == "unittest"
    return isinstance(base, ast.Name) and base.id in testcase_names


def static_check(code, allowed, min_tests=3):
    """None when `code` passes, else the first reason it fails. `allowed` is the set of importable
    module roots (e.g. {"unittest", "re", "control_state"}); FORBIDDEN_MODULES win over it."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as e:
        return "the code does not parse (%s, line %s)" % (type(e).__name__, getattr(e, "lineno", "?"))
    testcase_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                return "relative import (from %s%s import ...)" % ("." * node.level, node.module or "")
            names = [node.module or ""]
            if node.module == "unittest":
                testcase_names |= {a.asname or a.name for a in node.names if a.name == "TestCase"}
        else:
            names = []
        for name in names:
            if _root(name) in FORBIDDEN_MODULES or _root(name) not in allowed:
                return "imports %s, which is not on the allow-list (%s)" % (name, ", ".join(sorted(allowed)))
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id in NAMED_MODULES):
            return "uses %s" % node.id
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            return "uses .%s" % node.attr
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = next((w for w in sorted(FORBIDDEN_ATTRS | {"__import__"}) if w in node.value), None)
            if hit:
                return "mentions %s" % hit
    best = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_is_testcase_base(b, testcase_names) for b in node.bases):
            tests = [f for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and f.name.startswith("test")]
            best = max(best, len(tests))
    if best < min_tests:
        return ("no unittest.TestCase subclass with at least %d test methods (the most in one class: %d)"
                % (min_tests, best))
    return None
