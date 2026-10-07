"""Turn a natural-language question into a structured, validated QuerySpec.

Two interpreters produce the same QuerySpec: the deterministic parser below (no
network, supports a fixed question grammar) and the LLM interpreter in llm.py.
Everything downstream works only from the QuerySpec, never from free text.
"""
import calendar
import difflib
import re
from typing import Literal

from pydantic import BaseModel, Field

from .profiling import TableProfile, singular
from .security import sanitize_for_prompt

Op = Literal["==", "!=", ">", ">=", "<", "<="]
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR", "SEK", "NOK", "DKK", "NZD", "SGD",
              "HKD", "MXN", "BRL", "ZAR", "KRW", "VND", "THB", "IDR", "MYR", "PHP", "PKR", "BDT", "LKR", "NPR",
              "AED", "SAR", "QAR", "KWD", "TRY", "PLN", "CZK", "HUF", "RON", "ILS", "EGP", "NGN", "KES", "GHS",
              "ARS", "CLP", "COP", "PEN", "TWD", "RUB", "UAH"}
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
    order: Literal["desc", "asc"] = Field("desc", description="Ranking direction: desc = highest first")
    currency: str | None = Field(None, description="Requested reporting currency (ISO code) if stated")
    growth_from: str | None = Field(None, description="Base year for a growth rate")
    growth_to: str | None = Field(None, description="Comparison year for a growth rate")
    unresolved: list[str] = Field([], description="Terms in the question with no matching data or definition")
    notes: list[str] = Field([], description="How loosely worded parts of the question were read")

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
            if cl not in (last, joined, f"{last}_name", f"{last}_id", f"{joined}_id") and singular(cl) not in (last, joined):
                continue
            if qual and singular(t) != qual and not cl.startswith(qual) and t != base:
                continue
            rank = (t != base or not cl.endswith("_id") or not prefer_name,
                    0 if cl.endswith("_name") else 1 if cl in (last, joined) else 2)
            candidates.append((rank, f"{t}.{c}"))
    return min(candidates)[1] if candidates else None


def _date_column(table: str, profiles: dict[str, TableProfile], ql: str = "", notes: list | None = None) -> str | None:
    """The date a question is about. A word naming another date ("shipped" -> ship_date) wins over the
    base table's own date; answerability then decides whether that table can be joined safely."""
    words = re.findall(r"[a-z]+", ql)
    for t, p in profiles.items():
        for c, cp in p.columns.items():
            if cp.kind not in ("date", "datetime"):
                continue
            stem = re.sub(r"(_?date|_at|_on|_time)$", "", c.lower()).strip("_")
            if len(stem) < 3 or (t == table and stem == singular(table)):
                continue
            word = next((w for w in words if w.startswith(stem) and singular(w) != singular(t)), None)
            if word:
                if notes is not None:
                    notes.append(f"date taken from {t}.{c} (from '{word}')")
                return f"{t}.{c}"
    cols = [c for c, cp in profiles[table].columns.items() if cp.kind in ("date", "datetime")]
    own = [c for c in cols if c.lower().startswith(singular(table))]
    return f"{table}.{(own or cols)[0]}" if cols else None


DESC_WORDS = {"highest", "most", "top", "best", "largest", "biggest", "maximum", "max", "high", "higher",
              "leading", "greatest", "strongest"}
ASC_WORDS = {"lowest", "least", "bottom", "worst", "smallest", "fewest", "minimum", "min", "low", "lower", "weakest"}
# unambiguous currency names only ("dollar", "peso", "franc" name several currencies)
CURRENCY_WORDS = {"euro": "EUR", "euros": "EUR", "yen": "JPY", "rupee": "INR", "rupees": "INR", "sterling": "GBP",
                  "dong": "VND", "baht": "THB", "rupiah": "IDR", "ringgit": "MYR", "yuan": "CNY",
                  "renminbi": "CNY", "naira": "NGN", "rand": "ZAR", "zloty": "PLN", "lira": "TRY", "shekel": "ILS",
                  "rouble": "RUB", "ruble": "RUB", "hryvnia": "UAH", "forint": "HUF"}
AGGREGATIONS = [("median", r"\bmedian\b"), ("mean", r"\b(average|avg|mean)\b"),
                ("count", r"\bhow many\b|\bnumber of\b|\bcount\b|\bno\.? of\b"),
                ("sum", r"\b(total|sum|overall|combined|how much)\b")]
FILLER = {"what", "which", "who", "whats", "is", "are", "was", "were", "the", "a", "an", "of", "in", "on", "for", "to",
          "by", "per", "with", "and", "or", "me", "show", "give", "tell", "list", "find", "get", "does", "do", "did",
          "has", "have", "had", "there", "how", "much", "many", "number", "each", "every", "all", "our", "my", "from",
          "between", "during", "this", "that", "than", "across", "breakdown", "split", "generated", "made", "value",
          "generate", "make", "sold", "brought", "earned", "it", "its", "please", "be", "where", "when"}


