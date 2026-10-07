"""Install and start the local agent model (Ollama). Standard library only.

Nothing here runs on its own: installing is always started by the user from the app. Ollama itself is
installed only through the platform's package manager (winget, Homebrew); otherwise the user is told
where to get it. No remote script is ever run.
"""
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .local_model import ollama_status

DOWNLOAD_URL = "https://ollama.com/download"
Progress = Callable[[str, float | None], None]


class SetupError(Exception):
    pass


def ollama_exe() -> str | None:
    """The Ollama executable, also where installers put it before PATH is refreshed."""
    if found := shutil.which("ollama"):
        return found
    for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
              Path("/Applications/Ollama.app/Contents/Resources/ollama"), Path("/usr/local/bin/ollama")):
        if p.is_file():
            return str(p)
    return None


def start_server(url: str, wait_s: float = 20.0) -> bool:
    """Start `ollama serve` in the background if Ollama is installed and not already running."""
    if ollama_status("", url)["reachable"]:
        return True
    exe = ollama_exe()
    if not exe:
        return False
    nt = os.name == "nt"
    subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if nt else 0,
                     start_new_session=not nt)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if ollama_status("", url)["reachable"]:
            return True
        time.sleep(0.5)
    return False


def manual_instructions() -> str:
    extra = " On Linux: curl -fsSL https://ollama.com/install.sh | sh" if platform.system() == "Linux" else ""
    return f"Install Ollama from {DOWNLOAD_URL}, then press Install again.{extra}"


def install_ollama(progress: Progress):
    system = platform.system()
    if system == "Windows" and (winget := shutil.which("winget")):
        cmd = [winget, "install", "--id", "Ollama.Ollama", "-e", "--silent",
               "--accept-source-agreements", "--accept-package-agreements"]
    elif system == "Darwin" and (brew := shutil.which("brew")):
        cmd = [brew, "install", "ollama"]
    else:
        raise SetupError("Ollama is not installed and no supported package manager was found. " + manual_instructions())
    progress("Installing Ollama (the program that runs the model)…", None)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    tail = []
    for line in proc.stdout:
        if line := line.strip(" \r\n\t-\\|/"):
            tail = (tail + [line])[-5:]
    if proc.wait() != 0 and not ollama_exe():
        raise SetupError(f"Installing Ollama failed ({' / '.join(tail[-2:])}). " + manual_instructions())


def pull(model: str, url: str, progress: Progress):
    """Download the model through Ollama's API, reporting the download percentage."""
    req = urllib.request.Request(url.rstrip("/") + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                if not raw.strip():
                    continue
                e = json.loads(raw)
                if e.get("error"):
                    raise SetupError(f"Downloading {model} failed: {e['error']}")
                total, done = e.get("total"), e.get("completed")
                pct = round(100 * done / total, 1) if total and done is not None else None
                progress(f"Downloading {model}: {e.get('status', '')}", pct)
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
        raise SetupError(f"Downloading {model} failed: {e}") from e


def install(model: str, url: str, progress: Progress):
    """Make `model` available: install Ollama if needed, start it, download the model."""
    if not ollama_status(model, url)["reachable"]:
        if not ollama_exe():
            install_ollama(progress)
        progress("Starting Ollama…", None)
        if not start_server(url):
            raise SetupError("Ollama is installed but did not start. Start Ollama, then press Install again.")
    if not ollama_status(model, url)["model_installed"]:
        pull(model, url, progress)
    if not ollama_status(model, url)["model_installed"]:
        raise SetupError(f"{model} was downloaded but Ollama does not list it.")
    progress(f"{model} is installed.", 100.0)
