"""Turn a natural-language question into a structured, validated QuerySpec.

Two interpreters produce the same QuerySpec: the deterministic parser below (no
network, supports a fixed question grammar) and the LLM interpreter in llm.py.
Everything downstream works only from the QuerySpec, never from free text.
"""
import calendar
import difflib
import os
import re
from typing import Literal

from pydantic import BaseModel, Field

from .profiling import TableProfile, singular
from .security import is_instruction_like, sanitize_for_prompt
from .synonyms import apply_synonyms
from .traps import CURRENCY_COL_RE, DERIVED_TABLE_RE, UNIT_COL_RE, unit_column_for

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
    convert: bool = Field(False, description="Compare the value converted to the reporting currency (set by the app)")


THRESHOLD_OPS = {"over": ">", "above": ">", "more than": ">", "greater than": ">", "exceeding": ">", "at least": ">=",
                 "under": "<", "below": "<", "less than": "<", "fewer than": "<", "at most": "<=", "no more than": "<="}
THRESHOLD_RE = re.compile(r"\b(?:(?:with|where|whose|having|of)\s+)?(?:(?P<col>[a-z_]+(?:\s[a-z_]+)?)\s+)?"
                          r"(?P<op>no more than|more than|greater than|less than|fewer than|at least|at most|exceeding|"
                          r"over|above|under|below)\s+(?P<cur>[$€£])?(?P<num>\d[\d,]*(?:\.\d+)?)(?:\s?(?P<code>[a-z]{3})\b)?")
ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
QUARTER_RE = re.compile(r"\b(?:q([1-4])|(first|second|third|fourth|1st|2nd|3rd|4th) quarter)(?: of)?\s*((?:19|20)\d{2})\b")
HALF_RE = re.compile(r"\b(?:h([12])|(first|second|1st|2nd) half)(?: of)?\s*((?:19|20)\d{2})\b")
PERIOD_WORDS = {"q1", "q2", "q3", "q4", "h1", "h2", "quarter", "half", "first", "second", "third", "fourth",
                "1st", "2nd", "3rd", "4th"}
RELATIVE_RE = re.compile(r"\b(?:last|this|previous|current|past|prior)\s+(?:\d+\s+)?(?:week|month|quarter|year)s?\b"
                         r"|\bytd\b|\byear to date\b|\brecent(?:ly)?\b|\btoday\b|\byesterday\b")
THRESHOLD_WORDS = {"over", "above", "more", "greater", "exceeding", "least", "under", "below", "less", "fewer", "most",
                   "no", "than", "with", "where", "whose", "having"}


class QuerySpec(BaseModel):
    metric_term: str = Field("", description="Phrase in the question naming the metric")
    table: str | None = Field(None, description="Base table holding one row per measured record")
    measure: str | None = Field(None, description="Column of the base table to aggregate; null for row counts")
    aggregation: Literal["sum", "mean", "median", "max", "min", "count", "count_distinct"] = "sum"
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
    ambiguities: list[str] = Field([], description="Parts of the question that can be read in more than one way, "
                                                   "each stated as one sentence")
    unsupported: list[str] = Field([], description="Parts of the question this spec cannot express (OR conditions, "
                                                   "numeric thresholds, several metrics, forecasts, explanations), "
                                                   "each stated as one sentence. Never drop such a part silently")
    notes: list[str] = Field([], description="How loosely worded parts of the question were read")
    share_filter: Filter | None = Field(None, description="For a share of a sum: matching rows' sum / all rows' sum")
    group_by2: str | None = Field(None, description="Second 'table.column' to group by")
    periods: list[str] = Field([], description="Two periods compared side by side: 'YYYY', 'YYYY-MM' or 'YYYY-Qn'")
    without: str | None = Field(None, description="Count rows of `table` with no matching row in this table")
    date_diff: list[str] = Field([], description="['table.start_date', 'table.end_date']: aggregate the days between")

    def refs(self) -> list[str]:
        """Every column reference the reading uses, besides the measure."""
        out = [self.group_by, self.group_by2, self.date_column] + [f.column for f in self.filters]
        out += [x.column for x in (self.ratio_filter, self.share_filter) if x] + self.date_diff
        return [r for r in out if r]

    @property
    def kind(self) -> str:
        if self.without:
            return "anti"
        if self.share_filter:
            return "share"
        if self.ratio_filter:
            return "ratio"
        if self.growth_from:
            return "growth"
        if self.top_n:
            return "ranking"
        return "grouped" if (self.group_by or self.time_grain or self.periods) else "scalar"


def _match_table(word: str, tables) -> str | None:
    w = singular(word.lower())
    return next((t for t in tables if singular(t) == w), None)


def resolve_column(phrase: str, base: str | None, tables: dict, prefer_name=True) -> str | None:
    """Map a phrase like 'region' / 'product category' to 'table.column'."""
    candidates = column_candidates(phrase, base, tables, prefer_name)
    return candidates[0][1] if candidates else None


