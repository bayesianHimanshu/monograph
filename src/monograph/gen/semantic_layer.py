"""H1: the semantic layer over the warehouse, the strong baseline (docs/h1/PLAN.md, "Stage 2").

Built the way enterprises build one: SQL views with business names over the legacy tables (labels decoded from
the code table, real dates, booleans for Y/N flags, the recall table brought to one row per recall), and a
semantic model (entities with keys, dimensions, measures with their grain, joins, synonyms, descriptions).

Coverage follows the rule fixed in the plan before any question existed: the analyst-facing classes Product,
PackagedProduct, Substance, Organization, AdverseEventReport and Recall, their own properties, the
non-reference properties of their embedded parts, and their direct joins. No crosswalks, no salt-to-moiety or
brand-to-generic links, no labels or label sections, no ClinicalDrug.

Three artifacts, drift-checked: `h1.semantic.views.sql`, `h1.semantic.model.yaml` and `h1.semantic.mapping.json`
(every view column back to its ontology term, for the check). Descriptions are the H6 source's where one
exists, plus hand-written ones for the views, measures and joins.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from monograph.core.spec import SchemaSpec
from monograph.gen import warehouse as w

SEMANTIC_VERSION = "1.0.0"
SCHEMA = "sem"
VIEWS_FILE = "h1.semantic.views.sql"
MODEL_FILE = "h1.semantic.model.yaml"
MAPPING_FILE = "h1.semantic.mapping.json"

COVERED_CLASSES: tuple[str, ...] = (
    "Product",
    "PackagedProduct",
    "Substance",
    "Organization",
    "AdverseEventReport",
    "Recall",
)

ColRole = Literal["key", "ref", "value", "seq"]

# The examples given for the two large code lists, in both contexts (the semantic layer and the ontology).
EXAMPLES: dict[str, tuple[str, ...]] = {
    "DosageForm": ("TABLET", "TABLET, FILM COATED", "INJECTION, SOLUTION", "CAPSULE"),
    "Route": ("ORAL", "INTRAVENOUS", "TOPICAL", "SUBCUTANEOUS"),
}


def _examples(enum: str) -> str:
    return ", ".join(f"'{x}'" for x in EXAMPLES[enum])


@dataclass(frozen=True, slots=True)
class VCol:
    name: str
    expr: str  # over the view's FROM
    role: ColRole
    term: str | None  # the ontology term ("Product.dosage_form"); for key/ref the class key's property
    type: str
    description: str
    references: str | None = None  # ref: the class referenced by business key
    synonyms: tuple[str, ...] = ()
    values: tuple[str, ...] = ()  # the allowed values, for a small coded dimension


@dataclass(frozen=True, slots=True)
class View:
    name: str
    entity: str  # the entity in the semantic model
    grain: str  # one row per ...
    cls: str  # the class (or class.property) a row stands for
    from_sql: str
    cols: tuple[VCol, ...]
    description: str
    synonyms: tuple[str, ...] = ()
    distinct: bool = False


@dataclass(frozen=True, slots=True)
class Measure:
    name: str
    view: str
    sql: str
    grain: str
    description: str
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Join:
    name: str
    left: str
    right: str
    on: str
    relationship: Literal["many_to_one", "one_to_many"]
    description: str


def _ref(dom: str, expr: str) -> str:
    return f"(SELECT cd.CD_DSC FROM {w.SCHEMA}.{w.REF_CD} cd WHERE cd.CD_DOM = '{dom}' AND cd.CD_VAL = {expr})"


def _yn(expr: str) -> str:
    return f"({expr} = 'Y')"


def _date(expr: str) -> str:
    return f"to_date({expr}::text, 'YYYYMMDD')"


def _labels(enum: str) -> tuple[str, ...]:
    d = next(x for x in w.code_domains() if x.enum == enum)
    return tuple(label for _, label in d.codes)


def _rcl_class(expr: str) -> str:
    d = next(x for x in w.code_domains() if x.domain == "RCL_CLS")
    cases = " ".join(f"WHEN '{code}' THEN '{label}'" for code, label in d.codes)
    return f"CASE {expr} {cases} END"


def _sk_key(table: str, sk: str, key: str, value: str) -> str:
    return f"(SELECT k.{key} FROM {w.SCHEMA}.{table} k WHERE k.{sk} = {value})"


def views(desc: Callable[[str], str]) -> tuple[View, ...]:
    """`desc(term)`: the H6 source's description of an ontology term."""
    wh = w.SCHEMA
    return (
        View(
            "products",
            "product",
            "one row per product (product NDC)",
            "Product",
            f"{wh}.T_PRD_MSTR p",
            (
                VCol("product_ndc", "p.PRD_CD", "key", "Product.product_ndc", "text", desc("Product.product_ndc")),
                VCol(
                    "product_name",
                    "p.PRD_NM",
                    "value",
                    "Product.name",
                    "text",
                    desc("Product.name"),
                    synonyms=("brand name", "trade name"),
                ),
                VCol(
                    "dosage_form",
                    _ref("DSG_FRM", "p.DSG_FRM_CD"),
                    "value",
                    "Product.dosage_form",
                    "text",
                    desc("Product.dosage_form")
                    + f" Values are FDA SPL dosage form terms in capitals, for example {_examples('DosageForm')}.",
                    synonyms=("form", "formulation"),
                ),
                VCol(
                    "product_type",
                    _ref("PRD_TYP", "p.PRD_TYP_CD"),
                    "value",
                    "Product.product_type",
                    "text",
                    desc("Product.product_type"),
                    synonyms=("marketing category", "Rx or OTC"),
                    values=_labels("ProductType"),
                ),
                VCol(
                    "is_finished", _yn("p.FNSHD_FLG"), "value", "Product.finished", "boolean", desc("Product.finished")
                ),
            ),
            "Drug products as listed in the FDA NDC directory, one row per product NDC (labeler and product code).",
            ("drug product", "NDC product", "drug", "medication"),
        ),
        View(
            "product_routes",
            "product_route",
            "one row per product and route of administration",
            "Product.routes",
            f"{wh}.T_PRD_RTE r JOIN {wh}.T_PRD_MSTR p ON p.PRD_SK = r.PRD_SK",
            (
                VCol("product_ndc", "p.PRD_CD", "ref", "Product.product_ndc", "text", "The product.", "Product"),
                VCol(
                    "route",
                    _ref("RTE", "r.RTE_CD"),
                    "value",
                    "Product.routes",
                    "text",
                    desc("Product.routes")
                    + f" Values are FDA SPL route terms in capitals, for example {_examples('Route')}.",
                    synonyms=("route of administration", "administration route"),
                ),
            ),
            "The routes of administration of each product. A product can have several routes, so count distinct"
            " products when counting products by route.",
        ),
        View(
            "product_ingredients",
            "product_ingredient",
            "one row per product and active ingredient",
            "Product.ingredients",
            f"{wh}.T_PRD_INGR i JOIN {wh}.T_PRD_MSTR p ON p.PRD_SK = i.PRD_SK"
            f" JOIN {wh}.T_SUBST_MSTR s ON s.SUBST_SK = i.SUBST_SK",
            (
                VCol("product_ndc", "p.PRD_CD", "ref", "Product.product_ndc", "text", "The product.", "Product"),
                VCol(
                    "substance_unii",
                    "s.UNII_CD",
                    "ref",
                    "Substance.unii",
                    "text",
                    "The active ingredient substance, by UNII.",
                    "Substance",
                ),
                VCol(
                    "strength_value",
                    "i.STRNTH_VAL",
                    "value",
                    "Product.ingredients.strength_value",
                    "numeric",
                    desc("Product.ingredients.strength_value"),
                ),
                VCol(
                    "strength_unit",
                    "i.STRNTH_UOM",
                    "value",
                    "Product.ingredients.strength_unit",
                    "text",
                    desc("Product.ingredients.strength_unit"),
                ),
            ),
            "The active ingredients of each product, with their strength. A product with several active"
            " ingredients has several rows.",
            ("active ingredient", "ingredient"),
        ),
        View(
            "substances",
            "substance",
            "one row per substance (UNII)",
            "Substance",
            f"{wh}.T_SUBST_MSTR s",
            (
                VCol("substance_unii", "s.UNII_CD", "key", "Substance.unii", "text", desc("Substance.unii")),
                VCol(
                    "substance_name",
                    "s.SUBST_NM",
                    "value",
                    "Substance.name",
                    "text",
                    desc("Substance.name"),
                    synonyms=("ingredient name", "active ingredient name"),
                ),
            ),
            "Substances (active ingredients) from the FDA substance registry, identified by UNII.",
            ("active ingredient", "molecule", "compound"),
        ),
        View(
            "firms",
            "firm",
            "one row per firm",
            "Organization",
            f"{wh}.T_ORG_MSTR o",
            (
                VCol("firm_code", "o.ORG_CD", "key", "Organization.slug", "text", desc("Organization.slug")),
                VCol(
                    "firm_name",
                    "o.ORG_NM",
                    "value",
                    "Organization.name",
                    "text",
                    desc("Organization.name"),
                    synonyms=("company name", "manufacturer name"),
                ),
            ),
            "Firms: labelers of packages and recalling firms of recalls.",
            ("company", "manufacturer", "labeler", "organization", "firm"),
        ),
        View(
            "packages",
            "package",
            "one row per package (package NDC)",
            "PackagedProduct",
            f"{wh}.T_PKG k JOIN {wh}.T_PRD_MSTR p ON p.PRD_SK = k.PRD_SK"
            f" JOIN {wh}.T_ORG_MSTR o ON o.ORG_SK = k.LBLR_ORG_SK",
            (
                VCol("package_ndc", "k.PKG_NDC", "key", "PackagedProduct.ndc", "text", desc("PackagedProduct.ndc")),
                VCol(
                    "product_ndc", "p.PRD_CD", "ref", "Product.product_ndc", "text", "The product packaged.", "Product"
                ),
                VCol(
                    "labeler_firm_code",
                    "o.ORG_CD",
                    "ref",
                    "Organization.slug",
                    "text",
                    desc("PackagedProduct.labeler"),
                    "Organization",
                    synonyms=("labeler", "marketed by"),
                ),
                VCol(
                    "package_description",
                    "k.PKG_DESC",
                    "value",
                    "PackagedProduct.description",
                    "text",
                    desc("PackagedProduct.description"),
                ),
            ),
            "Packages (package NDCs). Each package belongs to one product and is labeled by one firm; a product"
            " usually has several packages, so count distinct products when counting products through packages.",
            ("package NDC", "SKU", "pack"),
        ),
        View(
            "adverse_event_reports",
            "adverse_event_report",
            "one row per adverse event report",
            "AdverseEventReport",
            f"{wh}.T_AE_HDR a",
            (
                VCol(
                    "report_id",
                    "a.RPT_ID",
                    "key",
                    "AdverseEventReport.report_id",
                    "text",
                    desc("AdverseEventReport.report_id"),
                ),
                VCol(
                    "source_system",
                    _ref("SRC_SYS", "a.SRC_SYS_CD"),
                    "value",
                    "AdverseEventReport.source_system",
                    "text",
                    desc("AdverseEventReport.source_system"),
                    values=_labels("SourceSystem"),
                ),
                VCol(
                    "received_date",
                    _date("a.RCPT_DT"),
                    "value",
                    "AdverseEventReport.receipt_date",
                    "date",
                    desc("AdverseEventReport.receipt_date"),
                    synonyms=("receipt date", "date received"),
                ),
                VCol(
                    "is_serious",
                    _yn("a.SRS_FLG"),
                    "value",
                    "AdverseEventReport.serious",
                    "boolean",
                    desc("AdverseEventReport.serious"),
                    synonyms=("serious",),
                ),
                *(
                    VCol(
                        name,
                        _yn(f"a.{col}"),
                        "value",
                        f"AdverseEventReport.seriousness_criteria={member}",
                        "boolean",
                        f"True when the report's seriousness criteria include '{member}'.",
                    )
                    for name, col, member in (
                        ("resulted_in_death", "SRS_DTH_FLG", "death"),
                        ("was_life_threatening", "SRS_LT_FLG", "life threatening"),
                        ("caused_hospitalization", "SRS_HOSP_FLG", "hospitalization"),
                        ("caused_disability", "SRS_DSBL_FLG", "disabling"),
                        ("congenital_anomaly", "SRS_CA_FLG", "congenital anomaly"),
                        ("other_serious", "SRS_OTH_FLG", "other"),
                    )
                ),
                VCol(
                    "patient_age_years",
                    "a.PT_AGE_YRS",
                    "value",
                    "AdverseEventReport.patient_age_years",
                    "numeric",
                    desc("AdverseEventReport.patient_age_years"),
                    synonyms=("age",),
                ),
            ),
            "Adverse event reports (FAERS / openFDA), one row per report.",
            ("adverse event", "AE report", "case", "safety report", "FAERS report"),
        ),
        View(
            "adverse_event_reactions",
            "adverse_event_reaction",
            "one row per reaction listed on a report",
            "AdverseEventReport.reactions",
            f"{wh}.T_AE_RXN x JOIN {wh}.T_AE_HDR a ON a.AE_SK = x.AE_SK",
            (
                VCol(
                    "report_id",
                    "a.RPT_ID",
                    "ref",
                    "AdverseEventReport.report_id",
                    "text",
                    "The report.",
                    "AdverseEventReport",
                ),
                VCol(
                    "reaction_term",
                    "x.RXN_TRM",
                    "value",
                    "AdverseEventReport.reactions.term",
                    "text",
                    desc("AdverseEventReport.reactions.term"),
                    synonyms=("reaction", "adverse reaction", "event term", "MedDRA term"),
                ),
            ),
            "The reactions listed on each report, in report order. A report lists several reactions and can list"
            " the same term more than once, so count distinct reports when counting reports by reaction.",
            ("reaction", "adverse reaction"),
        ),
        View(
            "adverse_event_drugs",
            "adverse_event_drug",
            "one row per drug listed on a report",
            "AdverseEventReport.drugs",
            f"{wh}.T_AE_DRG d JOIN {wh}.T_AE_HDR a ON a.AE_SK = d.AE_SK",
            (
                VCol(
                    "report_id",
                    "a.RPT_ID",
                    "ref",
                    "AdverseEventReport.report_id",
                    "text",
                    "The report.",
                    "AdverseEventReport",
                ),
                VCol(
                    "drug_name",
                    "d.DRG_NM",
                    "value",
                    "AdverseEventReport.drugs.drug_name",
                    "text",
                    desc("AdverseEventReport.drugs.drug_name"),
                ),
                VCol(
                    "drug_role",
                    _ref("DRG_ROLE", "d.DRG_ROLE_CD"),
                    "value",
                    "AdverseEventReport.drugs.role",
                    "text",
                    desc("AdverseEventReport.drugs.role"),
                    values=_labels("DrugRole"),
                ),
            ),
            "The drugs listed on each report as the reporter named them, with their role.",
            ("suspect drug", "concomitant drug"),
        ),
        View(
            "recalls",
            "recall",
            "one row per recall",
            "Recall",
            f"{wh}.T_RCL_PRD_DNRM r",
            (
                VCol("recall_number", "r.RCL_NO", "key", "Recall.recall_number", "text", desc("Recall.recall_number")),
                VCol(
                    "recall_class",
                    _rcl_class("r.RCL_CLS_CD"),
                    "value",
                    "Recall.classification",
                    "text",
                    desc("Recall.classification") + " Class I is the most serious.",
                    synonyms=("classification", "recall classification", "severity"),
                    values=_labels("RecallClassification"),
                ),
                VCol("reason", "r.RCL_RSN_TXT", "value", "Recall.reason", "text", desc("Recall.reason")),
                VCol(
                    "report_date",
                    _date("r.RPT_DT"),
                    "value",
                    "Recall.report_date",
                    "date",
                    desc("Recall.report_date"),
                ),
                VCol(
                    "recalling_firm_code",
                    _sk_key("T_ORG_MSTR", "ORG_SK", "ORG_CD", "r.FIRM_ORG_SK"),
                    "ref",
                    "Organization.slug",
                    "text",
                    desc("Recall.recalling_firm"),
                    "Organization",
                    synonyms=("recalling firm", "firm that recalled"),
                ),
            ),
            "Drug recalls (FDA enforcement reports), one row per recall.",
            ("enforcement report", "product recall", "recall event"),
            distinct=True,
        ),
        View(
            "recall_products",
            "recall_product",
            "one row per recall and product it names",
            "Recall.products",
            f"{wh}.T_RCL_PRD_DNRM r JOIN {wh}.T_PRD_MSTR p ON p.PRD_SK = r.PRD_SK",
            (
                VCol("recall_number", "r.RCL_NO", "ref", "Recall.recall_number", "text", "The recall.", "Recall"),
                VCol("product_ndc", "p.PRD_CD", "ref", "Product.product_ndc", "text", "A product it names.", "Product"),
            ),
            "The products each recall names. A recall can name several products and a product can be in several"
            " recalls; count distinct recalls or distinct products as the question needs.",
            ("recalled product",),
        ),
    )


