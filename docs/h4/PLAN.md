# H4 plan: LLM extraction is usable only with SHACL repair

## Hypothesis, as reframed

H6 showed that Pydantic and SHACL agree on every single record, because both are generated from one
source. Repairing one record with Pydantic feedback or with record-local SHACL feedback would therefore
be the same experiment twice. H4 uses SHACL where it adds something Pydantic cannot: validating an
extraction **merged into the existing reference graph**, so that references must resolve to real nodes
of the right class and cross-entity facts must line up.

The question: is record-local schema repair (A1, A2) enough, or does graph-context repair (A3) change
whether extraction is usable?

## Task

Extract structured product facts from the narrative text of FDA drug labels (SPL documents from
DailyMed) and score them against the structured product data inside the same SPL document.

* **Input** (`extract/sources/dailymed.py`): the document title and the `<text>` of these narrative
  sections, by LOINC code: Description 34089-3, Dosage Forms and Strengths 43678-2, How Supplied
  34069-5, Storage and Handling 44425-7, OTC Active Ingredient 55106-9, Package Label Principal
  Display Panel 51945-4. Nothing else from the XML: no `<subject>`/`<manufacturedProduct>`, no codes,
  no indexing sections. A leak test builds an SPL whose structured part carries sentinel values that
  appear nowhere in its narrative, and proves none of them reaches a prompt.
* **Gold** (same module): every `manufacturedProduct` in the document: product NDC, dosage form
  (`formCode`), routes (`routeCode`), active ingredients (UNII, name, strength numerator value and
  unit, denominator value and unit), and every package NDC (nested `containerPackagedProduct`).
* **Targets**, typed by new extraction models in `core/models.py` (so they go through the H6
  pipeline, drift check and breaking-change gate): per product, the dosage form, routes, active
  ingredients (substance name, strength value and unit, basis value and unit, for example "per 5 mL"),
  and the package NDCs mentioned for it.

### Endpoint check (done before planning)

`https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{SETID}.xml` is listed on the DailyMed web
services page ("Returns an SPL document for specific SET ID") and returns HL7 v3 SPL XML. NDC to set
ID uses the documented `/spls.json?ndc=...` filter. Raw XML is cached under `data/raw/spl/`.

## Vocabulary the brief needs and the repo did not have

| Need | Source | Model change |
|---|---|---|
| "dosage form is an enum individual" | FDA SPL dosage forms (158 terms with NCI codes, current as of 02/03/2022) | new `DosageForm` enum |
| "strength unit is in the allowed UCUM set" | FDA SPL units of measure (52 UCUM codes, current as of 10/24/2024) | new `UcumUnit` enum |
| route | the existing H6 `Route` enum (FDA SPL routes) | none |

Both FDA pages refuse non-browser clients, so they were read with a fetch tool; URLs and dates go in
the enum docstrings. The extraction classes and enums are additive to the ontology, so the gate will
demand a minor bump (2.1.0) and a snapshot in the same change.

## Reference graph and resolution

The reference graph (`extract/resolve.py`, an in-memory rdflib graph of H6 instances):

* Substances: the union of three public sources, pooled over all labels and never built per label:
  DailyMed `/uniis` (7,997 active moieties), the H6 openFDA substances, and every ingredient (active
  and inactive) declared in the structured part of the 200 sampled SPLs. Salts (for example histamine
  dihydrochloride) are not active moieties, so the moiety list alone would not cover the gold.
