# H2.1 pre-registration

Committed before the first test run. `monograph h2_1 run --split test` refuses to start from a dirty tree, if any file in the frozen table below no longer matches its sha256, if code that executes no longer equals its pinned copy, or if a generated cell artifact no longer equals its pin. Every file the agents are sent is read from these pins. Any change after this commit is a logged deviation in `docs/h2_1/DEVIATIONS.md`, and both results are reported.

## Question and design

H2 failed at a ceiling (every arm 0.97 to 1.00 on multi-hop and identity questions). Its report inferred that the identity layer, not the store, did the work. H2.1 measures that inference and gives the graph store a fair chance on deep and variable-length paths. Two ablation factors are crossed with H2's four arms (S-free text-to-SQL, S-tools, G-free text-to-Cypher, G-tools):

| Factor | On | Off |
|---|---|---|
| Identity layer | as in H2 | data as ingested: no derived cross-source mapping in either store (the tables, columns and edge labels do not exist) |
| Schema descriptions | as in H2: DDL column comments, node/edge property descriptions | names and types only, in both stores |

Cells: `id-on_desc-on`, `id-on_desc-off`, `id-off_desc-on`, `id-off_desc-off` (baseline `id-on_desc-on`, hardest `id-off_desc-off`).

Configurations (22): `claude-haiku-5-5` runs the full factorial, 4 cells x 4 arms; `claude-sonnet-5-5` runs S-free, G-free and G-tools in the baseline and the hardest cell. **Deviation from the brief, decided by the user on 2026-11-28 before any run:** the brief's Sonnet set was S-free and G-tools only; G-free was added because P3 and the refined verdict compare G-free with S-free for each model. **Sonnet's test runs cover T3, T9, T10 and T11 only, decided by the user on 2026-11-28** after the dev cost estimate for the full set (USD 52.34) exceeded the USD 40 stop, and before this PREREG. Dev ran every type for every configuration; T1, T4 and T6 were at the ceiling there. P3 and the refined verdict (T9+T10) are covered for both models; P1 and P2 are judged on Haiku, as specified.

### The identity layer

Removed with identity off, from the data and from the generated schema of both stores:

| Class | Field |
|---|---|
| Product | clinical_drug |
| Product | drug_mappings |
| DrugInvolvement | clinical_drug |
| DrugInvolvement | harmonized_clinical_drugs |
| PackagedProduct | ndc11 |
| Substance | active_moiety |
| ClinicalDrug | generic |
| Organization | aliases |

Firms with identity off: a labeler is identified as the NDC directory identifies it, by labeler code (the same organizations and IRIs as with identity on, without aliases); a recalling firm is identified as an enforcement record identifies it, by name (one organization per slug of the name within enforcement records), never merged with a labeler, under `organization/recalling-firm-<slug>`. Relations that the source records carry are kept in every cell: a recall names products, a label covers products, a package belongs to a product and is labeled by a firm, a product has active ingredients.

With identity off, `resolve_entity` matches canonical identifiers and names exactly as written; its description is the only tool definition that differs between cells:

```
Find canonical entities for a name, code or identifier. Matches exact identifiers as written (UNII, product NDC, package NDC with hyphens, RxCUI, recall number, report id, SPL set id, firm slug) and exact names, with no normalization. Returns each candidate's id, kind, display name and which identifier or name matched.
```

### Isolation

