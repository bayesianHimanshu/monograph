"""SchemaSpec -> property graph projection for Apache AGE.

* one vertex label per entity class (the ontology class name), with the scalar and enum fields as
  properties plus `iri`;
* one edge label per object property, named after the ontology property (`labelsProduct` becomes
  `LABELS_PRODUCT`), so one edge label always has one range (`core/spec.py` checks that every use of a
  property name agrees);
* an embedded value whose only reference is required, and which has no embedded or collection-of-
  reference fields, becomes an edge with properties (Ingredient: Product -INGREDIENT-> Substance with the
  strength on the edge). Any other embedded value becomes a vertex, with an edge from its owner.

Every vertex and edge source names the relational table and columns it is loaded from, so
`graph/loader.py` builds the graph from the generated Postgres tables and nothing else. Property and
edge descriptions are the field descriptions, the same text the DDL carries as column comments.

The rendered artifact is JSON, drift-checked like the other generated files.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from monograph.core.spec import Card, ClassSpec, FieldSpec, Kind, SchemaError, SchemaSpec
from monograph.gen.ddl import child_table, column_name, table_name

GRAPH_NAME = "drug"

PROPERTY_TYPE: dict[type, str] = {
    str: "string",
    int: "integer",
    Decimal: "number",
    bool: "boolean",
    date: "string (ISO date, YYYY-MM-DD)",
    datetime: "string (ISO timestamp)",
}


def edge_label(prop_name: str) -> str:
    """`labelsProduct` -> `LABELS_PRODUCT`."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", prop_name).upper()


@dataclass(frozen=True, slots=True)
class Property:
    name: str
    type: str
    description: str
    column: str


@dataclass(frozen=True, slots=True)
class VertexLabel:
    label: str
    description: str
    table: str
    properties: tuple[Property, ...]


@dataclass(frozen=True, slots=True)
class EdgeSource:
    """One way an edge label arises: rows of `table`, from the vertex whose iri is in `from_column` to the
    vertex whose iri is in `to_column`."""

    from_label: str
    to_label: str
    description: str
    table: str
    from_column: str
    to_column: str
    properties: tuple[Property, ...] = ()


@dataclass(frozen=True, slots=True)
class EdgeLabel:
    label: str
    sources: tuple[EdgeSource, ...]


@dataclass(frozen=True, slots=True)
class Projection:
    version: str
    graph: str
    vertices: tuple[VertexLabel, ...]
    edges: tuple[EdgeLabel, ...]

    def vertex(self, label: str) -> VertexLabel:
        return next(v for v in self.vertices if v.label == label)

    def edge(self, label: str) -> EdgeLabel:
        return next(e for e in self.edges if e.label == label)

    def render(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def parse(text: str) -> Projection:
        data = json.loads(text)
        return Projection(
            data["version"],
            data["graph"],
            tuple(
                VertexLabel(v["label"], v["description"], v["table"], tuple(Property(**p) for p in v["properties"]))
                for v in data["vertices"]
            ),
            tuple(
                EdgeLabel(
                    e["label"],
                    tuple(
                        EdgeSource(**{**s, "properties": tuple(Property(**p) for p in s["properties"])})
                        for s in e["sources"]
                    ),
                )
                for e in data["edges"]
            ),
        )


@dataclass
class _Builder:
    vertices: list[VertexLabel] = field(default_factory=list)
    edges: dict[str, list[EdgeSource]] = field(default_factory=dict)

    def edge(self, label: str, source: EdgeSource) -> None:
        self.edges.setdefault(label, []).append(source)


def _describe(f: FieldSpec) -> str:
    return f.description or f.prop.label


def collapses_to_edge(c: ClassSpec) -> FieldSpec | None:
    """The single required reference of an embedded class that becomes an edge, or None."""
    refs = [f for f in c.fields if f.kind is Kind.REF]
    if len(refs) != 1 or refs[0].card is not Card.ONE:
        return None
    if any(f.kind is Kind.EMBEDDED for f in c.fields):
        return None
    return refs[0]


class AgeProjectionGenerator:
    def __init__(self, spec: SchemaSpec, exclude: tuple[type[BaseModel], ...] = ()) -> None:
        self._spec = spec
        self._exclude = exclude

    def render(self) -> str:
        return self.generate().render()

    def generate(self) -> Projection:
        b = _Builder()
        for c in self._spec.entities():
            if c.cls in self._exclude:
                continue
            table = table_name(c.cls)
            self._vertex(b, c, c.meta.name, table, c.doc)
        edges = tuple(EdgeLabel(label, tuple(sources)) for label, sources in sorted(b.edges.items()))
        return Projection(self._spec.version, GRAPH_NAME, tuple(b.vertices), edges)

    def _properties(self, c: ClassSpec) -> tuple[Property, ...]:
        out = [Property("iri", "string", "Canonical identifier (IRI).", "iri")]
        for f in c.fields:
            if f.kind is Kind.SCALAR:
                assert f.scalar is not None
                out.append(Property(f.name, PROPERTY_TYPE[f.scalar], _describe(f), column_name(f)))
            elif f.kind is Kind.ENUM:
                kind = "list of strings" if f.is_collection else "string"
                assert f.enum is not None
                values = ", ".join(m.value for m in f.enum)
                text = f"{_describe(f)} One of: {values}." if len(f.enum) <= 12 else _describe(f)
                out.append(Property(f.name, kind, text, column_name(f)))
        return tuple(out)

    def _vertex(
        self, b: _Builder, c: ClassSpec, label: str, table: str, description: str, extra: tuple[Property, ...] = ()
    ) -> None:
        b.vertices.append(VertexLabel(label, description, table, self._properties(c) + extra))
        for f in c.fields:
            if f.kind is Kind.REF:
                assert f.target is not None
                target = self._spec.of(f.target).meta.name
                if f.is_collection:
                    source = EdgeSource(label, target, _describe(f), child_table(table, f), "owner_iri", "target_iri")
                else:
                    source = EdgeSource(label, target, _describe(f), table, "iri", column_name(f))
                b.edge(edge_label(f.prop.name), source)
            elif f.kind is Kind.EMBEDDED:
                assert f.target is not None
                self._embedded(b, label, table, f, self._spec.of(f.target))

    def _embedded(self, b: _Builder, owner_label: str, owner_table: str, f: FieldSpec, target: ClassSpec) -> None:
        child = child_table(owner_table, f)
        ref = collapses_to_edge(target)
        if ref is not None:
            assert ref.target is not None
            props = [p for p in self._properties(target) if p.name != "iri"]
            if f.card is Card.LIST:
                props.append(Property("position", "integer", "Position in the owner's list, from 0.", "position"))
            to_label = self._spec.of(ref.target).meta.name
            b.edge(
                edge_label(f.prop.name),
                EdgeSource(owner_label, to_label, _describe(f), child, "owner_iri", column_name(ref), tuple(props)),
            )
            return
        label = target.meta.name
        if any(v.label == label for v in b.vertices):
            raise SchemaError(f"{label}: embedded in two places; the projection needs one vertex label per class")
        position = (Property("position", "integer", "Position in the owner's list, from 0.", "position"),)
        self._vertex(b, target, label, child, target.doc, position if f.card is Card.LIST else ())
        b.edge(edge_label(f.prop.name), EdgeSource(owner_label, label, _describe(f), child, "owner_iri", "iri"))
