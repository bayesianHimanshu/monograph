"""H1 analysis: the decision rules of docs/h1/PREREG.md, attribution, coverage, the check's quality, failure
categories and cost, all from the results JSONL alone.

Verdict (primary): H1 holds if, for at least one model, ONT+OC beats SV by at least 0.10 on the core types
combined (Q2 to Q5, less any type marked non-informative) with the 95% interval excluding 0, and ONT+OC is no
more than 0.05 below SV on Q1 (point estimates; Sonnet has no Q1 runs, so for Sonnet the Q1 condition uses
Haiku's). H1 fails if, for both models, ONT+OC beats SV by at most 0.05 on the core types (SV within 0.05 of
ONT+OC, or ahead of it); "fails" takes precedence. Otherwise it partly holds.

Secondary: the same rule restricted to the Q2 and Q5 questions inside semantic coverage.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from monograph.ablation.analysis import mean_ci, paired
from monograph.h1.arms import ARMS
from monograph.h1.record import FAILURES
from monograph.h1.run import HAIKU, MODELS, SONNET
from monograph.qa.h1_templates import CORE_TYPES, TYPES

HOLDS_MARGIN = 0.10
Q1_TOLERANCE = 0.05
FAILS_WITHIN = 0.05
CEILING = 0.95
SECONDARY_TYPES = ("Q2", "Q5")
METRIC = "h1.run"


def latest_runs(records: Iterable[Mapping[str, Any]], split: str) -> list[Mapping[str, Any]]:
    """One record per (model, arm, question): the last written, so a rerun after an API error counts once."""
    latest: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for r in records:
        if r.get("metric") != METRIC:
            continue
        d = r["details"]
        if d.get("split") == split:
            latest[(d["model"], d["arm"], d["question_id"])] = d
    return list(latest.values())


@dataclass
class Table:
    exact: dict[tuple[str, str], dict[str, float]] = field(default_factory=lambda: defaultdict(dict))
    types: dict[str, str] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)
    records: dict[tuple[str, str], list[Mapping[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    @staticmethod
    def of(details: Iterable[Mapping[str, Any]]) -> Table:
        t = Table()
        for d in details:
            key = (d["model"], d["arm"])
            t.exact[key][d["question_id"]] = 1.0 if d["correct"] else 0.0
            t.types[d["question_id"]] = d["type"]
            t.coverage[d["question_id"]] = d.get("coverage", "")
            t.records[key].append(d)
        return t

    def has(self, model: str, arm: str) -> bool:
        return bool(self.exact.get((model, arm)))

    def of_types(self, model: str, arm: str, types: Sequence[str], coverage: str | None = None) -> dict[str, float]:
        row = self.exact.get((model, arm), {})
        return {
            q: v
            for q, v in row.items()
            if self.types[q] in types and (coverage is None or self.coverage[q] == coverage)
        }


# ------------------------------------------------------------------------------------------ ceiling


def ceiling(t: Table) -> dict[str, dict[str, float]]:
    """Dev: per type, each configuration's accuracy."""
    out: dict[str, dict[str, float]] = {}
    for qtype in TYPES:
        row = {}
        for model in MODELS:
            for arm in ARMS:
                values = t.of_types(model, arm, (qtype,))
                if values:
                    row[f"{model} {arm}"] = statistics.fmean(values.values())
        out[qtype] = row
    return out


def non_informative(t: Table) -> list[str]:
    """Types at CEILING or above in every arm (every configuration that ran them) on dev."""
    return sorted(q for q, row in ceiling(t).items() if row and all(v >= CEILING for v in row.values()))


# ------------------------------------------------------------------------------------------ verdicts


def _core(excluded: Iterable[str]) -> tuple[str, ...]:
    gone = set(excluded)
    return tuple(x for x in CORE_TYPES if x not in gone)


def comparison(
    t: Table, model: str, types: Sequence[str], coverage: str | None = None, q1_model: str | None = None
) -> dict[str, Any]:
    """ONT+OC against SV for one model on `types`, and the Q1 condition."""
    a = t.of_types(model, "ONT+OC", types, coverage)
    b = t.of_types(model, "SV", types, coverage)
    diff = paired(a, b)
    q1m = q1_model or model
    q1 = paired(t.of_types(q1m, "ONT+OC", ("Q1",)), t.of_types(q1m, "SV", ("Q1",)))
    holds = (
        diff.get("n", 0) > 0
        and diff["mean"] >= HOLDS_MARGIN
        and diff["ci_low"] > 0
        and q1.get("n", 0) > 0
        and q1["mean"] >= -Q1_TOLERANCE
    )
    within = diff.get("n", 0) > 0 and diff["mean"] <= FAILS_WITHIN
    return {
        "model": model,
        "types": list(types),
        "coverage": coverage,
        "ont_oc": mean_ci(list(a.values())),
        "sv": mean_ci(list(b.values())),
        "difference": diff,
        "q1_model": q1m,
        "q1_difference": q1,
        "holds": holds,
        "sv_within": within,
    }


def _models(t: Table) -> list[str]:
    return [m for m in MODELS if t.has(m, "ONT+OC") and t.has(m, "SV")]


def verdict(comparisons: Sequence[Mapping[str, Any]]) -> str:
    if comparisons and all(c["sv_within"] for c in comparisons):
        return "fails"
    if any(c["holds"] for c in comparisons):
        return "holds"
    return "partly holds"


def primary(t: Table, excluded: Iterable[str] = ()) -> dict[str, Any]:
    types = _core(excluded)
    cs = [comparison(t, m, types, None, HAIKU if m == SONNET else None) for m in _models(t)]
    return {"types": list(types), "comparisons": cs, "verdict": verdict(cs)}


