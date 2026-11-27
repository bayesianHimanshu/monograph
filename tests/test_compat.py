from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from rdflib import OWL, RDF, Graph, Literal, URIRef
from rdflib.collection import Collection

from monograph import wiring
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import OWL_FILE, SHACL_FILE
from monograph.gen.shacl import SH
from monograph.validate.compat import CompatChecker, SchemaSignature, diff

V = Vocabulary()
FILES = wiring.artifact_builder(wiring.build_spec(), V).build().files


def _graphs() -> tuple[Graph, Graph]:
    return Graph().parse(data=FILES[OWL_FILE], format="turtle"), Graph().parse(data=FILES[SHACL_FILE], format="turtle")


def _changes(edit: Callable[[Graph, Graph], None]) -> list[tuple[str, bool]]:
    owl, shacl = _graphs()
    before = SchemaSignature.from_graphs(owl, shacl)
    edit(owl, shacl)
    return [(c.detail, c.breaking) for c in diff(before, SchemaSignature.from_graphs(owl, shacl))]


def ps(name: str) -> URIRef:
    return URIRef(V.shapes_ns + name)


def test_identical_graphs_have_no_changes() -> None:
    assert _changes(lambda o, s: None) == []


def test_min_count_up_is_breaking_and_down_is_additive() -> None:
    def up(o: Graph, s: Graph) -> None:
        s.set((ps("PackagedProduct-packageDescription"), SH.minCount, Literal(1)))

    def down(o: Graph, s: Graph) -> None:
        s.remove((ps("AdverseEventReport-reaction"), SH.minCount, None))

    assert _changes(up) == [("minCount None -> 1", True)]
    assert _changes(down) == [("minCount 1 -> None", False)]


def test_pattern_change_is_breaking_even_when_looser() -> None:
    def loosen(o: Graph, s: Graph) -> None:
        s.set((ps("Substance-unii"), SH.pattern, Literal("^[0-9A-Z]+$")))

    [(detail, breaking)] = _changes(loosen)
    assert breaking
    assert "pattern" in detail


def test_enum_member_added_is_additive() -> None:
    def add_member(o: Graph, s: Graph) -> None:
        member = URIRef(V.ns + "Route-SYNTHETIC_TEST_MEMBER")  # must not exist in any real model version
        o.add((member, RDF.type, OWL.NamedIndividual))
        o.add((member, RDF.type, URIRef(V.ns + "Route")))
        head = s.value(ps("Product-route"), SH["in"])
        assert head is not None
        Collection(s, head).append(member)

    changes = _changes(add_member)
    assert changes
    assert not any(b for _, b in changes)


def test_class_removed_is_breaking() -> None:
    def drop(o: Graph, s: Graph) -> None:
        o.remove((URIRef(V.ns + "Recall"), None, None))

    assert ("class removed (or its IRI changed)", True) in _changes(drop)


def _variant(kind: str) -> tuple[str, str]:
    owl, shacl = _graphs()
    if kind == "breaking":
        shacl.set((ps("PackagedProduct-packageDescription"), SH.minCount, Literal(1)))
    elif kind == "additive":
        shacl.set((ps("Substance-name"), SH.maxLength, Literal(600)))
    return owl.serialize(format="turtle"), shacl.serialize(format="turtle")


def _release(directory: Path, version: str, kind: str) -> None:
    owl, shacl = _variant(kind)
    (directory / version).mkdir()
    (directory / version / OWL_FILE).write_text(owl)
    (directory / version / SHACL_FILE).write_text(shacl)


