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
    GET  /api/history                     past analyses (newest first)
    GET  /api/benchmark, POST /api/benchmark

The server binds to localhost. POST bodies must be application/json, which a plain
cross-site HTML form cannot send.
"""
import base64
import binascii
import json
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .benchmark import RESULTS_FILE, run_and_save
from .catalog import Catalog, build_catalog
from .config import Config
from .ingestion import READERS, IngestionError, build_workspace, load_directory
from .sandbox import Sandbox
from .verification import verify
from .workflow import AnalysisState, Analyst, format_value, setup_logging

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DEMO = ROOT / "data" / "synthetic"
HISTORY = ROOT / ".pcda" / "history"
MAX_BODY = 100 * 1024 * 1024
SAMPLE_ROWS = 25


# ------------------------------------------------------------------ serialization

def serialize(st: AnalysisState, cat: Catalog) -> dict:
    a, p = st.answerability, st.plan
    rows = {t: cat.profiles[t].rows for t in st.datasets if t in cat.profiles}
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
        } for at in st.attempts],
        "stages": st.stages,
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


class App:
    def __init__(self, cfg: Config, history_dir: Path = HISTORY):
        self.cfg = cfg
        self.history_dir = history_dir
        self.lock = threading.Lock()
        self.runs: dict[str, Run] = {}
        self.workspace_label = "demonstration"
        self.catalog = build_catalog(load_directory(DEMO, Path(tempfile.mkdtemp(prefix="pcda_demo_"))))
        self.sandbox_probe = self._probe()
        history_dir.mkdir(parents=True, exist_ok=True)

    def _probe(self) -> dict:
        r = Sandbox(self.cfg).run('print("RESULT: 1")', ROOT, [])
        return {"ready": r.ok, "status": r.status, "error": r.error, "checked_at": round(time.time())}

    def set_workspace(self, files: list[tuple[str, bytes]] | None):
        if files is None:
            cat, label = build_catalog(load_directory(DEMO, Path(tempfile.mkdtemp(prefix="pcda_demo_")))), "demonstration"
        else:
            src = Path(tempfile.mkdtemp(prefix="pcda_up_"))
            try:
                for name, data in files:
                    (src / Path(name).name).write_bytes(data)
                cat = build_catalog(build_workspace(sorted(p for p in src.iterdir() if p.is_file()), src / "workspace"))
            except IngestionError:
                shutil.rmtree(src, ignore_errors=True)
                raise
            label = "uploaded"
        with self.lock:
            self.catalog, self.workspace_label = cat, label

    def start(self, question: str, claim) -> str:
        cat = self.catalog
        analyst = Analyst(cat, self.cfg)
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

    def rerun(self, rid: str) -> dict:
        run = self.runs.get(rid)
        st = run.state if run else None
        if not st or not st.verified:
            raise LookupError("Re-run is available for verified analyses from the current server session.")
        sb = Sandbox(self.cfg)
        data_dir = run.catalog.workspace.data_dir
        code = st.final["proof_code"]
        ex = sb.run(code, data_dir, st.plan.tables)
        v = verify(st.plan, code, ex, sb, data_dir)
        same = ex.ok and ex.result == st.final["numeric_value"]
        return {"execution": ex.__dict__, "verification": v.record(), "matches_shown_result": same}


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
                demo = app.workspace_label == "demonstration" and gt.exists()
                return self._json([c["question"] for c in json.loads(gt.read_text(encoding="utf-8"))] if demo else [])
            if parts == ["history"]:
                return self._json(app.history())
            if parts == ["benchmark"]:
                return self._json(json.loads(RESULTS_FILE.read_text(encoding="utf-8")) if RESULTS_FILE.exists() else None)
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
            return self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _status(self):
        app, cat, cfg = self.app, self.app.catalog, self.app.cfg
        return {
            "product": "Proof-Carrying Data Analyst",
            "interpreter": f"Claude ({cfg.llm_model})" if cfg.llm_provider == "anthropic" else "Deterministic parser",
            "sandbox": {"provider": cfg.sandbox, "timeout_s": cfg.timeout_s, "memory_mb": cfg.memory_mb, **app.sandbox_probe},
            "max_repairs": cfg.max_repairs,
            "workspace": app.workspace_label,
            "tables": len(cat.tables),
            "supported_uploads": sorted(READERS) + ["metrics.json"],
            "benchmark_available": app.workspace_label == "demonstration" and (DEMO / "ground_truth.json").exists(),
        }

    def _datasets(self):
        cat = self.app.catalog
        tables = [{"name": t, "rows": p.rows, "columns": len(p.columns), "key": p.key,
                   "issues": sum(1 for i in cat.issues if i.table == t)} for t, p in cat.profiles.items()]
        return {"workspace": self.app.workspace_label, "tables": tables,
                "totals": {"tables": len(tables), "columns": sum(t["columns"] for t in tables),
                           "records": sum(t["rows"] for t in tables)},
                "relationships": [r.__dict__ for r in cat.relationships],
                "issues": [i.__dict__ for i in cat.issues],
                "metrics": cat.metrics}

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
        if len(parts) == 3 and parts[0] == "analysis" and parts[2] == "rerun":
            try:
                return self._json(app.rerun(parts[1]))
            except LookupError as e:
                return self._error(HTTPStatus.CONFLICT, str(e))
        if parts == ["workspace"]:
            try:
                if body.get("demo"):
                    app.set_workspace(None)
                else:
                    files = [(str(f["name"]), base64.b64decode(f["data_base64"], validate=True))
                             for f in body.get("files") or []]
                    if not files:
                        return self._error(HTTPStatus.BAD_REQUEST, "No files were provided.")
                    app.set_workspace(files)
            except IngestionError as e:
                return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
            except (KeyError, TypeError, binascii.Error):
                return self._error(HTTPStatus.BAD_REQUEST, "Each file needs a name and base64 data.")
            return self._json(self._datasets())
        if parts == ["benchmark"]:
            if app.workspace_label != "demonstration":
                return self._error(HTTPStatus.CONFLICT, "The benchmark runs on the demonstration data only.")
            return self._json(run_and_save(app.catalog, DEMO / "ground_truth.json", analyst=Analyst(app.catalog, app.cfg)))
        return self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")


def create_server(host: str = "127.0.0.1", port: int = 8600, cfg: Config | None = None) -> ThreadingHTTPServer:
    """Port 0 picks a free port; read it back from `server.server_port`."""
    setup_logging()
    Handler.app = App(cfg or Config.from_env())
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8600, cfg: Config | None = None):
    httpd = create_server(host, port, cfg)
    print(f"Proof-Carrying Data Analyst on http://{host}:{httpd.server_port}")
    httpd.serve_forever()
