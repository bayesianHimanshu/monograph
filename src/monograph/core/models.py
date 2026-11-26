"""THE single source of truth for the MONOGRAPH drug product model.

Everything in `generated/` is derived from this file. Edit it, then run `make gen`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from monograph.core.meta import OntClass, OntEnum, OntProp, SafeIdentifier, SafeText
from monograph.core.refs import Ref
from monograph.core.regex import safe
from monograph.core.rules import (
    AtLeast,
    PydanticOnlyViolation,
    RequiredWhen,
    Rule,
    RuleViolation,
    UniqueWithin,
    pydantic_only,
    rule_engine,
)

# ---------------------------------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------------------------------


class ModelBase(BaseModel):
    """Shared Pydantic configuration and the rule engine. Not part of the ontology."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", revalidate_instances="always")

    __rules__: ClassVar[tuple[Rule, ...]] = ()

    @model_validator(mode="after")
    @rule_engine
    def generated_rules(self) -> Self:
        for rule in type(self).__rules__:
            if not rule.holds(self):
                raise RuleViolation(rule.id, rule.message)
        return self


class Entity(ModelBase):
    """A thing with a public identifier and its own IRI."""

    __ont__: ClassVar[OntClass] = OntClass(name="Entity", label="Entity", abstract=True)


class EmbeddedValue(ModelBase):
    """A value that only exists inside its parent entity. Its IRI derives from the parent's IRI."""

    __ont__: ClassVar[OntClass] = OntClass(name="EmbeddedValue", label="Embedded value", abstract=True)


# ---------------------------------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------------------------------


class Route(StrEnum):
    """Route of administration, using FDA SPL route names as they appear in openFDA."""

    __ont__ = OntEnum(name="Route", label="Route of administration")

    BUCCAL = "BUCCAL"
    CUTANEOUS = "CUTANEOUS"
    DENTAL = "DENTAL"
    EPIDURAL = "EPIDURAL"
    INTRA_ARTICULAR = "INTRA-ARTICULAR"
    INTRADERMAL = "INTRADERMAL"
    INTRAMUSCULAR = "INTRAMUSCULAR"
    INTRATHECAL = "INTRATHECAL"
    INTRAVENOUS = "INTRAVENOUS"
    INTRAVESICAL = "INTRAVESICAL"
    INTRAVITREAL = "INTRAVITREAL"
    NASAL = "NASAL"
    OPHTHALMIC = "OPHTHALMIC"
    ORAL = "ORAL"
    OTIC = "OTIC"
    PERCUTANEOUS = "PERCUTANEOUS"
    RECTAL = "RECTAL"
    RESPIRATORY_INHALATION = "RESPIRATORY (INHALATION)"
    SUBCUTANEOUS = "SUBCUTANEOUS"
    SUBLINGUAL = "SUBLINGUAL"
    TOPICAL = "TOPICAL"
    TRANSDERMAL = "TRANSDERMAL"
    VAGINAL = "VAGINAL"


class DrugRole(StrEnum):
    """The role a drug played in an adverse event report.

    `suspect` is E2B characterization 1 when the source does not say which suspect is primary.
    """

    __ont__ = OntEnum(name="DrugRole", label="Drug role")

    PRIMARY_SUSPECT = "primary suspect"
    SECONDARY_SUSPECT = "secondary suspect"
    SUSPECT = "suspect"
    CONCOMITANT = "concomitant"
    INTERACTING = "interacting"


class SeriousnessCriterion(StrEnum):
    """Why a report is classified as serious (ICH E2B seriousness criteria)."""

    __ont__ = OntEnum(name="SeriousnessCriterion", label="Seriousness criterion")

    DEATH = "death"
    LIFE_THREATENING = "life threatening"
    HOSPITALIZATION = "hospitalization"
    DISABLING = "disabling"
    CONGENITAL_ANOMALY = "congenital anomaly"
    OTHER = "other"


class RecallClassification(StrEnum):
    """FDA recall classification, from I (most serious) to III."""

    __ont__ = OntEnum(name="RecallClassification", label="Recall classification")

    CLASS_I = "I"
    CLASS_II = "II"
    CLASS_III = "III"


class SourceSystem(StrEnum):
    """Which reporting system an adverse event report came from. The two differ in what they record
    (for example, only the FAERS quarterly files say which suspect drug is primary)."""

    __ont__ = OntEnum(name="SourceSystem", label="Source system")

    OPENFDA_E2B = "openfda_e2b"
    FAERS_QDE = "faers_qde"