def _metric_names(metrics: dict) -> dict[str, str]:
    """Every name a metric can be called by (its key and any `aliases`) -> metric key."""
    names = {k.lower(): k for k in metrics}
    for k, d in metrics.items():
        names.update({a.lower(): k for a in d.get("aliases", [])})
    return names


def _vocabulary(tables: dict, metrics: dict) -> set[str]:
    words = set(MONTHS) | DESC_WORDS | ASC_WORDS | {"average", "median", "total", "count", "monthly", "yearly",
                                                     "annual", "growth", "between", "distinct", "unique"}
    for name in _metric_names(metrics):
        words |= set(name.split())
    for t, df in tables.items():
        words |= {t, singular(t)}
        for c in df.columns:
            words |= set(re.findall(r"[a-z]+", c.lower()))
    return {w for w in words if len(w) > 2}


def _correct_typos(ql: str, vocab: set[str], notes: list[str]) -> str:
    """Replace likely misspellings of data words ('revnue' -> 'revenue') with stdlib fuzzy matching."""
    for w in sorted(set(re.findall(r"[a-z]+", ql))):
        if len(w) < 4 or w in vocab or singular(w) in vocab or w in FILLER or w.upper() in CURRENCIES:
            continue
        match = difflib.get_close_matches(w, vocab, n=1, cutoff=0.84)
        if match:
            ql = re.sub(rf"\b{w}\b", match[0], ql)
            notes.append(f"'{w}' read as '{match[0]}'")
    return ql


def _currency(ql: str) -> str | None:
    for tok in re.findall(r"[a-z]+", ql):
        if tok.upper() in CURRENCIES:
            return tok.upper()
        if tok in CURRENCY_WORDS:
            return CURRENCY_WORDS[tok]
    return None


def _is_measure(col: str, profiles: dict) -> bool:
    t, c = col.split(".", 1)
    return profiles[t].columns[c].kind in ("integer", "decimal") and c != profiles[t].key and not c.endswith("_id")


def _numeric_column(ql: str, tables: dict, profiles: dict):
    """A numeric column named anywhere in the question, for data without metric definitions."""
    words = [w for w in re.findall(r"[a-z_]+", ql) if w not in FILLER and w not in DESC_WORDS | ASC_WORDS]
    for phrase in [" ".join(words[i:i + 2]) for i in range(len(words) - 1)] + words:
        col = resolve_column(phrase, None, tables, prefer_name=False)
        if col and _is_measure(col, profiles):
            return phrase, col
    return None


def _scan_dimension(ql: str, spec: QuerySpec, tables: dict, profiles: dict, skip: set[str]):
    """Find an entity to group by when the question names one without 'by' ('which region ...')."""
    words = [w for w in re.findall(r"[a-z_]+", ql)
             if w not in FILLER and w not in skip and w not in DESC_WORDS | ASC_WORDS and w.upper() not in CURRENCIES
             and w not in CURRENCY_WORDS and w not in MONTHS]
    candidates = [" ".join(words[i:i + 2]) for i in range(len(words) - 1)] + words
    for phrase in candidates:
        col = resolve_column(phrase, spec.table, tables)
        if not col:
            continue
        t, c = col.split(".", 1)
        if c == spec.measure or (t == spec.table and c == profiles[t].key):
            continue  # an entity grouped by its own identifier says nothing
        if profiles[t].columns[c].kind not in ("decimal", "date", "datetime"):
            return phrase, col
    return None


def _count_fallback(ql: str, spec: QuerySpec, tables: dict, profiles: dict, order, explicit_agg) -> bool:
    """No measure named: count the entity the question is about ("shipments by carrier", "orders in 2024",
    "which carrier shipped the most"). Returns False when there is no sensible entity to count."""
    entity = next((t for w in re.findall(r"[a-z_]+", ql) if (t := _match_table(w, tables))), None)
    dim = None
    grouping = bool(order or re.search(r"(which|by|per|each|every)", ql))
    if grouping or explicit_agg == "count":
        probe = QuerySpec(table=entity)
        dim = _scan_dimension(ql, probe, tables, profiles, set())
    table = entity or (dim[1].split(".", 1)[0] if dim else None)
    if not table:
        return False
    if dim and explicit_agg == "count" and not grouping and not entity:  # "how many shops" -> distinct shops
        t, c = dim[1].split(".", 1)
        spec.metric_term, spec.table, spec.aggregation, spec.measure = c, t, "count_distinct", c
        spec.notes.append(f"counted distinct values of {c.replace('_', ' ')} (from '{dim[0]}')")
        return True
    if dim:
        t, c = dim[1].split(".", 1)
        if t == table and (c == profiles[t].key or c.endswith("_name")):
            return False  # counting an entity per itself says nothing
        spec.group_by = dim[1]
        spec.notes.append(f"grouped by {c.replace('_', ' ')} (from '{dim[0]}')")
    elif order:
        return False  # "best products" names no measure to rank by
    key = profiles[table].key
    spec.metric_term, spec.table = table, table
    spec.aggregation, spec.measure = ("count_distinct", key) if key else ("count", None)
    spec.notes.append(f"no measure named, so {table} are counted")
    return True


