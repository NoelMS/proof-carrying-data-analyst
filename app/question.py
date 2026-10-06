"""Turn a natural-language question into a structured, validated QuerySpec.

Two interpreters produce the same QuerySpec: the deterministic parser below (no
network, supports a fixed question grammar) and the LLM interpreter in llm.py.
Everything downstream works only from the QuerySpec, never from free text.
"""
import calendar
import re
from typing import Literal

from pydantic import BaseModel, Field

from .profiling import TableProfile, singular
from .security import sanitize_for_prompt

Op = Literal["==", "!=", ">", ">=", "<", "<="]
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR", "SEK", "NOK", "DKK",
              "NZD", "SGD", "HKD", "MXN", "BRL", "ZAR", "KRW"}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
STOP = {"the", "a", "an", "of", "all", "each", "every", "our", "my", "total", "overall"}


class Filter(BaseModel):
    column: str = Field(description="'table.column'")
    op: Op
    value: str


class QuerySpec(BaseModel):
    metric_term: str = Field("", description="Phrase in the question naming the metric")
    table: str | None = Field(None, description="Base table holding one row per measured record")
    measure: str | None = Field(None, description="Column of the base table to aggregate; null for row counts")
    aggregation: Literal["sum", "mean", "median", "count", "count_distinct"] = "sum"
    ratio_filter: Filter | None = Field(None, description="For rates/shares: rows matching / all rows")
    filters: list[Filter] = []
    date_column: str | None = Field(None, description="'table.column' used for date filters / time grain")
    date_from: str | None = Field(None, description="Inclusive ISO date")
    date_to: str | None = Field(None, description="Inclusive ISO date")
    group_by: str | None = Field(None, description="'table.column' to group by")
    time_grain: Literal["month", "year"] | None = None
    top_n: int | None = None
    currency: str | None = Field(None, description="Requested reporting currency (ISO code) if stated")
    growth_from: str | None = Field(None, description="Base year for a growth rate")
    growth_to: str | None = Field(None, description="Comparison year for a growth rate")
    unresolved: list[str] = Field([], description="Terms in the question with no matching data or definition")

    @property
    def kind(self) -> str:
        if self.ratio_filter:
            return "ratio"
        if self.growth_from:
            return "growth"
        if self.top_n:
            return "ranking"
        return "grouped" if (self.group_by or self.time_grain) else "scalar"


def _match_table(word: str, tables) -> str | None:
    w = singular(word.lower())
    return next((t for t in tables if singular(t) == w), None)


def resolve_column(phrase: str, base: str | None, tables: dict, prefer_name=True) -> str | None:
    """Map a phrase like 'region' / 'product category' to 'table.column'."""
    words = [singular(w) for w in re.findall(r"[a-z0-9]+", phrase.lower()) if w not in STOP]
    if not words:
        return None
    last, qual = words[-1], (words[0] if len(words) > 1 else None)
    joined = "_".join(words)
    candidates = []
    for t, df in tables.items():
        for c in df.columns:
            cl = c.lower()
            if cl not in (last, joined, f"{last}_name", f"{last}_id", f"{joined}_id"):
                continue
            if qual and singular(t) != qual and not cl.startswith(qual) and t != base:
                continue
            rank = (t != base or not cl.endswith("_id") or not prefer_name,
                    0 if cl.endswith("_name") else 1 if cl in (last, joined) else 2)
            candidates.append((rank, f"{t}.{c}"))
    return min(candidates)[1] if candidates else None


def _date_column(table: str, profiles: dict[str, TableProfile]) -> str | None:
    cols = [c for c, cp in profiles[table].columns.items() if cp.kind in ("date", "datetime")]
    own = [c for c in cols if c.lower().startswith(singular(table))]
    return f"{table}.{(own or cols)[0]}" if cols else None


