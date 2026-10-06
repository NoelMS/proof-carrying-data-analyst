"""User-confirmed data corrections with preview, change log and rollback.

Nothing here runs on its own. A fix is (1) proposed for a detected issue, (2) previewed as a
diff of removed rows and changed cells, (3) applied only when the user confirms, with the
previous table saved first so the change can be rolled back. Applied tables are stored as
overrides on top of the source files, which are never modified.
"""
import hashlib
import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from .catalog import Catalog
from .ingestion import Workspace
from .profiling import SLASH_RE
from .security import is_instruction_like

PREVIEW_ROWS = 50
UNIT_FACTORS = {  # to a base unit per dimension; exact definitions
    "mass": {"kg": Decimal(1), "g": Decimal("0.001"), "lb": Decimal("0.45359237"), "oz": Decimal("0.028349523125")},
    "length": {"m": Decimal(1), "cm": Decimal("0.01"), "mm": Decimal("0.001"), "km": Decimal(1000),
               "ft": Decimal("0.3048"), "in": Decimal("0.0254"), "mi": Decimal("1609.344")},
    "volume": {"l": Decimal(1), "ml": Decimal("0.001"), "gal": Decimal("3.785411784")},
}


class FixError(Exception):
    pass


@dataclass
class FixOption:
    id: str
    kind: str
    table: str
    column: str | None
    title: str
    description: str
    choices: list[dict] = field(default_factory=list)  # [{"value", "label"}]; the user must pick one
    input: dict | None = None  # free value the user supplies, e.g. a fill value


# ------------------------------------------------------------------ proposals

def _unit_dimension(units: set[str]) -> str | None:
    for dim, factors in UNIT_FACTORS.items():
        if units <= set(factors):
            return dim
    return None


def _measure_for_unit(df: pd.DataFrame, unit_col: str) -> str | None:
    stem = unit_col.lower().replace("_unit", "").replace("unit", "").strip("_")
    return next((c for c in df.columns if c.lower() == stem), None)


def propose(cat: Catalog) -> list[FixOption]:
    out = []
    for i in cat.issues:
        t, c = i.table, i.column
        df = cat.tables.get(t)
        if df is None:
            continue
        fid = f"{i.kind}:{t}:{c or ''}"
        if i.kind == "duplicate_rows":
            out.append(FixOption(fid, i.kind, t, None, "Remove exact duplicate rows",
                                 f"Keep one copy of each fully identical row in {t}. {i.detail}"))
        elif i.kind == "conflicting_records":
            out.append(FixOption(fid, i.kind, t, c, f"Keep one record per {c}",
                                 f"{i.detail} You decide which version is kept; the others are removed.",
                                 [{"value": "first", "label": "Keep the first occurrence"},
                                  {"value": "last", "label": "Keep the last occurrence"}]))
        elif i.kind in ("ambiguous_date", "inconsistent_date_format"):
            out.append(FixOption(fid, i.kind, t, c, f"Convert {c} to ISO dates",
                                 "Rewrite d/m/yyyy-style values as yyyy-mm-dd using the reading you choose. "
                                 "Values that are already ISO are left as they are.",
                                 [{"value": "dmy", "label": "Day first (31/12/2024)"},
                                  {"value": "mdy", "label": "Month first (12/31/2024)"}]))
        elif i.kind == "missing_values":
            out.append(FixOption(fid, i.kind, t, c, f"Handle missing {c}",
                                 f"{i.detail} Remove the incomplete rows, or fill them with a value you provide.",
                                 [{"value": "drop", "label": "Remove rows where it is missing"},
                                  {"value": "fill", "label": "Fill with a value"}],
                                 {"name": "value", "label": "Fill value (used with 'Fill with a value')"}))
        elif i.kind == "mixed_units":
            units = set(df[c]) - {""}
            measure = _measure_for_unit(df, c)
            dim = _unit_dimension(units)
            if measure and dim:
                out.append(FixOption(fid, i.kind, t, c, f"Convert {measure} to one unit",
                                     f"Convert {measure} using exact {dim} factors and set {c} to the chosen unit.",
                                     [{"value": u, "label": f"Convert everything to {u}"} for u in sorted(units)]))
        elif i.kind == "orphan_keys":
            out.append(FixOption(fid, i.kind, t, c, f"Remove rows with unmatched {c}",
                                 f"{i.detail} These rows reference records that do not exist."))
        elif i.kind == "prompt_injection":
            out.append(FixOption(fid, i.kind, t, c, f"Remove instruction-like text in {c}",
                                 "Replace values that read like instructions with '[removed]'. "
                                 "They are already ignored during analysis; this cleans the data itself."))
        elif i.kind == "negative_values":
            out.append(FixOption(fid, i.kind, t, c, f"Remove rows with negative {c}",
                                 f"{i.detail} Only do this if negative values are errors, not returns or refunds."))
    return out


