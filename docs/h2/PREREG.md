# H2 pre-registration

Committed before the first test run. `monograph h2 run --split test` refuses to start if any file in the
frozen table below no longer matches its sha256. Any change after this commit is a logged deviation, and
both results are reported.

## Hypothesis and design

H2: graph tools win on multi-hop and identity questions. Tested as a 2 x 2 of store (Postgres tables vs
an Apache AGE graph in the same Postgres) and interface (free-form queries vs one shared tool registry):
S-free (text-to-SQL), S-tools (the tools in SQL), G-free (text-to-Cypher), G-tools (the tools in Cypher).

## Data

Loaded once into both stores from the same validated instances: {"AdverseEventReport": 1422, "ClinicalDrug": 8468, "Label": 892, "Organization": 646, "PackagedProduct": 4713, "Product": 2132, "Recall": 1161, "Substance": 866}.
The graph is built from the tables following the generated projection, and every vertex label and edge
source is counted on both sides at load time. The tool backends return identical results on the mini
fixture (41 calls over every tool), and every question's gold was reproduced through the tools on both
stores at generation time.

## Models and settings

`claude-sonnet-5-5` and `claude-haiku-5-5`, the same for every arm:

| Setting | Value |
|---|---|
| max_tokens per response | 8000 |
| effort | medium |
| thinking | adaptive (default; not sent) |
| data tool calls per question | at most 12 (submit_answer does not count) |
| model turns per question | at most 16 |
| output tokens per question | at most 40000 |
| tool result text sent to the model | at most 20000 characters |
| rows per query or tool result | at most 200 |
| statement timeout | 10 s (read-only roles h2_sql and h2_graph, each seeing only its own store) |
| refusal fallbacks | none: a refusal is recorded |

These models reject non-default sampling parameters, so fixed sampling means the fixed effort, thinking
mode and output budget above. Responses are cached content-addressed, with the arm in the key: S-tools
and G-tools send byte-identical prompts and receive identical tool results by design, and the arm tag
keeps their samples independent instead of replaying one arm's responses for the other.

## Frozen prompts

Every arm gets the same base prompt; the arm's addendum names its tools.

| Arm | system prompt sha256 | tools sha256 |
|---|---|---|
| S-free | `7f938391103f84fadebf92ba23ff1cf7cb7d2426971ed94007a58dbc6b479895` | `046cb02032decbc01fff46f2e0ba9b26065adcc6a9a0ad597c07c473b3b85334` |
| S-tools | `a70a451175e92ace2d56ecdb38acced5420769ac25540e626a9fc94410b67637` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` |
| G-free | `0f1dfc3e3ac7198520ef99f445bc534eaa8e13fac2025a3834ec38f8190d286a` | `7992840d7b4b76be849872518fa122d89306dd66cef0f4e939e8fe050f3a998d` |
| G-tools | `a70a451175e92ace2d56ecdb38acced5420769ac25540e626a9fc94410b67637` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` |

Base prompt (v1):

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

