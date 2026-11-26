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


@pytest.mark.parametrize(
    ("release", "current", "breaking", "ok"),
    [
        ("0.1.0", "0.1.0", None, True),
        ("0.1.0", "0.1.0", False, False),
        ("0.1.0", "0.2.0", False, True),
        ("0.1.0", "0.2.0", True, False),
        ("0.1.0", "1.0.0", True, True),
        ("0.2.0", "0.1.0", None, False),
    ],
)
def test_version_rules(tmp_path: Path, release: str, current: str, breaking: bool | None, ok: bool) -> None:
    directory = tmp_path / release
    directory.mkdir()
    (directory / OWL_FILE).write_text(FILES[OWL_FILE])
    (directory / SHACL_FILE).write_text(FILES[SHACL_FILE])
    owl, shacl = _graphs()
    if breaking is True:
        shacl.set((ps("PackagedProduct-packageDescription"), SH.minCount, Literal(1)))
    elif breaking is False:
        shacl.add((ps("Substance-name"), SH.maxLength, Literal(400)))
        shacl.remove((ps("Substance-name"), SH.maxLength, Literal(400)))
        shacl.set((ps("Substance-name"), SH.maxLength, Literal(600)))
    report = CompatChecker(tmp_path, OWL_FILE, SHACL_FILE).check(
        current, owl.serialize(format="turtle"), shacl.serialize(format="turtle")
    )
    assert report.ok is ok, report


def test_committed_release_matches_current_schema() -> None:
    from .conftest import ROOT

    report = CompatChecker(ROOT / "generated" / "releases", OWL_FILE, SHACL_FILE).check(
        wiring.build_spec().version, FILES[OWL_FILE], FILES[SHACL_FILE]
    )
    assert report.ok
    assert report.release == "0.1.0"
