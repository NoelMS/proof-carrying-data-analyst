"""Independent verification of a proof run.

A result is accepted only if every critical check passes:
execution, output contract, precision, static policy, data access, no exception
swallowing, no hard-coded answer, reproduction in a fresh sandbox, agreement with
an independent DuckDB re-computation, and (when given) agreement with the claimed value.
"""
import ast
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .codegen import duckdb_check
from .planning import Plan
from .sandbox import ExecutionResult, Sandbox
from .security import check_code


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Verification:
    checks: list[Check] = field(default_factory=list)
    claimed_value: Any = None
    executed_value: Any = None
    independent_value: Any = None
    reproduced: bool = False

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> bool:
        self.checks.append(Check(name, passed, detail))
        return passed

    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]

    def record(self) -> dict:
        return {"status": "verified" if self.passed else "failed", "claimed_value": self.claimed_value,
                "executed_value": self.executed_value, "independent_value": self.independent_value,
                "match": self.passed, "checks": [c.__dict__ for c in self.checks]}


def normalize(x: Any) -> Any:
    """Canonical form for exact comparison: numbers -> Decimal, containers recursively."""
    if isinstance(x, dict):
        return {str(k): normalize(v) for k, v in x.items()}
    if isinstance(x, list):
        return [normalize(v) for v in x]
    if isinstance(x, bool) or x is None:
        return x
    try:
        return Decimal(str(x).replace(",", ""))
    except InvalidOperation:
        return x


def _values(result: Any) -> list:
    if isinstance(result, dict):
        return list(result.values())
    if isinstance(result, list):
        return [pair[1] for pair in result if isinstance(pair, list) and len(pair) == 2]
    return [result]


def _shape_ok(plan: Plan, result: Any) -> str | None:
    if plan.output == "mapping" and not isinstance(result, dict):
        return "expected a JSON object of group -> value"
    if plan.output == "ranking":
        if not (isinstance(result, list) and all(isinstance(p, list) and len(p) == 2 for p in result)):
            return "expected a list of [key, value] pairs"
        if len(result) > plan.spec.top_n:
            return f"ranking has {len(result)} entries, more than top {plan.spec.top_n}"
    if plan.output == "scalar" and isinstance(result, (dict, list)):
        return "expected a single value"
    return None


def _precision_ok(plan: Plan, result: Any) -> str | None:
    for v in _values(result):
        if plan.integer_result:
            if not isinstance(v, int) or isinstance(v, bool):
                return f"count {v!r} is not an integer"
        elif not isinstance(v, str) or normalize(v) is v or (
                len(v.split(".")[1]) if "." in v else 0) != plan.precision:
            return f"value {v!r} does not have exactly {plan.precision} decimal places"
    return None


def _hardcoded(code: str, result: Any) -> str | None:
    consts = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)) and not isinstance(node.value, bool):
            n = normalize(node.value)
            if isinstance(n, Decimal):
                consts.add(n)
    for v in _values(result):
        n = normalize(v)
        if isinstance(n, Decimal) and (abs(n) >= 10 or n != n.to_integral_value()) and n in consts:
            return f"result value {v} appears as a literal in the code"
    return None


def verify(plan: Plan, code: str, execution: ExecutionResult, sandbox: Sandbox, data_dir: Path,
           claimed: Any = None) -> Verification:
    v = Verification(claimed_value=claimed, executed_value=execution.result)
    if not v.add("execution", execution.ok, f"{execution.status}" + (f": {execution.error}" if execution.error else "")):
        return v
    result = execution.result
    if not v.add("output_contract", (err := _shape_ok(plan, result)) is None, err or plan.output):
        return v
    v.add("precision", (err := _precision_ok(plan, result)) is None,
          err or ("integer counts" if plan.integer_result else f"{plan.precision} decimal places, as justified by the source data"))
    problems = check_code(code)
    v.add("static_policy", not problems, "; ".join(problems) or "allowed imports and operations only")
    missing = [t for t in plan.tables if f"data/{t}.csv" not in code]
    v.add("data_access", not missing, f"does not read {', '.join(missing)}" if missing else
          "reads " + ", ".join(f"data/{t}.csv" for t in plan.tables))
    swallow = any(isinstance(n, ast.Try) for n in ast.walk(ast.parse(code)))
    v.add("no_exception_suppression", not swallow, "try/except found: errors could be hidden" if swallow else "none")
    v.add("no_hardcoded_result", (err := _hardcoded(code, result)) is None, err or "result derived from data")

    rerun = sandbox.run(code, data_dir, plan.tables)
    v.reproduced = rerun.ok and normalize(rerun.result) == normalize(result)
    v.add("reproduction", v.reproduced, "identical result in a fresh sandbox" if v.reproduced
          else f"re-execution gave {rerun.status}: {rerun.result if rerun.ok else rerun.error}")

    check = sandbox.run(duckdb_check(plan), data_dir, plan.tables, trusted=True)
    v.independent_value = check.result
    agree = check.ok and normalize(check.result) == normalize(result)
    v.add("independent_recomputation", agree, "DuckDB SQL re-computation agrees exactly" if agree else
          ("independent computation disagrees with the proof result" if check.ok
           else f"independent computation failed: {check.error}"))
    if claimed is not None:
        same = normalize(claimed) == normalize(result)
        v.add("claim_matches_execution", same, f"claimed {claimed}, executed {result}")
    return v
