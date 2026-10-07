"""Question types beyond single aggregates. Expected values are computed here independently with plain pandas."""
from decimal import Decimal

import pytest

from app.workflow import Analyst
from tests.conftest import CFG


def run(cat, q):
    return Analyst(cat, CFG).run(q).final


def usd_values(cat):
    o = cat.tables["orders"].drop_duplicates()
    rate = dict(zip(cat.tables["exchange_rates"]["currency"], cat.tables["exchange_rates"]["rate_to_usd"].map(Decimal)))
    return o, [Decimal(a) * rate[c] for a, c in zip(o["amount"], o["currency"])]


# ---------------------------------------------------------------- numeric thresholds
def test_threshold_on_converted_amounts(catalog):
    _, usd = usd_values(catalog)
    f = run(catalog, "What is the revenue in USD for orders over 1000?")
    assert f["status"] == "verified"
    assert f["numeric_value"] == str(sum((v for v in usd if v > 1000), Decimal(0)).quantize(Decimal("0.01")))


def test_threshold_on_a_plain_column(catalog):
    o, _ = usd_values(catalog)
    assert run(catalog, "How many orders have quantity over 3?")["numeric_value"] == sum(int(q) > 3 for q in o["quantity"])
    assert str(run(catalog, "What is the total quantity for orders with quantity of at least 5?")["numeric_value"]) == \
        str(sum(int(q) for q in o["quantity"] if int(q) >= 5))


@pytest.mark.parametrize("q", [
    "How many orders over 1000?",  # amounts in EUR, GBP and USD: '1000' compares different money
    "How many shipments over 5?",  # names no column to compare
])
def test_threshold_that_cannot_be_compared_is_refused(catalog, q):
    assert run(catalog, q)["status"] == "refused"  # once answered 600: the threshold fell on the text id column


# ---------------------------------------------------------------- shares, comparisons, two groupings, anti-joins
def test_share_of_a_sum(catalog):
    o, usd = usd_values(catalog)
    web = sum((v for v, ch in zip(usd, o["channel"]) if ch == "web"), Decimal(0))
    f = run(catalog, "What percentage of revenue in USD comes from the web channel?")
    assert f["numeric_value"] == str((web / sum(usd)).quantize(Decimal("0.0001")))


def test_two_periods_compared(catalog):
    o, usd = usd_values(catalog)
    per = {p: str(sum((v for v, d in zip(usd, o["order_date"]) if d.startswith(p)), Decimal(0)).quantize(Decimal("0.01")))
           for p in ("2024-02", "2024-03")}
    assert run(catalog, "What is the revenue in USD in March 2024 compared to February 2024?")["numeric_value"] == per


def test_two_grouping_columns(catalog):
    o, usd = usd_values(catalog)
    want = {}
    for v, ch, cu in zip(usd, o["channel"], o["currency"]):
        want[f"{ch} · {cu}"] = want.get(f"{ch} · {cu}", Decimal(0)) + v
    f = run(catalog, "What is the revenue in USD by channel and currency?")
    assert f["numeric_value"] == {k: str(v.quantize(Decimal("0.01"))) for k, v in sorted(want.items())}


def test_count_per_related_entity(catalog):
    o, _ = usd_values(catalog)
    names = dict(zip(catalog.tables["customers"]["customer_id"], catalog.tables["customers"]["customer_name"]))
    counts = o.assign(name=o["customer_id"].map(names)).groupby("name")["order_id"].nunique()
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    assert run(catalog, "Which customer placed the most orders?")["numeric_value"] == [list(top)]


def test_rows_with_no_match(make_catalog):
    cat = make_catalog({"customers": "customer_id,name\nC1,A\nC2,B\nC3,C", "orders": "order_id,customer_id\n1,C1\n2,C1\n3,C3"})
    assert run(cat, "How many customers have no orders?")["numeric_value"] == 1


def test_quarters_and_relative_dates(catalog):
    o, _ = usd_values(catalog)
    assert run(catalog, "How many orders were placed in Q1 2024?")["numeric_value"] == \
        o["order_date"].str[:10].between("2024-01-01", "2024-03-31").sum()
    f = Analyst(catalog, CFG, suggest_fixes=True).run("What was the revenue in USD last quarter?").final
    assert f["status"] == "refused" and "today's date" in f["reason"] and f["suggestions"]


def test_synonyms_are_read_and_noted(catalog):
    st = Analyst(catalog, CFG).run("How many clients do we have?")
    assert st.verified and st.final["numeric_value"] == 60
    assert "'clients' read as 'customers' (synonym)" in st.spec.notes
    assert run(catalog, "total profit in usd")["status"] == "refused"  # never a synonym of anything


# ---------------------------------------------------------------- days between two dates
def test_days_between_two_named_dates(make_catalog):
    cat = make_catalog({"orders": "order_id,order_date\n1,2024-01-01\n2,2024-01-10",
                        "deliveries": "delivery_id,order_id,delivered_on\nD1,1,2024-01-04\nD2,2,2024-01-11\nD3,2,2024-01-20"})
    assert run(cat, "What is the average days between order date and delivered on?")["numeric_value"] == "4.67"  # (3 + 1 + 10) / 3
    assert run(cat, "What is the longest time between order date and delivered on?")["numeric_value"] == "10"


def test_days_across_contradictory_dates_are_refused(catalog):
    f = run(catalog, "What is the average days between order date and payment date?")
    assert f["status"] == "refused" and f["answerability"] == "CONTRADICTORY_DATA"  # payments dated before orders
