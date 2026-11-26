# H6 plan: one schema keeps OWL, SHACL and Pydantic in sync

## Hypothesis

H6: if the domain model is written once, as Pydantic v2 models, then an OWL ontology, SHACL shapes,
JSON Schema and Postgres DDL can be generated from it, CI can catch every deliberate break, and the
places where single-sourcing leaks can be listed exhaustively.

The leaks are a result, not a failure. Every leak is recorded in `results/h6/*.jsonl` and surfaced in
`docs/h6/RESULTS.md`.

## Architecture

```
core/models.py  --analyze-->  core/spec.py (SchemaSpec IR)  --+--> gen/owl.py        --> monograph.owl.ttl
   ^  uses                                                     +--> gen/shacl.py      --> monograph.shapes.ttl
core/meta.py (OntClass, OntProp, OntEnum)                      +--> gen/jsonschema.py --> monograph.schema.json
core/rules.py (RequiredWhen, AtLeast, pydantic_only)           +--> gen/ddl.py        --> monograph.ddl.sql
core/refs.py (Ref[T])                                          +--> rdf/mapping.py (instances <-> RDF)
                                                               +--> validate/strategies.py (hypothesis)
```

All generators consume one intermediate representation, `SchemaSpec`, produced by
`core/spec.py:analyze()`. `analyze()` is the single place that interprets Python types, Pydantic
`FieldInfo` metadata and ontology annotations. Anything it does not understand is a `SchemaError`, so
no generator can silently drop a constraint.

### `meta.py` annotation API

```python
class Substance(Entity):
    """A chemical substance identified by its FDA UNII."""
    __ont__: ClassVar[OntClass] = OntClass(name="Substance", label="Substance", path="substance", key=("unii",))

    unii: Annotated[str, OntProp("unii", "UNII"), Field(pattern=r"^[0-9A-Z]{10}$")]
    name: Annotated[str, OntProp("name", "name"), Field(min_length=1, max_length=500)]
```

* `OntClass` (class level, via a typed `ClassVar`): local name of the `owl:Class`, label, the IRI path
  segment for instances, and the key fields. Embedded value classes have no path; their IRIs derive
  from the parent.
* `OntProp` (field level, via `Annotated`): local name of the RDF property and its label. The field's
  `Field(description=...)` becomes `rdfs:comment`.
* `OntEnum` (enum level, as a dunder attribute in the `StrEnum` body, which `Enum` never turns into a
  member): local name and label of the enumeration class.
* The class docstring becomes `rdfs:comment`. A missing docstring, a missing `__ont__`, or a field
  without exactly one `OntProp` raises `SchemaError` during generation. There are no defaults.
* All three are frozen dataclasses, so mypy checks every argument.

### Projectability rules enforced by `analyze()`

These exist because Pydantic and RDF disagree about absence. RDF has no "null" and no "present but
empty", so the Python side is constrained to the subset that RDF can represent:

* `T` (exactly one) must not have a default.
* `T | None` (at most one) must default to `None`.
* `list[T]` or `frozenset[T]` with `min_length` 0 must default to empty. With `min_length >= 1` it must
  have no default.
* Only these types are accepted: `str`, `int`, `Decimal`, `bool`, `date`, `AwareDatetime`, a
  `StrEnum` with `OntEnum`, `Ref[T]`, an embedded value model, and collections of the last three.
  `float`, naive `datetime`, `dict`, `Any` and nested unions are rejected.
* Only these Pydantic constraints are accepted: `pattern`, `min_length`, `max_length`, `ge`, `le`,
  `gt`, `lt`. Anything else (`multiple_of`, `max_digits`, a functional validator in `Annotated`) is a
  `SchemaError` or a lint failure.

### Collections: lists versus sets

RDF has no order, and the prompt forbids blank nodes (so no `rdf:List` in instance data). We therefore
separate two kinds of collection:

* **Key-identified collections are sets** (`frozenset[T]`): references (`frozenset[Ref[Product]]`),
  enum sets, and keyed embedded values (`LabelSection`, keyed by LOINC code). Their RDF form is lossless.
* **Position-identified collections are lists** (`list[T]`): unkeyed embedded values (`Ingredient`,
  `DrugInvolvement`, `Reaction`). Each item's IRI ends in its index (`.../drugs/0`), and the reverse
  mapping orders items by that index. The index lives in the IRI only; there is no extra property.

This is a deviation from the prompt's `list[T]` wording, and it is the first leak: order is a Python
concept that has to be encoded in the IRI scheme to survive.

### Rule DSL (`core/rules.py`)

