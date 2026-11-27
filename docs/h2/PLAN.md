# H2 plan: graph tools win on multi-hop and identity questions

## Hypothesis, as designed

A graph agent against a text-to-SQL agent confounds the **store** (graph or relational) with the
**interface** (free-form queries or parameterised tools). H2 is therefore a 2 x 2:

| | Free-form queries | Parameterised tools |
|---|---|---|
| Relational (Postgres) | S-free: text-to-SQL | S-tools: the tool set in SQL |
| Graph (Apache AGE in the same Postgres) | G-free: text-to-Cypher | G-tools: the tool set in Cypher |

S-tools and G-tools expose one registry: identical names, input models, output models and
descriptions. Only the backend differs, and a test proves both backends return identical results on a
fixture set before any LLM run. "The win comes from the tools, not the graph" is a valid outcome.

## Data: what is loaded, and why the H6 samples are not enough on their own

All H6 and H4 data is loaded. Measured before planning, the H6 samples barely connect: **0 of 200 H6
recalls name a product that is in the loaded data** (only 38 carry `openfda.product_ndc` at all), and
only 21 of 1,454 event drug entries carry a single RxCUI, which is when the H6 mapper links them. On
that data the multi-hop templates would have empty gold almost everywhere, and T2/T4 would measure
nothing.

So the H2 frame adds targeted, cached fetches through the existing clients (`OpenFdaClient`,
`DailyMedClient`), deterministic given the cache:

1. Recalls: openFDA enforcement records with `_exists_:openfda.product_ndc` (paged, fixed size).
2. Products: the NDC directory record for every product NDC those recalls name, plus the H6 and H4
   NDC records.
3. Events: for a fixed seeded sample of the frame's RxCUIs, a fixed number of openFDA event reports
   whose drug harmonization names that RxCUI.
4. SPLs (DailyMed): the structured part of each frame product's label, for ingredient pairing and
   active moieties (below). H4's cached SPLs are reused.
5. RxNav (below), for the crosswalk, clinical drug names and term types, and brand to generic.

The frame, its sizes and the fetch counts are written to `results/h2/` and the PREREG. Raw caches stay
under `data/raw/` (gitignored, as in H6 and H4); the question set and its gold are committed, so the
experiment is reproducible from the committed files plus the caches.

**Ingredient pairing.** openFDA NDC records list UNIIs without names and in no stated order, so the H6
mapper leaves multi-ingredient products unpaired. The SPL structured part pairs each ingredient's
name, UNII and strength. Where the NDC record cannot be paired, the product's ingredients come from its
SPL (H4's `parse_spl`), and the loader records which source each product's ingredients came from.

Every entity is validated by the H6 Pydantic model before loading. Rejected candidates are counted per
reason and never repaired, as in H6.

## Model changes (through the H6 pipeline; the gates decide the bump, expected 2.2.0, additive)

| Change | Source | Provenance recorded |
|---|---|---|
| `ProductDrugMapping` (new entity): Product to ClinicalDrug | openFDA `openfda.rxcui` on the NDC record; RxNav `ndcproperties` | `source` enum plus `evidence` text (endpoint, query, RxNorm data version) |
| `ClinicalDrug.term_type`, `ClinicalDrug.name` filled, `ClinicalDrug.generic_drug` (SBD to SCD) | RxNav `rxcui/{id}/properties`, `rxcui/{id}/related?tty=SCD` | in the ClinicalDrug's `name_source` |
| `PackagedProduct.ndc11` | derived: the FDA 10-digit to 11-digit (5-4-2) padding rule, cross-checked against RxNav `ndcItem` | rule in the field description; disagreements reported |
| `Substance.active_moiety` (hasActiveMoiety, salt to moiety) and `active_moiety_source` | SPL `activeIngredient/ingredientSubstance/activeMoiety`, the moiety data H4 used | `spl:<set id>` |
| `Organization.aliases` (OrganizationAlias: name, source, evidence) | name variants in NDC directory, SPL author, enforcement and RxNav records | each alias carries its source and the evidence that ties it to the firm |
| `DrugInvolvement.harmonized_clinical_drugs` | openFDA's harmonization of the reported drug (`patient.drug.openfda.rxcui`) | the field description names openFDA harmonization |

Organization identity: two names are aliases of one firm when they are tied by an identifier, not by
similarity. The identifiers are the NDC labeler code (the first NDC segment, assigned by FDA to one
labeler) and, for recalls, the recalled product's labeler code when the recalling firm's normalized
name equals one of that labeler's names. Normalization is a fixed list (case, punctuation, whitespace,
corporate suffixes); anything else is not an alias. The canonical name is the NDC directory name used
on the most products, ties broken by sort order.

## Property graph projection (new generated artifact, `gen/age.py`)

