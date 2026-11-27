"""`monograph h4 score`: score the latest records of a split and append every aggregate to results/h4."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from monograph.extract.config import ARMS, MODELS, UPPER_BOUND_ARMS
from monograph.extract.score import (
    LabelScorer,
    aggregate,
    as_dict,
    attribute,
    bootstrap_difference,
    bootstrap_mean,
    error_patterns,
    gap_products_correct,
    group_scores,
    usable,
    verdict,
)
from monograph.extract.setup import H4Context
from monograph.harness.results import ResultsWriter

SEED = 20261127

Key = tuple[str, str, str]  # model, arm, set_id


def latest(
    results: Iterable[dict[str, Any]], split: str, system_hash: str | None = None
) -> tuple[dict[Key, dict[str, Any]], dict[Key, list[dict[str, Any]]]]:
    """The latest record per (model, arm, label) for the split, and the rounds of that same run. With
    `system_hash`, only records made with that system prompt count."""
    rows = sorted(
        (
            r
            for r in results
            if r["details"].get("split") == split
            and (
                system_hash is None
                or r["metric"] != "h4.record"
                or r["details"].get("system_prompt_sha256") == system_hash
            )
        ),
        key=lambda r: r["timestamp"],
    )
    records: dict[Key, dict[str, Any]] = {}
    for r in rows:
        if r["metric"] == "h4.record":
            d = r["details"]
            records[(d["model"], d["arm"], d["set_id"])] = r
    rounds: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["metric"] != "h4.round":
            continue
        d = r["details"]
        key = (d["model"], d["arm"], d["set_id"])
        if key in records and records[key]["run_id"] == r["run_id"]:
            rounds[key].append(r)
    for key, sequence in rounds.items():  # a rerun inside one run_id keeps only its last sequence
        starts = [i for i, r in enumerate(sequence) if r["value"] == 0]
        rounds[key] = sorted(sequence[starts[-1] :], key=lambda r: r["value"])
    return records, rounds


class ScoreRun:
    def __init__(self, ctx: H4Context, writer: ResultsWriter, log: Callable[[str], None]) -> None:
        self._ctx = ctx
        self._writer = writer
        self._log = log

    def run(self, results: Iterable[dict[str, Any]], split: str, gap_lookup: dict[str, list[str]]) -> None:
        ctx = self._ctx
        rows = {r["set_id"]: r for r in ctx.split[split]}
        label_known = {sid: bool(ctx.reference.label_products(sid)) for sid in rows}
        ndc_known = set(ctx.checker._ndcs._index)  # the registry's NDCs
        scorer = LabelScorer(ctx.substances, label_known, ndc_known)
        records, rounds = latest(results, split, ctx.prompts.system_hash)
        by_config: dict[tuple[str, str], list[Any]] = defaultdict(list)
        attribution: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        gap: dict[tuple[str, str], dict[str, bool]] = defaultdict(dict)
        gap_by_label: dict[str, list[str]] = defaultdict(list)
        for product_ndc, set_ids in gap_lookup.items():
            for sid in set_ids:
                gap_by_label[sid].append(product_ndc)
        for (model, arm, set_id), record in sorted(records.items()):
            if set_id not in rows:
                continue
            doc = ctx.docs[set_id]
            score = scorer.score(doc, rows[set_id], record)
            by_config[(model, arm)].append(score)
            self._writer.write("h4.label", score.f1, {"model": model, "arm": arm, "split": split, **as_dict(score)})
            attribution[(model, arm)].append(attribute(doc, rounds[(model, arm, set_id)], ctx.substances))
            if set_id in gap_by_label:
                gap[(model, arm)] |= gap_products_correct(
                    doc, record["details"]["final_payload"], gap_by_label[set_id], ctx.substances
                )
        usable_by: dict[tuple[str, str], bool] = {}
        f1_by: dict[tuple[str, str], dict[str, float]] = {}
        for (model, arm), scores in sorted(by_config.items()):
            for group, members in sorted(group_scores(scores).items()):
                agg = aggregate(members)
                ok, checks = usable(agg) if group == "headline" else (None, None)
                if group == "headline":
                    usable_by[(model, arm)] = bool(ok)
                    low, high = bootstrap_mean([s.f1 for s in members], SEED)
                    agg |= {"mean_f1_ci_low": low, "mean_f1_ci_high": high}
                    f1_by[(model, arm)] = {s.set_id: s.f1 for s in members}
                self._writer.write(
                    "h4.aggregate",
                    agg.get("mean_f1"),
                    {
                        "model": model,
                        "arm": arm,
                        "split": split,
                        "group": group,
                        "usable": ok,
                        "checks": checks,
                        "upper_bound_only": arm in UPPER_BOUND_ARMS,
                        **agg,
                    },
                )
            totals: dict[str, Any] = defaultdict(int)
            merged: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for item in attribution[(model, arm)]:
                for k in ("first_round_errors", "final_errors", "graph_fixes_with_gold_in_feedback"):
                    totals[k] += item[k]
                for k in ("fixed_by", "fixed_by_round", "regressions_by"):
                    for name, n in item[k].items():
                        merged[k][name] += n
            self._writer.write(
                "h4.attribution",
                totals["final_errors"],
                {"model": model, "arm": arm, "split": split, **totals, **{k: dict(v) for k, v in merged.items()}},
            )
            headline = [s for s in scores if s.headline]
            self._writer.write(
                "h4.errors",
                len(error_patterns(headline)),
                {"model": model, "arm": arm, "split": split, "patterns": error_patterns(headline)},
            )
            products = gap[(model, arm)]
            self._writer.write(
                "h4.gap",
                sum(products.values()),
                {
                    "model": model,
                    "arm": arm,
                    "split": split,
                    "products_in_split": len(products),
                    "correct": sorted(k for k, v in products.items() if v),
                },
            )
        for model in MODELS:
            for a, b in (("A1", "A0"), ("A2", "A1"), ("A3", "A2"), ("A3", "A1"), ("A3+", "A3")):
                if (model, a) in f1_by and (model, b) in f1_by:
                    diff = bootstrap_difference(f1_by[(model, a)], f1_by[(model, b)], SEED)
                    self._writer.write(
                        "h4.difference",
                        diff.get("difference"),
                        {"split": split, "comparison": f"{model} {a} - {b}", **diff},
                    )
        for arm in ARMS:
            if all((m, arm) in f1_by for m in MODELS):
                diff = bootstrap_difference(f1_by[(MODELS[0], arm)], f1_by[(MODELS[1], arm)], SEED)
                self._writer.write(
                    "h4.difference",
                    diff.get("difference"),
                    {"split": split, "comparison": f"{arm} {MODELS[0]} - {MODELS[1]}", **diff},
                )
        if split == "test" and all((m, a) in usable_by for m in MODELS for a in ARMS):
            outcome = verdict(usable_by, MODELS)
            self._writer.write(
                "h4.verdict",
                outcome,
                {"split": split, "usable": {f"{m} {a}": usable_by[(m, a)] for m in MODELS for a in ARMS}},
            )
            self._log(f"H4 verdict on test: {outcome}")
        self._log(f"scored {sum(len(v) for v in by_config.values())} records over {len(by_config)} configurations")
