"""End-to-end: question -> verified answer or refusal, including repair and adversarial paths."""
import json

import pytest

from app.codegen import pandas_proof
from app.llm import LLMError
from app.question import QuerySpec
from app.sandbox import Sandbox
from app.verification import normalize
from app.workflow import REFUSAL, Analyst
from tests.conftest import CASES, CFG


@pytest.mark.parametrize("case", CASES, ids=[c["question"][:50] for c in CASES])
def test_ground_truth(analyst, case):
    st = analyst.run(case["question"])
    f = st.final
    if case["answerable"]:
        assert f["status"] == "verified", f.get("reason")
        assert normalize(f["numeric_value"]) == normalize(case["expected"])
        assert f["verification"]["status"] == "verified" and f["verification"]["match"]
        # the shown proof is executable on its own and reproduces the answer
        r = analyst.sandbox.run(f["proof_code"], analyst.cat.workspace.data_dir, st.plan.tables)
        assert r.ok and normalize(r.result) == normalize(case["expected"])
    else:
        assert f["status"] == "refused" and f["answer"] == REFUSAL and f["reason"]
        assert f["answerability"] == case["expected_refusal"], f["reason"]
        assert f["proof_code"] is None and "numeric_value" not in f


def test_refusal_reasons_are_specific(analyst):
    reasons = {q: analyst.run(q).final for q in [
        "What is the total revenue?", "What is the total revenue in JPY?",
        "How many shipments were made in March 2024?", "What is the revenue in USD by product category?"]}
    assert reasons["What is the total revenue?"]["answerability"] == "AMBIGUOUS"
    assert "JPY" in reasons["What is the total revenue in JPY?"]["reason"]
    assert "day/month" in reasons["How many shipments were made in March 2024?"]["reason"]
    assert reasons["What is the revenue in USD by product category?"]["answerability"] == "CONTRADICTORY_DATA"


def test_prompt_injection_in_data_is_inert(analyst):
    st = analyst.run("How many customers are there?")
    assert st.verified and st.final["numeric_value"] == 60
    assert any("instruction-like" in d for d in st.final["diagnostics"])
    dumped = json.dumps(st.final)
    assert "IGNORE ALL PREVIOUS" not in dumped and "system prompt" not in dumped.lower()


def test_failed_code_is_repaired_and_reverified(catalog):
    def writer(plan, attempt, feedback):
        if attempt == 1:
            return "import pandas as pd\nraise RuntimeError('broken first draft')\n", "supplied"
        assert "broken first draft" in feedback
        return pandas_proof(plan), "template"
    st = Analyst(catalog, CFG, code_writer=writer).run("What is the total revenue in USD?")
    assert st.verified and len(st.attempts) == 2
    assert st.attempts[0].execution.status == "runtime_error"
    assert st.final["numeric_value"] == "815497.70"


def test_result_disagreeing_with_check_triggers_repair(catalog):
    def writer(plan, attempt, feedback):
        code = pandas_proof(plan)
        if attempt == 1:  # naive analysis: keeps the duplicate order rows
            return code.replace("t_orders = t_orders.drop_duplicates()", ""), "supplied"
        return code, "template"
    st = Analyst(catalog, CFG, code_writer=writer).run("What is the total revenue in USD?")
    first = st.attempts[0].verification
    assert not first.passed and first.executed_value != first.independent_value
    assert st.verified and st.final["numeric_value"] == "815497.70"


def test_repairs_are_bounded_then_refuse(catalog):
    calls = []

    def writer(plan, attempt, feedback):
        calls.append(attempt)
        return "print('RESULT: 1')\n", "supplied"
    st = Analyst(catalog, CFG, code_writer=writer).run("What is the total revenue in USD?")
    assert calls == [1, 2, 3]  # first attempt + max_repairs (2)
    assert st.final["status"] == "refused" and st.final["answerability"] == "VERIFICATION_FAILED"


def test_wrong_claim_is_never_shown_as_verified(make_catalog):
    cat = make_catalog({"sales": "sale_id,amount\n1,500\n2,750"})
    st = Analyst(cat, CFG).run("What is the total amount?", claimed_value=1000)
    assert st.final["status"] == "refused" and "1250" in st.final["reason"]
    assert Analyst(cat, CFG).run("What is the total amount?", claimed_value="1250").verified


