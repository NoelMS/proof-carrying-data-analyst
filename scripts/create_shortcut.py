"""Create Desktop and Start Menu shortcuts that open the analyst with one click (Windows).

    .venv\\Scripts\\python scripts\\create_shortcut.py

The shortcut runs launch.pyw with the pythonw.exe of the interpreter used to run this script,
so run it with the environment that has the requirements installed.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Proof-Carrying Data Analyst"


def main():
    if os.name != "nt":
        sys.exit("Shortcuts are created on Windows only. Elsewhere, run: python launch.pyw")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        sys.exit(f"pythonw.exe not found next to {sys.executable}")
    folders = subprocess.run(  # honours OneDrive-redirected Desktops
        ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop'); [Environment]::GetFolderPath('Programs')"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    targets = [Path(f) for f in folders if f.strip()]
    for folder in targets:
        if not folder.exists():
            continue
        lnk = folder / f"{NAME}.lnk"
        ps = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PCDA_LNK);"
            "$s.TargetPath = $env:PCDA_TARGET; $s.Arguments = $env:PCDA_ARGS;"
            "$s.WorkingDirectory = $env:PCDA_ROOT; $s.IconLocation = $env:PCDA_ICON;"
            "$s.Description = 'Verified data analysis with executable proof'; $s.Save()"
        )
        env = {**os.environ, "PCDA_LNK": str(lnk), "PCDA_TARGET": str(pythonw), "PCDA_ARGS": f'"{ROOT / "launch.pyw"}"',
               "PCDA_ROOT": str(ROOT), "PCDA_ICON": str(ROOT / "frontend" / "assets" / "app.ico")}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True)
        print(f"Created {lnk}")


if __name__ == "__main__":
    main()
