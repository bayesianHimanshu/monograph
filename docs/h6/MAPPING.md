# H6 mapping rules: Pydantic to OWL, SHACL, JSON Schema, Postgres and RDF instances

The source of truth is `src/monograph/core/models.py`. `core/spec.py:analyze()` reads it into a
`SchemaSpec`; every generator and the instance mapping read only that. This file states every rule.
If code and this file disagree, the code is wrong.

Namespaces: vocabulary `mono:` = `https://example.org/monograph/ontology#`, shapes `shape:` =
`https://example.org/monograph/shapes#`, instances under a configurable base (default
`https://example.org/monograph/`).

## Classes

| Python | OWL | SHACL | Postgres |
|---|---|---|---|
| model class with `__ont__ = OntClass(name=N, ...)` | `mono:N a owl:Class` | `shape:N a sh:NodeShape ; sh:targetClass mono:N ; sh:closed true ; sh:ignoredProperties (rdf:type)` | table `snake(N)` with `iri text PRIMARY KEY` (entities only) |
| docstring | `rdfs:comment` (whitespace collapsed) | | |
| `OntClass.label` | `rdfs:label` | `rdfs:label` | |
| Python base class with its own `__ont__` | `rdfs:subClassOf` | | |
| `OntClass(abstract=True)` | class only | open node shape with no properties | no table |
| `OntClass.key` | | | `UNIQUE (key columns)` |

A class without a docstring or without its own `__ont__` fails generation.

## Fields

Each field carries exactly one `OntProp(name, label)`. The property IRI is `mono:<name>`. The same
property name may be used by several classes only with the same kind and range (checked). Its
`rdfs:domain` is the class, or `owl:unionOf` the classes when shared. `Field(description=...)` becomes
`rdfs:comment` when all uses agree.

### Kinds and ranges

| Python type | OWL | SHACL property shape | Postgres |
|---|---|---|---|
| `SafeIdentifier` (a `str` with no control characters) | `owl:DatatypeProperty`, range `xsd:string` | `sh:datatype xsd:string ; sh:not [ sh:pattern "[U+0000-U+001F U+007F-U+009F]" ]` | `text` plus `CHECK (x !~ '[\x01-\x1f\x7f-\x9f]')`; NUL is rejected by the text type |
| `SafeText` (a `str` with no control characters except tab, LF, CR) | same | same, with tab, LF and CR left out of the class | same, with `\x09`, `\x0a`, `\x0d` left out |
| plain `str` | rejected: every string must say which charset it allows | | |
| `int` | range `xsd:integer` | `sh:datatype xsd:integer` | `bigint` |
| `Decimal` | range `xsd:decimal` | `sh:datatype xsd:decimal` | `numeric` plus `CHECK (x NOT IN ('NaN','Infinity','-Infinity'))`, because xsd:decimal has no such values |
| `bool` | range `xsd:boolean` | `sh:datatype xsd:boolean` | `boolean` |
| `date` | range `xsd:date` | `sh:datatype xsd:date` | `date` |
| `AwareDatetime` | range `xsd:dateTime` | `sh:datatype xsd:dateTime` plus `sh:pattern "(Z\|[+\-][0-9]{2}:[0-9]{2})$"`, since xsd:dateTime alone allows naive values | `timestamptz` (cannot reject naive input) |
| `float`, naive `datetime`, `dict`, `Any`, other unions | rejected | | |
| `StrEnum` with `__ont__ = OntEnum(...)` | `owl:ObjectProperty`, range the enum class | `sh:in ( mono:Enum-MEMBER ... )` | `text CHECK (x IN (...))`, or `text[]` with `CHECK (x <@ ARRAY[...])` for sets |
| `Ref[T]` | `owl:ObjectProperty`, range `mono:T` | `sh:nodeKind sh:IRI ; sh:class mono:T ; sh:pattern <IRI pattern> ; sh:not [ identifier charset ]` | `<field>_iri text REFERENCES t (iri) DEFERRABLE` plus the IRI pattern as CHECK |
| embedded value model | `owl:ObjectProperty`, range `mono:T` | `sh:nodeKind sh:IRI ; sh:class mono:T ; sh:node shape:T` | child table `<owner>__<field>` |

