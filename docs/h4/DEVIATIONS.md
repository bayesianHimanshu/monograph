# H4 deviations

Append-only: entries are added at the end and never edited or removed (`monograph check prereg` enforces
it). The PREREG itself is immutable; a change to how H4 is frozen is recorded here instead.

## D1, 2026-11-27: frozen artifacts pinned under experiments/h4/frozen/

**Reason.** The PREREG froze `generated/monograph.extraction.schema.json` and
`generated/monograph.context.shapes.ttl` at their live paths. Those files are regenerated from the drug
product model, and the live model must be free to evolve: the 2.2.0 bump for H2 restamped the extraction
schema, and any additive enum change (a new Route value, say) rewrites both files. Keeping H4 frozen at the
live paths would either freeze the model or force H4's check to be loosened, which was tried and reverted.
Instead every file the PREREG references is pinned, byte-identical, under `experiments/h4/frozen/`, and the
H4 check and any H4 rerun use the pins.

**H4 ran at** commit `d8692460f97abdc9cc39a5790d9a63eea483ac4a` (the PREREG commit): all 1600 test records
in `results/h4/` carry that git sha.

**Verification of the copies.** Each pin is the file's blob at the run commit, and its sha256 equals the
PREREG's frozen-table digest. The extraction schema pin (stamped 2.1.1) is also byte-identical to
`generated/releases/2.1.1/monograph.extraction.schema.json`, and the system prompt and transformed schema
built from it hash to the PREREG's `system_prompt_sha256` (`34445d01...`) and `schema_sha256`
(`5ac2d990...`), the same values recorded on all 1600 test records. The script that made the copies aborts
on any mismatch; none occurred.

**How the pins are used.** Data H4 loads is read from the pin, never the live path: the extraction schema,
the context shapes and the split (`monograph.extract.setup`). Code, and the prompt file that the frozen
prompt loader reads from its own package, still execute from `src/`, so the H4 check requires each live
file to equal its pin (as before). The live extraction schema now follows `SCHEMA_VERSION` again.

**No H4 result changes.** `results/h4/` is untouched, the pins are the exact bytes that ran, and the H4
report and verdict are unchanged.

| PREREG reference | Pinned copy | sha256 |
|---|---|---|
| `src/monograph/extract/prompts/system_v3.md` | `experiments/h4/frozen/system_v3.md` | `221c81ca28b96ae6690f8b45a1bbc0225a3080117b184739b1f0b1e342d13732` |
| `generated/monograph.extraction.schema.json` | `experiments/h4/frozen/monograph.extraction.schema.json` | `600131d475ec05e4c0a6f430efd8d607c3d068e77dd63bfcf7c5cac6a4e70661` |
| `generated/monograph.context.shapes.ttl` | `experiments/h4/frozen/monograph.context.shapes.ttl` | `bd4697b3a4c6df2a094e7e78f7e8376351ee2ed8d9f4ab2e84547ecb45665c70` |
| `fixtures/h4/split.json` | `experiments/h4/frozen/split.json` | `0a754a258538953b4fcd0acc4eaf55c335e34c27a417f5e82a217db8e4598ac1` |
| `src/monograph/extract/repair/pydantic_feedback.py` | `experiments/h4/frozen/code/src/monograph/extract/repair/pydantic_feedback.py` | `4c03594100c783b9b4f82a52790fba6e291fd634919b70c7ba83178b8f32f1cb` |
| `src/monograph/extract/repair/deterministic.py` | `experiments/h4/frozen/code/src/monograph/extract/repair/deterministic.py` | `5966efe6c19a414e390a54a6cb3132fa944313d5ca2427c7517f93764239376f` |
| `src/monograph/extract/repair/shacl_feedback.py` | `experiments/h4/frozen/code/src/monograph/extract/repair/shacl_feedback.py` | `8f1541b5452591bb48f7f86757b0764fce6a1419a908276546f5e7ed90704a5d` |
| `src/monograph/extract/arms.py` | `experiments/h4/frozen/code/src/monograph/extract/arms.py` | `317074b51533374015177285f1d6b9912ee88b95f580e77649fe3c540a2df482` |
| `src/monograph/extract/resolve.py` | `experiments/h4/frozen/code/src/monograph/extract/resolve.py` | `edbab35c5e51c7451af6a84caa28aaa2d4d0fd674776f265300d634475d3daf1` |
| `src/monograph/extract/score.py` | `experiments/h4/frozen/code/src/monograph/extract/score.py` | `401426ffa74235573031723c91190e147837c22b8a03b23fcedef2268bd57c70` |
| `src/monograph/extract/config.py` | `experiments/h4/frozen/code/src/monograph/extract/config.py` | `75e2390b2aad84ac3decaf08cc27fc546d773b629d269cf9bd8d29ee4470ed7e` |
| `src/monograph/extract/prompts/__init__.py` | `experiments/h4/frozen/code/src/monograph/extract/prompts/__init__.py` | `df02653ac7c16ca0875dd6fa566010d5fe4c334e4e62bd05422c077f4e17f170` |
| `extract/config.py` | `experiments/h4/frozen/code/src/monograph/extract/config.py` | `75e2390b2aad84ac3decaf08cc27fc546d773b629d269cf9bd8d29ee4470ed7e` |
| `repair/pydantic_feedback.py` | `experiments/h4/frozen/code/src/monograph/extract/repair/pydantic_feedback.py` | `4c03594100c783b9b4f82a52790fba6e291fd634919b70c7ba83178b8f32f1cb` |
| `repair/shacl_feedback.py` | `experiments/h4/frozen/code/src/monograph/extract/repair/shacl_feedback.py` | `8f1541b5452591bb48f7f86757b0764fce6a1419a908276546f5e7ed90704a5d` |
| `tests/test_h4_feedback_leak.py` | `experiments/h4/frozen/code/tests/test_h4_feedback_leak.py` | `ac49ab933080d59d6aa3d362d761ae8e793807f52c0de5cea274f1435ca1fb54` |
