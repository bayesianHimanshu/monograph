"""H1: a legacy-style warehouse generated from the H6 source through an explicit, versioned mapping.

The warehouse holds exactly the canonical data (the H2 classes, identity on) in a deliberately realistic
representation: cryptic names, integer surrogate keys, codes instead of labels (NCI Thesaurus codes for routes
and dosage forms, decoded through one generic code table), Y/N flags, recall class as 1/2/3, integer YYYYMMDD
dates in some tables, one wide denormalized recall table, seriousness criteria as flag columns, unused legacy
columns and no comments. Fact tables sit at a finer grain than their entities.

`LAYOUT` is the naming table: every warehouse table and column, and the ontology term each column holds. From
it and the `SchemaSpec` the generator renders four artifacts (docs/h1/PLAN.md):

* `h1.warehouse.mapping.json`: the mapping, class and property to table and column with the transform;
* `h1.warehouse.ddl.sql`: schema `wh`;
* `h1.warehouse.load.sql`: `INSERT ... SELECT` from the canonical schema `h2`, plus the code table rows;
* `h1.warehouse.inverse.sql`: schemas `h1_eq_wh` and `h1_eq_h2`, one view per canonical table keyed by
  business keys, rebuilt from the warehouse and read from the canonical tables, for the round-trip test.

`analyze` refuses a layout that leaves an in-scope property unmapped, maps one twice, or names a property the
spec does not have.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

from monograph.core.spec import Card, ClassSpec, FieldSpec, Kind, SchemaSpec
from monograph.gen.ddl import child_table, column_name, table_name

WAREHOUSE_VERSION = "1.0.0"
SCHEMA = "wh"
SOURCE = "h2"  # the canonical schema the warehouse is loaded from
EQ_WH, EQ_H2 = "h1_eq_wh", "h1_eq_h2"

MAPPING_FILE = "h1.warehouse.mapping.json"
DDL_FILE = "h1.warehouse.ddl.sql"
LOAD_FILE = "h1.warehouse.load.sql"
INVERSE_FILE = "h1.warehouse.inverse.sql"

# The classes the warehouse carries: the H2 data (no H4 extraction views).
CLASSES: tuple[str, ...] = (
    "Substance",
    "Organization",
    "ClinicalDrug",
    "Product",
    "PackagedProduct",
    "Label",
    "AdverseEventReport",
    "Recall",
)

# ------------------------------------------------------------------------------------------- codes

REF_CD = "T_REF_CD"


@dataclass(frozen=True, slots=True)
class CodeDomain:
    """A coded enum: stored as `code`, decoded through T_REF_CD (`table=True`) or by the mapping alone."""

    domain: str
    enum: str
    codes: tuple[tuple[str, str], ...]  # (code, label = the enum value)
    table: bool = True

    def code_of(self, label: str) -> str:
        return dict((lbl, c) for c, lbl in self.codes)[label]


def nci_codes(enum: type[StrEnum]) -> tuple[tuple[str, str], ...]:
    """(NCI code, value) for every member, read from the H6 source, where each member carries its NCI
    Thesaurus code as a trailing comment (`ORAL = "ORAL"  # C38288`)."""
    source = textwrap.dedent(inspect.getsource(enum))
    lines = source.splitlines()
    out: dict[str, str] = {}
    for node in ast.parse(source).body[0].body:  # type: ignore[attr-defined]
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            span = lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
            m = next((m for line in span if (m := re.search(r"#\s*(C\d+)\s*$", line))), None)
            if m:
                out[node.value.value] = m.group(1)
    values = [m.value for m in enum]
    missing = [v for v in values if v not in out]
    if missing:
        raise ValueError(f"{enum.__name__}: no NCI code for {missing[:5]}")
    if len(set(out.values())) != len(out):
        raise ValueError(f"{enum.__name__}: NCI codes are not unique")
    return tuple((out[v], v) for v in values)


def code_domains() -> tuple[CodeDomain, ...]:
    from monograph.core.models import (
        AliasSource,
        DosageForm,
        DrugRole,
        MappingSource,
        ProductType,
        RecallClassification,
        Route,
        SourceSystem,
    )

    def hand(domain: str, enum: type[StrEnum], codes: dict[str, str], table: bool = True) -> CodeDomain:
        if set(codes) != {m.value for m in enum}:
            raise ValueError(f"{domain}: the codes must cover exactly the members of {enum.__name__}")
        return CodeDomain(domain, enum.__name__, tuple((c, v) for v, c in codes.items()), table)

    return (
        CodeDomain("RTE", "Route", nci_codes(Route)),
        CodeDomain("DSG_FRM", "DosageForm", nci_codes(DosageForm)),
        hand(
            "PRD_TYP",
            ProductType,
            {
                "HUMAN PRESCRIPTION DRUG": "HRX",
                "HUMAN OTC DRUG": "HOTC",
                "PLASMA DERIVATIVE": "PLSM",
                "CELLULAR THERAPY": "CELL",
                "VACCINE": "VACC",
                "STANDARDIZED ALLERGENIC": "SALG",
                "NON-STANDARDIZED ALLERGENIC": "NALG",
                "BULK INGREDIENT": "BULK",
                "DRUG FOR FURTHER PROCESSING": "DFFP",
                "LICENSED VACCINE BULK INTERMEDIATE": "LVBI",
            },
        ),
        hand("SRC_SYS", SourceSystem, {"openfda_e2b": "E2B", "faers_qde": "QDE"}),
        hand(
            "DRG_ROLE",
            DrugRole,
            {
                "primary suspect": "PS",
                "secondary suspect": "SS",
                "suspect": "SU",
                "concomitant": "C",
                "interacting": "I",
            },
        ),
        hand("ALIAS_SRC", AliasSource, {"ndc_directory": "NDCD", "enforcement": "ENF", "rxnav": "RXNV"}),
        hand("MAP_SRC", MappingSource, {"openfda_ndc": "OFDA", "rxnav_ndcproperties": "RXNP"}),
        hand("RCL_CLS", RecallClassification, {"I": "1", "II": "2", "III": "3"}, table=False),
    )


def domain(name: str) -> CodeDomain:
    return next(d for d in code_domains() if d.domain == name)


# --------------------------------------------------------------------------------------- the layout

Role = Literal["identity", "owner", "seq", "value", "ref", "flag", "denorm", "legacy"]
Transform = Literal["text", "numeric", "int", "date", "date_int", "flag", "code", "seq"]
Shape = Literal["entity", "child", "enum_set", "ref_set", "nested_ref_set", "wide"]


@dataclass(frozen=True, slots=True)
class Col:
    name: str
    sqltype: str
    role: Role
    term: str | None = None  # "Product.dosage_form"; for identity/owner the class; for flags the member
    transform: Transform = "text"
    code: str | None = None  # code domain, for transform "code"
    nullable: bool = False
    via: str | None = None  # denorm: the reference followed ("Recall.recalling_firm")


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    shape: Shape
    grain: str  # what one row is: "Product", "Product.routes", "Recall x Recall.products"
    cls: str  # the entity class the table belongs to
    field: str | None  # the property it holds (child, enum_set, ref_set); "drugs.harmonized_clinical_drugs"
    cols: tuple[Col, ...]
    pk: tuple[str, ...] = ()


def _c(name: str, sqltype: str, role: Role, term: str | None = None, **kw: Any) -> Col:
    return Col(name, sqltype, role, term, **kw)


LAYOUT: tuple[Table, ...] = (
    Table(
        "T_SUBST_MSTR",
        "entity",
        "Substance",
        "Substance",
        None,
        (
            _c("SUBST_SK", "integer", "identity", "Substance"),
            _c("UNII_CD", "varchar(10)", "value", "Substance.unii"),
            _c("SUBST_NM", "varchar(400)", "value", "Substance.name"),
            _c("LGCY_SUBST_ID", "varchar(20)", "legacy", nullable=True),
        ),
        ("SUBST_SK",),
    ),
    Table(
        "X_SALT_MOIETY",
        "child",
        "Substance.active_moiety",
        "Substance",
        "active_moiety",
        (
            _c("SUBST_SK", "integer", "owner", "Substance"),
            _c("MOIETY_SUBST_SK", "integer", "ref", "Substance.active_moiety.moiety"),
            _c("SRC_TXT", "varchar(200)", "value", "Substance.active_moiety.source"),
        ),
        ("SUBST_SK",),
    ),
    Table(
        "T_ORG_MSTR",
        "entity",
        "Organization",
        "Organization",
        None,
        (
            _c("ORG_SK", "integer", "identity", "Organization"),
            _c("ORG_CD", "varchar(200)", "value", "Organization.slug"),
            _c("ORG_NM", "varchar(400)", "value", "Organization.name"),
            _c("DUNS_NO", "varchar(9)", "legacy", nullable=True),
        ),
        ("ORG_SK",),
    ),
    Table(
        "X_ORG_ALIAS",
        "child",
        "Organization.aliases",
        "Organization",
        "aliases",
        (
            _c("ORG_SK", "integer", "owner", "Organization"),
            _c("ALIAS_NM", "varchar(400)", "value", "Organization.aliases.name"),
            _c("SRC_CD", "varchar(8)", "value", "Organization.aliases.source", transform="code", code="ALIAS_SRC"),
            _c("EVID_TXT", "text", "value", "Organization.aliases.evidence"),
        ),
        ("ORG_SK", "ALIAS_NM"),
    ),
    Table(
        "T_CLIN_DRG",
        "entity",
        "ClinicalDrug",
        "ClinicalDrug",
        None,
        (
            _c("CLIN_DRG_SK", "integer", "identity", "ClinicalDrug"),
            _c("RXCUI", "varchar(12)", "value", "ClinicalDrug.rxcui"),
            _c("CLIN_DRG_NM", "text", "value", "ClinicalDrug.name", nullable=True),
            _c("TTY_CD", "varchar(8)", "value", "ClinicalDrug.term_type", nullable=True),
        ),
        ("CLIN_DRG_SK",),
    ),
    Table(
        "X_BRND_GNRC",
        "child",
        "ClinicalDrug.generic",
        "ClinicalDrug",
        "generic",
        (
            _c("BRND_CLIN_DRG_SK", "integer", "owner", "ClinicalDrug"),
            _c("GNRC_CLIN_DRG_SK", "integer", "ref", "ClinicalDrug.generic.drug"),
            _c("SRC_TXT", "varchar(200)", "value", "ClinicalDrug.generic.source"),
        ),
        ("BRND_CLIN_DRG_SK",),
    ),
    Table(
        "T_PRD_MSTR",
        "entity",
        "Product",
        "Product",
        None,
        (
            _c("PRD_SK", "integer", "identity", "Product"),
            _c("PRD_CD", "varchar(12)", "value", "Product.product_ndc"),
            _c("PRD_NM", "varchar(400)", "value", "Product.name"),
            _c("DSG_FRM_CD", "varchar(8)", "value", "Product.dosage_form", transform="code", code="DSG_FRM"),
            _c("PRD_TYP_CD", "varchar(8)", "value", "Product.product_type", transform="code", code="PRD_TYP"),
            _c("FNSHD_FLG", "char(1)", "value", "Product.finished", transform="flag"),
            _c("RXN_CLIN_DRG_SK", "integer", "ref", "Product.clinical_drug", nullable=True),
            _c("OLD_PRD_CD", "varchar(12)", "legacy", nullable=True),
        ),
        ("PRD_SK",),
    ),
    Table(
        "T_PRD_RTE",
        "enum_set",
        "Product.routes",
        "Product",
        "routes",
        (
            _c("PRD_SK", "integer", "owner", "Product"),
            _c("RTE_CD", "varchar(8)", "value", "Product.routes", transform="code", code="RTE"),
        ),
        ("PRD_SK", "RTE_CD"),
    ),
    Table(
        "T_PRD_INGR",
        "child",
        "Product.ingredients",
        "Product",
        "ingredients",
        (
            _c("PRD_SK", "integer", "owner", "Product"),
            _c("INGR_SEQ", "integer", "seq", "Product.ingredients", transform="seq"),
            _c("SUBST_SK", "integer", "ref", "Product.ingredients.substance"),
            _c("STRNTH_VAL", "numeric", "value", "Product.ingredients.strength_value", transform="numeric"),
            _c("STRNTH_UOM", "varchar(60)", "value", "Product.ingredients.strength_unit"),
        ),
        ("PRD_SK", "INGR_SEQ"),
    ),
    Table(
        "X_NDC_RXN",
        "child",
        "Product.drug_mappings",
        "Product",
        "drug_mappings",
        (
            _c("PRD_SK", "integer", "owner", "Product"),
            _c("MAP_SEQ", "integer", "seq", "Product.drug_mappings", transform="seq"),
            _c("CLIN_DRG_SK", "integer", "ref", "Product.drug_mappings.clinical_drug"),
            _c("SRC_CD", "varchar(8)", "value", "Product.drug_mappings.source", transform="code", code="MAP_SRC"),
            _c("EVID_TXT", "text", "value", "Product.drug_mappings.evidence"),
        ),
        ("PRD_SK", "MAP_SEQ"),
    ),
    Table(
        "T_PKG",
        "entity",
        "PackagedProduct",
        "PackagedProduct",
        None,
        (
            _c("PKG_SK", "integer", "identity", "PackagedProduct"),
            _c("PKG_NDC", "varchar(14)", "value", "PackagedProduct.ndc"),
            _c("PRD_SK", "integer", "ref", "PackagedProduct.product"),
            _c("LBLR_ORG_SK", "integer", "ref", "PackagedProduct.labeler"),
            _c("PKG_DESC", "text", "value", "PackagedProduct.description", nullable=True),
            _c("PKG_NDC11", "varchar(11)", "value", "PackagedProduct.ndc11", nullable=True),
            _c("MKTG_STAT_CD", "varchar(4)", "legacy", nullable=True),
        ),
        ("PKG_SK",),
    ),
    Table(
        "T_LBL",
        "entity",
        "Label",
        "Label",
        None,
        (
            _c("LBL_SK", "integer", "identity", "Label"),
            _c("SET_ID", "varchar(36)", "value", "Label.set_id"),
            _c("VER_NO", "integer", "value", "Label.version", transform="int"),
            _c("EFF_DT", "date", "value", "Label.effective_date", transform="date"),
        ),
        ("LBL_SK",),
    ),
    Table(
        "T_LBL_PRD",
        "ref_set",
        "Label.products",
        "Label",
        "products",
        (
            _c("LBL_SK", "integer", "owner", "Label"),
            _c("PRD_SK", "integer", "ref", "Label.products"),
        ),
        ("LBL_SK", "PRD_SK"),
    ),
    Table(
        "T_LBL_SECT",
        "child",
        "Label.sections",
        "Label",
        "sections",
        (
            _c("LBL_SK", "integer", "owner", "Label"),
            _c("SECT_CD", "varchar(12)", "value", "Label.sections.code"),
            _c("SECT_TTL", "text", "value", "Label.sections.title"),
            _c("SECT_TXT", "text", "value", "Label.sections.text"),
        ),
        ("LBL_SK", "SECT_CD"),
    ),
    Table(
        "T_AE_HDR",
        "entity",
        "AdverseEventReport",
        "AdverseEventReport",
        None,
        (
            _c("AE_SK", "integer", "identity", "AdverseEventReport"),
            _c("RPT_ID", "varchar(40)", "value", "AdverseEventReport.report_id"),
            _c(
                "SRC_SYS_CD",
                "varchar(8)",
                "value",
                "AdverseEventReport.source_system",
                transform="code",
                code="SRC_SYS",
            ),
            _c("RCPT_DT", "integer", "value", "AdverseEventReport.receipt_date", transform="date_int"),
            _c("SRS_FLG", "char(1)", "value", "AdverseEventReport.serious", transform="flag"),
            _c("SRS_DTH_FLG", "char(1)", "flag", "AdverseEventReport.seriousness_criteria=death", transform="flag"),
            _c(
                "SRS_LT_FLG",
                "char(1)",
                "flag",
                "AdverseEventReport.seriousness_criteria=life threatening",
                transform="flag",
            ),
            _c(
                "SRS_HOSP_FLG",
                "char(1)",
                "flag",
                "AdverseEventReport.seriousness_criteria=hospitalization",
                transform="flag",
            ),
            _c(
                "SRS_DSBL_FLG", "char(1)", "flag", "AdverseEventReport.seriousness_criteria=disabling", transform="flag"
            ),
            _c(
                "SRS_CA_FLG",
                "char(1)",
                "flag",
                "AdverseEventReport.seriousness_criteria=congenital anomaly",
                transform="flag",
            ),
            _c("SRS_OTH_FLG", "char(1)", "flag", "AdverseEventReport.seriousness_criteria=other", transform="flag"),
            _c(
                "PT_AGE_YRS",
                "numeric",
                "value",
                "AdverseEventReport.patient_age_years",
                transform="numeric",
                nullable=True,
            ),
            _c("LGCY_BTCH_ID", "varchar(20)", "legacy", nullable=True),
        ),
        ("AE_SK",),
    ),
    Table(
        "T_AE_DRG",
        "child",
        "AdverseEventReport.drugs",
        "AdverseEventReport",
        "drugs",
        (
            _c("AE_SK", "integer", "owner", "AdverseEventReport"),
            _c("DRG_SEQ", "integer", "seq", "AdverseEventReport.drugs", transform="seq"),
            _c("DRG_NM", "text", "value", "AdverseEventReport.drugs.drug_name"),
            _c("CLIN_DRG_SK", "integer", "ref", "AdverseEventReport.drugs.clinical_drug", nullable=True),
            _c(
                "DRG_ROLE_CD", "varchar(4)", "value", "AdverseEventReport.drugs.role", transform="code", code="DRG_ROLE"
            ),
        ),
        ("AE_SK", "DRG_SEQ"),
    ),
    Table(
        "X_AE_DRG_RXN",
        "nested_ref_set",
        "AdverseEventReport.drugs.harmonized_clinical_drugs",
        "AdverseEventReport",
        "drugs.harmonized_clinical_drugs",
        (
            _c("AE_SK", "integer", "owner", "AdverseEventReport"),
            _c("DRG_SEQ", "integer", "seq", "AdverseEventReport.drugs", transform="seq"),
            _c("CLIN_DRG_SK", "integer", "ref", "AdverseEventReport.drugs.harmonized_clinical_drugs"),
        ),
        ("AE_SK", "DRG_SEQ", "CLIN_DRG_SK"),
    ),
    Table(
        "T_AE_RXN",
        "child",
        "AdverseEventReport.reactions",
        "AdverseEventReport",
        "reactions",
        (
            _c("AE_SK", "integer", "owner", "AdverseEventReport"),
            _c("RXN_SEQ", "integer", "seq", "AdverseEventReport.reactions", transform="seq"),
            _c("RXN_TRM", "varchar(400)", "value", "AdverseEventReport.reactions.term"),
        ),
        ("AE_SK", "RXN_SEQ"),
    ),
    Table(
        "T_RCL_PRD_DNRM",
        "wide",
        "Recall x Recall.products",
        "Recall",
        "products",
        (
            _c("RCL_SK", "integer", "identity", "Recall"),
            _c("RCL_NO", "varchar(20)", "value", "Recall.recall_number"),
            _c("RCL_CLS_CD", "char(1)", "value", "Recall.classification", transform="code", code="RCL_CLS"),
            _c("RCL_RSN_TXT", "text", "value", "Recall.reason"),
            _c("RPT_DT", "integer", "value", "Recall.report_date", transform="date_int"),
            _c("FIRM_ORG_SK", "integer", "ref", "Recall.recalling_firm"),
            _c("FIRM_NM", "varchar(400)", "denorm", "Organization.name", via="Recall.recalling_firm"),
            _c("PRD_SK", "integer", "ref", "Recall.products", nullable=True),
            _c("PRD_CD", "varchar(12)", "denorm", "Product.product_ndc", via="Recall.products", nullable=True),
            _c("CMNT_TXT", "text", "legacy", nullable=True),
        ),
    ),
)

TABLE_BY_NAME = {t.name: t for t in LAYOUT}


# --------------------------------------------------------------------------------------- analysis


@dataclass(frozen=True)
class Warehouse:
    """The spec and the layout, checked against each other."""

    spec: SchemaSpec
    tables: tuple[Table, ...]
    domains: tuple[CodeDomain, ...]
    identity: dict[str, Table] = field(default_factory=dict)  # class -> the table holding its identity

    def cls(self, name: str) -> ClassSpec:
        return next(c for c in self.spec.classes if c.cls.__name__ == name)

    def key_cols(self, cls: str) -> list[Col]:
        """The warehouse columns that hold a class's business key, in key order."""
        t = self.identity[cls]
        c = self.cls(cls)
        return [next(col for col in t.cols if col.term == f"{cls}.{k}") for k in c.meta.key]

    def sk(self, cls: str) -> Col:
        return next(c for c in self.identity[cls].cols if c.role == "identity")

    def domain(self, name: str) -> CodeDomain:
        return next(d for d in self.domains if d.domain == name)


