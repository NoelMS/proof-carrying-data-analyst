"""Headless smoke test of the Streamlit interface."""
from streamlit.testing.v1 import AppTest

from tests.conftest import ROOT


def ask(at, question):
    at.text_input[0].set_value(question)
    at.button[0].click().run()
    assert not at.exception, at.exception
    return " ".join(m.value for m in at.markdown)


def test_verified_and_refused_paths_render(monkeypatch):
    monkeypatch.setenv("PCDA_LLM_PROVIDER", "none")
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=120).run()
    assert not at.exception, at.exception
    page = ask(at, "What is the total revenue in USD?")
    assert ">Verified<" in page and "815,497.70 USD" in page
    assert any("drop_duplicates" in c.value for c in at.code)  # proof is shown
    page = ask(at, "What is the total revenue?")
    assert ">Not verified<" in page and "Ambiguous" in page and "I cannot determine this" in page
    assert ">Verified<" not in page
