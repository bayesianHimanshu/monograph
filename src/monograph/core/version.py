"""The version of the drug product model. Bump it according to semver; CI enforces the rules."""

SCHEMA_VERSION = "2.2.0"

# What changed in each release and why. Every entry corrects the model in response to an H6 or H6.1
# real-data finding; none of them loosens a constraint for its own sake. `monograph measure` writes this
# to the results so the report can state it from the data.
CHANGES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "1.0.0": (
        (
            "product-identity",
            "H6: 64% of NDC records carry no RxCUI, so Product (keyed by RxCUI) conformed 19%.",
            "Product is keyed by product NDC; the RxNorm concept is a separate ClinicalDrug (key RxCUI), "
            "referenced optionally from Product and from DrugInvolvement (which was Ref[Product]).",
        ),
        (
            "suspect-rule",
            "H6: openFDA follows ICH E2B, which does not record which suspect is primary; 39% of reports failed.",
            "The rule asks for at least one involvement with role primary suspect, secondary suspect or suspect. "
            "AdverseEventReport gains source_system (openfda_e2b, faers_qde) so the provenance of roles is explicit.",
        ),
        (
            "recall-number-pattern",
            "H6: 17% of recall numbers use 3 digits in the middle segment (for example D-321-2016).",
            "The middle segment allows 3 or 4 digits.",
        ),
        (
            "report-id-pattern",
            "H6: safety report IDs can carry a -digits version suffix (for example 5801206-7).",
            "An optional -digits suffix (1 to 3 digits) is allowed.",
        ),
        (
            "regex-digit-escape",
            "H6: 10 of 12 developer-written patterns fell outside the safe subset, most because of \\d.",
            "safe() canonicalizes \\d to [0-9] for every target, Pydantic included; lookarounds and other "
            "constructs are still rejected.",
        ),
        (
            "safe-strings",
            "H6: pySHACL accepts a trailing newline before $, and Postgres cannot store NUL.",
            "Every string is SafeIdentifier (no control characters) or SafeText (no control characters "
            "except tab, LF, CR), enforced in Pydantic, SHACL (sh:not) and a Postgres CHECK. Bounded patterns "
            "also emit sh:maxLength.",
        ),
        (
            "uniqueness-in-postgres",
            "H6: the two uniqueness constraints were pydantic_only and unenforced in Postgres.",
            "They stay pydantic_only for SHACL, and the DDL generates UNIQUE constraints for them.",
        ),
        (
            "ddl-profiles",
            "H6: the DDL used deferrable foreign keys only because the integration probe needed them.",
            "The default DDL has plain foreign keys; a test profile keeps them deferrable. Both are drift-checked.",
        ),
    ),
    "2.0.0": (
        (
            "route-conditional",
            "H6.1: 86 of 200 NDC products had no route; 84 of them are not finished (60 bulk ingredients, 23 drugs "
            "for further processing, 1 licensed vaccine bulk intermediate) and 2 are finished kits.",
            "Product gains product_type (NDC directory product type) and finished. Routes are required only when "
            "the product is finished and its type is human prescription or human OTC drug (a conditional rule); "
            "kits are not exempt.",
        ),
        (
            "route-vocabulary",
            "H6.1: the route enum was the 23 routes observed in one sample; real records used routes outside it.",
            "Route is the FDA SPL route of administration list (121 terms with NCI codes). OTIC is replaced by the "
            "FDA term AURICULAR (OTIC).",
        ),
    ),
    "2.1.0": (
        (
            "h4-extraction-targets",
            "H4 extracts product facts from label narrative and needs typed targets that resolve against the graph.",
            "Additive: LabelExtraction, ExtractedProduct, ExtractedIngredient and ExtractedPackage; the FDA SPL "
            "DosageForm (158 terms) and UcumUnit (52 codes) enums; Derived fields filled by deterministic "
            "resolution; graph-context shapes (generated/monograph.context.shapes.ttl) and two extraction_only "
            "checks; rule listed-package-resolves.",
        ),
        (
            "json-schema-patterns",
            "H4: since 1.0.0 the generated JSON Schema had silently lost every pattern once strings became "
            "SafeIdentifier or SafeText (Pydantic renders only the first step of a validator chain).",
            "The JSON Schema generator now writes every string and integer constraint from the SchemaSpec, and a "
            "test checks that each one is present.",
        ),
    ),
    "2.1.1": (
        (
            "json-schema-projection",
            "H4: validating real records against the generated JSON Schema showed it ignored SafeIdentifier and "
            "SafeText, and Decimal bounds.",
            "The JSON Schema now states the charset as `not: {pattern}` and Decimal bounds on the number branch. "
            "What it still cannot state (bounds on string-encoded Decimals, floats for Decimals, pydantic_only) is "
            "a declared list that the agreement gate checks.",
        ),
    ),
    "2.2.0": (
        (
            "identity-layer",
            "H2: identity questions need a product-to-RxCUI crosswalk, salt-to-moiety links, brand-to-generic "
            "links, firm name variants, the 11-digit NDC form and openFDA's harmonized RxCUIs on event drugs.",
            "Additive: Product.drug_mappings (DrugMapping with source and evidence), Substance.active_moiety "
            "(MoietyLink), ClinicalDrug.term_type and generic (GenericLink), Organization.aliases "
            "(OrganizationAlias), PackagedProduct.ndc11, DrugInvolvement.harmonized_clinical_drugs. Every link "
            "carries its provenance.",
        ),
    ),
}


# Known real-data failures that are left failing on purpose and handed to later experiments. The model
# is not loosened for them. `monograph measure` joins this ledger with the live failure counts.
DEFERRED: tuple[tuple[str, str, str, str], ...] = (
    (
        "Product",
        "ingredients[].substance: missing",
        "H4/H2",
        "NDC records list UNIIs without names and in no stated order, so a multi-ingredient product's "
        "ingredients cannot be paired with substances deterministically.",
    ),
    (
        "Label",
        "sections[].text: too_short",
        "H4/H2",
        "openFDA delivers some label sections with empty text; the section exists but its content is missing.",
    ),
)
