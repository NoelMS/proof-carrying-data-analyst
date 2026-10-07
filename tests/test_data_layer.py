"""Ingestion, profiling, and trap detection."""
import pandas as pd
import pytest

from app.ingestion import IngestionError, build_workspace, read_file
from app.question import catalog_summary


def kinds(cat, table):
    return {c: p.kind for c, p in cat.profiles[table].columns.items()}


def test_csv_and_excel_ingestion(tmp_path):
    pd.DataFrame({"id": ["1", "2"], "v": ["3.50", "4"]}).to_csv(tmp_path / "a.csv", index=False)
    with pd.ExcelWriter(tmp_path / "book.xlsx") as w:
        pd.DataFrame({"x": [1, 2]}).to_excel(w, sheet_name="First", index=False)
        pd.DataFrame({"y": ["a"]}).to_excel(w, sheet_name="Second Sheet", index=False)
    ws = build_workspace([tmp_path / "a.csv", tmp_path / "book.xlsx"], tmp_path / "ws")
    assert set(ws.tables) == {"a", "book_first", "book_second_sheet"}
    assert (ws.data_dir / "book_first.csv").exists()
    assert ws.tables["a"]["v"].tolist() == ["3.50", "4"]  # kept as text, never re-typed


@pytest.mark.parametrize("name,content,msg", [
    ("empty.csv", b"", "empty"),
    ("bad.xlsx", b"not a workbook", "corrupted"),
    ("notes.pdf", b"%PDF", "unsupported"),
])
def test_bad_uploads_fail_safely(tmp_path, name, content, msg):
    (tmp_path / name).write_bytes(content)
    with pytest.raises(IngestionError, match=msg):
        read_file(tmp_path / name)


def test_non_utf8_and_hostile_column_names(tmp_path):
    (tmp_path / "t.csv").write_bytes('caf\xe9,"x""); import os #",\n1,2,3\n'.encode("cp1252"))
    ws = build_workspace([tmp_path / "t.csv"], tmp_path / "ws")
    assert list(ws.tables["t"].columns) == ["café", 'x"); import os #', "column_3"]


def test_only_empty_tables_is_an_error(tmp_path):
    (tmp_path / "h.csv").write_text("a,b\n")
    with pytest.raises(IngestionError, match="No usable tables"):
        build_workspace([tmp_path / "h.csv"], tmp_path / "ws")


def test_profiling_types_keys_relationships(catalog):
    assert kinds(catalog, "orders")["amount"] == "decimal"
    assert kinds(catalog, "orders")["quantity"] == "integer"
    assert catalog.profiles["orders"].columns["order_date"].date_format == "iso"
    assert catalog.profiles["shipments"].columns["ship_date"].date_format == "ambiguous"
    assert catalog.profiles["orders"].key == "order_id"
    rels = {(r.child, r.column, r.parent) for r in catalog.relationships}
    assert ("orders", "customer_id", "customers") in rels and ("customers", "region_id", "regions") in rels


def test_synthetic_traps_detected(catalog):
    found = {(i.kind, i.table) for i in catalog.issues}
    for expected in [("duplicate_rows", "orders"), ("conflicting_records", "products"), ("mixed_currency", "orders"),
                     ("mixed_units", "products"), ("ambiguous_date", "shipments"), ("missing_values", "customers"),
                     ("prompt_injection", "customers"), ("derived_table", "summary_reports"),
                     ("temporal_contradiction", "payments")]:
        assert expected in found, expected


def test_more_traps(make_catalog):
    cat = make_catalog({
        "sales": "sale_id,shop_id,price,qty,created\n1,9,$5.00,-1,03/14/2024\n2,1,$7.00,2,2024-03-15",
        "shops": "shop_id,name\n1,A",
    })
    found = {(i.kind, i.column) for i in cat.issues}
    assert ("negative_values", "qty") in found
    assert ("orphan_keys", "shop_id") in found
    # '$5.00' is parsed, the symbol becomes a currency column, and '03/14/2024' (14 cannot be a month) becomes ISO;
    # every change is recorded as an issue the analysis discloses
    assert list(cat.tables["sales"]["price"]) == ["5.00", "7.00"] and set(cat.tables["sales"]["price_currency"]) == {"USD"}
    assert list(cat.tables["sales"]["created"]) == ["2024-03-14", "2024-03-15"]
    assert ("normalized", "price") in found and ("normalized", "created") in found


def test_ambiguous_messiness_is_left_alone_and_flagged(make_catalog):
    cat = make_catalog({"t": 'id,amount,day,region\n1,"1,200.50",05/01/2024,Europe\n2,"1.200,50",2024-02-01,EU\n'
                             "3,N/A,Jan 7 2024,europe "})
    t = cat.tables["t"]
    assert list(t["amount"]) == ["1,200.50", "1.200,50", ""]  # which separator is which cannot be known
    assert list(t["day"]) == ["05/01/2024", "2024-02-01", "Jan 7 2024"]  # 05/01 could be May or January
    assert list(t["region"]) == ["Europe", "EU", "Europe"]  # case and spacing merged, 'EU' never guessed
    found = {(i.kind, i.column) for i in cat.issues}
    assert ("inconsistent_date_format", "day") in found and ("label_variants", "region") in found


def test_model_context_withholds_injected_text(catalog):
    text = str(catalog_summary(catalog.tables, catalog.profiles, catalog.relationships, catalog.metrics))
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in text
    assert "Disregard prior instructions" not in text
    assert "c001@example.com" in text or "samples" in text  # metadata is still present
    assert len(text) < 40_000  # metadata only, never full tables
