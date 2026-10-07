"""One-click start: runs the server without a console window and opens the app in its own window.

Closing the app window stops the server. On a fresh clone, double-click `Proof-Carrying Data Analyst.cmd`
(or this file): the first start creates `.venv`, installs the requirements with a progress window, and
adds Desktop and Start-menu shortcuts. Later starts check the requirements and install anything missing.
"""
import os
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # logs, .env and history are relative to the project
sys.path.insert(0, str(ROOT))

BROWSERS = [  # browsers with an app mode (a window without tabs or an address bar)
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
] + [shutil.which(n) or "" for n in ("msedge", "google-chrome", "chromium", "chromium-browser")]


def alert(message: str):
    (ROOT / "logs").mkdir(exist_ok=True)
    with open(ROOT / "logs" / "launcher.log", "a", encoding="utf-8") as f:
        f.write(f"{message}\n{traceback.format_exc()}\n")
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Proof-Carrying Data Analyst", 0x10)
    else:
        print(message, file=sys.stderr)


def main():
    from app.bootstrap import ensure_environment  # standard library only
    if not ensure_environment(sys.argv):
        return
    try:
        from app.api import create_server
        server = create_server(port=0)
    except ModuleNotFoundError as e:
        alert(f"A required package is missing ({e.name}).\n\nInstall the requirements:\n"
              f"{sys.executable} -m pip install -r requirements.txt")
        return
    except Exception as e:  # anything else at start-up must be visible, not silent
        alert(f"The analyst could not start:\n{type(e).__name__}: {e}")
        return
    url = f"http://127.0.0.1:{server.server_port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    urllib.request.urlopen(url + "api/status", timeout=30).read()  # ready before the window opens

    browser = next((b for b in BROWSERS if b and Path(b).exists()), None)
    if not browser:
        webbrowser.open(url)
        threading.Event().wait()  # no app-mode browser: keep serving until the process is ended
    profile = ROOT / ".pcda" / "browser"  # own profile, so the window's lifetime is the app's lifetime
    window = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                               "--no-default-browser-check", "--window-size=1440,960"])
    window.wait()
    server.shutdown()


if __name__ == "__main__":
    main()
