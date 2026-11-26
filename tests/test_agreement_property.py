"""Property-based agreement: valid instances, and instances with exactly one constraint broken."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.meta import DEFAULT_BASE
from monograph.rdf.mapping import RdfMapper
from monograph.validate.agreement import AgreementChecker, Outcome
from monograph.validate.pydantic_side import PydanticValidator
from monograph.validate.strategies import Perturber, RecordStrategies, Site

from .conftest import TODAY

SPEC = wiring.build_spec()
INSTANTIATOR = Instantiator(SPEC)
PYDANTIC = PydanticValidator(TODAY)


def _is_valid(cls: type[BaseModel], record: dict[str, Any]) -> bool:
    return PYDANTIC.check(INSTANTIATOR.instantiate(cls, record)).valid


STRATEGIES = RecordStrategies(SPEC, DEFAULT_BASE, TODAY, _is_valid)
PERTURBER = Perturber(SPEC)
ENTITIES = [c.cls for c in SPEC.entities()]


@pytest.mark.parametrize("cls", ENTITIES, ids=lambda c: c.__name__)
def test_generated_valid_instances_agree(cls: type[BaseModel], checker: AgreementChecker) -> None:
    @given(STRATEGIES.valid_record(cls))
    def run(record: dict[str, Any]) -> None:
        result = checker.check(INSTANTIATOR.instantiate(cls, record))
        assert result.outcome is Outcome.AGREE_VALID, (record, result.shacl)

    run()


# SHACL components that count as the right reason for a perturbation kind. A nested site is reported by
# pySHACL as a sh:node failure on the parent, so Node is accepted for nested paths.
REASON = {
    "closed": "Closed",
    "required": "MinCount",
    "pattern": "Pattern",
    "min_length": "MinLength",
    "max_length": "MaxLength",
    "ge": "MinInclusive",
    "le": "MaxInclusive",
    "gt": "MinExclusive",
    "lt": "MaxExclusive",
    "enum": "In",
    "min_count": "MinCount",
    "max_count": "MaxCount",
    "rule:serious-requires-criterion": "Or",
    "rule:has-primary-suspect": "QualifiedMinCount",
}

SITES = [(c, site) for c in ENTITIES for site in PERTURBER.sites(c)]


@pytest.mark.parametrize(("cls", "site"), SITES, ids=lambda x: x.__name__ if isinstance(x, type) else x.label())
def test_single_constraint_mutations_agree(cls: type[BaseModel], site: Site, checker: AgreementChecker) -> None:
    @settings(max_examples=max(3, settings().max_examples // 8))
    @given(STRATEGIES.valid_record(cls), st.data())
    def run(record: dict[str, Any], data: st.DataObject) -> None:
        mutated = PERTURBER.apply(data.draw, record, site)
        assume(mutated is not None)
        assert mutated is not None
        result = checker.check(INSTANTIATOR.instantiate(cls, mutated))
        assert result.outcome is not Outcome.DISAGREE, (site.label(), mutated, result.pydantic, result.shacl)
        expected = REASON.get(site.kind)
        if expected is not None and not result.shacl.valid:
            nested = len(site.path) > 1
            kinds = {v.kind.removesuffix("ConstraintComponent") for v in result.shacl.violations}
            assert expected in kinds or (nested and "Node" in kinds), (site.label(), kinds)

    run()


def test_every_class_has_mutation_sites() -> None:
    for cls in ENTITIES:
        kinds = {s.kind for s in PERTURBER.sites(cls)}
        assert {"closed", "required", "wrong_type"} <= kinds


@pytest.mark.parametrize("cls", ENTITIES, ids=lambda c: c.__name__)
def test_generated_instances_round_trip(cls: type[BaseModel], mapper: RdfMapper) -> None:
    @given(STRATEGIES.valid_record(cls))
    def run(record: dict[str, Any]) -> None:
        model = cls.model_validate(INSTANTIATOR.instantiate(cls, record), context=PYDANTIC.context)
        again = mapper.from_graph(mapper.to_graph(model), cls, mapper.iri_of(model), PYDANTIC.context)
        assert again == model

    run()
