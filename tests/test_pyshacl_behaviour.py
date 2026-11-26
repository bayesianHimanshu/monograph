"""Characterization tests for pySHACL behaviour that H6 depends on or works around.

If one of these starts failing after a pySHACL upgrade, the behaviour changed: update
`validate/shacl_side.py` and the findings in docs/h6/RESULTS.md.
"""

from __future__ import annotations

import pyshacl
from rdflib import Graph, Literal, URIRef

SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://ex/> .
ex:Rule a sh:NodeShape ; sh:targetClass ex:Report ;
  sh:property [ sh:path ex:drug ; sh:qualifiedMinCount 1 ;
                sh:qualifiedValueShape [ sh:property [ sh:path ex:role ; sh:hasValue ex:Primary ] ] ] .
ex:Code a sh:NodeShape ; sh:targetClass ex:Coded ; sh:property [ sh:path ex:code ; sh:pattern "^[A-Z]{3}$" ] .
"""
DATA = """
@prefix ex: <http://ex/> .
ex:r1 a ex:Report ; ex:drug ex:d1 . ex:d1 ex:role ex:Secondary .
"""


def _shapes() -> Graph:
    return Graph().parse(data=SHAPES, format="turtle")


def test_focus_nodes_mode_skips_qualified_value_shapes() -> None:
    data = Graph().parse(data=DATA, format="turtle")
    r1 = URIRef("http://ex/r1")
    assert pyshacl.validate(data, shacl_graph=_shapes())[0] is False
    assert pyshacl.validate(data, shacl_graph=_shapes(), focus_nodes=[r1])[0] is True  # the bug
    assert (
        pyshacl.validate(data, shacl_graph=_shapes(), focus_nodes=[r1], use_shapes=[URIRef("http://ex/Rule")])[0]
        is False
    )


def test_pattern_dollar_matches_before_trailing_newline() -> None:
    """XPath says $ is end of input. pySHACL uses Python re.search, where $ also matches before a final newline."""
    data = Graph()
    node = URIRef("http://ex/c")
    data.add((node, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef("http://ex/Coded")))
    data.add((node, URIRef("http://ex/code"), Literal("ABC\n")))
    assert pyshacl.validate(data, shacl_graph=_shapes())[0] is True
