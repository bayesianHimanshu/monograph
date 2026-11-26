# H4 pre-registration

Committed before the first test run. `monograph h4 run --split test` refuses to start unless this file is
committed and unchanged. Any change after the first test run is a deviation: it is logged as an
`h4.deviation` result line, and both the original and the changed results are reported.

## Hypothesis

LLM extraction of product facts from FDA label narrative is usable only with graph-context (SHACL)
repair: record-local repair (Pydantic feedback, deterministic fixes) is not enough, and validating the
extraction merged into the reference graph is what makes it usable.

## Data

* 200 SPL labels from DailyMed, stratified by ingredient count (single, multi), form family (solid oral,
  liquid, injectable) and marketing category (rx, otc), including every H6 gap label whose SPL exists
  and has narrative text. Split with a fixed seed into 40 dev and 160 test labels:
  `fixtures/h4/split.json`, sha256 `0a754a258538953b4fcd0acc4eaf55c335e34c27a417f5e82a217db8e4598ac1`.
* Input to the model: document title and the narrative sections 34089-3, 43678-2, 55106-9, 34069-5,
  44425-7, 51945-4. Gold: the structured product data (section 48780-1) of the same document. A leak test
  proves no structured value reaches a prompt.

### Allergenic stratum (part of the design, not a deviation)

Rule: any label whose product type is an allergenic extract (standardized or non-standardized) forms its
own stratum, "allergenic". Reason: the five such labels carry 70% of all gold ingredients and 75% of all
package NDCs in the sample, so in pooled metrics one product type would decide the result. Allergenic
labels are run through every arm and reported as their own stratum and in the H6 gap section. They are
excluded from the headline metrics, the threshold and the verdict. Test has 4 allergenic labels, so the
headline set is 156 test labels.

## Configurations

Models: `claude-sonnet-5-5`, `claude-haiku-5-5`. Settings, fixed for every request: `max_tokens` 16000,
`effort` "medium", thinking adaptive (the default; not sent), no temperature/top_p/top_k (these models
reject non-default values), no refusal fallbacks, structured output with the generated extraction JSON
Schema passed through `anthropic.transform_schema`, system prompt cached (`cache_control` ephemeral). Prompt version: **v3**.

| Arm | After the first extraction |
|---|---|
| A0 | nothing |
| A1 | Pydantic validation; on failure the errors are sent back and the model answers again |
| A2 | A1, then deterministic repair (UCUM unit spellings, whitespace, NDC hyphenation); no LLM call |
| A3 | A2, then SHACL validation of the extraction merged into the reference graph, plus the two `extraction_only` checks; on violations the report (location, path, message, offending value, allowed candidates) is sent back and the model answers again |
| A3+ | A3, but allowed candidates come from the label's own registry records. **Upper bound only, outside the verdict.** |

Each repair loop is capped at 3 rounds after the first extraction. Every round is logged.

### A3 feedback never contains values from the label itself

A3 candidates come only from sources independent of the label being extracted: substance names that
some source not linked to this label also contributes (DailyMed's moiety list, other labels' SPLs, NDC
directory records of other labels), and NDCs with the same labeler code known from records not linked to
this label. Never the label's own SPL, its DailyMed NDC list, or the NDC directory records that carry its
set ID. Messages say a value does not resolve or does not belong; they state no registry fact taken from
the label (the ingredient-count message does not give the count). `tests/test_h4_feedback_leak.py`
plants sentinels in the label's own lists and proves A3 never offers them, and that A3+ does.

