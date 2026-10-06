"""Streamlit interface.  Run with:  streamlit run streamlit_app.py"""
import hashlib
import json
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from app.benchmark import RESULTS_FILE, run_and_save
from app.catalog import build_catalog
from app.config import Config
from app.ingestion import IngestionError, build_workspace, load_directory
from app.workflow import Analyst, setup_logging

DEMO = Path(__file__).parent / "data" / "synthetic"
STATE_LABEL = {"ANSWERABLE": "Answerable", "AMBIGUOUS": "Ambiguous", "INSUFFICIENT_DATA": "Insufficient data",
               "CONTRADICTORY_DATA": "Contradictory data", "UNSUPPORTED_OPERATION": "Unsupported operation",
               "VERIFICATION_FAILED": "Verification failed", "REFUSE": "Refused"}

st.set_page_config(page_title="Proof-Carrying Data Analyst", layout="wide")
st.markdown("""
<style>
.block-container { padding-top: 2.2rem; max-width: 1180px; }
h1 { font-weight: 600; letter-spacing: -0.01em; font-size: 1.7rem !important; }
h2, h3 { font-weight: 600; }
.sub { color: var(--muted); margin-top: -0.6rem; margin-bottom: 1.4rem; }
.pill { display: inline-block; padding: 0.1rem 0.55rem; border: 1px solid; border-radius: 3px;
        font-size: 0.78rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }
.ok { color: #1f6b45; border-color: #1f6b45; } .bad { color: #9a3b1b; border-color: #9a3b1b; }
.answer { font-size: 1.9rem; font-weight: 600; margin: 0.4rem 0 0.2rem 0; }
.label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 0.2rem; }
.flow { font-family: 'IBM Plex Mono', monospace; font-size: 0.8rem; color: var(--muted); margin-bottom: 1rem; }
:root { --muted: #6b6b66; }
@media (prefers-color-scheme: dark) { :root { --muted: #a3a29c; } .ok { color: #6cc596; border-color: #6cc596; } .bad { color: #e39272; border-color: #e39272; } }
</style>
""", unsafe_allow_html=True)
setup_logging()


@st.cache_resource(show_spinner="Profiling datasets")
def load_catalog(key: str, files: tuple[tuple[str, bytes], ...]):
    if not files:
        return build_catalog(load_directory(DEMO, Path(tempfile.mkdtemp(prefix="pcda_demo_"))))
    src = Path(tempfile.mkdtemp(prefix="pcda_up_"))
    for name, data in files:
        (src / Path(name).name).write_bytes(data)
    return build_catalog(build_workspace(sorted(src.iterdir()), src / "workspace"))


def pill(text: str, ok: bool) -> str:
    return f'<span class="pill {"ok" if ok else "bad"}">{text}</span>'


# ------------------------------------------------------------------ sidebar
cfg = Config.from_env()
with st.sidebar:
    st.markdown("### Data")
    source = st.radio("Source", ["Demonstration data", "Upload files"], label_visibility="collapsed")
    files: tuple = ()
    if source == "Upload files":
        ups = st.file_uploader("CSV, Excel, and optional metrics.json", type=["csv", "xlsx", "xlsm", "json"],
                               accept_multiple_files=True)
        files = tuple((u.name, u.getvalue()) for u in ups or [])
        if not files:
            st.info("Upload one or more files to begin.")
            st.stop()
    key = hashlib.sha256(b"".join(n.encode() + d for n, d in files)).hexdigest()
    try:
        cat = load_catalog(key, files)
    except IngestionError as e:
        st.error(f"Could not load the data: {e}")
        st.stop()
    st.dataframe(pd.DataFrame([{"table": t, "rows": p.rows, "columns": len(p.columns)} for t, p in cat.profiles.items()]),
                 hide_index=True, use_container_width=True)
    for note in cat.workspace.notes:
        st.caption(note)
    st.markdown("### System")
    interp = f"Claude ({cfg.llm_model})" if cfg.llm_provider == "anthropic" else "Deterministic parser"
    st.markdown(f"Interpreter: {interp}  \nSandbox: {cfg.sandbox}  \nTimeout: {cfg.timeout_s:g} s  \n"
                f"Memory cap: {cfg.memory_mb} MB  \nMax repairs: {cfg.max_repairs}")

st.title("Proof-Carrying Data Analyst")
st.markdown('<div class="sub">Every number is computed by executable code, re-run in an isolated sandbox, and checked '
            'against an independent computation before it is shown. Otherwise the answer is a refusal.</div>',
            unsafe_allow_html=True)

tab_q, tab_profile, tab_quality, tab_bench = st.tabs(["Analysis", "Data profile", "Data quality", "Benchmark"])

