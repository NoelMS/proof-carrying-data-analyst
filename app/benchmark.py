"""Run labelled questions through the full workflow and measure outcomes.

Labels come from data/synthetic/ground_truth.json, computed by the data generator
from clean records, independently of the analysis engine.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog
from .verification import normalize
from .workflow import Analyst

RESULTS_FILE = Path("benchmark_results.json")


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def run_benchmark(analyst: Analyst, cases: list[dict], progress=None) -> dict:
    """`progress(event)` receives {"type": "case_start" | "stage" | "case_done", ...} as work happens."""
    rows, attempts, exec_ok, verified_attempts, reproduced = [], 0, 0, 0, 0
    t0 = time.monotonic()
    emit = progress or (lambda e: None)
    for i, case in enumerate(cases):
        emit({"type": "case_start", "index": i, "question": case["question"], "answerable": case["answerable"]})
        tc = time.monotonic()
        st = analyst.run(case["question"], on_event=lambda s, i=i: emit(
            {"type": "stage", "index": i, "stage": s.stages[-1]["stage"], "next": s.stages[-1].get("next")}))
        f = st.final
        verified = f["status"] == "verified"
        correct = verified and case["answerable"] and normalize(f["numeric_value"]) == normalize(case["expected"])
        for at in st.attempts:
            attempts += 1
            exec_ok += bool(at.execution and at.execution.ok)
            verified_attempts += bool(at.verification and at.verification.passed)
        repro = None
        if verified:  # reproduce the shown proof once more, outside the workflow
            emit({"type": "stage", "index": i, "stage": "answer", "next": "reproduce"})
            r = analyst.sandbox.run(f["proof_code"], analyst.cat.workspace.data_dir, st.plan.tables)
            repro = bool(r.ok and normalize(r.result) == normalize(f["numeric_value"]))
            reproduced += repro
        row = {"question": case["question"], "answerable": case["answerable"], "status": f["status"],
               "correct": correct if case["answerable"] else not verified,
               "confident_wrong": verified and not correct,
               "expected": case["expected"], "got": f.get("numeric_value"),
               "attempts": len(st.attempts), "reproduced": repro,
               "duration_s": round(time.monotonic() - tc, 2),
               "detail": f.get("answer") if verified else f.get("reason")}
        if repro is not None:
            emit({"type": "stage", "index": i, "stage": "reproduce", "next": None})
        rows.append(row)
        emit({"type": "case_done", "index": i, "row": row})
    ans = [r for r in rows if r["answerable"]]
    unans = [r for r in rows if not r["answerable"]]
    n_verified = sum(r["status"] == "verified" for r in rows)
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(time.monotonic() - t0, 1),
        "cases": len(rows),
        "metrics": {
            "valid_answer_accuracy": _rate(sum(r["correct"] for r in ans), len(ans)),
            "refusal_accuracy": _rate(sum(r["correct"] for r in unans), len(unans)),
            "verification_success_rate": _rate(verified_attempts, attempts),
            "execution_success_rate": _rate(exec_ok, attempts),
            "proof_reproduction_rate": _rate(reproduced, n_verified),
            "confident_wrong_rate": _rate(sum(r["confident_wrong"] for r in rows), len(rows)),
        },
        "rows": rows,
    }


def run_and_save(catalog: Catalog, cases_file: Path, out: Path = RESULTS_FILE, analyst: Analyst | None = None,
                 progress=None) -> dict:
    cases = json.loads(Path(cases_file).read_text(encoding="utf-8"))
    report = run_benchmark(analyst or Analyst(catalog), cases, progress)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
