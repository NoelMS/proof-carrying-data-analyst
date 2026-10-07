"""Turn a refusal caused by vague wording into questions the data can answer.

A refusal stays a refusal: nothing is answered on the user's behalf. Each suggestion is a rewritten
question that has already passed interpretation, the every-word check and every answerability check,
so choosing it runs a normal, fully verified analysis. Questions the data cannot answer at all (no such
metric, contradictory or missing data) get no rewrites, only a hint about what the data does hold.
"""
import os
import re

from .answerability import Assessment, assess
from .catalog import Catalog
from .local_model import reading
from .question import (AMBIGUOUS_CURRENCY_WORDS, CURRENCY_WORDS, GRAMMAR, QuerySpec, _is_measure, _match_table, _tokens,
                       _value_columns, _value_words, blocked, ground, parse_question, singular, unexplained_terms,
                       vocabulary)
from .traps import DERIVED_TABLE_RE

MAX_SUGGESTIONS = 4
MAX_DEPTH = 3  # rewrites stack: 'total amount' -> 'orders amount' -> '... in USD'
NEGATIONS = {"not", "no", "except", "excluding", "exclude", "without", "never", "other"}
COMPARISON = {"than", "compared", "vs", "versus", "higher", "lower", "more", "less", "and"}
DANGLING = {"in", "on", "for", "from", "of", "the", "a", "an", "and", "or", "than", "to", "with", "by", "during",
            "compared", "excluding", "except", "over", "at", "did", "does", "do", "were", "was", "is", "are", "be",
            "total"}
NO_REWRITE = {"why", "will", "would", "should", "predict", "forecast", "explain", "cause", "reason", "answer"}
CURRENCY_NAMES = {"dollar": ["USD", "CAD", "AUD", "NZD", "SGD", "HKD"], "pound": ["GBP", "EGP"],
                  "peso": ["MXN", "ARS", "CLP", "COP", "PHP"], "franc": ["CHF"],
                  "rupee": ["INR", "PKR", "LKR", "NPR"], "krona": ["SEK"], "krone": ["NOK", "DKK"],
                  "kroner": ["NOK", "DKK"], "kronor": ["SEK"], "dinar": ["KWD"], "riyal": ["SAR", "QAR"],
                  "shilling": ["KES"]}


def _tidy(q: str) -> str:
    q = re.sub(r"\s+", " ", q).strip()
    q = re.sub(r"\s+([?.!,;])", r"\1", q)
    while True:  # words left hanging by a removal: "revenue in USD in the?" -> "revenue in USD?"
        m = re.search(r"\s(\w+)([?.!]*)$", q)
        if not m or m.group(1).lower() not in DANGLING:
            return q
        q = q[:m.start()] + m.group(2)


def _drop(q: str, words) -> str:
    for w in words:
        q = re.sub(rf"\b{re.escape(w)}\b", " ", q, flags=re.I)
    parts = re.split(r"(?<=[?.!;])\s+", q)  # a sentence left with no content is removed whole
    return _tidy(" ".join(_tidy(p) for p in parts if any(t not in GRAMMAR for t in _tokens(p))))


def _append(q: str, phrase: str) -> str:
    m = re.search(r"[?.!]*\s*$", q)
    return _tidy(f"{q[:m.start()]} {phrase}{m.group(0).strip()}")


def _replace(q: str, word: str, new: str) -> str:
    return _tidy(re.sub(rf"\b{re.escape(word)}\b", new, q, count=1, flags=re.I))


def _replace_phrase(q: str, phrase: str, new: str) -> str:
    return _tidy(re.sub(re.escape(phrase), new, q, count=1, flags=re.I))


def _latest_period(phrase: str, spec: QuerySpec, cat: Catalog) -> str | None:
    """The most recent complete month / quarter / year in the table the question is about."""
    import calendar
    p = cat.profiles.get(spec.table or "")
    dates = [cp.max for cp in (p.columns.values() if p else []) if cp.kind in ("date", "datetime") and cp.max]
    if not dates:
        return None
    y, mo = int(max(dates)[:4]), int(max(dates)[5:7])
    unit = next((u for u in ("quarter", "month", "year") if u in phrase.lower()), "year")
    back = 1 if re.search(r"last|previous|past|prior", phrase.lower()) else 0
    if unit == "month":
        mo -= back
        if mo == 0:
            y, mo = y - 1, 12
        return f"in {calendar.month_name[mo]} {y}"
    if unit == "quarter":
        qn = (mo - 1) // 3 + 1 - back
        if qn == 0:
            y, qn = y - 1, 4
        return f"in Q{qn} {y}"
    return f"in {y - back}"


