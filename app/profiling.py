"""Profile all-string tables: inferred types, missingness, keys, ranges, relationships."""
import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

INT_RE = re.compile(r"^-?\d+$")
DEC_RE = re.compile(r"^-?\d+\.\d+$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
CURRENCY_SYMBOL_RE = re.compile(r"^\s*[-]?[$€£¥]")
CATEGORICAL_MAX = 20


@dataclass
class ColumnProfile:
    name: str
    kind: str  # integer | decimal | date | datetime | text | empty
    nulls: int
    null_pct: float
    unique: int
    samples: list[str]
    min: str | None = None
    max: str | None = None
    decimals: int = 0
    date_format: str | None = None  # iso | dmy | mdy | ambiguous | mixed (date-like columns only)
    has_timezone: bool | None = None
    top_values: dict[str, int] = field(default_factory=dict)
    currency_symbols: bool = False


@dataclass
class TableProfile:
    name: str
    rows: int
    columns: dict[str, ColumnProfile]
    exact_duplicate_rows: int
    key: str | None
    duplicate_keys: list[str]  # key values on >1 distinct rows (after removing exact duplicates)


@dataclass
class Relationship:
    child: str
    column: str
    parent: str
    unmatched: int  # child key values with no parent row


def _valid_iso(v: str) -> bool:
    try:
        date.fromisoformat(v[:10])
        return True
    except ValueError:
        return False


def _date_format(values: pd.Series) -> str | None:
    iso = values.str.match(ISO_DATE_RE) | values.str.match(ISO_DT_RE)
    slash = values.str.extract(SLASH_RE).dropna().astype(int)
    if iso.all():
        return "iso" if values.map(_valid_iso).all() else "mixed"
    if len(slash) != len(values):
        return "mixed" if (iso.any() or len(slash)) else None
    first_big, second_big = (slash[0] > 12).any(), (slash[1] > 12).any()
    if first_big and second_big:
        return "mixed"
    return "dmy" if first_big else "mdy" if second_big else "ambiguous"


def profile_column(name: str, s: pd.Series) -> ColumnProfile:
    filled = s[s.str.strip() != ""]
    nulls = len(s) - len(filled)
    p = ColumnProfile(name=name, kind="empty", nulls=nulls, null_pct=round(100 * nulls / max(len(s), 1), 2),
                      unique=int(filled.nunique()), samples=[str(v) for v in filled.drop_duplicates().head(3)])
    if filled.empty:
        return p
    if filled.str.match(INT_RE).all():
        p.kind = "integer"
        nums = pd.to_numeric(filled)
        p.min, p.max = str(nums.min()), str(nums.max())
    elif (filled.str.match(INT_RE) | filled.str.match(DEC_RE)).all():
        p.kind = "decimal"
        nums = pd.to_numeric(filled)
        p.min, p.max = str(nums.min()), str(nums.max())
        p.decimals = int(filled.str.split(".").str[1].fillna("").str.len().max())
    else:
        fmt = _date_format(filled)
        if fmt:
            p.date_format = fmt
            p.kind = "datetime" if filled.str.match(ISO_DT_RE).any() else "date"
            if fmt == "iso":
                p.min, p.max = filled.min(), filled.max()
                if p.kind == "datetime":
                    p.has_timezone = bool(filled.str.contains(r"(Z|[+-]\d{2}:?\d{2})$").all())
        else:
            p.kind = "text"
            p.currency_symbols = bool(filled.str.match(CURRENCY_SYMBOL_RE).mean() > 0.5)
    if p.kind == "text" and p.unique <= CATEGORICAL_MAX:
        p.top_values = {str(k): int(v) for k, v in filled.value_counts().head(CATEGORICAL_MAX).items()}
    return p


def singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def _likely_key(name: str, df: pd.DataFrame) -> str | None:
    for c in (f"{singular(name)}_id", "id"):
        if c in df.columns:
            return c
    first = df.columns[0]
    return first if first.endswith("_id") else None


def profile_table(name: str, df: pd.DataFrame) -> TableProfile:
    deduped = df.drop_duplicates()
    key = _likely_key(name, df)
    dup_keys = []
    if key:
        counts = deduped[key].value_counts()
        dup_keys = sorted(counts[counts > 1].index.astype(str))
    return TableProfile(name=name, rows=len(df), columns={c: profile_column(c, df[c]) for c in df.columns},
                        exact_duplicate_rows=int(len(df) - len(deduped)), key=key, duplicate_keys=dup_keys)


def find_relationships(tables: dict[str, pd.DataFrame], profiles: dict[str, TableProfile]) -> list[Relationship]:
    """Foreign key = a `<x>_id` column whose parent table is the one keyed by that column."""
    rels = []
    for parent, pp in profiles.items():
        if not pp.key or pp.key == "id":
            continue
        keys = set(tables[parent][pp.key])
        for child, df in tables.items():
            if child != parent and pp.key in df.columns:
                vals = set(df[pp.key][df[pp.key] != ""])
                rels.append(Relationship(child, pp.key, parent, len(vals - keys)))
    return rels


def profile_workspace(tables: dict[str, pd.DataFrame]) -> tuple[dict[str, TableProfile], list[Relationship]]:
    profiles = {n: profile_table(n, df) for n, df in tables.items()}
    return profiles, find_relationships(tables, profiles)
