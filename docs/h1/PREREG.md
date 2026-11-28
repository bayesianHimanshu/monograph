# H1 pre-registration

Committed before the first test run. `monograph h1 run --split test` refuses to start from a dirty tree, if any file in the frozen table below no longer matches its sha256, if code that executes no longer equals its pinned copy, if a generated artifact no longer equals its pin, if the database it would query was not built from the pinned artifacts (build stamp), or without the approved spend cap (written by the user after the test estimate; a run stops at it). Every file the agents are sent, and every mapping the check reads, comes from these pins. Any change after this commit is a logged deviation in `docs/h1/DEVIATIONS.md`.

## Hypothesis

**H1: ontology-checked text-to-SQL beats semantic views alone.** On a deliberately realistic warehouse whose schema does not carry the meaning, the model is given either a well-built semantic layer or the ontology with its mapping to the warehouse, and its SQL is either run as written (with execution-error retry) or checked against the ontology first and repaired on violations. The design, the warehouse, the semantic layer and its coverage rule, the four checks and the metrics are in `docs/h1/PLAN.md` (committed before any code or question existed); this file fixes what the test runs and how they are judged.

## Arms

| Arm | Context (describe_data) | Database (schemas) | Check |
|---|---|---|---|
| W-raw | the warehouse DDL only | monograph_h1_wh (wh) | shadow |
| SV | semantic model, views, raw tables listed | monograph_h1_sv (wh, sem) | shadow |
| SV+OC | as SV | monograph_h1_sv (wh, sem) | enforce |
| ONT | ontology and its mapping to the warehouse | monograph_h1_wh (wh) | shadow |
| ONT+OC | as ONT | monograph_h1_wh (wh) | enforce |

Every arm: the same base prompt (`h1_base_v1.md`), the same tools (describe_data, run_sql, submit_answer), the same answer schema, the same settings and the same execution-error retry (a failed execution is returned; after 4 failed executions, the first plus 3 retries, run_sql executes nothing more). Enforce: a query with violations is not executed and the feedback is returned, at most 3 repair rounds per question, after which it runs as written with the violations attached. Shadow: every query is checked, nothing is sent.

## Models and settings

* `claude-haiku-5-5`: all five arms, every type.
* `claude-sonnet-5-5`: SV, ONT+OC, W-raw, on Q2, Q3, Q4, Q5.

| Setting | Value |
|---|---|
| max_tokens | 16000 |
| effort | medium |
| thinking | adaptive (default; not sent) |
| max_tool_calls | 12 |
| max_turns | 16 |
| max_output_tokens | 64000 |
| result_char_cap | 60000 |
| refusal_fallbacks | False |

## Questions

60 per type, 15 dev and 45 test, seed 20261128, 31 templates with three hand-written phrasings each; gold by code over the canonical instances; every question's reference SQL on the warehouse reproduces its gold.

| Type | Dev | Test | Test inside coverage | Test outside coverage |
|---|---|---|---|---|
| Q1 | 15 | 45 | 45 | 0 |
| Q2 | 15 | 45 | 45 | 0 |
| Q3 | 15 | 45 | 0 | 45 |
| Q4 | 15 | 45 | 0 | 45 |
| Q5 | 15 | 45 | 45 | 0 |
| Q6 | 15 | 45 | 35 | 10 |

## Scoring

* values: the answered set equals the gold set (business keys; firm and substance names accepted; case and surrounding space ignored; order and duplicates do not matter);
* number: |answer - gold| <= max(0.01, 0.001 x |gold|);
* none: `none` or an empty values list.

Failure categories, the first that applies: refusal; budget exhausted; SQL execution error after retries; hallucinated table or column; outside-coverage miss; identity miss; wrong or invalid code; fan-out double count; wrong join path; other wrong result (definitions in PLAN.md).

## Decision rules

