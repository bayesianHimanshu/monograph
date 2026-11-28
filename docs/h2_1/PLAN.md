# H2.1 plan: what made H2's multi-hop and identity questions answerable

## Why H2.1

H2 failed at a ceiling: every arm scored 0.97 to 1.00 on multi-hop and identity questions with both models.
Its report inferred that the identity layer (crosswalks, moiety links, generic links, firm aliases), not the
store, did the work, but nothing measured that. H2.1 is a separate, separately pre-registered experiment
that measures it, and gives the graph store a fair chance on questions where its native strength
(variable-length traversal) should matter.

It reuses the H2 setup and does not fork it: the AGE image, the projection generator, the tool registry with
its two backends, the agent loop, the response cache and the scorer. Each of those gains a switch whose
default reproduces H2 byte for byte (the existing drift checks prove the H2 and H6 artifacts are unchanged).

## Design

Two ablation factors crossed with the four H2 arms (S-free, S-tools, G-free, G-tools):

| Factor | On | Off |
|---|---|---|
| Identity layer | as in H2 | data as ingested: each source keeps only its own identifiers, no derived cross-source mapping in either store |
| Schema descriptions | as in H2: DDL column comments, node/edge property descriptions | names and types only, in both stores |

Four cells: `id-on_desc-on` (the baseline), `id-on_desc-off`, `id-off_desc-on`, `id-off_desc-off` (the
hardest).

Models, both pinned by id:

* `claude-haiku-5-5`: the full factorial, 2 x 2 x 4 = 16 configurations.
* `claude-sonnet-5-5`: a confirmation set in the baseline and the hardest cell: S-free, G-free and G-tools
  (6 configurations). **Deviation from the brief, decided by the user on 2026-11-28 before any run:** the
  brief listed S-free and G-tools only, but P3 and the refined-H2 verdict compare G-free with S-free for each
  model ("for at least one model", "for both models"), which cannot be checked without Sonnet G-free.

### The identity layer, exactly

The identity layer is every mapping whose job is to say that two identifiers or names denote the same thing,
or a canonical counterpart of it. With identity off, these are absent from the data and from the generated
schema of both stores (the tables, columns and edge labels do not exist, rather than exist empty):

| Removed | H6 model field | Source of the mapping |
|---|---|---|
| Product to RxCUI crosswalk | `Product.drug_mappings`, `Product.clinical_drug` | openFDA `openfda.rxcui`, RxNav `ndcproperties` |
| Report drug to RxCUI crosswalk | `DrugInvolvement.harmonized_clinical_drugs`, `DrugInvolvement.clinical_drug` | openFDA harmonization |
| 11-digit NDC | `PackagedProduct.ndc11` | derived (5-4-2 padding rule) |
| Salt to moiety | `Substance.active_moiety` | SPL active moiety |
| Brand to generic | `ClinicalDrug.generic` | RxNav related SCD/GPCK |
| Firm aliases | `Organization.aliases` | NDC directory, RxNav, enforcement name variants |
| Firm merges | the recalling firm resolved to a labeler (630 of 1,161 recalls in the H2 frame) | normalized name plus labeler code |

Firms with identity off: a labeler is identified the way the NDC directory identifies it, by its labeler code
(one organization per code, its canonical name, no aliases; the same IRI as with identity on). A recalling
firm is identified the way an enforcement record identifies it, by its name: one organization per H6 ingest
key (the slug of the name, within enforcement records only), never merged with a labeler. Its IRI is
`organization/recalling-firm-<slug>`, so a recalling firm can never collide with a labeler that has the same
name; agents can still match the two by exact name, as the resolver may.

Measured on the H2 frame before planning: of the 630 recalls whose firm H2 merged into a labeler, 219 match
the labeler's canonical name exactly, 320 match after case and punctuation, and 91 only after suffix
normalization or through a recorded alias. An ingest-key ablation (slugs everywhere) would have kept 539 of
the 630 merges, so it would not remove firm identity.

