"""The analysis state machine.

    interpret -> assess -> plan -> generate -> execute -> verify -> answer
                   |                  ^                       |
                   v                  +------ repair ---------+
                 refuse  <------------------ (repairs exhausted)

Each stage reads and writes an explicit AnalysisState; the stage log records
what happened without exposing any model reasoning.
"""
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .answerability import Assessment, assess
from .catalog import Catalog
from .codegen import pandas_proof
from .config import Config
from .llm import ClaudeClient, LLMError
from .local_model import LocalInterpreter, differences
from .planning import Plan, build_plan
from .question import QuerySpec, catalog_summary, parse_question
from .sandbox import ExecutionResult, Sandbox
from .traps import Issue
from .verification import Verification, verify

log = logging.getLogger("pcda")
REFUSAL = "I cannot determine this from the available data."
CodeWriter = Callable[[Plan, int, str | None], tuple[str, str]]  # (plan, attempt, feedback) -> (code, source)


@dataclass
class Attempt:
    number: int
    source: str  # template | model | supplied
    code: str
    execution: ExecutionResult | None = None
    verification: Verification | None = None
    failure_reason: str | None = None


@dataclass
class AnalysisState:
    question: str
    claimed_value: Any = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    spec: QuerySpec | None = None
    interpreter: str = ""
    datasets: list[str] = field(default_factory=list)
    detected_issues: list[Issue] = field(default_factory=list)
    answerability: Assessment | None = None
    plan: Plan | None = None
    attempts: list[Attempt] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)
    final: dict = field(default_factory=dict)

    @property
    def last(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def verified(self) -> bool:
        return self.final.get("status") == "verified"


def setup_logging(path: Path = Path("logs/pcda.jsonl")):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not log.handlers:
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)


def _event(st: AnalysisState, stage: str, on_event=None, **fields):
    rec = {"ts": round(time.time(), 3), "request_id": st.request_id, "stage": stage, **fields}
    st.stages.append(rec)
    log.info(json.dumps(rec, default=str))
    if on_event:
        on_event(st)


def format_value(plan: Plan, v: Any) -> str:
    if plan.spec.kind in ("ratio", "growth"):
        return f"{Decimal(v) * 100:.2f}%"
    if plan.integer_result:
        return f"{v:,}"
    return f"{Decimal(v):,.{plan.precision}f}" + (f" {plan.unit}" if plan.unit else "")


