"""Stage 4: arms A0 to A3 on a scripted fake client and a small reference graph."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from rdflib import Graph

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.models import LabelExtraction
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.extract.arms import Extractor
from monograph.extract.config import MAX_REPAIR_ROUNDS, SETTINGS, SONNET
from monograph.extract.llm import FakeClient, LlmRequest, scripted
from monograph.extract.prompts import load_prompts
from monograph.extract.registry import label_links, nested_packages
from monograph.extract.repair.deterministic import normalize_ndc, repair
from monograph.extract.repair.shacl_feedback import EXTRACTION_ONLY_CHECKS, GraphContextChecker
from monograph.extract.resolve import NdcResolver, ReferenceGraph, SubstanceResolver, SubstanceSource
from monograph.extract.sources.dailymed import NarrativeSection, SplDocument, parse_spl
from monograph.gen.artifacts import CONTEXT_SHACL_FILE, EXTRACTION_SCHEMA_FILE
from monograph.ingest.openfda import OpenFdaMapper
from monograph.validate.pydantic_side import PydanticValidator

from .conftest import ROOT
from .test_h4_context import NDC_RECORD, SET_ID

V = Vocabulary()
FILES = wiring.artifact_builder(wiring.build_spec(), V).build().files
DOC = SplDocument(
    SET_ID,
    1,
    "HUMAN OTC DRUG LABEL",
    "Bayer Low Dose Aspirin Chewable Tablets",
    (NarrativeSection("34069-5", "HOW SUPPLIED", "Bottles of 36 chewable tablets, aspirin 81 mg: NDC 0280-2000-10."),),
)


def _answer(name: str = "aspirin", ndc: str = "0280-2000-10", ingredients: int = 1) -> str:
    ingredient = {
        "substance_name": name,
        "strength_value": 81,
        "strength_unit": "mg",
        "basis_value": 1,
        "basis_unit": "1",
    }
    product = {
        "name": "Bayer Low Dose",
        "dosage_form": "TABLET, CHEWABLE",
        "routes": ["ORAL"],
        "ingredients": [ingredient] * ingredients
        if ingredients == 1
        else [ingredient, dict(ingredient, substance_name="caffeine")],
        "packages": [{"ndc": ndc}],
    }
    return json.dumps({"products": [product]})


@pytest.fixture(scope="module")
def context(spec: SchemaSpec) -> GraphContextChecker:
    mapper = wiring.mapper(spec, V)
    ref = ReferenceGraph(spec, V, mapper)
    ref.add_substances(
        [SubstanceSource("R16CO5Y76E", "ASPIRIN", "spl"), SubstanceSource("3G6A5W338E", "CAFFEINE", "spl")]
    )
    mapped = OpenFdaMapper().ndc(NDC_RECORD)
    ref.add_candidates([*mapped, *nested_packages(mapped, NDC_RECORD), *label_links(NDC_RECORD)])
    shapes = Graph().parse(data=FILES[CONTEXT_SHACL_FILE], format="turtle")
    return GraphContextChecker(ref, SubstanceResolver(ref, V), NdcResolver(ref, V), shapes, mapper)


def _extractor(client: FakeClient, context: GraphContextChecker, spec: SchemaSpec) -> Extractor:
    prompts = load_prompts("v1", json.loads(FILES[EXTRACTION_SCHEMA_FILE]))
    pyd = PydanticValidator(date(2026, 11, 27))
    return Extractor(client, prompts, SONNET, SETTINGS, pyd, Instantiator(spec), context, MAX_REPAIR_ROUNDS)


def _run(answers: list[Any], arm: str, context: GraphContextChecker, spec: SchemaSpec) -> tuple[Any, FakeClient]:
    client = FakeClient(scripted(answers))
    return _extractor(client, context, spec).run(DOC, arm), client


def test_a0_scores_the_first_answer_as_is(context: GraphContextChecker, spec: SchemaSpec) -> None:
    result, client = _run([_answer(ndc="0280200010")], "A0", context, spec)
    assert len(client.requests) == 1
    assert result.status == "invalid"
    assert result.final_payload["products"][0]["packages"][0]["ndc"] == "0280200010"


def test_a1_repairs_from_pydantic_feedback(context: GraphContextChecker, spec: SchemaSpec) -> None:
    result, client = _run([_answer(ndc="0280200010"), _answer()], "A1", context, spec)
    assert result.status == "ok"
    assert [r.phase for r in result.rounds] == ["first", "pydantic"]
    feedback = client.requests[1].messages[-1].text
    assert "products[0].packages[0].ndc" in feedback
    assert client.requests[1].messages[1].role == "assistant"


def test_a1_stops_at_the_round_cap(context: GraphContextChecker, spec: SchemaSpec) -> None:
    result, client = _run([_answer(ndc="NDC 0280-2000-10")] * 4, "A1", context, spec)
    assert len(client.requests) == 1 + MAX_REPAIR_ROUNDS
    assert result.status == "invalid"


def test_a2_fixes_mechanical_issues_without_a_call(context: GraphContextChecker, spec: SchemaSpec) -> None:
    result, client = _run([_answer(ndc="NDC 0280-2000-10")] * 4, "A2", context, spec)
    assert len(client.requests) == 1 + MAX_REPAIR_ROUNDS  # A2 is A1 plus a step that never calls the model
    assert result.status == "ok"
    assert result.rounds[-1].fixes == [
        {"path": "products[0].packages[0].ndc", "kind": "ndc", "before": "NDC 0280-2000-10", "after": "0280-2000-10"}
    ]


def test_a3_repairs_from_graph_context(context: GraphContextChecker, spec: SchemaSpec) -> None:
    result, client = _run([_answer(name="asprin"), _answer()], "A3", context, spec)
    assert result.status == "ok"
    assert [r.phase for r in result.rounds] == ["first", "graph"]
    feedback = client.requests[1].messages[-1].text
    assert "products[0].ingredients[0]" in feedback
    assert "ASPIRIN" in feedback
    assert result.context_issues == []


def test_a3_reports_registry_mismatches(context: GraphContextChecker, spec: SchemaSpec) -> None:
    result, client = _run([_answer(ndc="0280-2000-99"), _answer(ingredients=2), _answer()], "A3", context, spec)
    kinds = [[i["source"] for i in r.issues] for r in result.rounds]
    assert kinds[0] == ["shacl"]
    assert "0280-2000-10" not in client.requests[1].messages[-1].text  # never the label's own NDCs under A3
    assert kinds[1] == ["extraction_only:ingredient-count-matches-product"]
    assert result.status == "ok"


def test_truncated_answers_are_recorded_not_repaired(context: GraphContextChecker, spec: SchemaSpec) -> None:
    result, client = _run([('{"products": [', "max_tokens")], "A3", context, spec)
    assert len(client.requests) == 1
    assert result.status == "truncated"


def test_every_extraction_only_declaration_has_one_implementation(spec: SchemaSpec) -> None:
    declared = {e.id for c in spec.classes for e in c.extraction_only}
    assert declared == set(EXTRACTION_ONLY_CHECKS)


def test_shared_rounds_are_identical_across_arms(context: GraphContextChecker, spec: SchemaSpec) -> None:
    keys = {}
    for arm in ("A0", "A1", "A2", "A3"):
        result, _ = _run([_answer()] * 4, arm, context, spec)
        keys[arm] = result.rounds[0].request_key
    assert len(set(keys.values())) == 1


def test_no_structured_value_reaches_any_prompt(context: GraphContextChecker, spec: SchemaSpec) -> None:
    from .test_h4_leak import SENTINEL, STRUCTURED_ONLY

    sentinel = parse_spl(SENTINEL.read_bytes())
    seen: list[LlmRequest] = []

    def script(request: LlmRequest) -> tuple[str, str]:
        seen.append(request)
        return _answer(), "end_turn"

    _extractor(FakeClient(script), context, spec).run(sentinel, "A3")
    sent = "".join(r.system + "".join(m.text for m in r.messages) for r in seen)
    assert [s for s in STRUCTURED_ONLY if s in sent] == []


@pytest.mark.parametrize(
    ("raw", "ndc"),
    [
        ("0280-2000-10", "0280-2000-10"),
        ("NDC 0280-2000-10", "0280-2000-10"),
        ("0280 2000 10", "0280-2000-10"),
        ("0280200010", None),
        ("12345-6789-012", None),
    ],
)
def test_normalize_ndc(raw: str, ndc: str | None) -> None:
    assert normalize_ndc(raw) == ndc


def test_deterministic_repair_touches_only_mechanical_fields() -> None:
    payload = {
        "products": [
            {
                "name": "  Bayer   Low Dose ",
                "ingredients": [{"strength_unit": "MG", "basis_unit": "1", "substance_name": "aspirin"}],
            }
        ]
    }
    repaired, fixes = repair(payload)
    assert repaired["products"][0]["name"] == "Bayer Low Dose"
    assert repaired["products"][0]["ingredients"][0]["strength_unit"] == "mg"
    assert {f.kind for f in fixes} == {"whitespace", "unit"}
    assert payload["products"][0]["name"] == "  Bayer   Low Dose "  # the input is not mutated


def test_model_round_trip_for_extraction(spec: SchemaSpec) -> None:
    data = json.loads(_answer())
    model = LabelExtraction.model_validate(
        Instantiator(spec).instantiate(LabelExtraction, {"set_id": SET_ID, "version": 1, **data})
    )
    assert model.products[0].ingredients[0].strength_unit == "mg"
    assert ROOT.exists()


def test_the_wired_extractor_runs_every_arm() -> None:
    """setup.extractor must wire the A3+ checker; a missing one only showed up in the dev run."""
    import inspect

    from monograph.extract import setup

    assert "ctx.checker_label" in inspect.getsource(setup.extractor)
