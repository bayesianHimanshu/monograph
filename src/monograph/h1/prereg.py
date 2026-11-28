"""`monograph h1 prereg`: pin every artifact a run reads and write docs/h1/PREREG.md, before any test run.

The PREREG is rendered from the code's own constants (arms, settings, decision thresholds), the committed
question set and the dev results, so what it says is what the runs and the analysis do.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from monograph.gen import semantic_layer as s
from monograph.gen import warehouse as w
from monograph.h1 import analysis, arms, pins
from monograph.h1.record import FAILURES
from monograph.h1.run import HAIKU, MODELS, SONNET, SONNET_ARMS, SONNET_TYPES, configs, types_for
from monograph.h1.score import ABS_TOLERANCE, REL_TOLERANCE
from monograph.qa.h1_templates import DEV_PER_TYPE, PER_TYPE, SEED, TEMPLATES, TYPES
from monograph.sqlcheck.gate import MAX_EXEC_ERRORS, MAX_ROUNDS


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def estimate(dev: Sequence[Mapping[str, Any]], test: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Test cost from dev: per configuration and type, mean dev USD per question times the test questions."""
    per: dict[tuple[str, str, str], list[float]] = {}
    for r in dev:
        per.setdefault((r["model"], r["arm"], r["type"]), []).append(float(r["cost_usd"]))
    n_test = Counter(q["type"] for q in test)
    rows = []
    total = 0.0
    for c in configs():
        covered = types_for(c.model)
        usd = 0.0
        for qtype in TYPES:
            if covered is not None and qtype not in covered:
                continue
            costs = per.get((c.model, c.arm, qtype))
            if costs:
                usd += sum(costs) / len(costs) * n_test[qtype]
        rows.append({"model": c.model, "arm": c.arm, "usd": usd})
        total += usd
    return {"rows": rows, "total": total}


