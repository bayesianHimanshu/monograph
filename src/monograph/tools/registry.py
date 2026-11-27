"""The H2 tool registry: one definition per tool (input model, output model, description), shared by the SQL
and Cypher backends, the in-memory reference used for gold, and the MCP adapter.

Semantics (identical in every backend; the shared glossary in the base prompt states the same rules):

* a product **contains** substance S when it has an ingredient whose substance is S;
* **counting salt forms**, it also contains S when an ingredient's substance has S as its active moiety;
* a product is **mapped to** clinical drug C through its drug mappings (any source) or its clinical drug;
* a report **involves** clinical drug C when one of its drugs has C as clinical drug or harmonized
  clinical drug; it involves a product when it involves a clinical drug the product is mapped to; it
  involves a substance when it involves a product that contains the substance (no salt expansion); a
  package stands for its product;
* the **recalls of** a product are the recalls that name it; of a package, its product's; of a substance,
  those of the products containing it; of a clinical drug, those of the products mapped to it; of an
  organization, the recalls it issued (recalling firm);
* the **labelers** of a product are the organizations that label one of its packages.

Results are sorted by id and capped at `ROW_CAP` items, with `truncated` set when more existed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field

from monograph.core.meta import DEFAULT_BASE

ROW_CAP = 200
MAX_IDS = 50
MAX_HOPS = 4
MAX_PATHS = 10
TEXT_CAP = 300


class EntityKind(StrEnum):
    SUBSTANCE = "substance"
    PRODUCT = "product"
    PACKAGE = "package"
    ORGANIZATION = "organization"
    CLINICAL_DRUG = "clinical_drug"
    LABEL = "label"
    REPORT = "report"
    RECALL = "recall"


KIND_LABEL: dict[EntityKind, str] = {
    EntityKind.SUBSTANCE: "Substance",
    EntityKind.PRODUCT: "Product",
    EntityKind.PACKAGE: "PackagedProduct",
    EntityKind.ORGANIZATION: "Organization",
    EntityKind.CLINICAL_DRUG: "ClinicalDrug",
    EntityKind.LABEL: "Label",
    EntityKind.REPORT: "AdverseEventReport",
    EntityKind.RECALL: "Recall",
}
PATH_LABEL: dict[str, str] = {
    "substance": "Substance",
    "product": "Product",
    "packaged-product": "PackagedProduct",
    "organization": "Organization",
    "clinical-drug": "ClinicalDrug",
    "label": "Label",
    "adverse-event-report": "AdverseEventReport",
    "recall": "Recall",
}
# The embedded values that are vertices, by the property name in their IRI.
EMBEDDED_LABEL: dict[str, str] = {
    "alias": "OrganizationAlias",
    "section": "LabelSection",
    "drugInvolvement": "DrugInvolvement",
    "reaction": "Reaction",
}
# The property shown as a node's display name: the first one present, in this order.
DISPLAY_ORDER = ("name", "title", "drug_name", "term", "ndc", "recall_number", "report_id", "set_id", "rxcui")


def label_of(iri: str) -> str | None:
    """The vertex label of an id, from its IRI alone, or None when it is not one of ours."""
    if not iri.startswith(DEFAULT_BASE):
        return None
    parts = iri[len(DEFAULT_BASE) :].split("/")
    entity = PATH_LABEL.get(parts[0])
    if entity is None or len(parts) < 2:
        return None
    key_parts = 3 if entity == "Label" else 2
    if len(parts) == key_parts:
        return entity
    if len(parts) == key_parts + 2 and parts[key_parts] in EMBEDDED_LABEL:
        return EMBEDDED_LABEL[parts[key_parts]]
    return None


def canonical_id(text: str) -> str:
    """Accept a full IRI or the part after the base; return the full IRI."""
    text = text.strip()
    return text if text.startswith(("http://", "https://")) else DEFAULT_BASE + text.lstrip("/")


def short_id(iri: str) -> str:
    return unquote(iri[len(DEFAULT_BASE) :]) if iri.startswith(DEFAULT_BASE) else iri


def canon(value: Any) -> Any:
    """One rendering of a stored value, whichever store it came from."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float | Decimal):
        d = Decimal(str(value)).normalize()
        return format(d, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list | tuple | set | frozenset):
        return sorted(canon(v) for v in value)
    text = str(value)
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + " [truncated]"