* **Primary.** H1 **holds** if, for at least one model, ONT+OC beats SV by at least 0.1 on Q2 to Q5 combined (types the ceiling rule marks stay in), with the 95% paired bootstrap interval excluding 0, and ONT+OC is no more than 0.05 below SV on Q1 (point estimate). Sonnet has no Q1 runs, so for Sonnet the Q1 condition uses Haiku's Q1 result. H1 **fails** if, for both models, ONT+OC beats SV by at most 0.05 on Q2 to Q5 (SV within 0.05 of ONT+OC, or ahead of it); fails takes precedence. Otherwise it **partly holds**.
* **Secondary, inside coverage only.** The same rule on the Q2 and Q5 test questions inside semantic coverage.
* **Attribution**, always reported: context effect (ONT - SV, ONT+OC - SV+OC), check effect (SV+OC - SV, ONT+OC - ONT), floor (each arm - W-raw), per model where both arms ran.
* **Ceiling rule:** a type at 0.95 or higher in every arm (every configuration that ran it) on dev is marked non-informative. The verdicts above are on Q2 to Q5 as the hypothesis states; a sensitivity verdict without the marked types is reported when any core type remains unmarked.

### Ceiling marks (dev)

| Type | claude-haiku-5-5 W-raw | claude-haiku-5-5 SV | claude-haiku-5-5 SV+OC | claude-haiku-5-5 ONT | claude-haiku-5-5 ONT+OC | claude-sonnet-5-5 W-raw | claude-sonnet-5-5 SV | claude-sonnet-5-5 ONT+OC | non-informative |
|---|---|---|---|---|---|---|---|---|---|
| Q1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | n/a | n/a | n/a | yes |
| Q2 | 1.00 | 0.93 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | no |
| Q3 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | yes |
| Q4 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | yes |
| Q5 | 0.87 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | no |
| Q6 | 1.00 | 0.93 | 0.93 | 1.00 | 1.00 | n/a | n/a | n/a | no |

Non-informative types: Q1, Q3, Q4.

## Dev iteration

Prompts were not tuned between rounds; the question set and the warehouse were changed once, between
round 1 and round 2, as recorded in PLAN.md before round 2 ran.

### Round 1 (before the hardening pass): a finding

On a cryptic but consistent warehouse, both models answered from bare DDL with no meaning layer: Q1, Q3 and Q4 scored 1.00 in every arm, the W-raw floor included. The Q2 and Q5 failures were mostly defects in the question set shared by every arm (exact drug-name matching, the bare role suspect, recall year read from the recall number, loose dosage forms), fixed before round 2.

| Model | Arm | Prompt versions | Questions | Q1 | Q2 | Q3 | Q4 | Q5 | Q6 | All |
|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | ONT | ONT v1, base v1 | 90 | 1.00 | 0.80 | 1.00 | 1.00 | 0.87 | 0.93 | 0.93 |
| claude-haiku-5-5 | ONT+OC | ONT+OC v1, base v1 | 90 | 1.00 | 0.80 | 1.00 | 1.00 | 0.87 | 1.00 | 0.94 |
| claude-haiku-5-5 | SV | SV v1, base v1 | 90 | 1.00 | 0.80 | 1.00 | 1.00 | 0.87 | 1.00 | 0.94 |
| claude-haiku-5-5 | SV+OC | SV+OC v1, base v1 | 90 | 1.00 | 0.73 | 1.00 | 1.00 | 0.87 | 0.93 | 0.92 |
| claude-haiku-5-5 | W-raw | W-raw v1, base v1 | 90 | 1.00 | 0.80 | 1.00 | 1.00 | 0.87 | 1.00 | 0.94 |
| claude-sonnet-5-5 | ONT+OC | ONT+OC v1, base v1 | 60 |  | 0.87 | 1.00 | 1.00 | 0.87 |  | 0.93 |
| claude-sonnet-5-5 | SV | SV v1, base v1 | 60 |  | 0.87 | 1.00 | 1.00 | 0.87 |  | 0.93 |
| claude-sonnet-5-5 | W-raw | W-raw v1, base v1 | 60 |  | 0.87 | 1.00 | 1.00 | 0.87 |  | 0.93 |

Round 1 spend: USD 4.79.

### Round 2 (the pinned question set and warehouse)

The hardening pass of PLAN.md (staging tables, stale and ETL columns, a second code table, harder Q2 and Q5 templates) and the question fixes, applied once. W-raw core accuracy at most 0.80 was a check, not a target; whatever round 2 showed, the warehouse was not hardened again.

