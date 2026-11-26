"""Stage 5: the scorer, tested against the oracle (an extraction built from gold) and degraded copies."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from monograph import wiring
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.extract.oracle import oracle_payload
from monograph.extract.resolve import ReferenceGraph, SubstanceResolver, substance_sources_from_spls
from monograph.extract.score import (
    LabelScorer,
    aggregate,
    attribute,
    bootstrap_difference,
    strength_aliases,
    strength_key,
    usable,
    verdict,
)
from monograph.extract.sources.dailymed import SplDocument, parse_spl

from .conftest import ROOT

V = Vocabulary()
DOCS = [
    parse_spl(p.read_bytes())
    for p in sorted((ROOT / "fixtures" / "h4" / "spl").glob("*.xml"))
    if p.name != "sentinel.xml"
]


@pytest.fixture(scope="module")
def resolver(spec: SchemaSpec) -> SubstanceResolver:
    ref = ReferenceGraph(spec, V, wiring.mapper(spec, V))
    ref.add_substances(substance_sources_from_spls(DOCS))
    return SubstanceResolver(ref, V)


def _record(payload: Any, status: str = "ok") -> dict[str, Any]:
    return {
        "value": status,
        "details": {
            "final_payload": payload,
            "repair_rounds": 0,
            "cost_usd": "0.01",
            "latency_s": 1.0,
            "usage": {},
            "pydantic_valid": True,
            "context_issues": [],
        },
    }


def _score(doc: SplDocument, payload: Any, resolver: SubstanceResolver) -> Any:
    row = {"stratum": "single/solid_oral/rx", "h6_gap": False}
    return LabelScorer(resolver, {doc.set_id: False}, set()).score(doc, row, _record(payload))


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.set_id[:8])
def test_oracle_is_perfect(doc: SplDocument, resolver: SubstanceResolver) -> None:
    s = _score(doc, oracle_payload(doc), resolver)
    assert s.f1 == 1.0
    assert (s.form_correct, s.route_correct) == (s.form_total, s.route_total)
    assert s.values_hallucinated == 0
    assert s.refs_unresolved == 0
    assert s.exact_match, s.errors


def test_degradations_cost_exactly_what_they_break(resolver: SubstanceResolver) -> None:
    doc = DOCS[0]
    oracle = oracle_payload(doc)
    wrong_strength = copy.deepcopy(oracle)
    wrong_strength["products"][0]["ingredients"][0]["strength_value"] = Decimal("999")
    s = _score(doc, wrong_strength, resolver)
    assert (s.ing_fp, s.ing_fn) == (1, 1)
    assert s.values_hallucinated == 1
    assert not s.exact_match
    wrong_form = copy.deepcopy(oracle)
    wrong_form["products"][0]["dosage_form"] = "CAPSULE"
    s = _score(doc, wrong_form, resolver)
    assert s.f1 == 1.0
    assert s.form_correct == s.form_total - 1
    misspelled = copy.deepcopy(oracle)
    misspelled["products"][0]["ingredients"][0]["substance_name"] = "Zzyzxium"
    s = _score(doc, misspelled, resolver)
    assert s.refs_unresolved == 1
    assert s.values_hallucinated == 1


def test_invalid_payloads_score_zero_without_crashing(resolver: SubstanceResolver) -> None:
    doc = DOCS[0]
    for payload in (None, "text", {"products": "x"}, {"products": [{"ingredients": [{"strength_value": "abc"}]}]}):
        s = _score(doc, payload, resolver)
        assert s.ing_tp == 0
        assert not s.exact_match


def test_strength_normalization() -> None:
    assert strength_key(160, "mg", 5, "mL") == strength_key(32, "mg", 1, "mL")
    assert strength_key(Decimal("0.5"), "g", 1, "1") == strength_key(500, "mg", 1, "1")
    assert strength_key(2, "g", 100, "g") == strength_key(20, "mg", 1, "g")
    assert strength_key(1, "mg", 0, "1") is None


def test_attribution_counts_fixes_and_regressions(resolver: SubstanceResolver) -> None:
    doc = DOCS[0]
    good = oracle_payload(doc)
    bad = copy.deepcopy(good)
    bad["products"][0]["ingredients"][0]["strength_value"] = Decimal("999")

    def rnd(i: int, phase: str, payload: Any, fixed: Any = None, fixes: bool = False) -> dict[str, Any]:
        return {
            "value": i,
            "details": {
                "phase": phase,
                "feedback": None,
                "raw_payload": payload,
                "payload": fixed if fixed is not None else payload,
                "fixes": [{}] if fixes else [],
            },
        }

    result = attribute(doc, [rnd(0, "first", bad), rnd(1, "pydantic", good), rnd(2, "graph", bad)], resolver)
    assert result["fixed_by"] == {"pydantic": 1}
    assert result["regressions_by"] == {"graph": 1}
    det = attribute(doc, [rnd(0, "first", bad, fixed=good, fixes=True)], resolver)
    assert det["fixed_by"] == {"deterministic": 1}


def test_usability_and_verdict_rules() -> None:
    good = {
        "mean_f1": 0.96,
        "dosage_form_accuracy": 0.98,
        "route_accuracy": 0.99,
        "unresolvable_rate": 0.0,
        "hallucination_rate": 0.0,
    }
    assert usable(good)[0]
    assert not usable(good | {"hallucination_rate": 0.006})[0]
    s, h = "claude-sonnet-5-5", "claude-haiku-5-5"
    base = {(m, a): False for m in (s, h) for a in ("A0", "A1", "A2", "A3")}
    assert verdict(base | {(h, "A3"): True}, (s, h)) == "holds"
    assert verdict(base | {(h, "A2"): True, (h, "A3"): True}, (s, h)) == "partly holds"  # A2 already usable
    assert verdict(base | {(s, "A1"): True, (h, "A2"): True, (h, "A3"): True}, (s, h)) == "fails"  # fails first
    sonnet_all = {(s, "A1"): True, (s, "A2"): True, (s, "A3"): True}
    assert verdict(base | sonnet_all, (s, h)) == "partly holds"  # A3 usable, but so is A2, and Haiku never
    assert verdict(base | {(s, "A3+"): True, (h, "A3+"): True}, (s, h)) == "partly holds"  # A3+ never counts


def test_aggregate_and_paired_bootstrap(resolver: SubstanceResolver) -> None:
    scores = [_score(d, oracle_payload(d), resolver) for d in DOCS]
    agg = aggregate(scores)
    assert agg["mean_f1"] == 1.0
    assert agg["cost_per_usable_record_usd"] == "0.01"
    diff = bootstrap_difference({"a": 1.0, "b": 0.5}, {"a": 0.5, "b": 0.5}, seed=1, n=200)
    assert diff["labels"] == 2
    assert 0.0 <= diff["ci_low"] <= diff["difference"] <= diff["ci_high"] <= 0.5


def test_strength_basis_moiety_counts_as_the_gold_ingredient(resolver: SubstanceResolver) -> None:
    """The ampicillin/sulbactam SPL states 1 g of ampicillin sodium as ampicillin (classCode ACTIM)."""
    doc = next(d for d in DOCS if d.set_id.startswith("006f9a3f"))
    sodium = doc.products[0].active[0]
    assert sodium.name == "ampicillin sodium"
    assert sodium.strength_basis == (("7C782967RD", "ampicillin"),)
    moiety_named = copy.deepcopy(oracle_payload(doc))
    for product in moiety_named["products"]:
        for ingredient in product["ingredients"]:
            ingredient["substance_name"] = ingredient["substance_name"].removesuffix(" sodium")
    assert _score(doc, moiety_named, resolver).f1 == 1.0
    assert strength_aliases(doc.products) == {"7C782967RD": "JFN36L5S8K", "S4TF6I2330": "DKQ4T82YE6"}


def test_actib_strength_has_no_moiety_alias() -> None:
    from monograph.extract.sources.dailymed import GoldIngredient, GoldProduct

    actib = GoldIngredient("SALT000001", "x hydrochloride", Decimal(500), "mg", Decimal(1), "1")
    product = GoldProduct(None, "X", "TABLET", ("ORAL",), (actib,), (), (), False)
    assert strength_aliases([product]) == {}


def test_a_gold_ingredient_is_never_aliased_to_another() -> None:
    """Nitrofurantoin capsules list nitrofurantoin and nitrofurantoin monohydrate (moiety nitrofurantoin)."""
    from monograph.extract.sources.dailymed import GoldIngredient, GoldProduct

    plain = GoldIngredient("927AH8112L", "NITROFURANTOIN", Decimal(25), "mg", Decimal(1), "1")
    hydrate = GoldIngredient(
        "E1QI2CQQ1I", "NITROFURANTOIN MONOHYDRATE", Decimal(75), "mg", Decimal(1), "1", (("927AH8112L", "x"),)
    )
    assert strength_aliases([GoldProduct(None, "N", "CAPSULE", ("ORAL",), (plain, hydrate), (), (), False)]) == {}