A3+ keeps the label-derived candidates (the label's products' package NDCs, the nearest names in the
whole graph, the registry's ingredient count). Those can contain gold, which is why A3+ measures how far
a registry lookup can go, not extraction, and is never part of the verdict.

### Resolution is one deterministic function for every arm and for scoring

Substance names resolve by normalized exact match against the reference graph; NDCs by exact match after
hyphen normalization. When several substances share a normalized name, the tie is broken by the UNIIs
that label-linked registry records (the openFDA NDC directory and label records carrying this set ID)
attach to the label; if exactly one of the tied UNIIs is among them it wins, otherwise the name stays
ambiguous. The tie-break uses label-linked registry data, which is what a production pipeline would have
for a known label. It never resolves a name that matches nothing, and it is the same code
(`SubstanceResolver.for_label`, called from `resolve_extraction` and from `LabelScorer.score`) for the
A3 context check, for A3+, and for scoring every arm, A0 included. So it shifts every arm alike and
cannot favour A3.

## Metrics

Computed by `monograph h4 score` per model and arm, on the headline labels, per stratum, and on the H6
gap labels.

The executable definitions are the docstring and code of `src/monograph/extract/score.py` (hash below).

* Ingredient tuple: (UNII, strength per one basis unit, numerator dimension, denominator unit), after
  converting mass to mg and volume to mL. Extracted names get their UNII from the deterministic resolver;
  an unresolved name yields no UNII and cannot match. A label's tuples are the distinct tuples of its
  products (a kit's repeated parts, or one strength split into package-size products, state one fact).
* Strength basis: when the SPL states an ingredient's strength in terms of its active moiety (classCode
  ACTIM) or a reference substance (ACTIR), an extracted name resolving to that substance counts as the
  gold ingredient. ACTIB strengths are of the ingredient itself and get no alias; a UNII that is itself
  a gold ingredient of the label is never aliased.
* Other strengths: an extracted product that aligns to no gold product, uses only the label's gold
  substances, matches no gold tuple and prints every strength value in the text is a strength the
  narrative describes but this SPL's product data omits. It is counted on its own and left out of F1,
  product and NDC counts; its values still enter the hallucination and unresolvable rates.
* **Primary: the mean of per-label ingredient-strength F1** over those tuples (1.0 when gold and
  extraction are both empty; a record with no valid final answer extracts nothing, so it scores 0).
* Secondary: pooled F1 over all tuples.
* Products are aligned gold to extracted, greedily by shared tuples then name similarity. Dosage form
  accuracy and route accuracy are over gold products (an unaligned gold product counts as wrong).
* Unresolvable reference rate: extracted substance names that resolve to no single Substance, and
  extracted NDCs that match no PackagedProduct when the registry lists the label's products, over all
  extracted references.
* Hallucination rate: extracted substance names, strength values, basis values and NDCs that appear
  neither in the input text nor in gold, over all such extracted values. Enum fields (dosage form, route,
  units) are classifications and are not checked. Basis 1 is the implicit basis (per unit, or per 1 mL in
  "mg/mL"); basis 100 is not checked when the text prints that strength as a percentage.
* Exact match: tuples equal, every gold product aligned with the right dosage form and routes, the same
  number of products, and the extracted NDC set equal to the gold NDCs that the narrative prints.
* Violation-free rate (Pydantic-valid with no context issue), mean repair rounds.
* **Refusal rate** and **truncation rate**: the share of records whose final response stopped with
  `refusal` or `max_tokens`. A truncated record is recorded as `truncated`, not as an ordinary failure.
* Repair attribution: field errors after the first round and at the end, which step fixed each, and how
  many correct fields a step broke (regressions).
* H6 gap: gap products whose gold ingredient tuples are extracted exactly.
* Cost (from recorded token usage at the prices in `extract/config.py`, cache reads and writes
  separately), cost per record, cost per usable (exact-match) record; latency p50 and p95.
* 95% bootstrap intervals (10,000 resamples over labels, seed 20261127) for mean F1, and paired
  bootstrap intervals for the arm differences A1-A0, A2-A1, A3-A2, A3-A1, A3+-A3 and for the model
  difference per arm.
* Top 10 residual error patterns per model for A2 and A3.

## Threshold (fixed by the brief)

A configuration is usable if, on the headline test labels, all hold:

* mean per-label ingredient-strength F1 >= 0.95
* dosage form accuracy >= 0.97 and route accuracy >= 0.97
* unresolvable reference rate <= 1%
* hallucination rate <= 0.5%

## Decision rule

Over arms A0 to A3 only. Evaluated in this order:

1. **fails** if, for every model, A1 or A2 is usable (record-local repair is enough everywhere);
2. **holds** if, for at least one model, A3 is usable and A2 is not;
3. **partly holds** otherwise.

## Oracle ceiling

Scoring the gold itself as if it were an extraction, through the same resolution and scorer:

| Split | Labels | Mean F1 | Pooled F1 | Form | Route | Unresolvable | Hallucinated |
|---|---|---|---|---|---|---|---|
| dev headline | 39 | 0.974 | 0.990 | 1.000 | 1.000 | 0.72% | 0.00% |
| test headline | 156 | 1.000 | 1.000 | 1.000 | 1.000 | 0.00% | 0.00% |

The one dev label below 1 (vasopressin, set ID 40dab227) has no NDC directory record and an empty
`openfda` block on its label record, so no label-linked registry data breaks the tie between the two
UNIIs named VASOPRESSIN. The test ceiling is 1.0 on every metric.

## Frozen artifacts

| Artifact | sha256 |
|---|---|
| `src/monograph/extract/prompts/system_v3.md` | `221c81ca28b96ae6690f8b45a1bbc0225a3080117b184739b1f0b1e342d13732` |
| `generated/monograph.extraction.schema.json` | `600131d475ec05e4c0a6f430efd8d607c3d068e77dd63bfcf7c5cac6a4e70661` |
| `generated/monograph.context.shapes.ttl` | `bd4697b3a4c6df2a094e7e78f7e8376351ee2ed8d9f4ab2e84547ecb45665c70` |
| `fixtures/h4/split.json` | `0a754a258538953b4fcd0acc4eaf55c335e34c27a417f5e82a217db8e4598ac1` |
| `src/monograph/extract/repair/pydantic_feedback.py` | `4c03594100c783b9b4f82a52790fba6e291fd634919b70c7ba83178b8f32f1cb` |
| `src/monograph/extract/repair/deterministic.py` | `5966efe6c19a414e390a54a6cb3132fa944313d5ca2427c7517f93764239376f` |
| `src/monograph/extract/repair/shacl_feedback.py` | `8f1541b5452591bb48f7f86757b0764fce6a1419a908276546f5e7ed90704a5d` |
| `src/monograph/extract/arms.py` | `317074b51533374015177285f1d6b9912ee88b95f580e77649fe3c540a2df482` |
| `src/monograph/extract/resolve.py` | `edbab35c5e51c7451af6a84caa28aaa2d4d0fd674776f265300d634475d3daf1` |
| `src/monograph/extract/score.py` | `401426ffa74235573031723c91190e147837c22b8a03b23fcedef2268bd57c70` |
| `src/monograph/extract/config.py` | `75e2390b2aad84ac3decaf08cc27fc546d773b629d269cf9bd8d29ee4470ed7e` |
| `src/monograph/extract/prompts/__init__.py` | `df02653ac7c16ca0875dd6fa566010d5fe4c334e4e62bd05422c077f4e17f170` |
| system prompt as sent (instructions + transformed schema), `system_prompt_sha256` | `34445d019dc7645fb265953ed53bd7f22e43c432eea7041a86b88235024a29ca` |
| transformed schema as sent, `schema_sha256` | `5ac2d9902d852c81c8f1e0c4d061749739c1a613e1746e681be6d8e68daa9203` |

The feedback texts are code (`repair/pydantic_feedback.py`, `repair/shacl_feedback.py`), so their hashes
freeze them. Test runs are refused unless this file is committed and unchanged.

## Dev iteration

Dev (40 labels, 39 headline) was used to find scorer artifacts and tune the prompt. Every scorer
change below corrects a disagreement between a faithful extraction and how SPL product data encodes the
same fact; each applies to every arm alike, and the oracle ceiling was re-checked after each (unchanged).

| Change | Evidence on dev | Effect on Sonnet A3 mean F1 (dev) |
|---|---|---|
| Strength basis aliases (ACTIM/ACTIR) | "ampicillin 1 g" for ampicillin sodium stated as ampicillin, scored wrong | 0.723 to 0.813 |
| Distinct tuples per label | a kit repeats one tuple 8 times; one strength split into 15 mL and 30 mL products | 0.813 to 0.866 (with the next row) |
| Other strengths outside the SPL data | ciprofloxacin narrative describes 250/500/750 mg, SPL data has 500 mg only | (as above) |
| Percent and implicit bases not hallucinated | "basis 100" from "5%", "per 1 mL" from "mg/mL" | hallucination 4.3% to 1.8% |

Prompts:

* v1: the first prompt. It told the model a bare percentage is w/w; SPL encodes liquids per mL.
* v2: percentages by dosage form (per 100 mL for liquids, per 100 g for solids and semisolids), based on
  how 1,302 SPLs outside the test split encode them; and a kit instruction.
* v3 (frozen): v2 without the kit instruction, which on dev fixed no kit and turned two multi-bottle
  solutions into KIT.

Dev results with v3 (headline, 39 labels; dev is not the test and is not used for the verdict):

| Model | A0 | A1 | A2 | A3 | A3+ |
|---|---|---|---|---|---|
| claude-sonnet-5-5 mean F1 | 0.786 | 0.786 | 0.786 | 0.917 | 0.891 |
| claude-haiku-5-5 mean F1 | 0.774 | 0.774 | 0.774 | 0.896 | 0.896 |

No dev configuration meets the threshold (dosage form accuracy 0.78 to 0.80 is below 0.97 in every arm).

Remaining dev errors are mostly dosage form terms the narrative does not settle (LIQUID vs SOLUTION,
INJECTION vs "INJECTION, SOLUTION", CAPSULE vs "CAPSULE, LIQUID FILLED"). No arm targets dosage form
beyond enum membership, and the threshold is fixed, so they were left in the score rather than tuned.

## Cost approval

Before the test run, `monograph h4 run --split test` estimates the cost from the mean dev cost per record
for each model and arm (shared rounds counted in every arm, so it overestimates), prints it, and stops if
it exceeds USD 25 unless `--approve-cost` is given.
