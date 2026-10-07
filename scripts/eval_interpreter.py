"""Compare question interpreters on held-out questions: the rule-based parser and any Ollama models.

    .venv\\Scripts\\python scripts\\eval_interpreter.py                         # parser only
    .venv\\Scripts\\python scripts\\eval_interpreter.py --models qwen2.5:1.5b pcda-interpreter --limit 200

Scores use the same `reading()` comparison the app uses: exact match of what the spec means for the
calculation, plus per-field accuracy. Unresolved questions count as correct only if marked unresolved.
"""
import argparse
import json
import random
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.catalog import build_catalog  # noqa: E402
from app.ingestion import load_directory  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.local_model import LocalInterpreter, reading  # noqa: E402
from app.profiling import profile_workspace  # noqa: E402
from app.question import blocked, ground, names_measure, parse_question  # noqa: E402

SAMPLE = {"text": ["A1", "B2", "C3"], "integer": ["1", "2", "3"], "decimal": ["1.50", "2.25", "3.75"],
          "date": ["2023-01-05", "2024-06-30", "2025-02-11"]}


def tables_for(summary: dict) -> dict:
    """Small stand-in tables with the schema's columns and types, enough for the parser to profile."""
    out = {}
    for t, info in summary["tables"].items():
        out[t] = pd.DataFrame({c: [f"{c[:3]}{i}" if c.endswith("_id") else v for i, v in enumerate(SAMPLE[d["type"]])]
                               for c, d in info["columns"].items()})
    for r in summary.get("relationships", []):  # child keys must exist in the parent
        child, parent = (x.strip() for x in r.split("->"))
        ct, cc = child.split(".")
        pt, pc = parent.split(".")
        out[ct][cc] = out[pt][pc].values
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "training" / "heldout.jsonl"))
    ap.add_argument("--models", nargs="*", default=[])
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args()
    rows = [json.loads(line) for line in open(args.file, encoding="utf-8")]
    random.Random(0).shuffle(rows)
    rows = rows[:args.limit]
    schemas = json.loads((ROOT / "training" / "schemas.json").read_text(encoding="utf-8"))
    demo = build_catalog(load_directory(ROOT / "data" / "synthetic", Path(tempfile.mkdtemp())))
    contexts = {}
    for sid in {r["schema_id"] for r in rows}:
        if sid == "demo":
            contexts[sid] = (demo.tables, demo.profiles, demo.metrics)
        else:
            tables = tables_for(schemas[sid])
            contexts[sid] = (tables, profile_workspace(tables)[0], schemas[sid].get("metric_definitions", {}))

    def parser(r):
        t, p, m = contexts[r["schema_id"]]
        return parse_question(r["question"], t, p, m)

    systems = {"rule-based parser": parser}
    specs: dict[str, list] = {}
    for name in args.models:
        client = LocalInterpreter(name, timeout=300)
        systems[name] = lambda r, c=client: c.interpret(r["question"], schemas[r["schema_id"]])

    print(f"{len(rows)} held-out questions\n")
    print(f"{'interpreter':28} {'exact':>7} {'fields':>7} {'unresolved ok':>14} {'errors':>7} {'s/question':>11}")
    for name, fn in systems.items():
        exact = fields = field_n = unres_ok = unres_n = errors = 0
        t0 = time.monotonic()
        for r in rows:
            want = reading(r["target"])
            try:
                spec = fn(r)
                got = reading(spec)
            except LLMError:
                errors += 1
                specs.setdefault(name, []).append(None)
                continue
            specs.setdefault(name, []).append(spec)
            exact += got == want
            if want.get("unresolved"):
                unres_n += 1
                unres_ok += bool(got.get("unresolved"))
            for k, v in want.items():
                field_n += 1
                fields += got.get(k) == v
        dt = (time.monotonic() - t0) / len(rows)
        print(f"{name:28} {exact / len(rows):7.1%} {fields / max(field_n, 1):7.1%} "
              f"{unres_ok / max(unres_n, 1):14.1%} {errors:7d} {dt:11.2f}")

    in_app(rows, contexts, specs, args.models)


def refs_exist(spec, tables) -> bool:
    refs = [spec.group_by, spec.date_column] + [f.column for f in spec.filters]
    refs += [spec.ratio_filter.column] if spec.ratio_filter else []
    if spec.table not in tables or (spec.measure and spec.measure not in tables[spec.table].columns):
        return False
    return all(r.partition(".")[0] in tables and r.partition(".")[2] in tables[r.partition(".")[0]].columns
               for r in refs if r)


def in_app(rows, contexts, specs, models):
    """What the app's parser-first policy produces with each model behind the parser (workflow._interpret_model):
    the parser's complete reading wins; the model is used only when the parser could not read a word (never when
    it refused on purpose), and only if its reading passes the every-word guard, names columns that exist and
    measures something the question names."""
    print("\nIn the app (parser first, model only where the parser cannot read the question):")
    print(f"{'model behind the parser':28} {'correct':>8} {'rescued':>8} {'harmful':>8} {'still refused':>14}")
    parsed = []
    for r in rows:
        t, p, m = contexts[r["schema_id"]]
        parsed.append(ground(r["question"], parse_question(r["question"], t, p, m), t, p, m))
    examples = []
    for name in ["(none)"] + models:
        correct = rescued = harmful = refused = 0
        for i, r in enumerate(rows):
            t, p, m = contexts[r["schema_id"]]
            want = reading(r["target"])
            if not blocked(parsed[i]):
                correct += reading(parsed[i]) == want
                continue
            spec = specs[name][i] if name in specs else None
            if spec is not None:
                spec = ground(r["question"], spec, t, p, m)
            if (spec is None or parsed[i].ambiguities or parsed[i].unsupported or blocked(spec)
                    or not refs_exist(spec, t) or not names_measure(r["question"], spec, t, m)):
                refused += 1
                correct += bool(want.get("unresolved"))  # refusing an unanswerable question is right
            elif reading(spec) == want:
                correct += 1
                rescued += 1
            else:
                harmful += 1
                got = reading(spec)
                examples.append(f"  {name}: {r['question']!r}: " + ", ".join(
                    f"{k} {want.get(k)!r} -> {got.get(k)!r}" for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)))
        n = len(rows)
        print(f"{name:28} {correct / n:8.1%} {rescued:8d} {harmful:8d} {refused:14d}")
    if examples:
        print("\nHarmful readings (wrong, yet past every guard):\n" + "\n".join(examples))


if __name__ == "__main__":
    main()
