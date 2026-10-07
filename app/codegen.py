"""Generate executable analysis code from a Plan.

Two independent implementations come from the same plan:
- ``pandas_proof``: the proof script shown to the user (pandas + exact Decimal arithmetic).
- ``duckdb_check``: an independent re-computation in SQL, used only by the verifier.
They share no code at runtime, so a bug in one shows up as a disagreement.
"""
from .planning import Plan

CSV_ARGS = "dtype=str, keep_default_na=False"
DUCK_CSV = "header=true, all_varchar=true, delim=',', quote='\"', escape='\"'"


def _col(plan: Plan, ref: str) -> str:
    t, c = ref.split(".", 1)
    return c if t == plan.base else ref


def _parent_columns(plan: Plan, parent: str, col: str) -> list[str]:
    s = plan.spec
    refs = [s.group_by, s.date_column] + [f.column for f in s.filters] + ([s.ratio_filter.column] if s.ratio_filter else [])
    cols = [col] + [r.split(".", 1)[1] for r in refs if r and r.startswith(parent + ".")]
    cols += [c for ch, c, _ in plan.joins if ch == parent]
    return list(dict.fromkeys(cols))


def _py_cond(plan: Plan, column: str, op: str, value: str) -> str:
    c = _col(plan, column)
    if plan.column_kinds.get(column) in ("integer", "decimal"):
        return f"df[{c!r}].map(Decimal) {op} Decimal({value!r})"
    return f"df[{c!r}] {op} {value!r}"


def pandas_proof(plan: Plan) -> str:
    s, base = plan.spec, plan.base
    q = f'Decimal("1e-{plan.precision}")' if plan.precision else 'Decimal("1")'
    L = [f'"""Proof script.\n\nQuestion: {plan.question.replace(chr(34), chr(39))}\n"""',
         "import json", "import statistics", "from decimal import ROUND_HALF_EVEN, Decimal", "", "import pandas as pd", ""]
    for t in plan.tables:
        L.append(f"t_{t} = pd.read_csv({f'data/{t}.csv'!r}, {CSV_ARGS})")
        if t in plan.dedupe:
            L.append(f"t_{t} = t_{t}.drop_duplicates()  # exact duplicate records are counted once")
    L += ["", f"df = t_{base}.copy()"]
    for child, col, parent in plan.joins:
        cols = _parent_columns(plan, parent, col)
        left = col if child == base else f"{child}.{col}"
        L += [f"right = t_{parent}[{cols!r}].rename(columns=lambda c: {parent + '.'!r} + c)",
              f"assert right[{parent + '.' + col!r}].is_unique, {f'{parent}.{col} is not unique'!r}",
              f"df = df.merge(right, left_on={left!r}, right_on={parent + '.' + col!r}, how='left', validate='many_to_one')",
              f"assert df[{parent + '.' + col!r}].notna().all(), {f'unmatched {col} in {child}'!r}"]
    for f in s.filters:
        L.append(f"df = df[{_py_cond(plan, f.column, f.op, f.value)}]")
    if s.date_from:
        L += [f"day = df[{_col(plan, s.date_column)!r}].str[:10]",
              f"df = df[(day >= {s.date_from!r}) & (day <= {s.date_to!r})]"]
    L.append("")
    if s.ratio_filter:
        rf = s.ratio_filter
        L += [f"hits = int(({_py_cond(plan, rf.column, rf.op, rf.value)}).sum())", "total = len(df)",
              "assert total > 0, 'no rows to compute a rate over'",
              f"result = str((Decimal(hits) / Decimal(total)).quantize({q}, ROUND_HALF_EVEN))"]
    else:
        L += _pandas_aggregate(plan, q)
    L += ['print("RESULT: " + json.dumps(result))']
    return "\n".join(L) + "\n"


