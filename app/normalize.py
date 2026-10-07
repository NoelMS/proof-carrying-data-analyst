"""Clean-ups applied once at ingestion, only where the result is unambiguous. Every change is recorded.

Proof code reads the workspace CSVs, so a value is normalized here or not at all: never differently by the two
implementations. Anything ambiguous is left exactly as uploaded, for profiling to flag and analysis to refuse:
  - "N/A", "null", ... in numeric or date columns -> missing (in text columns only the unmistakable markers)
  - "$1,200.50", "€300", "1 200 EUR" -> 1200.50 plus a `<column>_currency` column; "1.200,50" next to
    "1,200.50" is ambiguous and left alone
  - "europe ", "EUROPE" -> the most common spelling, "Europe" (case and spacing only, never different words)
  - "Jan 7 2024", "7 January 2024", ISO and unambiguous day/month dates in one column -> ISO (YYYY-MM-DD)
"""
import re
from datetime import datetime

import pandas as pd

from .question import CURRENCIES

MISSING_STRICT = {"n/a", "#n/a", "null", "none", "nan", "nil"}  # missing in any column
MISSING_NUMERIC = MISSING_STRICT | {"na", "-", "--", "—", "–", "?", "missing", "unknown"}  # 'NA' may be North America
SYMBOLS = {"US$": "USD", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW", "₱": "PHP"}
_SYM = "|".join(re.escape(s) for s in sorted(SYMBOLS, key=len, reverse=True))
MONEY_RE = re.compile(rf"^(?P<neg>-)?\s*(?P<pre>{_SYM}|[A-Z]{{3}})?\s*(?P<neg2>-)?\s*(?P<num>\d[\d,. ']*\d|\d)"
                      rf"\s*(?P<post>{_SYM}|[A-Z]{{3}})?$")
US_RE = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")       # 1,200.50
EU_RE = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$|^\d+,\d{1,2}$")  # 1.200,50 / 3,5
SPACE_RE = re.compile(r"^\d{1,3}([ '’]\d{3})+([.,]\d+)?$")  # 1 200.50 / 1'200
PLAIN_RE = re.compile(r"^\d+(\.\d+)?$")
DATE_FORMATS = ["%b %d %Y", "%b %d, %Y", "%B %d %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y",
                "%Y/%m/%d", "%Y.%m.%d"]
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")
SLASH_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")


def normalize_table(df: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Returns the cleaned table and (column, what changed) records."""
    df = df.copy()
    notes: list[tuple[str, str]] = []
    for c in list(df.columns):
        s = df[c]
        stripped = s.str.strip()
        if (n := int((stripped != s).sum())):
            s = df[c] = stripped
            notes.append((c, f"{n} value(s) had surrounding spaces removed"))
        for fn in (_missing, _money, _dates, _labels):
            changed = fn(df, c)
            if changed:
                notes.append((c, changed))
    return df, notes


def _missing(df, c) -> str | None:
    s = df[c]
    low = s.str.lower()
    strict = low.isin(MISSING_STRICT)
    loose = low.isin(MISSING_NUMERIC)
    rest = s[~loose & (s != "")]
    numeric_like = len(rest) > 0 and rest.map(lambda v: bool(MONEY_RE.match(v)) or _parse_date(v) is not None).all()
    hit = loose if numeric_like else strict
    if not hit.any():
        return None
    words = sorted(set(s[hit]))
    df.loc[hit, c] = ""
    return f"{int(hit.sum())} placeholder(s) {', '.join(repr(w) for w in words)} treated as missing"


def _parse_number(num: str, style: str) -> str:
    num = re.sub(r"[ '’]", "", num)
    if style == "eu":
        num = num.replace(".", "").replace(",", ".")
    else:
        num = num.replace(",", "")
    return num


def _money(df, c) -> str | None:
    s = df[c]
    filled = s[s != ""]
    if filled.empty or filled.map(lambda v: bool(PLAIN_RE.match(v) or re.match(r"^-\d+(\.\d+)?$", v))).all():
        return None  # already plain numbers
    m = filled.map(MONEY_RE.match)
    if not m.map(bool).all():
        return None
    nums = m.map(lambda x: re.sub(r"[ '’]", " ", x["num"]))
    marks = m.map(lambda x: x["pre"] or x["post"] or "")  # never None: pandas would turn it into a truthy NaN
    codes = marks.map(lambda k: SYMBOLS.get(k, k) if k else "")
    if any(k and k not in SYMBOLS and k not in CURRENCIES for k in marks):
        return None  # 'ABC 12' is not money
    plain_nums = nums.str.replace(" ", "", regex=False)
    us = plain_nums.map(lambda v: bool(US_RE.match(v)))
    eu = plain_nums.map(lambda v: bool(EU_RE.match(v)) and not US_RE.match(v))
    if not (us.any() or eu.any() or (codes != "").any() or nums.str.contains(" ").any()):
        return None
    if us.any() and eu.any():
        return None  # '1,200.50' and '1.200,50' in one column: which separator is which cannot be known
    style = "eu" if eu.any() else "us"
    values = nums.map(lambda v: _parse_number(v, style))
    neg = m.map(lambda x: bool(x["neg"] or x["neg2"]))
    values = values.where(~neg, "-" + values)
    if not values.map(lambda v: bool(re.match(r"^-?\d+(\.\d+)?$", v))).all():
        return None
    df.loc[filled.index, c] = values
    out = f"formatted numbers parsed ({'1.234,56' if style == 'eu' else '1,234.56'} style)"
    if (codes != "").any():
        cur_col = f"{c}_currency"
        if cur_col not in df.columns:
            df[cur_col] = ""
        df.loc[filled.index, cur_col] = codes
        found = sorted(set(codes) - {""})
        out += f"; currency taken from symbols into {cur_col} ({', '.join(found)})"
        if "$" in set(marks):
            out += "; '$' read as USD"
        if (codes == "").any():
            out += f"; {int((codes == '').sum())} value(s) carry no currency"
    return out


def _parse_date(v: str):
    if ISO_RE.match(v):
        return v
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(v, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _dates(df, c) -> str | None:
    s = df[c]
    filled = s[s != ""]
    if filled.empty or filled.map(lambda v: bool(ISO_RE.match(v))).all():
        return None
    slash = filled.str.extract(SLASH_RE)
    is_slash = slash[0].notna()
    named = filled[~is_slash].map(_parse_date)
    if named.isna().any():
        return None  # not all values are dates
    if is_slash.any():
        a, b = slash.loc[is_slash, 0].astype(int), slash.loc[is_slash, 1].astype(int)
        dmy, mdy = (a > 12).any(), (b > 12).any()
        if dmy == mdy:
            return None  # day/month order unknown or contradictory: left for the ambiguous-date check
        if not (~is_slash).any():
            return None  # one consistent d/m/y or m/d/y column is already understood by profiling
        day, month = (a, b) if dmy else (b, a)
        conv = [f"{int(y):04d}-{int(mo):02d}-{int(dd):02d}" for dd, mo, y in zip(day, month, slash.loc[is_slash, 2])]
        try:
            [datetime.strptime(x, "%Y-%m-%d") for x in conv]
        except ValueError:
            return None
        df.loc[slash.index[is_slash], c] = conv
    df.loc[named.index, c] = named
    return f"{len(filled)} dates in several formats written as YYYY-MM-DD"


def _labels(df, c) -> str | None:
    s = df[c]
    filled = s[s != ""]
    if filled.empty or c.lower().endswith("_id") or filled.map(lambda v: bool(MONEY_RE.match(v) or ISO_RE.match(v))).all():
        return None
    key = filled.str.casefold().str.replace(r"\s+", " ", regex=True)
    merged = []
    for _, group in filled.groupby(key):
        spellings = group.value_counts()
        if len(spellings) > 1:
            canon = sorted(spellings.index, key=lambda v: (-spellings[v], v))[0]
            others = [v for v in spellings.index if v != canon]
            df.loc[group.index, c] = canon
            merged.append(f"{', '.join(repr(o) for o in others)} -> {canon!r}")
    if not merged:
        return None
    return "spellings differing only in case or spacing merged: " + "; ".join(merged[:5])
