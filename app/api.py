"""HTTP API + static frontend, standard library only.

    python server.py            ->  http://127.0.0.1:8600

Endpoints (JSON unless noted):
    GET  /api/status                      configuration, sandbox health, current workspace
    GET  /api/datasets                    tables with row/column counts, relationships, issues
    GET  /api/datasets/<name>             profile, sample rows, relationships and issues for one table
    POST /api/workspace                   {"files": [{"name", "data_base64"}]} or {"demo": true}
    POST /api/analyze                     {"question", "claim"?} -> {"id"}
    GET  /api/analysis/<id>               full analysis state
    GET  /api/analysis/<id>/events        Server-Sent Events: state after every workflow stage
    GET  /api/analysis/<id>/export        analysis as a JSON download
    POST /api/analysis/<id>/rerun         re-execute and re-verify the shown proof
    GET  /api/analysis/<id>/bundle        zip: proof.py, its data, requirements, run script
    POST /api/analysis/<id>/verify        {"code"?} run code (default: the proof) and every verification check
    POST /api/run                         {"code", "tables"} run code in the sandbox against the workspace data
    GET  /api/fixes                       proposed data fixes and the change log
    POST /api/fixes/preview|apply         {"fix_id", "choice"?, "value"?, "token" (apply)} preview / confirm a fix
    POST /api/fixes/rollback|restore      {"entry_id"} undo one change / {"table"} restore the original table
    GET  /api/history                     past analyses (newest first)
    GET  /api/benchmark, POST /api/benchmark, GET /api/benchmark/progress

The server binds to localhost. POST bodies must be application/json, which a plain
cross-site HTML form cannot send.
"""
import base64
import binascii
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .benchmark import run_benchmark
from .catalog import Catalog, build_catalog
from .answerability import assess
from .config import Config
from .documents import rules as doc_rules
from .fixes import FixError, FixStore, as_dict, diff, fingerprint, option_by_id, propose, transform
from . import local_setup
from .ingestion import DOCUMENTS, READERS, IngestionError, build_workspace, load_directory, read_file
from .local_model import ollama_status
from .local_setup import DOWNLOAD_URL, ollama_exe
from .planning import build_plan
from .question import QuerySpec
from .sandbox import Sandbox
from .verification import normalize, verify
from .workflow import AnalysisState, Analyst, format_value, setup_logging

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DEMO = ROOT / "data" / "synthetic"
MESSY = ROOT / "data" / "messy"  # real-world messiness and documents; its own independently computed answer key
BENCHMARKS = {"demonstration": DEMO, "messy": MESSY}
STATE = Path(os.environ.get("PCDA_STATE_DIR") or ROOT / ".pcda")  # history, uploads, fixes, active workspace
HISTORY = STATE / "history"
UPLOADS = STATE / "uploads"
MAX_BODY = 100 * 1024 * 1024
SAMPLE_ROWS = 25


# ------------------------------------------------------------------ serialization

def serialize(st: AnalysisState, cat: Catalog) -> dict:
    a, p = st.answerability, st.plan
    rows = {t: cat.profiles[t].rows for t in st.datasets if t in cat.profiles}
    # Drafts that failed verification are internal to the agent: the user sees the proof that passed (or the
    # refusal), not the attempts before it. They stay in logs/pcda.jsonl.
    failed = {at.number for at in st.attempts if at.failure_reason}
    if st.final and st.final["status"] == "refused" and st.attempts:  # a refusal shows the evidence it rests on
        failed.discard(st.attempts[-1].number)
    out = {
        "id": st.request_id, "question": st.question, "claimed_value": st.claimed_value,
        "interpreter": st.interpreter, "spec": st.spec.model_dump(exclude_defaults=True) if st.spec else None,
        "datasets": [{"name": t, "rows": n} for t, n in rows.items()],
        "issues": [i.__dict__ for i in st.detected_issues],
        "answerability": {"status": a.status, "reasons": a.reasons, "diagnostics": a.diagnostics} if a else None,
        "plan": {"steps": p.steps, "output": p.output, "unit": p.unit, "precision": p.precision,
                 "tables": p.tables, "kind": p.spec.kind} if p else None,
        "attempts": [{
            "number": at.number, "source": at.source, "code": at.code, "failure_reason": at.failure_reason,
            "execution": at.execution.__dict__ if at.execution else None,
            "verification": at.verification.record() if at.verification else None,
        } for at in st.attempts if at.number not in failed],
        "stages": [e for e in st.stages if e.get("attempt") not in failed],
        "final": st.final or None,
    }
    if st.verified:
        r = st.final["numeric_value"]
        items = r.items() if isinstance(r, dict) else r if isinstance(r, list) else []
        out["final"] = {**st.final, "display": [
            {"key": str(k), "value": v, "formatted": format_value(p, v)} for k, v in items]}
    return out


