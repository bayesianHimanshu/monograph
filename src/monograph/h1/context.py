"""What `describe_data` returns in each context (docs/h1/PLAN.md, "Arms").

* W-raw: the warehouse DDL (no comments, cryptic names).
* SV: the semantic model (YAML), the view columns, and the raw warehouse tables, which are queryable too.
* ONT: the ontology (classes, properties, descriptions, cardinalities, enums with their codes, relations) and
  the mapping of every class and property to the warehouse.

All three are rendered from generated artifacts only, so they are as reproducible as the artifacts. Small code
lists are given in full in both SV and ONT; the two large ones (121 routes, 158 dosage forms) are given by
examples and by where to look them up, in both.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from monograph import wiring
from monograph.core.spec import Card, FieldSpec, Kind
from monograph.gen import semantic_layer as s
from monograph.gen import warehouse as w

CONTEXTS = ("W-raw", "SV", "ONT")
LARGE_DOMAIN = 20


def _ddl(root: Path) -> str:
    text = (wiring.generated_dir(root) / w.DDL_FILE).read_text(encoding="utf-8")
    keep = [ln for ln in text.splitlines() if not ln.startswith(("--", "CREATE INDEX", "CREATE SCHEMA"))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip() + "\n"


def warehouse_text(root: Path) -> str:
    return "PostgreSQL 18. The warehouse tables are in schema wh (on the search path).\n\n" + _ddl(root)


def semantic_text(root: Path) -> str:
    g = wiring.generated_dir(root)
    model = (g / s.MODEL_FILE).read_text(encoding="utf-8")
    mapping = json.loads((g / s.MAPPING_FILE).read_text(encoding="utf-8"))
    views = []
    for v in mapping["views"]:
        cols = ",\n".join(f"    {c['name']} {c['type']}" for c in v["columns"])
        views.append(f"VIEW sem.{v['view']} ({v['grain']}) (\n{cols}\n)")
    return (
        "PostgreSQL 18. Query the semantic layer: the views in schema sem. The semantic model below describes"
        " them: entities, dimensions, measures, joins, synonyms. The raw warehouse tables (schema wh) are also"
        " queryable; they are listed at the end.\n\n"
        + model
        + "\nViews (schema sem):\n\n"
        + "\n\n".join(views)
        + "\n\nRaw warehouse tables (schema wh), not described by the semantic layer:\n\n"
        + _ddl(root)
    )


def _card(f: FieldSpec) -> str:
    if f.card is Card.ONE:
        return "exactly one"
    if f.card is Card.OPTIONAL:
        return "at most one"
    order = "ordered" if f.card is Card.LIST else "unordered"
    least = "one or more" if f.min_count >= 1 else "zero or more"
    return f"{least}, {order}"


def _desc(f: FieldSpec) -> str:
    return " ".join((f.description or "").split())


def _domain_text(d: dict[str, Any]) -> str:
    codes = d["codes"]
    where = (
        f"decode through T_REF_CD where CD_DOM = '{d['domain']}' (CD_VAL is the code, CD_DSC the term)"
        if d["in_code_table"]
        else "the codes are fixed"
    )
    if len(codes) <= LARGE_DOMAIN:
        listed = ", ".join(f"{c['code']} = {c['label']}" for c in codes)
        return f"codes: {listed}; {where}"
    chosen = [c for c in codes if c["label"] in s.EXAMPLES.get(d["enum"], ())]
    examples = ", ".join(f"{c['code']} = {c['label']}" for c in chosen)
    return f"{len(codes)} codes, for example {examples}; {where}"


def ontology_text(root: Path) -> str:
    spec = wiring.build_spec()
    wh = w.analyze(spec)
    mapping = json.loads((wiring.generated_dir(root) / w.MAPPING_FILE).read_text(encoding="utf-8"))
    domains = {d["domain"]: d for d in mapping["code_domains"]}
    where: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    flags: dict[str, list[tuple[str, str, str]]] = {}
    legacy: list[str] = []
    tables = {t["table"]: t for t in mapping["tables"]}
    for t in mapping["tables"]:
        for c in t["columns"]:
            term = c.get("term")
            if c["role"] == "legacy":
                legacy.append(f"{t['table']}.{c['name']}")
            elif c["role"] == "flag" and term:
                prop, _, member = term.partition("=")
                flags.setdefault(prop, []).append((t["table"], c["name"], member))
            elif term and c["role"] in ("value", "ref"):
                where[term] = (t, c)
    lines = [
        f"Ontology of the drug data (MONOGRAPH model {spec.version}), with its mapping to the warehouse.",
        "The data is in a legacy warehouse, PostgreSQL 18, schema wh (on the search path). Every class and",
        "property below says where it is stored. Join a reference column to the identity column of the class it",
        "references; finer tables (one row per list item or set member) repeat their owner's row, so count",
        "distinct identities when counting owners.",
        "",
    ]
    for name in w.CLASSES:
        c = wh.cls(name)
        t = wh.identity[name]
        sk = wh.sk(name)
        keys = ", ".join(f"{k} = {col.name}" for k, col in zip(c.meta.key, wh.key_cols(name), strict=True))
        doc = " ".join(c.doc.split())
        grain = (
            f"stored in {t.name}, one row per {name}"
            if t.shape == "entity"
            else f"stored in {t.name}, one row per {name} and product it names (a {name} with no product has one"
            " row with empty product columns)"
        )
        lines += [f"Class {name}: {doc}", f"  {grain}; identity {sk.name}; business key {keys}."]
        for f in c.fields:
            if f.derived is not None:
                continue
            term = f"{name}.{f.name}"
            lines.append(f"  - {f.name} ({_kind(f)}, {_card(f)}): {_desc(f)}".rstrip())
            lines += [f"      {x}" for x in _storage(term, f, where, flags, tables, domains, wh)]
        lines.append("")
    lines.append("Unused legacy columns (always empty, mapped to nothing): " + ", ".join(legacy) + ".")
    return "\n".join(lines) + "\n"


def _kind(f: FieldSpec) -> str:
    if f.kind is Kind.REF:
        assert f.target is not None
        return f"reference to {f.target.__name__}"
    if f.kind is Kind.EMBEDDED:
        assert f.target is not None
        return f"embedded {f.target.__name__}"
    if f.kind is Kind.ENUM:
        assert f.enum is not None
        return f"{f.enum.__name__} term"
    if f.scalar is not None:
        return {"str": "text", "Decimal": "decimal", "int": "integer", "bool": "boolean", "date": "date"}.get(
            f.scalar.__name__, f.scalar.__name__
        )
    return "value"


def _column_text(t: dict[str, Any], c: dict[str, Any], domains: dict[str, dict[str, Any]]) -> str:
    out = f"{t['table']}.{c['name']}"
    tr = c.get("transform")
    if c["role"] == "ref":
        out += f" references {c['references']} ({c['references']} identity)"
    elif tr == "flag":
        out += " as Y/N"
    elif tr == "date_int":
        out += " as an integer YYYYMMDD"
    elif tr == "code":
        out += f" as a code ({_domain_text(domains[c['code_domain']])})"
    elif tr == "seq":
        out += " (position, 1-based)"
    return out


def _storage(
    term: str,
    f: FieldSpec,
    where: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    flags: dict[str, list[tuple[str, str, str]]],
    tables: dict[str, dict[str, Any]],
    domains: dict[str, dict[str, Any]],
    wh: w.Warehouse,
) -> list[str]:
    if term in flags:
        cols = ", ".join(f"{c} ({m})" for _, c, m in flags[term])
        return [f"stored as Y/N flag columns on {flags[term][0][0]}: {cols}."]
    if f.kind is Kind.EMBEDDED:
        assert f.target is not None
        holder = next(x for x in tables.values() if x["grain"] == term)
        owner = next(c for c in holder["columns"] if c["role"] == "owner")
        owner_cls = term.split(".", maxsplit=1)[0]
        out = [f"stored in {holder['table']} (one row per {f.name} item), {owner['name']} is the owning {owner_cls}."]
        for g in wh.spec.of(f.target).fields:
            sub = f"{term}.{g.name}"
            if sub in where:
                t, c = where[sub]
                out.append(f"{g.name} ({_kind(g)}, {_card(g)}): {_desc(g)} Stored in {_column_text(t, c, domains)}.")
            else:
                nested = next(x for x in tables.values() if x["grain"] == sub)
                out.append(
                    f"{g.name} ({_kind(g)}, {_card(g)}): {_desc(g)} Stored in {nested['table']} (one row per item),"
                    " keyed by the owner and position."
                )
        return out
    if term in where:
        t, c = where[term]
        if t["grain"] == term:  # a multi-valued property in its own table
            owner = next(x for x in t["columns"] if x["role"] == "owner")
            return [
                f"stored in {t['table']} (one row per {term.split('.', maxsplit=1)[0]} and value), {owner['name']} is"
                " the owner;"
                f" {_column_text(t, c, domains)}."
            ]
        return [f"stored in {_column_text(t, c, domains)}."]
    return []


def describe(root: Path, context: str) -> str:
    if context == "W-raw":
        return warehouse_text(root)
    if context == "SV":
        return semantic_text(root)
    if context == "ONT":
        return ontology_text(root)
    raise ValueError(context)