# ---------------------------------------------------------------------------------------------------
# Entities and embedded values
# ---------------------------------------------------------------------------------------------------


class Substance(Entity):
    """A chemical or biological substance identified by its FDA Unique Ingredient Identifier."""

    __ont__: ClassVar[OntClass] = OntClass(name="Substance", label="Substance", path="substance", key=("unii",))

    unii: Annotated[
        SafeIdentifier, OntProp("unii", "UNII"), Field(pattern=safe(r"^[0-9A-Z]{10}$"), description="FDA UNII code.")
    ]
    name: Annotated[
        SafeText, OntProp("name", "name"), Field(min_length=1, max_length=500, description="Preferred name.")
    ]


class Organization(Entity):
    """A company acting as manufacturer, labeler or recalling firm."""

    __ont__: ClassVar[OntClass] = OntClass(
        name="Organization", label="Organization", path="organization", key=("slug",)
    )

    slug: Annotated[
        SafeIdentifier,
        OntProp("slug", "slug"),
        Field(
            pattern=safe(r"^[a-z0-9]+(-[a-z0-9]+)*$"),
            max_length=120,
            description="Stable key: the normalized name, lowercase, words joined by hyphens.",
        ),
    ]
    name: Annotated[
        SafeText, OntProp("name", "name"), Field(min_length=1, max_length=500, description="Preferred name.")
    ]


class ClinicalDrug(Entity):
    """A clinical drug concept in RxNorm, identified by its RxCUI.

    Several NDC products can share one clinical drug. The name is optional because openFDA carries
    RxCUIs without their RxNorm names.
    """

    __ont__: ClassVar[OntClass] = OntClass(
        name="ClinicalDrug", label="Clinical drug", path="clinical-drug", key=("rxcui",)
    )

    rxcui: Annotated[
        SafeIdentifier, OntProp("rxcui", "RxCUI"), Field(pattern=safe(r"^\d{1,8}$"), description="RxNorm CUI.")
    ]
    name: Annotated[
        SafeText | None, OntProp("name", "name"), Field(min_length=1, max_length=500, description="RxNorm name.")
    ] = None


class Ingredient(EmbeddedValue):
    """An active ingredient of a product with its strength."""

    __ont__: ClassVar[OntClass] = OntClass(name="Ingredient", label="Ingredient")

    substance: Annotated[Ref[Substance], OntProp("substance", "substance"), Field(description="The substance.")]
    strength_value: Annotated[
        Decimal, OntProp("strengthValue", "strength value"), Field(gt=0, description="Numeric strength.")
    ]
    strength_unit: Annotated[
        SafeIdentifier,
        OntProp("strengthUnit", "strength unit"),
        Field(min_length=1, max_length=50, description="Unit of the strength, for example mg/1."),
    ]


class Product(Entity):
    """A drug product as listed with FDA, identified by its product NDC (labeler and product segments)."""

    __ont__: ClassVar[OntClass] = OntClass(name="Product", label="Product", path="product", key=("product_ndc",))

    product_ndc: Annotated[
        SafeIdentifier,
        OntProp("productNdc", "product NDC"),
        Field(
            pattern=safe(r"^(\d{4}-\d{4}|\d{5}-\d{3}|\d{5}-\d{4})$"),
            description="Product NDC in 4-4, 5-3 or 5-4 form.",
        ),
    ]
    name: Annotated[
        SafeText, OntProp("name", "name"), Field(min_length=1, max_length=500, description="Name as listed.")
    ]
    dosage_form: Annotated[
        SafeText,
        OntProp("dosageForm", "dosage form"),
        Field(min_length=1, max_length=100, description="Dosage form as written in the source."),
    ]
    routes: Annotated[
        frozenset[Route], OntProp("route", "route"), Field(min_length=1, description="Routes of administration.")
    ]
    ingredients: Annotated[
        list[Ingredient], OntProp("ingredient", "ingredient"), Field(description="Active ingredients, in order.")
    ] = []
    clinical_drug: Annotated[
        Ref[ClinicalDrug] | None,
        OntProp("clinicalDrug", "clinical drug"),
        Field(description="The RxNorm clinical drug, when the source names exactly one."),
    ] = None

    @model_validator(mode="after")
    @pydantic_only(
        id="product-ingredient-substances-unique",
        reason="Uniqueness of a child property across sibling nodes has no SHACL Core construct.",
        unique=UniqueWithin("ingredients", ("substance",)),
    )
    def ingredient_substances_unique(self) -> Self:
        seen = [ingredient.substance for ingredient in self.ingredients]
        if len(seen) != len(set(seen)):
            raise PydanticOnlyViolation(
                "product-ingredient-substances-unique", "a substance appears twice in the ingredients"
            )
        return self