def render(root: Path, dev: Sequence[Mapping[str, Any]], dev_spend: float, hashes: Mapping[str, str]) -> str:
    t = analysis.Table.of(dev)
    qs = pins.questions(root)
    marked = analysis.non_informative(t)
    by_type = Counter((q["type"], q["split"]) for q in qs)
    cov = Counter((q["type"], q["coverage"]) for q in qs if q["split"] == "test")
    lines = [
        "# H1 pre-registration",
        "",
        "Committed before the first test run. `monograph h1 run --split test` refuses to start from a dirty tree,"
        " if any file in the frozen table below no longer matches its sha256, if code that executes no longer"
        " equals its pinned copy, if a generated artifact no longer equals its pin, if the database it would query"
        " was not built from the pinned artifacts (build stamp), or without the approved spend cap in"
        " `experiments/h1/budget.json`. Every file the agents are sent, and every mapping the check reads, comes"
        " from these pins. Any change after this commit is a logged deviation in `docs/h1/DEVIATIONS.md`.",
        "",
        "## Hypothesis",
        "",
        "**H1: ontology-checked text-to-SQL beats semantic views alone.** On a deliberately realistic warehouse"
        " whose schema does not carry the meaning, the model is given either a well-built semantic layer or the"
        " ontology with its mapping to the warehouse, and its SQL is either run as written (with execution-error"
        " retry) or checked against the ontology first and repaired on violations. The design, the warehouse, the"
        " semantic layer and its coverage rule, the four checks and the metrics are in `docs/h1/PLAN.md` (committed"
        " before any code or question existed); this file fixes what the test runs and how they are judged.",
        "",
        "## Arms",
        "",
        *_table(
            ["Arm", "Context (describe_data)", "Database (schemas)", "Check"],
            [
                ["W-raw", "the warehouse DDL only", "monograph_h1_wh (wh)", "shadow"],
                ["SV", "semantic model, views, raw tables listed", "monograph_h1_sv (wh, sem)", "shadow"],
                ["SV+OC", "as SV", "monograph_h1_sv (wh, sem)", "enforce"],
                ["ONT", "ontology and its mapping to the warehouse", "monograph_h1_wh (wh)", "shadow"],
                ["ONT+OC", "as ONT", "monograph_h1_wh (wh)", "enforce"],
            ],
        ),
        "",
        f"Every arm: the same base prompt (`h1_base_{arms.PROMPT_VERSIONS['base']}.md`), the same tools"
        " (describe_data, run_sql, submit_answer), the same answer schema, the same settings and the same"
        f" execution-error retry (a failed execution is returned; after {MAX_EXEC_ERRORS} failed executions,"
        " the first plus 3 retries, run_sql executes nothing more). Enforce: a query with violations is not"
        f" executed and the feedback is returned, at most {MAX_ROUNDS} repair rounds per question, after which"
        " it runs as written with the violations attached. Shadow: every query is checked, nothing is sent.",
        "",
        "## Models and settings",
        "",
        f"* `{HAIKU}`: all five arms, every type.",
        f"* `{SONNET}`: {', '.join(SONNET_ARMS)}, on {', '.join(SONNET_TYPES)}.",
        "",
        *_table(["Setting", "Value"], [[k, v] for k, v in arms.SETTINGS.as_dict().items()]),
        "",
        "## Questions",
        "",
        f"{PER_TYPE} per type, {DEV_PER_TYPE} dev and {PER_TYPE - DEV_PER_TYPE} test, seed {SEED},"
        f" {len(TEMPLATES)} templates with three hand-written phrasings each; gold by code over the canonical"
        " instances; every question's reference SQL on the warehouse reproduces its gold.",
        "",
        *_table(
            ["Type", "Dev", "Test", "Test inside coverage", "Test outside coverage"],
            [[q, by_type[(q, "dev")], by_type[(q, "test")], cov[(q, "inside")], cov[(q, "outside")]] for q in TYPES],
        ),
        "",
        "## Scoring",
        "",
        "* values: the answered set equals the gold set (business keys; firm and substance names accepted;"
        " case and surrounding space ignored; order and duplicates do not matter);",
        f"* number: |answer - gold| <= max({ABS_TOLERANCE}, {REL_TOLERANCE} x |gold|);",
        "* none: `none` or an empty values list.",
        "",
        "Failure categories, the first that applies: " + "; ".join(FAILURES) + " (definitions in PLAN.md).",
        "",
        "## Decision rules",
        "",
        f"* **Primary.** H1 **holds** if, for at least one model, ONT+OC beats SV by at least {analysis.HOLDS_MARGIN}"
        " on Q2 to Q5 combined (less any non-informative type), with the 95% paired bootstrap interval excluding"
        f" 0, and ONT+OC is no more than {analysis.Q1_TOLERANCE} below SV on Q1 (point estimate). Sonnet has no Q1"
        " runs, so for Sonnet the Q1 condition uses Haiku's Q1 result. H1 **fails** if, for both models, ONT+OC"
        f" beats SV by at most {analysis.FAILS_WITHIN} on Q2 to Q5 (SV within {analysis.FAILS_WITHIN} of ONT+OC, or"
        " ahead of it); fails takes precedence. Otherwise it **partly holds**.",
        "* **Secondary, inside coverage only.** The same rule on the Q2 and Q5 test questions inside semantic"
        " coverage.",
        "* **Attribution**, always reported: context effect (ONT - SV, ONT+OC - SV+OC), check effect (SV+OC - SV,"
        " ONT+OC - ONT), floor (each arm - W-raw), per model where both arms ran.",
        f"* **Ceiling rule:** a type at {analysis.CEILING} or higher in every arm (every configuration that ran it)"
        " on dev is non-informative and excluded from the combined core.",
        "",
        "### Ceiling marks (dev)",
        "",
    ]
    ceil = analysis.ceiling(t)
    cols = [f"{m} {a}" for m in MODELS for a in arms.ARMS if t.has(m, a)]
    lines += _table(
        ["Type", *cols, "non-informative"],
        [[q, *(_f(ceil[q].get(c)) for c in cols), "yes" if q in marked else "no"] for q in TYPES],
    )
    lines += [
        "",
        f"Non-informative types: {', '.join(marked) if marked else 'none'}.",
        "",
        "## Dev runs",
        "",
    ]
    rows = []
    for (model, arm), rs in sorted(t.records.items()):
        versions = ", ".join(sorted({f"{k} {v}" for r in rs for k, v in r["prompt_versions"].items()}))
        rows.append([model, arm, versions, len(rs), _f(sum(r["correct"] for r in rs) / len(rs))])
    lines += _table(["Model", "Arm", "Prompt versions", "Dev questions", "Dev accuracy"], rows)
    lines += [
        "",
        f"Dev spend recorded: USD {dev_spend:.2f}. The test estimate is printed by `monograph h1 estimate` after"
        " this commit; the test runs wait for the approved cap.",
        "",
        "## Known limitations",
        "",
        "* The data is the H2.1 baseline cell's (a targeted, connected frame, not a random sample of FDA data);"
        " the warehouse is generated from it, so its legacy traits are designed, not found.",
        "* The semantic layer is written by the same author as the ontology and the check; it is built to the"
        " coverage rule fixed in PLAN.md before any question existed, with rich descriptions, synonyms, measures"
        " and joins, but it is one layer, not a sample of deployed ones.",
        "* The check runs on the model's SQL only. Its rule 3 (fan-out) is a heuristic and knowingly flags some"
        " correct queries (a deliberate count of reaction mentions; a name join); the false-block rate measures"
        " the cost.",
        "* Each test question runs once per configuration (one sample).",
        "* Adverse event reports are concentrated in 2014, so report questions use months of 2014.",
        "* A count of zero is never gold; questions naming nothing that exists are Q6 and must answer none.",
        "",
        "## Frozen files",
        "",
        *_table(["File", "sha256"], [[f"`{k}`", f"`{v}`"] for k, v in sorted(hashes.items())]),
        "",
        f"Warehouse mapping version {w.WAREHOUSE_VERSION}; semantic layer version {s.SEMANTIC_VERSION}.",
        "",
    ]
    return "\n".join(lines)
