"""Detect data-quality issues that commonly turn naive analysis into wrong answers.

Issues are facts about the data. Whether an issue blocks a particular question is
decided later by the answerability stage, based on which tables/columns it uses.
"""
import re
from dataclasses import dataclass

import pandas as pd

from .profiling import Relationship, TableProfile
from .security import is_instruction_like

CURRENCY_COL_RE = re.compile(r"(^|_)(currency|ccy|currency_code)$")
UNIT_COL_RE = re.compile(r"(^|_)(unit|units|uom)$")
DERIVED_TABLE_RE = re.compile(r"summary|report|aggregate|rollup|total")
NON_NEGATIVE_RE = re.compile(r"quantity|qty|amount|price|count")


@dataclass
class Issue:
    kind: str
    table: str
    column: str | None
    detail: str
    severity: str = "warning"  # info | warning


def currency_column(df: pd.DataFrame) -> str | None:
    return next((c for c in df.columns if CURRENCY_COL_RE.search(c.lower())), None)


def unit_column_for(df: pd.DataFrame, measure: str) -> str | None:
    units = [c for c in df.columns if UNIT_COL_RE.search(c.lower())]
    return next((u for u in units if u.lower().startswith(measure.lower())), units[0] if len(units) == 1 else None)


def label_variant_pairs(values) -> list[tuple[str, str]]:
    """(short, full) labels that may name the same thing: 'EU' / 'Europe', 'NA' / 'North America'.
    Only an all-caps short label that is the prefix or the initials of another label counts."""
    vals = [v for v in values if v]
    out = []
    for short in vals:
        if not (2 <= len(short) <= 4 and short.isalpha() and short.isupper()):
            continue
        for full in vals:
            words = full.split()
            if full == short or len(full) <= len(short):
                continue
            if full.upper().startswith(short) or "".join(w[0] for w in words).upper() == short:
                out.append((short, full))
    return out


def _date_col(p: TableProfile) -> str | None:
    return next((c for c, cp in p.columns.items() if cp.kind in ("date", "datetime")), None)


def detect_issues(tables: dict[str, pd.DataFrame], profiles: dict[str, TableProfile],
                  rels: list[Relationship], normalized: list | None = None) -> list[Issue]:
    out: list[Issue] = []
    for name, df in tables.items():
        p = profiles[name]
        if p.exact_duplicate_rows:
            out.append(Issue("duplicate_rows", name, None,
                             f"{p.exact_duplicate_rows} exact duplicate row(s); identical records are counted once."))
        if p.duplicate_keys:
            ids = ", ".join(p.duplicate_keys[:5])
            out.append(Issue("conflicting_records", name, p.key,
                             f"{len(p.duplicate_keys)} {p.key} value(s) have conflicting records ({ids})."))
        if DERIVED_TABLE_RE.search(name):
            out.append(Issue("derived_table", name, None,
                             "Pre-aggregated table; not used as a source of truth for record-level metrics.", "info"))
        cur = currency_column(df)
        if cur:
            vals = sorted(set(df[cur]) - {""})
            if len(vals) > 1 and p.columns[cur].unique < p.rows:  # a per-currency lookup table is not mixing
                out.append(Issue("mixed_currency", name, cur, f"Multiple currencies: {', '.join(vals)}."))
        for c, cp in p.columns.items():
            if cp.nulls:
                out.append(Issue("missing_values", name, c, f"{cp.nulls} missing value(s) ({cp.null_pct}%).", "info"))
            if cp.date_format == "ambiguous":
                out.append(Issue("ambiguous_date", name, c,
                                 "Day/month order cannot be determined (no component exceeds 12)."))
            elif cp.date_format == "mixed":
                out.append(Issue("inconsistent_date_format", name, c, "Dates use inconsistent or invalid formats."))
            if cp.kind == "datetime" and cp.has_timezone is False:
                out.append(Issue("timezone_unspecified", name, c, "Timestamps carry no timezone.", "info"))
            if cp.currency_symbols:
                out.append(Issue("currency_symbols", name, c, "Values embed currency symbols; not numeric as stored."))
            if cp.kind in ("integer", "decimal") and NON_NEGATIVE_RE.search(c.lower()) and float(cp.min) < 0:
                out.append(Issue("negative_values", name, c, f"Negative values present (min {cp.min})."))
            if UNIT_COL_RE.search(c.lower()) and 1 < cp.unique <= 20:
                out.append(Issue("mixed_units", name, c, f"Multiple units: {', '.join(sorted(cp.top_values))}."))
            if cp.kind == "text" and cp.top_values and (pairs := label_variant_pairs(cp.top_values)):
                shown = "; ".join(f"'{a}' / '{b}'" for a, b in pairs[:3])
                out.append(Issue("label_variants", name, c, f"Labels that may name the same thing: {shown}."))
            if cp.kind == "text":
                hits = int(df[c].map(is_instruction_like).sum())
                if hits:
                    out.append(Issue("prompt_injection", name, c,
                                     f"{hits} value(s) contain instruction-like text; treated strictly as data."))
    for t, c, what in normalized or []:
        out.append(Issue("normalized", t, c, what + ".", "info"))
    for r in rels:
        if r.unmatched:
            out.append(Issue("orphan_keys", r.child, r.column,
                             f"{r.unmatched} {r.column} value(s) have no matching row in {r.parent}."))
        out += _temporal_conflicts(tables, profiles, r)
    return out


def _temporal_conflicts(tables, profiles, r: Relationship) -> list[Issue]:
    # ponytail: assumes child events (payment, shipment) should not precede the parent event (order);
    # surfaced as a warning only, never used to drop rows.
    cd, pd_ = _date_col(profiles[r.child]), _date_col(profiles[r.parent])
    if not cd or not pd_ or profiles[r.child].columns[cd].date_format != "iso" \
            or profiles[r.parent].columns[pd_].date_format != "iso":
        return []
    parent = tables[r.parent].drop_duplicates(r.column)[[r.column, pd_]]
    m = tables[r.child][[r.column, cd]].merge(parent, on=r.column, suffixes=("", "_parent"))
    n = int((m[cd].str[:10] < m[pd_ if pd_ != cd else f"{cd}_parent"].str[:10]).sum())
    if not n:
        return []
    return [Issue("temporal_contradiction", r.child, cd, f"{n} row(s) dated before their {r.parent} {pd_}.")]
