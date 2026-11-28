"""H1 answers and scoring (docs/h1/PLAN.md, "Answers and scoring").

* values: correct when the answered set equals the gold set. Each gold entity accepts its business key and,
  for firms and substances, its name; matching ignores case and surrounding space; order and duplicates do
  not matter.
* number: correct when |answer - gold| <= max(0.01, 0.001 x |gold|).
* none: correct when the answer is `none` or an empty `values` list.

`result_matches` applies the same rule to a query result, so a query can be scored against gold without an
answer (gold-equivalent blocked queries, and the reference SQL that proves the warehouse answers each question
the way the canonical model does).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ABS_TOLERANCE = 0.01
REL_TOLERANCE = 0.001


class H1Answer(BaseModel):
    """The final answer, the same for every arm."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["values", "number", "none"]
    values: list[str] = Field(
        default=[],
        description="kind values: the complete set of identifiers (or names, where the question asks for names).",
    )
    number: float | None = Field(default=None, description="kind number: the count or measure.")
    evidence: list[int] = Field(default=[], description="Numbers of the tool calls that produced the answer.")


SUBMIT = {
    "name": "submit_answer",
    "description": (
        "Submit the final answer, once. kind 'values' with the complete set of identifiers the question asks for"
        " (product NDCs, package NDCs, UNIIs, report ids, recall numbers, RxCUIs, firm names), 'number' for a"
        " count or a measure, or 'none' when the entity the question names does not exist or nothing satisfies"
        " the question; evidence lists the numbers of the tool calls that produced it."
    ),
    "input_schema": {**H1Answer.model_json_schema(), "additionalProperties": False},
}


def norm(text: Any) -> str:
    return " ".join(str(text).split()).casefold()


def values_gold(entities: Sequence[tuple[str, Sequence[str]]]) -> dict[str, Any]:
    """Gold for a set: each entity's key and the other strings that name it."""
    return {
        "kind": "values",
        "entities": [{"key": k, "names": sorted(set(n for n in names if n and n != k))} for k, names in entities],
    }


def number_gold(value: int | float | Decimal) -> dict[str, Any]:
    return {"kind": "number", "number": float(value)}


NONE_GOLD: dict[str, Any] = {"kind": "none"}


def _lookup(gold: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, e in enumerate(gold["entities"]):
        for s in (e["key"], *e["names"]):
            out.setdefault(norm(s), i)
    return out


def _set_matches(values: Sequence[Any], gold: Mapping[str, Any]) -> bool:
    got = {norm(v) for v in values if v is not None and str(v).strip()}
    lookup = _lookup(gold)
    hit: set[int] = set()
    for v in got:
        if v not in lookup:
            return False
        hit.add(lookup[v])
    return hit == set(range(len(gold["entities"])))


def number_matches(value: Any, gold: float) -> bool:
    try:
        x = float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return abs(x - gold) <= max(ABS_TOLERANCE, REL_TOLERANCE * abs(gold))


def score_answer(answer: Mapping[str, Any] | None, gold: Mapping[str, Any]) -> bool:
    if answer is None:
        return False
    kind = answer.get("kind")
    if gold["kind"] == "none":
        return kind == "none" or (kind == "values" and not answer.get("values"))
    if gold["kind"] == "number":
        return (
            kind == "number" and answer.get("number") is not None and number_matches(answer["number"], gold["number"])
        )
    return kind == "values" and _set_matches(answer.get("values") or [], gold)


def result_matches(columns: Sequence[str], rows: Sequence[Sequence[Any]], gold: Mapping[str, Any]) -> bool:
    """A query result already answers the question: for values, some column's set of values is the gold set;
    for a number, a single cell within tolerance; for none, no rows or a single zero or empty cell."""
    del columns
    if gold["kind"] == "none":
        return not rows or (len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] in (0, None, "0"))
    if gold["kind"] == "number":
        return len(rows) == 1 and len(rows[0]) == 1 and number_matches(rows[0][0], gold["number"])
    if not rows:
        return False
    width = len(rows[0])
    return any(_set_matches([r[i] for r in rows], gold) for i in range(width))