Each cell is its own database with its own two read-only roles (refused by the other cells' databases), so no comment is readable from a descriptions-off cell. After loading, both stores of every cell were compared in full (every vertex and edge with its properties against the rows it comes from): identical in every cell. Responses are cached with the cell and arm in the key, so no cell replays another's samples.

## Data

The H2 frame plus a targeted extension for high fan-out questions: NDC directory records for the 30 frame substances in the most frame products and the 30 frame labeler codes on the most frame products (one page of 100 each; 4869 records, 4361 new products), with RxNav for the new products. Multi-ingredient extension products without a cached SPL are rejected by H2's pairing rule.

| Identity | AdverseEventReport | ClinicalDrug | Label | Organization | PackagedProduct | Product | Recall | Substance |
|---|---|---|---|---|---|---|---|---|
| identity on | 1422 | 9842 | 892 | 823 | 11335 | 5176 | 1161 | 1116 |
| identity off | 1422 | 9842 | 892 | 963 | 11335 | 5176 | 1161 | 1116 |

| Cell | rows | vertices | edges | stores identical |
|---|---|---|---|---|
| id-off_desc-off | 68854 | 59912 | 60778 | yes |
| id-off_desc-on | 68854 | 59912 | 60778 | yes |
| id-on_desc-off | 175093 | 60148 | 168793 | yes |
| id-on_desc-on | 175093 | 60148 | 168793 | yes |

## Models and settings

`claude-haiku-5-5` and `claude-sonnet-5-5`; the same settings for every arm, model and cell:

| Setting | Value |
|---|---|
| max_tokens per response | 16000 |
| effort | medium |
| thinking | adaptive (default; not sent) |
| data tool calls per question | at most 20 (submit_answer does not count) |
| model turns per question | at most 24 |
| output tokens per question | at most 64000 |
| tool result text sent to the model | at most 60000 characters |
| rows per query or tool result | at most 200 |
| statement timeout | 10 s (each cell's read-only roles, each seeing only its own store) |
| refusal fallbacks | none: a refusal is recorded |

## Frozen prompts and tools

One base prompt for every arm and cell; the arm's addendum names its tools. Prompt versions: base v2, S-free v2, S-tools v1, G-free v2, G-tools v1.

| Arm | system prompt sha256 | tools sha256, identity on | tools sha256, identity off |
|---|---|---|---|
| S-free | `be07ac58aadc1c55c580097e3e552ce6aff89acb412079cecff48b906c4c42e1` | `8c38f4090ed96eb3858edd7a3217bffbc143635c2185d133b4b7e2b40fdc34d6` | `8c38f4090ed96eb3858edd7a3217bffbc143635c2185d133b4b7e2b40fdc34d6` |
| S-tools | `2c84aebe9a529eb70ff96289f4e24998c4707cddca968605d4ec9f299c35a8e3` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` | `b953de048cea4d73f4b5bedfe2963f26afb3e82c7118b2b2759e996d830a64c6` |
| G-free | `af4de339d328d793641536f088ce45734ca2d361d8fef431de1f13cd60df32a7` | `221064b2121e3cafa12c71a9be2bc8fe930c095431dde753ce4b0865de7f29e9` | `221064b2121e3cafa12c71a9be2bc8fe930c095431dde753ce4b0865de7f29e9` |
| G-tools | `2c84aebe9a529eb70ff96289f4e24998c4707cddca968605d4ec9f299c35a8e3` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` | `b953de048cea4d73f4b5bedfe2963f26afb3e82c7118b2b2759e996d830a64c6` |

Base prompt (v2):

```
You answer questions about FDA drug data: products (identified by product NDC), packages (package NDC),
substances (UNII), firms, RxNorm clinical drugs (RxCUI), labels, adverse event reports and recalls. Look
everything up with your tools; never answer from memory.

Every entity has a canonical id, its IRI, for example https://example.org/monograph/product/0069-3150.
Answer with ids exactly as the data gives them.

How the questions use these words:
- A product contains a substance when one of its active ingredients is that substance. "Counting salt
  forms" means also when an ingredient is a salt, ester or other form whose active moiety is the substance.
- A product is mapped to a clinical drug through its drug mappings (any source) or its clinical drug.
- A brand clinical drug's generic equivalent is its generic clinical drug; the generic equivalents of a
  brand are the products mapped to that generic clinical drug.
- An adverse event report involves a clinical drug when one of its drug entries names it (as clinical drug
  or harmonized clinical drug); it involves a product when it involves a clinical drug the product is
  mapped to; it involves a substance when it involves a product that contains the substance. A package
  stands for its product.
- The recalls of a product are the recalls that name it; of a substance, the recalls of products that
  contain it. A firm recalled a product when it is the recalling firm of a recall that names the product.
- The labelers of a product are the firms that label one of its packages.
- A firm may appear under other names (aliases); a package NDC may be written as 11 digits without hyphens.
- A step goes from an entity to a directly related one, in either direction, along these relations only: a
  package and its product, a package and its labeler, a product and one of its active ingredient substances,
  a recall and a product it names. "Within N steps" ("at most N steps away") allows any mix of these
  relations; the starting entity does not count as reached. The fewest steps between two entities is the
  length of the shortest such connection.
- Two products share an active ingredient when some substance is an active ingredient of both.

Finish by calling submit_answer exactly once:
- kind "ids" with the complete set of ids that answer the question, no more and no fewer;
- kind "number" for a question that asks how many;
- kind "none" when the entity the question names does not exist in the data, or nothing satisfies it;
- evidence: the numbers (1, 2, ...) of the tool calls that produced the answer.
You may make at most 20 tool calls before submit_answer.
```

S-free addendum (v2):

```
Your tools: describe_schema and run_sql (read-only PostgreSQL). Call describe_schema first; it lists every
table and its columns.
```

S-tools addendum (v1):

```
Your tools: resolve_entity, get_entity, neighbors, find_path, products_with_substance, events_for,
recalls_for and count. Resolve names and identifiers to ids with resolve_entity before using the others.
```

G-free addendum (v2):

```
Your tools: describe_schema and run_cypher (read-only openCypher on Apache AGE). Call describe_schema first;
it lists every vertex label, edge label and property.
```

G-tools addendum (v1):

```
Your tools: resolve_entity, get_entity, neighbors, find_path, products_with_substance, events_for,
recalls_for and count. Resolve names and identifiers to ids with resolve_entity before using the others.
```

Tools arms: H2's registry, identical names, inputs, outputs and descriptions in S-tools and G-tools: `resolve_entity`, `get_entity`, `neighbors`, `find_path`, `products_with_substance`, `events_for`, `recalls_for`, `count`. Free-form arms: `describe_schema` (Describe the data: every table, or every vertex and edge label, with its columns or properties.) and `run_sql` or `run_cypher`, as in H2. Each cell's schema text is pinned per store.

## Questions

420 questions, split {"T1": {"dev": 15, "test": 45}, "T3": {"dev": 15, "test": 45}, "T4": {"dev": 15, "test": 45}, "T9": {"dev": 15, "test": 45}, "T10": {"dev": 15, "test": 45}, "T11": {"dev": 15, "test": 45}, "T6": {"dev": 15, "test": 45}}, committed in `experiments/h2_1/frozen/questions.json`. Gold is computed by code over the validated instances of each identity level, never by an LLM; phrasings are fixed strings written by hand. The identity reference of a T3 or T4 question is resolved with the identity layer (the recorded pair defines what the question refers to); every other hop follows the cell's own data. T1, T9, T10, T11 and T6 use only relations that are the same in every cell, and their gold is the same in every cell (checked at generation). A step (T9, T10) is one of: PRODUCT, LABELER, INGREDIENT, RECALLS_PRODUCT, in either direction. Answer sets have 1 to 30 ids, T11 50 to 150. Every question's tool plan reproduced its gold on the SQL store of every cell with no truncated result and no result over the character cap, and was re-checked on every cell's Cypher store (0 disagreements).

| Template | Type | Phrasings |
|---|---|---|
| `t1_package_labeler` | T1 | Which firm labels the package with NDC {ndc}? / Who is the labeler of package NDC {ndc}? / Name the firm that labels the package {ndc}. / Which organization is the labeler of the package with NDC {ndc}? |
| `t1_product_ingredients` | T1 | Which substances are the active ingredients of the product with product NDC {product_ndc}? / What are the active ingredients of product NDC {product_ndc}? / List the active ingredient substances of the product {product_ndc}. |
| `t1_recall_products` | T1 | Which products does recall {recall_number} name? / What products are named in recall {recall_number}? / List the products covered by recall {recall_number}. |
| `t3_ndc11_labeler` | T3 | Which firm labels the package with 11-digit NDC {ndc11}? / Who is the labeler of the package whose NDC in 11-digit form is {ndc11}? / Name the labeler of package {ndc11} (11-digit NDC). |
| `t3_rxcui_products` | T3 | Which products are mapped to RxCUI {rxcui}? / Which products correspond to the RxNorm clinical drug with RxCUI {rxcui}? / List the products mapped to RxCUI {rxcui}. |
| `t3_moiety_products` | T3 | Which products contain {name}, counting salt forms? / Which products contain {name} as an active ingredient, including its salt and ester forms? / List the products whose active ingredients include {name}, counting salt forms. |
| `t3_alias_recalls` | T3 | Which recalls were issued by {firm}? / Which recalls did {firm} issue as recalling firm? / List the recalls whose recalling firm is {firm}. |
| `t3_brand_generics` | T3 | Which products are generic equivalents of the brand drug {brand}? / List the products that are generic equivalents of {brand}. / Which products are mapped to the generic clinical drug of the brand {brand}? |
| `t4_recall_firms_by_moiety` | T4 | Which firms recalled products that contain {name}, counting salt forms? / Which recalling firms issued recalls of products containing {name}, including its salt and ester forms? / Name the firms that recalled a product containing {name}, counting salt forms. |
| `t4_substances_recalled_by_alias` | T4 | Which substances are active ingredients of products recalled by {firm}? / What active ingredients are in the products that {firm} recalled? / List the active ingredient substances of products recalled by {firm}. |
| `t4_labelers_by_rxcui` | T4 | Which firms label packages of products mapped to RxCUI {rxcui}? / Who are the labelers of products mapped to RxCUI {rxcui}? / Name the firms that label a product mapped to the RxNorm clinical drug {rxcui}. |
| `t4_recall_firms_by_ndc11` | T4 | Which firms recalled the product packaged under 11-digit NDC {ndc11}? / Which recalling firms issued recalls of the product whose package has the 11-digit NDC {ndc11}? / Name the firms that recalled the product of package {ndc11} (11-digit NDC). |
| `t4_recalls_of_generics` | T4 | Which recalls name generic equivalents of the brand drug {brand}? / Which recalls cover a product that is a generic equivalent of {brand}? / List the recalls of products that are generic equivalents of the brand {brand}. |
| `t9_recalls_sharing_an_ingredient` (4 hops: recall, product, substance, product, recall) | T9 | Which recalls other than {recall_number} name a product that shares an active ingredient with a product named in recall {recall_number}? / Apart from {recall_number} itself, which recalls name a product containing one of the active ingredients of the products in recall {recall_number}? / List the recalls, other than {recall_number}, that name a product sharing an active ingredient with a product of recall {recall_number}. |
| `t9_recalls_of_colabeled_products` (5 hops: product, package, labeler, package, product, recall) | T9 | Which recalls name a product labeled by a firm that labels a package of product NDC {product_ndc}? / Take the firms that label packages of the product {product_ndc}: which recalls name any product those firms label? / List the recalls that name a product labeled by one of the labelers of product NDC {product_ndc}. |
| `t9_labelers_of_ingredient_sharers` (5 hops: product, substance, product, package, labeler) | T9 | Which firms label a product that shares an active ingredient with the product {product_ndc}? / Who labels products containing any of the active ingredients of product NDC {product_ndc}? / List the labelers of products that share an active ingredient with product NDC {product_ndc}. |
| `t9_substances_of_colabeled_recall_products` (6 hops: recall, product, package, labeler, package, product, substance) | T9 | Which substances are active ingredients of products labeled by the firms that label a product named in recall {recall_number}? / Take the labelers of the products in recall {recall_number}: which active ingredients are in the products those firms label? / List the active ingredient substances of every product labeled by a labeler of a product named in recall {recall_number}. |
| `t10_firms_within_six_steps` | T10 | Which other firms can be reached from {firm} in at most 6 steps? / Which firms, other than {firm} itself, are within 6 steps of {firm}? / List the firms other than {firm} that are at most 6 steps away from it. |
| `t10_substances_within_four_steps` | T10 | Which substances are within 4 steps of recall {recall_number}? / Which substances can be reached from recall {recall_number} in at most 4 steps? / List the substances at most 4 steps away from recall {recall_number}. |
| `t10_recalls_within_four_steps` | T10 | Which recalls are within 4 steps of the product with product NDC {product_ndc}? / Which recalls can be reached from product NDC {product_ndc} in at most 4 steps? / List the recalls at most 4 steps away from the product {product_ndc}. |
| `t10_fewest_steps_substance_to_firm` | T10 | What is the fewest number of steps between {name} (UNII {unii}) and the firm {firm}? / How many steps does the shortest connection from {name} (UNII {unii}) to {firm} take? / In how few steps can {name} (UNII {unii}) be connected to the firm {firm}? |
| `t11_products_with_substance` | T11 | Which products contain {name} (UNII {unii})? / List every product that has {name} (UNII {unii}) as an active ingredient. / Which products have the active ingredient {name} (UNII {unii})? |
| `t11_packages_of_substance` | T11 | Which packages belong to products that contain {name} (UNII {unii})? / List every package NDC of the products containing {name} (UNII {unii}). / Which packages are packages of a product with the active ingredient {name} (UNII {unii})? |
| `t11_products_of_labeler` | T11 | Which products does {firm} label? / List every product that has a package labeled by {firm}. / Which products are labeled by {firm}? |
| `t11_packages_of_labeler` | T11 | Which packages does {firm} label? / List every package whose labeler is {firm}. / Which package NDCs are labeled by {firm}? |
| `t6_fake_package` (an NDC of a real product with a package segment that does not exist) | T6 | Which firm labels the package with NDC {ndc}? / Who is the labeler of package NDC {ndc}? / Name the firm that labels the package {ndc}. |
| `t6_fake_unii` (a well-formed UNII that is not in the data) | T6 | Which firms recalled products that contain the substance with UNII {unii}? / Which recalling firms issued recalls of products containing the substance with UNII {unii}? / Name the firms that recalled a product whose active ingredient has UNII {unii}. |
| `t6_substance_without_recalls` (a real substance whose products were never recalled, in either identity level) | T6 | Which firms recalled products that contain {name} (UNII {unii})? / Which recalling firms issued recalls of products containing {name} (UNII {unii})? / Name the firms that recalled a product with the active ingredient {name} (UNII {unii}). |
| `t6_fake_firm` (a plausible firm name that matches no organization or alias in either identity level) | T6 | Which recalls were issued by {firm}? / Which recalls did {firm} issue as recalling firm? / List the recalls whose recalling firm is {firm}. |
| `t6_fake_rxcui` (an RxCUI that is not in the data) | T6 | Which products are mapped to RxCUI {rxcui}? / Which products correspond to the RxNorm clinical drug with RxCUI {rxcui}? / List the products mapped to RxCUI {rxcui}. |

## Metrics

Per model x cell x arm x type, on test, each question run once:

* **Primary:** accuracy (exact set match for ids, ids compared as canonical IRIs with the part after the base accepted; exact value for counts; `none` or an empty set for T6); for T11, set F1, with exact match secondary.
* Failure categories for wrong answers, first that applies: refusal; budget exhausted; used a crosswalk that does not exist (identity off only: a call names an identity-layer table, column, edge label or option the cell lacks); hallucinated id; wrong entity resolution; query error or timeout; missing hop; wrong set.
* Tool calls, tokens (input, cache read, cache write, output), USD per correct answer, agent latency p50/p95, backend execution latency per tool and backend (SQL vs Cypher, p50/p95) on T9 to T11, and AGE limitations hit per kind (H2's classifier).
* 95% percentile bootstrap intervals (2000 resamples, seed 20261128), paired over questions for every difference.

## Predictions and decision rules

* **P1, identity effect** (claude-haiku-5-5, descriptions on): combined T3+T4 accuracy, identity on minus identity off, per arm. **Confirmed** if the drop is at least 0.20 in every arm; **refuted** if it is under 0.10 in any arm (checked second); otherwise partly confirmed.
* **P2, description effect** (claude-haiku-5-5, identity on): over every type but T6, per question, (drop for S-free plus drop for G-free)/2 minus (drop for S-tools plus drop for G-tools)/2, where a drop is the score with descriptions on minus off. **Holds** if the mean is at least 0.10. The identity-off interaction is reported as secondary.
* **P3, store effect on hard paths** (baseline cell): G-free minus S-free on combined T9+T10, excluding non-informative types. **Holds** if, for at least one model, it is at least 0.10 with the 95% interval excluding 0. Tool-arm store effects are identical by construction and not judged.
* **P1 sensitivity check** (reported, does not change the verdict): P1 recomputed without the moiety questions whose gold the recorded moiety links may understate (a substance in some product whose name starts with the moiety's name has no recorded link to it). Sensitivity set: `t3_moiety_products-06`, `t4_recall_firms_by_moiety-09`.
* **Refined H2** (the graph store helps free-form agents on deep and variable-length paths): **holds** if P3 holds; **fails** if G-free is within 0.05 of S-free on combined T9+T10 for both models; otherwise **partly holds**.
* **Ceiling rule:** a type on which every arm of both models scores at least 0.95 in the baseline cell on dev is non-informative and excluded from P3.

### Ceiling marks (dev, baseline cell)

| Type | claude-haiku-5-5 S-free | claude-haiku-5-5 S-tools | claude-haiku-5-5 G-free | claude-haiku-5-5 G-tools | claude-sonnet-5-5 S-free | claude-sonnet-5-5 G-free | claude-sonnet-5-5 G-tools | non-informative |
|---|---|---|---|---|---|---|---|---|
| T1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | yes |
| T3 | 0.80 | 1.00 | 0.93 | 1.00 | 0.93 | 0.93 | 1.00 | no |
| T4 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | yes |
| T9 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.93 | 1.00 | no |
| T10 | 0.93 | 0.73 | 0.67 | 0.80 | 1.00 | 1.00 | 0.93 | no |
| T11 | 0.93 | 1.00 | 1.00 | 1.00 | 0.93 | 1.00 | 0.87 | no |
| T6 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | yes |

Non-informative types: T1, T4, T6. Excluded from P3: none.

## Dev iteration (logged per arm)

Prompts could be tuned on the dev questions only. Runs per configuration and prompt version:

| Model | Cell | Arm | Prompt versions | Dev runs | Dev score |
|---|---|---|---|---|---|
| claude-haiku-5-5 | id-on_desc-on | S-free | S-free v2, base v2 | 108 | 0.95 |
| claude-haiku-5-5 | id-on_desc-on | S-tools | S-tools v1, base v2 | 105 | 0.96 |
| claude-haiku-5-5 | id-on_desc-on | G-free | G-free v2, base v2 | 105 | 0.94 |
| claude-haiku-5-5 | id-on_desc-on | G-tools | G-tools v1, base v2 | 105 | 0.97 |
| claude-haiku-5-5 | id-on_desc-off | S-free | S-free v2, base v2 | 105 | 0.95 |
| claude-haiku-5-5 | id-on_desc-off | S-tools | S-tools v1, base v2 | 105 | 0.98 |
| claude-haiku-5-5 | id-on_desc-off | G-free | G-free v2, base v2 | 105 | 0.94 |
| claude-haiku-5-5 | id-on_desc-off | G-tools | G-tools v1, base v2 | 105 | 0.96 |
| claude-haiku-5-5 | id-off_desc-on | S-free | S-free v2, base v2 | 105 | 0.82 |
| claude-haiku-5-5 | id-off_desc-on | S-tools | S-tools v1, base v2 | 105 | 0.70 |
| claude-haiku-5-5 | id-off_desc-on | G-free | G-free v2, base v2 | 105 | 0.80 |
| claude-haiku-5-5 | id-off_desc-on | G-tools | G-tools v1, base v2 | 105 | 0.72 |
| claude-haiku-5-5 | id-off_desc-off | S-free | S-free v2, base v2 | 105 | 0.82 |
| claude-haiku-5-5 | id-off_desc-off | S-tools | S-tools v1, base v2 | 105 | 0.73 |
| claude-haiku-5-5 | id-off_desc-off | G-free | G-free v2, base v2 | 105 | 0.79 |
| claude-haiku-5-5 | id-off_desc-off | G-tools | G-tools v1, base v2 | 105 | 0.71 |
| claude-sonnet-5-5 | id-on_desc-on | S-free | S-free v2, base v2 | 105 | 0.98 |
| claude-sonnet-5-5 | id-on_desc-on | G-free | G-free v2, base v2 | 105 | 0.98 |
| claude-sonnet-5-5 | id-on_desc-on | G-tools | G-tools v1, base v2 | 105 | 0.97 |
| claude-sonnet-5-5 | id-off_desc-off | S-free | S-free v2, base v2 | 105 | 0.88 |
| claude-sonnet-5-5 | id-off_desc-off | G-free | G-free v2, base v2 | 105 | 0.88 |
| claude-sonnet-5-5 | id-off_desc-off | G-tools | G-tools v1, base v2 | 105 | 0.79 |

## Cost

Dev spend recorded: USD 17.45. Test estimate from dev (USD per dev question, per configuration and type, times the test questions it covers): USD 41.32. The user approved this set on 2026-11-28 at an estimate of USD 41.32, against the USD 40 stop.

## Known limitations

* The frame is targeted (H2's recall-centred frame plus a fan-out extension) and connected by construction; it is not a random sample of FDA data.
* Identity off is defined operationally (the PREREG's table): the H6 ingest keys without the derived cross-source mappings. Labelers sharing a canonical name across labeler codes stay one organization in both identity levels (an exact-name merge within the NDC directory).
* T3 and T4 gold with identity off is what the identity layer says the question refers to; an agent can only reach it by reconstructing identity from names and codes, which is what P1 measures.
* Prompts and tool definitions are the same in every cell (except resolve_entity's matching sentence), so the base prompt's vocabulary (aliases, 11-digit NDCs, drug mappings) is present even where the data has no such links.
* With descriptions off the SQL store still exposes CHECK constraints (enumerated values, patterns) through the catalog, as in H2; the graph store exposes values only through the data.
* The ceiling rule is applied once, on dev, before the test run; questions were not changed after dev.
* Each test question runs once per configuration (one sample).
* Gold for identity references follows the recorded identity layer, which is incomplete: a salt with no recorded moiety link, a product with no recorded crosswalk or a firm variant with no recorded alias is not in the gold even when a careful reading of names would include it. The name-detectable case for moieties is listed and checked as a sensitivity analysis.
* Answers must be ids as the data gives them (IRIs, or the part after the base); a bare code such as a recall number is scored wrong, as in H2.
* The fewest-steps questions (T10) answer 5 or 7 steps (11 and 4 questions).

## Frozen files

| File | sha256 |
|---|---|
| `experiments/h2_1/frozen/arm_G-free_v2.md` | `4d6018fe893ef8755b0cf39d52fdf167000525881ac8cfeba7b7e3e64b5c7bdc` |
| `experiments/h2_1/frozen/arm_G-tools_v1.md` | `1a189c1368922ad13085cf7f06586715b47977bcf71886395731886d2eec355e` |
| `experiments/h2_1/frozen/arm_S-free_v2.md` | `273b005180279b4a3b5a531035ffcac1b336810cb10b037da26c7ce8068ada70` |
| `experiments/h2_1/frozen/arm_S-tools_v1.md` | `1a189c1368922ad13085cf7f06586715b47977bcf71886395731886d2eec355e` |
| `experiments/h2_1/frozen/base_v2.md` | `1624a36e5c1d9b874dbdcea8b958053aac24f8627f48502ae4502146db516d29` |
| `experiments/h2_1/frozen/h2_1.id-off_desc-off.monograph.age.json` | `c45bc6a4977bb3ebad0ae3873d7c3c8009a07786cd199bc7a7a57f59eb7c5814` |
| `experiments/h2_1/frozen/h2_1.id-off_desc-off.monograph.ddl.sql` | `2298241702702ed0929c8b3a58103fa987f21b2c0f84266fe1ffa834335cd97d` |
| `experiments/h2_1/frozen/h2_1.id-off_desc-on.monograph.age.json` | `6ba54eb64957ff38b2c7ee61d5299088408dcac5e826a24411825cfb352475e1` |
| `experiments/h2_1/frozen/h2_1.id-off_desc-on.monograph.ddl.sql` | `145aa892b4591b7783841482306af9241b5cb3ebbe706759dae8db50fc35383c` |
| `experiments/h2_1/frozen/h2_1.id-on_desc-off.monograph.age.json` | `517ffa8b2878161d63205298279606fa7705c64f2a4abaa849c22ad60dd27a9c` |
| `experiments/h2_1/frozen/h2_1.id-on_desc-off.monograph.ddl.sql` | `1baffc953af7e9bc11b03500a7ec41dd6bdec6341c7da73d96b4d753299d9ddc` |
| `experiments/h2_1/frozen/h2_1.id-on_desc-on.monograph.age.json` | `f4fec6f5900b230901ec05de82633f77ea2b44dc6989998f1185769bbf5128f9` |
| `experiments/h2_1/frozen/h2_1.id-on_desc-on.monograph.ddl.sql` | `f744c93e50b5dfa76f3f37cfafbfd7b63febf2116aa04cc0a6a1d7849f3887d4` |
| `experiments/h2_1/frozen/questions.json` | `444acc864ea9336caa99729923f1266afdc86ae10f005bb8307ce38f6ed710b9` |
| `experiments/h2_1/frozen/schema.id-off_desc-off.graph.txt` | `45a39f766951fcd167940f06939a8253c2ee8d5a42ddc6e6d503928730350b97` |
| `experiments/h2_1/frozen/schema.id-off_desc-off.sql.txt` | `1704b9e4e3a6ec1d17768ccd5f14f397813681b4ccd9d4139f1202bd9233bee2` |
| `experiments/h2_1/frozen/schema.id-off_desc-on.graph.txt` | `007bc1b5cca3e1a620f51967ac039ef69648fe99382e4b5717ffa28bc890ce89` |
| `experiments/h2_1/frozen/schema.id-off_desc-on.sql.txt` | `389e71b1b133e2cee57bdfd4852d45b3678705fe4625d1e04497930e5eef2cd8` |
| `experiments/h2_1/frozen/schema.id-on_desc-off.graph.txt` | `ba8b1ae2af3f0a702572cf29d9659f88ffee8acc82843ff357a44a10f816b654` |
| `experiments/h2_1/frozen/schema.id-on_desc-off.sql.txt` | `29d6d3b5e9a2f88dab4810054ce901b1a8540df0685d59f2924d9d26749bef6d` |
| `experiments/h2_1/frozen/schema.id-on_desc-on.graph.txt` | `a01bd46f38ac0b9dc30ce8e648db4b7ad9b730ad1f3005a1f46391ca42fcf52d` |
| `experiments/h2_1/frozen/schema.id-on_desc-on.sql.txt` | `48848b239ec0faa81ef967c327893e27297279636e906a315bb02f1f14706574` |
| `experiments/h2_1/frozen/tools.G-free.id-off.json` | `221064b2121e3cafa12c71a9be2bc8fe930c095431dde753ce4b0865de7f29e9` |
| `experiments/h2_1/frozen/tools.G-free.id-on.json` | `221064b2121e3cafa12c71a9be2bc8fe930c095431dde753ce4b0865de7f29e9` |
| `experiments/h2_1/frozen/tools.G-tools.id-off.json` | `b953de048cea4d73f4b5bedfe2963f26afb3e82c7118b2b2759e996d830a64c6` |
| `experiments/h2_1/frozen/tools.G-tools.id-on.json` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` |
| `experiments/h2_1/frozen/tools.S-free.id-off.json` | `8c38f4090ed96eb3858edd7a3217bffbc143635c2185d133b4b7e2b40fdc34d6` |
| `experiments/h2_1/frozen/tools.S-free.id-on.json` | `8c38f4090ed96eb3858edd7a3217bffbc143635c2185d133b4b7e2b40fdc34d6` |
| `experiments/h2_1/frozen/tools.S-tools.id-off.json` | `b953de048cea4d73f4b5bedfe2963f26afb3e82c7118b2b2759e996d830a64c6` |
| `experiments/h2_1/frozen/tools.S-tools.id-on.json` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` |

Code that executes in a run is pinned under `experiments/h2_1/frozen/code/` and must equal the live files byte for byte: analysis.py, cells.py, cli.py, load.py, pins.py, questions.py, run.py, arms.py, llm.py, loop.py, run.py, score.py, cypher_backend.py, freeform.py, registry.py, service.py, sql_backend.py.
