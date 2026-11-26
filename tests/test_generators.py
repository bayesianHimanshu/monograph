from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import Annotated, ClassVar

import pytest
from pydantic import Field
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef

from monograph import wiring
from monograph.core.meta import OntClass, OntProp, SafeIdentifier
from monograph.core.models import Entity
from monograph.core.regex import RegexSubsetError
from monograph.core.spec import SchemaSpec, analyze
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import JSON_SCHEMA_FILE, OWL_FILE, SHACL_FILE
from monograph.gen.shacl import SH
from monograph.gen.turtle import TurtleError, TurtleWriter

V = Vocabulary()


@pytest.fixture(scope="module")
def files(spec: SchemaSpec) -> dict[str, str]:
    return wiring.artifact_builder(spec, V).build().files


def _graph(text: str) -> Graph:
    return Graph().parse(data=text, format="turtle")


def test_owl_has_every_class_with_comment_and_parent(spec: SchemaSpec, files: dict[str, str]) -> None:
    g = _graph(files[OWL_FILE])
    for c in spec.classes:
        iri = V.cls(c.cls)
        assert (iri, RDF.type, OWL.Class) in g
        assert g.value(iri, RDFS.comment) is not None
        if c.parent is not None:
            assert (iri, RDFS.subClassOf, V.cls(c.parent)) in g
    unii = URIRef(V.ns + "unii")
    assert (unii, RDF.type, OWL.DatatypeProperty) in g
    assert (unii, RDF.type, OWL.FunctionalProperty) in g
    route = URIRef(V.ns + "route")
    assert (route, RDF.type, OWL.ObjectProperty) in g
    assert (route, RDF.type, OWL.FunctionalProperty) not in g
    assert (URIRef(V.ns + "Route-ORAL"), RDF.type, OWL.NamedIndividual) in g


def test_shacl_shapes_are_closed_and_targeted(spec: SchemaSpec, files: dict[str, str]) -> None:
    g = _graph(files[SHACL_FILE])
    for c in spec.concrete():
        shape = V.node_shape(c.cls)
        assert (shape, SH.targetClass, V.cls(c.cls)) in g
        assert (shape, SH.closed, Literal(True)) in g
        assert len(list(g.objects(shape, SH.property))) == len(c.fields)
    unii = URIRef(V.shapes_ns + "Substance-unii")
    assert g.value(unii, SH.pattern) == Literal(r"^[0-9A-Z]{10}$")
    assert g.value(unii, SH.minCount) == Literal(1)
    ingredient = URIRef(V.shapes_ns + "Product-ingredient")
    assert g.value(ingredient, SH.node) == V.node_shape(spec.by_name("Ingredient").cls)
    assert (URIRef(V.shapes_ns + "rule-has-suspect"), RDF.type, SH.NodeShape) in g


def test_json_schema_has_rules(files: dict[str, str]) -> None:
    import json

    doc = json.loads(files[JSON_SCHEMA_FILE])
    aer = doc["$defs"]["AdverseEventReport"]
    assert len(aer["allOf"]) == 2
    assert aer["x-rdf-class"] == V.ns + "AdverseEventReport"


def test_generation_is_byte_identical_across_processes() -> None:
    code = (
        "import hashlib; from monograph import wiring; from monograph.core.vocab import Vocabulary;"
        "f = wiring.artifact_builder(wiring.build_spec(), Vocabulary()).build().files;"
        "print(hashlib.sha256(repr(sorted(f.items())).encode()).hexdigest())"
    )
    digests = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        digests.add(out.stdout.strip())
    assert len(digests) == 1
    assert hashlib.sha256(b"x").hexdigest()


def test_pattern_outside_subset_fails_generation() -> None:
    class Bad(Entity):
        """Bad."""

        __ont__: ClassVar[OntClass] = OntClass(name="Bad", label="bad", path="bad", key=("code",))
        code: Annotated[SafeIdentifier, OntProp("code", "code"), Field(pattern=r"^\d+$")]

    spec = analyze((Entity, Bad), "0.0.0")
    with pytest.raises(RegexSubsetError, match="canonical"):
        wiring.artifact_builder(spec, V).build()


def test_turtle_writer_rejects_shared_blank_nodes() -> None:
    g = Graph()
    b = BNode()
    g.add((URIRef("http://x/a"), RDFS.seeAlso, b))
    g.add((URIRef("http://x/c"), RDFS.seeAlso, b))
    g.add((b, RDFS.label, Literal("shared")))
    with pytest.raises(TurtleError):
        TurtleWriter([]).write(g)


def test_turtle_writer_round_trips_through_rdflib(files: dict[str, str]) -> None:
    from rdflib.compare import isomorphic

    g = _graph(files[SHACL_FILE])
    again = _graph(wiring.turtle_writer(V).write(g))
    assert isomorphic(g, again)


def test_ddl_lists_what_postgres_cannot_enforce(spec: SchemaSpec) -> None:
    from monograph.core.regex import check_pattern
    from monograph.gen.ddl import DdlGenerator

    result = DdlGenerator(spec, check_pattern).generate()
    kinds = {(u.cls, u.kind) for u in result.unenforced}
    assert ("AdverseEventReport", "rule:has-suspect") in kinds
    assert ("AdverseEventReport", "min_count") in kinds
    assert ("Label", "pydantic_only:label-effective-date-not-future") in kinds
    assert "ck_adverse_event_report_rule_serious_requires_criterion" in result.sql
    assert "ck_product_routes_min_count" in result.sql
    assert ("Product", "pydantic_only:product-ingredient-substances-unique") not in kinds  # projected as UNIQUE
    assert "uq_product__ingredients_substance UNIQUE (owner_iri, substance_iri)" in result.sql
    assert "DEFERRABLE" not in result.sql


def test_test_profile_ddl_has_deferrable_foreign_keys(spec: SchemaSpec) -> None:
    from monograph.core.regex import check_pattern
    from monograph.gen.ddl import DdlGenerator, DdlProfile

    default = DdlGenerator(spec, check_pattern, DdlProfile.DEFAULT).render()
    test = DdlGenerator(spec, check_pattern, DdlProfile.TEST).render()
    assert test.count("DEFERRABLE INITIALLY IMMEDIATE") == test.count("REFERENCES")
    assert default.replace("default profile", "test profile") == test.replace(" DEFERRABLE INITIALLY IMMEDIATE", "")


def test_bounded_patterns_imply_max_length_and_strings_forbid_control_characters(files: dict[str, str]) -> None:
    g = _graph(files[SHACL_FILE])
    assert g.value(URIRef(V.shapes_ns + "Substance-unii"), SH.maxLength) == Literal(10)
    assert g.value(URIRef(V.shapes_ns + "Organization-slug"), SH.maxLength) == Literal(120)  # explicit max_length
    for name in ("Substance-unii", "Substance-name", "PackagedProduct-labeler"):
        negated = g.value(URIRef(V.shapes_ns + name), SH["not"])
        assert negated is not None
        assert "\x00" in str(g.value(negated, SH.pattern))
