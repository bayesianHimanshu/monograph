"""Scoring one H1 run record: correctness, the check's verdicts, false blocks, the failure category.

The record carries the gate's log (`queries`): every run_sql call with the check's violations, whether it was
blocked, executed or failed, and its result (returned, or shadow for a blocked query).

* Evidence queries: the run_sql calls the answer cites, or the last successful one if it cites none.
* Flagged: the check reports a violation on an evidence query (in the non-OC arms, in shadow).
* Gold-equivalent blocked query: its shadow result already answers the question (`result_matches`).
* False block: a gold-equivalent blocked query in a question that ended wrong.

Failure categories (docs/h1/PLAN.md), the first that applies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from monograph.agent.arms import Arm
from monograph.gen import warehouse as w
from monograph.h1.arms import CONTEXT, H1Arm
from monograph.h1.score import result_matches, score_answer

FAILURES: tuple[str, ...] = (
    "refusal",
    "budget exhausted",
    "SQL execution error after retries",
    "hallucinated table or column",
    "outside-coverage miss",
    "identity miss",
    "wrong or invalid code",
    "fan-out double count",
    "wrong join path",
    "other wrong result",
)
KEEP_ROWS = 20

# Where each crosswalk lives in the warehouse (for "identity miss"): a table, or table.column.
CROSSWALK_SQL: dict[str, tuple[str, ...]] = {
    "Substance.active_moiety": ("X_SALT_MOIETY",),
    "Product.drug_mappings": ("X_NDC_RXN",),
    "Product.clinical_drug": ("RXN_CLIN_DRG_SK",),
    "ClinicalDrug.generic": ("X_BRND_GNRC",),
    "Organization.aliases": ("X_ORG_ALIAS",),
    "PackagedProduct.ndc11": ("PKG_NDC11",),
    "AdverseEventReport.drugs.clinical_drug": ("T_AE_DRG",),
    "AdverseEventReport.drugs.harmonized_clinical_drugs": ("X_AE_DRG_RXN",),
}
RAW_TABLE = re.compile(r"\b(T|X)_[A-Z_]+\b", re.IGNORECASE)
DOES_NOT_EXIST = re.compile(r"(column|relation) .* does not exist", re.IGNORECASE)

# What a query must name to filter on a coded value, by ontology term: warehouse code or decoded label.
CODE_DOMAINS: dict[str, str] = {
    "Product.product_type": "PRD_TYP",
    "Product.dosage_form": "DSG_FRM",
    "Product.routes": "RTE",
    "Recall.classification": "RCL_CLS",
    "AdverseEventReport.drugs.role": "DRG_ROLE",
}
FLAG_FORMS: dict[str, tuple[str, ...]] = {
    "death": ("SRS_DTH_FLG", "resulted_in_death"),
}


def annotate(record: dict[str, Any], arm: Arm) -> None:
    """Attach the gate's log to the record (run_split calls this before scoring)."""
    gate = arm.gate if isinstance(arm, H1Arm) else None
    record["queries"] = gate.log() if gate is not None else []
    record["repair_rounds"] = gate.rounds if gate is not None else 0
    record["exec_errors"] = gate.exec_errors if gate is not None else 0
    record["retries_exhausted"] = gate.retries_exhausted if gate is not None else False