@pytest.mark.parametrize(
    ("releases", "current", "fresh", "problem"),
    [
        ([("1.0.0", "base")], "1.0.0", "base", None),
        ([("1.0.0", "base")], "1.1.0", "additive", "no snapshot"),
        ([("1.0.0", "base")], "2.0.0", "breaking", "no snapshot"),
        ([("1.0.0", "base")], "1.0.0", "additive", "no longer match the 1.0.0 snapshot"),
        ([("1.0.0", "base"), ("1.1.0", "additive")], "1.1.0", "additive", None),
        ([("1.0.0", "base"), ("1.1.0", "breaking")], "1.1.0", "breaking", "need a major version bump"),
        ([("1.0.0", "base"), ("2.0.0", "breaking")], "2.0.0", "breaking", None),
        ([("2.0.0", "base")], "1.0.0", "base", "older than the last release"),
        (
            [("1.0.0", "base"), ("1.1.0", "breaking"), ("2.0.0", "breaking")],
            "2.0.0",
            "breaking",
            "release 1.1.0 contains breaking changes",
        ),
    ],
)
def test_release_rules(
    tmp_path: Path, releases: list[tuple[str, str]], current: str, fresh: str, problem: str | None
) -> None:
    for version, kind in releases:
        _release(tmp_path, version, kind)
    owl, shacl = _variant(fresh)
    report = CompatChecker(tmp_path, OWL_FILE, SHACL_FILE).check(current, owl, shacl)
    if problem is None:
        assert report.ok, report.problem
    else:
        assert not report.ok
        assert report.problem is not None
        assert problem in report.problem


@pytest.mark.parametrize(
    "edit",
    [
        "unique-lang",
        "severity",
        "new-subject",
        "ignored-property-removed",
        "abstract-shape-constraint",
        "second-pattern",
        "ontology-import",
    ],
)
def test_unhandled_constructs_fail_closed(edit: str) -> None:
    """Regression: anything the classifier does not model must be breaking, never silently ignored."""

    def apply(o: Graph, s: Graph) -> None:
        if edit == "unique-lang":
            s.add((ps("Substance-name"), SH.uniqueLang, Literal(True)))
        elif edit == "severity":
            s.add((ps("Substance-unii"), SH.severity, SH.Warning))
        elif edit == "new-subject":
            s.add((ps("Unattached"), SH.path, URIRef(V.ns + "name")))
        elif edit == "ignored-property-removed":
            s.remove((ps("Substance"), SH.ignoredProperties, None))
        elif edit == "abstract-shape-constraint":
            s.add((ps("Entity"), SH.closed, Literal(True)))
        elif edit == "second-pattern":
            s.add((ps("Substance-unii"), SH.pattern, Literal("^R")))
        elif edit == "ontology-import":
            o.add((V.ontology, OWL.imports, URIRef("http://example.org/other")))

    changes = _changes(apply)
    assert changes, f"{edit} was not seen at all"
    assert any(breaking for _, breaking in changes), changes


def test_generated_artifacts_leave_no_unclassified_residual() -> None:
    owl, shacl = _graphs()
    assert SchemaSignature.from_graphs(owl, shacl).residual == set()


def test_committed_release_matches_current_schema() -> None:
    from .conftest import ROOT

    report = CompatChecker(ROOT / "generated" / "releases", OWL_FILE, SHACL_FILE).check(
        wiring.build_spec().version, FILES[OWL_FILE], FILES[SHACL_FILE]
    )
    assert report.ok
    assert report.release is not None  # a release exists; an additive bump may precede the next one


def test_added_negated_constraint_is_breaking() -> None:
    """Regression: the signature once ignored sh:not, so new control-character constraints were invisible."""
    from rdflib import BNode

    def negate(o: Graph, s: Graph) -> None:
        shape = BNode()
        s.add((shape, SH.pattern, Literal("[x]")))
        s.add((ps("Recall-recallReason"), SH["not"], shape))

    assert [b for _, b in _changes(negate)] == [True]


def test_constraints_and_rules_on_a_new_class_are_additive() -> None:
    def new_class(o: Graph, s: Graph) -> None:
        cls = URIRef(V.ns + "Brand")
        o.add((cls, RDF.type, OWL.Class))
        node = ps("Brand")
        s.add((node, RDF.type, SH.NodeShape))
        s.add((node, SH.targetClass, cls))
        s.add((node, SH.closed, Literal(True)))
        prop = ps("Brand-name")
        s.add((node, SH.property, prop))
        s.add((prop, SH.path, URIRef(V.ns + "brandName")))
        s.add((prop, SH.minCount, Literal(1)))
        rule = URIRef(V.shapes_ns + "rule-brand-named")
        s.add((rule, RDF.type, SH.NodeShape))
        s.add((rule, SH.targetClass, cls))
        s.add((rule, SH.message, Literal("m")))

    changes = _changes(new_class)
    assert changes
    assert not any(b for _, b in changes), changes
