"""The tool implementations, shared by both stores. A `Backend` answers store queries (SQL or Cypher); the
service turns them into tool outputs with one set of rules for normalization, sorting, capping and display,
so S-tools and G-tools cannot differ in anything but the store."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from monograph.extract.resolve import normalize_name
from monograph.graph.identity import normalize_firm
from monograph.tools import registry as r
from monograph.tools.registry import (
    KIND_LABEL,
    ROW_CAP,
    EntityKind,
    ToolError,
    canon,
    canonical_id,
    display,
    label_of,
    longest_token,
)

Direction = Literal["out", "in"]


class EdgeRow(BaseModel):
    from_id: str
    relation: str
    direction: Direction
    to_id: str
    to_label: str
    to_props: dict[str, Any]  # at least the display properties
    edge: dict[str, Any] = {}


class Backend(Protocol):
    name: str

    def vertices(self, label: str, iris: Sequence[str]) -> list[dict[str, Any]]: ...
    def by_property(self, label: str, prop: str, values: Sequence[str]) -> list[dict[str, Any]]: ...
    def by_token(self, label: str, prop: str, token: str) -> list[dict[str, Any]]: ...
    def alias_owners(self, token: str) -> list[tuple[str, str]]: ...
    def edges(self, label: str, iris: Sequence[str], relation: str | None, direction: Direction) -> list[EdgeRow]: ...
    def products_with_substance(self, substance: str, moiety: bool) -> list[tuple[str, str, str, str]]: ...
    def clinical_drugs_of(self, label: str, iri: str) -> list[str]: ...
    def reports_involving(self, clinical_drugs: Sequence[str]) -> list[tuple[str, str, str, bool]]: ...
    def recalls_of_products(self, products: Sequence[str]) -> list[tuple[str, str, str, str, str]]: ...
    def recalls_by_firm(self, organization: str) -> list[tuple[str, str, str, str, str]]: ...
    def products_of(self, label: str, iri: str) -> list[str]: ...


_DIGITS = re.compile(r"\D")


def _cap[T](items: list[T]) -> tuple[list[T], bool]:
    return items[:ROW_CAP], len(items) > ROW_CAP


class ToolService:
    def __init__(self, backend: Backend) -> None:
        self._b = backend

    @property
    def backend(self) -> str:
        return self._b.name

    def call(self, name: str, arguments: Mapping[str, Any]) -> BaseModel:
        spec = r.TOOL_BY_NAME.get(name)
        if spec is None:
            raise ToolError(f"unknown tool {name!r}")
        args = spec.input.model_validate(dict(arguments))
        out: BaseModel = getattr(self, name)(args)
        return out

    # ----------------------------------------------------------------------------------- helpers

    def _label(self, iri: str, allowed: Sequence[str] | None = None) -> str:
        label = label_of(iri)
        if label is None:
            raise ToolError(f"not an id: {iri!r}")
        if allowed is not None and label not in allowed:
            raise ToolError(f"{iri} is a {label}; this tool takes {', '.join(allowed)}")
        if not self._b.vertices(label, [iri]):
            raise ToolError(f"no entity with id {iri}")
        return label

    def _group(self, ids: Sequence[str]) -> tuple[dict[str, list[str]], list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        unknown = []
        for raw in ids:
            iri = canonical_id(raw)
            label = label_of(iri)
            if label is None:
                unknown.append(raw)
            else:
                groups[label].append(iri)
        return groups, unknown

    # ------------------------------------------------------------------------------------- tools

    def resolve_entity(self, a: r.ResolveEntityIn) -> r.ResolveEntityOut:
        text = a.text.strip()
        kinds = [a.kind] if a.kind else list(EntityKind)
        found: dict[tuple[str, str], r.Candidate] = {}

        def add(kind: EntityKind, rows: list[dict[str, Any]], match: str) -> None:
            for row in rows:
                key = (row["iri"], match)
                label = KIND_LABEL[kind]
                found.setdefault(
                    key, r.Candidate(id=row["iri"], kind=kind.value, display=display(label, row), match=match)
                )

        exact = {
            EntityKind.SUBSTANCE: [("unii", text.upper())],
            EntityKind.PRODUCT: [("product_ndc", text)],
            EntityKind.PACKAGE: [("ndc", text)],
            EntityKind.CLINICAL_DRUG: [("rxcui", text)],
            EntityKind.RECALL: [("recall_number", text)],
            EntityKind.REPORT: [("report_id", text)],
            EntityKind.LABEL: [("set_id", text.lower())],
            EntityKind.ORGANIZATION: [("slug", text)],
        }
        digits = _DIGITS.sub("", text)
        if len(digits) == 11 and len(text.replace("-", "")) == 11:
            exact[EntityKind.PACKAGE].append(("ndc11", digits))
        for kind in kinds:
            for prop, value in exact[kind]:
                add(kind, self._b.by_property(KIND_LABEL[kind], prop, [value]), prop)
        for kind in kinds:
            if kind in (EntityKind.SUBSTANCE, EntityKind.PRODUCT, EntityKind.CLINICAL_DRUG):
                target = normalize_name(text)
                token = longest_token(target)
                if token:
                    rows = self._b.by_token(KIND_LABEL[kind], "name", token)
                    add(kind, [x for x in rows if x.get("name") and normalize_name(str(x["name"])) == target], "name")
            if kind is EntityKind.ORGANIZATION:
                target = normalize_firm(text)
                token = longest_token(target)
                if token:
                    rows = self._b.by_token("Organization", "name", token)
                    add(kind, [x for x in rows if normalize_firm(str(x["name"])) == target], "name")
                    owners = sorted({o for o, name in self._b.alias_owners(token) if normalize_firm(name) == target})
                    add(kind, self._b.vertices("Organization", owners), "alias")
        order = {k.value: i for i, k in enumerate(EntityKind)}
        items = sorted(found.values(), key=lambda c: (order[c.kind], c.id, c.match))
        capped, truncated = _cap(items)
        return r.ResolveEntityOut(candidates=capped, truncated=truncated)

    def get_entity(self, a: r.GetEntityIn) -> r.GetEntityOut:
        groups, unknown = self._group(a.ids)
        entities = []
        for label, iris in sorted(groups.items()):
            rows = {row["iri"]: row for row in self._b.vertices(label, iris)}
            for iri in iris:
                row = rows.get(iri)
                if row is None:
                    unknown.append(iri)
                    continue
                props = {k: canon(v) for k, v in sorted(row.items()) if k != "iri" and v is not None}
                entities.append(r.Entity(id=iri, kind=label, properties=props))
        entities.sort(key=lambda e: e.id)
        return r.GetEntityOut(entities=entities, unknown_ids=sorted(set(unknown)))

    def _neighbors(
        self, ids: Sequence[str], relation: str | None, direction: str
    ) -> tuple[list[r.Neighbor], list[str]]:
        groups, unknown = self._group(ids)
        out: dict[tuple[str, str, str, str], r.Neighbor] = {}
        directions: list[Direction] = ["out", "in"] if direction == "both" else [direction]  # type: ignore[list-item]
        for label, iris in sorted(groups.items()):
            for d in directions:
                for e in self._b.edges(label, sorted(set(iris)), relation, d):
                    key = (e.from_id, e.relation, e.direction, e.to_id)
                    out.setdefault(
                        key,
                        r.Neighbor(
                            from_id=e.from_id,
                            relation=e.relation,
                            direction=e.direction,
                            id=e.to_id,
                            kind=e.to_label,
                            display=display(e.to_label, e.to_props),
                            edge={k: canon(v) for k, v in sorted(e.edge.items()) if v is not None},
                        ),
                    )
        return [out[k] for k in sorted(out)], unknown

    def neighbors(self, a: r.NeighborsIn) -> r.NeighborsOut:
        items, unknown = self._neighbors(a.ids, a.relation, a.direction)
        capped, truncated = _cap(items)
        return r.NeighborsOut(neighbors=capped, truncated=truncated, unknown_ids=sorted(set(unknown)))

    def find_path(self, a: r.FindPathIn) -> r.FindPathOut:
        source, target = canonical_id(a.from_id), canonical_id(a.to_id)
        self._label(source)
        self._label(target)
        if source == target:
            return r.FindPathOut(length=0, paths=[r.PathOut(nodes=[source], relations=[])])
        parents: dict[str, list[tuple[str, str]]] = {source: []}
        frontier = [source]
        truncated = False
        for _depth in range(1, a.max_hops + 1):
            nxt: dict[str, list[tuple[str, str]]] = defaultdict(list)
            edges, _ = self._neighbors(frontier, None, "both")
            for e in edges:
                if e.id in parents:
                    continue
                step = f"{e.relation}>" if e.direction == "out" else f"<{e.relation}"
                nxt[e.id].append((e.from_id, step))
            if not nxt:
                break
            for node, ps in nxt.items():
                parents[node] = sorted(set(ps))
            if target in nxt:
                paths = self._paths(parents, source, target)
                paths.sort(key=lambda p: (p.nodes, p.relations))
                return r.FindPathOut(
                    length=len(paths[0].relations), paths=paths[: r.MAX_PATHS], truncated=len(paths) > r.MAX_PATHS
                )
            frontier = sorted(nxt)
            if len(frontier) > 20 * ROW_CAP:
                truncated = True
                break
        return r.FindPathOut(length=None, paths=[], truncated=truncated)

    def _paths(self, parents: Mapping[str, list[tuple[str, str]]], source: str, node: str) -> list[r.PathOut]:
        if node == source:
            return [r.PathOut(nodes=[source], relations=[])]
        out = []
        for parent, step in parents[node]:
            for p in self._paths(parents, source, parent):
                out.append(r.PathOut(nodes=[*p.nodes, node], relations=[*p.relations, step]))
                if len(out) > 1000:
                    return out
        return out

    def products_with_substance(self, a: r.ProductsWithSubstanceIn) -> r.ProductsWithSubstanceOut:
        iri = canonical_id(a.substance_id)
        self._label(iri, ["Substance"])
        best: dict[str, r.ProductMatch] = {}
        for pid, name, ndc, via in self._b.products_with_substance(iri, a.include_moiety_matches):
            m = r.ProductMatch(id=pid, display=str(name), product_ndc=str(ndc), via=via)
            if pid not in best or (via == "ingredient" and best[pid].via == "moiety"):
                best[pid] = m
        items, truncated = _cap([best[k] for k in sorted(best)])
        return r.ProductsWithSubstanceOut(products=items, truncated=truncated)

    def _reports(
        self, entity_id: str, date_from: date | None, date_to: date | None, serious: bool
    ) -> list[r.ReportItem]:
        iri = canonical_id(entity_id)
        label = self._label(iri, ["ClinicalDrug", "Product", "PackagedProduct", "Substance"])
        drugs = sorted(set(self._b.clinical_drugs_of(label, iri)))
        rows = self._b.reports_involving(drugs) if drugs else []
        out: dict[str, r.ReportItem] = {}
        for rid, report_id, received, is_serious in rows:
            d = str(canon(received))
            if date_from and d < date_from.isoformat():
                continue
            if date_to and d > date_to.isoformat():
                continue
            if serious and not is_serious:
                continue
            out[rid] = r.ReportItem(id=rid, report_id=str(report_id), receipt_date=d, serious=bool(is_serious))
        return [out[k] for k in sorted(out)]

    def events_for(self, a: r.EventsForIn) -> r.EventsForOut:
        items = self._reports(a.entity_id, a.date_from, a.date_to, a.serious_only)
        capped, truncated = _cap(items)
        return r.EventsForOut(count=len(items), reports=capped, truncated=truncated)

    def _recalls(self, entity_id: str) -> list[r.RecallItem]:
        iri = canonical_id(entity_id)
        label = self._label(iri, ["Product", "PackagedProduct", "Substance", "ClinicalDrug", "Organization"])
        if label == "Organization":
            rows = self._b.recalls_by_firm(iri)
        else:
            rows = self._b.recalls_of_products(sorted(set(self._b.products_of(label, iri))))
        out = {
            rid: r.RecallItem(id=rid, recall_number=str(n), classification=str(c), report_date=str(canon(d)), firm_id=f)
            for rid, n, c, d, f in rows
        }
        return [out[k] for k in sorted(out)]

    def recalls_for(self, a: r.RecallsForIn) -> r.RecallsForOut:
        items, truncated = _cap(self._recalls(a.entity_id))
        return r.RecallsForOut(recalls=items, truncated=truncated)

    def count(self, a: r.CountIn) -> r.CountOut:
        if a.what == "reports":
            return r.CountOut(count=len(self._reports(a.related_to, a.date_from, a.date_to, a.serious_only)))
        if a.what == "recalls":
            items = self._recalls(a.related_to)
            return r.CountOut(count=sum(1 for x in items if a.classification in (None, x.classification)))
        iri = canonical_id(a.related_to)
        label = self._label(iri, ["Substance", "ClinicalDrug", "Organization"])
        if label == "Substance":
            rows = self._b.products_with_substance(iri, a.include_moiety_matches)
            return r.CountOut(count=len({row[0] for row in rows}))
        return r.CountOut(count=len(set(self._b.products_of(label, iri))))
