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

## D2, 2026-11-28: one T9 template counts a product as sharing an ingredient with itself

**What happened.** Reading the worked examples after the test runs showed that the gold of
`t9_labelers_of_ingredient_sharers` ("Which firms label a product that shares an active ingredient with the
product P?") includes P itself among the products that share an ingredient with P. The base prompt defines
the relation for "two products", which reads as two distinct products. For most questions the two readings
give the same answer; for 4 of the 15 test questions of this template they do not (`-06`, `-07`, `-08`, `-12`:
the gold has one more firm, P's own labeler, than the distinct reading; for `-06` and `-08` the distinct reading
is the empty set). Most arms answered with the distinct reading and were scored wrong on these.

**What was done.** Nothing in the questions, gold or scoring changes: questions do not change after dev, and
the results are reported as pre-registered. The report adds one line, labelled post hoc and not part of any
verdict: P3 recomputed without these 4 questions.

**What this changes.** No verdict. On P3's 90 baseline questions per model the 4 questions move G-free minus
S-free by about one point.

### D1, the rerun selection rule (appended 2026-11-28, on review)

**The rule, applied mechanically to the test records written before the reruns:** rerun every record (`h2_1.run`
or `h2_1.error`) that was written after the crash (06:50:42.356 UTC) by a process that was running at the
crash, whatever its score, except the crashing run. A process is one CLI run of one configuration (its `run_id`,
on every line); it was running at the crash if it wrote records both before and after it. Nothing else is
rerun, and the rule looks at no score.

Six processes were running at the crash: Haiku S-tools in all four cells and Sonnet G-free in both of its
cells. The rule selects 1,219 records:

| Selected | Records | What they were |
|---|---|---|
| Haiku S-tools, `h2_1.error` | 1,083 | unscored: every one is a lost connection (1,067 "the connection is closed", 16 "server closed the connection") |
| Haiku S-tools, `h2_1.run` | 5 | in flight at the crash; finished without another store call; all 5 had scored correct |
| Sonnet G-free, `h2_1.run` | 131 | every call after the crash returned "the connection is lost"; 3 had still scored correct |

Checked against the records (`tests/test_h2_1_d1.py` recomputes all of this from `results/h2_1/` and fails on
any difference):

* Every one of the 1,219 was rerun, and nothing else was re-sampled: the questions with a new sample or a first
  scored run after the reruns are exactly the 1,083 + 131 above (the 5 in-flight runs replayed from the cache).
* The rule and the connection-error criterion agree: every run with a connection-loss error in a call was
  written after the crash by one of these processes, and in the Sonnet processes every run written after the
  crash has one (132, the crashing run included). No error record of these processes has another cause.
* The unscorable Haiku G-free question (`t10_fewest_steps_substance_to_firm-13`) came from a process that
  started after the crash; the rule does not select it and it was not rerun.

**What ran again that the rule did not select, and why it changes nothing.** The Haiku S-tools reruns used the
unchanged CLI, which runs a whole configuration. Its 172 questions scored before the crash, all correct and on
a live connection, were therefore executed again, but every response came from the response cache (no new
sample), and each replay has the same answer, the same score and the same calls as the original. So no run with
a correct score on a live connection received a new sample or a different record. The 5 in-flight S-tools runs
also replayed identically; the 3 Sonnet runs that had scored correct on a dead connection were re-sampled, as
the rule requires, and scored correct again.