| Model | Arm | Prompt versions | Questions | Q1 | Q2 | Q3 | Q4 | Q5 | Q6 | All |
|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | ONT | ONT v1, base v1 | 90 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| claude-haiku-5-5 | ONT+OC | ONT+OC v1, base v1 | 90 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| claude-haiku-5-5 | SV | SV v1, base v1 | 90 | 1.00 | 0.93 | 1.00 | 1.00 | 1.00 | 0.93 | 0.98 |
| claude-haiku-5-5 | SV+OC | SV+OC v1, base v1 | 90 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.93 | 0.99 |
| claude-haiku-5-5 | W-raw | W-raw v1, base v1 | 90 | 1.00 | 1.00 | 1.00 | 1.00 | 0.87 | 1.00 | 0.98 |
| claude-sonnet-5-5 | ONT+OC | ONT+OC v1, base v1 | 60 |  | 1.00 | 1.00 | 1.00 | 1.00 |  | 1.00 |
| claude-sonnet-5-5 | SV | SV v1, base v1 | 60 |  | 1.00 | 1.00 | 1.00 | 1.00 |  | 1.00 |
| claude-sonnet-5-5 | W-raw | W-raw v1, base v1 | 60 |  | 1.00 | 1.00 | 1.00 | 1.00 |  | 1.00 |

Round 2 spend: USD 5.01. W-raw core (Q2 to Q5) accuracy on round 2: claude-haiku-5-5 0.97, claude-sonnet-5-5 1.00. The test estimate is printed by `monograph h1 estimate` after this commit; the test runs wait for the approved cap.

## Known limitations

* The data is the H2.1 baseline cell's (a targeted, connected frame, not a random sample of FDA data); the warehouse is generated from it, so its legacy traits are designed, not found.
* The semantic layer is written by the same author as the ontology and the check; it is built to the coverage rule fixed in PLAN.md before any question existed, with rich descriptions, synonyms, measures and joins, but it is one layer, not a sample of deployed ones.
* The check runs on the model's SQL only. Its rule 3 (fan-out) is a heuristic and knowingly flags some correct queries (a deliberate count of reaction mentions; a name join); the false-block rate measures the cost.
* Each test question runs once per configuration (one sample).
* Adverse event reports are concentrated in 2014, so report questions use months of 2014.
* A count of zero is never gold; questions naming nothing that exists are Q6 and must answer none.

## Frozen files

