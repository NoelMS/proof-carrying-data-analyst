"""Command-line entry point.

    python scripts/cli.py ask "What is the total revenue in USD?" [--data DIR] [--claim VALUE]
    python scripts/cli.py benchmark [--data DIR]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.benchmark import run_and_save  # noqa: E402
from app.catalog import build_catalog  # noqa: E402
from app.ingestion import load_directory  # noqa: E402
from app.workflow import Analyst, setup_logging  # noqa: E402

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


def main():
    ap = argparse.ArgumentParser(description="Proof-Carrying Data Analyst")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--claim", help="a claimed value to verify against the executed proof")
    bench = sub.add_parser("benchmark")
    for p in (ask, bench):
        p.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = ap.parse_args()
    setup_logging()
    cat = build_catalog(load_directory(args.data))
    if args.cmd == "benchmark":
        report = run_and_save(cat, args.data / "ground_truth.json")
        print(json.dumps(report["metrics"], indent=2))
        return
    st = Analyst(cat).run(args.question, args.claim)
    f = st.final
    print(f["answer"])
    if f["status"] == "verified":
        print("\nMethod:", f["method"])
        print("\nVerification:", ", ".join(c["name"] for c in f["verification"]["checks"]), "- all passed")
        print("\nProof:\n" + f["proof_code"])
        print("Execution result:", f["execution_output"])
    else:
        print("\nReason:", f["reason"])
        for r in f["also"]:
            print("Also:", r)
    for d in f["diagnostics"]:
        print("- " + d)


if __name__ == "__main__":
    main()
