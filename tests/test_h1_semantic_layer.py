"""The H1 semantic layer: the coverage rule from docs/h1/PLAN.md, as generated data; the views show the
canonical data; each context's database holds only what its arms may see."""

from __future__ import annotations

import json
import os

import psycopg
import pytest

from monograph import wiring
from monograph.gen import semantic_layer as s
from monograph.h1 import build

from .conftest import ROOT
from .h2_support import mini_instances


def _layer() -> s.SemanticLayer:
    return s.build(wiring.build_spec())


def test_coverage_follows_the_rule() -> None:
    cov = _layer().coverage()
    for inside in (
        "Product",
        "PackagedProduct",
        "Substance",
        "Organization",
        "AdverseEventReport",
        "Recall",
        "Product.dosage_form",
        "Product.routes",
        "Product.ingredients.substance",
        "Product.ingredients.strength_value",
        "PackagedProduct.labeler",
        "Recall.products",
        "Recall.recalling_firm",
        "AdverseEventReport.reactions.term",
        "AdverseEventReport.drugs.role",
        "AdverseEventReport.seriousness_criteria",
    ):
        assert inside in cov, inside
    for outside in (
        "ClinicalDrug",
        "Label",
        "Label.sections",
        "Product.clinical_drug",
        "Product.drug_mappings",
        "Substance.active_moiety",
        "Organization.aliases",
        "PackagedProduct.ndc11",
        "AdverseEventReport.drugs.clinical_drug",
        "AdverseEventReport.drugs.harmonized_clinical_drugs",
    ):
        assert outside not in cov, outside


def test_a_view_outside_the_rule_is_refused() -> None:
    layer = _layer()
    bad = s.View(
        "drugs",
        "clinical_drug",
        "x",
        "ClinicalDrug",
        "wh.T_CLIN_DRG c",
        (s.VCol("rxcui", "c.RXCUI", "key", "ClinicalDrug.rxcui", "text", "x"),),
        "x",
    )
    with pytest.raises(ValueError, match="outside the coverage rule"):
        s.build_from((*layer.views, bad), layer.measures, layer.joins)


def test_named_measures_with_grain() -> None:
    measures = _layer().measures
    assert len(measures) >= 8
    names = {m.name for m in measures}
    assert {"serious_report_count", "recall_count", "product_count"} <= names
    serious = next(m for m in measures if m.name == "serious_report_count")
    assert "DISTINCT" in serious.sql
    assert serious.grain == "adverse_event_report"


def test_codes_are_decoded_and_the_model_is_rich() -> None:
    text = (wiring.generated_dir(ROOT) / s.MODEL_FILE).read_text(encoding="utf-8")
    assert "\t" not in text
    assert "synonyms:" in text
    assert "allowed_values:" in text
    assert 'relationship: "many_to_one"' in text
    views = (wiring.generated_dir(ROOT) / s.VIEWS_FILE).read_text(encoding="utf-8")
    assert "CD_DOM = 'RTE'" in views
    assert "CD_DOM = 'DSG_FRM'" in views
    mapping = json.loads((wiring.generated_dir(ROOT) / s.MAPPING_FILE).read_text(encoding="utf-8"))
    assert {v["view"] for v in mapping["views"]} >= {"products", "recalls", "recall_products", "packages"}


def test_the_yaml_emitter() -> None:
    lines = s.to_yaml({"a": "x", "b": [{"c": 1, "d": ["e"]}], "f": [], "g": True})
    assert lines == ['a: "x"', "b:", "  - c: 1", "    d:", '      - "e"', "f: []", "g: true"]


@pytest.mark.integration
def test_both_contexts_on_the_mini_fixture() -> None:
    admin = os.environ.get("MONOGRAPH_PG_DSN")
    if not admin:
        pytest.skip("MONOGRAPH_PG_DSN is not set (make it)")
    reports = build.build(admin, mini_instances(), ROOT, lambda _: None, prefix="h1_test")
    assert reports["sv"].semantic_checks["products"] == 3
    assert reports["wh"].semantic_checks == {}
    for context in build.CONTEXTS:
        with psycopg.connect(build.agent_dsn(admin, context, "h1_test")) as conn:
            visible = {
                r[0]
                for r in conn.execute(
                    "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'"
                )
            }
            assert visible - {"public"} == set(build.schemas(context))
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TEMP TABLE x (a int)")
