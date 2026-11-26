"""SchemaSpec -> SHACL shapes (Turtle).

Every concrete class gets a closed sh:NodeShape with sh:targetClass. Every field gets a named
sh:PropertyShape (named so validation reports say which field failed). Every rule gets its own named
sh:NodeShape. Patterns are checked against the safe regex subset before they are emitted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

from rdflib import RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.term import Node

from monograph.core.meta import Charset
from monograph.core.refs import IRI_PATTERN
from monograph.core.regex import analyze_pattern
from monograph.core.rules import AtLeast, RequiredWhen, Rule, RuleValue
from monograph.core.spec import ClassSpec, FieldSpec, Kind, SchemaSpec
from monograph.core.vocab import XSD_FOR, Vocabulary
from monograph.gen.turtle import TurtleWriter

SH = Namespace("http://www.w3.org/ns/shacl#")

# Lexical form of an xsd:dateTime with an explicit timezone. xsd:dateTime itself allows naive values,
# so AwareDatetime needs this extra pattern to mean the same thing on both sides.
TZ_PATTERN = r"(Z|[+\-][0-9]{2}:[0-9]{2})$"


def forbidden_class(charset: Charset) -> str:
    """A regex character class of the forbidden characters, written with the raw characters.

    XPath regex has no \\x escape, so the characters themselves go in the class. The Turtle writer
    escapes them as \\uXXXX in the file.
    """
    return "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in charset.forbidden_ranges()) + "]"


class ShaclGenerator:
    def __init__(
        self,
        spec: SchemaSpec,
        vocab: Vocabulary,
        writer: TurtleWriter,
        check_pattern: Callable[[str], None],
    ) -> None:
        self._spec = spec
        self._vocab = vocab
        self._writer = writer
        self._check_pattern = check_pattern

    def graph(self) -> Graph:
        g = Graph()
        for c in self._spec.classes:
            self._node_shape(g, c)
        return g

    def render(self) -> str:
        return self._writer.write(self.graph())

    # -----------------------------------------------------------------------------------------------

    def _list(self, g: Graph, items: Sequence[Node]) -> BNode:
        head = BNode()
        Collection(g, head, list(items))
        return head

    def _node_shape(self, g: Graph, c: ClassSpec) -> None:
        v = self._vocab
        shape = v.node_shape(c.cls)
        g.add((shape, RDF.type, SH.NodeShape))
        g.add((shape, SH.targetClass, v.cls(c.cls)))
        g.add((shape, RDFS.label, Literal(c.meta.label)))
        if c.meta.abstract:
            return
        g.add((shape, SH.closed, Literal(True)))
        g.add((shape, SH.ignoredProperties, self._list(g, [RDF.type])))
        for f in c.fields:
            ps = v.property_shape(c.cls, f)
            g.add((shape, SH.property, ps))
            self._property_shape(g, ps, f)
        for rule in c.rules:
            self._rule_shape(g, c, rule)

    def _property_shape(self, g: Graph, ps: URIRef, f: FieldSpec) -> None:
        v = self._vocab
        g.add((ps, RDF.type, SH.PropertyShape))
        g.add((ps, SH.path, v.prop(f)))
        g.add((ps, SH.name, Literal(f.prop.label)))
        if f.min_count > 0:
            g.add((ps, SH.minCount, Literal(f.min_count)))
        if f.max_count is not None:
            g.add((ps, SH.maxCount, Literal(f.max_count)))

        if f.kind is Kind.SCALAR:
            assert f.scalar is not None
            g.add((ps, SH.datatype, XSD_FOR[f.scalar]))
            if f.scalar is datetime:
                g.add((ps, SH.pattern, Literal(TZ_PATTERN)))
        elif f.kind is Kind.ENUM:
            assert f.enum is not None
            enum = f.enum
            g.add((ps, SH["in"], self._list(g, [v.member(enum, m) for m in enum])))
        elif f.kind is Kind.REF:
            assert f.target is not None
            g.add((ps, SH.nodeKind, SH.IRI))
            g.add((ps, SH["class"], v.cls(f.target)))
            self._check_pattern(IRI_PATTERN)
            g.add((ps, SH.pattern, Literal(IRI_PATTERN)))
        else:
            assert f.target is not None
            g.add((ps, SH.nodeKind, SH.IRI))
            g.add((ps, SH["class"], v.cls(f.target)))
            g.add((ps, SH.node, v.node_shape(f.target)))

        k = f.constraints
        max_length = k.max_length
        if k.pattern is not None:
            self._check_pattern(k.pattern)
            g.add((ps, SH.pattern, Literal(k.pattern)))
            implied = analyze_pattern(k.pattern).max_length
            if implied is not None and (max_length is None or implied < max_length):
                # Redundant with the pattern in a correct engine; it closes pySHACL's "$ before a final
                # newline" gap for every bounded pattern.
                max_length = implied
        if k.min_length is not None:
            g.add((ps, SH.minLength, Literal(k.min_length)))
        if max_length is not None:
            g.add((ps, SH.maxLength, Literal(max_length)))
        if k.charset is not None:
            g.add((ps, SH["not"], self._forbidden(g, k.charset)))
        if f.kind is Kind.REF:
            g.add((ps, SH["not"], self._forbidden(g, Charset.IDENTIFIER)))
        for bound, predicate in ((k.ge, SH.minInclusive), (k.le, SH.maxInclusive)):
            if bound is not None:
                g.add((ps, predicate, self._bound(f, bound)))
        for bound, predicate in ((k.gt, SH.minExclusive), (k.lt, SH.maxExclusive)):
            if bound is not None:
                g.add((ps, predicate, self._bound(f, bound)))

    def _forbidden(self, g: Graph, charset: Charset) -> BNode:
        """A shape matched by any string that contains a forbidden control character.

        Used under sh:not. An unanchored class needs no `$`, so the projection does not depend on how an
        engine treats a final newline.
        """
        shape = BNode()
        g.add((shape, SH.pattern, Literal(forbidden_class(charset))))
        return shape

    def _bound(self, f: FieldSpec, bound: int | Decimal) -> Literal:
        if f.scalar is Decimal:
            return Literal(format(Decimal(bound), "f"), datatype=XSD_FOR[Decimal])
        return Literal(int(bound))

    def _value(self, f: FieldSpec, value: RuleValue) -> Node:
        if f.kind is Kind.ENUM:
            assert f.enum is not None
            return self._vocab.member(f.enum, f.enum(str(value)))
        return Literal(value)

    def _rule_shape(self, g: Graph, c: ClassSpec, rule: Rule) -> None:
        v = self._vocab
        shape = v.rule_shape(rule)
        g.add((shape, RDF.type, SH.NodeShape))
        g.add((shape, SH.targetClass, v.cls(c.cls)))
        g.add((shape, RDFS.label, Literal(rule.id)))
        g.add((shape, SH.message, Literal(rule.message)))
        if isinstance(rule, RequiredWhen):
            when, then = c.field(rule.when), c.field(rule.then)
            has_value = BNode()
            g.add((has_value, SH.path, v.prop(when)))
            g.add((has_value, SH.hasValue, self._value(when, rule.equals)))
            not_case = BNode()
            g.add((not_case, SH["not"], has_value))
            then_case = BNode()
            g.add((then_case, SH.path, v.prop(then)))
            g.add((then_case, SH.minCount, Literal(1)))
            g.add((shape, SH["or"], self._list(g, [not_case, then_case])))
        elif isinstance(rule, AtLeast):
            parent = c.field(rule.field)
            assert parent.target is not None
            child = self._spec.of(parent.target).field(rule.where)
            condition = BNode()
            g.add((condition, SH.path, v.prop(child)))
            g.add((condition, SH.minCount, Literal(1)))
            g.add((condition, SH["in"], self._list(g, [self._value(child, value) for value in rule.values])))
            qualified = BNode()
            g.add((qualified, SH.property, condition))
            counted = BNode()
            g.add((counted, SH.path, v.prop(parent)))
            g.add((counted, SH.qualifiedValueShape, qualified))
            g.add((counted, SH.qualifiedMinCount, Literal(rule.min_count)))
            g.add((shape, SH.property, counted))
