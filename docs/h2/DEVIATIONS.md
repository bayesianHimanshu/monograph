# H2 deviations

Append-only: entries are added at the end and never edited or removed (`monograph check prereg` enforces
it). The PREREG itself is immutable; a change to how H2 is frozen is recorded here instead.

## D1, 2026-11-27: frozen artifacts pinned under experiments/h2/frozen/

**Reason.** The PREREG was committed with live paths in its frozen table: prompts under `src/`, the
question set under `fixtures/`, and the generated DDL and graph projection, which are regenerated whenever
the drug product model evolves. The rule now is that a PREREG names pinned copies under
`experiments/hN/frozen/`, never live paths, so the H2 artifacts are pinned byte-identical here, along with
the rendered artifacts the model actually saw: the tool definitions as sent for each arm and the two schema
descriptions returned by `describe_schema`.

**H2 ran at** commit `c9788be4c0b9e780dafdbbc2678104340e91f9b7` (the PREREG commit): all 1920 test records
in `results/h2/` carry that sha, marked `-dirty` because the working tree held uncommitted files outside the
frozen set; the frozen-file guard passed before every test run.

**Verification of the copies.** Each file pin is the blob at the run commit, and its sha256 equals the
PREREG's frozen-table digest (the live files were identical as well). For every arm, the system prompt
assembled from the pinned prompt files and the pinned tool definitions hash to the PREREG's per-arm
digests and to the `system_prompt_sha256` and `tools_sha256` on every test record of that arm. The schema
descriptions are rendered by the frozen `freeform.py` from the pinned DDL and projection; nothing earlier
recorded their hash, so the digests below are the first record of them.

**How the pins are used.** A rerun sends the pinned system prompts and tool definitions, returns the pinned
schema descriptions, and loads the pinned question set; the report selects runs by the pinned hashes. Code
executes from `src/`, so a test rerun is refused unless each live code file equals its pin, and unless the
live DDL and projection equal theirs (the database is loaded from the live model, so it must still be the
store the descriptions describe).

**No H2 result changes.** `results/h2/` is untouched, the pins are the exact bytes that ran, and the H2
report and verdict are unchanged.

| PREREG reference | Pinned copy | sha256 |
|---|---|---|
| `src/monograph/agent/prompts/base_v1.md` | `experiments/h2/frozen/base_v1.md` | `e18219d46adfd65be9b42760d9870c9129b9af5f5f5771cb448b3b52d2844bea` |
| `src/monograph/agent/prompts/arm_S-free_v1.md` | `experiments/h2/frozen/arm_S-free_v1.md` | `db77173c3120a456f04c8fac65dae6ffaadbce1b457d57d63012ea55718d95b6` |
| `src/monograph/agent/prompts/arm_S-tools_v1.md` | `experiments/h2/frozen/arm_S-tools_v1.md` | `1a189c1368922ad13085cf7f06586715b47977bcf71886395731886d2eec355e` |
| `src/monograph/agent/prompts/arm_G-free_v1.md` | `experiments/h2/frozen/arm_G-free_v1.md` | `a376a5de6db4bdf148d8a3cd1134d7c0e7fe4f5953b5db9f23e8a3c38a5ee235` |
| `src/monograph/agent/prompts/arm_G-tools_v1.md` | `experiments/h2/frozen/arm_G-tools_v1.md` | `1a189c1368922ad13085cf7f06586715b47977bcf71886395731886d2eec355e` |
| `src/monograph/agent/arms.py` | `experiments/h2/frozen/code/src/monograph/agent/arms.py` | `102fc84d111cb963d2b434959c1fbfcae360ad3ebc0410b7d1428c75298aa0a4` |
| `src/monograph/agent/loop.py` | `experiments/h2/frozen/code/src/monograph/agent/loop.py` | `39806558b930645131dcb3e830ffd042d0383cbb4b750d00260ba5e0a7fab4bb` |
| `src/monograph/agent/llm.py` | `experiments/h2/frozen/code/src/monograph/agent/llm.py` | `07ee36e167fbd051ec797e2966c4da1ec91d7d7bcd45de1d2304a9b46ba89bd6` |
| `src/monograph/tools/registry.py` | `experiments/h2/frozen/code/src/monograph/tools/registry.py` | `a5e44e9dad4f47ed7e04019b7a5b78ba171a5c5a6d556ee5712c01d6cbc4a9a0` |
| `src/monograph/tools/service.py` | `experiments/h2/frozen/code/src/monograph/tools/service.py` | `830e0f1a27b0cfe4cb2355c3e4cd634a2c362d0d6f78f9d2ea5ab12e6c1f3ba2` |
| `src/monograph/tools/sql_backend.py` | `experiments/h2/frozen/code/src/monograph/tools/sql_backend.py` | `9e3259dda7e13a02fcf01305b42f30934da9aa432df29c2c66a9668669aaaa8d` |
| `src/monograph/tools/cypher_backend.py` | `experiments/h2/frozen/code/src/monograph/tools/cypher_backend.py` | `8d40f89a4a325b51b4ad6c7cec3ed3d730d2006f856dda27ce0f483a97d3ccb3` |
| `src/monograph/tools/freeform.py` | `experiments/h2/frozen/code/src/monograph/tools/freeform.py` | `3ee517b6af194d8dec908826fbca501e571da91f6cb8f05a6aa7403d4bf5ab41` |
| `src/monograph/qa/score.py` | `experiments/h2/frozen/code/src/monograph/qa/score.py` | `9da2a341dc0581876814e3e82e6a461cbddea23b317ea9956dbd53993cf92e68` |
| `src/monograph/qa/templates.py` | `experiments/h2/frozen/code/src/monograph/qa/templates.py` | `9df10495241508c31f7736adcfec95f2c386bf93186e6addf2724368ebe01d5a` |
| `fixtures/h2/questions.json` | `experiments/h2/frozen/questions.json` | `ad61c96ea777ec09d83203a917cac3c430bb287ba1497cf848851bc4a18aa113` |
| `generated/monograph.age.json` | `experiments/h2/frozen/monograph.age.json` | `f4fec6f5900b230901ec05de82633f77ea2b44dc6989998f1185769bbf5128f9` |
| `generated/monograph.ddl.sql` | `experiments/h2/frozen/monograph.ddl.sql` | `f744c93e50b5dfa76f3f37cfafbfd7b63febf2116aa04cc0a6a1d7849f3887d4` |
| tools as sent, S-free | `experiments/h2/frozen/tools.S-free.json` | `046cb02032decbc01fff46f2e0ba9b26065adcc6a9a0ad597c07c473b3b85334` |
| tools as sent, S-tools | `experiments/h2/frozen/tools.S-tools.json` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` |
| tools as sent, G-free | `experiments/h2/frozen/tools.G-free.json` | `7992840d7b4b76be849872518fa122d89306dd66cef0f4e939e8fe050f3a998d` |
| tools as sent, G-tools | `experiments/h2/frozen/tools.G-tools.json` | `af3e4043cb91ed0d0df132d71e7310aaac6b56798ddf01404a90397aa7a5adb4` |
| schema description, SQL | `experiments/h2/frozen/schema.sql.txt` | `48848b239ec0faa81ef967c327893e27297279636e906a315bb02f1f14706574` |
| schema description, graph | `experiments/h2/frozen/schema.graph.txt` | `a01bd46f38ac0b9dc30ce8e648db4b7ad9b730ad1f3005a1f46391ca42fcf52d` |
