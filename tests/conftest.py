import json
from pathlib import Path

import pytest

from app.catalog import build_catalog
from app.config import Config
from app.ingestion import build_workspace, load_directory
from app.workflow import Analyst

ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC = ROOT / "data" / "synthetic"
CFG = Config(llm_provider="none", timeout_s=20, max_repairs=2)
CASES = json.loads((SYNTHETIC / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def catalog(tmp_path_factory):
    return build_catalog(load_directory(SYNTHETIC, tmp_path_factory.mktemp("ws")))


@pytest.fixture(scope="session")
def analyst(catalog):
    return Analyst(catalog, CFG)


@pytest.fixture
def make_catalog(tmp_path):
    """Build a catalog from {table: csv_text} (plus optional metrics)."""
    def make(tables: dict[str, str], metrics: dict | None = None):
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        files = []
        for name, text in tables.items():
            (src / f"{name}.csv").write_text(text.strip() + "\n", encoding="utf-8")
            files.append(src / f"{name}.csv")
        if metrics:
            (src / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
            files.append(src / "metrics.json")
        return build_catalog(build_workspace(files, tmp_path / "ws"))
    return make