Generated from the same `SchemaSpec` as OWL, SHACL, JSON Schema and DDL, written to
`generated/monograph.age.json` (labels, properties with their descriptions, edge labels with their
endpoints) and drift-checked like the other artifacts:

* one vertex label per entity class; vertex properties are the scalar and enum fields plus `iri`;
* one edge label per object property (reference field), from the owning vertex to the target;
* an embedded value that holds exactly one reference and nothing else that needs a node becomes an
  edge with properties (Ingredient: `(Product)-[:INGREDIENT {strength_value, strength_unit}]->(Substance)`),
  which is how a graph modeller would draw it; any other embedded value becomes a vertex with an edge
  from its owner;
* the loader (`graph/loader.py`) builds the AGE graph from the Postgres tables, with GIN indexes on
  vertex properties.

A mutation that drops an edge label must be caught by the gates (drift plus a loader/projection test).

**Schema descriptions for the agents** are generated from the field descriptions on both sides: the
DDL gains `COMMENT ON TABLE/COLUMN` (so `describe_schema` for SQL returns DDL with comments) and the
AGE description carries the same text per label and property. A test checks both cover the same set of
field descriptions.

## Infrastructure

* `apache/age:release_PG18_1.8.0`, the official image, pinned by digest (PostgreSQL 18.6, AGE 1.8.0),
  started with `shared_preload_libraries=age` (a non-superuser cannot `LOAD 'age'`). A separate compose
  service, so the H6 integration tests keep plain `postgres:18`.
* One database holds both: relational tables in schema `h2`, the graph `drug` in AGE.
* Read-only role `h2_agent`: `SELECT` only, `default_transaction_read_only = on`, `statement_timeout`
  fixed, row cap enforced by the tool layer (it fetches cap + 1 rows and reports truncation). Probed
  before planning: writes and DDL fail for this role, and the timeout fires.

### AGE limitations found while probing (more will be recorded as they come up)

* No `shortestPath()` / `allShortestPaths()`: `find_path` uses a bounded variable-length match ordered
  by length.
* No `CALL { ... }` subqueries.
* `cypher()` needs a column definition list that matches the `RETURN` items, so `run_cypher` derives
  the column list from the query's final top-level `RETURN`; when it cannot, the error is returned to
  the agent like any query error.
* Parameters must be a single agtype argument (psycopg binds it as `%s::agtype`).

## Tools

One registry (`tools/registry.py`): each tool is a Pydantic input model, an output model and a
description. Two backends implement it (`tools/sql_backend.py`, `tools/cypher_backend.py`). Minimum set:
`resolve_entity`, `get_entity`, `neighbors`, `find_path`, `products_with_substance`, `events_for`,
`recalls_for`, `count`. Results are sorted and capped identically. `resolve_entity` uses the H4
deterministic resolver (`normalize_name`, NDC normalization) plus aliases and crosswalks.

The free-form arms get `run_sql` or `run_cypher` (read-only, same timeout and row cap) and
`describe_schema`.

`tools/mcp_server.py` exposes the same registry over stdio with the official `mcp` SDK (the one new
dependency; AGE needs no driver, psycopg talks to it). A test lists the tools and calls one. The
experiment calls tools in-process.

## Questions (deterministic gold)

Templates over the loaded data, gold computed by code from the validated instances, 3 to 5 phrasings
per template written by hand. About 300 questions, 50 per type, 10 dev and 40 test per type, fixed
seed, split committed.

| Type | Templates (shapes) | Role |
|---|---|---|
| T1 single-hop | labeler of a package NDC; active substances of a product NDC; products in a recall | control |
| T2 multi-hop | firms that recalled products containing substance S; labelers of products containing S; substances in products recalled by firm F | core |
| T3 identity | a T1-shaped question asked through an alias: 11-digit NDC without hyphens; RxCUI instead of NDC; moiety instead of salt; firm name variant; brand drug to generic equivalents | core |
| T4 multi-hop + identity | a T2 template asked through a T3 alias | core |
| T5 aggregate | count of serious reports in a period for products with substance S; count of recalls of a class for a firm | control |
| T6 unanswerable | a plausible entity that does not exist; real entities with no connecting path | must answer `none` |

* Identity variants are drawn only from recorded alias or crosswalk pairs.
* T5 counts are drawn only where the count is at least 1, so `none` belongs to T6 alone.
* **T1 deviation from the brief's example.** "Dosage form of NDC X" has a text answer, which the fixed
  answer schema (IDs, a number or `none`) cannot hold. T1 uses single-hop questions with ID answers.

**Final answer schema** (same for every arm): `kind` (`ids`, `number`, `none`), `ids`, `number`, and
`evidence` (indices of the tool calls that produced it). The harness also logs every call.

A shared glossary in the base system prompt defines the relations questions use ("contains",
"contains, counting salt forms", "recalled", "labels", "a report involves a product"), identically for
all arms.

