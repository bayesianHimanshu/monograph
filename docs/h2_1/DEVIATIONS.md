# H2.1 deviations

Append-only. Each entry: date, what happened, what was done, and what it changes.

## D1, 2026-11-28: an AGE server crash during the test runs; reruns; one question that cannot be scored

**What happened.** At 06:50:42 UTC a PostgreSQL backend in the AGE container was terminated by signal 11
(segmentation fault) while running a G-free Cypher query: Sonnet, the hardest cell, question
`t10_firms_within_six_steps-10` (three OPTIONAL MATCH branches collected with `collect(distinct id(...))`, then
`MATCH ... WHERE id(x) IN ids`). PostgreSQL then restarted every backend, so every open agent connection in
every cell was lost. Each worker thread keeps one connection for all its questions, so the loss persisted:

* Haiku S-tools, all four cells (the configurations running at that moment): the 1,083 questions after the
  crash failed with a closed connection before the agent could use the store. They were logged as
  `h2_1.error` and not scored.
* Sonnet G-free, baseline (50 runs) and hardest cell (82 runs, the crashing run among them): the free-form
  runner returns a database error to the agent as a query error, so every later `run_cypher` call returned
  "the connection is lost" and these runs were scored as wrong answers although no query reached a store.
* No other configuration was affected (checked: no other test run has a connection-loss error in any call).

**What was done.**

* The four Haiku S-tools configurations are rerun with the unchanged CLI (`monograph h2_1 run --split test`)
  from a clean tree. Questions already scored replay from the response cache at no cost and produce the same
  records; the errored questions run for the first time.
* The 131 Sonnet G-free runs that lost their connection without causing the crash are rerun with the pinned
  run code unchanged (`run_one`, with its clean-tree gate, frozen-file check, pinned prompts, tools and
  settings), with the question list narrowed to those ids, in small batches with a check for a new server
  crash after each batch.
* The crashing run is kept as recorded: the agent's own query crashed the store. It counts against G-free,
  and the report lists the crash among the AGE limitations.

**Unscorable question.** Haiku, G-free, baseline cell, `t10_fewest_steps_substance_to_firm-13` raised
`TypeError: '<' not supported between instances of 'NoneType' and 'str'` inside the free-form runner: `canon`
sorts every list in a result, and the agent's Cypher query returned a list mixing nulls and strings (the same
code ran H2). The code is pinned and is not changed during the test runs; a rerun would replay the same cached
query and fail the same way. The question is left out of that configuration (314 of 315); every paired
comparison uses the questions both sides have.

**How the results are read.** Results are append-only: the original records stay in `results/h2_1/`. The
report and the predictions use the latest record per model, cell, arm and question, so a rerun replaces the
record it reruns and nothing else.

**What this changes.** No question, gold, prompt, tool, setting, threshold or decision rule changes. Only
infrastructure failures are rerun; no agent outcome is rerun or discarded.

### D1, outcome (appended after the reruns, 2026-11-28)

* Haiku S-tools: the four configurations were rerun with the CLI from clean commit `62de336`; each now has 315
  scored questions. Questions scored before the crash replayed from the response cache.
* Sonnet G-free: the 131 runs that lost their connection were rerun from clean commit `62de336` in 14 batches
  of up to 10 questions; no batch crashed the server (one crash in the container's log, the original). The
  latest record of every test question now has no connection-loss error, except the crashing run
  (`t10_firms_within_six_steps-10`, Sonnet, G-free, hardest cell), which is kept as recorded.
* Test API spend, all test records including the ones the reruns replaced: USD 45.12, against the USD 41.32
  estimate the user approved; the difference is the work the crash forced to be redone.
