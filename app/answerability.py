"""Decide whether a QuerySpec can be answered reliably, and resolve how.

Every check either resolves a decision from evidence in the data (recorded as a
diagnostic) or blocks the question with a specific reason. Nothing is assumed.
"""
import re
from collections import deque
from dataclasses import dataclass, field

from .catalog import Catalog
from .question import QuerySpec
from .traps import DERIVED_TABLE_RE, currency_column, unit_column_for

ANSWERABLE = "ANSWERABLE"
AMBIGUOUS = "AMBIGUOUS"
INSUFFICIENT = "INSUFFICIENT_DATA"
CONTRADICTORY = "CONTRADICTORY_DATA"
UNSUPPORTED = "UNSUPPORTED_OPERATION"
MINOR_UNITS = {"JPY": 0, "KRW": 0}  # ISO 4217 exponents that differ from 2
MAX_SOURCE_DECIMALS = 6
MEAN_MIN_DECIMALS = 2  # an average of whole numbers is rarely whole; rounding it to 0 places would hide that
MONEY_RE = re.compile(r"amount|price|revenue|cost|sales|spend|fee|paid|payment|income|profit|margin|balance|tax|"
                      r"discount|value|total|salary|wage|charge|refund")
STATUS_RE = re.compile(r"(.*_)?(status|state)")


@dataclass
class Assessment:
    status: str = ANSWERABLE
    reasons: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    joins: list[tuple[str, str, str]] = field(default_factory=list)  # (child, column, parent), many-to-one
    dedupe: list[str] = field(default_factory=list)
    currency: dict | None = None  # source_column, target, rates_table, rates_key, rate_column (or None = no conversion)
    unit: str | None = None
    precision: int = 0

    def block(self, status: str, reason: str):
        if self.status == ANSWERABLE:
            self.status = status
        self.reasons.append(reason)

    @property
    def answerable(self) -> bool:
        return self.status == ANSWERABLE


def _split(ref: str) -> tuple[str, str]:
    t, _, c = ref.partition(".")
    return t, c


def _join_path(cat: Catalog, base: str, target: str) -> list[tuple[str, str, str]] | None:
    """Breadth-first search along child -> parent (many-to-one) relationships only."""
    prev, queue = {base: None}, deque([base])
    while queue:
        t = queue.popleft()
        if t == target:
            path = []
            while prev[t]:
                path.append(prev[t])
                t = prev[t][0]
            return path[::-1]
        for r in cat.relationships:
            if r.child == t and r.parent not in prev:
                prev[r.parent] = (r.child, r.column, r.parent)
                queue.append(r.parent)
    return None


def _check_refs(spec: QuerySpec, cat: Catalog, a: Assessment) -> bool:
    refs = [spec.group_by, spec.date_column] + [f.column for f in spec.filters]
    refs += [spec.ratio_filter.column] if spec.ratio_filter else []
    if spec.table not in cat.tables:
        a.block(INSUFFICIENT, f"Table '{spec.table}' does not exist in the data.")
        return False
    if spec.measure and spec.measure not in cat.tables[spec.table].columns:
        a.block(INSUFFICIENT, f"Column '{spec.measure}' does not exist in table '{spec.table}'.")
    for ref in filter(None, refs):
        t, c = _split(ref)
        if t not in cat.tables or c not in cat.tables[t].columns:
            a.block(INSUFFICIENT, f"Column '{ref}' does not exist in the data.")
    return a.answerable


