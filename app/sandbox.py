"""Run untrusted analysis code in isolation and return a structured result.

Layers (all providers):
1. Static policy (security.check_code): import allowlist, no eval/exec/open/dunder access.
2. Ephemeral working directory holding read-only copies of only the tables the plan needs.
3. Fresh interpreter in isolated mode (-I) with an empty environment: no secrets to read.
4. Audit hook installed before the code runs: blocks process creation, sockets, ctypes,
   file writes, and file reads outside the working directory and the Python installation.
5. Resource limits: wall-clock timeout; memory and process-count limits
   (Windows job object / POSIX rlimits / Docker cgroups).
The docker provider adds: no network, read-only root filesystem, dropped capabilities,
non-root user, pids limit.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .security import check_code

OUTPUT_CAP = 64_000
VIOLATION = "SANDBOX_VIOLATION"

GUARD = r'''
import io, json, os, sys
if len(sys.argv) > 2:  # library paths of the parent interpreter (-I would otherwise hide user site-packages)
    sys.path[:] = json.loads(sys.argv.pop(2))
import collections, datetime, decimal, fractions, functools, itertools, json, math, operator, re, statistics
import numpy, pandas
try:
    import duckdb
except ImportError:
    pass
pandas.read_csv(io.StringIO("a,b\n1,2"), dtype=str).merge(pandas.DataFrame({"a": ["1"]}), on="a")  # pre-load lazy modules
sys.stdout.reconfigure(encoding="utf-8")

WORK = os.path.realpath(os.getcwd())
ROOTS = {sys.prefix, sys.base_prefix, sys.exec_prefix, *[p for p in sys.path if p]}
READ_OK = tuple(os.path.join(os.path.realpath(p), "") for p in ROOTS) + (os.path.join(WORK, ""),)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
BLOCKED = ("subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.kill",
           "os.putenv", "os.unsetenv", "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod",
           "os.symlink", "os.link", "os.startfile", "os.chdir", "os.truncate", "shutil.", "socket.",
           "ctypes.", "winreg.", "urllib.", "http.", "ftplib.", "smtplib.", "webbrowser.", "sqlite3.",
           "builtins.input", "pdb.")


def deny(what):
    sys.__stderr__.write(f"SANDBOX_VIOLATION {what}\n")
    sys.__stderr__.flush()
    raise PermissionError(f"blocked by sandbox: {what}")


def hook(event, args):
    if event.startswith(BLOCKED):
        deny(event)
    if event == "open":
        path, mode, flags = (list(args) + [None, None])[:3]
        if path is None or isinstance(path, int):
            return
        p = os.path.realpath(os.fsdecode(path))
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & WRITE_FLAGS)
        if writing or not (p.startswith(READ_OK) or p == WORK):
            deny(f"open {p}")


src = open(sys.argv[1], encoding="utf-8").read()
code = compile(src, "proof.py", "exec")
sys.addaudithook(hook)
exec(code, {"__name__": "__main__"})
'''


@dataclass
class ExecutionResult:
    status: str  # ok | invalid_output | runtime_error | timeout | security_violation | policy_rejected | sandbox_error
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    result: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def parse_result(stdout: str) -> tuple[Any, str | None]:
    lines = [ln[len("RESULT:"):].strip() for ln in stdout.splitlines() if ln.startswith("RESULT:")]
    if len(lines) != 1:
        return None, f"expected exactly one 'RESULT:' line, found {len(lines)}"
    try:
        return json.loads(lines[0]), None
    except json.JSONDecodeError as e:
        return None, f"RESULT is not valid JSON ({e.msg})"


def _rmtree(path: Path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=onerror) if sys.version_info >= (3, 12) else shutil.rmtree(path, onerror=onerror)


class Sandbox:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def run(self, code: str, data_dir: Path, tables: list[str], trusted: bool = False) -> ExecutionResult:
        """Execute `code` with read-only access to `tables`. `trusted` skips only the static policy
        (used for the verifier's own DuckDB check); every other layer still applies."""
        if not trusted and (problems := check_code(code)):
            return ExecutionResult("policy_rejected", error="; ".join(problems))
        work = Path(tempfile.mkdtemp(prefix="pcda_run_"))
        try:
            (work / "data").mkdir()
            for t in tables:
                src = Path(data_dir) / f"{t}.csv"
                if not src.exists():
                    return ExecutionResult("sandbox_error", error=f"table file missing: {t}.csv")
                shutil.copyfile(src, work / "data" / f"{t}.csv")
                os.chmod(work / "data" / f"{t}.csv", stat.S_IREAD)
            (work / "proof.py").write_text(code, encoding="utf-8")
            (work / "_guard.py").write_text(GUARD, encoding="utf-8")
            if self.cfg.sandbox == "docker":
                return self._run_docker(work)
            return self._run_local(work)
        finally:
            _rmtree(work)

    def _finish(self, rc, out, err, dt, timed_out) -> ExecutionResult:
        out, err = out[:OUTPUT_CAP], err[:OUTPUT_CAP]
        r = ExecutionResult("ok", rc, out, err, round(dt, 3), timed_out)
        if timed_out:
            r.status, r.error = "timeout", f"execution exceeded {self.cfg.timeout_s}s"
        elif VIOLATION in err:
            r.status = "security_violation"
            r.error = next(ln for ln in err.splitlines() if VIOLATION in ln)
        elif rc != 0:
            r.status = "runtime_error"
            r.error = (err.strip().splitlines() or [f"exit code {rc}"])[-1]
        else:
            r.result, r.error = parse_result(out)
            if r.error:
                r.status = "invalid_output"
        return r

    def _run_local(self, work: Path) -> ExecutionResult:
        env = {"PATH": os.path.dirname(sys.executable)}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        kwargs = {}
        if os.name == "posix":
            kwargs["preexec_fn"] = self._posix_limits
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen([sys.executable, "-I", "_guard.py", "proof.py", json.dumps(_library_paths())],
                                    cwd=work, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", **kwargs)
        except OSError as e:
            return ExecutionResult("sandbox_error", error=f"could not start interpreter: {e}")
        try:
            job = _windows_job(proc, self.cfg.memory_mb) if os.name == "nt" else None
        except RuntimeError as e:
            return ExecutionResult("sandbox_error", error=str(e))
        try:
            out, err = proc.communicate(timeout=self.cfg.timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            timed_out = True
        finally:
            if job:
                job.close()
        return self._finish(proc.returncode, out or "", err or "", time.monotonic() - t0, timed_out)

    def _posix_limits(self):
        import resource
        mem = self.cfg.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        cpu = int(self.cfg.timeout_s) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

    def _run_docker(self, work: Path) -> ExecutionResult:
        name = f"pcda-{uuid.uuid4().hex[:12]}"
        cmd = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
               "--memory", f"{self.cfg.memory_mb}m", "--cpus", "1", "--user", "65534:65534",
               "--tmpfs", "/tmp:size=16m", "-v", f"{work}:/work:ro", "-w", "/work",
               self.cfg.docker_image, "python", "-I", "_guard.py", "proof.py"]
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=self.cfg.timeout_s + 15)  # container start-up allowance
            return self._finish(p.returncode, p.stdout, p.stderr, time.monotonic() - t0, False)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return self._finish(None, "", "", time.monotonic() - t0, True)
        except FileNotFoundError:
            return ExecutionResult("sandbox_error", error="docker is not installed")


def _library_paths() -> list[str]:
    """The parent's import path minus the project and working directory, so the sandbox sees the same
    installed packages (including user site-packages) but none of this application's code."""
    project = os.path.realpath(Path(__file__).resolve().parent.parent)
    out = []
    for p in sys.path:
        rp = os.path.realpath(p) if p else ""
        if rp and os.path.isdir(rp) and rp not in (project, os.path.realpath(os.getcwd())):
            out.append(rp)
    return out


class _Job:
    def __init__(self, k32, handle):
        self.k32, self.handle = k32, handle

    def close(self):
        self.k32.CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE terminates anything left


def _windows_job(proc: subprocess.Popen, memory_mb: int) -> _Job | None:
    """Memory cap + single-process limit via a job object.
    ponytail: assigned just after start; a few ms race before limits apply. Use the docker provider
    when that window matters."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.Basic.LimitFlags = 0x100 | 0x2000 | 0x8  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE | ACTIVE_PROCESS
    # a venv python.exe on Windows is a launcher that starts the base interpreter as its child
    info.Basic.ActiveProcessLimit = 1 if sys.executable == getattr(sys, "_base_executable", sys.executable) else 2
    info.ProcessMemoryLimit = memory_mb * 1024 * 1024
    ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    ok = ok and k32.AssignProcessToJobObject(job, int(proc._handle))
    if not ok:
        proc.kill()
        raise RuntimeError(f"could not apply sandbox resource limits (error {ctypes.get_last_error()})")
    return _Job(k32, job)