def _expected_terms(spec: SchemaSpec) -> set[str]:
    """Every property the warehouse must carry: each field of each carried class, and each field of each
    embedded value under it (`Product.ingredients.substance`)."""
    out: set[str] = set()

    def walk(prefix: str, c: ClassSpec) -> None:
        for f in c.fields:
            if f.derived is not None:
                continue
            term = f"{prefix}.{f.name}"
            if f.kind is Kind.EMBEDDED:
                assert f.target is not None
                for g in spec.of(f.target).fields:
                    sub = f"{term}.{g.name}"
                    if g.kind is Kind.EMBEDDED:
                        raise ValueError(f"{sub}: embedded values nested two deep are not supported")
                    out.add(sub)
            else:
                out.add(term)

    for name in CLASSES:
        walk(name, next(c for c in spec.classes if c.cls.__name__ == name))
    return out


def analyze(spec: SchemaSpec, tables: Sequence[Table] = LAYOUT) -> Warehouse:
    expected = _expected_terms(spec)
    seen: dict[str, str] = {}
    flags: dict[str, set[str]] = {}
    identity: dict[str, Table] = {}
    for t in tables:
        for c in t.cols:
            if c.role == "identity":
                identity[t.cls] = t
            if c.role in ("value", "ref") and c.term is not None:
                if c.term in seen:
                    raise ValueError(f"{c.term} is mapped twice ({seen[c.term]} and {t.name}.{c.name})")
                seen[c.term] = f"{t.name}.{c.name}"
            if c.role == "flag" and c.term is not None:
                term, _, member = c.term.partition("=")
                flags.setdefault(term, set()).add(member)
    for term, members in flags.items():
        prop = _field(spec, term)
        assert prop.enum is not None
        if members != {m.value for m in prop.enum}:
            raise ValueError(f"{term}: the flag columns must cover every member")
        seen[term] = "flags"
    unknown = set(seen) - expected
    missing = expected - set(seen)
    if unknown:
        raise ValueError(f"the layout maps properties the spec does not have: {sorted(unknown)}")
    if missing:
        raise ValueError(f"the layout leaves properties unmapped: {sorted(missing)}")
    if set(identity) != set(CLASSES):
        raise ValueError(f"classes without an identity table: {sorted(set(CLASSES) - set(identity))}")
    return Warehouse(spec, tuple(tables), code_domains(), identity)