def column_candidates(phrase: str, base: str | None, tables: dict, prefer_name=True) -> list[tuple]:
    """Every (rank, 'table.column') a phrase could name, best first."""
    words = [sw for w in re.findall(r"[a-z0-9]+", phrase.lower()) if w not in STOP and (sw := singular(w))]
    if not words:
        return []
    last, qual = words[-1], (words[0] if len(words) > 1 else None)
    joined = "_".join(words)
    plain = len(words) > 1 and not set(words) & (DESC_WORDS | ASC_WORDS)  # 'highest rate' is not 'HR'
    initials = "".join(w[0] for w in words) if plain else None
    candidates = []
    for t, df in tables.items():
        for c in df.columns:
            cl, cw = c.lower(), col_words(c)
            cn = "_".join(cw)  # 'ChestPain' -> 'chest_pain'
            acronym = initials is not None and initials in abbreviations(c)  # 'heart rate' -> the HR in MaxHR
            if cl not in (last, joined, f"{last}_name", f"{last}_id", f"{joined}_id") and singular(cl) not in (last, joined) \
                    and cn != joined and singular(cn) != joined and not acronym:
                continue
            if qual and not acronym and singular(t) != qual and not cl.startswith(qual) and t != base:
                continue
            rank = (t != base, t != base or not cl.endswith("_id") or not prefer_name,  # the base table wins ties
                    0 if cl.endswith("_name") else 1 if cl in (last, joined) or cn == joined else 3 if acronym else 2)
            candidates.append((rank, f"{t}.{c}"))
    return sorted(candidates)


def col_words(name: str) -> list[str]:
    """The words of a column name, also split at case changes: 'MaxHR' -> ['max', 'hr'], 'order_id' -> ['order', 'id']."""
    return [w.lower() for w in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", name)]


def abbreviations(name: str) -> set[str]:
    """Capitalised abbreviations in a column name ('MaxHR' -> {'hr'}); ordinary words never count."""
    return {w.lower() for w in re.findall(r"[A-Z]{2,}(?![a-z])", name)}


def acronym_words(tokens: list[str], column: str) -> set[str]:
    """Question words whose initials spell a word of `column` ('heart rate' for the 'HR' in MaxHR)."""
    abbrevs = abbreviations(column)
    out = set()
    for n in (2, 3):
        for i in range(len(tokens) - n + 1):
            span = tokens[i:i + n]
            if "".join(w[0] for w in span) in abbrevs and not set(span) & (DESC_WORDS | ASC_WORDS):
                out |= set(span)
    return out


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
              "leading", "greatest", "strongest", "oldest", "longest"}
ASC_WORDS = {"lowest", "least", "bottom", "worst", "smallest", "fewest", "minimum", "min", "low", "lower", "weakest", "youngest", "shortest"}
# a single extreme record when nothing is grouped ("oldest age"); comparatives like "higher" are not
MAX_WORDS = {"highest", "largest", "biggest", "maximum", "max", "greatest", "oldest", "longest"}
MIN_WORDS = {"lowest", "smallest", "minimum", "min", "youngest", "shortest"}
# unambiguous currency names only ("dollar", "peso", "franc" name several currencies)
CURRENCY_WORDS = {"euro": "EUR", "euros": "EUR", "yen": "JPY", "sterling": "GBP",
                  "dong": "VND", "baht": "THB", "rupiah": "IDR", "ringgit": "MYR", "yuan": "CNY",
                  "renminbi": "CNY", "naira": "NGN", "rand": "ZAR", "zloty": "PLN", "lira": "TRY", "shekel": "ILS",
                  "rouble": "RUB", "ruble": "RUB", "hryvnia": "UAH", "forint": "HUF"}
CURRENCY_PHRASES = {"us dollar": "USD", "american dollar": "USD", "canadian dollar": "CAD",
                    "australian dollar": "AUD", "new zealand dollar": "NZD", "singapore dollar": "SGD",
                    "hong kong dollar": "HKD", "british pound": "GBP", "pound sterling": "GBP", "swiss franc": "CHF",
                    "indian rupee": "INR", "pakistani rupee": "PKR", "vietnamese dong": "VND", "japanese yen": "JPY",
                    "chinese yuan": "CNY", "mexican peso": "MXN", "south african rand": "ZAR"}
# names shared by several currencies: guessing one would be a silent assumption
AMBIGUOUS_CURRENCY_WORDS = {"dollar", "dollars", "pound", "pounds", "peso", "pesos", "franc", "francs", "rupee",
                            "rupees", "krona", "krone", "kroner", "kronor", "dinar", "dinars", "riyal", "riyals",
                            "shilling", "shillings"}
AGGREGATIONS = [("median", r"\bmedian\b"), ("mean", r"\b(average|avg|mean)\b"),
                ("count", r"\bhow many\b|\bnumber of\b|\bcount\b|\bno\.? of\b"),
                ("sum", r"\b(total|sum|overall|combined|how much)\b")]
FILLER = {"what", "which", "who", "whats", "is", "are", "was", "were", "the", "a", "an", "of", "in", "on", "for", "to",
          "by", "per", "with", "and", "or", "me", "show", "give", "tell", "list", "find", "get", "does", "do", "did",
          "has", "have", "had", "there", "how", "much", "many", "number", "each", "every", "all", "our", "my", "from",
          "between", "during", "this", "that", "than", "across", "breakdown", "split", "generated", "made", "value",
          "generate", "make", "sold", "brought", "earned", "it", "its", "please", "be", "where", "when"}


TIME_WORDS = {"month", "months", "monthly", "year", "years", "yearly", "annual", "annually"}
RATE_RE = re.compile(r"\b(rate|percentage|percent|share|proportion|fraction|ratio)\b|%")
VALUE_COL_SKIP_RE = re.compile(r"note|comment|description|desc$|email|address|phone|url")