def display(label: str, props: Mapping[str, Any]) -> str:
    for key in DISPLAY_ORDER:
        value = props.get(key)
        if value not in (None, ""):
            return str(canon(value))
    return label


# ------------------------------------------------------------------------------------------- models


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


IdList = list[str]


class ResolveEntityIn(_In):
    text: str = Field(min_length=1, max_length=200, description="A name, code or identifier as written.")
    kind: EntityKind | None = Field(default=None, description="Restrict to one kind of entity.")


class Candidate(_Out):
    id: str
    kind: str
    display: str
    match: str = Field(description="Which identifier or name matched, for example 'ndc11' or 'alias'.")


class ResolveEntityOut(_Out):
    candidates: list[Candidate]
    truncated: bool = False


class GetEntityIn(_In):
    ids: IdList = Field(min_length=1, max_length=MAX_IDS, description="Entity ids (IRIs).")


class Entity(_Out):
    id: str
    kind: str
    properties: dict[str, Any]


class GetEntityOut(_Out):
    entities: list[Entity]
    unknown_ids: list[str] = []


class NeighborsIn(_In):
    ids: IdList = Field(min_length=1, max_length=MAX_IDS, description="Start node ids (IRIs).")
    relation: str | None = Field(default=None, description="Only this relation, for example LABELER.")
    direction: Literal["out", "in", "both"] = Field(default="both", description="Edge direction from the start.")


class Neighbor(_Out):
    from_id: str
    relation: str
    direction: Literal["out", "in"]
    id: str
    kind: str
    display: str
    edge: dict[str, Any] = {}


class NeighborsOut(_Out):
    neighbors: list[Neighbor]
    truncated: bool = False
    unknown_ids: list[str] = []


class FindPathIn(_In):
    from_id: str
    to_id: str
    max_hops: int = Field(default=3, ge=1, le=MAX_HOPS, description="Longest path to look for, in edges.")


class PathOut(_Out):
    nodes: list[str]
    relations: list[str] = Field(description="'R>' for an edge followed forwards, '<R' backwards.")


class FindPathOut(_Out):
    length: int | None
    paths: list[PathOut]
    truncated: bool = False


class ProductsWithSubstanceIn(_In):
    substance_id: str
    include_moiety_matches: bool = Field(
        default=False, description="Also products whose ingredient has this substance as its active moiety."
    )


class ProductMatch(_Out):
    id: str
    display: str
    product_ndc: str
    via: Literal["ingredient", "moiety"]


class ProductsWithSubstanceOut(_Out):
    products: list[ProductMatch]
    truncated: bool = False


class EventsForIn(_In):
    entity_id: str = Field(description="A substance, product, package or clinical drug id.")
    date_from: date | None = Field(default=None, description="Receipt date on or after (YYYY-MM-DD).")
    date_to: date | None = Field(default=None, description="Receipt date on or before (YYYY-MM-DD).")
    serious_only: bool = False


class ReportItem(_Out):
    id: str
    report_id: str
    receipt_date: str
    serious: bool


class EventsForOut(_Out):
    count: int
    reports: list[ReportItem]
    truncated: bool = False


class RecallsForIn(_In):
    entity_id: str = Field(description="A product, package, substance, clinical drug or organization id.")


class RecallItem(_Out):
    id: str
    recall_number: str
    classification: str
    report_date: str
    firm_id: str


class RecallsForOut(_Out):
    recalls: list[RecallItem]
    truncated: bool = False


class CountIn(_In):
    what: Literal["reports", "recalls", "products"]
    related_to: str = Field(description="The entity id the counted items relate to.")
    date_from: date | None = Field(default=None, description="reports: receipt date on or after.")
    date_to: date | None = Field(default=None, description="reports: receipt date on or before.")
    serious_only: bool = Field(default=False, description="reports: serious reports only.")
    classification: Literal["I", "II", "III"] | None = Field(default=None, description="recalls: this class only.")
    include_moiety_matches: bool = Field(default=False, description="products of a substance: count salt forms.")


class CountOut(_Out):
    count: int


class ToolError(Exception):
    """A tool could not answer (unknown id, unsupported kind). The message goes back to the agent."""