MEASURES: tuple[Measure, ...] = (
    Measure("product_count", "products", "COUNT(DISTINCT product_ndc)", "product", "Number of distinct products."),
    Measure("package_count", "packages", "COUNT(DISTINCT package_ndc)", "package", "Number of distinct packages."),
    Measure(
        "substance_count", "substances", "COUNT(DISTINCT substance_unii)", "substance", "Number of distinct substances."
    ),
    Measure(
        "labeler_count",
        "packages",
        "COUNT(DISTINCT labeler_firm_code)",
        "firm",
        "Number of distinct firms that label at least one package.",
        ("number of labelers",),
    ),
    Measure(
        "report_count",
        "adverse_event_reports",
        "COUNT(DISTINCT report_id)",
        "adverse_event_report",
        "Number of distinct adverse event reports.",
        ("number of cases", "case count"),
    ),
    Measure(
        "serious_report_count",
        "adverse_event_reports",
        "COUNT(DISTINCT report_id) FILTER (WHERE is_serious)",
        "adverse_event_report",
        "Number of distinct serious adverse event reports.",
        ("serious cases",),
    ),
    Measure(
        "death_report_count",
        "adverse_event_reports",
        "COUNT(DISTINCT report_id) FILTER (WHERE resulted_in_death)",
        "adverse_event_report",
        "Number of distinct reports whose seriousness criteria include death.",
        ("fatal reports", "deaths"),
    ),
    Measure(
        "average_patient_age",
        "adverse_event_reports",
        "AVG(patient_age_years)",
        "adverse_event_report",
        "Average patient age in years over reports that give an age, each report counted once.",
        ("mean age",),
    ),
    Measure(
        "reaction_mention_count",
        "adverse_event_reactions",
        "COUNT(*)",
        "adverse_event_reaction",
        "Number of reaction rows (a report listing a term twice counts twice).",
    ),
    Measure(
        "recall_count",
        "recalls",
        "COUNT(DISTINCT recall_number)",
        "recall",
        "Number of distinct recalls.",
        ("number of enforcement reports",),
    ),
    Measure(
        "class_1_recall_count",
        "recalls",
        "COUNT(DISTINCT recall_number) FILTER (WHERE recall_class = 'I')",
        "recall",
        "Number of distinct Class I recalls.",
        ("most serious recalls",),
    ),
    Measure(
        "recalled_product_count",
        "recall_products",
        "COUNT(DISTINCT product_ndc)",
        "product",
        "Number of distinct products named by at least one recall.",
    ),
)

