"""SchemaSpec -> OWL ontology (Turtle)."""

from __future__ import annotations

from collections import defaultdict

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.collection import Collection

from monograph.core.spec import Card, FieldSpec, Kind, SchemaSpec
from monograph.core.vocab import XSD_FOR, Vocabulary
from monograph.gen.turtle import TurtleWriter


def _text(doc: str) -> str:
    return " ".join(doc.split())


class OwlGenerator:
    def __init__(self, spec: SchemaSpec, vocab: Vocabulary, writer: TurtleWriter) -> None:
        self._spec = spec
        self._vocab = vocab
        self._writer = writer

    def graph(self) -> Graph:
        g = Graph()
        v = self._vocab
        onto = v.ontology
        g.add((onto, RDF.type, OWL.Ontology))
        g.add((onto, RDFS.label, Literal("MONOGRAPH drug product model")))
        g.add((onto, OWL.versionInfo, Literal(self._spec.version)))
        g.add((onto, OWL.versionIRI, URIRef(f"{onto}/{self._spec.version}")))

        for c in self._spec.classes:
            iri = v.cls(c.cls)
            g.add((iri, RDF.type, OWL.Class))
            g.add((iri, RDFS.label, Literal(c.meta.label)))
            g.add((iri, RDFS.comment, Literal(_text(c.doc))))
            if c.parent is not None:
                g.add((iri, RDFS.subClassOf, v.cls(c.parent)))

        for e in self._spec.enums:
            iri = v.enum(e.cls)
            g.add((iri, RDF.type, OWL.Class))
            g.add((iri, RDFS.label, Literal(e.meta.label)))
            g.add((iri, RDFS.comment, Literal(_text(e.doc))))
            for member in e.cls:
                ind = v.member(e.cls, member)
                g.add((ind, RDF.type, OWL.NamedIndividual))
                g.add((ind, RDF.type, iri))
                g.add((ind, RDFS.label, Literal(member.value)))

        uses: dict[URIRef, list[tuple[URIRef, FieldSpec]]] = defaultdict(list)
        for c in self._spec.classes:
            for f in c.fields:
                uses[v.prop(f)].append((v.cls(c.cls), f))
        for prop, sites in uses.items():
            first = sites[0][1]
            kind = OWL.DatatypeProperty if first.kind is Kind.SCALAR else OWL.ObjectProperty
            g.add((prop, RDF.type, kind))
            g.add((prop, RDFS.label, Literal(first.prop.label)))
            if all(f.card in (Card.ONE, Card.OPTIONAL) for _, f in sites):
                g.add((prop, RDF.type, OWL.FunctionalProperty))
            descriptions = sorted({f.description for _, f in sites if f.description})
            if len(descriptions) == 1:
                g.add((prop, RDFS.comment, Literal(descriptions[0])))
            g.add((prop, RDFS.range, self._range(first)))
            domains = sorted({d for d, _ in sites}, key=str)
            if len(domains) == 1:
                g.add((prop, RDFS.domain, domains[0]))
            else:
                union = BNode()
                head = BNode()
                Collection(g, head, list(domains))
                g.add((union, RDF.type, OWL.Class))
                g.add((union, OWL.unionOf, head))
                g.add((prop, RDFS.domain, union))
        return g

    def _range(self, f: FieldSpec) -> URIRef:
        if f.kind is Kind.SCALAR:
            assert f.scalar is not None
            return XSD_FOR[f.scalar]
        if f.kind is Kind.ENUM:
            assert f.enum is not None
            return self._vocab.enum(f.enum)
        assert f.target is not None
        return self._vocab.cls(f.target)

    def render(self) -> str:
        return self._writer.write(self.graph(), first=[self._vocab.ontology])