## Agent loop and arms

`agent/loop.py`: a minimal tool-use loop, no framework, client injected. Every arm gets the same base
system prompt, model, settings, final answer tool, a budget of at most 12 data tool calls (the answer
call does not count), a per-question output token budget, and the same row cap. Thinking blocks are
replayed unchanged (Sonnet 5.5 and Haiku 5.5 tie them to the conversation). Caching: an explicit marker
on the static system prefix plus top-level automatic caching for the growing tail. No refusal fallback;
a refusal is recorded as such. The response cache stores full content blocks so cached replays are
exact.

Each arm's dev iteration is logged (prompt versions per arm, runs per version). If one arm got
noticeably more prompt work, the PREREG says so as a limitation.

## Metrics and verdict

Per model x arm x question type: answer accuracy (exact set match for IDs, exact value for counts,
correct `none` for T6), set F1 as secondary; failure categories (wrong entity resolution, missing hop,
query error or timeout, hallucinated ID, budget exhausted, refusal); tool calls per question, tokens
including cache, USD per correct answer, p50/p95 latency; 95% bootstrap intervals on accuracy and on
every arm difference (paired over questions).

Verdict, exactly as the brief: **fails** if S-free is within 0.05 of G-tools on T2+T3+T4 for both
models (checked first); **holds** if for at least one model G-tools beats S-free by at least 0.10 on
T2+T3+T4 with the interval excluding 0, and G-tools is no more than 0.05 below S-free on T1; otherwise
**partly holds**. Store effect (G-tools minus S-tools, G-free minus S-free) and interface effect
(S-tools minus S-free, G-tools minus G-free) are reported regardless; if the store interval includes 0
and the interface interval does not, the report says the win comes from the tools, not the graph.

## Stages

1. AGE image, projection generator, drift and mutation gates.
2. Identity layer and model bump.
3. Frame fetch, loader, read-only role.
4. Tool registry, both backends, backend-equivalence test.
5. MCP adapter.
6. Question generation and gold.
7. Agent loop and the four arms with a fake client.
8. Dev iteration.
9. PREREG commit.
10. Test runs (cost estimate first; stop above USD 40).
11. Report.
12. PR.

## Findings and decisions while building (stages 1 to 7)

* **Frame.** 1,000 recalls that name product NDCs (of 3,268 such recalls in openFDA), the NDC directory
  records of 1,880 of their 1,947 product NDCs, 1,028 SPLs, RxNav for 2,283 products and 2,438 RxCUIs, and
  994 event reports for 400 sampled RxCUIs. Loaded: 866 substances, 646 organizations, 8,468 clinical
  drugs, 2,132 products, 4,713 packages, 892 labels, 1,422 reports, 1,161 recalls (147,515 rows; 48,544
  vertices; 138,484 edges, every label and edge source verified against the tables).
* **Identity layer as loaded.** 311 salt-to-moiety links, 377 brand-to-generic links, 239 organization
  aliases (48 differ from the canonical name even after normalization), 576 recalling firms tied to a
  labeler. The derived 11-digit NDC agrees with RxNav's `ndcItem` on all 5,892 packages checked.
* **Rejects, never repaired.** 36 products fail `finished-rx-otc-requires-route`, 115 products cannot be
  paired (no SPL) and are dropped, 13 labels have empty sections, 1 recall reason has a control character,
  1 report has no suspect drug. References to entities not in the frame are dropped and counted.
* **`neighbors` and `get_entity` take up to 50 ids** (the brief's single-id form is the one-element case):
  with one id per call, a multi-hop walk over a set of entities cannot fit the 12-call budget, which would
  penalize the tools arms on T2 for a reason that has nothing to do with tools.
* **Gold is independent of the tools.** It is computed over the validated instances (`qa/index.py`); each
  template's tool plan must reproduce it exactly on the SQL store, and every question is re-checked on the
  Cypher store (0 disagreements).
* **Two read-only roles**, one per store, so a free-form arm cannot query the other store even by breaking
  out of `cypher()` quoting.
* **Response cache keyed by arm**: S-tools and G-tools send byte-identical requests and get identical tool
  results by design, so without the arm in the key G-tools would replay S-tools' samples and the store
  effect under tools would be exactly 0 with no interval.
* **AGE 1.8.0 limitations** found so far: no `shortestPath()`, no `CALL { }`, no `[:A|B]` alternation,
  `cypher()` needs a column definition list, a variable bound by `UNWIND`/`WITH` cannot be reused in a later
  `MATCH` pattern, an internal error on lists of graph elements ("unknown type of agtype container"), and
  grouping stricter than openCypher. None blocks the experiment.
* Stage 7 (agent loop and arms) was built before stage 6 (questions) while the frame fetch ran.
