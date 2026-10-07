"""The local agent writes and repairs proofs; the app can install it, or the user can choose rules only.
A fake Ollama server stands in for the real one, so these tests need no model."""
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import local_setup
from app.api import App
from app.workflow import Analyst
from tests.conftest import CFG

Q = "What is the total revenue in USD?"
CLOSED = "http://127.0.0.1:9"  # nothing listens here


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data: bytes, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(json.dumps({"models": [{"name": n} for n in self.server.models]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv = self.server
        if self.path == "/api/pull":
            srv.models.append(body["model"])
            lines = [{"status": "pulling", "total": 100, "completed": 40}, {"status": "success", "total": 100, "completed": 100}]
            return self._send("\n".join(json.dumps(x) for x in lines).encode() + b"\n", "application/x-ndjson")
        kind = "read" if "format" in body else "code"  # questions come back as JSON, code as a python block
        srv.calls.append((kind, body["model"], body["messages"][1]["content"]))
        fence = "`" * 3
        content = f"{fence}python\n{srv.codes.pop(0)}{fence}" if kind == "code" else json.dumps({"unresolved": ["x"]})
        self._send(json.dumps({"message": {"content": content}}).encode())


@pytest.fixture
def ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    srv.models, srv.calls, srv.codes = ["qwen2.5:1.5b"], [], []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()


@pytest.fixture(scope="module")
def good_code(catalog):
    return Analyst(catalog, CFG).run(Q).final["proof_code"]


def local(url):
    return replace(CFG, llm_provider="local", ollama_url=url, local_model="qwen2.5:1.5b")


def test_model_repairs_its_proof_from_verifier_feedback(catalog, ollama, good_code):
    ollama.codes = ["import pandas as pd\nprint('RESULT: ' + undefined_name)\n", good_code]
    st = Analyst(catalog, local(ollama.url)).run(Q)
    assert st.verified and [a.source for a in st.attempts] == ["model", "model"]
    assert st.final["proof_source"] == "model"
    code_calls = [c for c in ollama.calls if c[0] == "code"]
    assert "<feedback>" not in code_calls[0][2] and "NameError" in code_calls[1][2]  # the repair saw why it failed
    assert "undefined_name" in code_calls[1][2]  # and the script it is repairing


def test_template_is_the_labelled_safety_net(catalog, ollama):
    ollama.codes = ["print('RESULT: 1')\n"] * 3
    st = Analyst(catalog, local(ollama.url)).run(Q)
    assert [a.source for a in st.attempts] == ["model"] * 3 + ["template (fallback)"]
    assert st.verified and st.final["numeric_value"] == "815497.70"


def test_unreachable_model_falls_back_to_rules(catalog):
    st = Analyst(catalog, local(CLOSED)).run(Q)
    assert st.verified and st.interpreter.startswith("deterministic parser")
    assert [a.source for a in st.attempts] == ["template (fallback)"]


def test_fine_tuned_reader_reads_and_agent_writes(catalog, ollama, good_code):
    ollama.models.append("pcda-interpreter:latest")
    ollama.codes = [good_code]
    assert Analyst(catalog, local(ollama.url)).run(Q).verified
    assert {(k, m) for k, m, _ in ollama.calls} == {("read", "pcda-interpreter"), ("code", "qwen2.5:1.5b")}


def make_app(tmp_path, url):
    return App(local(url), history_dir=tmp_path / "history", uploads_dir=tmp_path / "pcda" / "uploads")


def test_install_downloads_the_model_and_switches_to_it(tmp_path, ollama, monkeypatch):
    monkeypatch.setenv("PCDA_LLM_PROVIDER", "auto")
    ollama.models = []
    app = make_app(tmp_path, ollama.url)
    app.set_mode("none")
    assert not app.model_info()["model_installed"]
    app.start_install()
    for _ in range(100):
        if not app.install_state["running"]:
            break
        time.sleep(0.05)
    assert app.install_state["done"] and not app.install_state["error"], app.install_state
    assert app.cfg.llm_provider == "local" and app.saved_mode() == "local"


def test_without_ollama_or_a_package_manager_the_user_is_told_where_to_get_it(monkeypatch):
    monkeypatch.setattr(local_setup, "ollama_exe", lambda: None)
    monkeypatch.setattr(local_setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(local_setup.subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may be run"))
    with pytest.raises(local_setup.SetupError, match="ollama.com/download"):
        local_setup.install("qwen2.5:1.5b", CLOSED, lambda *a: None)


def test_rules_only_choice_persists_and_local_needs_the_model(tmp_path, monkeypatch):
    monkeypatch.setenv("PCDA_LLM_PROVIDER", "auto")
    app = make_app(tmp_path, CLOSED)
    with pytest.raises(LookupError, match="install it first"):
        app.set_mode("local")
    app.set_mode("none")
    restarted = make_app(tmp_path, CLOSED)
    assert restarted.saved_mode() == "none" and restarted.cfg.llm_provider == "none"
