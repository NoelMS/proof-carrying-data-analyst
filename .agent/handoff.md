# Handoff

## Current implementation status
Working end to end: ingestion -> profiling -> trap detection -> interpretation -> answerability -> plan ->
code generation -> sandbox execution -> independent verification -> repair/refusal -> Streamlit UI / CLI.

## Completed tasks
- T-01 Repository created; architecture is an explicit state machine (`app/workflow.py`). LangGraph was not added, since the explicit loop covers the need.
- T-02 Ingestion (CSV/XLSX, encodings, hostile headers) and profiling (types, keys, date formats, relationships).
- T-03 Trap detection (`app/traps.py`) and answerability (`app/answerability.py`).
- T-04 Analytical planning (`app/planning.py`).
- T-05 Code generation: pandas/Decimal proof and DuckDB SQL check (`app/codegen.py`). Optional Claude writer (`app/llm.py`).
- T-06 Sandbox: subprocess provider (isolated interpreter, empty env, audit hook, job object/rlimits) and Docker provider.
- T-07 Verification (`app/verification.py`): 9 checks, plus a claim check when a claimed value is supplied.
- T-08 Bounded repair loop and refusals with specific reasons.
- T-09 Streamlit UI (`streamlit_app.py`) and CLI (`scripts/cli.py`).
- T-10 Synthetic data + ground truth, pytest suite, benchmark, README, security hardening.

## Current task
None in progress.

## Known issues
- Claude mode has not been exercised against the live API: no API key was available during development. It is covered by tests with a stand-in client, and SDK call shapes were checked against anthropic 1.11.0 signatures.
- The Windows job object is assigned just after process start (a few ms window). The Docker provider has no such window.
- The deterministic parser's grammar is limited (see README > Configuration).

## Tests executed
`python -m pytest -q`: all pass on Windows 11, Python 3.13. Docker tests ran with Docker Desktop and `pcda-sandbox:latest`.
`python scripts/cli.py benchmark`: 19 questions, all metrics 1.0, confident-wrong rate 0.0.

## Tests failing
None.

## Architectural decisions
- The model only fills a schema-validated QuerySpec, plus optionally proof code. All checks (answerability, joins, currency, dates) are deterministic code.
- The independent check is a second implementation (DuckDB SQL), so verification is not the same code run twice.
- Exact duplicate rows are counted once and disclosed. Same key with different fields is a contradiction and causes a refusal.
- Business definitions come only from `metrics.json` or column names, never from assumptions.
- Money uses Decimal from text, converts per record, and rounds once (half-to-even).

## Next recommended action
Run the benchmark with `PCDA_LLM_PROVIDER=anthropic` and an API key, and add phrasing-variation questions to the ground truth.
