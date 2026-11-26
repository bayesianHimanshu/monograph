"""Model -> RDF -> model is lossless, in memory and through Turtle text."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from rdflib import Graph

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.rdf.mapping import RdfMapper
from monograph.validate.fixtures import Fixture, FixtureLoader

from .conftest import ROOT, TODAY

_VALID = FixtureLoader(wiring.build_spec(), wiring.fixtures_dir(ROOT)).valid()
CONTEXT = {"today": TODAY}


def _valid_instance(instantiator: Instantiator, f: Fixture) -> BaseModel:
    return f.cls.model_validate(instantiator.instantiate(f.cls, f.record), context=CONTEXT)


@pytest.mark.parametrize("fixture", _VALID, ids=lambda f: f.name)
def test_round_trip_in_memory(fixture: Fixture, instantiator: Instantiator, mapper: RdfMapper) -> None:
    model = _valid_instance(instantiator, fixture)
    again = mapper.from_graph(mapper.to_graph(model), fixture.cls, mapper.iri_of(model), CONTEXT)
    assert again == model


@pytest.mark.parametrize("fixture", _VALID, ids=lambda f: f.name)
def test_round_trip_through_turtle(fixture: Fixture, instantiator: Instantiator, mapper: RdfMapper) -> None:
    model = _valid_instance(instantiator, fixture)
    text = mapper.to_graph(model).serialize(format="turtle")
    again = mapper.from_graph(Graph().parse(data=text, format="turtle"), fixture.cls, mapper.iri_of(model), CONTEXT)
    assert again == model


def test_no_blank_nodes_in_instance_graphs(instantiator: Instantiator, mapper: RdfMapper) -> None:
    from rdflib import BNode

    for f in _VALID:
        g = mapper.to_graph(_valid_instance(instantiator, f))
        assert not any(isinstance(t, BNode) for triple in g for t in triple)


def test_list_order_survives(instantiator: Instantiator, mapper: RdfMapper) -> None:
    [report] = [f for f in _VALID if f.id == "serious"]
    model = _valid_instance(instantiator, report)
    again = mapper.from_graph(mapper.to_graph(model), report.cls, mapper.iri_of(model), CONTEXT)
    assert [r.term for r in again.reactions] == ["Nausea", "Vomiting", "Nausea"]  # type: ignore[attr-defined]