```python
__rules__: ClassVar[tuple[Rule, ...]] = (
    RequiredWhen(id="serious-requires-criterion", when="serious", equals=True,
                 then="seriousness_criteria", message="..."),
    AtLeast(id="has-primary-suspect", field="drugs", where="role",
            equals=DrugRole.PRIMARY_SUSPECT, min_count=1, message="..."),
)
```

* `RequiredWhen(when, equals, then)`: if `when == equals` then `then` is present (not `None`, non-empty).
  SHACL: `sh:or ( [ sh:not [ sh:path when ; sh:hasValue v ] ] [ sh:path then ; sh:minCount 1 ] )`.
* `AtLeast(field, where, equals, min_count)`: at least `min_count` items of `field` have
  `where == equals`. SHACL: `sh:qualifiedValueShape [ sh:property [ sh:path where ; sh:hasValue v ] ]`
  with `sh:qualifiedMinCount`.
* Each rule becomes its own named `sh:NodeShape` (`shape:rule-<id>`), so SHACL reports name the rule.
* Pydantic side: the base model has one `model_validator`, marked `@rule_engine`, that evaluates
  `__rules__` with the same `Rule.holds()` the tests use. That validator is the generated projection.
* Field names in rules are strings, which mypy cannot check. `analyze()` checks them (field exists,
  types match, `then` is optional or a collection, `field` is a list of embedded values), so a typo
  fails generation.
* Any other validator must be decorated `@pydantic_only(id=..., reason=...)`. `monograph check lint`
  inspects `__pydantic_decorators__` and every `Annotated` functional validator on every model and
  fails if one is neither. The `pydantic_only` declarations are the leak inventory.

### Instance mapping and agreement

* Instance IRIs: `<base><path>/<key>` with key components percent-encoded. Base is a constructor
  argument of `RdfMapper` (default `https://example.org/monograph/`). Vocabulary IRIs live under
  `https://example.org/monograph/ontology#`, shapes under `https://example.org/monograph/shapes#`.
* A record under test is a JSON object. `core/instances.py:instantiate()` converts it into an
  unvalidated model tree with `model_construct`, lifting JSON values to Python types only where that
  is lossless (ISO date string to `date`, string to `Decimal`, enum value to member). Unknown keys are
  kept as `__pydantic_extra__`.
* Pydantic verdict: `Model.model_validate(instance)` with `strict=True` and
  `revalidate_instances="always"`, so nothing is coerced and nested values are re-checked.
* SHACL verdict: `RdfMapper.to_graph(instance)` (which maps whatever is there, including wrong types
  and extra keys), validated by pySHACL against the committed `generated/monograph.shapes.ttl`.
* **References in per-record validation.** `sh:class T` needs the referenced node's type in the data
  graph, and a single record does not contain its referents. The mapper adds a typed stub
  (`<ref> a T`) for each `Ref[T]`, which is exactly the information `Ref[T]` carries. Stubs are
  targeted by their class's node shape and fail it (they carry no data), so results whose focus node
  is not the record or one of its embedded nodes are dropped. (The first design used pySHACL's
  `focus_nodes` option instead; it turned out to skip `sh:qualifiedValueShape` silently, which the
  fixtures caught. See "Deviations".) A separate dataset-level check merges all valid fixtures without
  stubs and validates referential integrity, which Pydantic cannot do per record.
* Disagreement is a bug unless the only Pydantic failures come from `pydantic_only` validators.

### Regex safe subset (`gen/regex.py`)

Allowed: literal ASCII characters, the XPath single-character escapes (`\\ \| \. \? \* \+ \( \) \{ \}
\- \[ \] \^ \$`), character classes with ASCII ranges (optionally negated), capturing groups,
alternation, `^`, `$`, and the quantifiers `* + ? {n} {n,} {n,m}` with bounds up to 255 (the Postgres
limit). Rejected with a positioned error: `.` (dot treats newlines differently in Postgres and XPath),
`\d \w \s \b` and other class escapes (Unicode-aware in some dialects, not others), lookaround and
`(?...)` groups, back references, lazy or possessive quantifiers, and non-ASCII characters.
One known divergence stays inside the subset on purpose: `$` matches before a trailing newline in
Python `re`, which pySHACL uses, but not in Rust `regex` (Pydantic), XPath or Postgres. It is measured
by a dedicated probe, not hidden.

### Gates

