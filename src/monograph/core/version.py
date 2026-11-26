"""The version of the drug product model. Bump it according to semver; CI enforces the rules."""

SCHEMA_VERSION = "1.0.0"

# What changed in each release and why. Every 1.0.0 entry corrects the model in response to an H6
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
}