# H2.1, identity off: the only tool description that differs between cells, because the resolver does.
RESOLVE_EXACT_DESCRIPTION = (
    "Find canonical entities for a name, code or identifier. Matches exact identifiers as written (UNII,"
    " product NDC, package NDC with hyphens, RxCUI, recall number, report id, SPL set id, firm slug) and exact"
    " names, with no normalization. Returns each candidate's id, kind, display name and which identifier or"
    " name matched."
)


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    input: type[_In]
    output: type[_Out]


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="resolve_entity",
        description=(
            "Find canonical entities for a name, code or identifier. Matches exact identifiers (UNII, product"
            " NDC, package NDC with hyphens, 11-digit NDC without hyphens, RxCUI, recall number, report id,"
            " SPL set id) and names after normalization (case, punctuation, whitespace, trademark marks,"
            " pharmacopoeia suffixes; for firms also corporate suffixes and recorded aliases). Returns each"
            " candidate's id, kind, display name and which identifier or name matched."
        ),
        input=ResolveEntityIn,
        output=ResolveEntityOut,
    ),
    ToolSpec(
        name="get_entity",
        description="Properties of one or more entities by id.",
        input=GetEntityIn,
        output=GetEntityOut,
    ),
    ToolSpec(
        name="neighbors",
        description=(
            "Nodes one edge away from the given ids, with the relation, direction and edge properties."
            " Relations: INGREDIENT (product to substance), HAS_ACTIVE_MOIETY (salt to moiety), PRODUCT"
            " (package to product), LABELER (package to firm), CLINICAL_DRUG and DRUG_MAPPING (product to"
            " RxNorm clinical drug), HAS_GENERIC (brand clinical drug to generic), RECALLS_PRODUCT (recall to"
            " product), RECALLING_FIRM (recall to firm), LABELS_PRODUCT (label to product), DRUG_INVOLVEMENT"
            " (report to drug entry), HARMONIZED_CLINICAL_DRUG and CLINICAL_DRUG (drug entry to clinical"
            " drug), REACTION, SECTION, ALIAS (firm to name variant)."
        ),
        input=NeighborsIn,
        output=NeighborsOut,
    ),
    ToolSpec(
        name="find_path",
        description=(
            "All shortest paths (up to 10) between two nodes, following edges in either direction, up to"
            " max_hops edges. length is null when there is none within max_hops."
        ),
        input=FindPathIn,
        output=FindPathOut,
    ),
    ToolSpec(
        name="products_with_substance",
        description=(
            "Products that contain a substance as an active ingredient; with include_moiety_matches, also"
            " products whose ingredient is a salt or ester form of it (its active moiety is the substance)."
        ),
        input=ProductsWithSubstanceIn,
        output=ProductsWithSubstanceOut,
    ),
    ToolSpec(
        name="events_for",
        description=(
            "Adverse event reports that involve an entity: a clinical drug (a report drug names it), a product"
            " or package (the report involves a clinical drug the product is mapped to), or a substance (the"
            " report involves a product containing it). Optional receipt-date range and serious-only filter."
        ),
        input=EventsForIn,
        output=EventsForOut,
    ),
    ToolSpec(
        name="recalls_for",
        description=(
            "Recalls of an entity: a product (recalls naming it), a package (its product's), a substance or"
            " clinical drug (recalls of the products containing or mapped to it), or an organization (recalls"
            " it issued as recalling firm)."
        ),
        input=RecallsForIn,
        output=RecallsForOut,
    ),
    ToolSpec(
        name="count",
        description=(
            "Count reports, recalls or products related to an entity, with the same rules as events_for,"
            " recalls_for and products_with_substance (products of a clinical drug: products mapped to it; of"
            " an organization: products it labels). Not capped."
        ),
        input=CountIn,
        output=CountOut,
    ),
)
TOOL_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """The input schema as sent to the API (no titles; additionalProperties false)."""
    schema = spec.input.model_json_schema()

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    out: dict[str, Any] = strip(schema)
    out["additionalProperties"] = False
    return out


_TOKEN = re.compile(r"[A-Z0-9]+")


def longest_token(normalized: str) -> str:
    """The longest alphanumeric run of a normalized string: a coarse store filter before exact matching."""
    tokens = _TOKEN.findall(normalized.upper())
    return max(tokens, key=lambda t: (len(t), t)) if tokens else ""
