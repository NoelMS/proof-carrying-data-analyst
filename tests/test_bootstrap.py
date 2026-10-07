"""Launcher setup: requirement parsing and the installed-version check."""
from importlib import metadata

from app import bootstrap


def test_parses_pins_comments_and_blank_lines(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("pandas==3.0.6\n\n# comment\nduckdb  # unpinned\n", encoding="utf-8")
    assert bootstrap.requirements(req) == [("pandas", "3.0.6"), ("duckdb", None)]


def test_reports_missing_and_wrong_versions(tmp_path):
    have = metadata.version("pydantic")
    req = tmp_path / "requirements.txt"
    req.write_text(f"pydantic=={have}\nnumpy==0.0.1\nsurely-not-installed-pkg==1.0\n", encoding="utf-8")
    missing = bootstrap.missing_requirements(req)
    assert missing[0].startswith("numpy (have ") and missing[0].endswith("need 0.0.1)")
    assert missing[1] == "surely-not-installed-pkg"
    assert len(missing) == 2  # pydantic matches


def test_project_requirements_are_satisfied_in_the_test_environment():
    assert bootstrap.missing_requirements() == []


def test_launcher_files_exist_with_correct_line_endings():
    cmd = (bootstrap.ROOT / "Proof-Carrying Data Analyst.cmd").read_bytes()
    assert b"\r\n" in cmd and b"launch.pyw" in cmd
    assert b"\r\n" not in (bootstrap.ROOT / "start.sh").read_bytes()
