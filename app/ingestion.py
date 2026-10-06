"""Load CSV / Excel files into a workspace of normalized, all-string tables.

Every table is stored as UTF-8 CSV under ``<workspace>/data/<table>.csv`` so that
generated proof code always reads data the same way, whatever the upload format.
Values are kept as strings; typing is the profiler's job, never a silent guess here.
"""
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

READERS = {".csv": "csv", ".xlsx": "excel", ".xlsm": "excel"}  # extend here for new formats
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class IngestionError(Exception):
    pass


@dataclass
class Workspace:
    root: Path
    tables: dict[str, pd.DataFrame]
    metrics: dict = field(default_factory=dict)  # business definitions supplied with the data
    notes: list[str] = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


def table_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_") or "table"
    return f"t_{name}" if name[0].isdigit() else name


def _clean_columns(cols) -> list[str]:
    out = []
    for i, c in enumerate(cols):
        c = _CONTROL.sub(" ", str(c)).strip()
        if not c or re.fullmatch(r"Unnamed: \d+", c):  # pandas placeholder for a blank header
            c = f"column_{i + 1}"
        base, n = c, 2
        while c in out:
            c, n = f"{base}_{n}", n + 1
        out.append(c)
    return out


def _read_csv(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return pd.read_csv(path, dtype=str, keep_default_na=False, encoding=enc)
        except UnicodeDecodeError:
            continue
        except pd.errors.EmptyDataError as e:
            raise IngestionError(f"{path.name}: file is empty") from e
        except pd.errors.ParserError as e:
            raise IngestionError(f"{path.name}: malformed CSV ({e})") from e
    raise IngestionError(f"{path.name}: unsupported text encoding")


def _read_excel(path: Path) -> dict[str, pd.DataFrame]:
    try:
        return pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False)
    except Exception as e:  # openpyxl raises many types for corrupt workbooks
        raise IngestionError(f"{path.name}: unreadable or corrupted workbook ({type(e).__name__})") from e


def read_file(path: Path) -> dict[str, pd.DataFrame]:
    kind = READERS.get(path.suffix.lower())
    if kind is None:
        raise IngestionError(f"{path.name}: unsupported file type (supported: {', '.join(READERS)})")
    if kind == "csv":
        return {table_name(path.stem): _read_csv(path)}
    sheets = _read_excel(path)
    if not sheets:
        raise IngestionError(f"{path.name}: workbook has no sheets")
    single = len(sheets) == 1
    return {table_name(path.stem if single else f"{path.stem}_{s}"): df for s, df in sheets.items()}


def build_workspace(files: list[Path], dest: Path | None = None) -> Workspace:
    """Ingest files into a fresh workspace directory. ``metrics.json`` is read as business definitions."""
    root = Path(dest or tempfile.mkdtemp(prefix="pcda_ws_"))
    if (root / "data").exists():
        shutil.rmtree(root / "data")
    (root / "data").mkdir(parents=True)
    ws = Workspace(root=root, tables={})
    for path in map(Path, files):
        if path.name == "metrics.json":
            try:
                ws.metrics = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise IngestionError(f"metrics.json: invalid JSON ({e})") from e
            continue
        if path.suffix.lower() not in READERS:
            ws.notes.append(f"Skipped {path.name}: unsupported file type.")
            continue
        for name, df in read_file(path).items():
            df.columns = _clean_columns(df.columns)
            if df.empty or not len(df.columns):
                ws.notes.append(f"Skipped table '{name}': no rows.")
                continue
            if name in ws.tables:
                raise IngestionError(f"Two inputs map to the same table name '{name}'.")
            df = df.fillna("").astype(str)
            df.to_csv(ws.data_dir / f"{name}.csv", index=False)
            ws.tables[name] = df
    if not ws.tables:
        raise IngestionError("No usable tables were found in the provided files.")
    return ws


def load_directory(src: Path, dest: Path | None = None) -> Workspace:
    src = Path(src)
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in READERS or p.name == "metrics.json")
    return build_workspace(files, dest)
