"""Constraint coverage matrix: every constraint in the source, by layer, computed rather than written.

Rows come from `SchemaSpec.constraint_sites()`. For each row:

* Pydantic: the constraint is in the model, so Pydantic enforces it (strict mode, no coercion).
* SHACL: looked up in the generated shapes graph. A row counts as enforced only if the matching SHACL
  construct is actually present on the field's property shape (or the rule shape exists).
* Postgres: read from the DDL generator's own inventories (`unenforced` and `partial`).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from rdflib import Graph, Literal, URIRef

from monograph.core.spec import ConstraintSite, SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.gen.ddl import DdlResult
from monograph.gen.shacl import SH

ENFORCED = "enforced"
PARTIAL = "partial"
NOT_ENFORCED = "not enforced"

# Which SHACL predicate on the property shape carries each constraint kind.
_SHACL_PREDICATE: dict[str, URIRef] = {
    "required": SH.minCount,
    "min_count": SH.minCount,
    "max_count": SH.maxCount,
    "datatype": SH.datatype,
    "enum": SH["in"],
    "ref": SH["class"],
    "node": SH.node,
    "pattern": SH.pattern,
    "min_length": SH.minLength,
    "max_length": SH.maxLength,
    "min_inclusive": SH.minInclusive,
    "max_inclusive": SH.maxInclusive,
    "min_exclusive": SH.minExclusive,
    "max_exclusive": SH.maxExclusive,
    "charset": SH["not"],
}


@dataclass(frozen=True, slots=True)
class CoverageRow:
    cls: str
    field: str
    kind: str
    pydantic: str
    shacl: str
    postgres: str
    note: str = ""


class CoverageMatrix:
    def __init__(self, spec: SchemaSpec, shapes: Graph, ddl: DdlResult, vocab: Vocabulary) -> None:
        self._spec = spec
        self._shapes = shapes
        self._ddl = ddl
        self._vocab = vocab

    def rows(self) -> list[CoverageRow]:
        return [self._row(site) for site in self._spec.constraint_sites()]

    @staticmethod
    def totals(rows: list[CoverageRow]) -> dict[str, dict[str, int]]:
        return {layer: dict(Counter(getattr(r, layer) for r in rows)) for layer in ("pydantic", "shacl", "postgres")}

    def _row(self, site: ConstraintSite) -> CoverageRow:
        shacl, note = self._shacl(site)
        postgres, pg_note = self._postgres(site)
        return CoverageRow(
            site.cls, site.field or "", site.kind, ENFORCED, shacl, postgres, "; ".join(n for n in (note, pg_note) if n)
        )

    # -----------------------------------------------------------------------------------------------

    def _shacl(self, site: ConstraintSite) -> tuple[str, str]:
        c = self._spec.by_name(site.cls)
        if site.kind == "closed":
            return self._present(self._vocab.node_shape(c.cls), SH.closed, Literal(True)), ""
        if site.kind.startswith("rule:"):
            rule = next(r for r in c.rules if f"rule:{r.id}" == site.kind)
            return self._present(self._vocab.rule_shape(rule), SH.targetClass, self._vocab.cls(c.cls)), ""
        if site.kind.startswith("pydantic_only:"):
            declared = next(p for p in c.pydantic_only if f"pydantic_only:{p.id}" == site.kind)
            if declared.unique is not None:
                f = c.field(declared.unique.field)
                assert f.target is not None
                if tuple(self._spec.of(f.target).meta.key) == declared.unique.keys:
                    return PARTIAL, "keys mint the item IRI, so duplicates merge and break sh:maxCount (indirect)"
            return NOT_ENFORCED, "pydantic_only: no SHACL Core projection"
        assert site.field is not None
        f = c.field(site.field)
        predicate = _SHACL_PREDICATE[site.kind]
        return self._present(self._vocab.property_shape(c.cls, f), predicate, None), ""

    def _present(self, subject: URIRef, predicate: URIRef, value: Literal | URIRef | None) -> str:
        if value is None:
            return ENFORCED if (subject, predicate, None) in self._shapes else NOT_ENFORCED
        return ENFORCED if (subject, predicate, value) in self._shapes else NOT_ENFORCED

    def _postgres(self, site: ConstraintSite) -> tuple[str, str]:
        owner = self._owner(site.cls)
        for u in self._ddl.unenforced:
            if u.kind == site.kind and u.cls in (site.cls, owner) and (u.field is None or u.field == site.field):
                return NOT_ENFORCED, u.reason
        for p in self._ddl.partial:
            if p.kind == site.kind and p.cls == site.cls and p.field == site.field:
                return PARTIAL, p.reason
        return ENFORCED, ""

    def _owner(self, cls: str) -> str | None:
        """For an embedded class, the entity whose child table holds it (its inventory uses that name)."""
        for c in self._spec.classes:
            for f in c.fields:
                if f.target is not None and f.target.__name__ == cls and f.kind.value == "embedded":
                    return c.cls.__name__
        return None