JOINS: tuple[Join, ...] = (
    Join(
        "package_product",
        "packages",
        "products",
        "packages.product_ndc = products.product_ndc",
        "many_to_one",
        "A package belongs to one product.",
    ),
    Join(
        "package_labeler",
        "packages",
        "firms",
        "packages.labeler_firm_code = firms.firm_code",
        "many_to_one",
        "A package is labeled by one firm.",
    ),
    Join(
        "product_routes",
        "products",
        "product_routes",
        "products.product_ndc = product_routes.product_ndc",
        "one_to_many",
        "A product has one or more routes.",
    ),
    Join(
        "product_ingredients",
        "products",
        "product_ingredients",
        "products.product_ndc = product_ingredients.product_ndc",
        "one_to_many",
        "A product has one or more active ingredients.",
    ),
    Join(
        "ingredient_substance",
        "product_ingredients",
        "substances",
        "product_ingredients.substance_unii = substances.substance_unii",
        "many_to_one",
        "An ingredient is one substance.",
    ),
    Join(
        "report_reactions",
        "adverse_event_reports",
        "adverse_event_reactions",
        "adverse_event_reports.report_id = adverse_event_reactions.report_id",
        "one_to_many",
        "A report lists one or more reactions.",
    ),
    Join(
        "report_drugs",
        "adverse_event_reports",
        "adverse_event_drugs",
        "adverse_event_reports.report_id = adverse_event_drugs.report_id",
        "one_to_many",
        "A report lists one or more drugs.",
    ),
    Join(
        "recall_products",
        "recalls",
        "recall_products",
        "recalls.recall_number = recall_products.recall_number",
        "one_to_many",
        "A recall names zero or more products.",
    ),
    Join(
        "recalled_product",
        "recall_products",
        "products",
        "recall_products.product_ndc = products.product_ndc",
        "many_to_one",
        "Each recalled product row is one product.",
    ),
    Join(
        "recall_firm",
        "recalls",
        "firms",
        "recalls.recalling_firm_code = firms.firm_code",
        "many_to_one",
        "A recall has one recalling firm.",
    ),
)