| File | sha256 |
|---|---|
| `experiments/h1/frozen/code/src/monograph/agent/budget.py` | `9d07e09f2de0557371c369b3e35e209b734307b8b5b0f9fabf9c59d7d22e82c6` |
| `experiments/h1/frozen/code/src/monograph/agent/llm.py` | `07ee36e167fbd051ec797e2966c4da1ec91d7d7bcd45de1d2304a9b46ba89bd6` |
| `experiments/h1/frozen/code/src/monograph/agent/loop.py` | `3c32faff73162053856815f48221697fb8a270c10c3bda511721724f51380697` |
| `experiments/h1/frozen/code/src/monograph/h1/analysis.py` | `a91546d76b2196dca87eaa307f94b6f5dd2d11ad3ba1af769148a4a18d5579f5` |
| `experiments/h1/frozen/code/src/monograph/h1/arms.py` | `13bfb30e17211349e0f50efc7280b93f982ade0709b340a536f4b9769464f7f2` |
| `experiments/h1/frozen/code/src/monograph/h1/build.py` | `034071ea462ba7b5a41495e6d785a7d35f428e2d50ef4b239481f362514b9af4` |
| `experiments/h1/frozen/code/src/monograph/h1/cli.py` | `5aa9ca87e52ee3a8be1af2f1a784e3eb13edb982a2c0f7d50f9ac2e9401b8c60` |
| `experiments/h1/frozen/code/src/monograph/h1/context.py` | `09b0a16cb8e7c741cace8642e587186d1cc02b8b237e353307822d89c6f3e87d` |
| `experiments/h1/frozen/code/src/monograph/h1/pins.py` | `9858ec286f1a2b31523b79cc1729fc50e26ce8b7827549f2065a567096329ec6` |
| `experiments/h1/frozen/code/src/monograph/h1/questions.py` | `c1f7246540a16d17601ef21d932480531f1649318292664e1d7a9113f0e3fa34` |
| `experiments/h1/frozen/code/src/monograph/h1/record.py` | `0a7cd3c0c28350da16394d3d3b343a5975ac8338a39aa2fba1cdb98757fd0c10` |
| `experiments/h1/frozen/code/src/monograph/h1/run.py` | `28f88c168942bddf8d2650c9d337523c618dc68b7c42f3a4d34dbcfb90f1ef87` |
| `experiments/h1/frozen/code/src/monograph/h1/score.py` | `e3360b652646dba4bca8ac45b0cd94f7dad39043a2563a3c37e89858cba29f33` |
| `experiments/h1/frozen/code/src/monograph/qa/h1_templates.py` | `5814c217ae9c2543be25e603df21c7c0fa39f4c5bd4d39e225d21896a50af4fa` |
| `experiments/h1/frozen/code/src/monograph/qa/run.py` | `40ab0258f55fba47fdd1efb90f37e9ad701d1f1c4b2a8ab2cfd47eee6360cd39` |
| `experiments/h1/frozen/code/src/monograph/sqlcheck/catalog.py` | `54d1803821a64d0309ad5b455e18167ae4a4a222129012849d8ead7723984b22` |
| `experiments/h1/frozen/code/src/monograph/sqlcheck/check.py` | `886192f2a5ebc05d3b803a476a5319fae8197122825bacb08b7674ec95757c54` |
| `experiments/h1/frozen/code/src/monograph/sqlcheck/gate.py` | `e41ef84843e53014732cdcd878c564c6b65c022e212ee94dbe58f481abc94086` |
| `experiments/h1/frozen/code/src/monograph/tools/freeform.py` | `c39a823907749b4dba6c1894784a7b5d66d5b2b61e851f8829f5e4f4d50f2aa8` |
| `experiments/h1/frozen/context.ONT.txt` | `ef30c2d9f8d50d92e82bb6bc435fa16902456bb3f00008992da3304bb2d4fa01` |
| `experiments/h1/frozen/context.SV.txt` | `24ecdebdcf57d2b66d7d86252a114d9020b19ada5c66dea45cf68847a6095aa0` |
| `experiments/h1/frozen/context.W-raw.txt` | `013a464b47226b66f0bdc47c7f94fc2dc29843a502e6ecee01385cde2aa43b23` |
| `experiments/h1/frozen/h1.semantic.mapping.json` | `0cd5111cb6bd3c0a728049773194fbcbd960aa4d821d761c59dde715fdbb2d3c` |
| `experiments/h1/frozen/h1.semantic.model.yaml` | `a43e44619795b09e370de155e674e0c3f6228cb7a1696618e2366fd0577c6f25` |
| `experiments/h1/frozen/h1.semantic.views.sql` | `b6d36a8c42399b2583a5edb295f6d5241f72da5ff81a312fbbf718b62a516915` |
| `experiments/h1/frozen/h1.warehouse.ddl.sql` | `b53e0f5b5122343b7d56c3cf7c5456571de245d3713c1b72f6f3dbb58410c866` |
| `experiments/h1/frozen/h1.warehouse.inverse.sql` | `f8d367cbf7936d65f9b75cecc8a734a1073add09dd2004a72d88c7ba954ffb3f` |
| `experiments/h1/frozen/h1.warehouse.load.sql` | `e0f64369063b2daf2d94cb59a9913752326f9c9befbd1bdf1b2833ae4da3cc31` |
| `experiments/h1/frozen/h1.warehouse.mapping.json` | `16b61d7c1817430257f69cbaf3a28c733883721bd2f29ab6532fc484a7b5cde7` |
| `experiments/h1/frozen/h1_arm_ONT_v1.md` | `f8d4444277736df20890437d93d87bc22c724ac4418e176f5ee7fb2782aa520f` |
| `experiments/h1/frozen/h1_arm_SV_v1.md` | `e640b906a4943afc93949100969933b3f19c4b19a537c1beb78b5005028ddfb3` |
| `experiments/h1/frozen/h1_arm_W-raw_v1.md` | `afbfc99d3c0e6e88ae21d13fd806c37d3fd81279933a9c9ea3bb8bc2bb867e08` |
| `experiments/h1/frozen/h1_base_v1.md` | `6744d6937993c921041c23749c2cc8c8317fe71f6baa4c1852a722f2e4d37adf` |
| `experiments/h1/frozen/h1_check_v1.md` | `be71de48893d6c62f4f92300ac1834ef55c9569356c6117350d7377b355885f8` |
| `experiments/h1/frozen/questions.json` | `be33018c1f9b9653553356a4aef5e0fa96efa7d55da77f40f6a204f7a98f0a34` |
| `experiments/h1/frozen/tools.json` | `f8ae1df79c7ad557b0b11fe35ef2ceddac563eb9dcda8c515664fbc68c865205` |

Warehouse mapping version 1.0.0; semantic layer version 1.0.0.