def _field(spec: SchemaSpec, term: str) -> FieldSpec:
    """The FieldSpec a term names: `Product.dosage_form` or `Product.ingredients.substance`."""
    parts = term.split(".")
    c = next(c for c in spec.classes if c.cls.__name__ == parts[0])
    f = c.field(parts[1])
    if len(parts) == 3:
        assert f.target is not None
        f = spec.of(f.target).field(parts[2])
    return f


def _target(spec: SchemaSpec, term: str) -> str:
    f = _field(spec, term)
    assert f.target is not None, term
    return f.target.__name__


# ------------------------------------------------------------------------------------------ DDL


def render_ddl(wh: Warehouse) -> str:
    out = [
        f"-- Generated by monograph.gen.warehouse {WAREHOUSE_VERSION} from the H6 source. Do not edit.",
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};",
        "",
        f"CREATE TABLE {SCHEMA}.{REF_CD} (",
        "    CD_DOM varchar(10) NOT NULL,",
        "    CD_VAL varchar(16) NOT NULL,",
        "    CD_DSC varchar(200) NOT NULL,",
        "    SORT_ORD integer,",
        "    PRIMARY KEY (CD_DOM, CD_VAL)",
        ");",
    ]
    for t in wh.tables:
        lines = [f"    {c.name} {c.sqltype}{'' if c.nullable else ' NOT NULL'}" for c in t.cols]
        if t.pk:
            lines.append(f"    PRIMARY KEY ({', '.join(t.pk)})")
        out += ["", f"CREATE TABLE {SCHEMA}.{t.name} (", ",\n".join(lines), ");"]
    # indexes the load and the agents' joins need (a legacy warehouse has them too)
    out.append("")
    for t in wh.tables:
        for c in t.cols:
            if c.role in ("owner", "ref") or (c.role == "value" and c.term in _key_terms(wh)):
                out.append(f"CREATE INDEX ON {SCHEMA}.{t.name} ({c.name});")
    return "\n".join(out) + "\n"