# ------------------------------------------------------------------ analysis
with tab_q:
    examples = []
    if not files and (DEMO / "ground_truth.json").exists():
        examples = [c["question"] for c in json.loads((DEMO / "ground_truth.json").read_text(encoding="utf-8"))]
    pick = st.selectbox("Example questions", ["(write your own)"] + examples) if examples else "(write your own)"
    with st.form("ask"):
        question = st.text_input("Question", value="" if pick == "(write your own)" else pick,
                                 placeholder="What is the total revenue in USD?")
        claim = st.text_input("Claimed value to check (optional)", placeholder="e.g. 1000")
        submitted = st.form_submit_button("Analyze", type="primary")
    if submitted and question.strip():
        with st.spinner("Interpreting, executing and verifying"):
            st.session_state["run"] = Analyst(cat, cfg).run(question, claim.strip() or None)
    run = st.session_state.get("run")
    if run:
        f = run.final
        st.markdown('<div class="flow">question → evidence → plan → execution → verification → answer</div>',
                    unsafe_allow_html=True)
        verified = f["status"] == "verified"
        state = "ANSWERABLE" if verified else f["answerability"]
        st.markdown(pill("Verified" if verified else "Not verified", verified) + "&nbsp;&nbsp;" +
                    pill(STATE_LABEL.get(state, state), state == "ANSWERABLE"), unsafe_allow_html=True)
        st.markdown(f'<div class="answer">{f["answer"] if verified and run.plan.output == "scalar" else ("Result" if verified else f["answer"])}</div>',
                    unsafe_allow_html=True)
        if verified and run.plan.output != "scalar":
            r = f["numeric_value"]
            rows = r.items() if isinstance(r, dict) else r
            st.dataframe(pd.DataFrame(list(rows), columns=["key", f"value{' (' + f['unit'] + ')' if f['unit'] else ''}"]),
                         hide_index=True, use_container_width=True)
        if not verified:
            st.markdown(f"**Reason.** {f['reason']}")
            for extra in f["also"]:
                st.markdown(f"- {extra}")
            st.caption("No numerical answer was produced because the available evidence was insufficient for verification.")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="label">Interpretation</div>', unsafe_allow_html=True)
            st.caption(run.interpreter)
            if run.spec:
                st.json(run.spec.model_dump(exclude_defaults=True), expanded=False)
            st.markdown('<div class="label">Data used</div>', unsafe_allow_html=True)
            st.write(", ".join(run.datasets) or "none")
            if run.answerability and run.answerability.diagnostics:
                st.markdown('<div class="label">Data checks</div>', unsafe_allow_html=True)
                for d in run.answerability.diagnostics:
                    st.markdown(f"- {d}")
        with c2:
            if run.plan:
                st.markdown('<div class="label">Analytical plan</div>', unsafe_allow_html=True)
                st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(run.plan.steps, 1)))
            if run.last and run.last.execution:
                ex = run.last.execution
                st.markdown('<div class="label">Execution</div>', unsafe_allow_html=True)
                st.markdown(f"Status `{ex.status}` · exit code `{ex.exit_code}` · {ex.duration_s:.2f} s · "
                            f"sandbox `{cfg.sandbox}`")
        if run.last and run.last.verification:
            v = run.last.verification
            st.markdown('<div class="label">Verification</div>', unsafe_allow_html=True)
            st.dataframe(pd.DataFrame([{"check": c.name, "result": "pass" if c.passed else "FAIL", "detail": c.detail}
                                       for c in v.checks]), hide_index=True, use_container_width=True)
            cmp = {"claimed value": v.claimed_value, "executed value": v.executed_value, "independent value": v.independent_value}
            st.code(json.dumps(cmp, indent=2, default=str), language="json")
        if verified:
            with st.expander("Proof (executable)", expanded=False):
                st.code(f["proof_code"], language="python")
                st.markdown('<div class="label">Execution output</div>', unsafe_allow_html=True)
                st.code(f["execution_output"] or "(none)", language="text")
        if len(run.attempts) > 1 or (run.attempts and not verified):
            with st.expander(f"Attempts ({len(run.attempts)})"):
                for at in run.attempts:
                    status = "verified" if at.verification and at.verification.passed else (at.failure_reason or "")
                    st.markdown(f"**Attempt {at.number}** ({at.source}): {status}")
                    st.code(at.code, language="python")
        with st.expander("Stage log"):
            st.dataframe(pd.DataFrame(run.stages), hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ profile
with tab_profile:
    t = st.selectbox("Table", list(cat.profiles))
    p = cat.profiles[t]
    st.markdown(f"{p.rows} rows · key `{p.key or 'none detected'}` · {p.exact_duplicate_rows} exact duplicate rows · "
                f"{len(p.duplicate_keys)} conflicting keys")
    st.dataframe(pd.DataFrame([{
        "column": c.name, "type": c.kind, "nulls": c.nulls, "null %": c.null_pct, "unique": c.unique,
        "min": c.min, "max": c.max, "decimals": c.decimals or None, "date format": c.date_format,
        "samples": ", ".join(c.samples)} for c in p.columns.values()]), hide_index=True, use_container_width=True)
    st.markdown('<div class="label">Relationships</div>', unsafe_allow_html=True)
    st.dataframe(pd.DataFrame([{"child": r.child, "column": r.column, "parent": r.parent, "unmatched keys": r.unmatched}
                               for r in cat.relationships]), hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ quality
with tab_quality:
    st.dataframe(pd.DataFrame([{"severity": i.severity, "issue": i.kind, "table": i.table, "column": i.column,
                                "detail": i.detail} for i in cat.issues]), hide_index=True, use_container_width=True)
    st.caption("Issues are facts about the data. Whether one blocks a question depends on the tables and columns that question uses.")

# ------------------------------------------------------------------ benchmark
with tab_bench:
    if files:
        st.write("The benchmark runs on the demonstration data, whose ground truth is known.")
    else:
        if st.button("Run benchmark"):
            with st.spinner("Running every labelled question through the full workflow"):
                run_and_save(cat, DEMO / "ground_truth.json")
        if RESULTS_FILE.exists():
            rep = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
            st.caption(f"Last run {rep['run_at']} · {rep['cases']} questions · {rep['duration_s']} s")
            m = rep["metrics"]
            cols = st.columns(len(m))
            for col, (k, val) in zip(cols, m.items()):
                col.metric(k.replace("_", " "), "n/a" if val is None else f"{val:.1%}")
            st.dataframe(pd.DataFrame(rep["rows"]), hide_index=True, use_container_width=True)
        else:
            st.write("No benchmark has been run yet.")