Kept in every cell, because they relate different things and the source record itself carries them: a recall
names products, a label covers products, a package belongs to a product and is labeled by a firm, a product
has active ingredients, a report has drug entries and reactions. RxNorm concepts stay as nodes with their own
name and term type (RxNorm's own identifiers), but with identity off nothing links to them. Entity sets are
the same in both identity levels except organizations, which split as above.

With identity off, `resolve_entity` (tools arms) matches canonical identifiers and names exactly, as given
(stripped of surrounding whitespace): no 11-digit NDC, no normalization, no aliases, no crosswalks, no moiety
links. Its description says so; it is the only tool description that differs between cells.

### Descriptions, exactly

With descriptions off, the generated DDL has no `COMMENT ON` statements and the generated projection has no
label, property or edge descriptions (an empty string), so `describe_schema` returns names and types only in
both stores. Prompts and tool descriptions are the same in every cell: they describe the question
vocabulary and the interface, not the schema. The free-form `describe_schema` tool description is reworded
once so it is true in every cell (it no longer promises "what they mean").

### Isolation and fairness

* Both stores see identical data in every cell: the graph is built from that cell's tables, and the loader
  counts every vertex label and edge source on both sides (H2's check).
* Each cell is its own database in the AGE container (`monograph_h2_1_<cell>`), with its own pair of
  read-only roles that may connect only to it. Comments live in `pg_description`, readable by any role in the
  same database, so a descriptions-off cell must not share a database with a descriptions-on one.
* The response cache tag includes the cell: tools arms send byte-identical requests in the two description
  cells, and without the cell in the key one cell would replay the other's samples.
* Generated artifacts per cell (`generated/h2_1.<cell>.monograph.ddl.sql`, `.monograph.age.json`) come from
  the H6 `SchemaSpec` through two switches (`ablate_spec` for identity, `descriptions=False` in the DDL and
  projection generators) and are drift-checked like every other generated file.

## Data: the H2 frame plus a targeted extension for high fan-out

Measured on the H2 frame before planning: deep paths (T9) and variable-length questions (T10) have hundreds
of candidates each, but answer sets of 50 or more ids (T11) do not: 1 substance is in 50 or more products, 15
substances reach 50 to 150 packages, 7 firms label 50 to 150 products and 15 firms label 50 to 150 packages.
Sixty T11 questions over those few anchors would be the same handful of firms and substances asked again.

So the H2.1 frame extends the cached ingest with targeted fetches, through the existing clients:

1. NDC directory records for the 30 frame substances in the most frame products (ties by UNII):
   `openfda.unii:"<UNII>"`, one page of 100.
2. NDC directory records for the 30 frame labeler codes on the most frame products (ties by code):
   `product_ndc:<code>-*`, one page of 100.
3. RxNav for every new product (NDC properties) and every new RxCUI (properties, generic links), as in H2.

No SPLs are fetched for the extension: a new multi-ingredient product whose SPL is not already cached cannot
be paired and is rejected (H2's rule, counted). The extension's pages are cached under `data/raw/h2_1/`, so
the H2 dataset build is unchanged. Counts go to `results/h2_1/` and the PREREG.

## Questions

Deterministic gold computed by code over the validated instances, phrasings written by hand (3 or 4 per
template), 60 per type: 15 dev and 45 test, stratified by template, fixed seed, split committed.

| Type | Definition | Templates | Scoring |
|---|---|---|---|
| T1 single-hop | control, as in H2 | H2's three | accuracy |
| T3 identity | an entity referred to by a non-canonical identifier or name, from recorded pairs | H2's five (11-digit NDC, RxCUI, moiety, firm alias, brand to generic) | accuracy |
| T4 multi-hop + identity | an H2 T2 shape asked through a T3 reference | H2's five | accuracy |
| T9 deep path | 4 to 6 hops along a fixed relation sequence | four sequences, 4, 5, 5 and 6 hops | accuracy |
| T10 variable-length | reachability within N steps over mixed relation types, or the fewest steps between two entities | four | accuracy |
| T11 high fan-out | an answer set of 50 to 150 ids | four | set F1 (primary), exact match (secondary) |
| T6 unanswerable | as in H2 | H2's five | correct `none` |

**Gold per cell.** A question's text is the same in every cell. Its gold is computed by the same code on each
cell's data, with one rule for identity references: the identity reference in a T3 or T4 question (an
11-digit NDC, an RxCUI, a moiety, a firm alias, a brand) is resolved with the identity layer, since the
recorded pair defines what the question refers to; every other hop follows the cell's own data. For T3 and
T4, the reference always resolves to entities whose ids are the same in both identity levels (packages,
products, substances, recalls). Answers that are firms through a recall's recalling firm therefore differ
between identity levels (recalling-firm organizations with identity off), and that is the correct answer in
that cell's data.

**T9 to T11 use only relations that are the same in every cell**: a package and its product, a package and
its labeler, a product and its active ingredients, a recall and the products it names. So their gold is
identical in all four cells, and they measure path difficulty alone. The base prompt defines a step as one of
these relations, in either direction.

Constraints, checked at generation:

* every anchor exists in every cell; T6 gold is `none` in every cell;
* non-T11 answer sets have 1 to 30 ids; T11 sets 50 to 150;
* each template's tool plan reproduces the gold on the SQL store of every cell with no truncated result and
  no tool result longer than the result cap sent to the model, and every question is re-checked on the Cypher
  store of every cell (0 disagreements);
* no two questions of a template share an anchor, and no two T11 questions share a gold set.

## Agents and budgets

The H2 loop and arms, with the same settings for every arm, model and cell:

| Setting | H2 | H2.1 | Why |
|---|---|---|---|
| data tool calls per question | 12 | 20 | deep paths need more steps (the brief) |
| model turns per question | 16 | 24 | scaled with the tool budget |
| max_tokens per response | 8000 | 16000 | a 150-id answer plus thinking fits in one response |
| output tokens per question | 40000 | 64000 | scaled with the tool budget |
| tool result characters sent to the model | 20000 | 60000 | a 150-row result fits in both interfaces |
| rows per query or tool result | 200 | 200 | unchanged |
| statement timeout | 10 s | 10 s | unchanged |

Base prompt v2 is v1 with the new budget and the step definition; the arm addenda v2 reword describe_schema
as above. One prompt version per arm, the same in every cell; dev iteration is logged per arm, as in H2.

## Pre-registered predictions (in the PREREG before any test run)

* **P1, identity effect.** Turning identity off lowers combined T3+T4 accuracy by at least 0.20 in every arm
  (Haiku, descriptions on): the H2 inference is confirmed. A drop under 0.10 in any arm refutes it. Otherwise
  partly confirmed.
* **P2, description effect.** Turning descriptions off hurts free-form arms more than tool arms: the
  interaction (drop for S-free plus G-free)/2 minus (drop for S-tools plus G-tools)/2 is at least 0.10 across
  all types except T6 (Haiku, identity on; T11 counted by F1). **Identity level fixed by the user on
  2026-11-28**; the identity-off interaction is reported as secondary.
* **P3, store effect on hard paths.** G-free beats S-free by at least 0.10 on combined T9+T10, 95% interval
  excluding 0, in the baseline cell for at least one model. Store effects under tools are identical by
  construction, so only free-form arms judge it. Backend execution latency of each tool on T9 to T11 (SQL vs
  Cypher, p50 and p95) is reported regardless.
* **Ceiling rule.** A type on which every arm scores at least 0.95 in the baseline cell on dev (every arm of
  both models) is marked non-informative in the PREREG before the test run and excluded from P3. Questions do
  not change after dev.
* **Refined H2** ("the graph store helps free-form agents on deep and variable-length paths"): holds if P3
  holds; fails if G-free is within 0.05 of S-free on combined T9+T10 for both models; otherwise partly holds.

Intervals: 2,000 percentile bootstrap resamples, paired over questions for every difference (same questions
in every cell and arm), seed fixed in the PREREG.

## Metrics, per model x cell x arm x type

Accuracy (F1 for T11) with 95% bootstrap intervals; H2's failure categories plus "used a crosswalk that does
not exist" for identity-off cells (a wrong answer whose calls name an identity-layer table, column, edge label
or relation that the cell lacks); tool calls, tokens, USD per correct answer, agent latency p50/p95, backend
execution latency per tool and backend; AGE limitations hit, per kind, as in H2.

## Cost

The test cost is estimated from dev before any test run; above USD 40 the run stops for the user's go-ahead.

## Stages (one commit each, all gates green)

0. CI: the mutation job also runs on pull requests that touch the model, the generators, the check and gate
   code, any frozen experiment file or any PREREG or DEVIATIONS file.
1. Ablation switches: identity (`ablate_spec`, the dataset builder) and descriptions (both generators); the
   four cells' artifacts generated from the H6 source and drift-checked.
2. Cell isolation and fairness tests: both stores hold identical data in every cell; the resolver behaves as
   specified with identity off.
3. The extension fetch, the question templates and gold.
4. Dev runs.
5. Ceiling marks and the PREREG commit, with pins under `experiments/h2_1/frozen/`.
6. Test runs (from a clean tree, after the cost estimate).
7. `monograph report h2_1`.
8. The PR (not merged).

## Findings and decisions while building

* **Stage 1, the switches.** The baseline cell's DDL and projection are byte-identical to H2's generated
  files (a test checks it). Identity off removes 5 tables, 4 columns and 6 edge labels (CLINICAL_DRUG,
  DRUG_MAPPING, HARMONIZED_CLINICAL_DRUG, HAS_ACTIVE_MOIETY, HAS_GENERIC, ALIAS) and one vertex label
  (OrganizationAlias); nothing else in the schema changes. Descriptions off removes all 134 COMMENTs (105 with
  identity off) and every projection description, and nothing else.
* **Labeler organizations are the same in both identity levels.** 39 canonical names cover more than one
  labeler code (Actavis Pharma, Inc. has three); H2 keeps one organization per canonical name. That merge is by
  exact name within a single source, the NDC directory, not a cross-source mapping, so identity off keeps it.
  It also keeps every relation T1 and T9 to T11 use identical across cells.
* **Organizations with identity off, on the H2 frame:** 416 labelers plus 362 recalling firms (778), against
  646 with identity on. All other entity counts and every reject count are identical in both levels. With
  identity on, 630 recalls name a recalling firm that is a labeler organization: 576 resolved through a labeler's
  product and 54 that share the labeler's slug.
* **Stage 2, fairness.** The loader now compares the two stores in full (every vertex with its properties,
  every edge with its properties, as multisets), not only by count; the first run of that comparison caught a
  bug in the comparison itself (decimal edge properties), not in the data. In every cell, on the H2 mini
  fixture: both stores are identical, both backends return identical results on H2's 41-call battery, a
  descriptions-off database has no comment on any object in schema `h2`, and each cell's roles are refused by
  the other cells' databases. With identity off the resolver matches only canonical identifiers and names as
  written (no 11-digit NDC, case, punctuation, suffix or alias match), salt forms, crosswalk events and
  crosswalk recalls come back empty, and `resolve_entity` is the only tool whose definition differs.
* **Stage 3, the extension.** 60 searches (30 substances, 30 labeler codes, one page of 100 each) returned
  4,869 NDC records, 4,361 of them new products; RxNav was fetched for all of them (3,204 RxCUIs). The H2.1
  dataset has 5,176 products (H2: 2,132), 11,335 packages (4,713), 1,116 substances (866), 9,842 clinical drugs
  (8,468); organizations 823 with identity on, 963 with identity off. All four cells loaded with both stores
  identical in full: 175,093 rows, 60,148 vertices and 168,793 edges with identity on; 68,854 rows, 59,912
  vertices and 60,778 edges with identity off (the crosswalk edges are most of the difference).
* **Labels are not a step relation.** The step graph (T9 to T11) uses package-product, package-labeler,
  product-substance and recall-product; label-product was planned but H2's gold index does not model labels,
  and adding it would change H2's frozen gold code. The base prompt states the four relations.
* **Stage 3, the questions.** 420 questions, 60 per type, 15 dev and 45 test each. Every plan reproduced its
  gold on the SQL store of all four cells, and the recheck on all four Cypher stores found 0 disagreements.
  `t11_products_of_labeler` has only 11 firms with 50 to 150 products and a unique name; its shortfall of 4 went
  to the other T11 templates (17, 16, 16), so T11 keeps 60. Gold differs between identity levels only for 24
  T4 questions, all answered by recalling firms (recalling-firm organizations with identity off), as designed;
  every T1, T9, T10, T11 and T6 gold is identical in all cells (asserted at generation). Gold sizes: T9 1 to 30
  (median 5), T10 sets 1 to 27 (median 3), T11 50 to 134 (median 92). The fewest-steps questions answer 5 (11)
  or 7 (4).
* A brand name longer than resolve_entity's 200-character input cannot be asked through the tools; such brands
  are not used (T3, T4).
* **Stage 4, dev, two findings that do not change any question.** (1) Gold for an identity reference follows
  the recorded identity layer, which is incomplete: MOXIFLOXACIN HYDROCHLORIDE has no recorded moiety link, so
  "products containing moxifloxacin, counting salt forms" excludes a product a chemist would count, and an agent
  reconstructing salts from names is scored wrong. Questions do not change after dev; instead the PREREG lists
  the moiety questions where a name-detectable gap exists (2 of 24, 1 on test) and P1 is also reported without
  them, as a pre-registered sensitivity check. (2) An agent that submits a bare code (a recall number) instead of
  the id is scored wrong, as in H2; the base prompt asks for ids as the data gives them.
* **Stage 5, cost and the test set.** Dev spend USD 17.45; the full test set was estimated at USD 52.34 (Haiku
  10.58, Sonnet 41.75), above the USD 40 stop. The user chose, on 2026-11-28 before the PREREG, to keep Haiku's
  full factorial and run Sonnet's test only on T3, T9, T10 and T11 (estimate USD 41.32). Ceiling marks on dev:
  T1, T4 and T6 are non-informative; P3's types (T9, T10) are not, so nothing is excluded from P3.
* **Stage 6, test runs.** All 22 configurations ran from a clean, pinned tree. An agent's Cypher query
  segfaulted the AGE server once; the runs it took down were rerun (DEVIATIONS D1), the crashing run is kept,
  and one question could not be scored for one configuration (a harness TypeError, D1). Test API spend USD
  45.12 against the USD 41.32 estimate; the difference is the crash.
* **Stage 7, the report.** One T9 template's gold counts a product as sharing an ingredient with itself while
  the base prompt says "two products" (4 test questions; DEVIATIONS D2); the report keeps the pre-registered
  numbers and adds a post-hoc P3 line without them. The report also breaks T3+T4 down by the kind of identity a
  question needs: identity encoded in names or codes survives without the layer for free-form agents, identity
  recorded only in a crosswalk does not survive for any arm.
* **After review: a hard spend cap.** The USD 40 stop was a manual check on the dev estimate, so D1's reruns could
  take the test spend to USD 45.12 against the USD 41.32 the user approved. Test runs now read the approved cap
  from `experiments/h2_1/budget.json` and count cumulative test spend from the results (every earlier run and
  rerun, and from now on one `h2_1.spend` line per API call). Each API call reserves its worst-case cost first,
  so the cap cannot be passed; a run that reaches it stops and records why. H2.1's own test spend is already past
  its cap, so any further H2.1 test run is refused (it is refused anyway: the run code changed after its pins).