def assess(spec: QuerySpec, cat: Catalog) -> Assessment:
    a = Assessment()
    for term in spec.unresolved:
        named = [(t, c, cp.kind) for t, p in cat.profiles.items() for c, cp in p.columns.items()
                 if c.lower().replace("_", " ") == term.lower() and cp.kind not in ("integer", "decimal")]
        if named:  # the column exists; it just holds no numbers to calculate with
            t, c, kind = named[0]
            a.block(UNSUPPORTED, f"{t}.{c} exists but holds {kind} values, not numbers, so it cannot be "
                                 "summed or averaged.")
            continue
        a.block(INSUFFICIENT, f"No column, value or metric definition for '{term}' exists in the data, so that part "
                              "of the question cannot be answered without guessing.")
    for reason in spec.ambiguities:
        a.block(AMBIGUOUS, reason)
    for reason in spec.unsupported:
        a.block(UNSUPPORTED, reason)
    if not spec.table or not a.answerable or not _check_refs(spec, cat, a):
        return a
    base = spec.table
    a.tables = [base]
    if spec.kind in ("ratio", "growth") and (spec.group_by or spec.time_grain or spec.top_n):
        a.block(UNSUPPORTED, "Grouped rates and growth rates are not supported; ask for one figure at a time.")
    if spec.top_n and not (spec.group_by or spec.time_grain):
        a.block(UNSUPPORTED, "A top-N ranking needs an entity to rank (for example 'top 5 products').")
    if spec.group_by and spec.time_grain:
        a.block(UNSUPPORTED, "Grouping by both a dimension and a time grain is not supported.")
    if spec.aggregation in ("count", "count_distinct") and spec.currency:
        a.diagnostics.append(f"'{spec.currency}' does not apply to a count and was ignored.")
    if not a.answerable:
        return a

    # joins needed for grouping / filtering on other tables
    for ref in filter(None, [spec.group_by, spec.date_column] + [f.column for f in spec.filters]):
        t, _ = _split(ref)
        if t in a.tables:
            continue
        path = _join_path(cat, base, t)
        if path is None:
            a.block(UNSUPPORTED, f"No many-to-one relationship links '{base}' to '{t}'; joining would multiply rows.")
            return a
        for step in path:
            if step not in a.joins:
                a.joins.append(step)
                a.tables.append(step[2])

    # labels that may name the same thing ('EU' / 'Europe') would split or miss a group
    for ref in filter(None, [spec.group_by] + [f.column for f in spec.filters]):
        t, c = _split(ref)
        for issue in cat.issues:
            if issue.kind == "label_variants" and issue.table == t and issue.column == c:
                a.block(AMBIGUOUS, f"{t}.{c}: {issue.detail[:-1]}. Grouping or filtering on it would treat them as "
                                   "different; merge them first if they are the same.")

    # record integrity of every table used
    for t in a.tables:
        p = cat.profiles[t]
        if p.exact_duplicate_rows:
            a.dedupe.append(t)
            a.diagnostics.append(f"{t}: removed {p.exact_duplicate_rows} exact duplicate row(s) "
                                 f"(identical records, including {p.key or 'all fields'}).")
        if p.duplicate_keys and t == base and _only_key_counted(spec, p.key):
            a.diagnostics.append(f"{t}: {len(p.duplicate_keys)} {p.key} value(s) have conflicting records "
                                 f"({', '.join(p.duplicate_keys[:3])}); counting distinct {p.key} is unaffected.")
        elif p.duplicate_keys:
            a.block(CONTRADICTORY, f"{t}: {len(p.duplicate_keys)} {p.key} value(s) have conflicting records "
                                   f"({', '.join(p.duplicate_keys[:3])}); the correct version cannot be determined.")
    for child, col, parent in a.joins:
        rel = next(r for r in cat.relationships if (r.child, r.column, r.parent) == (child, col, parent))
        if rel.unmatched:
            a.block(INSUFFICIENT, f"{rel.unmatched} {col} value(s) in {child} have no match in {parent}.")
        else:
            a.diagnostics.append(f"Join {child}.{col} -> {parent}: many-to-one, every key matched.")

    _check_measure(spec, cat, a)
    _check_currency(spec, cat, a)
    _check_dates(spec, cat, a)
    if spec.group_by:
        t, c = _split(spec.group_by)
        cp = cat.profiles[t].columns[c]
        # ponytail: checks nulls across the whole table, not only rows reachable from the base table
        if cp.nulls:
            a.block(INSUFFICIENT, f"{spec.group_by} is missing for {cp.nulls} row(s); those records cannot be grouped.")
    for f in spec.filters:
        t, c = _split(f.column)
        if cat.profiles[t].columns[c].nulls:
            a.block(INSUFFICIENT, f"{f.column} is missing for {cat.profiles[t].columns[c].nulls} row(s); "
                                  f"some of those records may also be '{f.value}', so the filter is unreliable.")
    if spec.measure and spec.aggregation in ("sum", "mean", "median", "max", "min") and not spec.ratio_filter:
        filtered = {f.column for f in spec.filters}
        for c, cp in cat.profiles[base].columns.items():
            if STATUS_RE.fullmatch(c.lower()) and len(cp.top_values) > 1 and f"{base}.{c}" not in filtered \
                    and spec.group_by != f"{base}.{c}":  # one figure per status mixes nothing
                a.block(AMBIGUOUS, f"{base}.{c} has the values {', '.join(sorted(cp.top_values))}; it is unclear "
                                   f"which {base} to include (for example '{next(iter(cp.top_values))} {base}').")
    for t, df in cat.tables.items():  # a pre-aggregated table states the same figure: say why it is not used
        if DERIVED_TABLE_RE.search(t) and t not in a.tables and spec.metric_term:
            for c in df.columns:
                if spec.metric_term.lower().split()[0] in c.lower() and c != spec.measure:
                    a.diagnostics.append(f"{t}.{c} also reports {spec.metric_term}, but {t} is pre-aggregated and "
                                         "is not reconciled with the record-level data, so it was not used.")
    if spec.ratio_filter:
        t, c = _split(spec.ratio_filter.column)
        if cat.profiles[t].columns[c].nulls:
            a.block(INSUFFICIENT, f"{spec.ratio_filter.column} has missing values; the rate's denominator is unclear.")
    for issue in cat.issues_for(a.tables):
        if issue.kind in ("prompt_injection", "temporal_contradiction", "negative_values", "derived_table", "normalized"):
            a.diagnostics.append(f"{issue.table}.{issue.column or ''}: {issue.detail}".replace(".:", ":"))
    if spec.kind in ("ratio", "growth"):
        a.precision = 4
    elif spec.aggregation in ("count", "count_distinct"):
        a.precision = 0
    return a


