"""Data fixes: proposed, previewed, applied only on confirm, logged, and reversible."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.api import App
from app.fixes import FixError
from tests.conftest import CASES, CFG

MESSY_CASES = json.loads((Path(__file__).resolve().parent.parent / "data" / "messy" / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture
def app(tmp_path):
    return App(CFG, history_dir=tmp_path / "history", uploads_dir=tmp_path / "pcda" / "uploads")


def option(app, kind, table):
    return next(o for o in app.fixes()["options"] if o["kind"] == kind and o["table"] == table)


def test_preview_changes_nothing_until_applied(app):
    opt = option(app, "ambiguous_date", "shipments")
    with pytest.raises(FixError):
        app.preview_fix(opt["id"], None, None)  # the reading must be chosen by the user
    p = app.preview_fix(opt["id"], "dmy", None)
    assert p["changed"]["count"] == 462 and p["removed"]["count"] == 0
    first = p["changed"]["cells"][0]
    assert first["column"] == "ship_date" and first["before"] == "12/09/2024" and first["after"] == "2024-09-12"
    assert app.catalog.tables["shipments"]["ship_date"].iloc[0] == "12/09/2024"  # still unchanged
    assert app.fixes()["log"] == []


def test_apply_then_rollback_and_restore(app):
    q = "How many shipments were made in March 2024?"
    assert app.catalog.profiles["shipments"].columns["ship_date"].date_format == "ambiguous"
    opt = option(app, "ambiguous_date", "shipments")
    p = app.preview_fix(opt["id"], "dmy", None)
    entry = app.apply_fix(opt["id"], "dmy", None, p["token"])
    assert entry["summary"] == "0 row(s) removed, 462 cell(s) changed"
    assert app.catalog.profiles["shipments"].columns["ship_date"].date_format == "iso"
    assert (app.catalog.workspace.data_dir / "shipments.csv").read_text().count("/") == 0  # sandbox sees the fix
    from app.workflow import Analyst
    assert Analyst(app.catalog, CFG).run(q).verified  # was refused as ambiguous before the fix

    restarted = App(CFG, history_dir=app.history_dir, uploads_dir=app.uploads_dir)  # fixes persist
    assert restarted.catalog.profiles["shipments"].columns["ship_date"].date_format == "iso"

    dup = option(app, "duplicate_rows", "orders")
    app.apply_fix(dup["id"], None, None, app.preview_fix(dup["id"], None, None)["token"])
    assert app.catalog.profiles["orders"].rows == 600
    app.rollback_fix(app.fixes()["log"][-1]["id"])
    assert app.catalog.profiles["orders"].rows == 612
    assert app.restore_table("shipments") == 1
    assert app.catalog.profiles["shipments"].columns["ship_date"].date_format == "ambiguous"
    assert app.fixes()["log"] == []


def test_refusal_suggests_fixes_that_unblock_it(app):
    from app.workflow import Analyst
    q = "How many shipments were made in March 2024?"
    st = Analyst(app.catalog, CFG, suggest_fixes=True).run(q)
    assert st.final["status"] == "refused"
    found = st.final["fixes"]
    assert {(x["fix"]["kind"], x["choice"], x["answerable"]) for x in found} == \
        {("ambiguous_date", "dmy", True), ("ambiguous_date", "mdy", True)}
    assert app.catalog.profiles["shipments"].columns["ship_date"].date_format == "ambiguous"  # nothing applied
    assert app.fixes()["log"] == []

    opt = found[0]["fix"]
    app.apply_fix(opt["id"], "dmy", None, app.preview_fix(opt["id"], "dmy", None)["token"])
    assert Analyst(app.catalog, CFG).run(q).verified


def test_suggestions_are_relevant_and_report_what_remains(app):
    from app.workflow import Analyst
    a = Analyst(app.catalog, CFG, suggest_fixes=True)
    weight = a.run("What is the total product weight?").final["fixes"]
    assert {x["fix"]["kind"] for x in weight} == {"conflicting_records", "mixed_units"}
    assert all(not x["answerable"] and x["remaining"] for x in weight)  # each clears one of two blockers
    assert a.run("What is the total revenue?").final["fixes"] == []  # currency ambiguity has no data fix
    assert "fixes" not in Analyst(app.catalog, CFG).run("What is the total revenue?").final  # off by default


def test_rollback_order_and_stale_preview(app):
    c = option(app, "conflicting_records", "products")
    m = option(app, "mixed_units", "products")
    stale = app.preview_fix(m["id"], "kg", None)  # previewed before the table changes
    p = app.preview_fix(c["id"], "first", None)
    assert p["removed"]["count"] == 1 and p["removed"]["labels"] == ["P07"]
    app.apply_fix(c["id"], "first", None, p["token"])
    with pytest.raises(FixError, match="changed since this preview"):
        app.apply_fix(m["id"], "kg", None, stale["token"])
    with pytest.raises(FixError, match="no longer present"):
        app.apply_fix(c["id"], "first", None, p["token"])
    first = app.fixes()["log"][0]["id"]
    app.apply_fix(m["id"], "kg", None, app.preview_fix(m["id"], "kg", None)["token"])
    assert set(app.catalog.tables["products"]["weight_unit"]) == {"kg"}
    with pytest.raises(FixError, match="later change"):
        app.rollback_fix(first)


def test_unit_conversion_is_exact(app):
    m = option(app, "mixed_units", "products")
    p = app.preview_fix(m["id"], "kg", None)
    lb = [c for c in p["changed"]["cells"] if c["column"] == "weight"]
    df = app.catalog.tables["products"]
    src = df[df["weight_unit"] == "lb"]["weight"].iloc[0]
    from decimal import Decimal
    assert Decimal(lb[0]["after"]) == (Decimal(src) * Decimal("0.45359237")).quantize(Decimal("0.000001")).normalize()


def test_rerun_after_restart_rebuilds_plan(app, tmp_path):
    rid = app.start("What is the total revenue in USD?", None)
    run = app.runs[rid]
    with run.cond:
        while not run.done:
            run.cond.wait(1)
    fresh = App(CFG, history_dir=app.history_dir, uploads_dir=app.uploads_dir)
    r = fresh.verify_code(rid)
    assert r["verification"]["status"] == "verified" and r["matches_shown_result"]
    bad = fresh.verify_code(rid, "print('RESULT: \"1.00\"')")
    assert bad["verification"]["status"] == "failed"
    z = zipfile.ZipFile(io.BytesIO(fresh.bundle(rid)))
    assert {"proof.py", "data/orders.csv", "data/exchange_rates.csv", "requirements.txt", "README.txt"} <= set(z.namelist())


def test_workbench_run(app):
    out = app.run_code('import pandas as pd\nprint("RESULT:", len(pd.read_csv("data/regions.csv")))', ["regions"])
    assert out["execution"]["status"] == "ok" and out["execution"]["result"] == 5
    assert app.run_code("import os", [])["execution"]["status"] == "policy_rejected"
    with pytest.raises(LookupError):
        app.run_code("print(1)", ["nope"])


def test_benchmark_progress(app):
    app.start_benchmark()
    with pytest.raises(LookupError):
        app.start_benchmark()  # one at a time
    seen_current = False
    import time
    while app.bench["running"]:
        seen_current |= bool(app.bench.get("current") and app.bench["current"]["stage"])
        time.sleep(0.05)
    assert seen_current and app.bench["done"] == app.bench["total"] == len(CASES) + len(MESSY_CASES) and not app.bench["error"]
    assert app.bench["report"]["metrics"]["refusal_accuracy"] == 1.0
    assert app.bench["report"]["metrics"]["confident_wrong_rate"] == 0.0
    assert all("duration_s" in r and "attempts" in r for r in app.bench["rows"])
    json.dumps({k: v for k, v in app.bench.items() if k != "report"})  # serialisable for the API


def test_benchmark_cancel_keeps_last_complete_results(app):
    import time
    app.start_benchmark()
    while not app.bench["rows"]:
        time.sleep(0.05)
    app.cancel_benchmark()
    while app.bench["running"]:
        time.sleep(0.05)
    assert app.bench["cancelled"] and 0 < app.bench["done"] < app.bench["total"]
    assert app.bench["report"]["cancelled"] and app.bench["report"]["cases"] == app.bench["done"]
    assert not app.results_file.exists()  # a cancelled run never replaces saved metrics
    with pytest.raises(LookupError):
        app.cancel_benchmark()  # nothing running


def test_cases_are_mixed():
    flags = [c["answerable"] for c in CASES]
    longest = max(len(list(g)) for _, g in __import__("itertools").groupby(flags))
    assert longest <= 2 and any(flags) and not all(flags)


def test_suggestion_failure_keeps_the_refusal(app, monkeypatch):
    import app.workflow as wf
    monkeypatch.setattr(wf, "unblocking_fixes", lambda *a: 1 / 0)
    f = wf.Analyst(app.catalog, CFG, suggest_fixes=True).run("How many shipments were made in March 2024?").final
    assert f["status"] == "refused" and f["answerability"] == "AMBIGUOUS" and f["fixes"] == []


def test_both_workspace_keeps_demo_tables_and_routes_fixes(app):
    # an upload named like a demonstration table is renamed, never shadows it
    app.add_uploads([("orders.csv", b"order_id,age\n1,63\n1,63\n2,37\n")])
    assert app.workspace_label == "both"
    assert {"orders", "orders_upload", "shipments"} <= set(app.catalog.tables)
    assert app.catalog.profiles["orders"].rows == 612 and app.catalog.profiles["orders_upload"].rows == 3

    dup = option(app, "duplicate_rows", "orders_upload")
    app.apply_fix(dup["id"], None, None, app.preview_fix(dup["id"], None, None)["token"])
    date = option(app, "ambiguous_date", "shipments")
    app.apply_fix(date["id"], "dmy", None, app.preview_fix(date["id"], "dmy", None)["token"])
    assert sorted(e["table"] for e in app.fixes()["log"]) == ["orders_upload", "shipments"]
    assert [e["table"] for e in app.fix_store("uploaded").log()] == ["orders"]  # stored under the upload's own name
    assert [e["table"] for e in app.fix_store("demonstration").log()] == ["shipments"]

    app.use_uploads()  # each fix is seen in its own workspace too
    assert app.catalog.profiles["orders"].rows == 2
    app.use_both()
    app.rollback_fix(next(e["id"] for e in app.fixes()["log"] if e["table"] == "orders_upload"))
    assert app.catalog.profiles["orders_upload"].rows == 3
    assert app.restore_table("shipments") == 1 and app.fixes()["log"] == []
    restarted = App(CFG, history_dir=app.history_dir, uploads_dir=app.uploads_dir)
    assert restarted.workspace_label == "both" and "orders_upload" in restarted.catalog.tables