* Products, PackagedProducts, ClinicalDrugs: from the openFDA NDC directory (the H6 data plus the
  records for the sampled labels' NDCs, fetched through the H6 openFDA client). The NDC directory is a
  registry that exists independently of the label's narrative, which is what a production pipeline
  would validate against.

Resolution is deterministic code, never the LLM: substance name to UNII by normalized exact match on
names in the graph (case, whitespace, punctuation and a fixed list of salt-form spellings), then
nothing. NDC to PackagedProduct by exact match after the deterministic hyphen normalization.
Unresolved names are a metric of their own.

## Arms

| Arm | After the first extraction |
|---|---|
| A0 | nothing |
| A1 | Pydantic validation; on failure send the errors back and retry |
| A2 | A1, then deterministic repair (UCUM unit spelling, whitespace, NDC hyphenation); no LLM call |
| A3 | A2, then SHACL validation of the extraction merged into the reference graph; on violations send the report (focus node, path, message, offending value, allowed candidates when the shape knows them) and retry |

Each repair loop is capped at 3 rounds and every round is logged. All arms share the first-round
request, so the response cache makes the first round identical across arms by construction.

### Graph-context shapes for A3

Generated into `generated/monograph.context.shapes.ttl` from the same source, by the SHACL generator
in a context mode: references must exist in the data graph (no stubs), and resolution links are
required.

| Check | Projection |
|---|---|
| every extracted ingredient resolves to a Substance node | SHACL Core: `resolvedSubstance` minCount 1, `sh:class Substance` |
| every package NDC exists as a PackagedProduct | SHACL Core: `resolvedPackage` minCount 1, `sh:class PackagedProduct` |
| ... whose Product is the label's product | `extraction_only`: membership of one path's values in the set reached by another path needs SPARQL, not SHACL Core |
| dosage form and route are enum individuals | SHACL Core: `sh:in` |
| strength unit is in the UCUM set | SHACL Core: `sh:in` |
| ingredient count matches the referenced Product where the graph knows it | `extraction_only`: equality of two paths' cardinalities has no SHACL Core construct |

`extraction_only` checks are declared on the model like `pydantic_only` in H6, run as deterministic
code inside A3, and appear in the leak inventory.

### A threat to validity, removed from A3 and kept as an upper bound (A3+)

Feedback that lists "allowed values" taken from the label's own registry records (its products' package
NDCs, its ingredient count) can contain gold: an A3 gain on such a field would be a registry lookup, not
extraction. So A3 offers candidates only from sources independent of the label (other labels' records,
DailyMed's moiety list, same-labeler NDCs not linked to this label), and its messages state no fact taken
from the label. A leak test plants sentinels in the label's own lists. A3+ keeps the label-derived
candidates and is reported as an upper bound outside the verdict. Every feedback message is still checked
for gold values, and the report counts graph-context fixes whose gold appeared in feedback.

## Models and API settings

* `claude-sonnet-5-5` and `claude-haiku-5-5`, pinned in `extract/config.py` and written on every
  result line.
* Structured output through `output_config.format` with the JSON Schema generated by the H6 pipeline,
  passed through the SDK's documented `anthropic.transform_schema` (it moves constraints the API does
  not enforce, such as `pattern` and `minLength`, into descriptions; Pydantic re-checks them).
* Sampling: these models reject non-default `temperature`, `top_p` and `top_k`, so "fixed sampling
  settings" means fixed `effort`, thinking mode and `max_tokens`, recorded on every line.
* **No refusal fallbacks.** A server-side fallback would silently answer with another model inside a
  run that is supposed to measure one model. A refusal is recorded as `refused` and reported as a refusal
  rate per model and arm.
* Prompt caching: system prompt, instructions and schema in a cached prefix; cache read and cache
  write tokens are recorded separately. A prefix under the model's minimum cacheable size simply does
  not cache, and the report says so if that happens.
* Response cache: content-addressed and append-only, keyed on SHA-256 of (model, full request,
  settings). Reruns cost nothing. `ANTHROPIC_API_KEY` is read by the SDK from the environment and never
  logged.
* `LlmClient` is a Protocol; arms receive it by injection. Tests use a scripted fake. CI never calls the
  API.
* Before the test run: estimate cost from the dev runs, print it, and stop for approval above USD 25.

## Sample, split, pre-registration

* Frame: SPLs reachable from the H6 openFDA data plus the H6 gap labels. Strata: single vs
  multi-ingredient, solid oral vs liquid vs injectable, prescription vs OTC, from each SPL's gold.
* 200 labels, stratified, including every H6 gap label whose SPL is available. Only finished products
  have SPLs on DailyMed (bulk ingredients, drugs for further processing and vaccine bulk intermediates
  do not), so at most 37 of the 121 gap products can be included; the exact number is reported.
* 40 dev, 160 test, fixed seed, split committed. Prompts are tuned on dev only.
* `docs/h4/PREREG.md` (frozen prompts and hashes, exact metrics, threshold, decision rule) is
  committed before the first test run. Test is scored once per arm; any later change is a logged
  deviation with both results reported.

## Stages

1. SPL fetch, input/gold split, leak test.
2. Extraction models, context shapes, reference graph, resolvers.
3. LLM client, response cache, fake client.
4. Arms A0 to A3.
5. Scorer.
6. Dev iteration (needs `ANTHROPIC_API_KEY`).
7. PREREG commit.
8. Test runs (cost estimate first).
9. Report.
10. PR.

## Findings while building (stages 1 and 2)

* **H6 regression found:** from schema 1.0.0 the generated JSON Schema had lost every `pattern` once
  strings became SafeIdentifier or SafeText. Nothing caught it, because the JSON Schema is drift-checked
  but not validated and the breaking gate compares OWL and SHACL only. Fixed in 2.1.0 with a test.
* **Registry shape.** The openFDA NDC directory's `package_ndc` is the outer package only; inner package
  NDCs appear only in the package description text, and SPL gold lists every level. The registry adds
  the nested NDCs. 30 of the 200 labels have no NDC directory record, so "this NDC must exist in the
  registry" applies only when the registry lists the label's products (rule listed-package-resolves).
* **Gold names resolve cleanly.** 2,119 of 2,124 gold active ingredient names resolve to their own UNII;
  5 are ambiguous (one name, several UNIIs); none resolves to a different UNII.
* **DailyMed returns 200 with the body `None`** for some set IDs, and occasionally 500. Both are recorded
  (not cached) instead of aborting the frame.
