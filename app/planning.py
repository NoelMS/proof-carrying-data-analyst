"""Turn an answerable QuerySpec + Assessment into an explicit, inspectable analytical plan."""
from dataclasses import dataclass, field

from .answerability import Assessment
from .catalog import Catalog
from .question import QuerySpec


@dataclass
class Plan:
    question: str
    spec: QuerySpec
    base: str
    tables: list[str]
    dedupe: list[str]
    joins: list[tuple[str, str, str]]
    currency: dict | None
    unit: str | None
    precision: int
    column_kinds: dict[str, str]  # "table.column" -> profiled type, for every column the plan touches
    steps: list[str] = field(default_factory=list)
    columns: dict[str, list[str]] = field(default_factory=dict)  # every column of each table read, by name

    @property
    def output(self) -> str:
        return {"ranking": "ranking", "grouped": "mapping"}.get(self.spec.kind, "scalar")  # share, anti: one figure

    @property
    def integer_result(self) -> bool:
        return self.spec.aggregation in ("count", "count_distinct") and self.spec.kind not in ("ratio", "growth", "share")

    def output_contract(self) -> str:
        fmt = "integer" if self.integer_result else f"decimal string with exactly {self.precision} decimal places"
        return {"scalar": f"a single {fmt}",
                "mapping": f"a JSON object mapping each group key (string, sorted ascending) to a {fmt}",
                "ranking": f"a JSON list of [key, value] pairs, value is a {fmt}, sorted by value "
                           f"{'descending' if self.spec.order == 'desc' else 'ascending'} then key "
                           f"ascending, first {self.spec.top_n} only"}[self.output]

    def text(self) -> str:
        lines = [f"Question: {self.question}", "Files: " + ", ".join(f"data/{t}.csv" for t in self.tables)]
        lines += [f"Columns of data/{t}.csv: {', '.join(cols)}" for t, cols in self.columns.items()]
        lines.append("Steps:")
        lines += [f"{i}. {s}" for i, s in enumerate(self.steps, 1)]
        lines.append(f"Output: print one line 'RESULT: <json>' where <json> is {self.output_contract()}.")
        return "\n".join(lines)


def _ref_desc(base: str, ref: str) -> str:
    return ref.split(".", 1)[1] if ref.startswith(base + ".") else ref


def build_plan(question: str, spec: QuerySpec, a: Assessment, cat: Catalog) -> Plan:
    base = spec.table
    refs = spec.refs() + ([f"{base}.{spec.measure}"] if spec.measure else [])
    kinds = {r: cat.profiles[r.split(".")[0]].columns[r.split(".", 1)[1]].kind for r in refs}
    p = Plan(question, spec, base, a.tables, a.dedupe, a.joins, a.currency, a.unit, a.precision, kinds)
    p.columns = {t: list(cat.tables[t].columns) for t in a.tables}
    s = p.steps
    for t in a.dedupe:
        s.append(f"Remove exact duplicate rows from {t}.")
    for child, col, parent in a.joins:
        s.append(f"Left-join {child} to {parent} on {col}; assert {parent}.{col} is unique (many-to-one) "
                 "and every row finds a match.")
    for f in spec.filters:
        s.append(f"Keep rows where {f.column}{' converted to ' + spec.currency if f.convert else ''} {f.op} {f.value!r}.")
    if spec.date_from:
        s.append(f"Keep rows where {spec.date_column} (ISO date) is between {spec.date_from} and {spec.date_to} inclusive.")
    if spec.growth_from:
        s.append(f"Use the year of {spec.date_column}; compare {spec.growth_from} with {spec.growth_to}.")
    measure = f"{base}.{spec.measure}" if spec.measure else None
    if spec.date_diff:
        s.append(f"Value = calendar days from {spec.date_diff[0]} to {spec.date_diff[1]} (first 10 characters, ISO).")
    elif a.currency:
        c = a.currency
        s.append(f"Value = Decimal({measure}) x Decimal({c['rates_table']}.{c['rate_column']}) looked up by "
                 f"{base}.{c['source_column']} = {c['rates_table']}.{c['rates_key']}; assert every currency has a rate.")
    elif measure and spec.aggregation in ("sum", "mean", "median", "max", "min"):
        s.append(f"Value = Decimal({measure}).")
    if spec.without:
        s.append(f"Count distinct {base}.{spec.measure} (non-empty) that appear in no {spec.without}.{spec.measure} value.")
    elif spec.share_filter:
        f = spec.share_filter
        s.append(f"Share = (sum of value where {f.column} {f.op} {f.value!r}) / (sum of all values); assert the total is non-zero.")
    elif spec.ratio_filter:
        f = spec.ratio_filter
        s.append(f"Rate = (rows where {f.column} {f.op} {f.value!r}) / (all rows); assert there is at least one row.")
    elif spec.growth_from:
        s.append(f"Growth = (sum {spec.growth_to} - sum {spec.growth_from}) / sum {spec.growth_from}; assert the base is non-zero.")
    else:
        agg = {"sum": "Sum of value", "mean": "Mean of value", "median": "Median of value",
               "max": "Largest single value", "min": "Smallest single value",
               "count": "Count of rows", "count_distinct": f"Count of distinct {measure}"}[spec.aggregation]
        if spec.group_by:
            agg += f" per {_ref_desc(base, spec.group_by)}"
            if spec.group_by2:
                agg += f" and {_ref_desc(base, spec.group_by2)} (key 'A · B')"
        elif spec.periods:
            agg += (f" per period of {spec.date_column} ({'YYYY-Qn' if 'Q' in spec.periods[0] else 'YYYY-MM' if len(spec.periods[0]) == 7 else 'YYYY'}),"
                    f" reported for {spec.periods[0]} and {spec.periods[1]}; assert both have rows")
        elif spec.time_grain:
            agg += f" per {spec.time_grain} of {spec.date_column} ({'YYYY-MM' if spec.time_grain == 'month' else 'YYYY'})"
        if spec.top_n:
            agg += (f"; keep the {'highest' if spec.order == 'desc' else 'lowest'} {spec.top_n} by value "
                    "(ties broken by key ascending)")
        s.append(agg + ".")
    if not p.integer_result:
        s.append(f"Round once at the end to {p.precision} decimal places, half-to-even.")
    return p