def secondary(t: Table, excluded: Iterable[str] = ()) -> dict[str, Any]:
    types = tuple(x for x in SECONDARY_TYPES if x not in set(excluded))
    cs = [comparison(t, m, types, "inside", HAIKU if m == SONNET else None) for m in _models(t)]
    return {"types": list(types), "comparisons": cs, "verdict": verdict(cs)}


# ---------------------------------------------------------------------------------------- attribution

EFFECTS: tuple[tuple[str, str, str], ...] = (
    ("context", "ONT", "SV"),
    ("context", "ONT+OC", "SV+OC"),
    ("check", "SV+OC", "SV"),
    ("check", "ONT+OC", "ONT"),
)


def attribution(t: Table, excluded: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Context and check effects, and each arm minus W-raw (the floor), per model, on the core types and on
    each type."""
    types = _core(excluded)
    out = []
    for model in MODELS:
        pairs = [(kind, a, b) for kind, a, b in EFFECTS] + [("floor", a, "W-raw") for a in ARMS if a != "W-raw"]
        for kind, a, b in pairs:
            if not (t.has(model, a) and t.has(model, b)):
                continue
            row: dict[str, Any] = {
                "model": model,
                "effect": kind,
                "arms": f"{a} - {b}",
                "core": paired(t.of_types(model, a, types), t.of_types(model, b, types)),
            }
            row["by_type"] = {
                q: paired(t.of_types(model, a, (q,)), t.of_types(model, b, (q,)))
                for q in TYPES
                if t.of_types(model, a, (q,))
            }
            out.append(row)
    return out


def accuracy(t: Table) -> list[dict[str, Any]]:
    """Accuracy per model, arm and type, with the core and coverage splits."""
    out = []
    for model in MODELS:
        for arm in ARMS:
            if not t.has(model, arm):
                continue
            row: dict[str, Any] = {"model": model, "arm": arm}
            for q in TYPES:
                values = t.of_types(model, arm, (q,))
                if values:
                    row[q] = mean_ci(list(values.values()))
            row["core"] = mean_ci(list(t.of_types(model, arm, CORE_TYPES).values()))
            for cov in ("inside", "outside"):
                row[cov] = mean_ci(list(t.of_types(model, arm, TYPES, cov).values()))
            out.append(row)
    return out


# --------------------------------------------------------------------------------------- the check


def check_quality(t: Table) -> list[dict[str, Any]]:
    """Per model and arm: precision and recall of the check's verdict on the evidence queries (shadow in the
    non-OC arms), blocked queries, gold-equivalent blocks, false blocks and repair rounds."""
    out = []
    for (model, arm), rs in sorted(t.records.items()):
        flagged = [r for r in rs if r.get("check_flagged")]
        wrong = [r for r in rs if not r["correct"]]
        blocked = sum(int(r.get("blocked_queries", 0)) for r in rs)
        false_blocks = sum(int(r.get("false_blocks", 0)) for r in rs)
        out.append(
            {
                "model": model,
                "arm": arm,
                "questions": len(rs),
                "flagged": len(flagged),
                "precision": (sum(not r["correct"] for r in flagged) / len(flagged)) if flagged else None,
                "recall": (sum(bool(r.get("check_flagged")) for r in wrong) / len(wrong)) if wrong else None,
                "blocked_queries": blocked,
                "gold_equivalent_blocks": sum(int(r.get("gold_equivalent_blocks", 0)) for r in rs),
                "false_blocks": false_blocks,
                "false_block_rate": (false_blocks / blocked) if blocked else None,
                "questions_with_false_block": sum(1 for r in rs if r.get("false_blocks")),
                "repair_rounds": sum(int(r.get("repair_rounds", 0)) for r in rs),
                "questions_repaired": sum(1 for r in rs if r.get("repair_rounds")),
            }
        )
    return out


def failures(t: Table) -> list[dict[str, Any]]:
    out = []
    for (model, arm), rs in sorted(t.records.items()):
        counts = Counter(r["failure"] for r in rs if not r["correct"])
        out.append({"model": model, "arm": arm, **{f: counts.get(f, 0) for f in FAILURES}})
    return out


def _pct(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def costs(t: Table) -> list[dict[str, Any]]:
    out = []
    for (model, arm), rs in sorted(t.records.items()):
        usd = sum(float(r["cost_usd"]) for r in rs)
        correct = sum(1 for r in rs if r["correct"])
        lat = [float(r["latency_s"]) for r in rs]
        out.append(
            {
                "model": model,
                "arm": arm,
                "questions": len(rs),
                "usd": usd,
                "usd_per_correct": usd / correct if correct else None,
                "tool_calls": statistics.fmean(len(r["calls"]) for r in rs) if rs else 0.0,
                "input_tokens": sum(
                    r["usage"]["input_tokens"] + r["usage"]["cache_read_tokens"] + r["usage"]["cache_write_tokens"]
                    for r in rs
                ),
                "output_tokens": sum(r["usage"]["output_tokens"] for r in rs),
                "latency_p50": _pct(lat, 0.5),
                "latency_p95": _pct(lat, 0.95),
            }
        )
    return out


# ------------------------------------------------------------------------------------------- summary


def summary_lines(runs: Sequence[Mapping[str, Any]]) -> list[str]:
    t = Table.of(runs)
    lines = []
    for row in accuracy(t):
        cells = " ".join(f"{q} {row[q]['mean']:.2f}" for q in TYPES if q in row)
        lines.append(f"{row['model']} {row['arm']:7s} {cells}")
    for c in costs(t):
        lines.append(f"{c['model']} {c['arm']:7s} USD {c['usd']:.2f} over {c['questions']} questions")
    marked = non_informative(t)
    lines.append(f"at the ceiling (>= {CEILING} in every arm): {', '.join(marked) if marked else 'none'}")
    return lines