def _key_terms(wh: Warehouse) -> set[str]:
    return {f"{n}.{k}" for n in CLASSES for k in wh.cls(n).meta.key}


# ----------------------------------------------------------------------------------------- load


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _encode(wh: Warehouse, c: Col, expr: str) -> str:
    """Canonical value -> warehouse representation."""
    if c.transform == "flag":
        return f"CASE WHEN {expr} THEN 'Y' ELSE 'N' END"
    if c.transform == "date_int":
        return f"to_char({expr}, 'YYYYMMDD')::integer"
    if c.transform == "seq":
        return f"{expr} + 1"
    if c.transform == "code":
        assert c.code is not None
        d = wh.domain(c.code)
        if d.table:
            return f"(SELECT r.CD_VAL FROM {SCHEMA}.{REF_CD} r WHERE r.CD_DOM = {_lit(d.domain)} AND r.CD_DSC = {expr})"
        cases = " ".join(f"WHEN {_lit(label)} THEN {_lit(code)}" for code, label in d.codes)
        return f"CASE {expr} {cases} END"
    return expr


def _decode(wh: Warehouse, c: Col, expr: str) -> str:
    """Warehouse representation -> canonical value."""
    if c.transform == "flag":
        return f"({expr} = 'Y')"
    if c.transform == "date_int":
        return f"to_date({expr}::text, 'YYYYMMDD')"
    if c.transform == "seq":
        return f"{expr} - 1"
    if c.transform == "code":
        assert c.code is not None
        d = wh.domain(c.code)
        if d.table:
            return f"(SELECT r.CD_DSC FROM {SCHEMA}.{REF_CD} r WHERE r.CD_DOM = {_lit(d.domain)} AND r.CD_VAL = {expr})"
        cases = " ".join(f"WHEN {_lit(code)} THEN {_lit(label)}" for code, label in d.codes)
        return f"CASE {expr} {cases} END"
    return expr