| Gate | Command | What it compares |
|---|---|---|
| generation | `monograph gen --dry-run` | `analyze()` and every generator succeed |
| drift | `monograph check drift` | regenerated bytes against committed `generated/` |
| breaking | `monograph check breaking` | semantic diff of OWL+SHACL against `generated/releases/<last>/` |
| lint | `monograph check lint` | every validator is rule-generated or `pydantic_only` |
| agreement | `monograph check agreement` | Pydantic and SHACL verdicts on fixtures and real records |
| tests | `pytest -m "not integration"` | round trip, property-based agreement, unit tests |

The breaking-change check works on a semantic signature (classes, properties, individuals, and every
property-shape constraint), not on text. Removals, tightenings and new required properties are
breaking. Additions and loosenings are additive. A pattern change is breaking because subset or
superset cannot be decided in general. Any change at all with an unchanged `SCHEMA_VERSION` fails.

## Deviations from the prompt, with reasons

1. **Extra modules.** `core/spec.py` (shared IR), `core/refs.py` (`Ref[T]`), `core/instances.py`
   (unvalidated instantiation), `gen/turtle.py` (deterministic Turtle writer, because rdflib's
   serializer orders blank nodes by random IDs), `gen/regex.py`, `validate/compat.py` (breaking-change
   classifier), `validate/postgres_side.py` (loader and probe), `validate/strategies.py` (hypothesis
   strategies and single-constraint perturbations), `harness/measure.py` and `harness/report.py`.
2. **Sets for key-identified collections**, as explained above.
3. **`owl:FunctionalProperty` for `T | None` as well as `T`.** Functional means "at most one value",
   which is true of both.
4. **`DrugRole` has a fifth member, `suspect`.** openFDA follows ICH E2B, whose
   `drugcharacterization` code 1 means "suspect" with no primary/secondary distinction (that split
   exists only in the FAERS quarterly files). Ingest maps a lone suspect drug to `primary suspect`,
   since every FAERS case has exactly one, and maps code 1 to `suspect` when a report has several.
   Those reports then fail the primary-suspect rule, which is a finding about the source.
5. **`Product.routes` is a set**, because many products have more than one route.
6. **Extra CLI commands.** `monograph measure` writes the metrics (the report must be built from JSONL
   alone, so something has to write it) and `monograph release` snapshots `generated/` into
   `generated/releases/<version>/`, refusing to overwrite.
7. **Fixtures layout.** `fixtures/valid/*.json` and `fixtures/invalid/*.json` carry a small envelope
   (`class`, `record`, and for invalid ones the violated constraint). Raw openFDA records for CI live
   in `fixtures/openfda/<endpoint>.json` and go through the real ingest mapping.
8. **JSON Schema is generated, drift-checked and size-measured, but not validated against
   instances.** That needs a JSON Schema validator such as `jsonschema`, which is not on the allowed
   list. Rules are projected into it (`if`/`then` and `contains`/`minContains`).
9. **The mutation runner reuses the current interpreter** with `PYTHONPATH` pointed at the temp copy
   instead of building a new virtualenv per mutation. Dependencies are identical, and this keeps a
   suite run to minutes.
10. **The datetime mapping** (`AwareDatetime` to `xsd:dateTime`) is implemented and unit-tested on a
    test-only model. No v0.1.0 class needs a timestamp.

11. **Per-record SHACL validation filters results by focus node** instead of using pySHACL's
    `focus_nodes` option. In pySHACL 0.40.2 `focus_nodes` without `use_shapes` silently skips
    `sh:qualifiedValueShape` constraints, so the primary-suspect rule never fired. A minimal
    reproduction lives in `validate/pyshacl_probes.py` and runs as a characterization test.
12. **Foreign keys are `DEFERRABLE INITIALLY IMMEDIATE`.** The integration probe replaces a valid row
    with its invalid variant inside a savepoint, and the references are still checked before the
    savepoint ends.
13. **Extra commands.** `monograph check all` runs every gate. `monograph ingest fixtures` selects the
    offline sample. `monograph ingest map` records conformance on its own.
14. **The mutation pipeline's gates** are generation, drift, breaking, lint, agreement and the
    non-integration tests. ruff and mypy are not gates there (they check style and types, not sync),
    and `tests/test_mutations.py` is excluded because it checks the mutations against the pristine
    source, which a mutant copy is not.

## Stages

1. Scaffold (uv, ruff, mypy, pytest, Makefile, compose, layout).
2. `meta`, `refs`, `rules`, `spec`, `models`.
3. Turtle writer, regex subset, OWL, SHACL, JSON Schema generators, drift check.
4. Instance mapping, round trip, agreement on fixtures and property-based cases.
5. DDL generator and Postgres integration test.
6. openFDA ingest, conformance, real-record fixtures.
7. Breaking-change check, results harness, mutation suite.
8. Measurements, report, CI.
