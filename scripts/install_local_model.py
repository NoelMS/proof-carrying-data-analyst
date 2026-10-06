"""Install a trained interpreter (.gguf from the Colab notebook) into Ollama and check that it answers.

    .venv\\Scripts\\python scripts\\install_local_model.py path\\to\\pcda-interpreter.gguf [--name pcda-interpreter]
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.catalog import build_catalog  # noqa: E402
from app.ingestion import load_directory  # noqa: E402
from app.local_model import LocalInterpreter, ollama_status, reading  # noqa: E402
from app.question import catalog_summary  # noqa: E402

TEMPLATE = (ROOT / "training" / "Modelfile").read_text(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gguf", type=Path)
    ap.add_argument("--name", default="pcda-interpreter")
    args = ap.parse_args()
    gguf = args.gguf.resolve()
    if not gguf.is_file():
        sys.exit(f"Not found: {gguf}")
    ollama = shutil.which("ollama")
    if not ollama:
        sys.exit("Ollama is not installed. Get it from https://ollama.com/download and run this again.")
    if not ollama_status(args.name)["reachable"]:
        subprocess.Popen([ollama, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(0.5)
            if ollama_status(args.name)["reachable"]:
                break
        else:
            sys.exit("Could not start the Ollama server.")
    modelfile = gguf.parent / "Modelfile"
    if not modelfile.exists():
        modelfile.write_text(TEMPLATE.replace("FROM ./MODEL.gguf", f"FROM ./{gguf.name}"), encoding="utf-8")
    print(f"Creating Ollama model '{args.name}' from {gguf.name} ...")
    subprocess.run([ollama, "create", args.name, "-f", str(modelfile)], cwd=gguf.parent, check=True)

    cat = build_catalog(load_directory(ROOT / "data" / "synthetic", Path(tempfile.mkdtemp())))
    summary = catalog_summary(cat.tables, cat.profiles, cat.relationships, cat.metrics)
    q = "which region had the highest sales in usd"
    t0 = time.monotonic()
    spec = LocalInterpreter(args.name, timeout=180).interpret(q, summary)
    print(f"Test question: {q!r} ({time.monotonic() - t0:.1f} s)")
    print("Reading:", {k: v for k, v in reading(spec).items() if v is not None})
    print("\nInstalled. Restart the analyst; the System page will show 'Local model'.")


if __name__ == "__main__":
    main()
