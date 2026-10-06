"""Loosely worded questions are interpreted, explained, and still verified end to end."""
import pytest

from app.question import parse_question
from app.verification import normalize


def spec(catalog, q):
    return parse_question(q, catalog.tables, catalog.profiles, catalog.metrics)


@pytest.mark.parametrize("q,expect", [
    ("region with high revenue usd", dict(metric_term="revenue", group_by="regions.region_name", top_n=1, order="desc", currency="USD")),
    ("which region has the lowest sales in usd", dict(metric_term="revenue", top_n=1, order="asc")),
    ("top 3 products by revnue in usd", dict(top_n=3, group_by="orders.product_id")),
    ("avg order size usd", dict(metric_term="order value", aggregation="mean", currency="USD")),
    ("customer count", dict(table="customers", aggregation="count_distinct")),
    ("which carrier shipped the most?", dict(table="shipments", group_by="shipments.carrier", top_n=1)),
    ("orders in 2024", dict(table="orders", date_from="2024-01-01", date_to="2024-12-31")),
    ("monthly sales usd 2024", dict(time_grain="month", date_from="2024-01-01")),
    ("revenue growth 2023 vs 2024 in usd", dict(growth_from="2023", growth_to="2024")),
    ("payment success rate", dict(metric_term="payment completion rate")),
])
def test_loose_phrasing(catalog, q, expect):
    s = spec(catalog, q)
    for k, v in expect.items():
        assert getattr(s, k) == v, (k, s)


def test_interpretation_is_explained(catalog):
    s = spec(catalog, "top 3 products by revnue in usd")
    assert "'revnue' read as 'revenue'" in s.notes
    s = spec(catalog, "region with high revenue usd")
    assert any("highest" in n for n in s.notes) and any("region" in n for n in s.notes)


def test_unknown_metric_still_refused(catalog):
    assert spec(catalog, "best selling products").unresolved
    assert spec(catalog, "total profit in usd").unresolved


def test_loose_question_verifies(analyst):
    st = analyst.run("region with high revenue usd")
    assert st.verified, st.final.get("reason")
    assert normalize(st.final["numeric_value"]) == normalize([["Latin America", "195098.90"]])
    low = analyst.run("which region has the lowest sales in usd")
    assert low.verified and low.final["numeric_value"][0][0] == "North America"