@pytest.mark.parametrize("tables,question,status,fragment", [
    ({"orders": "order_id,amount,currency\n1,10.00,EUR\n2,5.00,USD"}, "What is the total amount in USD?",
     "INSUFFICIENT_DATA", "exchange rates"),
    ({"orders": "order_id,amount\n1,10\n2,\n3,4"}, "What is the total amount?", "INSUFFICIENT_DATA", "missing"),
    ({"orders": "order_id,amount,order_date\n1,10,03/04/2024\n2,5,05/06/2024"},
     "What is the total amount in 2024?", "AMBIGUOUS", "day/month"),
    ({"orders": "order_id,shop_id,amount\n1,s1,10\n2,s1,5", "shops": "shop_id,shop_name\ns1,North\ns1,South"},
     "What is the total amount by shop name?", "CONTRADICTORY_DATA", "conflicting"),
    ({"orders": "order_id,shop_id,amount\n1,s1,10\n2,s9,5", "shops": "shop_id,shop_name\ns1,North"},
     "What is the total amount by shop name?", "INSUFFICIENT_DATA", "no match"),
    ({"parcels": "parcel_id,weight,weight_unit\n1,2,kg\n2,3,lb"}, "What is the total weight?",
     "UNSUPPORTED_OPERATION", "units"),
    ({"orders": "order_id,amount\n1,10\n2,5"}, "What is the total margin?", "INSUFFICIENT_DATA", "margin"),
])
def test_traps_lead_to_refusal(make_catalog, tables, question, status, fragment):
    st = Analyst(make_catalog(tables), CFG).run(question)
    assert st.final["status"] == "refused"
    assert st.final["answerability"] == status
    assert fragment in (st.final["reason"] + " ".join(st.final["also"]))


def test_supported_conversion_and_join(make_catalog):
    cat = make_catalog({
        "orders": "order_id,shop_id,amount,currency\n1,s1,10.00,EUR\n2,s2,5.50,USD\n3,s1,1.25,USD",
        "shops": "shop_id,shop_name\ns1,North\ns2,South",
        "fx": "currency,rate_to_usd\nEUR,1.1000\nUSD,1",
    })
    st = Analyst(cat, CFG).run("What is the total amount in USD by shop name?")
    assert st.verified, st.final.get("reason")
    assert st.final["numeric_value"] == {"North": "12.25", "South": "5.50"}


class FakeModel:
    """Stands in for the Claude client: returns fixed specs / code."""
    def __init__(self, spec=None, codes=(), fail=False):
        self.spec, self.codes, self.fail = spec, list(codes), fail

    def interpret(self, question, catalog):
        if self.fail:
            raise LLMError("model unreachable")
        return self.spec

    def write_code(self, plan_text, feedback=None):
        if not self.codes:
            raise LLMError("no more drafts")
        return self.codes.pop(0)


def test_model_spec_with_invented_column_is_refused(catalog):
    a = Analyst(catalog, CFG)
    a.llm = FakeModel(QuerySpec(metric_term="profit", table="orders", measure="profit"))
    st = a.run("What is the total profit?")
    assert st.final["status"] == "refused" and "profit" in st.final["reason"]


def test_hostile_model_code_is_rejected_then_repaired(catalog):
    a = Analyst(catalog, CFG)
    hostile = "import os\nprint('RESULT: ' + str(len(os.environ)))\n"
    a.llm = FakeModel(QuerySpec(metric_term="revenue", table="orders", measure="amount", currency="USD"), [hostile])
    st = a.run("What is the total revenue in USD?")
    assert st.attempts[0].execution.status == "policy_rejected" and st.attempts[0].source == "model"
    assert st.attempts[1].source == "template" and st.verified


def test_model_outage_falls_back_visibly(catalog):
    a = Analyst(catalog, CFG)
    a.llm = FakeModel(fail=True)
    st = a.run("How many customers are there?")
    assert st.verified and "model unavailable" in st.interpreter


def test_stage_log_is_structured(analyst):
    st = analyst.run("What is the total revenue in USD?")
    stages = [e["stage"] for e in st.stages]
    assert stages == ["interpret", "assess", "plan", "generate", "execute", "verify", "answer", "complete"]
    assert all("request_id" in e for e in st.stages)
    assert "815497" not in json.dumps(st.stages[:-1])  # values are not logged per stage


def test_sandbox_used_for_every_execution(catalog, monkeypatch):
    calls = []
    real = Sandbox.run
    monkeypatch.setattr(Sandbox, "run", lambda self, *a, **k: calls.append(k.get("trusted", False)) or real(self, *a, **k))
    Analyst(catalog, CFG).run("What is the total revenue in USD?")
    assert calls == [False, False, True]  # proof, reproduction, independent check