# ------------------------------------------------------------------ transformations

def _iso(value: str, order: str) -> str:
    m = SLASH_RE.match(value)
    if not m:
        return value
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    day, month = (a, b) if order == "dmy" else (b, a)
    try:
        return datetime(y, month, day).date().isoformat()
    except ValueError:
        return value  # impossible under this reading: left unchanged and reported


def _parent_of(cat: Catalog, table: str, column: str) -> str:
    return next(r.parent for r in cat.relationships if r.child == table and r.column == column)


def transform(cat: Catalog, opt: FixOption, choice: str | None, value: str | None) -> pd.DataFrame:
    df = cat.tables[opt.table].copy()
    c = opt.column
    if opt.choices and choice not in {x["value"] for x in opt.choices}:
        raise FixError("Choose one of the options before previewing this change.")
    k = opt.kind
    if k == "duplicate_rows":
        return df.drop_duplicates()
    if k == "conflicting_records":
        return df.drop_duplicates().drop_duplicates(subset=[c], keep=choice)
    if k in ("ambiguous_date", "inconsistent_date_format"):
        df[c] = df[c].map(lambda v: _iso(v, choice))
        return df
    if k == "missing_values":
        if choice == "drop":
            return df[df[c].str.strip() != ""]
        if value is None or not str(value).strip():
            raise FixError("Enter the value to fill missing cells with.")
        df.loc[df[c].str.strip() == "", c] = str(value).strip()
        return df
    if k == "mixed_units":
        measure = _measure_for_unit(df, c)
        factors = UNIT_FACTORS[_unit_dimension(set(df[c]) - {""})]

        def conv(row):
            try:
                v = Decimal(row[measure]) * factors[row[c]] / factors[choice]
            except (InvalidOperation, KeyError):
                return row[measure]
            return format(v.quantize(Decimal("0.000001")).normalize(), "f")
        df[measure] = df.apply(conv, axis=1)
        df.loc[df[c] != "", c] = choice
        return df
    if k == "orphan_keys":
        parent = cat.tables[_parent_of(cat, opt.table, c)]
        return df[(df[c] == "") | df[c].isin(set(parent[c]))]
    if k == "prompt_injection":
        df[c] = df[c].map(lambda v: "[removed]" if is_instruction_like(v) else v)
        return df
    if k == "negative_values":
        return df[~df[c].str.strip().str.startswith("-")]
    raise FixError(f"No fix is available for {k}.")


