"""Google Gemini as the question interpreter and proof writer (same role and interface as ClaudeClient).

Uses the Gemini REST API with the standard library, so no extra dependency. For privacy on the free tier,
the model receives only `compact_catalog` text (table and column names and types, no data values).
Output is schema-constrained JSON, validated with pydantic; everything downstream (answerability, sandbox,
independent verification) treats it like any other interpretation or code draft.
"""
import json
import socket
import time
import urllib.error
import urllib.request

from pydantic import ValidationError

from .llm import SYSTEM_CODE, SYSTEM_INTERPRET, CodeDraft, LLMError
from .question import QuerySpec, compact_catalog

API = "https://generativelanguage.googleapis.com/v1beta"


class GeminiClient:
    """`model` may be a comma-separated list: when one model is overloaded (503) or retired (404), the next
    is tried. Capacity on the free tier changes often, so a short list keeps the app responsive."""

    def __init__(self, model: str, api_key: str, timeout: float = 3.0):
        self.models_to_try = [m.strip() for m in model.split(",") if m.strip()]
        self.model, self.api_key, self.timeout = self.models_to_try[0], api_key, timeout

    @property
    def label(self) -> str:
        return f"Gemini ({self.model})"

    def _request(self, method: str, path: str, body: dict | None = None, retry_overload: bool = True,
                 deadline: float | None = None) -> dict:
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout
        req = urllib.request.Request(f"{API}/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError(f"Gemini took longer than {self.timeout:g}s")
            try:
                with urllib.request.urlopen(req, timeout=remaining) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = _error_message(e)
                if e.code == 404 or (e.code == 503 and not retry_overload):
                    raise _Unavailable(f"{e.code} {detail[:80]}") from e
                delay = _retry_delay(detail) or 1.0
                if e.code in (429, 503) and time.monotonic() + delay < deadline:  # brief limit that fits the budget
                    time.sleep(delay)
                    continue
                if e.code == 429:  # quota for this model used up: another model may still have some
                    raise _Unavailable(f"429 {detail[:80]}") from e
                raise LLMError(f"Gemini API error {e.code}: {detail[:160]}") from e
            except (TimeoutError, socket.timeout) as e:
                raise LLMError(f"Gemini took longer than {self.timeout:g}s") from e
            except urllib.error.URLError as e:
                if isinstance(e.reason, (TimeoutError, socket.timeout)):
                    raise LLMError(f"Gemini took longer than {self.timeout:g}s") from e
                raise LLMError(f"Gemini unreachable ({e})") from e
            except ConnectionError as e:
                raise LLMError(f"Gemini unreachable ({e})") from e
            except json.JSONDecodeError as e:
                raise LLMError("Gemini returned a malformed response") from e

    def _generate(self, system: str, user: str, schema: dict) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "responseJsonSchema": schema},
        }
        errors, deadline = [], time.monotonic() + self.timeout
        for model in self.models_to_try:
            try:
                resp = self._request("POST", f"models/{model}:generateContent", body,
                                     retry_overload=model == self.models_to_try[-1], deadline=deadline)
                self.model = model
                break
            except _Unavailable as e:  # overloaded or retired: try the next model
                errors.append(f"{model}: {e}")
        else:
            raise LLMError("no Gemini model available (" + "; ".join(errors)[:200] + ")")
        candidates = resp.get("candidates") or []
        if not candidates:
            reason = (resp.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"Gemini returned no answer ({reason})")
        cand = candidates[0]
        if cand.get("finishReason") not in (None, "STOP"):
            raise LLMError(f"Gemini stopped early ({cand.get('finishReason')})")
        return "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))

    def interpret(self, question: str, catalog: dict) -> QuerySpec:
        user = (f"<dataset_metadata>\n{compact_catalog(catalog)}\n</dataset_metadata>\n\n"
                f"<question>{question}</question>")
        text = self._generate(SYSTEM_INTERPRET, user, QuerySpec.model_json_schema())
        try:
            spec = QuerySpec.model_validate_json(text)
        except ValidationError as e:
            raise LLMError(f"Gemini output failed schema validation ({e.error_count()} error(s))") from e
        spec.notes = []
        return spec

    def write_code(self, plan_text: str, feedback: str | None = None) -> str:
        user = f"<plan>\n{plan_text}\n</plan>"
        if feedback:
            user += f"\n\nThe previous script failed verification:\n<feedback>{feedback}</feedback>\nWrite a corrected script."
        text = self._generate(SYSTEM_CODE, user, CodeDraft.model_json_schema())
        try:
            return CodeDraft.model_validate_json(text).code
        except ValidationError as e:
            raise LLMError("Gemini code draft failed schema validation") from e

    def models(self) -> list[str]:
        """Model names this key can call with generateContent."""
        data = self._request("GET", "models?pageSize=200", deadline=time.monotonic() + 30)
        return [m["name"].removeprefix("models/") for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]


class _Unavailable(Exception):
    """This model cannot answer now (overloaded or retired); another one may."""


def _error_message(e: urllib.error.HTTPError) -> str:
    try:
        return json.loads(e.read()).get("error", {}).get("message", str(e))
    except (json.JSONDecodeError, OSError):
        return str(e)


def _retry_delay(message: str) -> float | None:
    """Gemini rate-limit errors say e.g. 'Please retry in 12.5s'."""
    import re
    m = re.search(r"retry in ([\d.]+)s", message)
    return float(m.group(1)) + 0.5 if m else None