def rate_targets(cat: Catalog) -> list[str]:
    """Currencies the data can convert into (columns like rate_to_usd)."""
    return sorted({m.group(1).upper() for df in cat.tables.values() for c in df.columns
                   if (m := re.fullmatch(r".*rate_to_([a-z]{3})", c.lower()))})


def _check(q: str, cat: Catalog) -> tuple[QuerySpec, Assessment]:
    spec = ground(q, parse_question(q, cat.tables, cat.profiles, cat.metrics), cat.tables, cat.profiles, cat.metrics)
    return spec, assess(spec, cat)


def _label(col: str) -> str:
    return col.split(".", 1)[1].replace("_", " ")


def _candidates(q: str, spec: QuerySpec, a: Assessment, cat: Catalog) -> list[tuple[str, str]]:
    """(rewritten question, what the rewrite decides) for each way the wording could be made specific."""
    out: list[tuple[str, str]] = []
    reasons = " ".join(a.reasons)
    toks = _tokens(q)

    if "ask for a reporting currency" in reasons:  # mixed currencies, none requested
        out += [(_append(q, f"in {t}"), f"converted to {t} with the exchange rates in the data")
                for t in rate_targets(cat)]
        if spec.table:
            out.append((_append(q, f"by {singular(spec.table)} currency"), "one figure per currency, nothing converted"))

    for msg in spec.ambiguities:  # 'last quarter': offer the latest such period in the data
        if (m := re.match(r"'(.+?)' depends on today's date", msg)) and (period := _latest_period(m.group(1), spec, cat)):
            out.append((_replace_phrase(q, m.group(1), period), f"'{m.group(1)}' read as {period}, the latest in the data"))

    if m := re.search(r"no exchange rates to ([A-Z]{3})", reasons):  # 'in INR' with no INR rate: offer what exists
        code = m.group(1)
        named = next((w for w in re.findall(r"[A-Za-z]+", q) if w.upper() == code or CURRENCY_WORDS.get(w.lower()) == code), None)
        for t in rate_targets(cat):
            out.append((_replace(q, named, t) if named else _append(q, f"in {t}"),
                        f"in {t} instead: the data has no exchange rate to {code}"))

    for w in toks:  # 'dollars', 'pounds': offer only the currencies that name can mean
        if w in AMBIGUOUS_CURRENCY_WORDS:
            for code in CURRENCY_NAMES.get(singular(w), []):
                out.append((_replace(q, w, code), f"'{w}' read as {code}"))

    for msg in spec.ambiguities + a.reasons:
        if m := re.match(r"'(.+?)' could be (.+?); name the table", msg):  # a column in several tables
            for t, _ in re.findall(r"(\w+)\.(\w+)", m.group(2)):
                out.append((_replace(q, m.group(1), f"{singular(t)} {m.group(1)}"), f"uses the {t} table"))
        if m := re.match(r"'(mean|median) (.+?) per \.\.\.'", msg):  # average X per Y
            per = _replace(q, "per", "by")
            out.append((per, f"the {m.group(1)} single record in each group"))
            out.append((_drop(per, ["average", "avg", "mean", "median"]), "the total of each group"))
        if m := re.match(r"(\w+)\.(\w+) has the values (.+?); it is unclear which (\w+)", msg):  # status column
            table, col = m.group(1), m.group(2)
            out += [(_append(q, f"for {v} {table}"), f"only {v} {table}") for v in m.group(3).split(", ")]
            out.append((_append(q, f"by {col.replace('_', ' ')}"), f"one figure per {col.replace('_', ' ')}"))
        if m := re.match(r"The question names several values of (\w+)\.(\w+) \((.+?)\)", msg):  # 'web or partner'
            vals = m.group(3).split(", ")
            for v in vals:
                others = [w for o in vals if o != v for w in _tokens(o)]
                out.append((_drop(q, others + ["or"]), f"only {_label(m.group(1) + '.' + m.group(2))} '{v}'"))

    raw = parse_question(q, cat.tables, cat.profiles, cat.metrics)
    if not blocked(raw) and (left := unexplained_terms(q, raw, cat.tables, cat.profiles, cat.metrics)):
        out += _for_unused_words(q, raw, left, cat)
    return out


def _known(word: str, vocab: set[str]) -> bool:
    return any(len(os.path.commonprefix([word, v])) >= 4 or word == v for v in vocab)


