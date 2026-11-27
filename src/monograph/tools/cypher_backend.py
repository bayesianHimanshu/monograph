"""The Cypher backend: the tool queries against the AGE graph (read-only role h2_graph).

AGE 1.8.0 has no relationship-type alternation (`[:A|B]`), so queries that follow either of two relations
are written as a UNION; it has no shortestPath(), so find_path expands a frontier one hop at a time (in the
shared service, for both stores).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, ClassVar

import psycopg
from psycopg import sql

from monograph.gen.age import Projection
from monograph.tools.registry import DISPLAY_ORDER
from monograph.tools.service import Direction, EdgeRow


def parse_agtype(value: Any) -> Any:
    """agtype text -> Python. Vertices and edges carry a `::vertex`/`::edge` suffix; numbers may carry `::numeric`."""
    if value is None:
        return None
    text = str(value)
    for suffix in ("::vertex", "::edge", "::path", "::numeric"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return json.loads(text)


QUOTE = "$cypher$"


def cypher_statement(graph: str, query: str, columns: int, with_params: bool) -> sql.Composed:
    """`SELECT * FROM cypher(graph, $cypher$ query $cypher$[, %s::agtype]) AS (c0 agtype, ...)`. The query
    is a separate fragment, never formatted, so its braces are left alone; it must not contain the quote."""
    if QUOTE in query:
        raise ValueError(f"the query must not contain {QUOTE}")
    defs = sql.SQL(", ").join(sql.SQL(f"c{i} agtype") for i in range(columns))
    tail = " " + QUOTE + (", %s::agtype" if with_params else "") + ") AS ("
    if with_params:
        query = query.replace("%", "%%")
    return sql.Composed(
        [
            sql.SQL("SELECT * FROM cypher({}, ").format(sql.Literal(graph)),
            sql.SQL(QUOTE + " " + query + tail),
            defs,
            sql.SQL(")"),
        ]
    )


class CypherBackend:
    name = "cypher"

    def __init__(self, conn: psycopg.Connection[Any], projection: Projection) -> None:
        self._c = conn
        self._p = projection
        self._graph = projection.graph
        self._labels = {v.label for v in projection.vertices}
        self._props = {v.label: {p.name for p in v.properties} for v in projection.vertices}

    def run(self, query: str, params: dict[str, Any] | None, columns: int) -> list[list[Any]]:
        stmt = cypher_statement(self._graph, query, columns, params is not None)
        rows = self._c.execute(stmt, [json.dumps(params)] if params is not None else None).fetchall()
        return [[parse_agtype(v) for v in row] for row in rows]

    def _check(self, label: str) -> None:
        if label not in self._labels:
            raise ValueError(f"unknown label {label}")

    def vertices(self, label: str, iris: Sequence[str]) -> list[dict[str, Any]]:
        self._check(label)
        rows = self.run(f"MATCH (n:{label}) WHERE n.iri IN $ids RETURN properties(n)", {"ids": list(iris)}, 1)
        return [row[0] for row in rows]

    def by_property(self, label: str, prop: str, values: Sequence[str]) -> list[dict[str, Any]]:
        self._check(label)
        if prop not in self._props[label]:
            return []
        rows = self.run(f"MATCH (n:{label}) WHERE n.{prop} IN $v RETURN properties(n)", {"v": list(values)}, 1)
        return [row[0] for row in rows]

    def by_token(self, label: str, prop: str, token: str) -> list[dict[str, Any]]:
        self._check(label)
        rows = self.run(f"MATCH (n:{label}) WHERE toUpper(n.{prop}) CONTAINS $t RETURN properties(n)", {"t": token}, 1)
        return [row[0] for row in rows]

    def alias_owners(self, token: str) -> list[tuple[str, str]]:
        rows = self.run(
            "MATCH (o:Organization)-[:ALIAS]->(a:OrganizationAlias) WHERE toUpper(a.name) CONTAINS $t"
            " RETURN o.iri, a.name",
            {"t": token},
            2,
        )
        return [(str(a), str(b)) for a, b in rows]

    def edges(self, label: str, iris: Sequence[str], relation: str | None, direction: Direction) -> list[EdgeRow]:
        self._check(label)
        rel = ""
        if relation is not None:
            if relation not in {e.label for e in self._p.edges}:
                return []
            rel = f":{relation}"
        arrow = f"-[e{rel}]->" if direction == "out" else f"<-[e{rel}]-"
        shown = ", ".join(f"b.{k}" for k in DISPLAY_ORDER)
        rows = self.run(
            f"MATCH (a:{label}){arrow}(b) WHERE a.iri IN $ids"
            f" RETURN a.iri, type(e), b.iri, label(b), properties(e), [{shown}]",
            {"ids": list(iris)},
            6,
        )
        out = []
        for a, rtype, b, blabel, eprops, values in rows:
            props = {k: v for k, v in zip(DISPLAY_ORDER, values, strict=True) if k in self._props[blabel]}
            out.append(
                EdgeRow(
                    from_id=a,
                    relation=rtype,
                    direction=direction,
                    to_id=b,
                    to_label=blabel,
                    to_props=props,
                    edge=eprops or {},
                )
            )
        return out

    def products_with_substance(self, substance: str, moiety: bool) -> list[tuple[str, str, str, str]]:
        query = (
            "MATCH (p:Product)-[:INGREDIENT]->(s:Substance {iri: $s}) RETURN p.iri, p.name, p.product_ndc, 'ingredient'"
        )
        if moiety:
            query += (
                " UNION MATCH (p:Product)-[:INGREDIENT]->(:Substance)-[:HAS_ACTIVE_MOIETY]->(m:Substance {iri: $s})"
                " RETURN p.iri, p.name, p.product_ndc, 'moiety'"
            )
        return [(str(a), str(b), str(c), str(d)) for a, b, c, d in self.run(query, {"s": substance}, 4)]

    _PRODUCTS: ClassVar[dict[str, str]] = {
        "Product": "MATCH (p:Product {iri: $i})",
        "PackagedProduct": "MATCH (:PackagedProduct {iri: $i})-[:PRODUCT]->(p:Product)",
        "Substance": "MATCH (:Substance {iri: $i})<-[:INGREDIENT]-(p:Product)",
    }

    def clinical_drugs_of(self, label: str, iri: str) -> list[str]:
        if label == "ClinicalDrug":
            return [iri]
        start = self._PRODUCTS[label]
        query = (
            f"{start}-[:DRUG_MAPPING]->(c:ClinicalDrug) RETURN c.iri"
            f" UNION {start}-[:CLINICAL_DRUG]->(c:ClinicalDrug) RETURN c.iri"
        )
        return [str(row[0]) for row in self.run(query, {"i": iri}, 1)]

    def reports_involving(self, clinical_drugs: Sequence[str]) -> list[tuple[str, str, str, bool]]:
        ret = "RETURN r.iri, r.report_id, r.receipt_date, r.serious"
        query = (
            "MATCH (r:AdverseEventReport)-[:DRUG_INVOLVEMENT]->(:DrugInvolvement)-[:HARMONIZED_CLINICAL_DRUG]->"
            f"(c:ClinicalDrug) WHERE c.iri IN $c {ret}"
            " UNION MATCH (r:AdverseEventReport)-[:DRUG_INVOLVEMENT]->(:DrugInvolvement)-[:CLINICAL_DRUG]->"
            f"(c:ClinicalDrug) WHERE c.iri IN $c {ret}"
        )
        return [(str(a), str(b), str(c), bool(d)) for a, b, c, d in self.run(query, {"c": list(clinical_drugs)}, 4)]

    _RECALL = "RETURN DISTINCT r.iri, r.recall_number, r.classification, r.report_date, f.iri"

    def recalls_of_products(self, products: Sequence[str]) -> list[tuple[str, str, str, str, str]]:
        query = (
            "MATCH (f:Organization)<-[:RECALLING_FIRM]-(r:Recall)-[:RECALLS_PRODUCT]->(p:Product)"
            f" WHERE p.iri IN $p {self._RECALL}"
        )
        return [(str(a), str(b), str(c), str(d), str(e)) for a, b, c, d, e in self.run(query, {"p": list(products)}, 5)]

    def recalls_by_firm(self, organization: str) -> list[tuple[str, str, str, str, str]]:
        query = f"MATCH (r:Recall)-[:RECALLING_FIRM]->(f:Organization {{iri: $o}}) {self._RECALL}"
        return [(str(a), str(b), str(c), str(d), str(e)) for a, b, c, d, e in self.run(query, {"o": organization}, 5)]

    def products_of(self, label: str, iri: str) -> list[str]:
        if label == "ClinicalDrug":
            query = (
                "MATCH (p:Product)-[:DRUG_MAPPING]->(:ClinicalDrug {iri: $i}) RETURN p.iri"
                " UNION MATCH (p:Product)-[:CLINICAL_DRUG]->(:ClinicalDrug {iri: $i}) RETURN p.iri"
            )
        elif label == "Organization":
            query = (
                "MATCH (:Organization {iri: $i})<-[:LABELER]-(:PackagedProduct)-[:PRODUCT]->(p:Product)"
                " RETURN DISTINCT p.iri"
            )
        else:
            query = f"{self._PRODUCTS[label]} RETURN p.iri"
        return [str(row[0]) for row in self.run(query, {"i": iri}, 1)]
