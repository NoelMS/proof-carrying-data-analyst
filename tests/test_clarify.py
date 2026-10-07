"""Vague questions are still refused, but come with rewritten questions the data can answer."""
import pytest

from app.workflow import Analyst
from tests.conftest import CFG


def run(catalog, q):
    return Analyst(catalog, CFG).run(q).final


@pytest.mark.parametrize("q,expected", [
    ("What is the total revenue?", "What is the total revenue in USD?"),
    ("What is the revenue in dollars?", "What is the revenue in USD?"),
    ("What is the total payment amount in USD?", "What is the total payment amount in USD for completed payments?"),
    ("What is the revenue in USD from web or partner?", "What is the revenue in USD from web?"),
    ("Is revenue higher in 2024 than 2023?", "What was the revenue growth from 2023 to 2024 in USD?"),
    ("How much did customer 001 spend?", "How much did customer C001 spend in USD?"),
    ("What is the total revenue in USD excluding refunds?", "What is the total revenue in USD?"),
    ("Which region generated the highest income in INR", "Which region generated the highest income in USD"),
    ("What is the total revenue in yen?", "What is the total revenue in USD?"),  # no rate to JPY: offer USD
])
def test_vague_question_is_refused_with_answerable_rewrites(catalog, q, expected):
    f = run(catalog, q)
    assert f["status"] == "refused"  # the refusal itself is unchanged
    assert expected in [s["question"] for s in f["suggestions"]]
    for s in f["suggestions"]:  # every suggestion really is answerable and verifies
        assert Analyst(catalog, CFG).run(s["question"]).verified, s


@pytest.mark.parametrize("q", [
    "What is the revenue in USD not from web?",  # dropping 'not' would invert the question
    "How many orders were not shipped?",
    "Why did revenue drop in 2024?",  # an explanation, not a number
    "What will revenue be in 2026?",  # a forecast
    "What is the revenue in USD by product category?",  # contradictory data: a fix, not a rewrite
])
def test_no_rewrite_that_changes_the_meaning(catalog, q):
    assert run(catalog, q)["suggestions"] == []


def test_unknown_metric_gets_a_hint(catalog):
    f = run(catalog, "What is the total profit in USD?")
    assert f["suggestions"] == [] and "revenue" in f["hint"]


def test_breakdown_by_currency_needs_no_conversion(catalog):
    st = Analyst(catalog, CFG).run("What is the total revenue by order currency?")
    assert st.verified and set(st.final["numeric_value"]) == {"EUR", "GBP", "USD"}