Finish by calling submit_answer exactly once:
- kind "ids" with the complete set of ids that answer the question, no more and no fewer;
- kind "number" for a question that asks how many;
- kind "none" when the entity the question names does not exist in the data, or nothing satisfies it;
- evidence: the numbers (1, 2, ...) of the tool calls that produced the answer.
You may make at most 12 tool calls before submit_answer.
```

S-free addendum (v1):

```
Your tools: describe_schema and run_sql (read-only PostgreSQL). Call describe_schema first; it lists every
table and column with what it means.
```

S-tools addendum (v1):

```
Your tools: resolve_entity, get_entity, neighbors, find_path, products_with_substance, events_for,
recalls_for and count. Resolve names and identifiers to ids with resolve_entity before using the others.
```

G-free addendum (v1):

```
Your tools: describe_schema and run_cypher (read-only openCypher on Apache AGE). Call describe_schema first;
it lists every vertex label, edge label and property with what it means.
```

G-tools addendum (v1):

```
Your tools: resolve_entity, get_entity, neighbors, find_path, products_with_substance, events_for,
recalls_for and count. Resolve names and identifiers to ids with resolve_entity before using the others.
```

## Tools

Tools arms (identical names, inputs, outputs and descriptions in S-tools and G-tools):

* `resolve_entity`: Find canonical entities for a name, code or identifier. Matches exact identifiers (UNII, product NDC, package NDC with hyphens, 11-digit NDC without hyphens, RxCUI, recall number, report id, SPL set id) and names after normalization (case, punctuation, whitespace, trademark marks, pharmacopoeia suffixes; for firms also corporate suffixes and recorded aliases). Returns each candidate's id, kind, display name and which identifier or name matched.
  Input schema: `{"$defs": {"EntityKind": {"enum": ["substance", "product", "package", "organization", "clinical_drug", "label", "report", "recall"], "type": "string"}}, "additionalProperties": false, "properties": {"kind": {"anyOf": [{"$ref": "#/$defs/EntityKind"}, {"type": "null"}], "default": null, "description": "Restrict to one kind of entity."}, "text": {"description": "A name, code or identifier as written.", "maxLength": 200, "minLength": 1, "type": "string"}}, "required": ["text"], "type": "object"}`
* `get_entity`: Properties of one or more entities by id.
  Input schema: `{"additionalProperties": false, "properties": {"ids": {"description": "Entity ids (IRIs).", "items": {"type": "string"}, "maxItems": 50, "minItems": 1, "type": "array"}}, "required": ["ids"], "type": "object"}`
* `neighbors`: Nodes one edge away from the given ids, with the relation, direction and edge properties. Relations: INGREDIENT (product to substance), HAS_ACTIVE_MOIETY (salt to moiety), PRODUCT (package to product), LABELER (package to firm), CLINICAL_DRUG and DRUG_MAPPING (product to RxNorm clinical drug), HAS_GENERIC (brand clinical drug to generic), RECALLS_PRODUCT (recall to product), RECALLING_FIRM (recall to firm), LABELS_PRODUCT (label to product), DRUG_INVOLVEMENT (report to drug entry), HARMONIZED_CLINICAL_DRUG and CLINICAL_DRUG (drug entry to clinical drug), REACTION, SECTION, ALIAS (firm to name variant).
  Input schema: `{"additionalProperties": false, "properties": {"direction": {"default": "both", "description": "Edge direction from the start.", "enum": ["out", "in", "both"], "type": "string"}, "ids": {"description": "Start node ids (IRIs).", "items": {"type": "string"}, "maxItems": 50, "minItems": 1, "type": "array"}, "relation": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Only this relation, for example LABELER."}}, "required": ["ids"], "type": "object"}`
* `find_path`: All shortest paths (up to 10) between two nodes, following edges in either direction, up to max_hops edges. length is null when there is none within max_hops.
  Input schema: `{"additionalProperties": false, "properties": {"from_id": {"type": "string"}, "max_hops": {"default": 3, "description": "Longest path to look for, in edges.", "maximum": 4, "minimum": 1, "type": "integer"}, "to_id": {"type": "string"}}, "required": ["from_id", "to_id"], "type": "object"}`
* `products_with_substance`: Products that contain a substance as an active ingredient; with include_moiety_matches, also products whose ingredient is a salt or ester form of it (its active moiety is the substance).
  Input schema: `{"additionalProperties": false, "properties": {"include_moiety_matches": {"default": false, "description": "Also products whose ingredient has this substance as its active moiety.", "type": "boolean"}, "substance_id": {"type": "string"}}, "required": ["substance_id"], "type": "object"}`
* `events_for`: Adverse event reports that involve an entity: a clinical drug (a report drug names it), a product or package (the report involves a clinical drug the product is mapped to), or a substance (the report involves a product containing it). Optional receipt-date range and serious-only filter.
  Input schema: `{"additionalProperties": false, "properties": {"date_from": {"anyOf": [{"format": "date", "type": "string"}, {"type": "null"}], "default": null, "description": "Receipt date on or after (YYYY-MM-DD)."}, "date_to": {"anyOf": [{"format": "date", "type": "string"}, {"type": "null"}], "default": null, "description": "Receipt date on or before (YYYY-MM-DD)."}, "entity_id": {"description": "A substance, product, package or clinical drug id.", "type": "string"}, "serious_only": {"default": false, "type": "boolean"}}, "required": ["entity_id"], "type": "object"}`
* `recalls_for`: Recalls of an entity: a product (recalls naming it), a package (its product's), a substance or clinical drug (recalls of the products containing or mapped to it), or an organization (recalls it issued as recalling firm).
  Input schema: `{"additionalProperties": false, "properties": {"entity_id": {"description": "A product, package, substance, clinical drug or organization id.", "type": "string"}}, "required": ["entity_id"], "type": "object"}`
* `count`: Count reports, recalls or products related to an entity, with the same rules as events_for, recalls_for and products_with_substance (products of a clinical drug: products mapped to it; of an organization: products it labels). Not capped.
  Input schema: `{"additionalProperties": false, "properties": {"classification": {"anyOf": [{"enum": ["I", "II", "III"], "type": "string"}, {"type": "null"}], "default": null, "description": "recalls: this class only."}, "date_from": {"anyOf": [{"format": "date", "type": "string"}, {"type": "null"}], "default": null, "description": "reports: receipt date on or after."}, "date_to": {"anyOf": [{"format": "date", "type": "string"}, {"type": "null"}], "default": null, "description": "reports: receipt date on or before."}, "include_moiety_matches": {"default": false, "description": "products of a substance: count salt forms.", "type": "boolean"}, "related_to": {"description": "The entity id the counted items relate to.", "type": "string"}, "serious_only": {"default": false, "description": "reports: serious reports only.", "type": "boolean"}, "what": {"enum": ["reports", "recalls", "products"], "type": "string"}}, "required": ["what", "related_to"], "type": "object"}`

Free-form arms: `describe_schema` (Describe the data: every table or label with its columns or properties and what they mean.) and `run_sql` (Run one read-only SQL query (PostgreSQL 18, schema h2) and get up to 200 rows. A single SELECT or WITH statement; the statement timeout is 10 s.) or
`run_cypher` (Run one read-only openCypher query (Apache AGE 1.8.0, graph drug) and get up to 200 rows. Write only the Cypher: it is wrapped in cypher() for you and the result columns are read from the final RETURN. The statement timeout is 10 s.). The SQL schema description is the generated DDL with its
column comments; the graph description lists the generated labels, properties and edges with the same
descriptions (a test checks every description appears in both).

All arms: `submit_answer` with `kind` (ids, number, none), `ids`, `number`, `evidence`.

## Questions

300 questions, 50 per type, split {"T1": {"dev": 10, "test": 40}, "T2": {"dev": 10, "test": 40}, "T3": {"dev": 10, "test": 40}, "T4": {"dev": 10, "test": 40}, "T5": {"dev": 10, "test": 40}, "T6": {"dev": 10, "test": 40}}, committed in
`fixtures/h2/questions.json`. Gold is computed by code over the validated instances, never by an LLM.
Phrasings are fixed strings written by hand. Templates:

| Template | Type | Phrasings |
|---|---|---|
| `t1_package_labeler` | T1 | Which firm labels the package with NDC {ndc}? / Who is the labeler of package NDC {ndc}? / Name the firm that labels the package {ndc}. / Which organization is the labeler of the package with NDC {ndc}? |
| `t1_product_ingredients` | T1 | Which substances are the active ingredients of the product with product NDC {product_ndc}? / What are the active ingredients of product NDC {product_ndc}? / List the active ingredient substances of the product {product_ndc}. |
| `t1_recall_products` | T1 | Which products does recall {recall_number} name? / What products are named in recall {recall_number}? / List the products covered by recall {recall_number}. |
| `t2_recall_firms_by_substance` | T2 | Which firms recalled products that contain {name} (UNII {unii})? / Which recalling firms issued recalls of products containing {name} (UNII {unii})? / Name the firms that recalled a product with the active ingredient {name} (UNII {unii}). / Which organizations recalled products that have {name} (UNII {unii}) as an active ingredient? |
| `t2_labelers_by_substance` | T2 | Which firms label products that contain {name} (UNII {unii})? / Who are the labelers of products containing {name} (UNII {unii})? / Name the firms that label a product with the active ingredient {name} (UNII {unii}). |
| `t2_substances_recalled_by_firm` | T2 | Which substances are active ingredients of products recalled by {firm}? / What active ingredients are in the products that {firm} recalled? / List the active ingredient substances of products recalled by {firm}. |
| `t2_recalls_of_labeled_products` | T2 | Which recalls name products labeled by {firm}? / Which recalls cover a product that {firm} labels? / List the recalls that name a product labeled by {firm}. |
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
| `t5_serious_reports_by_substance` | T5 | How many serious adverse event reports received from {from} to {to} involve products that contain {name} (UNII {unii})? / Between {from} and {to}, how many serious adverse event reports involve products containing {name} (UNII {unii})? / Count the serious adverse event reports received from {from} to {to} that involve a product with the active ingredient {name} (UNII {unii}). |
| `t5_recalls_by_class` | T5 | How many class {class} recalls did {firm} issue? / How many recalls of class {class} have {firm} as recalling firm? / Count the class {class} recalls issued by {firm}. |
| `t5_reports_for_product` | T5 | How many adverse event reports involve the product with product NDC {product_ndc}? / How many adverse event reports involve product NDC {product_ndc}? / Count the adverse event reports that involve the product {product_ndc}. |
| `t5_products_labeled` | T5 | How many products does {firm} label? / How many distinct products have a package labeled by {firm}? / Count the products labeled by {firm}. |
| `t6_fake_package` (an NDC of a real product with a package segment that does not exist) | T6 | Which firm labels the package with NDC {ndc}? / Who is the labeler of package NDC {ndc}? / Name the firm that labels the package {ndc}. |
| `t6_fake_unii` (a well-formed UNII that is not in the data) | T6 | Which firms recalled products that contain the substance with UNII {unii}? / Which recalling firms issued recalls of products containing the substance with UNII {unii}? / Name the firms that recalled a product whose active ingredient has UNII {unii}. |
| `t6_substance_without_recalls` (a real substance whose products were never recalled: no path) | T6 | Which firms recalled products that contain {name} (UNII {unii})? / Which recalling firms issued recalls of products containing {name} (UNII {unii})? / Name the firms that recalled a product with the active ingredient {name} (UNII {unii}). |
| `t6_fake_firm` (a plausible firm name that matches no organization or alias) | T6 | Which recalls were issued by {firm}? / Which recalls did {firm} issue as recalling firm? / List the recalls whose recalling firm is {firm}. |
| `t6_fake_rxcui` (an RxCUI that is not in the data) | T6 | Which products are mapped to RxCUI {rxcui}? / Which products correspond to the RxNorm clinical drug with RxCUI {rxcui}? / List the products mapped to RxCUI {rxcui}. |

## Metrics

Per model x arm x question type, on test, each question run once:

* **Primary: accuracy**, exact set match for id answers (ids compared as canonical IRIs; the part after
  the base is accepted), exact value for counts, `none` (or an empty id set) for T6.
* Secondary: set F1.
* Failure categories for wrong answers, first that applies: refusal; budget exhausted (no answer for any
  reason: tool, turn or token budget, truncation, stopping without answering); hallucinated id (an id not
  in the database); wrong entity resolution (no tool input, result preview or result id mentions the
  question's anchor entity by id or key); query error or timeout (a call failed and the answer is wrong);
  missing hop (answer ids of a different kind than the gold, or a number for ids, or the reverse); wrong
  set.
* Cost: tool calls per question, tokens (input, cache read, cache write, output), USD per correct
  answer, p50 and p95 latency (model plus tool time).
* 95% percentile bootstrap intervals (2,000 resamples, seed 20261127) on accuracy and, paired over
  questions, on every arm difference.

## Decision rule

Checked in this order:

1. **H2 fails** if S-free is within 0.05 of G-tools on T2+T3+T4 for both models.
2. **H2 holds** if, for at least one model, G-tools beats S-free by at least 0.10 accuracy on T2+T3+T4
   with the interval excluding 0, and G-tools is no more than 0.05 below S-free on T1.
3. Otherwise **H2 partly holds**.

Reported regardless: the store effect (G-tools minus S-tools, G-free minus S-free) and the interface
effect (S-tools minus S-free, G-tools minus G-free). If a store effect's interval includes 0 while an
interface effect's does not, the report says plainly that the win comes from the tools, not the graph.

## Dev iteration (logged per arm)

Prompts could be tuned on the 60 dev questions only; dev was at the ceiling, so none was. Runs per arm and prompt version (including the rerun after the gold fix):

| Arm | Model | Prompt version | Dev runs | Dev accuracy |
|---|---|---|---|---|
| G-free | claude-haiku-5-5 | base v1, arm v1 | 122 | 0.97 |
| G-free | claude-sonnet-5-5 | base v1, arm v1 | 120 | 1.00 |
| G-tools | claude-haiku-5-5 | base v1, arm v1 | 122 | 1.00 |
| G-tools | claude-sonnet-5-5 | base v1, arm v1 | 120 | 1.00 |
| S-free | claude-haiku-5-5 | base v1, arm v1 | 122 | 0.97 |
| S-free | claude-sonnet-5-5 | base v1, arm v1 | 120 | 1.00 |
| S-tools | claude-haiku-5-5 | base v1, arm v1 | 122 | 1.00 |
| S-tools | claude-sonnet-5-5 | base v1, arm v1 | 120 | 1.00 |

## Known limitations

* Dev questions are only 60 (10 per type), so prompt tuning had little signal; the same budget applied to every arm, and the dev log above shows each arm's iterations.
* The frame is targeted: recalls that name products, their NDC records, and events sampled by RxCUI. It is connected by construction and is not a random sample of FDA data.
* Organization identity is by NDC labeler code (plus a recall's own product): firms that share a parent but not a labeler code stay separate, and a recalling firm that is not a labeler of the recalled product stays its own organization.
* Tool results and query results are capped at 200 rows; questions whose tool plan would hit a cap were not generated, so no question needs more than the cap on any arm.
* Gold for 'a report involves a product' follows openFDA's harmonization of the reported drug to RxCUIs, which is broad (every strength and brand of the reported ingredient); it is the defined relation, not a clinical judgement.
* AGE limitations found on dev are listed in the report; the G-free arm meets them, the G-tools backend works around them.
* Dev accuracy was at or near the ceiling for every arm and both models (0.97 to 1.00 after the gold fix), so no arm's prompt was tuned: all four ran prompt v1 on dev and test. The question set may not separate the arms; that is a property of these templates, not a reason to change them after seeing dev.
* One gold fix on dev: an alias that names a second firm in the data ('Azurity Pharmaceuticals, Inc. (formerly Arbor Pharmaceuticals)', while Arbor Pharmaceuticals is its own organization) made the gold ambiguous; such aliases are excluded for dev and test alike, and doubled periods in question text were removed. 23 questions changed; dev was rerun for every arm.

## Frozen files

| File | sha256 |
|---|---|
| `src/monograph/agent/prompts/base_v1.md` | `e18219d46adfd65be9b42760d9870c9129b9af5f5f5771cb448b3b52d2844bea` |
| `src/monograph/agent/prompts/arm_S-free_v1.md` | `db77173c3120a456f04c8fac65dae6ffaadbce1b457d57d63012ea55718d95b6` |
| `src/monograph/agent/prompts/arm_S-tools_v1.md` | `1a189c1368922ad13085cf7f06586715b47977bcf71886395731886d2eec355e` |
| `src/monograph/agent/prompts/arm_G-free_v1.md` | `a376a5de6db4bdf148d8a3cd1134d7c0e7fe4f5953b5db9f23e8a3c38a5ee235` |
| `src/monograph/agent/prompts/arm_G-tools_v1.md` | `1a189c1368922ad13085cf7f06586715b47977bcf71886395731886d2eec355e` |
| `src/monograph/agent/arms.py` | `102fc84d111cb963d2b434959c1fbfcae360ad3ebc0410b7d1428c75298aa0a4` |
| `src/monograph/agent/loop.py` | `39806558b930645131dcb3e830ffd042d0383cbb4b750d00260ba5e0a7fab4bb` |
| `src/monograph/agent/llm.py` | `07ee36e167fbd051ec797e2966c4da1ec91d7d7bcd45de1d2304a9b46ba89bd6` |
| `src/monograph/tools/registry.py` | `a5e44e9dad4f47ed7e04019b7a5b78ba171a5c5a6d556ee5712c01d6cbc4a9a0` |
| `src/monograph/tools/service.py` | `830e0f1a27b0cfe4cb2355c3e4cd634a2c362d0d6f78f9d2ea5ab12e6c1f3ba2` |
| `src/monograph/tools/sql_backend.py` | `9e3259dda7e13a02fcf01305b42f30934da9aa432df29c2c66a9668669aaaa8d` |
| `src/monograph/tools/cypher_backend.py` | `8d40f89a4a325b51b4ad6c7cec3ed3d730d2006f856dda27ce0f483a97d3ccb3` |
| `src/monograph/tools/freeform.py` | `3ee517b6af194d8dec908826fbca501e571da91f6cb8f05a6aa7403d4bf5ab41` |
| `src/monograph/qa/score.py` | `9da2a341dc0581876814e3e82e6a461cbddea23b317ea9956dbd53993cf92e68` |
| `src/monograph/qa/templates.py` | `9df10495241508c31f7736adcfec95f2c386bf93186e6addf2724368ebe01d5a` |
| `fixtures/h2/questions.json` | `ad61c96ea777ec09d83203a917cac3c430bb287ba1497cf848851bc4a18aa113` |
| `generated/monograph.age.json` | `f4fec6f5900b230901ec05de82633f77ea2b44dc6989998f1185769bbf5128f9` |
| `generated/monograph.ddl.sql` | `f744c93e50b5dfa76f3f37cfafbfd7b63febf2116aa04cc0a6a1d7849f3887d4` |
