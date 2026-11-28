"""H2.1 analysis: per-question scores, the three predictions, the refined-H2 verdict and the ceiling rule.

Everything is computed from the results JSONL. A question's score is its exact-match correctness, except T11,
which is scored by set F1 (exact match is reported as secondary). Every difference is paired over questions
(the same questions in every cell, arm and model) with a percentile bootstrap.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from monograph.ablation.cells import BASELINE, CELLS, HARDEST, Cell
from monograph.ablation.run import HAIKU, MODELS
from monograph.agent.arms import ARMS
from monograph.extract.score import bootstrap_mean

SEED = 20261128
RESAMPLES = 2000

F1_TYPES = frozenset({"T11"})
IDENTITY_TYPES = ("T3", "T4")
PATH_TYPES = ("T9", "T10")
P1_CONFIRMED_DROP = 0.20
P1_REFUTED_BELOW = 0.10
P2_MARGIN = 0.10
P3_MARGIN = 0.10
REFINED_FAILS_WITHIN = 0.05
CEILING = 0.95

FREE, TOOLS = ("S-free", "G-free"), ("S-tools", "G-tools")
CELL = {c.name: c for c in CELLS}
ID_ON_DESC_OFF = next(c for c in CELLS if c.identity and not c.descriptions)
ID_OFF_DESC_ON = next(c for c in CELLS if not c.identity and c.descriptions)


def score(record: Mapping[str, Any]) -> float:
    return float(record["f1"]) if record["type"] in F1_TYPES else (1.0 if record["correct"] else 0.0)


def runs(records: Iterable[Mapping[str, Any]], split: str) -> list[Mapping[str, Any]]:
    """One record per (model, cell, arm, question): the last written, so a rerun after an API error counts once."""
    latest: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for r in records:
        if r.get("metric") != "h2_1.run":
            continue
        d = r["details"]
        if d.get("split") != split:
            continue
        latest[(d["model"], d["cell"], d["arm"], d["question_id"])] = d
    return list(latest.values())


@dataclass
class Table:
    """Scores per configuration and question, and each question's type."""

    scores: dict[tuple[str, str, str], dict[str, float]] = field(default_factory=lambda: defaultdict(dict))
    exact: dict[tuple[str, str, str], dict[str, float]] = field(default_factory=lambda: defaultdict(dict))
    types: dict[str, str] = field(default_factory=dict)
    records: dict[tuple[str, str, str], list[Mapping[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    @staticmethod
    def of(details: Iterable[Mapping[str, Any]]) -> Table:
        t = Table()
        for d in details:
            key = (d["model"], d["cell"], d["arm"])
            t.scores[key][d["question_id"]] = score(d)
            t.exact[key][d["question_id"]] = 1.0 if d["correct"] else 0.0
            t.types[d["question_id"]] = d["type"]
            t.records[key].append(d)
        return t

    def has(self, model: str, cell: Cell, arm: str) -> bool:
        return bool(self.scores.get((model, cell.name, arm)))

    def of_types(self, model: str, cell: Cell, arm: str, types: Sequence[str], exact: bool = False) -> dict[str, float]:
        source = self.exact if exact else self.scores
        row = source.get((model, cell.name, arm), {})
        return {q: v for q, v in row.items() if self.types[q] in types}


def mean_ci(values: Sequence[float], seed: int = SEED) -> dict[str, float]:
    if not values:
        return {"n": 0}
    low, high = bootstrap_mean(list(values), seed, RESAMPLES)
    return {"n": len(values), "mean": statistics.fmean(values), "ci_low": low, "ci_high": high}


def paired(a: Mapping[str, float], b: Mapping[str, float], seed: int = SEED) -> dict[str, float]:
    """mean(a) - mean(b), paired over the questions both have."""
    keys = sorted(set(a) & set(b))
    return mean_ci([a[k] - b[k] for k in keys], seed)


ALL_BUT_T6 = ("T1", "T3", "T4", "T9", "T10", "T11")


# --------------------------------------------------------------------------------------- predictions


def p1(t: Table, model: str = HAIKU, exclude: Iterable[str] = ()) -> dict[str, Any]:
    """Identity effect: T3+T4, descriptions on, identity on minus identity off, per arm (`exclude`: question ids
    left out, for the pre-registered sensitivity check)."""
    skip = set(exclude)
    arms = {}
    for arm in ARMS:
        on = {q: v for q, v in t.of_types(model, BASELINE, arm, IDENTITY_TYPES).items() if q not in skip}
        off = {q: v for q, v in t.of_types(model, ID_OFF_DESC_ON, arm, IDENTITY_TYPES).items() if q not in skip}
        arms[arm] = paired(on, off)
    drops = [a.get("mean") for a in arms.values()]
    if any(d is None for d in drops):
        verdict = "not evaluable"
    elif all(d >= P1_CONFIRMED_DROP for d in drops if d is not None):
        verdict = "confirmed"
    elif any(d < P1_REFUTED_BELOW for d in drops if d is not None):
        verdict = "refuted"
    else:
        verdict = "partly confirmed"
    return {"model": model, "arms": arms, "verdict": verdict}


def _description_drops(t: Table, model: str, identity: bool) -> dict[str, dict[str, float]]:
    on = BASELINE if identity else ID_OFF_DESC_ON
    off = ID_ON_DESC_OFF if identity else HARDEST
    return {
        arm: {
            q: t.of_types(model, on, arm, ALL_BUT_T6)[q] - t.of_types(model, off, arm, ALL_BUT_T6)[q]
            for q in sorted(set(t.of_types(model, on, arm, ALL_BUT_T6)) & set(t.of_types(model, off, arm, ALL_BUT_T6)))
        }
        for arm in ARMS
    }


def p2(t: Table, model: str = HAIKU, identity: bool = True) -> dict[str, Any]:
    """Description effect: (drop S-free + drop G-free)/2 - (drop S-tools + drop G-tools)/2 over every type
    but T6, per question, at the given identity level (the prediction: identity on)."""
    drops = _description_drops(t, model, identity)
    common = sorted(set.intersection(*(set(d) for d in drops.values()))) if all(drops.values()) else []
    interaction = [
        (drops["S-free"][q] + drops["G-free"][q]) / 2 - (drops["S-tools"][q] + drops["G-tools"][q]) / 2 for q in common
    ]
    per_arm = {arm: mean_ci(list(d.values())) for arm, d in drops.items()}
    result = mean_ci(interaction)
    verdict = "not evaluable"
    if "mean" in result:
        verdict = "holds" if result["mean"] >= P2_MARGIN else "does not hold"
    return {"model": model, "identity": identity, "drops": per_arm, "interaction": result, "verdict": verdict}


def p3(t: Table, excluded: Iterable[str] = ()) -> dict[str, Any]:
    """Store effect on hard paths: G-free minus S-free on T9+T10 (minus non-informative types), baseline cell."""
    types = tuple(x for x in PATH_TYPES if x not in set(excluded))
    models = {}
    for model in MODELS:
        if not (t.has(model, BASELINE, "G-free") and t.has(model, BASELINE, "S-free")):
            continue
        g = t.of_types(model, BASELINE, "G-free", types)
        s = t.of_types(model, BASELINE, "S-free", types)
        models[model] = paired(g, s)
    holds = any(m.get("mean", 0) >= P3_MARGIN and m.get("ci_low", 0) > 0 for m in models.values())
    return {"types": list(types), "models": models, "verdict": "holds" if holds else "does not hold"}


def refined(p3_result: Mapping[str, Any]) -> str:
    models = p3_result["models"]
    if p3_result["verdict"] == "holds":
        return "holds"
    if len(models) == len(MODELS) and all(abs(m.get("mean", 1.0)) <= REFINED_FAILS_WITHIN for m in models.values()):
        return "fails"
    return "partly holds"


def ceiling(t: Table) -> dict[str, dict[str, float]]:
    """Dev, baseline cell: per type, each configuration's mean score. A type is non-informative when every
    configuration (every arm, both models) scores at least CEILING."""
    out: dict[str, dict[str, float]] = {}
    for qtype in sorted(set(t.types.values())):
        row = {}
        for model in MODELS:
            for arm in ARMS:
                values = t.of_types(model, BASELINE, arm, (qtype,))
                if values:
                    row[f"{model} {arm}"] = statistics.fmean(values.values())
        out[qtype] = row
    return out


def non_informative(t: Table) -> list[str]:
    return sorted(q for q, row in ceiling(t).items() if row and all(v >= CEILING for v in row.values()))
