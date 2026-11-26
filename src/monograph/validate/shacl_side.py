"""The SHACL verdict on an unvalidated instance, via the RDF mapping and pySHACL."""

from __future__ import annotations

from collections.abc import Callable

import pyshacl
from pydantic import BaseModel
from rdflib import RDF, Graph, URIRef

from monograph.gen.shacl import SH
from monograph.rdf.mapping import RdfMapper
from monograph.validate.verdict import Verdict, Violation


class ShaclValidator:
    def __init__(self, shapes: Graph, mapper: RdfMapper) -> None:
        self._shapes = shapes
        self._mapper = mapper

    def check(self, instance: BaseModel) -> Verdict:
        """Validate one record.

        The graph holds the record, its embedded nodes, and a typed stub for each reference. Stubs are
        targeted by their class's node shape like any typed node, and they fail it because they carry
        no data. Results whose focus node is not the record or one of its embedded nodes are about
        stubs and are dropped. (pySHACL's `focus_nodes` option would avoid that filter, but in 0.40.2 it
        silently skips sh:qualifiedValueShape; see tests/test_pyshacl_behaviour.py.)
        """
        record = str(self._mapper.iri_of(instance))
        graph = self._mapper.to_graph(instance, stubs=True)
        return self.validate(graph, keep=lambda focus: focus == record or focus.startswith(record + "/"))

    def validate(self, graph: Graph, keep: Callable[[str], bool] | None = None) -> Verdict:
        _, report, _ = pyshacl.validate(
            graph, shacl_graph=self._shapes, inference="none", advanced=False, allow_warnings=False
        )
        assert isinstance(report, Graph)
        violations = [v for focus, v in _violations(report) if keep is None or keep(focus)]
        return Verdict(not violations, tuple(sorted(violations, key=lambda v: (v.path, v.kind, v.message))))


def _violations(report: Graph) -> list[tuple[str, Violation]]:
    out = []
    for result in report.subjects(RDF.type, SH.ValidationResult):
        focus = report.value(result, SH.focusNode)
        path = report.value(result, SH.resultPath)
        component = report.value(result, SH.sourceConstraintComponent)
        shape = report.value(result, SH.sourceShape)
        message = report.value(result, SH.resultMessage)
        kind = str(component).rsplit("#", 1)[-1] if component is not None else "unknown"
        where = f"{focus} {path}" if path is not None else str(focus)
        detail = f"{shape}: {message}" if isinstance(shape, URIRef) else str(message)
        out.append((str(focus), Violation(where, kind, detail)))
    return out