def _evidence(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The gate events behind the answer's evidence (run_sql calls matched to events in order)."""
    events: list[dict[str, Any]] = list(record.get("queries", []))
    sql_calls = [c for c in record.get("calls", []) if c.get("name") == "run_sql"]
    by_call: dict[int, dict[str, Any]] = {}
    i = 0
    for c in sql_calls:
        query = str((c.get("input") or {}).get("query", ""))
        while i < len(events) and events[i]["query"] != query:
            i += 1
        if i < len(events):
            by_call[int(c["index"])] = events[i]
            i += 1
    cited = [int(x) for x in ((record.get("answer") or {}).get("evidence") or [])]
    chosen = [by_call[n] for n in cited if n in by_call and by_call[n]["executed"] and not by_call[n]["error"]]
    if chosen:
        return chosen
    ok = [e for e in events if e["executed"] and not e["error"]]
    return ok[-1:]


def _rules(events: Sequence[Mapping[str, Any]]) -> set[str]:
    return {v["rule"] for e in events for v in e["violations"]}


def _wrong_code(question: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]) -> bool:
    """An evidence query breaks rule 2, or does not name a code the gold was computed with (as the warehouse
    code or its label, quoted; for a seriousness flag, its column)."""
    if "code" in _rules(evidence):
        return True
    text = " ".join(e["query"] for e in evidence).casefold()
    for term, labels in (question.get("codes") or {}).items():
        for label in labels:
            if term in CODE_DOMAINS:
                options = [f"'{w.domain(CODE_DOMAINS[term]).code_of(label)}'", f"'{label}'"]
            elif label in FLAG_FORMS:
                options = list(FLAG_FORMS[label])
            else:
                continue
            if not any(o.casefold() in text for o in options):
                return True
    return False


def failure(record: Mapping[str, Any], question: Mapping[str, Any]) -> str | None:
    if record.get("correct"):
        return None
    outcome = record.get("outcome")
    if outcome == "refusal":
        return "refusal"
    if outcome != "answered" or record.get("answer") is None:
        return "budget exhausted"
    events: list[dict[str, Any]] = list(record.get("queries", []))
    if record.get("retries_exhausted"):
        return "SQL execution error after retries"
    unknown = any(
        v["rule"] == "existence" and "not mapped" not in v["message"] for e in events for v in e["violations"]
    ) or any(e["error"] and DOES_NOT_EXIST.search(e["error"]) for e in events)
    if unknown:
        return "hallucinated table or column"
    if (
        CONTEXT.get(str(record.get("arm"))) == "SV"
        and question.get("coverage") == "outside"
        and not any(RAW_TABLE.search(e["query"]) for e in events)
    ):
        return "outside-coverage miss"
    identity = question.get("identity") or []
    if identity:
        names = [n for term in identity for n in CROSSWALK_SQL.get(term, ())]
        if not any(n.casefold() in e["query"].casefold() for e in events for n in names):
            return "identity miss"
    evidence = _evidence(record)
    if _wrong_code(question, evidence):
        return "wrong or invalid code"
    gold = question["gold"]
    answer = record.get("answer") or {}
    over = (
        question.get("type") == "Q5"
        and gold["kind"] == "number"
        and answer.get("kind") == "number"
        and answer.get("number") is not None
        and float(answer["number"]) > gold["number"]
    )
    if "cardinality" in _rules(evidence) or over:
        return "fan-out double count"
    if "join" in _rules(evidence):
        return "wrong join path"
    return "other wrong result"


def score(record: dict[str, Any], question: Mapping[str, Any], known: set[str] | None = None) -> dict[str, Any]:
    """The H1 scoring hook for run_split: correctness, check verdicts, false blocks, failure category."""
    del known
    gold = question["gold"]
    record["correct"] = score_answer(record.get("answer"), gold)
    record["coverage"] = question.get("coverage")
    events: list[dict[str, Any]] = list(record.get("queries", []))
    for e in events:
        e["gold_equivalent"] = bool(e["blocked"] and not e["error"]) and result_matches(e["columns"], e["rows"], gold)
    evidence = _evidence(record)
    record["evidence_queries"] = [e["index"] for e in evidence]
    record["check_flagged"] = any(e["violations"] for e in evidence)
    record["check_rules"] = sorted(_rules(evidence))
    blocked = [e for e in events if e["blocked"]]
    record["blocked_queries"] = len(blocked)
    record["gold_equivalent_blocks"] = sum(e["gold_equivalent"] for e in blocked)
    record["false_blocks"] = 0 if record["correct"] else record["gold_equivalent_blocks"]
    record["failure"] = failure(record, question)
    for e in events:  # keep the record small: the first rows only
        e["rows"] = e["rows"][:KEEP_ROWS]
    return record