# ------------------------------------------------------------------ application state

@dataclass
class Run:
    catalog: Catalog | None = None  # the workspace the analysis ran against
    state: AnalysisState | None = None
    snapshots: list[dict] = field(default_factory=list)
    done: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)


MODES = {"local": "Local model", "none": "Predefined rules"}


class App:
    """Three workspaces: the demonstration data, the user's uploads (which persist in `uploads_dir` and
    accumulate across uploads and restarts), and both together. The active choice is remembered too.
    Confirmed data fixes are stored per source as overrides (see fixes.py) and applied whenever it is
    loaded; in the combined workspace each table's fixes go to the store of the source it came from."""

    def __init__(self, cfg: Config, history_dir: Path = HISTORY, uploads_dir: Path = UPLOADS):
        self.cfg = cfg
        self.history_dir, self.uploads_dir = history_dir, uploads_dir
        history_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.runs: dict[str, Run] = {}
        self.bench: dict = {"running": False}
        self.install_state: dict = {"running": False}
        self._apply_saved_mode()
        self.workspace_label = "demonstration"
        self.catalog = self._build("demonstration")
        active = self._active_file.read_text().strip() if self._active_file.exists() else ""
        if active in ("uploaded", "both") and self.uploads():
            try:
                self.catalog, self.workspace_label = self._build(active), active
            except IngestionError:
                pass  # stay on the demonstration data; the uploads stay listed for removal
        self.sandbox_probe = self._probe()

    @property
    def _active_file(self) -> Path:
        return self.uploads_dir.parent / "workspace"

    def fix_store(self, label: str | None = None) -> FixStore:
        return FixStore(self.uploads_dir.parent / "fixes" / (label or self.workspace_label))

    def _probe(self) -> dict:
        r = Sandbox(self.cfg).run('print("RESULT: 1")', ROOT, [])
        return {"ready": r.ok, "status": r.status, "error": r.error, "checked_at": round(time.time())}

    def _upload_files(self) -> list[Path]:
        return sorted(p for p in self.uploads_dir.iterdir() if p.is_file())

    def _load(self, label: str, files: list[Path] | None = None, with_fixes: bool = True):
        dest = Path(tempfile.mkdtemp(prefix="pcda_ws_"))
        ws = load_directory(DEMO, dest) if label == "demonstration" else build_workspace(files or self._upload_files(), dest)
        if with_fixes:
            self.fix_store(label).apply_overrides(ws)
        return ws

    def _build(self, label: str, files: list[Path] | None = None, with_fixes: bool = True) -> Catalog:
        """`origin` maps each table to the fix store it belongs to and its name there."""
        if label != "both":
            cat = build_catalog(self._load(label, files, with_fixes))
            cat.origin = {t: (label, t) for t in cat.tables}
            return cat
        ws = self._load("demonstration", with_fixes=with_fixes)
        up = self._load("uploaded", files, with_fixes)
        origin = {t: ("demonstration", t) for t in ws.tables}
        renamed = {}
        for t, df in up.tables.items():
            name = t if t not in ws.tables else f"{t}_upload"  # demonstration names stay as the benchmark knows them
            while name in ws.tables:
                name += "_"
            renamed[t], origin[name] = name, ("uploaded", t)
            ws.tables[name] = df
            shutil.copyfile(up.data_dir / f"{t}.csv", ws.data_dir / f"{name}.csv")
            if name != t:
                ws.notes.append(f"Uploaded table '{t}' is shown as '{name}': the demonstration data has a table "
                                "with that name.")
        for term, d in up.metrics.items():  # the demonstration's definitions win a name clash
            ws.metrics.setdefault(term, {**d, "table": renamed.get(d.get("table"), d.get("table"))})
        ws.notes += up.notes
        ws.documents.update(up.documents)
        ws.normalized += [(renamed.get(t, t), c, what) for t, c, what in up.normalized]
        shutil.rmtree(up.root, ignore_errors=True)
        cat = build_catalog(ws)
        cat.origin = origin
        return cat

    def _route(self, table: str) -> tuple[FixStore, str]:
        label, name = self.catalog.origin.get(table, (self.workspace_label, table))
        return self.fix_store(label), name

    def _stores(self) -> list[tuple[str, FixStore]]:
        labels = dict.fromkeys(label for label, _ in self.catalog.origin.values())
        return [(label, self.fix_store(label)) for label in labels or [self.workspace_label]]

    def uploads(self) -> list[dict]:
        return [{"name": p.name, "bytes": p.stat().st_size} for p in self._upload_files()]

    def _activate(self, cat: Catalog, label: str):
        with self.lock:
            self.catalog, self.workspace_label = cat, label
        self._active_file.write_text(label)

    def add_uploads(self, files: list[tuple[str, bytes]]):
        """Validate the new files together with the existing uploads before keeping anything."""
        staging = Path(tempfile.mkdtemp(prefix="pcda_up_"))
        try:
            for p in self._upload_files():
                shutil.copyfile(p, staging / p.name)
            new_tables = set()
            for name, data in files:
                path = staging / Path(name).name
                path.write_bytes(data)  # same file name replaces the earlier upload
                if path.suffix.lower() in READERS:
                    new_tables |= set(read_file(path))
            self._build("uploaded", sorted(p for p in staging.iterdir() if p.is_file()), with_fixes=False)
            for name, data in files:
                (self.uploads_dir / Path(name).name).write_bytes(data)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        store = self.fix_store("uploaded")
        for t in new_tables:  # a re-uploaded file replaces any fixes made to its old version
            store.restore_original(t)
        self._activate(self._build("both"), "both")  # the demonstration data stays usable alongside

    def use_demo(self):
        self._activate(self._build("demonstration"), "demonstration")

    def use_uploads(self):
        if not self.uploads():
            raise LookupError("No files have been uploaded yet.")
        self._activate(self._build("uploaded"), "uploaded")

    def use_both(self):
        if not self.uploads():
            raise LookupError("No files have been uploaded yet.")
        self._activate(self._build("both"), "both")

    def remove_upload(self, name: str):
        path = self.uploads_dir / Path(name).name
        if not path.is_file():
            raise LookupError(f"No uploaded file named {name}.")
        if path.suffix.lower() in READERS:
            for t in read_file(path):
                self.fix_store("uploaded").restore_original(t)
        path.unlink()
        if self.workspace_label != "demonstration":
            self._activate(self._build(self.workspace_label), self.workspace_label) if self.uploads() else self.use_demo()

    # ---------------------------------------------------------------- data fixes
    def fixes(self) -> dict:
        return {"workspace": self.workspace_label, "options": [as_dict(o) for o in propose(self.catalog)],
                "log": self._log()}

    def _log(self) -> list[dict]:
        """Every applied fix behind the active tables, under the names the tables have here, oldest first."""
        shown = {o: t for t, o in self.catalog.origin.items()}
        entries = [{**e, "table": shown.get((label, e["table"]), e["table"])} for label, store in self._stores()
                   for e in store.log() if (label, e["table"]) in shown]
        return sorted(entries, key=lambda e: e["ts"])

    def preview_fix(self, fix_id: str, choice, value) -> dict:
        cat = self.catalog
        opt = option_by_id(cat, fix_id)
        before = cat.tables[opt.table]
        after = transform(cat, opt, choice, value)
        return {"option": as_dict(opt), "choice": choice, "value": value, "token": fingerprint(before),
                **diff(cat, opt.table, before, after)}

    def apply_fix(self, fix_id: str, choice, value, token: str) -> dict:
        cat = self.catalog
        opt = option_by_id(cat, fix_id)
        before = cat.tables[opt.table]
        if token != fingerprint(before):
            raise FixError("The table changed since this preview. Preview the change again before applying it.")
        after = transform(cat, opt, choice, value)
        d = diff(cat, opt.table, before, after)
        if not d["removed"]["count"] and not d["changed"]["count"]:
            raise FixError("This change would not modify any rows.")
        store, name = self._route(opt.table)
        entry = {**store.commit(name, before, after, opt, choice, value, d), "table": opt.table}
        self._activate(self._build(self.workspace_label), self.workspace_label)
        return entry

    def rollback_fix(self, entry_id: str) -> dict:
        store = next((st for _, st in self._stores() if any(e["id"] == entry_id for e in st.log())), self.fix_store())
        entry = store.rollback(entry_id)
        self._activate(self._build(self.workspace_label), self.workspace_label)
        return entry

    def restore_table(self, table: str) -> int:
        store, name = self._route(table)
        n = store.restore_original(name)
        self._activate(self._build(self.workspace_label), self.workspace_label)
        return n

    # ---------------------------------------------------------------- analyses
    def start(self, question: str, claim) -> str:
        cat = self.catalog
        analyst = Analyst(cat, self.cfg, suggest_fixes=True)
        run = Run(catalog=cat)
        rid = uuid.uuid4().hex[:12]
        self.runs[rid] = run

        def push(st: AnalysisState):
            snap = serialize(st, cat)
            with run.cond:
                run.state = st
                run.snapshots.append(snap)
                run.cond.notify_all()

        def work():
            try:
                st = analyst.run(question, claim, on_event=push, request_id=rid)
                (self.history_dir / f"{rid}.json").write_text(json.dumps(serialize(st, cat), default=str), encoding="utf-8")
            finally:
                with run.cond:
                    run.done = True
                    run.cond.notify_all()
        threading.Thread(target=work, daemon=True).start()
        return rid

    def latest(self, rid: str) -> dict | None:
        run = self.runs.get(rid)
        if run and run.snapshots:
            return run.snapshots[-1]
        path = self.history_dir / f"{rid}.json"
        if rid.isalnum() and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def history(self) -> list[dict]:
        items = []
        for path in sorted(self.history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            f = d.get("final") or {}
            total = sum(e.get("duration_s") or 0 for e in d.get("stages", []))
            items.append({"id": d["id"], "question": d["question"], "status": f.get("status"),
                          "answerability": f.get("answerability"), "answer": f.get("answer"),
                          "reason": f.get("reason"), "duration_s": round(total, 2),
                          "ts": d["stages"][0]["ts"] if d.get("stages") else path.stat().st_mtime})
        return items

    def _plan_for(self, rid: str):
        """The analysis plan and the data it runs on. Analyses from earlier sessions are rebuilt from their
        saved interpretation against the current workspace; the checks then run exactly as before."""
        run = self.runs.get(rid)
        if run and run.state and run.state.plan:
            return run.state.plan, run.catalog, run.state.final
        snap = self.latest(rid)
        if not snap or not snap.get("spec"):
            raise LookupError("This analysis has no saved interpretation to rebuild.")
        cat = self.catalog
        spec = QuerySpec(**snap["spec"])
        a = assess(spec, cat)
        if not a.answerable:
            raise LookupError(f"The current data cannot support this analysis: {a.reasons[0]}")
        return build_plan(snap["question"], spec, a, cat), cat, snap.get("final") or {}

    def verify_code(self, rid: str, code: str | None = None) -> dict:
        """Execute `code` (default: the analysis's proof) in the sandbox and run every verification check."""
        plan, cat, final = self._plan_for(rid)
        code = code if code is not None else final.get("proof_code")
        if not code:
            raise LookupError("There is no proof code to run for this analysis.")
        sb = Sandbox(self.cfg)
        ex = sb.run(code, cat.workspace.data_dir, plan.tables)
        v = verify(plan, code, ex, sb, cat.workspace.data_dir)
        shown = final.get("numeric_value")
        same = bool(ex.ok and shown is not None and normalize(ex.result) == normalize(shown))
        return {"execution": ex.__dict__, "verification": v.record(), "matches_shown_result": same,
                "plan": {"steps": plan.steps, "tables": plan.tables}}

    def run_code(self, code: str, tables: list[str]) -> dict:
        unknown = [t for t in tables if t not in self.catalog.tables]
        if unknown:
            raise LookupError(f"Unknown table(s): {', '.join(unknown)}")
        ex = Sandbox(self.cfg).run(code, self.catalog.workspace.data_dir, tables)
        return {"execution": ex.__dict__}

    def bundle(self, rid: str) -> bytes:
        """A zip that runs outside the app: proof, the data it reads, requirements, and launch scripts."""
        plan, cat, final = self._plan_for(rid)
        code = final.get("proof_code")
        if not code:
            raise LookupError("Only verified analyses have a proof to download.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("proof.py", code)
            for t in plan.tables:
                z.write(cat.workspace.data_dir / f"{t}.csv", f"data/{t}.csv")
            z.writestr("requirements.txt", "pandas>=2.1\n")
            z.writestr("run_with_analyst_python.bat",
                       f'@echo off\r\ncd /d "%~dp0"\r\n"{sys.executable}" proof.py\r\npause\r\n')
            z.writestr("README.txt", (
                f"Proof for: {final.get('answer', '')}\n\n"
                "Run it in either way:\n"
                "  1. Windows: double-click run_with_analyst_python.bat (uses the analyst's own Python, which has\n"
                "     pandas installed).\n"
                "  2. Any Python 3.10+:  pip install -r requirements.txt  then  python proof.py\n\n"
                "Run it from this folder: the proof reads data/<table>.csv relative to the current directory.\n"
                "Expected output: one line starting with RESULT: and the verified value.\n"
                + "".join(f"\nPrepared at upload: {t}.{c}: {what}." for t, c, what in cat.workspace.normalized
                          if t in plan.tables)))
        return buf.getvalue()

    # ---------------------------------------------------------------- benchmark (background job with progress)
    @property
    def results_file(self) -> Path:
        return self.uploads_dir.parent / "benchmark_results.json"

    def cancel_benchmark(self):
        if not self.bench.get("running"):
            raise LookupError("No benchmark is running.")
        self.bench["cancel"] = True  # takes effect before the next question

    def start_benchmark(self):
        with self.lock:
            if self.bench.get("running"):
                raise LookupError("A benchmark is already running.")
            cases = [{**c, "dataset": name} for name, d in BENCHMARKS.items() if (d / "ground_truth.json").exists()
                     for c in json.loads((d / "ground_truth.json").read_text(encoding="utf-8"))]
            self.bench = {"running": True, "total": len(cases), "done": 0, "current": None, "rows": [],
                          "started_at": time.time(), "error": None, "report": None, "cancel": False, "cancelled": False,
                          "questions": [c["question"] for c in cases],
                          "cases": [{"question": c["question"], "answerable": c["answerable"], "expected": c["expected"],
                                     "expected_refusal": c.get("expected_refusal")} for c in cases]}
        # the answer keys refer to the original data: no fixes, each dataset in its own workspace
        analysts = {name: Analyst(build_catalog(load_directory(d, Path(tempfile.mkdtemp(prefix="pcda_bench_")))), self.cfg)
                    for name, d in BENCHMARKS.items() if (d / "ground_truth.json").exists()}

        def progress(e):
            b = self.bench
            if e["type"] == "case_start":
                b["current"] = {"index": e["index"], "question": e["question"], "answerable": e["answerable"],
                                "stage": "interpret", "stages_done": []}
            elif e["type"] == "stage" and b["current"]:
                b["current"]["stages_done"].append(e["stage"])
                b["current"]["stage"] = e["next"] or e["stage"]
            elif e["type"] == "case_done":
                b["rows"].append(e["row"])
                b["done"] = len(b["rows"])
                b["current"] = None

        def work():
            try:
                report = run_benchmark(analysts, cases, progress, should_stop=lambda: self.bench.get("cancel"))
                if not report["cancelled"]:
                    self.results_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
                self.bench["report"] = report
                self.bench["cancelled"] = self.bench["report"]["cancelled"]
            except Exception as e:  # surfaced in the progress view, never silent
                self.bench["error"] = f"{type(e).__name__}: {e}"
            finally:
                self.bench["running"] = False
                self.bench["finished_at"] = time.time()
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- agent mode and the local model
    @property
    def _mode_file(self) -> Path:
        return self.uploads_dir.parent / "mode"

    def saved_mode(self) -> str | None:
        mode = self._mode_file.read_text().strip() if self._mode_file.exists() else ""
        return mode if mode in MODES else None

    def _apply_saved_mode(self):
        """A mode chosen in the app replaces `auto`; an explicit PCDA_LLM_PROVIDER still wins."""
        mode = self.saved_mode()
        if mode and os.environ.get("PCDA_LLM_PROVIDER", "auto").strip() == "auto" and not self._unavailable(mode):
            self.cfg = replace(self.cfg, llm_provider=mode)

    def _unavailable(self, mode: str) -> str | None:
        if mode == "local" and not ollama_status(self.cfg.local_model, self.cfg.ollama_url)["model_installed"]:
            return f"The local model ({self.cfg.local_model}) is not installed yet: install it first."
        return None

    def set_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"Unknown mode '{mode}'.")
        if why := self._unavailable(mode):
            raise LookupError(why)
        self.cfg = replace(self.cfg, llm_provider=mode)  # every analysis builds its Analyst from self.cfg
        self._mode_file.write_text(mode)

    def model_info(self) -> dict:
        cfg = self.cfg
        status = ollama_status(cfg.local_model, cfg.ollama_url)
        return {"mode": cfg.llm_provider, "saved": self.saved_mode(), "agent_model": cfg.local_model,
                "interpreter_model": cfg.interpreter_model,
                "interpreter_installed": any(n.split(":")[0] == cfg.interpreter_model for n in status["models"]),
                "ollama_running": status["reachable"], "ollama_installed": bool(status["reachable"] or ollama_exe()),
                "model_installed": status["model_installed"],
                "download_url": DOWNLOAD_URL, "install": self.install_state}

    def start_install(self):
        with self.lock:
            if self.install_state.get("running"):
                raise LookupError("The local model is already being installed.")
            self.install_state = {"running": True, "message": "Starting…", "percent": None, "error": None, "done": False}

        def progress(message: str, percent: float | None):
            self.install_state.update(message=message, percent=percent)

        def work():
            try:
                local_setup.install(self.cfg.local_model, self.cfg.ollama_url, progress)
                self.set_mode("local")
                self.install_state["done"] = True
            except local_setup.SetupError as e:
                self.install_state["error"] = str(e)
            except Exception as e:  # surfaced in the panel, never silent
                self.install_state["error"] = f"{type(e).__name__}: {e}"
            finally:
                self.install_state["running"] = False
        threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ HTTP layer

class Handler(SimpleHTTPRequestHandler):
    app: App = None  # set by serve()

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(FRONTEND), **k)

    def log_message(self, fmt, *args):  # quiet; structured logs live in logs/pcda.jsonl
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if not self.path.startswith("/api/"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'")
        super().end_headers()

    def _json(self, data, status=HTTPStatus.OK, headers=None):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _body(self) -> dict | None:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json.")
            return None
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large.")
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._error(HTTPStatus.BAD_REQUEST, "Body is not valid JSON.")
            return None

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/"):
            if "." not in path.rsplit("/", 1)[-1]:
                self.path = "/index.html"  # client-side routes
            return super().do_GET()
        parts = path.strip("/").split("/")[1:]
        app = self.app
        try:
            if parts == ["status"]:
                return self._json(self._status())
            if parts == ["datasets"]:
                return self._json(self._datasets())
            if len(parts) == 2 and parts[0] == "datasets":
                return self._dataset(parts[1])
            if parts == ["examples"]:
                gt = DEMO / "ground_truth.json"
                demo = app.workspace_label != "uploaded" and gt.exists()
                return self._json([c["question"] for c in json.loads(gt.read_text(encoding="utf-8"))] if demo else [])
            if parts == ["history"]:
                return self._json(app.history())
            if parts == ["benchmark"]:
                f = app.results_file
                return self._json(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)
            if parts == ["model"]:
                return self._json(app.model_info())
            if parts == ["benchmark", "progress"]:
                return self._json({k: v for k, v in app.bench.items() if k != "report"})
            if parts == ["fixes"]:
                return self._json(app.fixes())
            if len(parts) >= 2 and parts[0] == "analysis":
                rid = parts[1]
                if len(parts) == 2:
                    snap = app.latest(rid)
                    return self._json(snap) if snap else self._error(HTTPStatus.NOT_FOUND, "Unknown analysis.")
                if parts[2] == "events":
                    return self._events(rid)
                if parts[2] == "export":
                    snap = app.latest(rid)
                    if not snap:
                        return self._error(HTTPStatus.NOT_FOUND, "Unknown analysis.")
                    return self._json(snap, headers={"Content-Disposition": f'attachment; filename="analysis-{rid}.json"'})
                if parts[2] == "bundle":
                    try:
                        data = app.bundle(rid)
                    except LookupError as e:
                        return self._error(HTTPStatus.CONFLICT, str(e))
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition", f'attachment; filename="proof-{rid}.zip"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            return self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _status(self):
        app, cat, cfg = self.app, self.app.catalog, self.app.cfg
        return {
            "product": "Proof-Carrying Data Analyst",
            "interpreter": MODES[cfg.llm_provider] + (f" ({cfg.local_model})" if cfg.llm_provider == "local" else ""),
            "mode": cfg.llm_provider, "mode_saved": app.saved_mode(),
            "sandbox": {"provider": cfg.sandbox, "timeout_s": cfg.timeout_s, "memory_mb": cfg.memory_mb, **app.sandbox_probe},
            "max_repairs": cfg.max_repairs,
            "workspace": app.workspace_label,
            "tables": len(cat.tables),
            "supported_uploads": sorted(READERS) + sorted(DOCUMENTS) + ["metrics.json"],
            "benchmark_available": (DEMO / "ground_truth.json").exists(),
        }

    def _datasets(self):
        cat = self.app.catalog
        tables = [{"name": t, "rows": p.rows, "columns": len(p.columns), "key": p.key,
                   "issues": sum(1 for i in cat.issues if i.table == t)} for t, p in cat.profiles.items()]
        return {"workspace": self.app.workspace_label, "uploads": self.app.uploads(), "tables": tables,
                "totals": {"tables": len(tables), "columns": sum(t["columns"] for t in tables),
                           "records": sum(t["rows"] for t in tables)},
                "relationships": [r.__dict__ for r in cat.relationships],
                "issues": [i.__dict__ for i in cat.issues],
                "metrics": cat.metrics,
                "documents": [{"name": n, "rules": [{"kind": r.kind, "sentence": r.sentence} for r in doc_rules({n: text})
                                                    ]}
                              for n, text in cat.workspace.documents.items()]}

    def _dataset(self, name):
        cat = self.app.catalog
        if name not in cat.profiles:
            return self._error(HTTPStatus.NOT_FOUND, "Unknown table.")
        p, df = cat.profiles[name], cat.tables[name]
        sample = df.head(SAMPLE_ROWS).map(lambda v: v if len(v) <= 120 else v[:120] + "...")
        return self._json({
            "name": name, "rows": p.rows, "key": p.key, "exact_duplicate_rows": p.exact_duplicate_rows,
            "duplicate_keys": p.duplicate_keys, "columns": [c.__dict__ for c in p.columns.values()],
            "sample": {"columns": list(df.columns), "rows": sample.values.tolist()},
            "relationships": [r.__dict__ for r in cat.relationships if name in (r.child, r.parent)],
            "issues": [i.__dict__ for i in cat.issues if i.table == name],
        })

    def _events(self, rid):
        run = self.app.runs.get(rid)
        if not run:
            snap = self.app.latest(rid)
            if not snap:
                return self._error(HTTPStatus.NOT_FOUND, "Unknown analysis.")
            run = Run(snapshots=[snap], done=True)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = 0
        while True:
            with run.cond:
                if sent >= len(run.snapshots) and not run.done:
                    run.cond.wait(timeout=15)
                pending, finished = run.snapshots[sent:], run.done
            try:
                for snap in pending:
                    self.wfile.write(f"event: state\ndata: {json.dumps(snap, default=str)}\n\n".encode())
                sent += len(pending)
                if finished:  # no snapshot is added after `done` is set
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                if not pending:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        parts = unquote(urlparse(self.path).path).strip("/").split("/")[1:]
        body = self._body()
        if body is None:
            return
        app = self.app
        if parts == ["analyze"]:
            q = str(body.get("question") or "").strip()
            if not q:
                return self._error(HTTPStatus.BAD_REQUEST, "A question is required.")
            if len(q) > 2000:
                return self._error(HTTPStatus.BAD_REQUEST, "Question is too long (2,000 characters maximum).")
            claim = body.get("claim")
            claim = str(claim).strip() if claim not in (None, "") else None
            return self._json({"id": app.start(q, claim)}, HTTPStatus.ACCEPTED)
        if len(parts) == 3 and parts[0] == "analysis" and parts[2] in ("rerun", "verify"):
            code = body.get("code")
            if code is not None and (not isinstance(code, str) or len(code) > 200_000):
                return self._error(HTTPStatus.BAD_REQUEST, "Code must be text under 200,000 characters.")
            try:
                return self._json(app.verify_code(parts[1], code))
            except LookupError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
        if parts == ["run"]:
            code, tables = body.get("code"), body.get("tables") or []
            if not isinstance(code, str) or not code.strip() or len(code) > 200_000:
                return self._error(HTTPStatus.BAD_REQUEST, "Code is required (under 200,000 characters).")
            if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
                return self._error(HTTPStatus.BAD_REQUEST, "tables must be a list of table names.")
            try:
                return self._json(app.run_code(code, tables))
            except LookupError as e:
                return self._error(HTTPStatus.BAD_REQUEST, str(e))
        if len(parts) == 2 and parts[0] == "fixes":
            try:
                if parts[1] == "preview":
                    return self._json(app.preview_fix(str(body.get("fix_id")), body.get("choice"), body.get("value")))
                if parts[1] == "apply":
                    app.apply_fix(str(body.get("fix_id")), body.get("choice"), body.get("value"), str(body.get("token")))
                elif parts[1] == "rollback":
                    app.rollback_fix(str(body.get("entry_id")))
                elif parts[1] == "restore":
                    app.restore_table(str(body.get("table")))
                else:
                    return self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
            except FixError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
            return self._json({"datasets": self._datasets(), "fixes": app.fixes()})
        if parts == ["workspace"]:
            try:
                if body.get("demo"):
                    app.use_demo()
                elif body.get("uploaded"):
                    app.use_uploads()
                elif body.get("both"):
                    app.use_both()
                else:
                    files = [(str(f["name"]), base64.b64decode(f["data_base64"], validate=True))
                             for f in body.get("files") or []]
                    if not files:
                        return self._error(HTTPStatus.BAD_REQUEST, "No files were provided.")
                    app.add_uploads(files)
            except IngestionError as e:
                return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
            except LookupError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
            except (KeyError, TypeError, binascii.Error):
                return self._error(HTTPStatus.BAD_REQUEST, "Each file needs a name and base64 data.")
            return self._json(self._datasets())
        if parts == ["model", "install"]:
            try:
                app.start_install()
            except LookupError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
            return self._json({"started": True}, HTTPStatus.ACCEPTED)
        if parts == ["mode"]:
            try:
                app.set_mode(str(body.get("mode")))
            except ValueError as e:
                return self._error(HTTPStatus.BAD_REQUEST, str(e))
            except LookupError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
            return self._json(app.model_info())
        if parts == ["benchmark", "cancel"]:
            try:
                app.cancel_benchmark()
            except LookupError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
            return self._json({"cancelling": True}, HTTPStatus.ACCEPTED)
        if parts == ["benchmark"]:
            try:
                app.start_benchmark()
            except LookupError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
            return self._json({"started": True}, HTTPStatus.ACCEPTED)
        return self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")

    # ---------------------------------------------------------------- DELETE
    def do_DELETE(self):
        parts = unquote(urlparse(self.path).path).strip("/").split("/")[1:]
        if len(parts) == 2 and parts[0] == "uploads":
            try:
                self.app.remove_upload(parts[1])
            except LookupError as e:
                return self._error(HTTPStatus.NOT_FOUND, str(e))
            except IngestionError as e:
                return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
            return self._json(self._datasets())
        return self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")


def create_server(host: str = "127.0.0.1", port: int = 8600, cfg: Config | None = None) -> ThreadingHTTPServer:
    """Port 0 picks a free port; read it back from `server.server_port`."""
    setup_logging()
    if cfg is None:  # a real launch: an installed Ollama that is not running is started, so the agent is found
        local_setup.start_server(os.environ.get("PCDA_OLLAMA_URL", Config.ollama_url), wait_s=8)
    Handler.app = App(cfg or Config.from_env())
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8600, cfg: Config | None = None):
    httpd = create_server(host, port, cfg)
    print(f"Proof-Carrying Data Analyst on http://{host}:{httpd.server_port}")
    httpd.serve_forever()
