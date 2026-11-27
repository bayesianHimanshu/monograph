"""A3 feedback never offers values taken from the label being extracted (its own SPL, its own DailyMed NDC
list, its own NDC directory records). A3+ does, which shows the test is sensitive."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.extract.arms import Extractor
from monograph.extract.config import MAX_REPAIR_ROUNDS, SETTINGS, SONNET
from monograph.extract.llm import FakeClient, scripted
from monograph.extract.prompts import load_prompts
from monograph.extract.registry import dailymed_ndcs, label_links, load_reference, ndc_query, nested_packages
from monograph.extract.repair.shacl_feedback import CandidatePolicy, GraphContextChecker
from monograph.extract.resolve import NdcResolver, ReferenceGraph, SubstanceResolver, SubstanceSource
from monograph.extract.sources.dailymed import SplCache
from monograph.gen.artifacts import CONTEXT_SHACL_FILE, EXTRACTION_SCHEMA_FILE
from monograph.ingest.openfda import OpenFdaMapper, RawCache
from monograph.validate.pydantic_side import PydanticValidator

from .test_h4_arms import DOC, FILES, _answer
from .test_h4_context import NDC_RECORD, SET_ID

V = Vocabulary()
OWN_SENTINEL_NAME = "ASPRINOLUM"  # only this label's own SPL declares it
OWN_SENTINEL_NDC = "0280-2000-77"  # only this label's own DailyMed NDC list has it
OTHER_LABEL_NDC = "0280-3000-10"  # same labeler, another label: an independent source
OWN_GOLD_NDCS = ("0280-2000-10", "0280-2000-11")  # this label's own NDC directory record


def _reference(spec: SchemaSpec) -> ReferenceGraph:
    ref = ReferenceGraph(spec, V, wiring.mapper(spec, V))
    ref.add_substances(
        [
            SubstanceSource("R16CO5Y76E", "ASPIRIN", "dailymed-uniis", None),
            SubstanceSource("SENT1NEL01", OWN_SENTINEL_NAME, "spl", SET_ID),
        ]
    )
    mapped = OpenFdaMapper().ndc(NDC_RECORD)
    ref.add_candidates([*mapped, *nested_packages(mapped, NDC_RECORD), *label_links(NDC_RECORD)])
    for ndc in OWN_GOLD_NDCS:
        ref.link_ndc(ndc, SET_ID)
    own_index = {"data": {"ndcs": [{"ndc": OWN_SENTINEL_NDC}]}}
    ref.add_candidates(list(dailymed_ndcs(SET_ID, own_index)))
    ref.link_ndc(OWN_SENTINEL_NDC, SET_ID)
    other = dict(
        NDC_RECORD,
        product_ndc="0280-3000",
        product_id="0280-3000_y",
        packaging=[{"package_ndc": OTHER_LABEL_NDC, "description": "1 BOTTLE"}],
        openfda={"spl_set_id": ["other-label"]},
    )
    ref.add_candidates(OpenFdaMapper().ndc(other))
    ref.link_ndc(OTHER_LABEL_NDC, "other-label")
    return ref


def _feedback(spec: SchemaSpec, policy: CandidatePolicy, answers: list[Any]) -> str:
    ref = _reference(spec)
    mapper = wiring.mapper(spec, V)
    shapes = Graph().parse(data=FILES[CONTEXT_SHACL_FILE], format="turtle")
    substances, ndcs = SubstanceResolver(ref, V), NdcResolver(ref, V)
    independent = GraphContextChecker(ref, substances, ndcs, shapes, mapper, CandidatePolicy.INDEPENDENT)
    label = GraphContextChecker(ref, substances, ndcs, shapes, mapper, CandidatePolicy.LABEL)
    client = FakeClient(scripted(answers))
    prompts = load_prompts("v1", json.loads(FILES[EXTRACTION_SCHEMA_FILE]))
    extractor = Extractor(
        client,
        prompts,
        SONNET,
        SETTINGS,
        PydanticValidator(date(2026, 11, 27)),
        Instantiator(spec),
        independent,
        MAX_REPAIR_ROUNDS,
        label,
    )
    extractor.run(DOC, "A3" if policy is CandidatePolicy.INDEPENDENT else "A3+")
    # Only the feedback: the last user message of every request after the first (the label text itself
    # legitimately prints the label's own NDCs).
    return "\n".join(r.messages[-1].text for r in client.requests[1:])


WRONG = [_answer(name="asprinol", ndc="0280-2000-99"), _answer(name="asprinol", ndc="0280-2000-99"), _answer()]


def test_a3_feedback_offers_nothing_from_the_label_itself(spec: SchemaSpec) -> None:
    feedback = _feedback(spec, CandidatePolicy.INDEPENDENT, WRONG)
    assert feedback, "the wrong answer must have produced feedback"
    for planted in (OWN_SENTINEL_NAME, OWN_SENTINEL_NDC, *OWN_GOLD_NDCS):
        assert planted not in feedback
    assert "ASPIRIN" in feedback  # an independent candidate is still offered
    assert OTHER_LABEL_NDC in feedback


def test_a3_plus_offers_label_derived_candidates(spec: SchemaSpec) -> None:
    feedback = _feedback(spec, CandidatePolicy.LABEL, WRONG)
    assert OWN_SENTINEL_NAME in feedback
    assert "0280-2000-10" in feedback


def test_ingredient_count_message_states_no_registry_count_under_a3(spec: SchemaSpec) -> None:
    answers = [_answer(ingredients=2), _answer()]
    assert "lists 1 active ingredient" not in _feedback(spec, CandidatePolicy.INDEPENDENT, answers)
    assert "lists 1 active ingredient" in _feedback(spec, CandidatePolicy.LABEL, answers)


def test_load_reference_links_every_source_to_its_label(tmp_path: Path, spec: SchemaSpec) -> None:
    raw, spl = RawCache(tmp_path / "raw"), SplCache(tmp_path / "spl")
    raw.write("ndc", 0, 100, [NDC_RECORD], search=ndc_query(SET_ID))
    raw.write("label", 0, 1, [], search=f'set_id:"{SET_ID}"')
    listing = spl.listing_path(f"ndcs-{SET_ID}", 100, 1)
    listing.parent.mkdir(parents=True)
    listing.write_text(json.dumps({"data": {"ndcs": [{"ndc": OWN_SENTINEL_NDC}]}}))
    ref = load_reference(spec, V, wiring.mapper(spec, V), raw, spl, [SET_ID], [], {})
    assert ref.ndc_links["0280-2000-10"] == {SET_ID}
    assert ref.ndc_links["0280-2000-11"] == {SET_ID}  # nested package, same record
    assert ref.ndc_links[OWN_SENTINEL_NDC] == {SET_ID}
    assert ref.independent_ndcs(SET_ID, "0280-2000-99") == ()


@pytest.mark.parametrize("arm", ["A0", "A1", "A2", "A3", "A3+"])
def test_every_arm_resolves_with_the_same_code(arm: str, spec: SchemaSpec) -> None:
    """Resolution, the label-context tie-break included, is one function used by every arm and the scorer."""
    import inspect

    from monograph.extract import resolve, score
    from monograph.extract.repair import shacl_feedback

    assert "for_label" in inspect.getsource(resolve.resolve_extraction)
    assert "for_label" in inspect.getsource(score.LabelScorer.score)
    assert "resolve_extraction" in inspect.getsource(shacl_feedback.GraphContextChecker.check)
    assert arm
