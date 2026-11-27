"""H2 scoring: exact answer accuracy (primary), set F1 (secondary), failure categories, the verdict.

* ids: correct when the answered set equals the gold set exactly (ids compared as canonical IRIs; the part
  after the base is accepted); F1 over the two sets.
* number: correct when equal.
* none: correct when the answer is `none` or an empty id set.

Failure categories, for a wrong answer, first that applies:
1. refusal: the model refused;
2. budget exhausted: no answer (tool, turn or token budget, truncation, or stopped without answering);
3. hallucinated id: the answer names an id that is not in the database;
4. wrong entity resolution: no tool call input, result preview or result id ever mentions the entity the
   question names (the anchor), by id or by key, so the agent never reached it;
5. query error or timeout: a tool call failed and the answer is wrong;
6. missing hop: the answer's ids are of a different kind than the gold's (for example products where firms
   were asked), or a number was given where ids were asked, or the reverse;
7. wrong set: right kind, wrong members.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from monograph.extract.score import bootstrap_difference, bootstrap_mean
from monograph.tools.registry import canonical_id, label_of

CORE_TYPES = ("T2", "T3", "T4")
TYPES = ("T1", "T2", "T3", "T4", "T5", "T6")
FAILURES = (
    "refusal",
    "budget exhausted",
    "hallucinated id",
    "wrong entity resolution",
    "query error or timeout",
    "missing hop",
    "wrong set",
)
SEED = 20261127

# Verdict thresholds (PREREG).
HOLDS_MARGIN = 0.10
T1_TOLERANCE = 0.05
FAILS_WITHIN = 0.05


@dataclass(frozen=True, slots=True)
class Gold:
    kind: str  # ids | number | none
    ids: frozenset[str] = frozenset()
    number: int | None = None

    @staticmethod
    def parse(data: Mapping[str, Any]) -> Gold:
        return Gold(str(data["kind"]), frozenset(data.get("ids", [])), data.get("number"))


def _answer_ids(answer: Mapping[str, Any]) -> set[str]:
    return {canonical_id(str(x)) for x in answer.get("ids") or []}


def score_answer(answer: Mapping[str, Any] | None, gold: Gold) -> tuple[bool, float]:
    """(exact, F1)."""
    if answer is None:
        return False, 0.0
    kind = answer.get("kind")
    if gold.kind == "none":
        ok = kind == "none" or (kind == "ids" and not answer.get("ids"))
        return ok, 1.0 if ok else 0.0
    if gold.kind == "number":
        ok = kind == "number" and answer.get("number") == gold.number
        return ok, 1.0 if ok else 0.0
    if kind != "ids":
        return False, 0.0
    got = _answer_ids(answer)
    if got == set(gold.ids):
        return True, 1.0
    both = len(got & gold.ids)
    return False, (2 * both / (len(got) + len(gold.ids))) if (got or gold.ids) else 1.0


def failure_category(
    record: Mapping[str, Any], gold: Gold, anchors: Sequence[str], known_ids: set[str] | None
) -> str | None:
    """The category of a wrong answer (None when the answer is correct)."""
    if record["correct"]:
        return None
    outcome = record["outcome"]
    if outcome == "refusal":
        return "refusal"
    answer = record.get("answer")
    if outcome != "answered" or answer is None:
        return "budget exhausted"
    got = _answer_ids(answer)
    if known_ids is not None and any(i not in known_ids for i in got):
        return "hallucinated id"
    seen = " ".join(
        str(c.get("input")) + " " + str(c.get("preview", "")) + " " + " ".join(c.get("ids_seen", []))
        for c in record.get("calls", [])
    )
    if anchors and not any(a in seen or a.rsplit("/", 1)[-1] in seen for a in anchors):
        return "wrong entity resolution"
    if any(c.get("is_error") for c in record.get("calls", [])):
        return "query error or timeout"
    kind = answer.get("kind")
    if gold.kind == "ids" and kind == "ids":
        gold_labels = {label_of(i) for i in gold.ids}
        if got and not ({label_of(i) for i in got} & gold_labels):
            return "missing hop"
        return "wrong set"
    if (gold.kind == "ids") != (kind == "ids") and kind != "none" and gold.kind != "none":
        return "missing hop"
    return "wrong set"


# --------------------------------------------------------------------------------------- aggregation


@dataclass
class Cell:
    """One model x arm x question-type cell."""

    n: int = 0
    correct: int = 0
    f1: list[float] = field(default_factory=list)
    failures: Counter[str] = field(default_factory=Counter)
    tool_calls: list[int] = field(default_factory=list)
    latency: list[float] = field(default_factory=list)
    cost: Decimal = Decimal(0)
    tokens: Counter[str] = field(default_factory=Counter)
    per_question: dict[str, float] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    def summary(self) -> dict[str, Any]:
        lo, hi = bootstrap_mean(list(self.per_question.values()), SEED, 2000) if self.n else (0.0, 0.0)
        lat = sorted(self.latency)
        return {
            "n": self.n,
            "accuracy": self.accuracy,
            "ci": [lo, hi],
            "f1": statistics.fmean(self.f1) if self.f1 else 0.0,
            "failures": dict(self.failures),
            "tool_calls_mean": statistics.fmean(self.tool_calls) if self.tool_calls else 0.0,
            "latency_p50": lat[len(lat) // 2] if lat else 0.0,
            "latency_p95": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else 0.0,
            "cost_usd": str(self.cost),
            "usd_per_correct": str(self.cost / self.correct) if self.correct else None,
            "tokens": dict(self.tokens),
        }


def aggregate(records: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], Cell]:
    """Cells keyed by (model, arm, type), plus type "core" (T2+T3+T4) and "all"."""
    cells: dict[tuple[str, str, str], Cell] = defaultdict(Cell)
    for r in records:
        groups = [r["type"], "all"] + (["core"] if r["type"] in CORE_TYPES else [])
        for g in groups:
            c = cells[(r["model"], r["arm"], g)]
            c.n += 1
            c.correct += int(r["correct"])
            c.f1.append(float(r["f1"]))
            c.per_question[r["question_id"]] = 1.0 if r["correct"] else 0.0
            if r.get("failure"):
                c.failures[r["failure"]] += 1
            c.tool_calls.append(len(r.get("calls", [])))
            c.latency.append(float(r.get("latency_s", 0.0)))
            c.cost += Decimal(str(r.get("cost_usd", "0")))
            for k, v in (r.get("usage") or {}).items():
                c.tokens[k] += int(v)
    return cells


def difference(cells: Mapping[tuple[str, str, str], Cell], model: str, a: str, b: str, group: str) -> dict[str, float]:
    """Paired bootstrap of accuracy(a) - accuracy(b) over the questions both answered."""
    x, y = cells[(model, a, group)], cells[(model, b, group)]
    d = bootstrap_difference(x.per_question, y.per_question, SEED, 2000)
    if not d.get("labels"):
        return {"n": 0, "mean": 0.0, "lo": 0.0, "hi": 0.0}
    return {"n": d["labels"], "mean": d["difference"], "lo": d["ci_low"], "hi": d["ci_high"]}


def verdict(cells: Mapping[tuple[str, str, str], Cell], models: Sequence[str]) -> dict[str, Any]:
    """PREREG decision rule. "fails" is checked first."""
    per_model = {}
    for m in models:
        core = difference(cells, m, "G-tools", "S-free", "core")
        t1 = difference(cells, m, "G-tools", "S-free", "T1")
        per_model[m] = {
            "core_G-tools_minus_S-free": core,
            "t1_G-tools_minus_S-free": t1,
            "within_fails_margin": abs(core["mean"]) <= FAILS_WITHIN,
            "holds": core["mean"] >= HOLDS_MARGIN and core["lo"] > 0 and t1["mean"] >= -T1_TOLERANCE,
        }
    if all(v["within_fails_margin"] for v in per_model.values()):
        outcome = "fails"
    elif any(v["holds"] for v in per_model.values()):
        outcome = "holds"
    else:
        outcome = "partly holds"
    return {"verdict": outcome, "per_model": per_model}


def attribution(cells: Mapping[tuple[str, str, str], Cell], model: str, group: str) -> dict[str, dict[str, float]]:
    return {
        "store: G-tools - S-tools": difference(cells, model, "G-tools", "S-tools", group),
        "store: G-free - S-free": difference(cells, model, "G-free", "S-free", group),
        "interface: S-tools - S-free": difference(cells, model, "S-tools", "S-free", group),
        "interface: G-tools - G-free": difference(cells, model, "G-tools", "G-free", group),
    }


def tools_not_graph(attr: Mapping[str, Mapping[str, float]]) -> bool:
    """The report must say the win comes from the tools when a store interval includes 0 and an interface
    interval does not."""
    store_zero = any(v["lo"] <= 0 <= v["hi"] for k, v in attr.items() if k.startswith("store"))
    interface_clear = any(not (v["lo"] <= 0 <= v["hi"]) for k, v in attr.items() if k.startswith("interface"))
    return store_zero and interface_clear