def _key_match(wh: Warehouse, cls: str, w: str, h: str) -> str:
    c = wh.cls(cls)
    return " AND ".join(
        f"{w}.{col.name} = {h}.{column_name(c.field(k))}" for col, k in zip(wh.key_cols(cls), c.meta.key, strict=True)
    )


def _sk_of(wh: Warehouse, cls: str, iri: str) -> str:
    """The surrogate key of the canonical row with this IRI."""
    t, c = wh.identity[cls], wh.cls(cls)
    canonical = f"{SOURCE}.{table_name(c.cls)}"
    return (
        f"(SELECT w.{wh.sk(cls).name} FROM {SCHEMA}.{t.name} w JOIN {canonical} h ON {_key_match(wh, cls, 'w', 'h')}"
        f" WHERE h.iri = {iri} LIMIT 1)"
    )


def _order(wh: Warehouse, cls: str, alias: str) -> str:
    c = wh.cls(cls)

    def one(k: str) -> str:
        collate = ' COLLATE "C"' if c.field(k).scalar is str else ""
        return f"{alias}.{column_name(c.field(k))}{collate}"

    return ", ".join(one(k) for k in c.meta.key)


def _canonical_column(term: str, spec: SchemaSpec) -> str:
    f = _field(spec, term)
    return column_name(f)