def diff(cat: Catalog, table: str, before: pd.DataFrame, after: pd.DataFrame) -> dict:
    """Removed rows and changed cells, identified by the table key when it has one, else row number."""
    key = cat.profiles[table].key
    label = (lambda i: before.at[i, key]) if key else (lambda i: f"row {i + 1}")
    removed_idx = [i for i in before.index if i not in after.index]
    common = [i for i in after.index if i in before.index]
    changed = []
    for col in before.columns:
        b, a = before.loc[common, col], after.loc[common, col]
        for i in b.index[b != a]:
            changed.append({"row": str(label(i)), "column": col, "before": b[i], "after": a[i]})
    return {
        "table": table, "rows_before": len(before), "rows_after": len(after),
        "removed": {"count": len(removed_idx), "columns": list(before.columns),
                    "rows": before.loc[removed_idx[:PREVIEW_ROWS]].values.tolist(),
                    "labels": [str(label(i)) for i in removed_idx[:PREVIEW_ROWS]]},
        "changed": {"count": len(changed), "cells": changed[:PREVIEW_ROWS * 2]},
    }


def fingerprint(df: pd.DataFrame) -> str:
    return hashlib.sha1(df.to_csv(index=False).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ persistent overrides, log, rollback

class FixStore:
    """Per-workspace overrides in <root>/tables, previous states in <root>/snapshots, log in <root>/log.json."""

    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "tables").mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir(parents=True, exist_ok=True)

    @property
    def _log_path(self) -> Path:
        return self.root / "log.json"

    def log(self) -> list[dict]:
        try:
            return json.loads(self._log_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_log(self, entries: list[dict]):
        self._log_path.write_text(json.dumps(entries, indent=1), encoding="utf-8")

    def apply_overrides(self, ws: Workspace):
        for path in (self.root / "tables").glob("*.csv"):
            t = path.stem
            if t in ws.tables:
                ws.tables[t] = pd.read_csv(path, dtype=str, keep_default_na=False)
                shutil.copyfile(path, ws.data_dir / f"{t}.csv")

    def commit(self, table: str, before: pd.DataFrame, after: pd.DataFrame, opt: FixOption, choice, value, d: dict) -> dict:
        entry_id = uuid.uuid4().hex[:10]
        override = self.root / "tables" / f"{table}.csv"
        was_original = not override.exists()
        before.to_csv(self.root / "snapshots" / f"{entry_id}.csv", index=False)
        after.to_csv(override, index=False)
        entry = {"id": entry_id, "ts": round(time.time()), "table": table, "fix": opt.id, "title": opt.title,
                 "choice": choice, "value": value, "was_original": was_original,
                 "summary": f"{d['removed']['count']} row(s) removed, {d['changed']['count']} cell(s) changed",
                 "rows_before": d["rows_before"], "rows_after": d["rows_after"]}
        self._save_log(self.log() + [entry])
        return entry

    def rollback(self, entry_id: str) -> dict:
        entries = self.log()
        entry = next((e for e in entries if e["id"] == entry_id), None)
        if not entry:
            raise FixError("That change is not in the log.")
        later = [e for e in entries[entries.index(entry) + 1:] if e["table"] == entry["table"]]
        if later:
            raise FixError(f"Roll back the later change to {entry['table']} first ({later[-1]['title']}).")
        override = self.root / "tables" / f"{entry['table']}.csv"
        snap = self.root / "snapshots" / f"{entry_id}.csv"
        if entry["was_original"]:
            override.unlink(missing_ok=True)
        else:
            shutil.copyfile(snap, override)
        snap.unlink(missing_ok=True)
        self._save_log([e for e in entries if e["id"] != entry_id])
        return entry

    def restore_original(self, table: str) -> int:
        entries = self.log()
        mine = [e for e in entries if e["table"] == table]
        (self.root / "tables" / f"{table}.csv").unlink(missing_ok=True)
        for e in mine:
            (self.root / "snapshots" / f"{e['id']}.csv").unlink(missing_ok=True)
        self._save_log([e for e in entries if e["table"] != table])
        return len(mine)


def option_by_id(cat: Catalog, fix_id: str) -> FixOption:
    opt = next((o for o in propose(cat) if o.id == fix_id), None)
    if not opt:
        raise FixError("That issue is no longer present; refresh the page.")
    return opt


def as_dict(opt: FixOption) -> dict:
    return asdict(opt)