class PackagedProduct(Entity):
    """A package of a product, identified by its 10-digit hyphenated package NDC."""

    __ont__: ClassVar[OntClass] = OntClass(
        name="PackagedProduct", label="Packaged product", path="packaged-product", key=("ndc",)
    )

    ndc: Annotated[
        SafeIdentifier,
        OntProp("ndc", "NDC"),
        Field(
            pattern=safe(r"^(\d{4}-\d{4}-\d{2}|\d{5}-\d{3}-\d{2}|\d{5}-\d{4}-\d)$"),
            description="Package NDC in 4-4-2, 5-3-2 or 5-4-1 form.",
        ),
    ]
    product: Annotated[Ref[Product], OntProp("product", "product"), Field(description="The packaged product.")]
    labeler: Annotated[Ref[Organization], OntProp("labeler", "labeler"), Field(description="The labeler firm.")]
    description: Annotated[
        SafeText | None,
        OntProp("packageDescription", "package description"),
        Field(min_length=1, max_length=2000, description="Package description as written in the source."),
    ] = None


class LabelSection(EmbeddedValue):
    """One LOINC-coded section of a structured product label."""

    __ont__: ClassVar[OntClass] = OntClass(name="LabelSection", label="Label section", key=("code",))

    code: Annotated[
        SafeIdentifier,
        OntProp("loincCode", "LOINC code"),
        Field(pattern=safe(r"^\d{1,7}-\d$"), description="LOINC code."),
    ]
    title: Annotated[
        SafeText, OntProp("sectionTitle", "section title"), Field(min_length=1, max_length=200, description="Title.")
    ]
    text: Annotated[SafeText, OntProp("sectionText", "section text"), Field(min_length=1, description="Plain text.")]