def _load_table(wh: Warehouse, t: Table) -> str:
    spec = wh.spec
    cls = wh.cls(t.cls)
    owner_table = table_name(cls.cls)
    names = ", ".join(c.name for c in t.cols)
    exprs: list[str] = []
    if t.shape in ("entity", "wide"):
        src = "c"
        for c in t.cols:
            if c.role == "identity":
                if t.shape == "wide":
                    exprs.append(f"dense_rank() OVER (ORDER BY {_order(wh, t.cls, 'c')})")
                else:
                    exprs.append(f"row_number() OVER (ORDER BY {_order(wh, t.cls, 'c')})")
            elif c.role == "value":
                assert c.term is not None
                exprs.append(_encode(wh, c, f"c.{_canonical_column(c.term, spec)}"))
            elif c.role == "flag":
                assert c.term is not None
                term, _, member = c.term.partition("=")
                exprs.append(_encode(wh, c, f"{_lit(member)} = ANY(c.{_canonical_column(term, spec)})"))
            elif c.role == "ref" and c.term is not None and c.term == f"{t.cls}.{t.field}" and t.shape == "wide":
                exprs.append(_sk_of(wh, _target(spec, c.term), "rp.target_iri"))
            elif c.role == "ref":
                assert c.term is not None
                exprs.append(_sk_of(wh, _target(spec, c.term), f"c.{_canonical_column(c.term, spec)}"))
            elif c.role == "denorm":
                assert c.term is not None
                assert c.via is not None
                target_cls, prop = c.term.split(".")
                via_iri = "rp.target_iri" if c.via == f"{t.cls}.{t.field}" else f"c.{_canonical_column(c.via, spec)}"
                target = table_name(wh.cls(target_cls).cls)
                col = column_name(wh.cls(target_cls).field(prop))
                exprs.append(f"(SELECT d.{col} FROM {SOURCE}.{target} d WHERE d.iri = {via_iri})")
            else:
                exprs.append("NULL")
        frm = f"{SOURCE}.{owner_table} {src}"
        if t.shape == "wide":
            assert t.field is not None
            bridge = child_table(owner_table, cls.field(t.field))
            frm += f" LEFT JOIN {SOURCE}.{bridge} rp ON rp.owner_iri = c.iri"
        return f"INSERT INTO {SCHEMA}.{t.name} ({names})\nSELECT {', '.join(exprs)}\nFROM {frm};"
    if t.shape == "enum_set":
        assert t.field is not None
        col = column_name(cls.field(t.field))
        for c in t.cols:
            exprs.append(_sk_of(wh, t.cls, "o.iri") if c.role == "owner" else _encode(wh, c, "v.val"))
        frm = f"{SOURCE}.{owner_table} o CROSS JOIN LATERAL unnest(o.{col}) AS v(val)"
        return f"INSERT INTO {SCHEMA}.{t.name} ({names})\nSELECT {', '.join(exprs)}\nFROM {frm};"
    if t.shape == "ref_set":
        assert t.field is not None
        bridge = child_table(owner_table, cls.field(t.field))
        for c in t.cols:
            if c.role == "owner":
                exprs.append(_sk_of(wh, t.cls, "b.owner_iri"))
            else:
                assert c.term is not None
                exprs.append(_sk_of(wh, _target(spec, c.term), "b.target_iri"))
        return f"INSERT INTO {SCHEMA}.{t.name} ({names})\nSELECT {', '.join(exprs)}\nFROM {SOURCE}.{bridge} b;"
    if t.shape == "child":
        assert t.field is not None
        f = cls.field(t.field)
        child = child_table(owner_table, f)
        for c in t.cols:
            if c.role == "owner":
                exprs.append(_sk_of(wh, t.cls, "x.owner_iri"))
            elif c.role == "seq":
                exprs.append(_encode(wh, c, "x.position"))
            elif c.role == "ref":
                assert c.term is not None
                exprs.append(_sk_of(wh, _target(spec, c.term), f"x.{_canonical_column(c.term, spec)}"))
            elif c.role == "value":
                assert c.term is not None
                exprs.append(_encode(wh, c, f"x.{_canonical_column(c.term, spec)}"))
            else:
                exprs.append("NULL")
        return f"INSERT INTO {SCHEMA}.{t.name} ({names})\nSELECT {', '.join(exprs)}\nFROM {SOURCE}.{child} x;"
    if t.shape == "nested_ref_set":
        assert t.field is not None
        outer, inner = t.field.split(".")
        f = cls.field(outer)
        child = child_table(owner_table, f)
        assert f.target is not None
        bridge = child_table(child, wh.spec.of(f.target).field(inner))
        for c in t.cols:
            if c.role == "owner":
                exprs.append(_sk_of(wh, t.cls, "d.owner_iri"))
            elif c.role == "seq":
                exprs.append(_encode(wh, c, "d.position"))
            else:
                assert c.term is not None
                exprs.append(_sk_of(wh, _target(spec, c.term), "b.target_iri"))
        frm = f"{SOURCE}.{bridge} b JOIN {SOURCE}.{child} d ON d.iri = b.owner_iri"
        return f"INSERT INTO {SCHEMA}.{t.name} ({names})\nSELECT {', '.join(exprs)}\nFROM {frm};"
    raise ValueError(t.shape)


def _load_order(wh: Warehouse) -> list[Table]:
    """Entity tables first, in reference order, then everything that points at them."""
    entities = [t for t in wh.tables if t.shape in ("entity", "wide")]
    rest = [t for t in wh.tables if t.shape not in ("entity", "wide")]
    ordered: list[Table] = []
    placed: set[str] = set()
    while entities:
        for t in list(entities):
            needs = {_target(wh.spec, c.term) for c in t.cols if c.role == "ref" and c.term is not None} - {t.cls}
            if needs <= placed:
                ordered.append(t)
                placed.add(t.cls)
                entities.remove(t)
                break
        else:
            raise ValueError("the entity tables reference each other in a cycle")
    return ordered + rest


def render_load(wh: Warehouse) -> str:
    out = [
        f"-- Generated by monograph.gen.warehouse {WAREHOUSE_VERSION} from the H6 source and the mapping. Do not edit.",
        f"-- Loads schema {SCHEMA} from the canonical schema {SOURCE}.",
        "",
    ]
    rows = [
        f"({_lit(d.domain)}, {_lit(code)}, {_lit(label)})" for d in wh.domains if d.table for code, label in d.codes
    ]
    out.append(f"INSERT INTO {SCHEMA}.{REF_CD} (CD_DOM, CD_VAL, CD_DSC) VALUES\n" + ",\n".join(rows) + ";")
    for t in _load_order(wh):
        out += ["", _load_table(wh, t)]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------- the round trip


@dataclass(frozen=True, slots=True)
class EqView:
    """One canonical table, keyed by business keys: the same columns from both sides."""

    name: str
    columns: tuple[str, ...]
    from_h2: str
    from_wh: str


def _key_select(wh: Warehouse, cls: str, iri: str, prefix: str) -> list[tuple[str, str]]:
    """(column, canonical expression) for the business key of the row with this IRI."""
    c = wh.cls(cls)
    t = table_name(c.cls)
    return [
        (f"{prefix}{k}", f"(SELECT k.{column_name(c.field(k))} FROM {SOURCE}.{t} k WHERE k.iri = {iri})")
        for k in c.meta.key
    ]


def _wh_key_select(wh: Warehouse, cls: str, sk: str, prefix: str) -> list[tuple[str, str]]:
    t = wh.identity[cls]
    return [
        (
            f"{prefix}{k}",
            f"(SELECT k.{col.name} FROM {SCHEMA}.{t.name} k WHERE k.{wh.sk(cls).name} = {sk} LIMIT 1)",
        )
        for k, col in zip(wh.cls(cls).meta.key, wh.key_cols(cls), strict=True)
    ]


def _sorted_array(expr: str) -> str:
    return f"(SELECT coalesce(array_agg(u ORDER BY u COLLATE \"C\"), '{{}}') FROM unnest({expr}) AS u)"


