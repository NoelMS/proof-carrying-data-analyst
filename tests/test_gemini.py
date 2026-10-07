"""Gemini client: request shape, structured output, errors and rate-limit retry, without network access."""
import io
import json
import urllib.error

import pytest

from app import gemini
from app.gemini import GeminiClient
from app.llm import LLMError
from app.question import QuerySpec


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply(obj):
    return FakeResponse(json.dumps({"candidates": [{"finishReason": "STOP",
                                                    "content": {"parts": [{"text": json.dumps(obj)}]}}]}).encode())


def http_error(code, message):
    return urllib.error.HTTPError("u", code, "e", {}, io.BytesIO(json.dumps({"error": {"message": message}}).encode()))


def test_interpret_sends_names_only_and_validates(monkeypatch, catalog):
    from app.question import catalog_summary
    sent = {}

    def fake_urlopen(req, timeout):
        sent["url"], sent["headers"], sent["body"] = req.full_url, dict(req.header_items()), json.loads(req.data)
        return reply({"metric_term": "revenue", "table": "orders", "measure": "amount", "currency": "USD"})
    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    summary = catalog_summary(catalog.tables, catalog.profiles, catalog.relationships, catalog.metrics)
    spec = GeminiClient("gemini-test", "KEY").interpret("total revenue in usd", summary)
    assert isinstance(spec, QuerySpec) and spec.currency == "USD" and spec.measure == "amount"
    assert sent["url"].endswith("/models/gemini-test:generateContent")
    assert sent["headers"]["X-goog-api-key"] == "KEY" and "KEY" not in sent["url"]  # key in header, not URL
    prompt = sent["body"]["contents"][0]["parts"][0]["text"]
    assert "c001@example.com" not in prompt and "orders" in prompt  # no data values, only names
    assert sent["body"]["generationConfig"]["responseMimeType"] == "application/json"


def test_invalid_output_and_api_errors_raise(monkeypatch):
    monkeypatch.setattr(gemini.urllib.request, "urlopen", lambda req, timeout: reply({"aggregation": "nonsense"}))
    with pytest.raises(LLMError, match="schema validation"):
        GeminiClient("m", "k").interpret("q", {"tables": {}})

    def denied(req, timeout):
        raise http_error(400, "API key not valid")
    monkeypatch.setattr(gemini.urllib.request, "urlopen", denied)
    with pytest.raises(LLMError, match="400: API key not valid"):
        GeminiClient("m", "k").write_code("plan")


def test_rate_limit_is_retried(monkeypatch):
    calls = []

    def flaky(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(429, "Quota exceeded. Please retry in 0.1s.")
        return reply({"code": "print('RESULT: 1')"})
    monkeypatch.setattr(gemini.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    assert GeminiClient("m", "k").write_code("plan") == "print('RESULT: 1')" and len(calls) == 2


def test_workflow_falls_back_when_gemini_fails(monkeypatch, catalog):
    from app.workflow import Analyst
    from tests.conftest import CFG

    def down(req, timeout):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(gemini.urllib.request, "urlopen", down)
    a = Analyst(catalog, CFG)
    a.llm = GeminiClient("m", "k")
    st = a.run("What is the total revenue in USD?")
    assert st.verified and "Gemini unreachable" in st.interpreter and st.last.source == "template"


def test_overloaded_model_falls_through_to_the_next(monkeypatch):
    tried = []

    def busy_first(req, timeout):
        tried.append(req.full_url.split("/models/")[1].split(":")[0])
        if len(tried) == 1:
            raise http_error(503, "This model is currently experiencing high demand.")
        return reply({"code": "print('RESULT: 1')"})
    monkeypatch.setattr(gemini.urllib.request, "urlopen", busy_first)
    c = GeminiClient("busy-model, spare-model", "k")
    assert c.write_code("plan") == "print('RESULT: 1')"
    assert tried == ["busy-model", "spare-model"] and c.label == "Gemini (spare-model)"


def test_model_reference_habits_are_normalized(catalog):
    from app.question import normalize_refs
    s = normalize_refs(QuerySpec(table="orders", measure="orders.amount", group_by="channel"), catalog.tables, catalog.profiles)
    assert s.measure == "amount" and s.group_by == "orders.channel"
    s = normalize_refs(QuerySpec(table="shipments", aggregation="count_distinct"), catalog.tables, catalog.profiles)
    assert s.measure == "shipment_id"


def test_slow_answer_times_out_and_the_parser_answers(monkeypatch, catalog):
    import socket
    from app.workflow import Analyst
    from tests.conftest import CFG
    budgets = []

    def slow(req, timeout):
        budgets.append(timeout)
        raise urllib.error.URLError(socket.timeout("timed out"))
    monkeypatch.setattr(gemini.urllib.request, "urlopen", slow)
    a = Analyst(catalog, CFG)
    a.llm = GeminiClient("m", "k", timeout=3)
    st = a.run("What is the total revenue in USD?")
    assert st.verified and "took longer than 3s" in st.interpreter and st.last.source == "template"
    assert budgets and max(budgets) <= 3  # never waits beyond the limit
