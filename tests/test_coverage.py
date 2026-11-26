from __future__ import annotations

from monograph import wiring
from monograph.core.regex import check_pattern
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.gen.ddl import DdlGenerator
from monograph.gen.shacl import SH
from monograph.harness.coverage import ENFORCED, NOT_ENFORCED, PARTIAL, CoverageMatrix

from .conftest import ROOT


def _matrix(spec: SchemaSpec) -> CoverageMatrix:
    return CoverageMatrix(spec, wiring.shapes_graph(ROOT), DdlGenerator(spec, check_pattern).generate(), Vocabulary())


def test_one_row_per_constraint_site(spec: SchemaSpec) -> None:
    rows = _matrix(spec).rows()
    assert len(rows) == sum(1 for _ in spec.constraint_sites())
    assert all(r.pydantic == ENFORCED for r in rows)


def test_known_cells(spec: SchemaSpec) -> None:
    cells = {(r.cls, r.field, r.kind): r for r in _matrix(spec).rows()}
    future = cells[("Label", "", "pydantic_only:label-effective-date-not-future")]
    assert (future.shacl, future.postgres) == (NOT_ENFORCED, NOT_ENFORCED)
    sections = cells[("Label", "", "pydantic_only:label-section-codes-unique")]
    assert (sections.shacl, sections.postgres) == (PARTIAL, ENFORCED)
    assert cells[("AdverseEventReport", "", "rule:has-suspect")].postgres == NOT_ENFORCED
    assert cells[("AdverseEventReport", "", "rule:serious-requires-criterion")].postgres == ENFORCED
    assert cells[("Label", "version", "datatype")].postgres == PARTIAL
    assert cells[("Reaction", "term", "charset")].shacl == ENFORCED


def test_shacl_column_reads_the_shapes_graph(spec: SchemaSpec) -> None:
    """Remove a constraint from the shapes and the matrix must notice."""
    shapes = wiring.shapes_graph(ROOT)
    shapes.remove((None, SH.pattern, None))
    matrix = CoverageMatrix(spec, shapes, DdlGenerator(spec, check_pattern).generate(), Vocabulary())
    patterns = [r for r in matrix.rows() if r.kind == "pattern"]
    assert patterns
    assert all(r.shacl == NOT_ENFORCED for r in patterns)