def _pandas_aggregate(plan: Plan, q: str) -> list[str]:
    s, c = plan.spec, plan.currency
    m = s.measure
    if c:
        L = [f"rates = t_{c['rates_table']}",
             f"rate = dict(zip(rates[{c['rates_key']!r}], rates[{c['rate_column']!r}].map(Decimal)))",
             f"missing = set(df[{c['source_column']!r}]) - set(rate)",
             "assert not missing, f'no exchange rate for {sorted(missing)}'",
             f"items = [Decimal(a) * rate[cur] for a, cur in zip(df[{m!r}], df[{c['source_column']!r}])]"]
    elif s.aggregation in ("sum", "mean", "median"):
        L = [f"items = [Decimal(a) for a in df[{m!r}]]"]
    elif s.aggregation == "count_distinct":
        L = [f"items = list(df[{m!r}])"]
    else:
        L = ["items = list(df.index)"]
    L += ["", "", "def aggregate(values):",
          "    " + {"sum": "return sum(values, Decimal(0))",
                    "mean": "assert values, 'no rows'\n    return sum(values, Decimal(0)) / len(values)",
                    "median": "assert values, 'no rows'\n    return statistics.median(values)",
                    "count": "return len(values)",
                    "count_distinct": "return len(set(values))"}[s.aggregation],
          "", "",
          "def fmt(x):",
          "    return x" if plan.integer_result else f"    return str(Decimal(x).quantize({q}, ROUND_HALF_EVEN))", ""]
    if s.kind == "scalar":
        return L + ["result = fmt(aggregate(items))"]
    if s.group_by:
        L.append(f"keys = list(df[{_col(plan, s.group_by)!r}])")
    else:
        width = 7 if s.time_grain == "month" else 4
        L.append(f"keys = [d[:{width}] for d in df[{_col(plan, s.date_column)!r}]]")
    L += ["groups = {}", "for k, v in zip(keys, items):", "    groups.setdefault(k, []).append(v)"]
    if s.kind == "growth":
        return L + [f"before, after = aggregate(groups.get({s.growth_from!r}, [])), aggregate(groups.get({s.growth_to!r}, []))",
                    "assert before != 0, 'base period total is zero'",
                    "result = fmt((after - before) / before)"]
    if s.kind == "ranking":
        return L + ["totals = {k: aggregate(g) for k, g in groups.items()}",
                    f"ranked = sorted(totals.items(), key=lambda kv: ({'-' if s.order == 'desc' else ''}kv[1], kv[0]))[:{s.top_n}]",
                    "result = [[k, fmt(v)] for k, v in ranked]"]
    return L + ["result = {k: fmt(aggregate(g)) for k, g in sorted(groups.items())}"]


# ---------------------------------------------------------------- DuckDB check

def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_col(plan: Plan, ref: str) -> str:
    t, c = ref.split(".", 1)
    return f"{'b' if t == plan.base else 'j_' + t}.{_ident(c)}"


def _sql_cond(plan: Plan, column: str, op: str, value: str) -> str:
    op = "=" if op == "==" else "<>" if op == "!=" else op
    if plan.column_kinds.get(column) in ("integer", "decimal"):
        return f"CAST({_sql_col(plan, column)} AS DECIMAL(18,6)) {op} {value}"
    return f"{_sql_col(plan, column)} {op} {_lit(value)}"


