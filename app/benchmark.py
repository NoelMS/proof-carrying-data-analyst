"""Run labelled questions through the full workflow and measure outcomes.

Labels come from data/synthetic/ground_truth.json, computed by the data generator
from clean records, independently of the analysis engine. A question that must be refused
counts as correct only when it is refused for the expected reason (answerability state).
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog
from .verification import normalize
from .workflow import Analyst

RESULTS_FILE = Path("benchmark_results.json")  # default; the app keeps it in its state directory


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def run_benchmark(analyst: Analyst | dict, cases: list[dict], progress=None, should_stop=None) -> dict:
    """`analyst` may map each case's "dataset" to the Analyst for that dataset.
    `progress(event)` receives {"type": "case_start" | "stage" | "case_done", ...} as work happens.
    `should_stop()` is checked before each question; the report then covers the questions completed."""
    rows, attempts, exec_ok, verified_attempts, reproduced = [], 0, 0, 0, 0
    t0 = time.monotonic()
    emit = progress or (lambda e: None)
    cancelled = False
    for i, case in enumerate(cases):
        if should_stop and should_stop():
            cancelled = True
            break
        emit({"type": "case_start", "index": i, "question": case["question"], "answerable": case["answerable"]})
        tc = time.monotonic()
        an = analyst[case["dataset"]] if isinstance(analyst, dict) else analyst
        st = an.run(case["question"], on_event=lambda s, i=i: emit(
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
            r = an.sandbox.run(f["proof_code"], an.cat.workspace.data_dir, st.plan.tables)
            repro = bool(r.ok and normalize(r.result) == normalize(f["numeric_value"]))
            reproduced += repro
        expected_refusal = case.get("expected_refusal")
        got_refusal = None if verified else f.get("answerability")
        refused_right = not verified and (expected_refusal is None or got_refusal == expected_refusal)
        row = {"question": case["question"], "dataset": case.get("dataset"), "answerable": case["answerable"], "status": f["status"],
               "correct": correct if case["answerable"] else refused_right,
               "expected_refusal": expected_refusal, "got_refusal": got_refusal,
               "confident_wrong": verified and not correct,
               "expected": case["expected"], "got": f.get("numeric_value"),
               "attempts": len(st.attempts), "proof_source": f.get("proof_source"), "reproduced": repro,
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
        "total_cases": len(cases),
        "cancelled": cancelled,
        "metrics": {
            "valid_answer_accuracy": _rate(sum(r["correct"] for r in ans), len(ans)),
            "refusal_accuracy": _rate(sum(r["correct"] for r in unans), len(unans)),
            "verification_success_rate": _rate(verified_attempts, attempts),
            "execution_success_rate": _rate(exec_ok, attempts),
            "proof_reproduction_rate": _rate(reproduced, n_verified),
            "confident_wrong_rate": _rate(sum(r["confident_wrong"] for r in rows), len(rows)),
        },
        "by_dataset": {d: {"questions": len(rs),
                           "valid_answer_accuracy": _rate(sum(r["correct"] for r in rs if r["answerable"]), sum(r["answerable"] for r in rs)),
                           "refusal_accuracy": _rate(sum(r["correct"] for r in rs if not r["answerable"]), sum(not r["answerable"] for r in rs)),
                           "confident_wrong_rate": _rate(sum(r["confident_wrong"] for r in rs), len(rs))}
                       for d in dict.fromkeys(r["dataset"] for r in rows) if d
                       for rs in [[r for r in rows if r["dataset"] == d]]},
        "rows": rows,
    }


def run_and_save(catalog: Catalog, cases_file: Path, out: Path = RESULTS_FILE, analyst: Analyst | None = None,
                 progress=None, should_stop=None) -> dict:
    """Run and save the report. A cancelled run is returned but not saved, so the saved metrics always
    describe a complete run."""
    cases = json.loads(Path(cases_file).read_text(encoding="utf-8"))
    report = run_benchmark(analyst or Analyst(catalog), cases, progress, should_stop)
    if report["cancelled"]:
        return report
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
