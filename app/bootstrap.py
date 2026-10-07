"""First-run setup and dependency checks for the launcher. Standard library only: this runs before
any requirement is installed.

- Started by any Python 3.11+ outside the project environment: create `.venv` if needed, install
  `requirements.txt` into it with a progress window, then restart the launcher inside `.venv`.
- Started inside `.venv` (the usual case, e.g. from the shortcut): compare installed packages with
  `requirements.txt` and install whatever is missing or at the wrong version before the app starts.
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
SETUP_LOG = ROOT / "logs" / "setup.log"
MIN_PYTHON = (3, 11)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashes from pythonw


def venv_python(gui: bool = False) -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / ("pythonw.exe" if gui else "python.exe")
    return VENV / "bin" / "python"


def in_project_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def requirements(path: Path = REQUIREMENTS) -> list[tuple[str, str | None]]:
    """(name, pinned version or None) for each line of requirements.txt."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        out.append((name.strip(), version.strip() if sep else None))
    return out


def missing_requirements(path: Path = REQUIREMENTS) -> list[str]:
    """Requirements not installed in this interpreter, or installed at a different pinned version."""
    missing = []
    for name, version in requirements(path):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        if version and installed != version:
            missing.append(f"{name} (have {installed}, need {version})")
    return missing


def _console_python() -> str:
    """python.exe rather than pythonw.exe, so tools like pip and venv behave normally."""
    exe = Path(sys.executable)
    candidate = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    return str(candidate if candidate.exists() else exe)


def _venv_healthy() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    try:
        return subprocess.run([str(py), "-c", "import sys"], timeout=30, creationflags=NO_WINDOW,
                              capture_output=True).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def alert(message: str, error: bool = True):
    SETUP_LOG.parent.mkdir(exist_ok=True)
    with open(SETUP_LOG, "a", encoding="utf-8") as f:
        f.write(message + "\n")
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Proof-Carrying Data Analyst", 0x10 if error else 0x40)
    else:
        print(message, file=sys.stderr)


def run_steps(steps: list[tuple[str, list[str]]], note: str) -> bool:
    """Run setup commands, showing a small progress window. Output goes to logs/setup.log."""
    SETUP_LOG.parent.mkdir(exist_ok=True)
    events: queue.Queue = queue.Queue()
    result = {"ok": False, "tail": []}

    def work():
        with open(SETUP_LOG, "a", encoding="utf-8") as log:
            for title, cmd in steps:
                events.put(("title", title))
                log.write(f"\n$ {' '.join(cmd)}\n")
                proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                for line in proc.stdout:
                    log.write(line)
                    line = line.strip()
                    if line:
                        result["tail"] = (result["tail"] + [line])[-12:]
                        events.put(("detail", line))
                if proc.wait() != 0:
                    events.put(("done", False))
                    return
        result["ok"] = True
        events.put(("done", True))

    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:  # no GUI toolkit: run quietly, then report
        work()
        return result["ok"] or _report_failure(result["tail"])

    root = tk.Tk()
    root.title("Proof-Carrying Data Analyst · setting up")
    root.geometry("560x190")
    root.resizable(False, False)
    root.configure(bg="#141413")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Bar.Horizontal.TProgressbar", troughcolor="#1e1e1c", background="#7cc49a", bordercolor="#141413")
    title = tk.Label(root, text="Preparing…", bg="#141413", fg="#ecebe6", font=("Segoe UI", 13, "bold"), anchor="w")
    title.pack(fill="x", padx=24, pady=(22, 4))
    tk.Label(root, text=note, bg="#141413", fg="#8e8d86", font=("Segoe UI", 9), anchor="w", justify="left",
             wraplength=510).pack(fill="x", padx=24)
    bar = ttk.Progressbar(root, mode="indeterminate", style="Bar.Horizontal.TProgressbar", length=510)
    bar.pack(padx=24, pady=(14, 8))
    bar.start(12)
    detail = tk.Label(root, text="", bg="#141413", fg="#b4b3ac", font=("Consolas", 9), anchor="w")
    detail.pack(fill="x", padx=24)

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "title":
                    title.config(text=value)
                elif kind == "detail":
                    detail.config(text=value[:84])
                elif kind == "done":
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(100, poll)

    threading.Thread(target=work, daemon=True).start()
    root.after(100, poll)
    root.protocol("WM_DELETE_WINDOW", lambda: None)  # finishing setup cleanly matters more than closing early
    root.mainloop()
    return result["ok"] or _report_failure(result["tail"])


def _report_failure(tail: list[str]) -> bool:
    alert("Setup could not finish.\n\n" + "\n".join(tail[-8:]) +
          f"\n\nThe full output is in {SETUP_LOG}. Check the internet connection and try again.")
    return False


def ensure_environment(argv: list[str]) -> bool:
    """True when the app can start in this process; False when it must not (setup failed, or the
    launcher was restarted inside the project environment)."""
    if not in_project_venv():
        if sys.version_info < MIN_PYTHON:
            alert(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required; this is "
                  f"{sys.version_info.major}.{sys.version_info.minor}. Install it from https://www.python.org/downloads/")
            return False
        fresh = not _venv_healthy()
        if fresh:
            if VENV.exists():
                shutil.rmtree(VENV, ignore_errors=True)  # a broken or half-created environment
            ok = run_steps([
                ("Creating the project environment", [_console_python(), "-m", "venv", str(VENV)]),
                ("Installing the analysis libraries", [str(venv_python()), "-m", "pip", "install",
                                                       "--disable-pip-version-check", "-r", str(REQUIREMENTS)]),
            ], "First start only: setting up Python libraries (pandas, DuckDB, …) for this app. "
               "This downloads about 150 MB and takes a few minutes.")
            if not ok:
                return False
        subprocess.Popen([str(venv_python(gui=True)), str(ROOT / "launch.pyw")] + (["--first-run"] if fresh else []),
                         cwd=ROOT, creationflags=NO_WINDOW)
        return False

    missing = missing_requirements()
    if missing:
        ok = run_steps([("Installing missing libraries", [_console_python(), "-m", "pip", "install",
                                                          "--disable-pip-version-check", "-r", str(REQUIREMENTS)])],
                       "Updating: " + ", ".join(missing[:6]) + ("…" if len(missing) > 6 else ""))
        if not ok:
            return False
    if "--first-run" in argv and os.name == "nt" and not os.environ.get("PCDA_NO_SHORTCUT"):
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from create_shortcut import create_shortcuts
            create_shortcuts(venv_python(gui=True), only_missing=True)
        except Exception as e:  # a missing shortcut is not a reason to stop the app
            alert(f"The app is ready, but the Desktop shortcut could not be created ({e}).", error=False)
    return True