class Label(Entity):
    """One version of a structured product label (SPL)."""

    __ont__: ClassVar[OntClass] = OntClass(name="Label", label="Label", path="label", key=("set_id", "version"))

    set_id: Annotated[
        SafeIdentifier,
        OntProp("setId", "SPL set ID"),
        Field(
            pattern=safe(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
            description="SPL set ID, a lowercase UUID.",
        ),
    ]
    version: Annotated[
        int, OntProp("labelVersion", "label version"), Field(ge=1, lt=1_000_000, description="SPL version.")
    ]
    effective_date: Annotated[
        date, OntProp("effectiveDate", "effective date"), Field(description="SPL effective time.")
    ]
    products: Annotated[
        frozenset[Ref[Product]], OntProp("labelsProduct", "labels product"), Field(description="Products.")
    ] = frozenset()
    sections: Annotated[
        frozenset[LabelSection], OntProp("section", "section"), Field(description="Coded sections.")
    ] = frozenset()

    @model_validator(mode="after")
    @pydantic_only(
        id="label-effective-date-not-future",
        reason="Needs the current date, which a static SHACL Core shapes graph cannot know.",
    )
    def effective_date_not_future(self, info: ValidationInfo) -> Self:
        context = info.context if isinstance(info.context, dict) else {}
        today = context.get("today", date.today())
        if self.effective_date > today:
            raise PydanticOnlyViolation("label-effective-date-not-future", "effective date is in the future")
        return self

    @model_validator(mode="after")
    @pydantic_only(
        id="label-section-codes-unique",
        reason=(
            "Uniqueness has no SHACL Core construct. Duplicate codes mint the same section IRI, so"
            " SHACL only notices when the merged node breaks sh:maxCount."
        ),
        unique=UniqueWithin("sections", ("code",)),
    )
    def section_codes_unique(self) -> Self:
        codes = [section.code for section in self.sections]
        if len(codes) != len(set(codes)):
            raise PydanticOnlyViolation("label-section-codes-unique", "a LOINC section code appears twice")
        return self


class DrugInvolvement(EmbeddedValue):
    """A drug mentioned in an adverse event report, as reported."""

    __ont__: ClassVar[OntClass] = OntClass(name="DrugInvolvement", label="Drug involvement")

    drug_name: Annotated[
        SafeText,
        OntProp("verbatimDrugName", "verbatim drug name"),
        Field(min_length=1, max_length=500, description="Drug name exactly as reported."),
    ]
    clinical_drug: Annotated[
        Ref[ClinicalDrug] | None,
        OntProp("clinicalDrug", "clinical drug"),
        Field(description="The RxNorm clinical drug, when the source resolves the name to exactly one RxCUI."),
    ] = None
    role: Annotated[DrugRole, OntProp("drugRole", "drug role"), Field(description="Role in the report.")]


class Reaction(EmbeddedValue):
    """A reaction, as a MedDRA preferred term string. No MedDRA hierarchy is shipped (licensed)."""

    __ont__: ClassVar[OntClass] = OntClass(name="Reaction", label="Reaction")

    term: Annotated[
        SafeText,
        OntProp("meddraPreferredTerm", "MedDRA preferred term"),
        Field(min_length=1, max_length=100, description="MedDRA PT text (at most 100 characters)."),
    ]


SUSPECT_ROLES = (DrugRole.PRIMARY_SUSPECT, DrugRole.SECONDARY_SUSPECT, DrugRole.SUSPECT)


class AdverseEventReport(Entity):
    """A safety report from FDA adverse event data (FAERS, now FDA AEMS), via openFDA."""

    __ont__: ClassVar[OntClass] = OntClass(
        name="AdverseEventReport", label="Adverse event report", path="adverse-event-report", key=("report_id",)
    )
    __rules__: ClassVar[tuple[Rule, ...]] = (
        RequiredWhen(
            id="serious-requires-criterion",
            when="serious",
            equals=True,
            then="seriousness_criteria",
            message="a serious report needs at least one seriousness criterion",
        ),
        AtLeast(
            id="has-suspect",
            field="drugs",
            where="role",
            values=SUSPECT_ROLES,
            min_count=1,
            message="a report needs at least one suspect drug (primary, secondary or unspecified)",
        ),
    )

    report_id: Annotated[
        SafeIdentifier,
        OntProp("safetyReportId", "safety report ID"),
        Field(
            pattern=safe(r"^\d{1,12}(-\d{1,3})?$"),
            description="Safety report ID, optionally with a -digits version suffix.",
        ),
    ]
    source_system: Annotated[
        SourceSystem, OntProp("sourceSystem", "source system"), Field(description="Where the report came from.")
    ]
    receipt_date: Annotated[
        date, OntProp("receiptDate", "receipt date"), Field(description="Date FDA received the report.")
    ]
    serious: Annotated[bool, OntProp("serious", "serious"), Field(description="Whether the report is serious.")]
    seriousness_criteria: Annotated[
        frozenset[SeriousnessCriterion],
        OntProp("seriousnessCriterion", "seriousness criterion"),
        Field(max_length=6, description="Seriousness criteria met."),
    ] = frozenset()
    patient_age_years: Annotated[
        Decimal | None,
        OntProp("patientAgeYears", "patient age in years"),
        Field(ge=0, le=150, description="Patient age at onset, in years."),
    ] = None
    drugs: Annotated[
        list[DrugInvolvement],
        OntProp("drugInvolvement", "drug involvement"),
        Field(min_length=1, description="Drugs, in reported order."),
    ]
    reactions: Annotated[
        list[Reaction], OntProp("reaction", "reaction"), Field(min_length=1, description="Reactions, in order.")
    ]


class Recall(Entity):
    """A drug recall from the FDA enforcement report."""

    __ont__: ClassVar[OntClass] = OntClass(name="Recall", label="Recall", path="recall", key=("recall_number",))

    recall_number: Annotated[
        SafeIdentifier,
        OntProp("recallNumber", "recall number"),
        Field(
            pattern=safe(r"^D-\d{3,4}-\d{4}$"),
            description="FDA recall number, for example D-321-2016 or D-0123-2025.",
        ),
    ]
    classification: Annotated[
        RecallClassification, OntProp("recallClassification", "classification"), Field(description="Class.")
    ]
    reason: Annotated[
        SafeText, OntProp("recallReason", "reason"), Field(min_length=1, description="Reason for the recall.")
    ]
    report_date: Annotated[date, OntProp("reportDate", "report date"), Field(description="Enforcement report date.")]
    products: Annotated[
        frozenset[Ref[Product]], OntProp("recallsProduct", "recalls product"), Field(description="Products.")
    ] = frozenset()
    recalling_firm: Annotated[
        Ref[Organization], OntProp("recallingFirm", "recalling firm"), Field(description="The recalling firm.")
    ]


# Every class that is part of the ontology, bases first. Generators walk this tuple.
MODELS: tuple[type[ModelBase], ...] = (
    Entity,
    EmbeddedValue,
    Substance,
    Organization,
    ClinicalDrug,
    Ingredient,
    Product,
    PackagedProduct,
    LabelSection,
    Label,
    DrugInvolvement,
    Reaction,
    AdverseEventReport,
    Recall,
)
