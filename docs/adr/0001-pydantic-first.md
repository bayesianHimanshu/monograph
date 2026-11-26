# ADR 0001: Pydantic models are the single source of truth

Status: accepted (2026-11-26)

## Context

MONOGRAPH needs one drug product model that every experiment reuses, expressed as an OWL ontology
(for reasoning and vocabulary), SHACL shapes (for graph validation), JSON Schema (for API contracts),
and Postgres DDL (for storage). Keeping four hand-written artifacts in sync is the failure mode we want
to avoid. H6 tests whether one source can drive all four.

Candidate sources were OWL (generate code from the ontology), LinkML (a purpose-built modeling
language) and Pydantic. LinkML is excluded by the engineering rules. OWL-first makes Python a second
class citizen, and OWL's open-world semantics carry no validation intent, so constraints would have
to live somewhere else anyway.

## Decision

Write the model once as Pydantic v2 classes in `src/monograph/core/models.py`. Ontology metadata
(IRIs, labels) is attached with typed annotations from `core/meta.py`. Cross-field constraints are
written in a small rule DSL (`core/rules.py`) that projects to both a Pydantic validator and SHACL
Core. Everything in `generated/` is derived, committed, and drift-checked.

Pydantic runs in strict mode with `extra="forbid"`, `frozen=True` and
`revalidate_instances="always"`. Strictness matters for H6: a lax validator that turns `"1"` into
`True` would disagree with SHACL's datatype check for reasons unrelated to the schema.

## Consequences

* Python developers edit one file and run `make gen`. Type checking covers the annotation API.
* The expressiveness of the whole system is capped by the intersection of what Pydantic, SHACL Core
  and (partly) Postgres can say. Anything outside it must be declared `pydantic_only`, which makes
  the gap visible instead of silent.
* Rule field names are strings, checked at generation time rather than by mypy.
* Generators interpret Pydantic internals (`FieldInfo.metadata`, `__pydantic_decorators__`). A
  Pydantic upgrade can change those internals, so `core/spec.py` is the only module allowed to read
  them and it fails loudly on anything unrecognized.
* Experiments H1 to H5 and later inherit the model, the IRI scheme and the generated artifacts. A
  breaking change needs a major `SCHEMA_VERSION` bump, enforced by CI.
