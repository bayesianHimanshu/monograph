"""The property graph projection, checked against the spec without using the generator's own rules."""

from __future__ import annotations

import re

from monograph.core.spec import Kind
from monograph.gen.age import AgeProjectionGenerator, Projection, collapses_to_edge, edge_label
from monograph.gen.artifacts import AGE_FILE, DDL_FILE
from monograph.gen.ddl import child_table, column_name, table_name
from monograph.wiring import build_spec, generated_dir, h2_excluded

from .conftest import ROOT


def _projection() -> Projection:
    return AgeProjectionGenerator(build_spec(), h2_excluded()).generate()


def _in_scope() -> list[tuple[str, object]]:
    """(table, ClassSpec) for every class the graph should cover, walking embedded fields by hand."""
    spec = build_spec()
    out: list[tuple[str, object]] = []

    def walk(table: str, c: object) -> None:
        out.append((table, c))
        for f in c.fields:  # type: ignore[attr-defined]
            if f.kind is Kind.EMBEDDED:
                walk(child_table(table, f), spec.of(f.target))

    for c in spec.entities():
        if c.cls not in h2_excluded():
            walk(table_name(c.cls), c)
    return out


def test_edge_label_names() -> None:
    assert edge_label("labelsProduct") == "LABELS_PRODUCT"
    assert edge_label("labeler") == "LABELER"


def test_every_reference_field_is_an_edge_with_the_right_endpoints() -> None:
    spec = build_spec()
    p = _projection()
    sources = {(e.label, s.table, s.to_column): s for e in p.edges for s in e.sources}
    expected = 0
    for table, c in _in_scope():
        for f in c.fields:  # type: ignore[attr-defined]
            if f.kind is not Kind.REF:
                continue
            expected += 1
            target = spec.of(f.target).meta.name
            if collapses_to_edge(c) is f:  # type: ignore[arg-type]
                continue  # checked below, as the owner's edge
            key = (edge_label(f.prop.name), child_table(table, f) if f.is_collection else table)
            match = [s for (label, t, _), s in sources.items() if (label, t) == key]
            assert len(match) == 1, f"{table}.{f.name}"
            assert match[0].to_label == target
    assert expected >= 8


def test_collapsed_embedded_values_carry_their_fields_on_the_edge() -> None:
    p = _projection()
    ingredient = p.edge("INGREDIENT").sources[0]
    assert (ingredient.from_label, ingredient.to_label) == ("Product", "Substance")
    assert [x.name for x in ingredient.properties] == ["strength_value", "strength_unit", "position"]
    assert not any(v.label == "Ingredient" for v in p.vertices)


def test_every_scalar_and_enum_field_is_a_property_somewhere() -> None:
    p = _projection()
    on_vertices = {(v.table, x.column) for v in p.vertices for x in v.properties}
    on_edges = {(s.table, x.column) for e in p.edges for s in e.sources for x in s.properties}
    for table, c in _in_scope():
        for f in c.fields:  # type: ignore[attr-defined]
            if f.kind in (Kind.SCALAR, Kind.ENUM):
                assert (table, column_name(f)) in on_vertices | on_edges, f"{table}.{f.name}"


def test_property_descriptions_equal_the_ddl_column_comments() -> None:
    ddl = (generated_dir(ROOT) / DDL_FILE).read_text(encoding="utf-8")
    comments = {
        (m.group(1), m.group(2)): m.group(3).replace("''", "'")
        for m in re.finditer(r"COMMENT ON COLUMN (\w+)\.(\w+) IS '((?:[^']|'')*)';", ddl)
    }
    p = _projection()
    checked = 0
    for v in p.vertices:
        for x in v.properties:
            assert comments[(v.table, x.column)].startswith(x.description), (v.table, x.column)
            checked += 1
    for e in p.edges:
        for s in e.sources:
            for x in s.properties:
                assert comments[(s.table, x.column)].startswith(x.description)
                checked += 1
    assert checked > 30


def test_committed_artifact_parses_back_to_the_generated_projection() -> None:
    text = (generated_dir(ROOT) / AGE_FILE).read_text(encoding="utf-8")
    assert Projection.parse(text) == _projection()


def test_extraction_classes_are_not_projected() -> None:
    labels = {v.label for v in _projection().vertices}
    assert "LabelExtraction" not in labels
    assert "ExtractedProduct" not in labels