def eq_views(wh: Warehouse) -> list[EqView]:
    views: list[EqView] = []
    for name in CLASSES:
        c = wh.cls(name)
        t = wh.identity[name]
        owner = table_name(c.cls)
        h2_cols: list[tuple[str, str]] = []
        wh_cols: list[tuple[str, str]] = []
        for f in c.fields:
            if f.derived is not None or f.kind is Kind.EMBEDDED:
                continue
            term = f"{name}.{f.name}"
            if f.kind is Kind.REF and f.card in (Card.ONE, Card.OPTIONAL):
                assert f.target is not None
                target = f.target.__name__
                h2_cols += _key_select(wh, target, f"c.{column_name(f)}", f"{f.name}__")
                col = next(x for x in t.cols if x.term == term)
                wh_cols += _wh_key_select(wh, target, f"w.{col.name}", f"{f.name}__")
            elif f.kind is Kind.REF:
                continue  # a reference set: its own view below
            elif f.kind is Kind.ENUM and f.is_collection:
                h2_cols.append((f.name, _sorted_array(f"c.{column_name(f)}")))
                holder = next((x for x in wh.tables if x.shape == "enum_set" and x.grain == term), None)
                if holder is not None:
                    owner_col = next(x for x in holder.cols if x.role == "owner")
                    code_col = next(x for x in holder.cols if x.role == "value")
                    inner = f"SELECT {_decode(wh, code_col, 'e.' + code_col.name)} FROM {SCHEMA}.{holder.name} e"
                    inner += f" WHERE e.{owner_col.name} = w.{wh.sk(name).name}"
                    wh_cols.append((f.name, _sorted_array(f"ARRAY({inner})")))
                else:
                    flags = [x for x in t.cols if x.role == "flag" and x.term and x.term.startswith(term + "=")]
                    items = ", ".join(
                        f"CASE WHEN w.{x.name} = 'Y' THEN {_lit(x.term.partition('=')[2])} END"  # type: ignore[union-attr]
                        for x in flags
                    )
                    wh_cols.append((f.name, _sorted_array(f"array_remove(ARRAY[{items}]::text[], NULL)")))
            else:
                h2_cols.append((f.name, f"c.{column_name(f)}"))
                col = next(x for x in t.cols if x.term == term)
                wh_cols.append((f.name, _decode(wh, col, f"w.{col.name}")))
        cols = tuple(n for n, _ in h2_cols)
        assert cols == tuple(n for n, _ in wh_cols)
        h2_sql = f"SELECT {', '.join(f'{e} AS {n}' for n, e in h2_cols)} FROM {SOURCE}.{owner} c"
        distinct = "DISTINCT " if t.shape == "wide" else ""
        wh_sql = f"SELECT {distinct}{', '.join(f'{e} AS {n}' for n, e in wh_cols)} FROM {SCHEMA}.{t.name} w"
        views.append(EqView(owner, cols, h2_sql, wh_sql))
        for f in c.fields:
            if f.derived is not None:
                continue
            if f.kind is Kind.EMBEDDED:
                views.append(_eq_child(wh, name, f))
            elif f.kind is Kind.REF and f.is_collection:
                views.append(_eq_ref_set(wh, name, f))
    views += _eq_nested(wh)
    return views


def _eq_child(wh: Warehouse, name: str, f: FieldSpec) -> EqView:
    spec = wh.spec
    c = wh.cls(name)
    owner = table_name(c.cls)
    child = child_table(owner, f)
    holder = next(t for t in wh.tables if t.grain == f"{name}.{f.name}")
    assert f.target is not None
    emb = spec.of(f.target)
    h2_cols = _key_select(wh, name, "x.owner_iri", "owner__")
    owner_col = next(x for x in holder.cols if x.role == "owner")
    wh_cols = _wh_key_select(wh, name, f"w.{owner_col.name}", "owner__")
    if f.card is Card.LIST:
        seq = next(x for x in holder.cols if x.role == "seq")
        h2_cols.append(("position", "x.position"))
        wh_cols.append(("position", _decode(wh, seq, f"w.{seq.name}")))
    for g in emb.fields:
        if g.kind is Kind.REF and g.is_collection:
            continue  # a reference set inside the embedded value: its own table (nested_ref_set)
        term = f"{name}.{f.name}.{g.name}"
        col = next(x for x in holder.cols if x.term == term)
        if g.kind is Kind.REF:
            assert g.target is not None
            h2_cols += _key_select(wh, g.target.__name__, f"x.{column_name(g)}", f"{g.name}__")
            wh_cols += _wh_key_select(wh, g.target.__name__, f"w.{col.name}", f"{g.name}__")
        else:
            h2_cols.append((g.name, f"x.{column_name(g)}"))
            wh_cols.append((g.name, _decode(wh, col, f"w.{col.name}")))
    cols = tuple(n for n, _ in h2_cols)
    return EqView(
        child,
        cols,
        f"SELECT {', '.join(f'{e} AS {n}' for n, e in h2_cols)} FROM {SOURCE}.{child} x",
        f"SELECT {', '.join(f'{e} AS {n}' for n, e in wh_cols)} FROM {SCHEMA}.{holder.name} w",
    )


def _eq_ref_set(wh: Warehouse, name: str, f: FieldSpec) -> EqView:
    c = wh.cls(name)
    owner = table_name(c.cls)
    bridge = child_table(owner, f)
    holder = next(t for t in wh.tables if t.grain == f"{name}.{f.name}" or (t.field == f.name and t.cls == name))
    assert f.target is not None
    target = f.target.__name__
    h2_cols = _key_select(wh, name, "b.owner_iri", "owner__") + _key_select(wh, target, "b.target_iri", "target__")
    ref = next(x for x in holder.cols if x.term == f"{name}.{f.name}")
    if holder.shape == "wide":
        owner_sk = wh.sk(name)
        wh_cols = _wh_key_select(wh, name, f"w.{owner_sk.name}", "owner__")
        where = f" WHERE w.{ref.name} IS NOT NULL"
        distinct = "DISTINCT "
    else:
        owner_col = next(x for x in holder.cols if x.role == "owner")
        wh_cols = _wh_key_select(wh, name, f"w.{owner_col.name}", "owner__")
        where, distinct = "", ""
    wh_cols += _wh_key_select(wh, target, f"w.{ref.name}", "target__")
    return EqView(
        bridge,
        tuple(n for n, _ in h2_cols),
        f"SELECT {', '.join(f'{e} AS {n}' for n, e in h2_cols)} FROM {SOURCE}.{bridge} b",
        f"SELECT {distinct}{', '.join(f'{e} AS {n}' for n, e in wh_cols)} FROM {SCHEMA}.{holder.name} w{where}",
    )


