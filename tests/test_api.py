"""HTTP API and static frontend, against a real server on a free port."""
import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app.api import App, Handler
from tests.conftest import CFG


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    Handler.app = App(CFG, history_dir=tmp_path_factory.mktemp("history"), uploads_dir=tmp_path_factory.mktemp("pcda") / "uploads")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.status, r.headers, r.read()


def post(url, body, ctype="application/json"):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def run(base, question, claim=None):
    status, body = post(f"{base}/api/analyze", {"question": question, "claim": claim})
    assert status == 202
    _, _, stream = get(f"{base}/api/analysis/{body['id']}/events")  # blocks until the run ends
    events = [ln for ln in stream.decode().splitlines() if ln.startswith("event:")]
    return body["id"], events


def test_status_and_frontend(base):
    _, _, raw = get(f"{base}/api/status")
    s = json.loads(raw)
    assert s["sandbox"]["ready"] is True and s["tables"] == 8
    status, headers, html = get(f"{base}/")
    assert status == 200 and b"Proof-Carrying Data Analyst" in html
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert get(f"{base}/history/deep/link")[0] == 200  # client-side routes serve the app


def test_datasets(base):
    d = json.loads(get(f"{base}/api/datasets")[2])
    assert {t["name"] for t in d["tables"]} >= {"orders", "customers"}
    one = json.loads(get(f"{base}/api/datasets/orders")[2])
    assert one["key"] == "order_id" and len(one["sample"]["rows"]) == 25


def test_verified_analysis_streams_and_reruns(base):
    rid, events = run(base, "What is the revenue in USD by region?")
    assert events.count("event: state") >= 7 and events[-1] == "event: end"
    snap = json.loads(get(f"{base}/api/analysis/{rid}")[2])
    assert snap["final"]["status"] == "verified"
    assert snap["final"]["display"][1] == {"key": "Europe", "value": "163760.97", "formatted": "163,760.97 USD"}
    status, rerun = post(f"{base}/api/analysis/{rid}/rerun", {})
    assert status == 200 and rerun["verification"]["status"] == "verified" and rerun["matches_shown_result"]
    _, headers, _ = get(f"{base}/api/analysis/{rid}/export")
    assert "attachment" in headers["Content-Disposition"]
    assert any(h["id"] == rid for h in json.loads(get(f"{base}/api/history")[2]))


def test_refusal_and_claim(base):
    rid, _ = run(base, "What is the total revenue?")
    snap = json.loads(get(f"{base}/api/analysis/{rid}")[2])
    assert snap["final"]["status"] == "refused" and snap["final"]["answerability"] == "AMBIGUOUS"
    assert post(f"{base}/api/analysis/{rid}/rerun", {})[0] == 409  # nothing verified to re-run


def test_input_validation(base):
    assert post(f"{base}/api/analyze", {"question": "  "})[0] == 400
    assert post(f"{base}/api/analyze", {"question": "x"}, ctype="text/plain")[0] == 415
    with pytest.raises(urllib.error.HTTPError) as e:
        get(f"{base}/api/analysis/doesnotexist")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        get(f"{base}/../app/api.py")


def test_uploads_accumulate_persist_and_switch(base):
    enc = lambda b: base64.b64encode(b).decode()  # noqa: E731
    status, d = post(f"{base}/api/workspace", {"files": [{"name": "sales.csv", "data_base64": enc(b"sale_id,amount\n1,500\n2,750\n")}]})
    assert status == 200 and [t["name"] for t in d["tables"]] == ["sales"]
    rid, _ = run(base, "What is the total amount?", claim="1250")
    assert json.loads(get(f"{base}/api/analysis/{rid}")[2])["final"]["status"] == "verified"
    status, d = post(f"{base}/api/workspace", {"files": [{"name": "shops.csv", "data_base64": enc(b"shop_id,name\ns1,North\n")}]})
    assert sorted(t["name"] for t in d["tables"]) == ["sales", "shops"]  # earlier upload kept
    assert post(f"{base}/api/workspace", {"files": [{"name": "x.csv", "data_base64": ""}]})[0] == 422
    assert sorted(u["name"] for u in json.loads(get(f"{base}/api/datasets")[2])["uploads"]) == ["sales.csv", "shops.csv"]

    assert post(f"{base}/api/workspace", {"demo": True})[1]["workspace"] == "demonstration"
    status, d = post(f"{base}/api/workspace", {"uploaded": True})
    assert d["workspace"] == "uploaded" and len(d["tables"]) == 2

    restarted = App(CFG, history_dir=Handler.app.history_dir, uploads_dir=Handler.app.uploads_dir)
    assert restarted.workspace_label == "uploaded" and set(restarted.catalog.tables) == {"sales", "shops"}

    req = urllib.request.Request(f"{base}/api/uploads/shops.csv", method="DELETE")
    with urllib.request.urlopen(req) as r:
        assert [t["name"] for t in json.loads(r.read())["tables"]] == ["sales"]
    post(f"{base}/api/workspace", {"demo": True})