def parse_question(question: str, tables: dict, profiles: dict[str, TableProfile], metrics: dict) -> QuerySpec:
    spec = QuerySpec()
    notes = spec.notes
    ql = _correct_typos(question.strip().lower(), _vocabulary(tables, metrics), notes)

    spec.currency = _currency(ql)
    if (m := re.search(r"\b(\d{4})\s*(?:to|vs\.?|versus|and|-)\s*(\d{4})\b", ql)) and \
            re.search(r"\b(growth|grow|grew|change|increase|decrease|rise|fall)\b", ql):
        spec.growth_from, spec.growth_to = m.groups()
    if m := re.search(r"between (\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2})", ql):
        spec.date_from, spec.date_to = m.groups()
    elif m := re.search(r"\b(?:in|during|for) (" + "|".join(MONTHS) + r") (\d{4})\b", ql):
        y, mo = int(m.group(2)), MONTHS[m.group(1)]
        spec.date_from, spec.date_to = f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    elif not spec.growth_from and (m := re.search(r"\b((?:19|20)\d{2})\b(?!-)", ql)):
        spec.date_from, spec.date_to = f"{m.group(1)}-01-01", f"{m.group(1)}-12-31"
    if re.search(r"\bmonthly\b|\b(per|by|each|every) month\b|\bmonth by month\b|\bover time\b|\btrend\b", ql):
        spec.time_grain = "month"
    elif re.search(r"\b(yearly|annual|annually)\b|\b(per|by|each|every) year\b", ql) and not spec.growth_from:
        spec.time_grain = "year"

    order_word = next((w for w in re.findall(r"[a-z]+", ql) if w in DESC_WORDS | ASC_WORDS), None)
    order = "asc" if order_word in ASC_WORDS else "desc" if order_word else None
    n = None
    if m := re.search(r"\b(?:top|bottom|first|last)\s+(\d+)\b|\b(\d+)\s+(?:highest|lowest|largest|smallest|best|worst|most|least|biggest)\b", ql):
        n = int(m.group(1) or m.group(2))
        order = order or "desc"
    explicit_agg = next((a for a, pat in AGGREGATIONS if re.search(pat, ql)), None)

    # 1) metrics defined alongside the data (business definitions, with aliases)
    names = _metric_names(metrics)
    hit = next((nm for nm in sorted(names, key=len, reverse=True) if re.search(rf"\b{re.escape(nm)}\b", ql)), None)
    metric_words: set[str] = set()
    if hit:
        term = names[hit]
        d = metrics[term]
        if hit != term.lower():
            notes.append(f"'{hit}' read as the defined metric '{term}'")
        metric_words = set(hit.split()) | set(term.lower().split())
        spec.metric_term, spec.table, spec.measure = term, d["table"], d.get("column")
        spec.aggregation = explicit_agg if explicit_agg in ("mean", "median", "sum") else d.get("aggregation", "sum")
        if rf := d.get("ratio_filter"):
            spec.ratio_filter = Filter(column=f"{d['table']}.{rf['column']}", op=rf["op"], value=rf["value"])
    # 2) counts of an entity: "how many customers", "number of orders", "customer count"
    elif explicit_agg == "count" and (
            (m := re.search(r"(?:how many|number of|count of|count|no\.? of)\s+(?:the |unique |distinct |different )?([a-z_]+)", ql))
            or (m := re.search(r"([a-z_]+)\s+count\b", ql))) and (t := _match_table(m.group(1), tables)):
        key = profiles[t].key
        spec.metric_term, spec.table = m.group(1), t
        metric_words = {m.group(1)}
        spec.aggregation, spec.measure = ("count_distinct", key) if key else ("count", None)
    # 3) a numeric column named in the question: "total product weight", "average unit price"
    else:
        m = re.search(r"\b(?:total|average|avg|mean|median|sum of|sum)\s+([a-z ]+?)(?=\s+(?:in|by|per|for|from|of|between|across)\b|\?|$)", ql)
        phrase = m.group(1).strip() if m else ""
        col = resolve_column(phrase, None, tables, prefer_name=False) if phrase else None
        if not (col and _is_measure(col, profiles)) and explicit_agg != "count":
            found = _numeric_column(ql, tables, profiles)  # "which shop has the lowest sales"
            if found:
                phrase, col = found
        if col and _is_measure(col, profiles):
            spec.table, spec.measure = col.split(".")
            spec.metric_term, spec.aggregation = phrase, explicit_agg or "sum"
            metric_words = set(phrase.split())
        elif not _count_fallback(ql, spec, tables, profiles, order, explicit_agg):
            spec.metric_term = phrase or question.strip()
            spec.unresolved.append(phrase or "the metric being asked for")
            return spec
        else:
            metric_words = {spec.table, singular(spec.table)}

    # grouping dimension: explicit ("by region", "top 5 products") or named loosely ("which region ...")
    group_phrase = None
    if n and (m := re.search(r"\b(?:top|bottom|first|last) \d+ ([a-z ]+?)(?=\s+(?:by|in|for|with|of)\b|\?|$)", ql)):
        group_phrase = m.group(1)
    elif m := re.search(r"\b(?:by|per|for each|for every|across) (?:each |every )?([a-z ]+?)(?=\s+(?:in|for|from|between|with)\b|\?|$)", ql):
        group_phrase = m.group(1)
    if group_phrase:  # drop currency codes, years and filler: "carrier usd" -> "carrier"
        kept = [w for w in group_phrase.split() if w.upper() not in CURRENCIES and w not in CURRENCY_WORDS
                and not w.isdigit() and w not in FILLER and w not in metric_words]
        group_phrase = " ".join(kept) or None
    if spec.group_by:
        pass
    elif group_phrase and group_phrase.strip() not in ("month", "year", spec.metric_term):
        col = resolve_column(group_phrase, spec.table, tables)
        if col:
            spec.group_by = col
        else:
            spec.unresolved.append(group_phrase.strip())
    elif not group_phrase and not spec.time_grain and (order or re.search(r"\b(which|each|every|breakdown|split)\b", ql)):
        found = _scan_dimension(ql, spec, tables, profiles, metric_words | {"total", "average", "median", "count"})
        if found:
            spec.group_by = found[1]
            notes.append(f"grouped by {found[1].split('.', 1)[1].replace('_', ' ')} (from '{found[0]}')")
    if order and spec.group_by and spec.kind not in ("ratio", "growth"):
        spec.top_n, spec.order = n or 1, order
        if not n:
            notes.append(f"'{order_word}' read as the single {'highest' if order == 'desc' else 'lowest'} value")
    elif order_word and not spec.group_by and not spec.time_grain:
        notes.append(f"'{order_word}' ignored: the question names nothing to rank")
    if spec.date_from or spec.time_grain or spec.growth_from:
        spec.date_column = _date_column(spec.table, profiles, ql, notes)
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


