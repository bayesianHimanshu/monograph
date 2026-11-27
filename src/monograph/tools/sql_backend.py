"""The SQL backend: the tool queries against the relational tables in schema `h2` (read-only role h2_sql)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from monograph.gen.age import Projection
from monograph.tools.registry import DISPLAY_ORDER
from monograph.tools.service import Direction, EdgeRow


class SqlBackend:
    name = "sql"

    def __init__(self, conn: psycopg.Connection[Any], projection: Projection) -> None:
        self._c = conn
        self._p = projection
        self._columns = {v.label: [p.column for p in v.properties] for v in projection.vertices}
        self._tables = {v.label: v.table for v in projection.vertices}

    def _all(self, query: str | sql.Composed, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return list(self._c.execute(query, list(params)).fetchall())

    def _select(self, label: str, where: sql.Composable, params: Sequence[Any]) -> list[dict[str, Any]]:
        cols = self._columns[label]
        query = sql.SQL("SELECT {} FROM {} WHERE {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in cols), sql.Identifier(self._tables[label]), where
        )
        return [dict(zip(cols, row, strict=True)) for row in self._all(query, params)]

    def vertices(self, label: str, iris: Sequence[str]) -> list[dict[str, Any]]:
        return self._select(label, sql.SQL("iri = ANY(%s)"), [list(iris)])

    def by_property(self, label: str, prop: str, values: Sequence[str]) -> list[dict[str, Any]]:
        if prop not in self._columns[label]:
            return []
        return self._select(label, sql.SQL("{} = ANY(%s)").format(sql.Identifier(prop)), [list(values)])

    def by_token(self, label: str, prop: str, token: str) -> list[dict[str, Any]]:
        return self._select(label, sql.SQL("strpos(upper({}), %s) > 0").format(sql.Identifier(prop)), [token])

    def alias_owners(self, token: str) -> list[tuple[str, str]]:
        if not self._p.has_edge("ALIAS"):
            return []
        rows = self._all("SELECT owner_iri, name FROM organization__aliases WHERE strpos(upper(name), %s) > 0", [token])
        return [(str(a), str(b)) for a, b in rows]

    def edges(self, label: str, iris: Sequence[str], relation: str | None, direction: Direction) -> list[EdgeRow]:
        out: list[EdgeRow] = []
        for e in self._p.edges:
            if relation is not None and e.label != relation:
                continue
            for s in e.sources:
                start, other = (s.from_label, s.to_label) if direction == "out" else (s.to_label, s.from_label)
                if start != label:
                    continue
                near, far = (s.from_column, s.to_column) if direction == "out" else (s.to_column, s.from_column)
                shown = [c for c in DISPLAY_ORDER if c in self._columns[other]]
                eprops = [p.column for p in s.properties]
                query = sql.SQL(
                    "SELECT e.{near}, e.{far}, {eprops}, {shown} FROM {table} e JOIN {other} n ON n.iri = e.{far}"
                    " WHERE e.{near} = ANY(%s)"
                ).format(
                    near=sql.Identifier(near),
                    far=sql.Identifier(far),
                    eprops=sql.SQL(", ").join([sql.SQL("NULL"), *(sql.SQL("e.") + sql.Identifier(c) for c in eprops)]),
                    shown=sql.SQL(", ").join(sql.SQL("n.") + sql.Identifier(c) for c in shown),
                    table=sql.Identifier(s.table),
                    other=sql.Identifier(self._tables[other]),
                )
                for row in self._all(query, [list(iris)]):
                    edge = dict(zip(eprops, row[3 : 3 + len(eprops)], strict=True))
                    props = dict(zip(shown, row[3 + len(eprops) :], strict=True))
                    out.append(
                        EdgeRow(
                            from_id=row[0],
                            relation=e.label,
                            direction=direction,
                            to_id=row[1],
                            to_label=other,
                            to_props=props,
                            edge=edge,
                        )
                    )
        return out

    def products_with_substance(self, substance: str, moiety: bool) -> list[tuple[str, str, str, str]]:
        query = (
            "SELECT p.iri, p.name, p.product_ndc, 'ingredient' FROM product__ingredients i"
            " JOIN product p ON p.iri = i.owner_iri WHERE i.substance_iri = %s"
        )
        params: list[Any] = [substance]
        if moiety and self._p.has_edge("HAS_ACTIVE_MOIETY"):
            query += (
                " UNION SELECT p.iri, p.name, p.product_ndc, 'moiety' FROM product__ingredients i"
                " JOIN substance__active_moiety m ON m.owner_iri = i.substance_iri"
                " JOIN product p ON p.iri = i.owner_iri WHERE m.moiety_iri = %s"
            )
            params.append(substance)
        return [(str(a), str(b), str(c), str(d)) for a, b, c, d in self._all(query, params)]

    def _product_sql(self, label: str) -> str:
        return {
            "Product": "SELECT %s::text",
            "PackagedProduct": "SELECT product_iri FROM packaged_product WHERE iri = %s",
            "Substance": "SELECT owner_iri FROM product__ingredients WHERE substance_iri = %s",
        }[label]

    def clinical_drugs_of(self, label: str, iri: str) -> list[str]:
        if label == "ClinicalDrug":
            return [iri]
        parts = []
        if self._p.has_edge("DRUG_MAPPING"):
            parts.append(
                "SELECT m.clinical_drug_iri FROM product__drug_mappings m WHERE m.owner_iri IN (SELECT * FROM p)"
            )
        if self._p.has_edge("CLINICAL_DRUG", "Product"):
            parts.append(
                "SELECT clinical_drug_iri FROM product WHERE iri IN (SELECT * FROM p) AND clinical_drug_iri IS NOT NULL"
            )
        if not parts:
            return []  # no crosswalk in this store (H2.1, identity off)
        query = f"WITH p AS ({self._product_sql(label)}) " + " UNION ".join(parts)
        return [str(row[0]) for row in self._all(query, [iri])]

    def reports_involving(self, clinical_drugs: Sequence[str]) -> list[tuple[str, str, str, bool]]:
        drugs = list(clinical_drugs)
        conditions, params = [], []
        if self._p.has_edge("CLINICAL_DRUG", "DrugInvolvement"):
            conditions.append("d.clinical_drug_iri = ANY(%s)")
            params.append(drugs)
        if self._p.has_edge("HARMONIZED_CLINICAL_DRUG"):
            conditions.append(
                "EXISTS (SELECT 1 FROM adverse_event_report__drugs__harmonized_clinical_drugs h"
                " WHERE h.owner_iri = d.iri AND h.target_iri = ANY(%s))"
            )
            params.append(drugs)
        if not conditions:
            return []  # report drugs are not linked to clinical drugs in this store (H2.1, identity off)
        query = (
            "SELECT DISTINCT r.iri, r.report_id, r.receipt_date, r.serious FROM adverse_event_report r"
            " JOIN adverse_event_report__drugs d ON d.owner_iri = r.iri WHERE " + " OR ".join(conditions)
        )
        return [(str(a), str(b), str(c), bool(d)) for a, b, c, d in self._all(query, params)]

    _RECALL = (
        "SELECT DISTINCT r.iri, r.recall_number, r.classification, r.report_date, r.recalling_firm_iri FROM recall r"
    )

    def recalls_of_products(self, products: Sequence[str]) -> list[tuple[str, str, str, str, str]]:
        query = self._RECALL + " JOIN recall__products rp ON rp.owner_iri = r.iri WHERE rp.target_iri = ANY(%s)"
        return [(str(a), str(b), str(c), str(d), str(e)) for a, b, c, d, e in self._all(query, [list(products)])]

    def recalls_by_firm(self, organization: str) -> list[tuple[str, str, str, str, str]]:
        query = self._RECALL + " WHERE r.recalling_firm_iri = %s"
        return [(str(a), str(b), str(c), str(d), str(e)) for a, b, c, d, e in self._all(query, [organization])]

    def products_of(self, label: str, iri: str) -> list[str]:
        if label == "ClinicalDrug":
            parts = []
            if self._p.has_edge("DRUG_MAPPING"):
                parts.append("SELECT owner_iri FROM product__drug_mappings WHERE clinical_drug_iri = %s")
            if self._p.has_edge("CLINICAL_DRUG", "Product"):
                parts.append("SELECT iri FROM product WHERE clinical_drug_iri = %s")
            if not parts:
                return []
            return [str(row[0]) for row in self._all(" UNION ".join(parts), [iri] * len(parts))]
        if label == "Organization":
            query = "SELECT DISTINCT product_iri FROM packaged_product WHERE labeler_iri = %s"
            return [str(row[0]) for row in self._all(query, [iri])]
        return [str(row[0]) for row in self._all(self._product_sql(label), [iri])]
