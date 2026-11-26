"""Minimal reproductions of pySHACL behaviour that H6 depends on or works around.

Used by tests/test_pyshacl_behaviour.py (as characterization tests) and by `monograph measure` (so the
report states what the installed pySHACL actually does).
"""

from __future__ import annotations

from dataclasses import dataclass

import pyshacl
from rdflib import RDF, Graph, Literal, URIRef

_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://ex/> .
ex:Rule a sh:NodeShape ; sh:targetClass ex:Report ;
  sh:property [ sh:path ex:drug ; sh:qualifiedMinCount 1 ;
                sh:qualifiedValueShape [ sh:property [ sh:path ex:role ; sh:hasValue ex:Primary ] ] ] .
ex:Code a sh:NodeShape ; sh:targetClass ex:Coded ; sh:property [ sh:path ex:code ; sh:pattern "^[A-Z]{3}$" ] .
"""
_REPORT = """
@prefix ex: <http://ex/> .
ex:r1 a ex:Report ; ex:drug ex:d1 . ex:d1 ex:role ex:Secondary .
"""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    id: str
    observed: bool  # True means the deviation from the SHACL spec is present in this pySHACL
    description: str


def _shapes() -> Graph:
    return Graph().parse(data=_SHAPES, format="turtle")


def focus_nodes_skip_qualified_shapes() -> ProbeResult:
    data = Graph().parse(data=_REPORT, format="turtle")
    r1 = URIRef("http://ex/r1")
    full = pyshacl.validate(data, shacl_graph=_shapes())[0]
    focused = pyshacl.validate(data, shacl_graph=_shapes(), focus_nodes=[r1])[0]
    return ProbeResult(
        "pyshacl-focus-nodes-skips-qualified-value-shape",
        bool(full is False and focused is True),
        "With focus_nodes (and no use_shapes), a sh:qualifiedMinCount violation on the focus node is not reported.",
    )


def dollar_matches_before_trailing_newline() -> ProbeResult:
    data = Graph()
    node = URIRef("http://ex/c")
    data.add((node, RDF.type, URIRef("http://ex/Coded")))
    data.add((node, URIRef("http://ex/code"), Literal("ABC\n")))
    conforms = pyshacl.validate(data, shacl_graph=_shapes())[0]
    return ProbeResult(
        "pyshacl-pattern-dollar-before-final-newline",
        bool(conforms is True),
        "sh:pattern is evaluated with Python re.search, so '$' also matches before a final newline "
        "(XPath, Rust regex and Postgres treat '$' as end of input).",
    )


def all_probes() -> list[ProbeResult]:
    return [focus_nodes_skip_qualified_shapes(), dollar_matches_before_trailing_newline()]
