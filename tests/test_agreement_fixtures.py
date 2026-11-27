"""Pydantic and SHACL agree on every curated fixture, and SHACL rejects for the expected reason."""

from __future__ import annotations

from datetime import date

import pytest
from rdflib import Graph

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.rdf.mapping import RdfMapper
from monograph.validate.agreement import AgreementChecker, Outcome
from monograph.validate.fixtures import FixtureLoader
from monograph.validate.shacl_side import ShaclValidator

from .conftest import ROOT, TODAY

_LOADER = FixtureLoader(wiring.build_spec(), wiring.fixtures_dir(ROOT))

# SHACL constraint components that count as "the right reason" for each fixture kind.
EXPECTED_COMPONENT = {
    "required": {"MinCountConstraintComponent"},
    "min_count": {"MinCountConstraintComponent"},
    "max_count": {"MaxCountConstraintComponent"},
    "datatype": {"DatatypeConstraintComponent"},
    "pattern": {"PatternConstraintComponent"},
    "min_length": {"MinLengthConstraintComponent"},
    "max_length": {"MaxLengthConstraintComponent"},
    "min_inclusive": {"MinInclusiveConstraintComponent"},
    "max_inclusive": {"MaxInclusiveConstraintComponent"},
    "min_exclusive": {"MinExclusiveConstraintComponent"},
    "max_exclusive": {"MaxExclusiveConstraintComponent"},
    "enum": {"InConstraintComponent"},
    "ref": {"PatternConstraintComponent", "NodeKindConstraintComponent"},
    "node": {"NodeConstraintComponent", "NodeKindConstraintComponent", "ClassConstraintComponent"},
    "closed": {"ClosedConstraintComponent"},
    "rule:serious-requires-criterion": {"OrConstraintComponent"},
    "rule:has-suspect": {"QualifiedMinCountConstraintComponent"},
    "rule:listed-package-resolves": {"NodeConstraintComponent", "OrConstraintComponent"},
    "charset": {"NotConstraintComponent"},
    "pydantic_only:label-section-codes-unique": {"MaxCountConstraintComponent"},
}


@pytest.mark.parametrize("fixture", _LOADER.valid(), ids=lambda f: f.name)
def test_valid_fixture_agrees(fixture: object, checker: AgreementChecker, instantiator: Instantiator) -> None:
    from monograph.validate.fixtures import Fixture

    assert isinstance(fixture, Fixture)
    result = checker.check(instantiator.instantiate(fixture.cls, fixture.record))
    assert result.outcome is Outcome.AGREE_VALID, (result.pydantic, result.shacl)


@pytest.mark.parametrize("fixture", _LOADER.invalid(), ids=lambda f: f.name)
def test_invalid_fixture_agrees(fixture: object, checker: AgreementChecker, instantiator: Instantiator) -> None:
    from monograph.validate.fixtures import Fixture

    assert isinstance(fixture, Fixture)
    result = checker.check(instantiator.instantiate(fixture.cls, fixture.record))
    assert result.outcome.value == fixture.expect, (result.pydantic, result.shacl)
    assert fixture.violates is not None
    expected = EXPECTED_COMPONENT.get(fixture.violates)
    if expected is not None:
        kinds = {v.kind for v in result.shacl.violations}
        assert kinds & expected, f"SHACL rejected for {kinds}, expected one of {expected}"


@pytest.mark.parametrize("fixture", _LOADER.probes(), ids=lambda f: f.name)
def test_dialect_probes_behave_as_documented(
    fixture: object, checker: AgreementChecker, instantiator: Instantiator
) -> None:
    """Characterization test. If pySHACL changes its regex engine, this fails and RESULTS.md must change."""
    from monograph.validate.fixtures import Fixture

    assert isinstance(fixture, Fixture)
    result = checker.check(instantiator.instantiate(fixture.cls, fixture.record))
    assert result.outcome.value == fixture.expect


def test_every_constraint_kind_and_rule_has_an_invalid_fixture(spec: SchemaSpec, fixtures: FixtureLoader) -> None:
    covered = {f.violates for f in fixtures.invalid()}
    # extraction_only checks need the reference graph by definition, so no single record can violate
    # them; tests/test_h4_context.py covers each of them against a graph.
    needed = {s.kind for s in spec.constraint_sites() if not s.kind.startswith("extraction_only:")}
    assert needed - covered == set()


def test_valid_dataset_is_referentially_complete(
    spec: SchemaSpec, fixtures: FixtureLoader, instantiator: Instantiator, mapper: RdfMapper
) -> None:
    """Without stubs, sh:class needs the referents in the graph. The merged valid fixtures provide them."""
    graph = Graph()
    for f in fixtures.valid():
        graph += mapper.to_graph(instantiator.instantiate(f.cls, f.record))
    verdict = ShaclValidator(wiring.shapes_graph(ROOT), mapper).validate(graph)
    assert verdict.valid, verdict.violations


def test_dangling_reference_is_caught_only_at_dataset_level(
    fixtures: FixtureLoader, instantiator: Instantiator, mapper: RdfMapper, checker: AgreementChecker
) -> None:
    [recall] = [f for f in fixtures.valid() if f.id == "class-i"]
    record = dict(recall.record, recalling_firm="https://example.org/monograph/organization/nobody")
    instance = instantiator.instantiate(recall.cls, record)
    assert checker.check(instance).outcome is Outcome.AGREE_VALID  # per record, a dangling ref is fine
    dataset = ShaclValidator(wiring.shapes_graph(ROOT), mapper).validate(mapper.to_graph(instance))
    assert not dataset.valid
    assert {v.kind for v in dataset.violations} == {"ClassConstraintComponent"}


def test_today_is_injected() -> None:
    assert date(2026, 11, 26) == TODAY
