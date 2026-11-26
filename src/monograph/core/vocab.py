"""IRIs for vocabulary terms and shapes. Generators and the instance mapping share this one class."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel
from rdflib import XSD, URIRef

from monograph.core.meta import SHAPES_NS, VOCAB_NS, OntClass, OntEnum
from monograph.core.rules import Rule
from monograph.core.spec import FieldSpec

XSD_FOR: dict[type, URIRef] = {
    str: XSD.string,
    int: XSD.integer,
    Decimal: XSD.decimal,
    bool: XSD.boolean,
    date: XSD.date,
    datetime: XSD.dateTime,
}


class Vocabulary:
    def __init__(self, ns: str = VOCAB_NS, shapes_ns: str = SHAPES_NS) -> None:
        self.ns = ns
        self.shapes_ns = shapes_ns

    @property
    def ontology(self) -> URIRef:
        return URIRef(self.ns.rstrip("#/"))

    def cls(self, model: type[BaseModel]) -> URIRef:
        meta = vars(model)["__ont__"]
        assert isinstance(meta, OntClass)
        return URIRef(self.ns + meta.name)

    def enum(self, enum: type[StrEnum]) -> URIRef:
        meta = vars(enum)["__ont__"]
        assert isinstance(meta, OntEnum)
        return URIRef(self.ns + meta.name)

    def member(self, enum: type[StrEnum], member: StrEnum) -> URIRef:
        return URIRef(f"{self.enum(enum)}-{member.name}")

    def prop(self, field: FieldSpec) -> URIRef:
        return URIRef(self.ns + field.prop.name)

    def node_shape(self, model: type[BaseModel]) -> URIRef:
        return URIRef(self.shapes_ns + vars(model)["__ont__"].name)

    def property_shape(self, model: type[BaseModel], field: FieldSpec) -> URIRef:
        return URIRef(f"{self.node_shape(model)}-{field.prop.name}")

    def rule_shape(self, rule: Rule) -> URIRef:
        return URIRef(f"{self.shapes_ns}rule-{rule.id}")

    def unmapped_datatype(self) -> URIRef:
        """Datatype for values the mapping cannot represent faithfully (wrong Python types)."""
        return URIRef(self.ns + "UnmappedValue")
