"""A small deterministic Turtle writer.

rdflib's own serializer orders blank nodes by their random IDs, so two runs over the same graph can
produce different bytes. This writer only accepts tree-shaped blank nodes (each used exactly once as
an object, never as a top-level subject), renders them inline as `[ ... ]` or `( ... )`, and sorts
everything else by its rendered text. Same graph in, same bytes out.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from rdflib import RDF, BNode, Graph, Literal, URIRef
from rdflib.term import Node

_LOCAL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")
_INDENT = "    "


class TurtleError(ValueError):
    """The graph cannot be written deterministically (for example a shared blank node)."""


class TurtleWriter:
    def __init__(self, prefixes: Sequence[tuple[str, str]]) -> None:
        self._prefixes = tuple(prefixes)

    def write(self, graph: Graph, first: Sequence[URIRef] = ()) -> str:
        self._graph = graph
        self._check_blank_nodes()
        subjects: set[Node] = {s for s in graph.subjects() if not isinstance(s, BNode)}
        ordered: list[Node] = [s for s in first if s in subjects]
        ordered += sorted((s for s in subjects if s not in ordered), key=str)
        lines = [f"@prefix {p}: <{ns}> ." for p, ns in self._prefixes]
        for subject in ordered:
            lines.append("")
            lines.append(self._term(subject))
            lines.append(self._predicate_block(subject, 1) + " .")
        return "\n".join(lines) + "\n"

    def _check_blank_nodes(self) -> None:
        uses: dict[BNode, int] = {}
        for _, _, o in self._graph:
            if isinstance(o, BNode):
                uses[o] = uses.get(o, 0) + 1
        for s in set(self._graph.subjects()):
            if isinstance(s, BNode) and uses.get(s, 0) != 1:
                raise TurtleError("blank nodes must be used exactly once, as an object")
        for b, count in uses.items():
            if count != 1:
                raise TurtleError(f"blank node {b} is shared by {count} triples")

    def _predicate_block(self, subject: Node, depth: int) -> str:
        indent = _INDENT * depth
        entries: list[tuple[str, list[str]]] = []
        predicates = set(self._graph.predicates(subject))
        for p in predicates:
            if isinstance(subject, BNode) and p in (RDF.first, RDF.rest):
                continue
            objects = sorted(self._render(o, depth + 1) for o in self._graph.objects(subject, p))
            name = "a" if p == RDF.type else self._term(p)
            entries.append((name, objects))
        entries.sort(key=lambda e: (e[0] != "a", e[0]))
        rendered = []
        for name, objects in entries:
            if len(objects) > 1 and any("\n" in o for o in objects):
                joined = (",\n" + indent + _INDENT).join(objects)
                rendered.append(f"{indent}{name}\n{indent}{_INDENT}{joined}")
            else:
                rendered.append(f"{indent}{name} {', '.join(objects)}")
        return " ;\n".join(rendered)

    def _render(self, node: Node, depth: int) -> str:
        if isinstance(node, BNode):
            if (node, RDF.first, None) in self._graph:
                return "( " + " ".join(self._render(item, depth) for item in self._list_items(node)) + " )"
            inner = self._predicate_block(node, depth)
            return "[\n" + inner + "\n" + _INDENT * (depth - 1) + "]"
        return self._term(node)

    def _list_items(self, head: Node) -> list[Node]:
        items: list[Node] = []
        node: Node | None = head
        while node is not None and node != RDF.nil:
            first = self._graph.value(node, RDF.first)
            if first is None:
                raise TurtleError("malformed RDF list")
            items.append(first)
            node = self._graph.value(node, RDF.rest)
        return items

    def _term(self, node: Node) -> str:
        if isinstance(node, URIRef):
            return self._iri(str(node))
        if isinstance(node, Literal):
            return self._literal(node)
        raise TurtleError(f"cannot render {node!r}")

    def _iri(self, iri: str) -> str:
        for prefix, ns in self._prefixes:
            if iri.startswith(ns) and _LOCAL_NAME.match(iri[len(ns) :]):
                return f"{prefix}:{iri[len(ns) :]}"
        return f"<{iri}>"

    def _literal(self, lit: Literal) -> str:
        text = (
            str(lit)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        if lit.language:
            return f'"{text}"@{lit.language}'
        if lit.datatype is not None:
            return f'"{text}"^^{self._iri(str(lit.datatype))}'
        return f'"{text}"'