@dataclass(frozen=True)
class SemanticLayer:
    views: tuple[View, ...]
    measures: tuple[Measure, ...]
    joins: tuple[Join, ...]

    def coverage(self) -> frozenset[str]:
        """The ontology classes and terms the layer models (the plan's coverage rule, as data)."""
        out: set[str] = set(COVERED_CLASSES)
        relations = {
            "PackagedProduct.product",
            "PackagedProduct.labeler",
            "Product.ingredients",
            "Product.ingredients.substance",
            "Recall.products",
            "Recall.recalling_firm",
            "Product.routes",
            "AdverseEventReport.reactions",
            "AdverseEventReport.drugs",
        }
        out |= relations
        for v in self.views:
            for c in v.cols:
                if c.term is not None:
                    out.add(c.term.split("=")[0])
        return frozenset(out)


def build(spec: SchemaSpec) -> SemanticLayer:
    def desc(term: str) -> str:
        f = w._field(spec, term)
        text = (f.description or "").strip()
        return " ".join(text.split()) if text else ""

    return build_from(views(desc), MEASURES, JOINS)


def build_from(vs: tuple[View, ...], measures: tuple[Measure, ...], joins: tuple[Join, ...]) -> SemanticLayer:
    """The layer, checked: every measure and join names a view, every column is inside the coverage rule."""
    layer = SemanticLayer(vs, measures, joins)
    names = {v.name for v in layer.views}
    for m in layer.measures:
        if m.view not in names:
            raise ValueError(f"measure {m.name}: no view {m.view}")
    for j in layer.joins:
        if j.left not in names or j.right not in names:
            raise ValueError(f"join {j.name}: unknown view")
    for v in layer.views:
        for c in v.cols:
            if c.term is not None and c.term.split("=")[0].split(".")[0] not in COVERED_CLASSES:
                raise ValueError(f"{v.name}.{c.name}: {c.term} is outside the coverage rule")
    if len(layer.measures) < 8:
        raise ValueError("the coverage rule asks for at least 8 named measures")
    return layer


