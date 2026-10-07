"""Curated synonyms: a question word is read as a data word only through these groups, never by guessing.

A word is replaced only when it is not itself a data word and exactly one member of its group is a word of this
data (a table, column or metric name). Two candidates means the word stays as asked, so the question is refused
rather than guessed. Terms that need a business definition (profit, margin, ROI, ...) are in no group on purpose:
'profit' must never become 'amount'.
"""
import re

GROUPS = [
    {"revenue", "sales", "income", "turnover", "earnings", "takings"},
    {"cost", "spend", "spending", "expense", "expenditure", "outlay"},
    {"quantity", "qty", "units", "volume"},
    {"customer", "client", "buyer", "purchaser", "shopper"},
    {"employee", "staff", "worker", "personnel", "headcount"},
    {"supplier", "vendor"},
    {"product", "sku", "article"},
    {"region", "area", "territory", "zone"},
    {"shipment", "delivery", "consignment"},
    {"carrier", "courier", "shipper"},
    {"doctor", "physician", "clinician"},
    {"kwh", "energy", "consumption", "usage", "electricity"},
    {"duration", "minutes", "length"},
    {"weight", "mass"},
    {"cholesterol", "chol"},
    {"salary", "pay", "wage", "wages", "compensation"},
    {"student", "pupil", "learner"},
    {"school", "campus"},
    {"booking", "reservation"},
    {"ticket", "admission"},
]
NEVER = {"profit", "profits", "margin", "margins", "roi", "ebitda", "nps", "churn", "retention", "ltv", "cac"}
GROUP_OF = {w: g for g in GROUPS for w in g}


def _base(w: str) -> str:
    if w.endswith("ies"):
        return w[:-3] + "y"
    return w[:-1] if w.endswith("s") and not w.endswith("ss") and w[:-1] in GROUP_OF else w


def apply_synonyms(ql: str, vocab: set[str], notes: list[str]) -> str:
    """Rewrite synonym words into the data's own words, recording each rewrite in `notes`."""
    for w in sorted(set(re.findall(r"[a-z]+", ql))):
        base = _base(w)
        if w in NEVER or w in vocab or base in vocab or base not in GROUP_OF:
            continue
        targets = sorted({t for t in GROUP_OF[base] if t != base and (t in vocab or t + "s" in vocab)})
        if len(targets) != 1:
            continue  # no data word, or several: the question keeps its own word and is not guessed
        t = targets[0]
        plural = w != base
        new = t + "s" if plural and t + "s" in vocab else t
        ql = re.sub(rf"\b{w}\b", new, ql)
        notes.append(f"'{w}' read as '{new}' (synonym)")
    return ql