def duckdb_check(plan: Plan) -> str:
    s, base, c = plan.spec, plan.base, plan.currency
    ctes = [f"t_{t} AS (SELECT {'DISTINCT ' if t in plan.dedupe else ''}* FROM read_csv('data/{t}.csv', {DUCK_CSV}))"
            for t in plan.tables]
    frm = f"t_{base} AS b"
    nulls = []
    for child, col, parent in plan.joins:
        left = "b" if child == base else f"j_{child}"
        frm += f"\nLEFT JOIN t_{parent} AS j_{parent} ON {left}.{_ident(col)} = j_{parent}.{_ident(col)}"
        nulls.append(f"j_{parent}.{_ident(col)} IS NULL")
    value = f"CAST(b.{_ident(s.measure)} AS DECIMAL(18,6))" if s.measure else "NULL"
    if c:
        frm += f"\nLEFT JOIN t_{c['rates_table']} AS r ON b.{_ident(c['source_column'])} = r.{_ident(c['rates_key'])}"
        nulls.append(f"r.{_ident(c['rates_key'])} IS NULL")
        value += f" * CAST(r.{_ident(c['rate_column'])} AS DECIMAL(18,6))"
    where = [_sql_cond(plan, f.column, f.op, f.value) for f in s.filters]
    if s.date_from:
        where.append(f"CAST(substr({_sql_col(plan, s.date_column)}, 1, 10) AS DATE) "
                     f"BETWEEN DATE {_lit(s.date_from)} AND DATE {_lit(s.date_to)}")
    if s.group_by:
        key = _sql_col(plan, s.group_by)
    elif s.time_grain or s.growth_from:
        key = f"substr({_sql_col(plan, s.date_column)}, 1, {7 if s.time_grain == 'month' else 4})"
    else:
        key = "'*'"
    if s.ratio_filter:
        aggs = f"COUNT(*) FILTER (WHERE {_sql_cond(plan, s.ratio_filter.column, s.ratio_filter.op, s.ratio_filter.value)}), COUNT(*)"
    else:
        aggs = {"sum": f"SUM({value})", "mean": f"SUM({value}), COUNT({value})", "median": f"list({value} ORDER BY {value})",
                "count": "COUNT(*)", "count_distinct": f"COUNT(DISTINCT b.{_ident(s.measure or '')})"}[s.aggregation]
    where_sql = ("\nWHERE " + " AND ".join(where)) if where else ""
    sql = f"WITH {', '.join(ctes)}\nSELECT {key} AS k, {aggs}\nFROM {frm}{where_sql}\nGROUP BY 1"
    check_sql = f"WITH {', '.join(ctes)}\nSELECT COUNT(*) FROM {frm}\nWHERE {' OR '.join(nulls)}" if nulls else ""
    q = f'Decimal("1e-{plan.precision}")' if plan.precision else 'Decimal("1")'
    finish = {"sum": "row[1] or Decimal(0)", "mean": "row[1] / row[2]", "count": "row[1]", "count_distinct": "row[1]",
              "median": "median(row[1])"}[s.aggregation]
    if s.ratio_filter:
        finish = "Decimal(row[1]) / Decimal(row[2])"
    fmt = "x" if plan.integer_result else f"str(Decimal(x).quantize({q}, ROUND_HALF_EVEN))"
    # no matching rows: a sum or count is zero; a mean, median or rate is undefined (the proof asserts the same)
    empty = {"sum": "Decimal(0)", "count": "0", "count_distinct": "0"}.get(s.aggregation)
    scalar = (f"result = fmt(values.get('*', {empty}))" if empty and not s.ratio_filter else
              "assert '*' in values, 'no rows match the question'\nresult = fmt(values['*'])")
    tail = {
        "scalar": scalar,
        "ratio": scalar,
        "mapping": "result = {k: fmt(v) for k, v in sorted(values.items())}",
        "growth": f"before, after = values[{s.growth_from!r}], values[{s.growth_to!r}]\nresult = fmt((after - before) / before)",
        "ranking": f"ranked = sorted(values.items(), key=lambda kv: ({'-' if s.order == 'desc' else ''}kv[1], kv[0]))[:{s.top_n}]\n"
                   "result = [[k, fmt(v)] for k, v in ranked]",
    }[s.kind if s.kind in ("ratio", "growth", "ranking") else plan.output]
    return f'''"""Independent check: recompute the plan in DuckDB SQL."""
import json
from decimal import ROUND_HALF_EVEN, Decimal

import duckdb

con = duckdb.connect()
CHECK = {check_sql!r}
if CHECK:
    assert con.execute(CHECK).fetchone()[0] == 0, "unmatched join or currency key"
SQL = {sql!r}


def median(xs):
    xs, n = sorted(xs), len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def fmt(x):
    return {fmt}


values = {{}}
for row in con.execute(SQL).fetchall():
    values[str(row[0])] = {finish}
{tail}
print("RESULT: " + json.dumps(result))
'''