Enum members become `mono:Enum-NAME a owl:NamedIndividual, mono:Enum ; rdfs:label "value"`.

### Cardinality

| Python | Meaning | OWL | SHACL | Postgres |
|---|---|---|---|---|
| `T` (no default allowed) | exactly one | `owl:FunctionalProperty` | `sh:minCount 1 ; sh:maxCount 1` | `NOT NULL` |
| `T \| None = None` (default required) | at most one | `owl:FunctionalProperty` | `sh:maxCount 1` | nullable |
| `list[T] = []` or `frozenset[T] = frozenset()` | zero or more | | (none) | |
| `Field(min_length=n)` on a collection (no default allowed when n >= 1) | at least n | | `sh:minCount n` | `cardinality() >= n` for enum arrays; not enforceable for child or join tables |
| `Field(max_length=m)` on a collection | at most m | | `sh:maxCount m` | same as above |

`list[T]` is ordered and position-identified; `frozenset[T]` is unordered and key-identified.
References and enum values always use `frozenset`. Embedded values use `list` when they have no key
and `frozenset` when `OntClass.key` is set. Collections of plain scalars are not supported.

### Field constraints

| Pydantic | SHACL | Postgres |
|---|---|---|
| `pattern`, written `Field(pattern=safe(...))` | `sh:pattern`, plus `sh:maxLength` when the pattern is fully anchored and bounded (the longest match), which a correct engine finds redundant and which closes pySHACL's final-newline gap | `CHECK (x ~ 'pattern')` |
| `min_length`, `max_length` on `str` | `sh:minLength`, `sh:maxLength` | `CHECK (char_length(x) >= n)` and `<=` |
| `ge`, `le`, `gt`, `lt` on `int` or `Decimal` | `sh:minInclusive`, `sh:maxInclusive`, `sh:minExclusive`, `sh:maxExclusive` | `CHECK (x >= n)` and so on |
| anything else (`multiple_of`, `max_digits`, `strict`, an `Annotated` validator) | rejected by the analyzer, or by the lint if it is a validator | |

### Safe regex subset

See `core/regex.py`. `safe()` checks a pattern and returns its canonical form, in which `\d` becomes
`[0-9]` (`0-9` inside a class). Every target, Pydantic included, reads the canonical text, because
`\d` matches any Unicode digit in Rust, Python and XPath. The analyzer rejects a pattern that is not
already canonical, so a raw `\d` cannot reach Pydantic. Other constructs outside the subset fail with a
position and a reason. pySHACL evaluates `sh:pattern` with Python `re`, where `$` also matches before a
final newline; the charset `sh:not` and the implied `sh:maxLength` close that gap without changing
the patterns.

## Cross-field rules

| Rule | Pydantic | SHACL | Postgres | JSON Schema |
|---|---|---|---|---|
| `RequiredWhen(when=(Condition(f1, values1), ...), then)` (all conditions must hold) | rule engine validator | `shape:rule-<id> sh:or ( [ sh:not C1 ] [ sh:not C2 ] ... [ sh:path then ; sh:minCount 1 ] )`, where Ci is `[ sh:path fi ; sh:hasValue v ]` for one value or `[ sh:path fi ; sh:minCount 1 ; sh:in (...) ]` for several | `CHECK (f1 IS NULL OR f1 NOT IN (...) OR ... OR then present)` when all fields are columns of one table | `if` (enum per field) / `then` |
| `AtLeast(field, where, equals, n)` | rule engine validator | `shape:rule-<id> sh:property [ sh:path field ; sh:qualifiedValueShape [ sh:property [ sh:path where ; sh:hasValue v ] ] ; sh:qualifiedMinCount n ]` | not enforceable (counts child rows) | `contains` / `minContains` |
| `@pydantic_only(id, reason)` validator | the validator | none | none | none |
| `@pydantic_only(id, reason, unique=UniqueWithin(field, keys))` | the validator | none (indirect when the keys are the item key: duplicates mint one IRI) | `UNIQUE (owner_iri, key columns)` on the child table | none |
| `AtLeast` with several `values` | rule engine validator | the qualified shape uses `sh:in ( ... ) ; sh:minCount 1` instead of `sh:hasValue` | not enforceable | `contains` with `enum` |

