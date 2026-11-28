# H1 plan: ontology-checked text-to-SQL against semantic views alone

## Why H1

H2.1 showed that the identity crosswalks are the part of the meaning layer nothing else can replace, and that
a graph store added cost but not accuracy. It could not test whether meaning helps when the schema itself
is unreadable: the generated names (`product.dosage_form`, `recall.classification`) already carried the
meaning. H1 builds a deliberately realistic warehouse in which they do not, and compares the two ways
enterprises put meaning back on top of one.

**H1: ontology-checked text-to-SQL beats semantic views alone.** The mechanism is the one the PoV cites: give
the model the ontology and a mapping to the warehouse, check the generated SQL against the ontology before
running it, and repair it on violations. The comparison is a well-built semantic layer of the kind
enterprises deploy (a semantic model with entities, dimensions, measures, joins, synonyms and descriptions,
plus business-named SQL views).

H1 reuses everything that exists and forks nothing: the H6 model and generators, the H2.1 dataset (identity
on) and its loaders, the agent loop and response cache, the split runner, the bootstrap, the spend guard,
the pin and PREREG machinery and the clean-tree gate. Two of those gain a hook whose default keeps today's
behaviour byte for byte:

* `AgentLoop` takes the answer model to validate `submit_answer` against (default: H2's `FinalAnswer`).
  H1's answers are warehouse values and measures, which can be non-integer.
* `run_split` takes the scoring function (default: H2's `score_record`).

`agent/loop.py` and `qa/run.py` are pinned by H2 and H2.1 (`experiments/h2*/frozen/code/`). Both experiments
are finished; their "a rerun is allowed now" tests already skip once the live code moves on, and their pins
are unchanged. H4's pins are not touched.

## Stage 1: the warehouse, a new generated projection

`src/monograph/gen/warehouse.py` generates, from the H6 `SchemaSpec` (the H2 classes, identity on) and one
explicit, versioned naming table (`WAREHOUSE_VERSION`), four artifacts in `generated/`:

| File | What |
|---|---|
| `h1.warehouse.mapping.json` | the mapping: every ontology class and property to its table and column, with the transform (R2RML in spirit) |
| `h1.warehouse.ddl.sql` | schema `wh`: the warehouse tables, no comments |
| `h1.warehouse.load.sql` | `INSERT ... SELECT` from the canonical schema `h2`, generated from the mapping, plus the code table rows |
| `h1.warehouse.inverse.sql` | views in schema `wh_inverse` that rebuild every canonical table from the warehouse through the mapping |

All four are drift-checked like every other projection, and mutation M19 hand-edits the generated mapping
(caught by the drift gate). The data is the H2.1 baseline cell's (identity on): the same instances, the same
crosswalks. Only the representation changes.

The representation is legacy-style by design:

* **Cryptic names.** `T_PRD_MSTR.PRD_CD`, `T_AE_HDR.SRS_FLG`, `X_NDC_RXN`, and so on. Entity tables carry an
  integer surrogate key (`PRD_SK`), assigned in business-key order, and references join on surrogate keys.
* **Codes instead of labels.**
  * Route and dosage form are stored as their NCI Thesaurus codes (`C38288`, `C42998`). The codes come from
    the H6 source itself, where every `Route` and `DosageForm` member carries its code. They are decoded
    through one generic code table, `T_REF_CD (CD_DOM, CD_VAL, CD_DSC)`, which also holds product type,
    source system and drug role codes; a lookup needs both the domain and the value.
  * Flags are `Y`/`N`; recall class is `1`/`2`/`3`.
* **Legacy quirks.**
  * Dates are integer `YYYYMMDD` in the adverse event and recall tables, real dates in the label table.
  * One wide denormalized table, `T_RCL_PRD_DNRM`: one row per recall and product named, with the recall's
    attributes and the recalling firm repeated on each (a recall that names no product has one row with
    empty product columns).
  * Seriousness criteria are six `Y`/`N` flag columns on the report header.
  * A few unused legacy columns, always empty, mapped to nothing.
  * No column comments.
* **Fan-out traps.** Fact tables sit at a finer grain than the entity: routes (`T_PRD_RTE`), ingredients,
  report reactions (`T_AE_RXN`), report drugs, recall products, label products. The data has real fan-out
  (267 products with more than one route, 80 reports that list one reaction twice, packages per product), so
  a naive `COUNT(*)` double-counts.

**Equivalence.** The warehouse holds exactly the canonical data:

1. the round trip: every canonical table, rebuilt from the warehouse by `wh_inverse`, equals the canonical
   table row for row (`tests/test_h1_warehouse.py`, against Postgres);
2. every question's gold (computed over the canonical instances) is reproduced by the template's hand-written
   reference SQL on the warehouse; generation keeps a candidate only when it does, and a test re-checks the
   committed question set.

## Stage 2: the semantic layer, the strong baseline

`src/monograph/gen/semantic_layer.py` generates, from the warehouse mapping:

* `h1.semantic.views.sql`: schema `sem`, SQL views over the warehouse with business names, labels decoded
  from the code table, real dates, `Y`/`N` as booleans;
* `h1.semantic.model.yaml`: the semantic model: entities (with their keys), dimensions, measures, joins,
  synonyms and descriptions, written for analysts; the descriptions are the H6 source's, plus hand-written
  ones for the measures and joins;
* `h1.semantic.mapping.json`: each view and column back to its ontology term (the check uses it).

**Coverage rule** (fixed here, before any question exists). The semantic layer:

* models the analyst-facing classes Product, PackagedProduct, Substance, Organization, AdverseEventReport
  and Recall, with their own properties, the properties of their embedded parts that are not references to
  other classes (a product's routes and ingredient strengths, a report's reactions and its drugs' names and
  roles), and their direct joins: package to product, package to labeler, product to ingredient substance,
  recall to product, recall to recalling firm;
* has named measures, at least 8, each with its grain stated (for example `serious_report_count`, the number
  of distinct serious reports);
* decodes every code table into labels;
* does **not** model the crosswalks (product to RxCUI, report drug to RxCUI, 11-digit NDC, firm aliases),
  salt-to-moiety links, brand-to-generic links, labels and label sections, or ClinicalDrug.

Every question is tagged inside or outside coverage by code: a question is inside when every ontology class
and property its template needs is in the generated semantic model's coverage set, and outside otherwise.

## Stage 3: the ontology check (OC)

`src/monograph/sqlcheck/` parses the generated SQL with `sqlglot` (the one new dependency), maps every table
and column back to ontology terms through the mappings (warehouse tables through the warehouse mapping, views
through the semantic mapping), and checks:

1. **Joins.** Every equi-join follows an ontology relation: a reference to class C joined to C's identity,
   or two references to the same class C, or an embedded part joined to its owner. Anything else (two
   identities of different classes, a reference joined to the wrong class, a join on a non-key column, a
   code table joined without its domain) is a violation.
2. **Codes.** Every literal compared with a coded column (`=`, `<>`, `IN`) is a valid code for that
   property's enum in that column's representation (an NCI code, `Y`/`N`, `1`/`2`/`3`, a code table value
   within the domain filtered on, a decoded label in a view). An integer `YYYYMMDD` date compared with a
   date or a date-like string is a violation.
3. **Cardinality.** `COUNT(*)`, `COUNT(x)` without `DISTINCT`, `SUM` and `AVG` are a likely fan-out double
   count when the rows they aggregate come from a join of an entity with a table at a finer grain than that
   entity (a multi-valued property, an embedded list, a bridge or the wide recall table), unless they count
   distinct values of the entity's key.
4. **Existence.** Every table and column referenced exists and is mapped to an ontology term (an unused
   legacy column is not).

Feedback names the violated rule, the SQL fragment and the ontology terms involved, and says what would be
valid (for codes: the valid codes of that property with their labels). It is built from the SQL text and the
mappings only: the checker never sees the question, its gold or the database. A leak test plants sentinel
values as gold and anchors and asserts that no tool result ever contains them.

The check has its own unit tests on hand-written good and bad queries, one or more per rule and per false
positive the design knowingly accepts.

**How the check runs.** In the OC arms every `run_sql` call is checked before it executes. A query with
violations is not executed; the feedback is returned instead, as one repair round. At most 3 repair rounds per
question: after the third, a query with violations is executed as written and its result carries the
remaining violations. In every other arm the check runs in shadow on every executed query (nothing is sent
to the model), so the check's quality can be measured on queries it did not influence.

## Arms (2 x 2 plus a floor)

Every arm gets the same base prompt (business vocabulary, the answer rules), the same model and settings, at
most 12 data tool calls, the same token budget, the same answer schema and the same **execution-error retry**:
a query that fails in the database returns its error, and the model may retry; after the 4th failed execution
of a question (the first plus 3 retries), `run_sql` executes nothing more and says so.

| | Execution-error retry only | Plus ontology check and repair |
|---|---|---|
| **Semantic-layer context**: the semantic model and the view definitions; the raw warehouse tables are listed and queryable too | SV | SV+OC |
| **Ontology context**: classes, properties, descriptions, cardinalities and enum codes, plus the mapping to the warehouse | ONT | ONT+OC |

**W-raw** (floor): the warehouse DDL only, no semantic layer, no ontology.

Tools: `describe_data` (the arm's context, the same text for the arm and its OC twin) and `run_sql`
(read-only, one statement, statement timeout, row cap). Each arm's database role can read only what the arm
is given: SV roles read `wh` and `sem`; ONT and W-raw roles read `wh`. No role reads the canonical schema.

## Answers and scoring

`submit_answer` takes kind `values` (a list of strings), `number`, or `none`, plus evidence (the tool calls
that produced it). Gold is computed by code over the canonical instances (`qa/index.py`), never by an LLM.

* **values:** correct when the answered set equals the gold set. Each gold entity accepts its business key
  (product NDC, package NDC, UNII, report id, recall number, RxCUI, firm code) and, for firms and substances,
  its name; matching ignores case and surrounding space. Order does not matter; duplicates count once.
* **number:** correct when |answer - gold| <= max(0.01, 0.001 x |gold|). Counts are integers, so this is
  exact for them; it allows rounding for averages.
* **none:** correct when the answer is `none` (or an empty `values` list).

**Failure categories**, for a wrong answer, the first that applies:

1. refusal;
2. budget exhausted (tool, turn or token budget, truncation, or no answer);
3. SQL execution error after retries: the question used up its execution retries;
4. hallucinated table or column: a query referenced a table or column that does not exist;
5. outside-coverage miss (SV arms): the question is outside coverage and no query read a raw warehouse table;
6. identity miss: the question needs a crosswalk and no query read it;
7. wrong or invalid code: an evidence query compares a coded column with an invalid code, or with a valid
   code other than the ones the question's gold was computed with;
8. fan-out double count: an evidence query has a rule 3 violation, or the answer is a number above gold on a
   fan-out (Q5) question;
9. wrong join path: an evidence query has a rule 1 violation;
10. other wrong result.

"Evidence queries" are the `run_sql` calls the answer cites, or the last successful one if it cites none.

## Questions

Analyst language, never table or column names. Hand-written phrasings, several variants per template.
Deterministic gold by code; generation with a fixed seed; 60 per type, 15 dev and 45 test, split committed.

| Type | Definition | Role |
|---|---|---|
| Q1 | single-entity aggregates inside semantic coverage | control: SV should do well |
| Q2 | multi-table joins with coded filters (route, form, flags, recall class) | core |
| Q3 | identity-dependent (brand vs generic, salt vs moiety, RxCUI) | core, mostly outside coverage |
| Q4 | other questions outside coverage that need raw tables | core |
| Q5 | fan-out traps: the right answer needs distinct counting at the right grain | core |
| Q6 | unanswerable (names an entity or code that does not exist) | must answer `none` |

A Q5 candidate is kept only when the naive count differs from the distinct count, so every Q5 question is a
real trap. **Ceiling rule** (from H2.1): a type at 0.95 or higher in every arm on dev is marked
non-informative in the PREREG.

## Models, runs and budget

* `claude-haiku-5-5`: all 5 arms, every type.
* `claude-sonnet-5-5`: SV, ONT+OC and W-raw on Q2 to Q5.

Both ids are pinned. After dev the test estimate is printed and the work stops for approval; a test run
refuses to start without the approved cap in `experiments/h1/budget.json`, and stops at it (cumulative,
reruns included). Test runs start only from a clean tree, and only from the pinned artifacts named in
`docs/h1/PREREG.md`.

## Metrics

* Execution accuracy (as scored above), with 95% bootstrap intervals throughout.
* Failure categories per arm.
* Accuracy inside and outside semantic coverage, separately for every arm.
* **The check's quality**, per arm:
  * **precision:** of the questions whose evidence queries the check flags, the share answered wrongly;
  * **recall:** of the wrongly answered questions, the share whose evidence queries the check flags;
  * in the non-OC arms these use the shadow check, on queries the check did not influence;
  * **false blocks** (OC arms): every blocked query is also executed in shadow (its result is never sent).
    A blocked query is **gold-equivalent** when its result already answers the question correctly. A false
    block is a gold-equivalent blocked query whose question ended wrong; the false-block rate is false
    blocks over blocked queries, also reported over questions.
  * repair rounds used.
* Tool calls, tokens, USD per correct answer, p50 and p95 latency.

## Verdicts (to be fixed in the PREREG)

**H1 holds** if, for at least one model, ONT+OC beats SV by at least 0.10 on Q2 to Q5 combined with the
interval excluding 0, and ONT+OC is no more than 0.05 below SV on Q1. **H1 fails** if SV is within 0.05 of
ONT+OC on Q2 to Q5 for both models; "fails" takes precedence. Otherwise it **partly holds**. Sonnet has no
Q1 runs, so for Sonnet the Q1 condition uses Haiku's Q1 result.

**Inside coverage only** (secondary): the same rule restricted to Q2 and Q5 questions inside coverage.

**Attribution**, always reported: the context effect (ONT minus SV, ONT+OC minus SV+OC), the check effect
(SV+OC minus SV, ONT+OC minus ONT) and the floor (each arm minus W-raw).

## Layout

```
src/monograph/
  gen/warehouse.py        the warehouse and its mapping (generated)
  gen/semantic_layer.py   the semantic model and views (generated)
  sqlcheck/               parse, map back to the ontology, check, feedback
  qa/h1_templates.py      templates, phrasings, gold, reference SQL
  h1/                     the arms, runs, pins, analysis, report, CLI
docs/h1/                  PLAN.md PREREG.md DEVIATIONS.md RESULTS.md
experiments/h1/           frozen/ budget.json
results/h1/
```