# ---------------------------------------------------------------------------------------- render


def render_views(layer: SemanticLayer) -> str:
    out = [
        f"-- Generated by monograph.gen.semantic_layer {SEMANTIC_VERSION}. Do not edit.",
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};",
    ]
    for v in layer.views:
        cols = ",\n    ".join(f"{c.expr} AS {c.name}" for c in v.cols)
        distinct = "DISTINCT " if v.distinct else ""
        out += ["", f"CREATE VIEW {SCHEMA}.{v.name} AS\nSELECT {distinct}\n    {cols}\nFROM {v.from_sql};"]
    return "\n".join(out) + "\n"


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def to_yaml(value: Any, indent: int = 0) -> list[str]:
    """Block-style YAML with JSON-quoted scalars (a strict, deterministic subset)."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines += to_yaml(v, indent + 1)
            elif isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                sub = to_yaml(item, indent + 1)
                lines.append(f"{pad}- {sub[0].lstrip()}")
                lines += sub[1:]
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def model(layer: SemanticLayer) -> dict[str, Any]:
    entities = []
    for v in layer.views:
        key = next((c.name for c in v.cols if c.role == "key"), None)
        dims = []
        for c in v.cols:
            d: dict[str, Any] = {"name": c.name, "type": c.type, "description": c.description}
            if c.role == "ref" and c.references:
                d["refers_to"] = next(x.entity for x in layer.views if x.cls == c.references)
            if c.synonyms:
                d["synonyms"] = list(c.synonyms)
            if c.values:
                d["allowed_values"] = list(c.values)
            dims.append(d)
        e: dict[str, Any] = {
            "name": v.entity,
            "view": f"{SCHEMA}.{v.name}",
            "grain": v.grain,
            "description": v.description,
        }
        if key:
            e["primary_key"] = key
        if v.synonyms:
            e["synonyms"] = list(v.synonyms)
        e["dimensions"] = dims
        entities.append(e)
    measures = []
    for m in layer.measures:
        md: dict[str, Any] = {
            "name": m.name,
            "view": f"{SCHEMA}.{m.view}",
            "sql": m.sql,
            "grain": m.grain,
            "description": m.description,
        }
        if m.synonyms:
            md["synonyms"] = list(m.synonyms)
        measures.append(md)
    joins = [
        {
            "name": j.name,
            "left": f"{SCHEMA}.{j.left}",
            "right": f"{SCHEMA}.{j.right}",
            "on": j.on.replace(f"{j.left}.", f"{SCHEMA}.{j.left}.").replace(f"{j.right}.", f"{SCHEMA}.{j.right}."),
            "relationship": j.relationship,
            "description": j.description,
        }
        for j in layer.joins
    ]
    return {
        "semantic_model": {
            "name": "drug_safety",
            "version": SEMANTIC_VERSION,
            "description": (
                "Semantic layer over the drug data warehouse (schema wh): FDA drug products and packages, their"
                " ingredients and labelers, adverse event reports and recalls. Query the views in schema sem;"
                " codes are already decoded into labels, dates are real dates and flags are booleans."
            ),
            "entities": entities,
            "measures": measures,
            "joins": joins,
        }
    }


def render_model(layer: SemanticLayer) -> str:
    head = [f"# Generated by monograph.gen.semantic_layer {SEMANTIC_VERSION}. Do not edit."]
    return "\n".join(head + to_yaml(model(layer))) + "\n"


def mapping(layer: SemanticLayer) -> dict[str, Any]:
    """Every view and column back to its ontology term (the check reads it as it reads the warehouse's)."""
    return {
        "version": SEMANTIC_VERSION,
        "schema": SCHEMA,
        "views": [
            {
                "view": v.name,
                "grain": v.grain,
                "class": v.cls,
                "columns": [
                    {
                        "name": c.name,
                        "role": c.role,
                        **({"term": c.term} if c.term else {}),
                        **({"references": c.references} if c.references else {}),
                        "type": c.type,
                        **({"allowed_values": list(c.values)} if c.values else {}),
                    }
                    for c in v.cols
                ],
            }
            for v in layer.views
        ],
        "coverage": sorted(layer.coverage()),
    }


def render_mapping(layer: SemanticLayer) -> str:
    return json.dumps(mapping(layer), indent=2, ensure_ascii=False) + "\n"


def semantic_renderers(spec: SchemaSpec) -> dict[str, Callable[[], str]]:
    layer = build(spec)
    return {
        VIEWS_FILE: lambda: render_views(layer),
        MODEL_FILE: lambda: render_model(layer),
        MAPPING_FILE: lambda: render_mapping(layer),
    }