## Instances (`rdf/mapping.py`)

| Thing | IRI |
|---|---|
| entity | `<base><path>/<key>[/<key2>]`, key values percent-encoded; a missing key becomes `~missing` |
| single embedded value | `<parent IRI>/<property name>` |
| keyed embedded value in a set | `<parent IRI>/<property name>/<key>` |
| list item | `<parent IRI>/<property name>/<index>`; the reverse mapping orders items by this index |

No blank nodes. Every node gets `rdf:type`. Literals are typed by the Python value's own type, not the
field's declared type, so a wrong type reaches SHACL as a wrong datatype. Values with no faithful RDF
form (a list where a scalar belongs) become literals of datatype `mono:UnmappedValue`. Unknown keys
become triples on `mono:<key>`, which the closed shape rejects. Decimals use the canonical xsd:decimal
lexical form (no exponent).

For per-record validation, `to_graph(..., stubs=True)` adds `<ref> a mono:T` for each `Ref[T]`, and
results whose focus node is not the record or one of its embedded nodes are dropped. For dataset
validation no stubs are added, so `sh:class` checks that referents exist.

Reading back (`from_graph`) requires every triple about a node to map to a field; anything else is an
error, which is what makes the round trip a real test.

## DDL profiles

`generated/monograph.ddl.sql` (default profile) has plain foreign keys. `generated/monograph.test.ddl.sql`
(test profile) makes every foreign key `DEFERRABLE INITIALLY IMMEDIATE`, which the integration probe
needs to replace a row inside a savepoint. Both are drift-checked; they differ only in that clause.

## JSON Schema

Pydantic's own `models_json_schema` in validation mode, one `$defs` entry per concrete class, plus
`x-rdf-class`, `x-rdf-property` and the rule projections above. It is drift-checked but not validated
against instances (that needs a JSON Schema validator, which is not an allowed dependency).

## Releases and the breaking-change gate

`monograph check breaking` enforces, in order:

1. `SCHEMA_VERSION` has a snapshot in `generated/releases/<version>/`, and that snapshot equals the
   current generation. A version bump is therefore complete only with `monograph release` in the same
   change; releases are immutable.
2. Against the previous release (the highest below `SCHEMA_VERSION`), any breaking change needs a
   major bump.
3. Every consecutive pair of releases obeys rule 2.

The classifier is fail-closed: OWL and SHACL statements that the signature does not model explicitly
are kept as residuals, and any residual difference counts as breaking.

## Derived fields and graph-context shapes (from 2.1.0, used by H4)

* `Derived(by=..., required_in_context=...)` marks a field that deterministic code fills, never an
  extractor. The extraction JSON Schema (`generated/monograph.extraction.schema.json`) leaves derived
  fields out; everything else about the field projects as usual.
* `generated/monograph.context.shapes.ttl` holds graph-context shapes: the classes reachable from
  `LabelExtraction`, with every `required_in_context` field required. They are validated against the
  extraction merged into the reference graph without reference stubs, so `sh:class` means the node exists.
* `__extraction_only__` declares graph-context checks with no SHACL Core form, the way `pydantic_only`
  declares record checks. They appear in the coverage matrix as not enforced by SHACL or Postgres.
* The JSON Schema generator writes every string and integer constraint from the SchemaSpec instead of
  trusting Pydantic's rendering, which drops `pattern` for validator-wrapped strings.
* Postgres identifiers longer than 63 characters keep a readable prefix and end in a hash of the full
  name, so two constraints can never collide through Postgres's silent truncation.
* The breaking-change classifier treats constraints and rules on a class the previous release did not
  have as additive: they cannot invalidate existing data.