class Analyst:
    def __init__(self, catalog: Catalog, cfg: Config | None = None, code_writer: CodeWriter | None = None):
        self.cat = catalog
        self.cfg = cfg or Config.from_env()
        self.sandbox = Sandbox(self.cfg)
        if self.cfg.llm_provider == "anthropic":
            self.llm = ClaudeClient(self.cfg.llm_model)
        elif self.cfg.llm_provider == "local":
            self.llm = LocalInterpreter(self.cfg.local_model, self.cfg.ollama_url)
        else:
            self.llm = None
        self.code_writer = code_writer or self._default_writer

    # ------------------------------------------------------------------ driver
    def run(self, question: str, claimed_value: Any = None, on_event: Callable[[AnalysisState], None] | None = None,
            request_id: str | None = None) -> AnalysisState:
        """Run the workflow. `on_event` is called with the state after every stage (for live progress)."""
        st = AnalysisState(question=question.strip(), claimed_value=claimed_value)
        st.request_id = request_id or st.request_id
        stage = "interpret"
        while stage != "done":
            t0 = time.monotonic()
            try:
                nxt = getattr(self, f"_{stage}")(st)
            except Exception as e:  # never let an internal error become an answer
                log.exception("stage failure")
                st.final = self._refusal(st, f"Internal error during {stage}: {type(e).__name__}.", [])
                nxt = "done"
            _event(st, stage, on_event, next=nxt, duration_s=round(time.monotonic() - t0, 3),
                   attempt=len(st.attempts) or None)
            stage = nxt
        _event(st, "complete", on_event, status=st.final["status"], repairs=max(len(st.attempts) - 1, 0),
               refusal_reason=st.final.get("reason"), datasets=st.datasets)
        return st

    # ------------------------------------------------------------------ stages
    def _interpret(self, st: AnalysisState) -> str:
        if not st.question:
            st.final = self._refusal(st, "No question was asked.", [])
            return "done"
        if isinstance(self.llm, LocalInterpreter):
            return self._interpret_local(st)
        if self.llm:
            try:
                st.spec = self.llm.interpret(st.question, catalog_summary(
                    self.cat.tables, self.cat.profiles, self.cat.relationships, self.cat.metrics))
                st.interpreter = f"model ({self.cfg.llm_model})"
                return "assess"
            except LLMError as e:
                st.interpreter = f"deterministic parser (model unavailable: {e})"
        st.spec = parse_question(st.question, self.cat.tables, self.cat.profiles, self.cat.metrics)
        st.interpreter = st.interpreter or "deterministic parser"
        return "assess"

    def _interpret_local(self, st: AnalysisState) -> str:
        """Parser first; the local model reads what the parser cannot. A small model can misread a question
        the parser reads correctly, so the parser's complete reading always wins and a model that disagrees
        is only reported. Whatever is chosen still goes through every answerability check."""
        parsed = parse_question(st.question, self.cat.tables, self.cat.profiles, self.cat.metrics)
        name = f"local model ({self.cfg.local_model})"
        try:
            model_spec = self.llm.interpret(st.question, catalog_summary(
                self.cat.tables, self.cat.profiles, self.cat.relationships, self.cat.metrics))
        except LLMError as e:
            st.spec, st.interpreter = parsed, f"deterministic parser ({name} unavailable: {e})"
            return "assess"
        if not parsed.unresolved:
            st.spec, st.interpreter = parsed, "deterministic parser"
            diff = differences(parsed, model_spec)
            parsed.notes.append(f"{name} agrees with this reading" if not diff
                                else f"{name} read it differently ({', '.join(diff)}); the parser's reading is used")
        elif not model_spec.unresolved and self._refs_exist(model_spec):
            model_spec.notes = [f"read by the {name}: the rule-based parser did not recognise "
                                f"{', '.join(repr(u) for u in parsed.unresolved)}"]
            st.spec, st.interpreter = model_spec, name
        else:
            st.spec, st.interpreter = parsed, "deterministic parser"
            if model_spec.unresolved:
                parsed.notes.append(f"{name} could not read it either")
            else:
                parsed.notes.append(f"{name} referred to columns that do not exist; its reading was discarded")
        return "assess"

    def _refs_exist(self, spec: QuerySpec) -> bool:
        tables = self.cat.tables
        refs = [spec.group_by, spec.date_column] + [f.column for f in spec.filters]
        refs += [spec.ratio_filter.column] if spec.ratio_filter else []
        if spec.table not in tables or (spec.measure and spec.measure not in tables[spec.table].columns):
            return False
        return all(r.partition(".")[0] in tables and r.partition(".")[2] in tables[r.partition(".")[0]].columns
                   for r in refs if r)

    def _assess(self, st: AnalysisState) -> str:
        a = st.answerability = assess(st.spec, self.cat)
        st.datasets = a.tables or ([st.spec.table] if st.spec.table in self.cat.tables else [])
        st.detected_issues = self.cat.issues_for(st.datasets)
        if not a.answerable:
            st.final = self._refusal(st, a.reasons[0], a.reasons[1:])
            return "done"
        return "plan"

    def _plan(self, st: AnalysisState) -> str:
        st.plan = build_plan(st.question, st.spec, st.answerability, self.cat)
        return "generate"

    def _generate(self, st: AnalysisState) -> str:
        feedback = "; ".join(st.last.verification.failures()) if st.last and st.last.verification else \
            (st.last.failure_reason if st.last else None)
        code, source = self.code_writer(st.plan, len(st.attempts) + 1, feedback)
        st.attempts.append(Attempt(len(st.attempts) + 1, source, code))
        return "execute"

    def _execute(self, st: AnalysisState) -> str:
        st.last.execution = self.sandbox.run(st.last.code, self.cat.workspace.data_dir, st.plan.tables)
        return "verify"

    def _verify(self, st: AnalysisState) -> str:
        at = st.last
        v = at.verification = verify(st.plan, at.code, at.execution, self.sandbox,
                                     self.cat.workspace.data_dir, st.claimed_value)
        if v.passed:
            return "answer"
        at.failure_reason = "; ".join(v.failures())
        only_claim = [c.name for c in v.checks if not c.passed] == ["claim_matches_execution"]
        if only_claim or len(st.attempts) > self.cfg.max_repairs:
            reason = (f"The claimed value {st.claimed_value} is contradicted by the executed proof, which gives "
                      f"{v.executed_value}." if only_claim else
                      f"Verification failed after {len(st.attempts)} attempt(s): {at.failure_reason}")
            st.final = self._refusal(st, reason, [])
            return "done"
        return "generate"

    def _answer(self, st: AnalysisState) -> str:
        p, at = st.plan, st.last
        r = at.execution.result
        if p.output == "scalar":
            answer = format_value(p, r)
        elif p.output == "mapping":
            answer = f"{len(r)} groups: " + ", ".join(f"{k}: {format_value(p, v)}" for k, v in r.items())
        else:
            answer = "; ".join(f"{i}. {k}: {format_value(p, v)}" for i, (k, v) in enumerate(r, 1))
        st.final = {
            "status": "verified", "answer": answer, "numeric_value": r, "unit": p.unit,
            "method": " ".join(p.steps), "proof_code": at.code, "execution_output": at.execution.stdout.strip(),
            "verification": at.verification.record(), "diagnostics": st.answerability.diagnostics,
            "attempts": len(st.attempts),
        }
        return "done"

    # ------------------------------------------------------------------ helpers
    def _refusal(self, st: AnalysisState, reason: str, more: list[str]) -> dict:
        a = st.answerability
        return {"status": "refused", "answer": REFUSAL, "reason": reason, "also": more,
                "answerability": a.status if a and not a.answerable else "VERIFICATION_FAILED" if st.attempts else "REFUSE",
                "diagnostics": a.diagnostics if a else [], "proof_code": None,
                "attempts": len(st.attempts)}

    def _default_writer(self, plan: Plan, attempt: int, feedback: str | None) -> tuple[str, str]:
        if self.llm and hasattr(self.llm, "write_code"):  # the local model only reads questions
            try:
                return self.llm.write_code(plan.text(), feedback), "model"
            except LLMError:
                pass  # the template below implements the same plan; the attempt is labelled accordingly
        return pandas_proof(plan), "template"