def compact_catalog(summary: dict) -> str:
    """Short text form of `catalog_summary` for small local models: names, types, keys, links and metric
    definitions only, with no data values. Training data uses the same function, so prompts match exactly."""
    lines = ["Tables:"]
    for t, info in summary["tables"].items():
        cols = ", ".join(f"{c} {d['type']}" for c, d in info["columns"].items())
        lines.append(f"- {t}" + (f" (key {info['key']})" if info.get("key") else "") + f": {cols}")
    if summary.get("relationships"):
        lines.append("Relationships:")
        lines += [f"- {r}" for r in summary["relationships"]]
    if summary.get("metric_definitions"):
        lines.append("Metrics:")
        for name, d in summary["metric_definitions"].items():
            if d.get("ratio_filter"):
                rf = d["ratio_filter"]
                body = f"share of {d['table']} rows where {rf['column']} {rf['op']} {rf['value']}"
            else:
                body = f"{d.get('aggregation', 'sum')} of {d['table']}.{d['column']}"
            aliases = d.get("aliases") or []
            lines.append(f"- {name}: {body}" + (f"; also called {', '.join(aliases)}" if aliases else ""))
    return "\n".join(lines)


def normalize_refs(spec: QuerySpec, tables: dict, profiles: dict[str, TableProfile]) -> QuerySpec:
    """Tidy a model-written spec's references into the form the checks expect, without changing its meaning:
    `measure` is a bare column of `table` ('orders.amount' -> 'amount'); other references are 'table.column'
    (a bare column of the base table gets the table prefix); a distinct count with no column counts the key."""
    t = spec.table
    if spec.measure and "." in spec.measure and spec.measure.split(".", 1)[0] == t:
        spec.measure = spec.measure.split(".", 1)[1]
    if t in tables:
        for field in ("group_by", "date_column"):
            ref = getattr(spec, field)
            if ref and "." not in ref and ref in tables[t].columns:
                setattr(spec, field, f"{t}.{ref}")
        if spec.aggregation == "count_distinct" and not spec.measure:
            key = profiles[t].key
            spec.measure, spec.aggregation = (key, "count_distinct") if key else (None, "count")
    return spec