def parse_question(question: str, tables: dict, profiles: dict[str, TableProfile], metrics: dict) -> QuerySpec:
    q = question.strip()
    ql = q.lower()
    spec = QuerySpec()

    m = re.search(r"\bin ([A-Za-z]{3})\b", q)
    if m and m.group(1).upper() in CURRENCIES:
        spec.currency = m.group(1).upper()
    m = re.search(r"growth\b.*?\bfrom (\d{4}) to (\d{4})", ql)
    if m:
        spec.growth_from, spec.growth_to = m.groups()
    m = re.search(r"between (\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2})", ql)
    if m:
        spec.date_from, spec.date_to = m.groups()
    elif m := re.search(r"\bin (" + "|".join(MONTHS) + r") (\d{4})\b", ql):
        y, mo = int(m.group(2)), MONTHS[m.group(1)]
        spec.date_from, spec.date_to = f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    elif not spec.growth_from and (m := re.search(r"\b(?:in|during) (\d{4})\b", ql)):
        spec.date_from, spec.date_to = f"{m.group(1)}-01-01", f"{m.group(1)}-12-31"
    if re.search(r"\bmonthly\b|\b(per|by|each) month\b", ql):
        spec.time_grain = "month"
    elif re.search(r"\b(yearly|annual)\b|\b(per|by|each) year\b", ql) and not spec.growth_from:
        spec.time_grain = "year"
    if m := re.search(r"\btop (\d+)\b", ql):
        spec.top_n = int(m.group(1))

    explicit_agg = ("median" if "median" in ql else "mean" if re.search(r"\b(average|mean)\b", ql)
                    else "count" if re.search(r"\bhow many\b|\bnumber of\b|\bcount of\b", ql)
                    else "sum" if re.search(r"\b(total|sum)\b", ql) else None)

    # 1) metrics defined alongside the data (business definitions)
    term = next((t for t in sorted(metrics, key=len, reverse=True) if re.search(rf"\b{re.escape(t)}\b", ql)), None)
    if term:
        d = metrics[term]
        spec.metric_term, spec.table, spec.measure = term, d["table"], d.get("column")
        spec.aggregation = explicit_agg if explicit_agg in ("mean", "median", "sum") else d.get("aggregation", "sum")
        if rf := d.get("ratio_filter"):
            spec.ratio_filter = Filter(column=f"{d['table']}.{rf['column']}", op=rf["op"], value=rf["value"])
    # 2) counts of an entity: "how many customers", "number of orders"
    elif explicit_agg == "count" and (m := re.search(r"(?:how many|number of|count of) ([a-z_]+)", ql)) \
            and (t := _match_table(m.group(1), tables)):
        key = profiles[t].key
        spec.metric_term, spec.table = m.group(1), t
        spec.aggregation, spec.measure = ("count_distinct", key) if key else ("count", None)
    # 3) a column named in the question: "total product weight"
    else:
        m = re.search(r"\b(?:total|average|mean|median|sum of)\s+([a-z ]+?)(?=\s+(?:in|by|per|for|from|of|between)\b|\?|$)", ql)
        phrase = m.group(1).strip() if m else ""
        col = resolve_column(phrase, None, tables, prefer_name=False) if phrase else None
        if col and profiles[col.split(".")[0]].columns[col.split(".")[1]].kind in ("integer", "decimal"):
            spec.table, spec.measure = col.split(".")
            spec.metric_term, spec.aggregation = phrase, explicit_agg or "sum"
        else:
            spec.metric_term = phrase or q
            spec.unresolved.append(phrase or "metric")
            return spec

    # grouping dimension
    group_phrase = None
    if spec.top_n and (m := re.search(r"\btop \d+ ([a-z ]+?)(?=\s+(?:by|in|for)\b|\?|$)", ql)):
        group_phrase = m.group(1)
    elif m := re.search(r"\b(?:by|per) (?:each )?([a-z ]+?)(?=\s+(?:in|for|from|between)\b|\?|$)", ql):
        group_phrase = m.group(1)
    if group_phrase and group_phrase.strip() not in ("month", "year", spec.metric_term):
        col = resolve_column(group_phrase, spec.table, tables)
        if col:
            spec.group_by = col
        else:
            spec.unresolved.append(group_phrase.strip())
    if spec.date_from or spec.time_grain or spec.growth_from:
        spec.date_column = _date_column(spec.table, profiles)
        if not spec.date_column:
            spec.unresolved.append(f"date for {spec.table}")
    return spec


def catalog_summary(tables: dict, profiles: dict[str, TableProfile], rels, metrics: dict) -> dict:
    """Metadata a model may see: schema and profiles with sanitized samples, never full data."""
    return {
        "tables": {
            t: {"rows": p.rows, "key": p.key, "columns": {
                c: {"type": cp.kind, "null_pct": cp.null_pct, "unique": cp.unique,
                    "samples": [sanitize_for_prompt(v) for v in cp.samples],
                    **({"values": [sanitize_for_prompt(v) for v in cp.top_values]} if cp.top_values else {})}
                for c, cp in p.columns.items()}}
            for t, p in profiles.items()},
        "relationships": [f"{r.child}.{r.column} -> {r.parent}.{r.column}" for r in rels],
        "metric_definitions": metrics,
    }