def _eq_nested(wh: Warehouse) -> list[EqView]:
    out = []
    for t in wh.tables:
        if t.shape != "nested_ref_set":
            continue
        assert t.field is not None
        outer, inner = t.field.split(".")
        c = wh.cls(t.cls)
        f = c.field(outer)
        assert f.target is not None
        g = wh.spec.of(f.target).field(inner)
        assert g.target is not None
        child = child_table(table_name(c.cls), f)
        bridge = child_table(child, g)
        target = g.target.__name__
        h2_cols = [
            *_key_select(wh, t.cls, "d.owner_iri", "owner__"),
            ("position", "d.position"),
            *_key_select(wh, target, "b.target_iri", "target__"),
        ]
        owner_col = next(x for x in t.cols if x.role == "owner")
        seq = next(x for x in t.cols if x.role == "seq")
        ref = next(x for x in t.cols if x.role == "ref")
        wh_cols = [
            *_wh_key_select(wh, t.cls, f"w.{owner_col.name}", "owner__"),
            ("position", _decode(wh, seq, f"w.{seq.name}")),
            *_wh_key_select(wh, target, f"w.{ref.name}", "target__"),
        ]
        out.append(
            EqView(
                bridge,
                tuple(n for n, _ in h2_cols),
                f"SELECT {', '.join(f'{e} AS {n}' for n, e in h2_cols)} FROM {SOURCE}.{bridge} b"
                f" JOIN {SOURCE}.{child} d ON d.iri = b.owner_iri",
                f"SELECT {', '.join(f'{e} AS {n}' for n, e in wh_cols)} FROM {SCHEMA}.{t.name} w",
            )
        )
    return out


def render_inverse(wh: Warehouse) -> str:
    out = [
        f"-- Generated by monograph.gen.warehouse {WAREHOUSE_VERSION}. Do not edit.",
        "-- Every canonical table keyed by business keys, rebuilt from the warehouse (h1_eq_wh) and read from the",
        "-- canonical schema (h1_eq_h2). The round-trip test compares the two, row for row.",
        f"CREATE SCHEMA IF NOT EXISTS {EQ_WH};",
        f"CREATE SCHEMA IF NOT EXISTS {EQ_H2};",
    ]
    for v in eq_views(wh):
        out += ["", f"CREATE VIEW {EQ_WH}.{v.name} AS\n{v.from_wh};", f"CREATE VIEW {EQ_H2}.{v.name} AS\n{v.from_h2};"]
    return "\n".join(out) + "\n"


# -------------------------------------------------------------------------------------- mapping


def mapping(wh: Warehouse) -> dict[str, Any]:
    """The mapping as data: what the ontology context and the check read."""
    spec = wh.spec
    classes = []
    for name in CLASSES:
        c = wh.cls(name)
        t = wh.identity[name]
        classes.append(
            {
                "class": name,
                "table": t.name,
                "identity": wh.sk(name).name,
                "key": {k: col.name for k, col in zip(c.meta.key, wh.key_cols(name), strict=True)},
            }
        )
    tables = []
    for t in wh.tables:
        cols = []
        for col in t.cols:
            entry: dict[str, Any] = {"name": col.name, "type": col.sqltype, "role": col.role, "nullable": col.nullable}
            if col.term is not None:
                entry["term"] = col.term
            if col.transform != "text":
                entry["transform"] = col.transform
            if col.code is not None:
                entry["code_domain"] = col.code
            if col.role == "ref" and col.term is not None:
                entry["references"] = _target(spec, col.term)
            if col.via is not None:
                entry["via"] = col.via
            cols.append(entry)
        tables.append(
            {
                "table": t.name,
                "grain": t.grain,
                "class": t.cls,
                "property": t.field,
                "primary_key": list(t.pk),
                "columns": cols,
            }
        )
    tables.insert(
        0,
        {
            "table": REF_CD,
            "grain": "code",
            "class": None,
            "property": None,
            "primary_key": ["CD_DOM", "CD_VAL"],
            "columns": [
                {"name": "CD_DOM", "type": "varchar(10)", "role": "code_domain", "nullable": False},
                {"name": "CD_VAL", "type": "varchar(16)", "role": "code_value", "nullable": False},
                {"name": "CD_DSC", "type": "varchar(200)", "role": "code_label", "nullable": False},
                {"name": "SORT_ORD", "type": "integer", "role": "legacy", "nullable": True},
            ],
        },
    )
    domains = [
        {
            "domain": d.domain,
            "enum": d.enum,
            "in_code_table": d.table,
            "codes": [{"code": code, "label": label} for code, label in d.codes],
        }
        for d in wh.domains
    ]
    return {
        "version": WAREHOUSE_VERSION,
        "schema": SCHEMA,
        "source_schema_version": spec.version,
        "classes": classes,
        "tables": tables,
        "code_domains": domains,
    }


def render_mapping(wh: Warehouse) -> str:
    return json.dumps(mapping(wh), indent=2, ensure_ascii=False) + "\n"


def warehouse_renderers(spec: SchemaSpec, excluded: tuple[type[BaseModel], ...]) -> dict[str, Any]:
    """`generated/`-relative file name -> renderer, for the four warehouse artifacts."""
    del excluded  # the warehouse names its classes explicitly (CLASSES)
    wh = analyze(spec)
    return {
        MAPPING_FILE: lambda: render_mapping(wh),
        DDL_FILE: lambda: render_ddl(wh),
        LOAD_FILE: lambda: render_load(wh),
        INVERSE_FILE: lambda: render_inverse(wh),
    }
