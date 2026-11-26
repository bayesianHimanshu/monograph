"""Model instance <-> RDF graph, driven by the same SchemaSpec as the generators.

`to_graph` maps whatever an instance holds, valid or not, so SHACL sees exactly what Pydantic sees.
Wrong Python types become literals of their own datatype (or `mono:UnmappedValue` when there is no
faithful RDF form), and unknown keys become triples on a vocabulary-namespace property, which the
closed shapes then reject.

IRIs:
* entity:                     <base><path>/<key>[/<key2>]   (key values percent-encoded)
* single embedded value:      <parent>/<property>
* keyed embedded value (set): <parent>/<property>/<key>
* list item:                  <parent>/<property>/<index>   (the index restores list order)

For per-record validation, `to_graph(..., stubs=True)` also adds `<ref> a T` for each `Ref[T]`. That
is exactly the information `Ref[T]` carries; it lets `sh:class T` be checked without the referent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel
from rdflib import RDF, XSD, Graph, Literal, URIRef
from rdflib.term import Node

from monograph.core.meta import DEFAULT_BASE
from monograph.core.refs import Ref
from monograph.core.spec import Card, ClassSpec, FieldSpec, Kind, SchemaSpec
from monograph.core.vocab import Vocabulary

_MISSING_KEY = "~missing"


class MappingError(ValueError):
    """The graph does not hold a lossless image of a model instance."""


def decimal_lexical(value: Decimal) -> str:
    """Canonical xsd:decimal lexical form (no exponent). Non-finite values stay as they are."""
    if not value.is_finite():
        return str(value)
    text = format(value, "f")
    return text


class RdfMapper:
    def __init__(self, spec: SchemaSpec, vocab: Vocabulary, base: str = DEFAULT_BASE) -> None:
        self._spec = spec
        self._vocab = vocab
        self._base = base
        self._members: dict[URIRef, StrEnum] = {vocab.member(e.cls, m): m for e in spec.enums for m in e.cls}

    # ------------------------------------------------------------------------------------- IRIs

    def iri_of(self, instance: BaseModel) -> URIRef:
        c = self._spec.of(type(instance))
        if not c.is_entity:
            raise MappingError(f"{c.cls.__name__} is not an entity")
        assert c.meta.path is not None
        parts = [self._key_part(instance, k) for k in c.meta.key]
        return URIRef(f"{self._base}{c.meta.path}/{'/'.join(parts)}")

    def _key_part(self, instance: BaseModel, key: str) -> str:
        value = instance.__dict__.get(key)
        if value is None:
            return _MISSING_KEY
        return quote(str(value), safe="")

    def _child_iri(self, parent: URIRef, f: FieldSpec, child: Any, index: int | None) -> URIRef:
        prefix = f"{parent}/{f.prop.name}"
        if f.card is Card.LIST:
            return URIRef(f"{prefix}/{index}")
        if f.card is Card.SET:
            assert f.target is not None
            parts = [self._key_part(child, k) for k in self._spec.of(f.target).meta.key]
            return URIRef(f"{prefix}/{'/'.join(parts)}")
        return URIRef(prefix)

    # ------------------------------------------------------------------------------- to RDF

    def to_graph(self, instance: BaseModel, *, stubs: bool = False) -> Graph:
        g = Graph()
        self._emit(g, self.iri_of(instance), instance, stubs)
        return g

    def _emit(self, g: Graph, node: URIRef, instance: BaseModel, stubs: bool) -> None:
        c = self._spec.of(type(instance))
        g.add((node, RDF.type, self._vocab.cls(c.cls)))
        for f in c.fields:
            if f.name not in instance.__dict__:
                continue
            value = instance.__dict__[f.name]
            if value is None:
                continue
            if f.is_collection and isinstance(value, list | tuple | set | frozenset):
                for index, item in enumerate(value):
                    self._emit_value(g, node, f, item, index, stubs)
            else:
                self._emit_value(g, node, f, value, None, stubs)
        for key, value in sorted((instance.__pydantic_extra__ or {}).items()):
            g.add((node, URIRef(self._vocab.ns + quote(key, safe="")), self.literal(value)))

    def _emit_value(self, g: Graph, node: URIRef, f: FieldSpec, value: Any, index: int | None, stubs: bool) -> None:
        predicate = self._vocab.prop(f)
        if f.kind is Kind.EMBEDDED:
            assert f.target is not None
            if isinstance(value, f.target):
                child = self._child_iri(node, f, value, index)
                g.add((node, predicate, child))
                self._emit(g, child, value, stubs)
            else:
                g.add((node, predicate, self.literal(value)))
            return
        term = self.term(f, value)
        g.add((node, predicate, term))
        if stubs and f.kind is Kind.REF and isinstance(term, URIRef):
            assert f.target is not None
            g.add((term, RDF.type, self._vocab.cls(f.target)))

    def term(self, f: FieldSpec, value: Any) -> Node:
        if f.kind is Kind.REF:
            if isinstance(value, Ref) and isinstance(value.iri, str):
                return URIRef(value.iri)
            return self.literal(value)
        if f.kind is Kind.ENUM:
            assert f.enum is not None
            if isinstance(value, f.enum):
                return self._vocab.member(f.enum, value)
            return self.literal(value)
        return self.literal(value)

    def literal(self, value: Any) -> Literal:
        """A literal typed by the Python value's own type, not by the field's declared type."""
        if isinstance(value, StrEnum):
            return Literal(value.value)
        if isinstance(value, bool):
            return Literal(value)
        if isinstance(value, int):
            return Literal(value, datatype=XSD.integer)
        if isinstance(value, Decimal):
            return Literal(decimal_lexical(value), datatype=XSD.decimal)
        if isinstance(value, float):
            return Literal(value, datatype=XSD.double)
        if isinstance(value, datetime):
            return Literal(value.isoformat(), datatype=XSD.dateTime)
        if isinstance(value, date):
            return Literal(value.isoformat(), datatype=XSD.date)
        if isinstance(value, str):
            return Literal(value)
        return Literal(json.dumps(value, default=str, sort_keys=True), datatype=self._vocab.unmapped_datatype())

    # ----------------------------------------------------------------------------- from RDF

    def from_graph(
        self, g: Graph, cls: type[BaseModel], iri: URIRef, context: Mapping[str, Any] | None = None
    ) -> BaseModel:
        """Read one instance back and validate it with Pydantic. Unknown triples are an error."""
        return cls.model_validate(self._read(g, self._spec.of(cls), iri, context), context=context)

    def _read(self, g: Graph, c: ClassSpec, node: URIRef, context: Mapping[str, Any] | None) -> dict[str, Any]:
        if (node, RDF.type, self._vocab.cls(c.cls)) not in g:
            raise MappingError(f"{node} is not typed {c.cls.__name__}")
        by_predicate = {self._vocab.prop(f): f for f in c.fields}
        collected: dict[str, list[Node]] = {}
        for p, o in g.predicate_objects(node):
            if p == RDF.type:
                continue
            f = by_predicate.get(p)  # type: ignore[call-overload]
            if f is None:
                raise MappingError(f"{node} has a triple with unknown predicate {p}")
            collected.setdefault(f.name, []).append(o)
        data: dict[str, Any] = {}
        for name, objects in collected.items():
            f = c.field(name)
            values = [self._value(g, f, node, o, context) for o in objects]
            if f.card is Card.LIST:
                order = [self._index(node, f, o) for o in objects]
                data[name] = [v for _, v in sorted(zip(order, values, strict=True), key=lambda pair: pair[0])]
            elif f.card is Card.SET:
                data[name] = frozenset(values)
            elif len(values) != 1:
                raise MappingError(f"{node} has {len(values)} values for single-valued {name}")
            else:
                data[name] = values[0]
        return data

    def _index(self, parent: URIRef, f: FieldSpec, child: Node) -> int:
        prefix = f"{parent}/{f.prop.name}/"
        text = str(child)
        if not text.startswith(prefix) or not text[len(prefix) :].isdigit():
            raise MappingError(f"list item {child} does not carry a position index")
        return int(text[len(prefix) :])

    def _value(self, g: Graph, f: FieldSpec, parent: URIRef, o: Node, context: Mapping[str, Any] | None) -> Any:
        if f.kind is Kind.EMBEDDED:
            assert f.target is not None
            if not isinstance(o, URIRef):
                raise MappingError(f"embedded value of {f.name} is not an IRI")
            return f.target.model_validate(self._read(g, self._spec.of(f.target), o, context), context=context)
        if f.kind is Kind.REF:
            if not isinstance(o, URIRef):
                raise MappingError(f"reference {f.name} is not an IRI")
            return Ref(str(o))
        if f.kind is Kind.ENUM:
            member = self._members.get(o) if isinstance(o, URIRef) else None
            if member is None:
                raise MappingError(f"{o} is not a member of {f.enum}")
            return member
        if not isinstance(o, Literal):
            raise MappingError(f"{f.name} expects a literal")
        return o.toPython()