def _for_unused_words(q: str, spec: QuerySpec, left: list[str], cat: Catalog) -> list[tuple[str, str]]:
    out = []
    years = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", q)))
    if len(years) == 2 and set(left) & COMPARISON and spec.measure:  # 'higher in 2024 than 2023'
        cur = f" in {spec.currency}" if spec.currency else ""
        out.append((f"What was the {spec.metric_term} growth{cur} from {years[0]} to {years[1]}?",
                     f"compares {years[0]} with {years[1]} as a growth rate"))
        return out  # leaving out one side of a comparison would answer something else
    for tok in [w for w in left if w.isdigit()]:  # 'customer 001' -> C001, only for an entity the question names
        named = {t for w in _tokens(q) if (t := _match_table(w, cat.tables))}
        ids = {v for t, c, values, is_id in _value_columns(cat.tables, cat.profiles)
               if is_id and any(c == cat.profiles[n].key for n in named) for v in values
               if re.sub(r"\D", "", v) == tok}
        if len(ids) == 1:
            v = ids.pop()
            out.append((_replace(q, tok, v), f"'{tok}' read as {v}"))
    if _droppable(q, spec, left, cat):
        out.append((_drop(q, left), "leaves out " + ", ".join(repr(w) for w in left)))
    return out


def _droppable(q: str, spec: QuerySpec, left: list[str], cat: Catalog) -> bool:
    """Offering the question without its unused words is only honest when what remains asks about the same
    thing: not for explanations or forecasts, not when the subject or a negated data word would go, and not
    when most of the question would go."""
    words = set(left)
    if words & NO_REWRITE or any(_match_table(w, cat.tables) for w in left):
        return False
    vocab = vocabulary(cat.tables, cat.metrics) | _value_words(cat.profiles)
    if words & NEGATIONS and (spec.filters or spec.ratio_filter or  # 'not from web' is not 'from web'
                              any(_known(w, vocab) for w in words - NEGATIONS)):  # 'not shipped' is not 'shipped'
        return False
    content = [w for w in _tokens(q) if w not in GRAMMAR]
    return len(left) * 2 <= len(content)


def suggest(question: str, spec: QuerySpec, a: Assessment, cat: Catalog) -> list[dict]:
    """Up to MAX_SUGGESTIONS rewritten questions, each already known to be answerable."""
    seen, meanings, results = {question.lower().strip()}, [], []
    frontier = [(question, spec, a, [])]
    for _ in range(MAX_DEPTH):
        nxt = []
        for q, sp, asm, why in frontier:
            for q2, w in _candidates(q, sp, asm, cat):
                key = q2.lower().strip()
                if key in seen or not q2:
                    continue
                seen.add(key)
                try:
                    s2, a2 = _check(q2, cat)
                except Exception:  # a rewrite the pipeline cannot read is simply not offered
                    continue
                if a2.answerable:
                    if (r := reading(s2)) in meanings:  # same calculation, different wording
                        continue
                    meanings.append(r)
                    results.append({"question": q2, "why": "; ".join(why + [w])})
                else:
                    nxt.append((q2, s2, a2, why + [w]))
        if len(results) >= MAX_SUGGESTIONS:
            break
        frontier = nxt[:8]
    return results[:MAX_SUGGESTIONS]


def available_hint(spec: QuerySpec, a: Assessment, cat: Catalog) -> str | None:
    """When nothing can be rewritten: what the data can answer, so the next question can be specific."""
    targets = rate_targets(cat)
    if spec.ambiguities and any(w in AMBIGUOUS_CURRENCY_WORDS for w in _tokens(" ".join(spec.ambiguities))):
        return f"The data can convert amounts into {', '.join(targets) or 'no other currency'} only."
    if not spec.unresolved or spec.table:
        return None
    measures = [f"{c.replace('_', ' ')} ({t})" for t, df in cat.tables.items() if not DERIVED_TABLE_RE.search(t)
                for c in df.columns if _is_measure(f"{t}.{c}", cat.profiles) and "rate" not in c.lower()]
    parts = []
    if cat.metrics:
        parts.append("defined metrics: " + ", ".join(cat.metrics))
    if measures:
        parts.append("numeric columns: " + ", ".join(measures[:8]))
    parts.append("counts of: " + ", ".join(t for t in cat.tables if not DERIVED_TABLE_RE.search(t)))
    return "The data can answer questions about " + "; ".join(parts) + "."
