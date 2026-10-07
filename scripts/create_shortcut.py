"""Create Desktop and Start Menu shortcuts that open the analyst with one click (Windows).

    .venv\\Scripts\\python scripts\\create_shortcut.py

The shortcut runs launch.pyw with the pythonw.exe of the interpreter used to run this script.
The launcher creates these shortcuts itself on first setup; run this only to recreate them.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Proof-Carrying Data Analyst"


NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def create_shortcuts(pythonw: Path, only_missing: bool = False) -> list[Path]:
    """Desktop + Start-menu shortcuts that run launch.pyw with `pythonw`. Returns the shortcuts written."""
    folders = subprocess.run(  # honours OneDrive-redirected Desktops
        ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop'); [Environment]::GetFolderPath('Programs')"],
        capture_output=True, text=True, check=True, creationflags=NO_WINDOW).stdout.splitlines()
    written = []
    for folder in (Path(f) for f in folders if f.strip()):
        lnk = folder / f"{NAME}.lnk"
        if not folder.exists() or (only_missing and lnk.exists()):
            continue
        ps = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PCDA_LNK);"
            "$s.TargetPath = $env:PCDA_TARGET; $s.Arguments = $env:PCDA_ARGS;"
            "$s.WorkingDirectory = $env:PCDA_ROOT; $s.IconLocation = $env:PCDA_ICON;"
            "$s.Description = 'Verified data analysis with executable proof'; $s.Save()"
        )
        env = {**os.environ, "PCDA_LNK": str(lnk), "PCDA_TARGET": str(pythonw), "PCDA_ARGS": f'"{ROOT / "launch.pyw"}"',
               "PCDA_ROOT": str(ROOT), "PCDA_ICON": str(ROOT / "frontend" / "assets" / "app.ico")}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                       creationflags=NO_WINDOW)
        written.append(lnk)
    return written


def main():
    if os.name != "nt":
        sys.exit("Shortcuts are created on Windows only. Elsewhere, run: python launch.pyw")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        sys.exit(f"pythonw.exe not found next to {sys.executable}")
    for lnk in create_shortcuts(pythonw):
        print(f"Created {lnk}")


if __name__ == "__main__":
    main()
