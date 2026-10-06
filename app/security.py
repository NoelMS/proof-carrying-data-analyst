"""Static policy for untrusted analytical code, and prompt-injection handling for data.

The static policy is a first filter only. The sandbox (process isolation, audit
hooks, stripped environment, resource limits, optional Docker with no network)
is the security boundary.
"""
import ast
import re

ALLOWED_IMPORTS = {"pandas", "numpy", "decimal", "json", "math", "statistics", "datetime",
                   "collections", "itertools", "functools", "re", "fractions", "operator"}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals",
                   "locals", "vars", "getattr", "setattr", "delattr", "memoryview", "exit", "quit", "help"}
FORBIDDEN_ATTRS = {"system", "popen", "ctypeslib", "load", "save", "savez", "fromfile", "tofile", "memmap",
                   "loadtxt", "genfromtxt", "savetxt", "DataSource", "environ", "getenv"}
ALLOWED_READERS = {"read_csv"}
ALLOWED_TO = {"to_datetime", "to_numeric", "to_dict", "to_list", "to_period", "to_timestamp", "to_json"}
FORBIDDEN_KWARGS = {"engine", "storage_options"}

INJECTION_RE = re.compile(
    r"(ignore|disregard|forget|override)\b.{0,40}\b(instruction|prompt|rule)s?"
    r"|system prompt|you are now|reveal\b.{0,30}\b(secret|prompt|key)|api[_ ]?key", re.I)


class PolicyViolation(Exception):
    pass


def check_code(code: str) -> list[str]:
    """Return policy violations in `code` (empty list = allowed)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error: {e.msg} (line {e.lineno})"]
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            problems += [f"import of '{m}' is not allowed" for m in mods if m.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a.startswith("__") or a in FORBIDDEN_ATTRS:
                problems.append(f"attribute '{a}' is not allowed")
            elif (a.startswith("read_") and a not in ALLOWED_READERS) or (a.startswith("to_") and a not in ALLOWED_TO):
                problems.append(f"I/O method '{a}' is not allowed")
        elif isinstance(node, ast.keyword) and node.arg in FORBIDDEN_KWARGS:
            problems.append(f"keyword '{node.arg}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            problems.append(f"{type(node).__name__} is not allowed")
    return sorted(set(problems))


def is_instruction_like(value: str) -> bool:
    return bool(INJECTION_RE.search(value))


def sanitize_for_prompt(value: str, limit: int = 60) -> str:
    """Data values shown to a model: instruction-like text withheld, length capped."""
    if is_instruction_like(value):
        return "[instruction-like text withheld]"
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return value if len(value) <= limit else value[:limit] + "..."
