"""What the check knows: every warehouse table and semantic view, each column's ontology term, and what it
denotes, read from the generated mappings only (`h1.warehouse.mapping.json`, `h1.semantic.mapping.json`).

A column *denotes* an entity when its values identify one: a surrogate key (`(Product, "sk")`), a business key
(`(Product, "key:product_ndc")`), or a position in an embedded list (`(AdverseEventReport.drugs, "seq")`).
Two columns may be joined when they denote the same thing in the same way; that is the ontology's relations,
read through the mapping (rule 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Kind = Literal["code", "label", "flag", "bool", "date_int", "date", "number", "text", "code_table"]
Denotes = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    table: str  # lower case, as Postgres folds it
    name: str
    role: str
    term: str | None
    kind: Kind
    denotes: Denotes | None = None
    domain: str | None = None  # code domain (warehouse code columns)
    allowed: frozenset[str] | None = None  # the literals a comparison may use
    owner_class: str | None = None  # the class whose property the column holds
    mapped: bool = True

    @property
    def label(self) -> str:
        return (
            f"{self.table.upper()}.{self.name.upper()}"
            if self.table.startswith("t_") or self.table.startswith("x_")
            else f"{self.table}.{self.name}"
        )


@dataclass(frozen=True, slots=True)
class TableInfo:
    schema: str
    name: str  # lower case
    grain: str
    cls: str | None  # the entity class one row stands for (entity grain), or the owner class (finer grain)
    fine: bool  # finer than the entity: a multi-valued property, an embedded list, a bridge, the wide table
    columns: dict[str, ColumnInfo] = field(default_factory=dict)
    row_term: str | None = None  # what one row of a finer table is ("Product.routes", "Recall.products")

    @property
    def label(self) -> str:
        return self.name.upper() if self.schema == "wh" else f"{self.schema}.{self.name}"


@dataclass(frozen=True, slots=True)
class Domain:
    name: str
    enum: str
    codes: dict[str, str]  # code -> label
    in_code_table: bool


@dataclass
class Catalog:
    tables: dict[tuple[str, str], TableInfo]
    domains: dict[str, Domain]
    keys: dict[str, tuple[str, ...]]  # class -> key properties
    enum_labels: dict[str, frozenset[str]]  # term -> labels (an enum-valued property)

    def find(self, name: str, schemas: tuple[str, ...]) -> TableInfo | None:
        for s in schemas:
            t = self.tables.get((s, name.lower()))
            if t is not None:
                return t
        return None

    def sqlglot_schema(self, schemas: tuple[str, ...]) -> dict[str, object]:
        out: dict[str, Any] = {}
        for (s, name), t in self.tables.items():
            if s in schemas:
                out.setdefault(s, {})[name] = {c: "text" for c in t.columns}
        return out


def _root(term: str) -> str:
    return term.split(".", maxsplit=1)[0].split("=", maxsplit=1)[0]


def _key_denotes(term: str | None, keys: dict[str, tuple[str, ...]]) -> Denotes | None:
    if term is None or "=" in term:
        return None
    parts = term.split(".")
    if len(parts) == 2 and parts[1] in keys.get(parts[0], ()):
        return (parts[0], f"key:{parts[1]}")
    return None


def load(warehouse_mapping: dict[str, Any], semantic_mapping: dict[str, Any] | None) -> Catalog:
    keys = {c["class"]: tuple(c["key"]) for c in warehouse_mapping["classes"]}
    domains = {
        d["domain"]: Domain(d["domain"], d["enum"], {x["code"]: x["label"] for x in d["codes"]}, d["in_code_table"])
        for d in warehouse_mapping["code_domains"]
    }
    term_domain: dict[str, str] = {}
    tables: dict[tuple[str, str], TableInfo] = {}
    schema = warehouse_mapping["schema"]
    for t in warehouse_mapping["tables"]:
        name = t["table"].lower()
        fine = t["grain"] != t["class"]
        cols: dict[str, ColumnInfo] = {}
        for c in t["columns"]:
            role, term = c["role"], c.get("term")
            kind: Kind = "text"
            denotes: Denotes | None = None
            allowed: frozenset[str] | None = None
            domain = c.get("code_domain")
            transform = c.get("transform")
            if role in ("code_domain", "code_value", "code_label"):
                kind = "code_table"
                if role == "code_domain":
                    allowed = frozenset(d for d, x in domains.items() if x.in_code_table)
            elif role == "identity":
                denotes = (t["class"], "sk")
            elif role in ("owner", "ref"):
                denotes = (c.get("references") or t["class"], "sk")
            elif role == "seq":
                denotes = (term, "seq")
            elif role == "flag" or transform == "flag":
                kind, allowed = "flag", frozenset({"Y", "N"})
            elif transform == "code":
                kind = "code"
                allowed = frozenset(domains[domain].codes)
                if term:
                    term_domain[term] = domain
            elif transform == "date_int":
                kind = "date_int"
            elif transform == "date":
                kind = "date"
            elif transform in ("numeric", "int"):
                kind = "number"
            if role in ("value", "denorm") and denotes is None:
                denotes = _key_denotes(term, keys)
            owner = _root(term) if term and role in ("value", "flag", "denorm") else t["class"]
            if role == "denorm":
                owner = t["class"]
            cols[c["name"].lower()] = ColumnInfo(
                name,
                c["name"].lower(),
                role,
                term,
                kind,
                denotes,
                domain,
                allowed,
                owner,
                mapped=role != "legacy",
            )
        row_term = t["grain"].split(" x ")[-1] if fine and t["class"] is not None else None
        tables[(schema, name)] = TableInfo(
            schema, name, t["grain"], t["class"], fine and t["class"] is not None, cols, row_term
        )
    enum_labels = {term: frozenset(domains[d].codes.values()) for term, d in term_domain.items()}
    if semantic_mapping is not None:
        s = semantic_mapping["schema"]
        for v in semantic_mapping["views"]:
            name = v["view"].lower()
            fine = "." in v["class"]
            cls = v["class"].split(".")[0]
            cols = {}
            for c in v["columns"]:
                term = c.get("term")
                kind = "text"
                allowed = frozenset(c["allowed_values"]) if c.get("allowed_values") else None
                if c["type"] == "boolean":
                    kind = "bool"
                elif c["type"] == "date":
                    kind = "date"
                elif c["type"] == "numeric":
                    kind = "number"
                elif term in enum_labels:
                    kind, allowed = "label", enum_labels[term]
                elif allowed is not None:
                    kind = "label"
                denotes = _key_denotes(term, keys) if c["role"] in ("key", "ref") else None
                owner = _root(term) if term and c["role"] == "value" else cls
                cols[c["name"].lower()] = ColumnInfo(
                    name, c["name"].lower(), c["role"], term, kind, denotes, None, allowed, owner
                )
            tables[(s, name)] = TableInfo(s, name, v["grain"], cls, fine, cols, v["class"] if fine else None)
    return Catalog(tables, domains, keys, enum_labels)


def from_files(warehouse: Path, semantic: Path | None) -> Catalog:
    wm = json.loads(warehouse.read_text(encoding="utf-8"))
    sm = json.loads(semantic.read_text(encoding="utf-8")) if semantic is not None else None
    return load(wm, sm)
