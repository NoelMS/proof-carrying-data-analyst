"""Documents (.txt / .md / .pdf) as rules: deterministic sentence patterns, never a model.

A rule that maps onto the data is applied to the reading, so it becomes part of the proof and of the independent
check ("Revenue excludes refunds." -> orders.status != 'refunded'). A sentence about the asked metric that cannot be
applied makes the question refused, quoting the sentence: a document is never silently ignored. Instruction-like
sentences are data, never followed.
"""
import calendar
import re
from dataclasses import dataclass

from .question import (QuerySpec, Filter, _metric_names, _name_words, _tokens, _value_columns, blocked, resolve_column,
                       singular)
from .security import is_instruction_like

MONTH = "|".join(m.lower() for m in calendar.month_name if m)
_W = r"[a-z][a-z \-]*?"
EXCLUDE_RE = re.compile(rf"^(?:the )?(?P<subj>{_W})\s+(?:excludes?|does not include|do not include|should not include|"
                        rf"must not include|never includes?|is net of|are net of|leaves? out)\s+(?:any |all )?(?P<obj>{_W})$")
ONLY_RE = re.compile(rf"^(?:only (?P<obj1>{_W}) (?:are|is) (?:counted|included) (?:in|as|towards?) (?:the )?(?P<subj1>{_W})"
                     rf"|(?:the )?(?P<subj2>{_W}) (?:includes only|counts only|only includes|only counts) (?P<obj2>{_W}))$")
DEFINE_RE = re.compile(rf"^(?:the )?(?P<subj>{_W})\s+(?:is defined as|means|is calculated as)\s+(?:the )?"
                       rf"(?P<agg>sum|total|average|mean|count|number) of (?:the |all )?(?P<obj>[a-z][a-z_ \-]*)$")
FISCAL_RE = re.compile(rf"fiscal year (?:starts|begins|runs from)(?: in| on| from)?(?: the first of)? (?P<month>{MONTH})")
RULE_WORDS = {"exclude", "excludes", "excluding", "include", "includes", "including", "only", "except", "must",
              "should", "net", "gross", "never", "always", "not"}


@dataclass
class Rule:
    doc: str
    sentence: str
    kind: str  # exclude | only | define | fiscal | other | instruction
    subject: str = ""
    obj: str = ""


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def rules(documents: dict[str, str]) -> list[Rule]:
    out = []
    for doc, text in documents.items():
        for raw in sentences(text):
            s = re.sub(r"\s+", " ", raw.lower()).strip(" .!?;:")
            if is_instruction_like(raw):
                out.append(Rule(doc, raw, "instruction"))
            elif m := EXCLUDE_RE.match(s):
                out.append(Rule(doc, raw, "exclude", m["subj"], m["obj"]))
            elif m := ONLY_RE.match(s):
                out.append(Rule(doc, raw, "only", m["subj1"] or m["subj2"], m["obj1"] or m["obj2"]))
            elif m := DEFINE_RE.match(s):
                out.append(Rule(doc, raw, "define", m["subj"], f"{m['agg']} of {m['obj']}"))
            elif m := FISCAL_RE.search(s):
                out.append(Rule(doc, raw, "fiscal", obj=m["month"]))
            elif set(_tokens(s)) & RULE_WORDS:
                out.append(Rule(doc, raw, "other", " ".join(_tokens(s))))
    return out


def _words(text: str) -> set[str]:
    return {singular(w) for w in _tokens(text) if len(w) > 2}


def _about(rule: Rule, spec: QuerySpec, cat) -> bool:
    """Is the rule's subject what the question measures (its metric, a metric alias, its column or table)?"""
    names = {spec.metric_term or "", spec.measure or "", spec.table or ""}
    key = _metric_names(cat.metrics).get((spec.metric_term or "").lower())
    if key:
        names |= {key, *cat.metrics[key].get("aliases", [])}
    mine = set().union(*(_name_words(n) for n in names if n)) - {"id"}
    return bool(_words(rule.subject) & {singular(w) for w in mine})


def _value_matches(obj: str, spec: QuerySpec, cat) -> list[tuple[str, str, str]]:
    """(table, column, value) whose value names the rule's object: 'refunds' -> status 'refunded'."""
    stems = {w[:5] for w in _tokens(obj) if len(w) >= 4}
    hits = []
    for t, c, values, is_id in _value_columns(cat.tables, cat.profiles):
        if is_id or t != spec.table:
            continue
        for v in values:
            if any(tok[:5] in stems for tok in _tokens(v) if len(tok) >= 4):
                hits.append((t, c, v))
    return hits


def apply_documents(spec: QuerySpec, question: str, cat) -> None:
    """Apply the documents' rules to a reading in place: filters for exclusions and restrictions, refusals (quoting
    the sentence) for anything that concerns the question but cannot be applied."""
    docs = getattr(cat.workspace, "documents", None)
    if not docs:
        return
    ql = question.lower()
    for r in rules(docs):  # a fiscal year is explained by the document even where the rules could not read it
        if r.kind == "fiscal" and re.search(r"\bfiscal\b|\bfy\s?\d{2,4}\b", ql):
            spec.unresolved = [u for u in spec.unresolved if not set(_tokens(u)) <= {"fiscal", "year", "fy"}]
            spec.ambiguities.append(f"{r.doc} says '{r.sentence.strip()}', but not whether fiscal year N starts or ends "
                                    "in N; ask with explicit dates (for example 'between 2024-04-01 and 2025-03-31').")
    if blocked(spec) or not spec.table:
        return
    for r in rules(docs):
        src = f"{r.doc} says '{r.sentence.strip()}'"
        if r.kind == "fiscal":
            continue
        if r.kind == "instruction" or not _about(r, spec, cat):
            continue
        if r.kind in ("exclude", "only"):
            hits = _value_matches(r.obj, spec, cat)
            if len(hits) == 1:
                t, c, v = hits[0]
                op = "!=" if r.kind == "exclude" else "=="
                if not any(f.column == f"{t}.{c}" and f.value == v and f.op == op for f in spec.filters):
                    spec.filters.append(Filter(column=f"{t}.{c}", op=op, value=v))
                spec.notes.append(f"applied from {r.doc}: '{r.sentence.strip()}' -> {c} {op} '{v}'")
            else:
                why = "several values could be meant" if hits else f"the data marks no '{r.obj}'"
                spec.unsupported.append(f"{src}, which applies to this question but cannot be applied: {why}.")
        elif r.kind == "define":
            agg, _, obj = r.obj.partition(" of ")
            col = resolve_column(obj, spec.table, cat.tables, prefer_name=False)
            if col and col == f"{spec.table}.{spec.measure}":
                spec.notes.append(f"matches the definition in {r.doc}")
            else:
                spec.unsupported.append(f"{src}, which disagrees with this reading ({spec.table}.{spec.measure}).")
        else:
            spec.unsupported.append(f"{src}, which may change this calculation and cannot be applied automatically.")


def metrics_from_documents(ws) -> dict:
    """'Revenue is defined as the sum of amount.' -> a metric like those in metrics.json (which win a clash)."""
    out = {}
    agg_of = {"sum": "sum", "total": "sum", "average": "mean", "mean": "mean"}
    for r in rules(ws.documents):
        if r.kind != "define":
            continue
        agg, _, obj = r.obj.partition(" of ")
        col = resolve_column(obj, None, ws.tables, prefer_name=False)
        if col and agg in agg_of:
            t, c = col.split(".", 1)
            out[r.subject.strip()] = {"table": t, "column": c, "aggregation": agg_of[agg], "source": r.doc}
    return out