def _only_key_counted(spec: QuerySpec, key: str | None) -> bool:
    """Counting distinct keys of the base table: conflicting non-key fields cannot change the answer."""
    if spec.aggregation != "count_distinct" or spec.measure != key or spec.ratio_filter:
        return False
    refs = [spec.group_by, spec.date_column] + [f.column for f in spec.filters]
    return all(not r or _split(r) == (spec.table, key) or _split(r)[0] != spec.table for r in refs)


def is_money(column: str) -> bool:
    return bool(MONEY_RE.search(column.lower()))


def _check_measure(spec: QuerySpec, cat: Catalog, a: Assessment):
    if not spec.measure:
        return
    cp = cat.profiles[spec.table].columns[spec.measure]
    if spec.aggregation in ("sum", "mean", "median", "max", "min"):
        if cp.kind not in ("integer", "decimal"):
            a.block(UNSUPPORTED, f"{spec.table}.{spec.measure} is not numeric (type: {cp.kind}).")
            return
        if cp.decimals > MAX_SOURCE_DECIMALS:
            a.block(UNSUPPORTED, f"{spec.measure} has {cp.decimals} decimal places; exact arithmetic is limited to {MAX_SOURCE_DECIMALS}.")
        a.precision = max(cp.decimals, MEAN_MIN_DECIMALS) if spec.aggregation in ("mean", "median") else cp.decimals
    if cp.nulls:
        a.block(INSUFFICIENT, f"{spec.table}.{spec.measure} is missing for {cp.nulls} row(s); treating them as zero "
                              "or dropping them would change the result.")
    unit_col = unit_column_for(cat.tables[spec.table], spec.measure)
    if unit_col and spec.aggregation in ("sum", "mean", "median", "max", "min"):
        units = sorted(set(cat.tables[spec.table][unit_col]) - {""})
        if len(units) > 1:
            a.block(UNSUPPORTED, f"{spec.measure} mixes units ({', '.join(units)}) and the data defines no conversion.")
        elif units:
            a.unit = units[0]