def stem(word: str) -> str:
    """Crude inflection stripping so 'failure' / 'failed' and 'completion' / 'completed' meet."""
    for suf in ("ation", "ion", "ure", "ing", "ed", "es", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[:-len(suf)]
    return word


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _value_columns(tables: dict, profiles: dict):
    """(table, column, values, is_id) for columns whose values a question may name as a filter."""
    for t, p in profiles.items():
        if DERIVED_TABLE_RE.search(t):
            continue  # pre-aggregated tables are never a source of truth
        for c, cp in p.columns.items():
            cl = c.lower()
            if CURRENCY_COL_RE.search(cl) or UNIT_COL_RE.search(cl) or VALUE_COL_SKIP_RE.search(cl):
                continue
            if c == p.key or cl.endswith("_id"):
                yield t, c, sorted(set(tables[t][c]) - {""}), True
            elif cp.kind == "text" and cp.top_values:
                yield t, c, list(cp.top_values), False


def _value_filters(ql: str, spec: "QuerySpec", tables: dict, profiles: dict, used: set[str]):
    """Data values named in the question ('web', 'Europe', 'failed', 'C001') become equality filters.
    A value held by several columns, or several values of one column, is reported, never guessed."""
    toks = _tokens(ql)
    hits: dict[tuple, set] = {}
    for t, c, values, is_id in _value_columns(tables, profiles):
        for v in values:
            vt = _tokens(v)
            if not vt or len(vt) > 4:
                continue
            if is_id and not (re.search(r"\d", v) and re.search(r"[a-z]", v.lower())):
                continue  # only code-like identifiers (C001, P07); plain words are not matched as IDs
            for i in range(len(toks) - len(vt) + 1):
                window = toks[i:i + len(vt)]
                if any(w in used for w in window):
                    continue
                if window == vt or (len(vt) == 1 and not is_id and len(stem(window[0])) >= 4
                                    and stem(window[0]) == stem(vt[0])):
                    hits.setdefault(tuple(window), set()).add((t, c, v))
    by_column: dict[str, set] = {}
    for phrase, cands in hits.items():
        if any(set(phrase) < set(other) for other in hits):
            continue  # part of a longer matched value
        if len({(t, c) for t, c, _ in cands}) > 1:  # prefer the base table, then the table the value is a key of
            base = {x for x in cands if x[0] == spec.table}
            parent = {x for x in cands if profiles[x[0]].key == x[1]}
            cands = base if len(base) == 1 else parent if len(parent) == 1 else cands
        if len({(t, c) for t, c, _ in cands}) > 1:
            where = ", ".join(sorted(f"{t}.{c}" for t, c, _ in cands))
            spec.ambiguities.append(f"'{' '.join(phrase)}' appears in several columns ({where}); "
                                    "say which one the question means.")
            continue
        t, c, v = next(iter(cands))
        by_column.setdefault(f"{t}.{c}", set()).add(v)
    for col, vals in sorted(by_column.items()):
        if len(vals) > 1:
            spec.unsupported.append(f"The question names several values of {col} ({', '.join(sorted(vals))}); "
                                    "only one value per column can be used as a filter.")
            continue
        v = vals.pop()
        spec.filters.append(Filter(column=col, op="==", value=v))
        spec.notes.append(f"only rows where {col.split('.', 1)[1].replace('_', ' ')} is '{v}'")


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


# ordinary English words that happen to be close to data words ('last' -> 'least', 'report' -> 'reported');
# correcting them changes what the question means
NOT_TYPOS = {"last", "past", "next", "week", "weeks", "days", "quarter", "report", "reports", "since", "until",
             "before", "after", "except", "excluding", "without", "only", "spend", "spent", "paid", "state", "rates",
             "price", "prices", "value", "values", "order", "orders", "share"}


def _value_words(profiles: dict) -> set[str]:
    """Words of categorical values ('pending', 'web'): real data, never a misspelling of something else."""
    return {w for p in profiles.values() for cp in p.columns.values() for v in cp.top_values for w in _tokens(v)}


def _correct_typos(ql: str, vocab: set[str], notes: list[str], keep: set[str] = frozenset()) -> str:
    """Replace likely misspellings of data words ('revnue' -> 'revenue') with stdlib fuzzy matching, after reading
    curated synonyms ('clients' -> 'customers') as the data's own words."""
    ql = apply_synonyms(ql, vocab, notes)
    for w in sorted(set(re.findall(r"[a-z]+", ql))):
        if (len(w) < 5 or w in vocab or singular(w) in vocab or w in FILLER or w in NOT_TYPOS
                or w.upper() in CURRENCIES or w in keep):
            continue
        swaps = {w[:i] + w[i + 1] + w[i] + w[i + 2:] for i in range(len(w) - 1)}  # 'regoin' -> 'region'
        match = sorted(swaps & vocab)[:1] or difflib.get_close_matches(w, vocab, n=1, cutoff=0.84)
        if match:
            ql = re.sub(rf"\b{w}\b", match[0], ql)
            notes.append(f"'{w}' read as '{match[0]}'")
    return ql


def _currency(ql: str) -> str | None:
    for phrase, code in CURRENCY_PHRASES.items():
        if re.search(rf"\b{phrase}s?\b", ql):
            return code
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
    every = [w for w in re.findall(r"[a-z_]+", ql) if w not in FILLER]  # pairs keep 'max' for a column like MaxHR
    words = [w for w in every if w not in DESC_WORDS | ASC_WORDS]
    for phrase in [" ".join(every[i:i + 2]) for i in range(len(every) - 1)] + words:
        col = resolve_column(phrase, None, tables, prefer_name=False)
        if col and _is_measure(col, profiles):
            return phrase, col
    return None


def _scan_dimension(ql: str, spec: QuerySpec, tables: dict, profiles: dict, skip: set[str]):
    """Find an entity to group by when the question names one without 'by' ('which region ...')."""
    words = [w for w in re.findall(r"[a-z_]+", ql)
             if w not in FILLER and w not in skip and w not in DESC_WORDS | ASC_WORDS and w.upper() not in CURRENCIES
             and w not in CURRENCY_WORDS and w not in MONTHS and w not in TIME_WORDS]
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
    grouping = bool(order or re.search(r"\b(which|by|per|each|every)\b", ql))
    named = list(dict.fromkeys(t for w in re.findall(r"[a-z_]+", ql) if (t := _match_table(w, tables))))
    if grouping and len(named) >= 2:  # 'which customer placed the most orders': count orders per customer
        for parent in named:
            pkey = profiles[parent].key
            child = next((c for c in named if c != parent and pkey and pkey in tables[c].columns), None)
            if child:
                label = next((c for c in tables[parent].columns if c.endswith("_name")), pkey)
                ckey = profiles[child].key
                spec.metric_term, spec.table = child, child
                spec.aggregation, spec.measure = ("count_distinct", ckey) if ckey else ("count", None)
                spec.group_by = f"{parent}.{label}"
                spec.notes.append(f"{child} counted per {singular(parent)} (linked by {pkey})")
                return True
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


PERIOD_RE = (r"(?:(?:" + "|".join(MONTHS) + r") (?:19|20)\d{2}|q[1-4] (?:19|20)\d{2}|(?:19|20)\d{2})")
COMPARE_RE = re.compile(rf"\b({PERIOD_RE})\s+(?:compared (?:to|with)|vs\.?|versus|against)\s+({PERIOD_RE})\b")


def _period_key(text: str) -> str:
    parts = text.split()
    if len(parts) == 1:
        return parts[0]
    if parts[0].startswith("q"):
        return f"{parts[1]}-Q{parts[0][1]}"
    return f"{parts[1]}-{MONTHS[parts[0]]:02d}"


def _compared_periods(ql: str) -> list[str] | None:
    """'March 2024 vs February 2024', 'Q1 2024 compared to Q1 2023': two periods of the same kind."""
    m = COMPARE_RE.search(ql)
    if not m:
        return None
    a, b = _period_key(m.group(1)), _period_key(m.group(2))
    return [a, b] if len(a) == len(b) and ("Q" in a) == ("Q" in b) and a != b else None


DIFF_RE = re.compile(r"\b(?:days?|time|duration)\s+(?:between|from)\s+(?:the\s+)?([a-z_ ]+?)\s+(?:and|to)\s+"
                     r"(?:the\s+)?([a-z_ ]+?)(?=\s+(?:in|for|by|per|across|during)\b|\?|$)")
DIFF_WORDS = {"days", "day", "time", "duration", "between", "from", "to", "and", "elapsed", "took", "take", "takes"}


def _date_diff(ql: str, spec: QuerySpec, tables: dict, profiles: dict) -> bool:
    """'average days between order date and ship date': both dates named; a date in a referring (many) table
    makes that table the base, joined many-to-one to the other."""
    m = DIFF_RE.search(ql)
    if not m:
        return False
    refs = [resolve_column(p, None, tables, prefer_name=False) for p in m.groups()]
    if not all(refs) or any(profiles[r.split(".")[0]].columns[r.split(".", 1)[1]].kind not in ("date", "datetime")
                            for r in refs) or refs[0] == refs[1]:
        spec.unresolved.append(f"{m.group(1)} / {m.group(2)} as two date columns")
        return True
    (ta, _), (tb, _) = (r.split(".", 1) for r in refs)
    base = ta
    if ta != tb:
        if profiles[ta].key and profiles[ta].key in tables[tb].columns:
            base = tb  # tb refers to ta: one ta row per tb row
        elif not (profiles[tb].key and profiles[tb].key in tables[ta].columns):
            spec.unresolved.append(f"a link between {ta} and {tb}")
            return True
    spec.table, spec.measure, spec.date_diff = base, None, refs
    spec.metric_term = f"days from {m.group(1)} to {m.group(2)}"
    spec.notes.append(f"days from {refs[0]} to {refs[1]}, per {singular(base)}")
    return True


ANTI_RE = re.compile(r"\b(?:how many|number of|count(?: of)?)\s+([a-z_]+)\s+(?:have|has|had|with|made|placed)?\s*"
                     r"(?:no|zero|without(?: any)?|never (?:placed|made|had) any)\s+([a-z_]+)")


def _threshold_filters(ql: str, spec: QuerySpec, tables: dict, profiles: dict, metrics: dict) -> set[str]:
    """'orders over 1000', 'with quantity of at least 5': a per-record numeric condition. The compared column is
    the one named before the comparison, else the measure, else the only metric defined on the table."""
    used: set[str] = set()
    for m in THRESHOLD_RE.finditer(ql):
        if m.group("code") and m.group("code").upper() not in CURRENCIES:
            continue
        col = None
        if m.group("col"):
            for phrase in (m.group("col"), m.group("col").split()[-1]):
                ref = resolve_column(phrase, spec.table, tables, prefer_name=False)
                if ref and _is_measure(ref, profiles) and ref.split(".")[0] == spec.table:
                    col = ref
                    used |= set(phrase.split())
                    break
        if not col and spec.measure and _is_measure(f"{spec.table}.{spec.measure}", profiles):
            col = f"{spec.table}.{spec.measure}"
        if not col and spec.table:
            defined = {d.get("column") for d in metrics.values() if d.get("table") == spec.table and d.get("column")}
            col = f"{spec.table}.{defined.pop()}" if len(defined) == 1 else None
        num = m.group("num").replace(",", "")
        if not col:
            spec.ambiguities.append(f"'{m.group('op')} {m.group('num')}' names no column to compare; say which "
                                    "(for example 'with amount over ...').")
            continue
        spec.filters.append(Filter(column=col, op=THRESHOLD_OPS[m.group("op")], value=num))
        spec.notes.append(f"kept {spec.table} where {col.split('.', 1)[1].replace('_', ' ')} "
                          f"{THRESHOLD_OPS[m.group('op')]} {num} (each record)")
        used |= set(m.group("op").split()) | {m.group("num")}
    return used


def parse_question(question: str, tables: dict, profiles: dict[str, TableProfile], metrics: dict) -> QuerySpec:
    spec = QuerySpec()
    notes = spec.notes
    ql = _correct_typos(question.strip().lower(), _vocabulary(tables, metrics), notes, _value_words(profiles))

    spec.currency = _currency(ql)
    if (m := RELATIVE_RE.search(ql)) and not re.search(r"\b(will|forecast|predict|projected?)\b", ql):
        spec.ambiguities.append(f"'{m.group(0)}' depends on today's date, which the data does not fix; name the "
                                "period (for example 'Q4 2024' or 'March 2024').")
    if not spec.currency and (w := next((t for t in _tokens(ql) if t in AMBIGUOUS_CURRENCY_WORDS), None)):
        spec.ambiguities.append(f"'{w}' names several currencies; state the currency code (for example USD, GBP).")
    if (m :=re.search(r"\b(\d{4})\s*(?:to|vs\.?|versus|and|-)\s*(\d{4})\b", ql)) and \
            re.search(r"\b(growth|grow|grew|change|increase|decrease|rise|fall)\b", ql):
        spec.growth_from, spec.growth_to = m.groups()
    if (cmp := _compared_periods(ql)) and not spec.growth_from:
        spec.periods = cmp
        notes.append(f"compares {cmp[0]} with {cmp[1]}")
    elif m := re.search(r"between (\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2})", ql):
        spec.date_from, spec.date_to = m.groups()
    elif m := re.search(r"\b(?:on|for|at) (\d{4}-\d{2}-\d{2})\b", ql):
        spec.date_from = spec.date_to = m.group(1)
    elif m := QUARTER_RE.search(ql):  # 'Q1 2024', 'first quarter of 2024'
        q, y = int(m.group(1) or ORDINAL[m.group(2)]), int(m.group(3))
        spec.date_from = f"{y}-{3 * q - 2:02d}-01"
        spec.date_to = f"{y}-{3 * q:02d}-{calendar.monthrange(y, 3 * q)[1]:02d}"
    elif m := HALF_RE.search(ql):  # 'H2 2023', 'first half of 2024'
        h, y = int(m.group(1) or ORDINAL[m.group(2)]), int(m.group(3))
        spec.date_from, spec.date_to = (f"{y}-01-01", f"{y}-06-30") if h == 1 else (f"{y}-07-01", f"{y}-12-31")
    elif m := re.search(r"\b(?:in|during|for) (" + "|".join(MONTHS) + r") (\d{4})\b", ql):
        y, mo = int(m.group(2)), MONTHS[m.group(1)]
        spec.date_from, spec.date_to = f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    elif not spec.growth_from and (m := re.search(r"\b((?:19|20)\d{2})\b(?!-)", ql)):
        spec.date_from, spec.date_to = f"{m.group(1)}-01-01", f"{m.group(1)}-12-31"
    if re.search(r"\bmonthly\b|\b(per|by|each|every) month\b|\bmonth by month\b|\bover time\b|\btrend\b", ql):
        spec.time_grain = "month"
    elif re.search(r"\b(yearly|annual|annually)\b|\b(per|by|each|every) year\b", ql) and not spec.growth_from:
        spec.time_grain = "year"
    elif m := re.search(r"\b(?:which|what) (month|year)\b", ql):  # "which month had the most ..." is a time grain
        spec.time_grain = m.group(1)

    order_word = next((w for w in re.findall(r"[a-z]+", ql) if w in DESC_WORDS | ASC_WORDS), None)
    order = "asc" if order_word in ASC_WORDS else "desc" if order_word else None
    n = None
    if m := re.search(r"\b(?:top|bottom|first|last)\s+(\d+)\b|\b(\d+)\s+(?:highest|lowest|largest|smallest|best|worst|most|least|biggest)\b", ql):
        n = int(m.group(1) or m.group(2))
        order = order or "desc"
    explicit_agg = next((a for a, pat in AGGREGATIONS if re.search(pat, ql)), None)

    # 0) 'customers with no orders': rows of one table with no match in a table that refers to it
    if (m := ANTI_RE.search(ql)) and (parent := _match_table(m.group(1), tables)) and \
            (child := _match_table(m.group(2), tables)) and (key := profiles[parent].key) and key in tables[child].columns:
        spec.metric_term, spec.table, spec.without = f"{parent} without {child}", parent, child
        spec.aggregation, spec.measure = "count_distinct", key
        notes.append(f"{parent} whose {key} appears in no {child} row")
        return spec

    # 0b) 'average days between order date and ship date'
    if _date_diff(ql, spec, tables, profiles):
        spec.aggregation = explicit_agg if explicit_agg in ("mean", "median", "sum") else "mean"
        if order_word in MAX_WORDS | MIN_WORDS:
            spec.aggregation = "max" if order == "desc" else "min"
        return spec

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
        if d.get("aggregation", "sum") == "sum" and explicit_agg in ("mean", "median") and re.search(r"\bper\b", ql):
            spec.ambiguities.append(f"'{explicit_agg} {hit} per ...' could mean the {explicit_agg} single record "
                                    "in each group or the average of each group's total; ask for one of them "
                                    f"(for example '{hit} by ...' or a defined per-record metric).")
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
            best = column_candidates(phrase, None, tables, prefer_name=False)
            tied = sorted({ref for rank, ref in best if rank == best[0][0] and _is_measure(ref, profiles)}) if best else []
            named = [ref for ref in tied if _match_table(ref.split(".")[0], tables) and
                     any(_match_table(w, tables) == ref.split(".")[0] for w in _tokens(ql))]
            if len(tied) > 1 and len(named) == 1:  # 'amount of completed payments' names the table
                tied = named
                spec.table, spec.measure = named[0].split(".")
                metric_words |= _name_words(spec.table)
            if len(tied) > 1:  # 'amount' in both orders and payments: picking one would be a guess
                spec.ambiguities.append(f"'{phrase}' could be {' or '.join(tied)}; name the table "
                                        f"(for example '{tied[0].split('.')[0]} {phrase}').")
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
    if group_phrase:  # drop currency codes, years and filler: "carrier usd" -> "carrier"; keep 'a and b' apart
        parts = [" ".join(w for w in part.split() if w.upper() not in CURRENCIES and w not in CURRENCY_WORDS
                          and not w.isdigit() and w not in FILLER and w not in metric_words)
                 for part in re.split(r"\s+and\s+", group_phrase, maxsplit=1)]
        group_phrase = " and ".join(p for p in parts if p) or None
    unmatched_group = None
    if spec.group_by:
        pass
    elif group_phrase and " and " in group_phrase:  # 'by region and channel'
        cols = [resolve_column(p.strip(), spec.table, tables) for p in group_phrase.split(" and ", 1)]
        if all(cols) and cols[0] != cols[1]:
            spec.group_by, spec.group_by2 = cols
        else:
            unmatched_group, group_phrase = group_phrase.strip(), None
    elif group_phrase and group_phrase.strip() not in ("month", "year", spec.metric_term):
        col = resolve_column(group_phrase, spec.table, tables)
        if col:
            spec.group_by = col
        else:  # may be a value ("shipped by Blue Arrow"); decided after value filters
            unmatched_group, group_phrase = group_phrase.strip(), None
    elif not group_phrase and not spec.time_grain and (order or re.search(r"\b(which|each|every|breakdown|split)\b", ql)):
        found = _scan_dimension(ql, spec, tables, profiles, metric_words | {"total", "average", "median", "count"})
        if found:
            spec.group_by = found[1]
            notes.append(f"grouped by {found[1].split('.', 1)[1].replace('_', ' ')} (from '{found[0]}')")
    used = metric_words | set((group_phrase or "").split()) | set(_tokens(spec.group_by or ""))
    used |= _threshold_filters(ql, spec, tables, profiles, metrics)
    _value_filters(ql, spec, tables, profiles, used)
    if unmatched_group and not any(set(_tokens(unmatched_group)) <= set(_tokens(f.value)) for f in spec.filters):
        spec.unresolved.append(unmatched_group)
    if RATE_RE.search(ql) and not spec.ratio_filter and spec.filters:  # "percentage of payments that failed"
        if spec.measure and spec.aggregation == "sum" and len(spec.filters) == 1:
            f = spec.share_filter = spec.filters.pop()
            notes.pop()
            notes.append(f"share = sum where {f.column.split('.', 1)[1].replace('_', ' ')} is '{f.value}' / sum of all")
        elif spec.measure and spec.aggregation in ("sum", "mean", "median", "max", "min"):
            spec.unsupported.append("Only the share of a total (a sum) is supported, with one condition.")
        elif len(spec.filters) > 1:
            spec.unsupported.append("A rate with several conditions is unclear (which one is the numerator?); "
                                    "ask with a single condition.")
        else:
            f = spec.ratio_filter = spec.filters.pop()
            notes.pop()
            spec.measure, spec.aggregation = None, "count"
            spec.metric_term = f"share of {spec.table} where {f.column.split('.', 1)[1]} is '{f.value}'"
            notes.append(f"rate = {spec.table} rows where {f.column.split('.', 1)[1].replace('_', ' ')} is "
                         f"'{f.value}' / all {spec.table} rows")
    if order and (spec.group_by or spec.time_grain) and spec.kind not in ("ratio", "growth"):
        spec.top_n, spec.order = n or 1, order
        if not n:
            notes.append(f"'{order_word}' read as the single {'highest' if order == 'desc' else 'lowest'} value")
    elif order_word and not spec.group_by and not spec.time_grain:
        if order_word in MAX_WORDS | MIN_WORDS and spec.measure and spec.aggregation == "sum" and not spec.ratio_filter and not spec.growth_from:
            spec.aggregation = "max" if order == "desc" else "min"  # "oldest age", "highest revenue"
            notes.append(f"'{order_word}' read as the {'largest' if order == 'desc' else 'smallest'} single value")
        else:
            notes.append(f"'{order_word}' ignored: the question names nothing to rank")
    if spec.date_from or spec.time_grain or spec.growth_from or spec.periods:
        spec.date_column = _date_column(spec.table, profiles, ql, notes)
        if not spec.date_column:
            spec.unresolved.append(f"date for {spec.table}")
    return spec


# Words that carry no analytical meaning of their own. Deliberately absent: negations (not, except, excluding,
# without, only), comparisons (than, more, greater, above, below), relative time (last, this year, recent),
# and anything a filter or a second metric could hide behind.
GRAMMAR = {"what", "whats", "which", "who", "whom", "whose", "how", "much", "many", "is", "are", "was", "were", "be",
           "been", "being", "am", "the", "a", "an", "of", "in", "on", "at", "for", "to", "by", "per", "with", "and",
           "or", "from", "between", "during", "within", "across", "each", "every", "all", "there", "it", "its", "this",
           "that", "these", "those", "me", "show", "give", "tell", "list", "find", "get", "calculate", "compute",
           "does", "do", "did", "has", "have", "had", "please", "our", "my", "we", "i", "you", "s", "total",
           "overall", "value", "values", "data", "dataset", "figure", "made", "placed", "recorded", "generated",
           "earned", "brought", "sold", "record", "records", "row", "rows", "entry", "entries", "unit", "up",
           "breakdown", "split", "grouped", "group", "us", "selling", "sell", "sells", "came", "come", "comes",
           "through", "via", "make", "makes",
           # request wording: "I'd like to know", "could you work out", "hey, quick question", "please report"
           "d", "like", "know", "could", "would", "can", "work", "out", "hey", "hi", "quick", "question", "report",
           "want", "need", "u", "ur", "pls", "plz"}
AGG_WORDS = {"mean": {"average", "avg", "mean"}, "median": {"median"},
             "max": MAX_WORDS, "min": MIN_WORDS, "sum": {"sum", "combined"},
             "count": {"count", "number", "many"}, "count_distinct": {"count", "number", "many", "distinct", "unique",
                                                                      "different"}}


def blocked(spec: QuerySpec) -> bool:
    """The reading already says the question cannot be answered as asked."""
    return bool(spec.unresolved or spec.ambiguities or spec.unsupported)


def vocabulary(tables: dict, metrics: dict) -> set[str]:
    return _vocabulary(tables, metrics)


def names_measure(question: str, spec: QuerySpec, tables: dict, metrics: dict) -> bool:
    """Does the question itself name what `spec` measures? A model may only fill in a reading for wording the rules
    could not read; it may not pick the measure ('best selling' -> quantity) or a metric the data does not define."""
    toks = set(_tokens(_correct_typos(question.lower(), _vocabulary(tables, metrics), [])))
    key = _metric_names(metrics).get((spec.metric_term or "").lower())
    if key and metrics[key].get("table") == spec.table and any(set(_tokens(n)) <= toks for n in [key, *metrics[key].get("aliases", [])]):
        return True
    if spec.ratio_filter or spec.aggregation in ("count", "count_distinct"):  # counts name their entity (table)
        return bool(_name_words(spec.table or "") & toks)
    if not spec.measure:
        return False
    stems = {w for w in col_words(spec.measure) if len(w) >= 4}
    return bool(_name_words(spec.measure) & toks or acronym_words(sorted(toks), spec.measure) or acronym_words(
        _tokens(question.lower()), spec.measure) or any(t.startswith(st) for t in toks for st in stems))


def ground(question: str, spec: QuerySpec, tables: dict, profiles: dict, metrics: dict) -> QuerySpec:
    """Block a reading that leaves part of the question unused (see unexplained_terms). Returns `spec`."""
    if blocked(spec):
        return spec
    left = unexplained_terms(question, spec, tables, profiles, metrics)
    if not left:
        return spec
    vocab = _vocabulary(tables, metrics)
    if any(w in vocab or w.isdigit() for w in left):  # known words used in a way the spec cannot express
        words = ", ".join(repr(w) for w in left)
        spec.unsupported.append(f"The question also says {words}, which this reading cannot use; answering "
                                "without it would answer a different question.")
    else:
        spec.unresolved.append(", ".join(left))
    return spec


def _name_words(name: str) -> set[str]:
    words = set(_tokens(name.replace("_", " "))) | set(col_words(name))
    return words | {singular(w) for w in words}


def unexplained_terms(question: str, spec: QuerySpec, tables: dict, profiles: dict, metrics: dict) -> list[str]:
    """Words of the question that the reading in `spec` does not use.

    Whatever produced the spec (rule-based parser or a model), an answer is only trustworthy if every
    meaningful word of the question shaped it. A leftover word is a qualifier that was dropped ("for
    customer C001", "failed", "in Q1", "excluding refunds") and would make the number answer a different
    question. Sentences that are instruction-like (prompt injection) are not part of the question."""
    sentences = [x for x in re.split(r"(?<=[.?!;])\s+|\n+", question) if x.strip() and not is_instruction_like(x)]
    ql = _correct_typos(" ".join(sentences).lower(), _vocabulary(tables, metrics), [], _value_words(profiles))
    for d in {spec.date_from, spec.date_to} - {None}:
        ql = ql.replace(d, " ")
    refs = spec.refs()
    data: set[str] = set()
    for t in {spec.table} | {r.split(".", 1)[0] for r in refs if r}:
        data |= _name_words(t or "")
    for r in filter(None, refs):
        data |= _name_words(r.split(".", 1)[-1])  # a model may omit the table
    for c in [r.split(".", 1)[-1] for r in filter(None, refs)] + [spec.measure or ""]:
        data |= acronym_words(_tokens(ql), c)  # 'heart rate' names MaxHR only by its initials
        stems = {w for w in col_words(c) if len(w) >= 4}  # 'cholesterol' for a column named Chol
        data |= {t for t in _tokens(ql) if any(t.startswith(st) for st in stems)}
    if spec.measure:
        data |= _name_words(spec.measure)
        if spec.table in tables and (unit_col := unit_column_for(tables[spec.table], spec.measure)):
            data |= {w for v in set(tables[spec.table][unit_col]) for w in _tokens(v)}  # 'in kg': units are checked
    key = _metric_names(metrics).get((spec.metric_term or "").lower())
    if key and metrics[key].get("table") == spec.table:
        for name in [key, *metrics[key].get("aliases", [])]:
            data |= _name_words(name)
    elif spec.measure and resolve_column(spec.metric_term or "", None, tables, prefer_name=False) == \
            f"{spec.table}.{spec.measure}":
        data |= _name_words(spec.metric_term)
    values = [f.value for f in spec.filters] + [x.value for x in (spec.ratio_filter, spec.share_filter) if x]
    for v in values:
        data |= set(_tokens(v))
    value_stems = {stem(w) for v in values for w in _tokens(v)}

    explained = GRAMMAR | data
    if not spec.ratio_filter:  # a rate ignores the aggregation
        explained |= AGG_WORDS.get(spec.aggregation, set())
    if spec.currency:
        explained |= {spec.currency.lower(), "currency", "converted", "convert", "conversion", "exchange", "rate",
                      "rates", "using", "expressed"}
        explained |= {w for w, c in CURRENCY_WORDS.items() if c == spec.currency}
        explained |= {w for ph, c in CURRENCY_PHRASES.items() if c == spec.currency for w in ph.split()}
        explained |= {w + "s" for ph, c in CURRENCY_PHRASES.items() if c == spec.currency for w in ph.split()}
    explained |= {d[:4] for d in (spec.date_from, spec.date_to, spec.growth_from, spec.growth_to) if d}
    if spec.date_from and (QUARTER_RE.search(ql) or HALF_RE.search(ql)):
        explained |= PERIOD_WORDS
    if spec.date_from and spec.date_from[5:7] == spec.date_to[5:7]:  # 'in March 2024'
        mo = int(spec.date_from[5:7])
        explained |= {calendar.month_name[mo].lower(), calendar.month_abbr[mo].lower()}
    if spec.time_grain == "month":
        explained |= {"month", "months", "monthly", "trend", "over", "time"}
    elif spec.time_grain == "year":
        explained |= {"year", "years", "yearly", "annual", "annually"}
    if spec.growth_from:
        explained |= {"growth", "grow", "grew", "change", "increase", "decrease", "rise", "fall", "vs", "versus",
                      "percentage", "percent", "pct"}
    if spec.top_n:
        explained |= DESC_WORDS | ASC_WORDS | {"top", "bottom", "first", "rank", "ranked", "ranking", str(spec.top_n)}
    if spec.ratio_filter:
        explained |= {"rate", "percentage", "percent", "share", "proportion", "fraction", "ratio"}
    if spec.share_filter:
        explained |= {"share", "percentage", "percent", "proportion", "fraction", "comes", "come", "contribute",
                      "contributes", "contributed", "accounted", "account", "accounts"}
    if spec.periods:
        explained |= {"compared", "compare", "vs", "versus", "against"} | {p[:4] for p in spec.periods} | PERIOD_WORDS
        explained |= {calendar.month_name[int(p[5:7])].lower() for p in spec.periods if len(p) == 7 and "Q" not in p}
    if spec.date_diff:
        explained |= DIFF_WORDS
    if spec.without:
        explained |= {"no", "zero", "without", "any", "never", "have", "has", "had", "placed", "made"}
        explained |= _name_words(spec.without)
    thresholds = {f.value for f in spec.filters if f.op in (">", ">=", "<", "<=")}
    for m in THRESHOLD_RE.finditer(ql):
        if m.group("num").replace(",", "") in thresholds:  # the comparison and its number are used
            explained |= THRESHOLD_WORDS | set(_tokens(m.group("num"))) | {(m.group("code") or "")}

    out: list[str] = []
    for w in _tokens(ql):
        if w in out or w in explained or singular(w) in explained:
            continue
        s = stem(w)
        if any(len(p := os.path.commonprefix([w, d])) >= 4 and len(p) >= len(s) - 1 for d in data):
            continue  # an inflection of a data word: 'shipped' -> shipments, 'ordered' -> orders, 'signed' -> signup
        if len(s) >= 4 and (s in value_stems or any(d.startswith(s) for d in data)):  # 'failure' -> 'failed'
            continue
        out.append(w)
    return out


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
