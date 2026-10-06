"""The verifier rejects every way a proof can be wrong."""
import pytest

from app.answerability import assess
from app.codegen import duckdb_check, pandas_proof
from app.config import Config
from app.planning import build_plan
from app.question import parse_question
from app.sandbox import Sandbox
from app.verification import verify

SB = Sandbox(Config(timeout_s=20))


@pytest.fixture(scope="module")
def plan_for(catalog):
    def make(q):
        spec = parse_question(q, catalog.tables, catalog.profiles, catalog.metrics)
        a = assess(spec, catalog)
        assert a.answerable, a.reasons
        return build_plan(q, spec, a, catalog)
    return make


def check(catalog, plan, code, claimed=None):
    ex = SB.run(code, catalog.workspace.data_dir, plan.tables)
    return verify(plan, code, ex, SB, catalog.workspace.data_dir, claimed)


def failed(v):
    return {c.name for c in v.checks if not c.passed}


def test_template_proof_verifies(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    v = check(catalog, plan, pandas_proof(plan))
    assert v.passed and v.reproduced and v.executed_value == v.independent_value == "815497.70"
    assert v.record()["status"] == "verified"


def test_claim_1000_vs_executed_1250_is_rejected(make_catalog):
    cat = make_catalog({"sales": "sale_id,amount\n1,500\n2,750"})
    q = "What is the total amount?"
    spec = parse_question(q, cat.tables, cat.profiles, cat.metrics)
    plan = build_plan(q, spec, assess(spec, cat), cat)
    v = check(cat, plan, pandas_proof(plan), claimed=1000)
    assert v.executed_value == "1250"
    assert failed(v) == {"claim_matches_execution"}
    rec = v.record()
    assert rec["status"] == "failed" and rec["match"] is False and rec["claimed_value"] == 1000


def test_naive_sum_with_duplicates_disagrees(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    code = pandas_proof(plan).replace("t_orders = t_orders.drop_duplicates()", "")
    assert "independent_recomputation" in failed(check(catalog, plan, code))


def test_wrong_operation_disagrees(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    code = pandas_proof(plan).replace("return sum(values, Decimal(0))", "return max(values)")
    assert "independent_recomputation" in failed(check(catalog, plan, code))


def test_wrong_dataset_fails(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    code = pandas_proof(plan).replace("data/orders.csv", "data/summary_reports.csv")
    assert {"execution"} <= failed(check(catalog, plan, code))  # file was never provided to the sandbox


def test_hardcoded_answer_rejected(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    code = pandas_proof(plan) + '\nresult = "815497.70"\nprint("RESULT: " + json.dumps(result))\n'
    code = code.replace('print("RESULT: " + json.dumps(result))\n', "", 1)
    assert "no_hardcoded_result" in failed(check(catalog, plan, code))


def test_exception_suppression_rejected(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    body = pandas_proof(plan).split("\n")
    code = "\n".join(body[:-2] + ["try:", "    pass", "except Exception:", "    pass", body[-2], ""])
    assert "no_exception_suppression" in failed(check(catalog, plan, code))


def test_unsupported_precision_rejected(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    code = pandas_proof(plan).replace('Decimal("1e-2")', 'Decimal("1e-6")')
    assert "precision" in failed(check(catalog, plan, code))


def test_no_output_rejected(catalog, plan_for):
    plan = plan_for("What is the total revenue in USD?")
    code = pandas_proof(plan).replace('print("RESULT: " + json.dumps(result))', "")
    v = check(catalog, plan, code)
    assert failed(v) == {"execution"} and "RESULT" in v.checks[0].detail


def test_independent_check_is_different_code(plan_for):
    plan = plan_for("What is the revenue in USD by region?")
    proof, sql = pandas_proof(plan), duckdb_check(plan)
    assert "import duckdb" in sql and "duckdb" not in proof
    assert "LEFT JOIN t_customers" in sql and "validate='many_to_one'" in proof