def _check_currency(spec: QuerySpec, cat: Catalog, a: Assessment):
    if spec.aggregation not in ("sum", "mean", "median", "max", "min") or spec.kind == "ratio":
        return
    if spec.measure and not is_money(spec.measure):  # quantities, weights: currencies do not apply
        if spec.currency:
            a.block(UNSUPPORTED, f"{spec.table}.{spec.measure} is not a monetary amount, so it cannot be expressed "
                                 f"in {spec.currency}.")
        return
    df = cat.tables[spec.table]
    col = currency_column(df)
    target = spec.currency
    if not col:
        if target:
            a.block(INSUFFICIENT, f"{spec.table} does not record a currency, so amounts cannot be expressed in {target}.")
        return
    if (df[col] == "").any():
        a.block(INSUFFICIENT, f"{(df[col] == '').sum()} row(s) in {spec.table} have no currency.")
        return
    found = sorted(set(df[col]))
    if not target and spec.group_by == f"{spec.table}.{col}":  # one figure per currency: nothing is mixed
        a.diagnostics.append(f"Each group is a single currency ({', '.join(found)}); amounts are not converted.")
        return
    if not target:
        if len(found) > 1:
            a.block(AMBIGUOUS, f"Amounts are in {', '.join(found)}. Adding them without conversion is meaningless; "
                               "ask for a reporting currency (for example 'in USD').")
        else:
            a.unit = found[0]
        return
    a.unit = target
    a.precision = MINOR_UNITS.get(target, 2)
    if found == [target]:
        a.diagnostics.append(f"All amounts are already in {target}; no conversion needed.")
        return
    rates = _find_rates(cat, target, spec.table)
    if not rates:
        a.block(INSUFFICIENT, f"The data contains no exchange rates to {target}.")
        return
    rt, key, rate_col = rates
    rdf = cat.tables[rt]
    missing = sorted(set(found) - set(rdf[key]))
    if missing:
        a.block(INSUFFICIENT, f"No {target} exchange rate for {', '.join(missing)} in {rt}.")
        return
    if rdf[key].duplicated().any():
        a.block(AMBIGUOUS, f"{rt} has several rates per currency and no rule selects one.")
        return
    if cat.profiles[rt].columns[rate_col].kind not in ("integer", "decimal") or (rdf[rate_col] == "").any():
        a.block(CONTRADICTORY, f"{rt}.{rate_col} contains non-numeric or missing rates.")
        return
    a.currency = {"source_column": col, "target": target, "rates_table": rt, "rates_key": key, "rate_column": rate_col}
    a.tables.append(rt)
    dated = [c for c, cp in cat.profiles[rt].columns.items() if cp.kind == "date"]
    as_of = f" as of {', '.join(sorted(set(rdf[dated[0]])))}" if dated else ""
    a.diagnostics.append(f"Converted {', '.join(found)} to {target} with the single fixed rate per currency in "
                         f"{rt}.{rate_col}{as_of}; each amount is multiplied exactly, rounding happens once at the end.")


def _find_rates(cat: Catalog, target: str, base: str):
    t_low = target.lower()
    for name, df in cat.tables.items():
        key = currency_column(df)
        if name == base or not key:
            continue
        rate = next((c for c in df.columns if "rate" in c.lower() and t_low in c.lower()), None)
        if rate:
            return name, key, rate
    return None


def _check_dates(spec: QuerySpec, cat: Catalog, a: Assessment):
    if not spec.date_column:
        return
    t, c = _split(spec.date_column)
    cp = cat.profiles[t].columns[c]
    if cp.date_format == "ambiguous":
        a.block(AMBIGUOUS, f"{spec.date_column} could be day/month or month/day (e.g. '{cp.samples[0]}'); "
                           "both readings are valid and give different results.")
        return
    if cp.date_format != "iso":
        a.block(CONTRADICTORY, f"{spec.date_column} mixes date formats ({cp.date_format}); dates cannot be read reliably.")
        return
    if cp.nulls:
        a.block(INSUFFICIENT, f"{spec.date_column} is missing for {cp.nulls} row(s).")
    if spec.date_from and spec.date_to and spec.date_from > spec.date_to:
        a.block(AMBIGUOUS, f"The requested period starts ({spec.date_from}) after it ends ({spec.date_to}).")
        return
    if cp.kind == "datetime" and not cp.has_timezone:
        a.diagnostics.append(f"{spec.date_column} has no timezone; dates are taken as recorded.")
    lo, hi = cp.min[:10], cp.max[:10]
    periods = [(spec.date_from, spec.date_to)] if spec.date_from else []
    if spec.growth_from:
        periods += [(f"{y}-01-01", f"{y}-12-31") for y in (spec.growth_from, spec.growth_to)]
    for start, end in periods:
        # ponytail: month-level coverage check; a month with no records at its edges still counts as covered
        if start[:7] < lo[:7] or end[:7] > hi[:7]:
            a.block(INSUFFICIENT, f"Requested period {start} to {end} is outside the data, which covers {lo} to {hi}.")
    if spec.date_from:
        a.diagnostics.append(f"Date range {spec.date_from} to {spec.date_to}, both ends inclusive, on {spec.date_column}.")
