"""H2 scoring run and report. Everything is computed from results/h2/*.jsonl.

`select_runs` picks, per (model, arm, split, question), the latest `h2.run` line made with the arm's
current system prompt and tools (by hash), so a record from an earlier prompt version never counts.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from monograph.qa.score import (
    CORE_TYPES,
    FAILURES,
    TYPES,
    aggregate,
    attribution,
    tools_not_graph,
    verdict,
)

ARMS = ("S-free", "S-tools", "G-free", "G-tools")
GRAPH_ARMS = ("G-free", "G-tools")


def select_runs(
    lines: Iterable[Mapping[str, Any]], split: str, current: Mapping[str, tuple[str, str]]
) -> list[dict[str, Any]]:
    """current: arm -> (system prompt sha256, tools sha256)."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in lines:
        if line.get("metric") != "h2.run":
            continue
        d = line["details"]
        if d.get("split") != split or d["arm"] not in current:
            continue
        if (d.get("system_prompt_sha256"), d.get("tools_sha256")) != current[d["arm"]]:
            continue
        latest[(d["model"], d["arm"], d["question_id"])] = d
    return [latest[k] for k in sorted(latest)]


def score_lines(records: Sequence[Mapping[str, Any]], models: Sequence[str]) -> dict[str, Any]:
    cells = aggregate(records)
    out: dict[str, Any] = {"cells": {}, "attribution": {}, "verdict": None}
    for (m, a, g), c in sorted(cells.items()):
        out["cells"][f"{m}|{a}|{g}"] = c.summary()
    complete = all((m, a, "core") in cells and (m, a, "T1") in cells for m in models for a in ARMS)
    if complete:
        out["verdict"] = verdict(cells, models)
        for m in models:
            out["attribution"][m] = {g: attribution(cells, m, g) for g in ("core", "all", *TYPES)}
    return out


# ----------------------------------------------------------------------------------------- rendering


def _pct(x: float) -> str:
    return f"{x:.2f}"


def _ci(c: Mapping[str, Any]) -> str:
    return f"{_pct(c['accuracy'])} [{_pct(c['ci'][0])}, {_pct(c['ci'][1])}]"


def _diff(d: Mapping[str, float]) -> str:
    if not d.get("n"):
        return "n/a"
    return f"{d['mean']:+.2f} [{d['lo']:+.2f}, {d['hi']:+.2f}]"


AGE_PATTERNS = (
    ("shortestPath() not supported", re.compile(r"shortestPath|allShortestPaths", re.I)),
    ("relationship type alternation [:A|B] not supported", re.compile(r'syntax error at or near "\|"')),
    ("CALL { } subquery not supported", re.compile(r'syntax error at or near "\{"|CALL\s*\{', re.I)),
    ("column list does not match the RETURN items", re.compile(r"return row and column definition list", re.I)),
    ("function not supported", re.compile(r"function .* does not exist|unknown function", re.I)),
    (
        "a variable bound by UNWIND or WITH cannot be reused in a later MATCH pattern",
        re.compile(r"variable '\w+' already exists", re.I),
    ),
    (
        "internal error on a list of graph elements (unknown agtype container)",
        re.compile(r"unknown type of agtype container"),
    ),
    (
        "grouping: non-aggregated expressions must be explicit grouping keys",
        re.compile(r"must be either part of an explicitly listed key", re.I),
    ),
    ("timeout", re.compile(r"^error: timeout", re.I)),
)


def age_errors(records: Iterable[Mapping[str, Any]]) -> Counter[str]:
    """Cypher errors the G-free agents hit, grouped by AGE limitation (other errors by their first words)."""
    out: Counter[str] = Counter()
    for r in records:
        if r["arm"] != "G-free":
            continue
        for c in r.get("calls", []):
            if c["name"] != "run_cypher" or not c.get("is_error"):
                continue
            text = c.get("preview", "")
            label = next((name for name, rx in AGE_PATTERNS if rx.search(text)), None)
            if label is None:
                first = re.sub(r"\s+", " ", text.split("\n")[0])[:90]
                label = "other: " + re.sub(r'"[^"]*"|\'[^\']*\'|\b\d+\b', "#", first)
            out[label] += 1
    return out


def sql_errors(records: Iterable[Mapping[str, Any]]) -> Counter[str]:
    out: Counter[str] = Counter()
    for r in records:
        if r["arm"] != "S-free":
            continue
        for c in r.get("calls", []):
            if c["name"] == "run_sql" and c.get("is_error"):
                first = re.sub(r"\s+", " ", c.get("preview", "").split("\n")[0])[:90]
                out[re.sub(r'"[^"]*"|\'[^\']*\'|\b\d+\b', "#", first)] += 1
    return out


def _calls_text(r: Mapping[str, Any], limit: int = 6) -> list[str]:
    lines = []
    for c in r.get("calls", [])[:limit]:
        arg = c["input"].get("query") if isinstance(c["input"], dict) and "query" in c["input"] else c["input"]
        text = re.sub(r"\s+", " ", str(arg))
        status = " (error)" if c.get("is_error") else ""
        lines.append(f"    {c['index']}. {c['name']}{status}: {text[:400]}")
    if len(r.get("calls", [])) > limit:
        lines.append(f"    ... {len(r['calls']) - limit} more calls")
    return lines


def examples(
    records: Sequence[Mapping[str, Any]], questions: Mapping[str, Mapping[str, Any]], model: str
) -> dict[str, dict[str, tuple[str, list[Mapping[str, Any]]] | None]]:
    """Per core type: a question a graph arm won, one it lost, one every arm failed (first by id)."""
    by_q: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for r in records:
        if r["model"] == model:
            by_q[r["question_id"]][r["arm"]] = r
    out: dict[str, dict[str, tuple[str, list[Mapping[str, Any]]] | None]] = {}
    for qtype in CORE_TYPES:
        qs = sorted(q for q in by_q if questions[q]["type"] == qtype and len(by_q[q]) == len(ARMS))
        won = lost = failed = None
        for q in qs:
            arms = by_q[q]
            graph_ok = [a for a in GRAPH_ARMS if arms[a]["correct"]]
            rel_ok = [a for a in ("S-free", "S-tools") if arms[a]["correct"]]
            if won is None and graph_ok and "S-free" not in rel_ok:
                won = (q, [arms["G-tools" if "G-tools" in graph_ok else "G-free"], arms["S-free"]])
            if lost is None and not graph_ok and rel_ok:
                lost = (q, [arms[rel_ok[0]], arms["G-tools"], arms["G-free"]])
            if failed is None and not any(arms[a]["correct"] for a in ARMS):
                failed = (q, [arms[a] for a in ARMS])
        out[qtype] = {"graph arm won": won, "graph arm lost": lost, "all arms failed": failed}
    return out


def render(
    scores: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    questions: Mapping[str, Mapping[str, Any]],
    models: Sequence[str],
    meta: Mapping[str, Any],
) -> str:
    cells = scores["cells"]
    v = scores["verdict"]
    lines = ["# H2 results: graph tools win on multi-hop and identity questions", ""]
    lines += [
        "Generated by `monograph report h2` from `results/h2/*.jsonl`. Test split, scored once per model and arm"
        " (see the PREREG for the frozen prompts, tools, questions and rules).",
        "",
        "## Verdict against the PREREG",
        "",
    ]
    if v is None:
        lines += ["Not every model x arm cell has test results yet, so there is no verdict.", ""]
    else:
        lines += [f"**H2 {v['verdict']}.**", ""]
        lines += [
            "| Model | G-tools minus S-free on T2+T3+T4 | G-tools minus S-free on T1 | holds rule"
            " | within the fails margin |"
        ]
        lines += ["|---|---|---|---|---|"]
        for m in models:
            pm = v["per_model"][m]
            lines.append(
                f"| {m} | {_diff(pm['core_G-tools_minus_S-free'])} | {_diff(pm['t1_G-tools_minus_S-free'])} |"
                f" {'yes' if pm['holds'] else 'no'} | {'yes' if pm['within_fails_margin'] else 'no'} |"
            )
        lines += [
            "",
            "Rules: holds when, for at least one model, G-tools beats S-free by at least 0.10 on T2+T3+T4 with the"
            " interval above 0, and is no more than 0.05 below S-free on T1. Fails when S-free is within 0.05 of"
            " G-tools on T2+T3+T4 for both models; fails is checked first. Otherwise partly holds.",
            "",
        ]
    lines += ["## Accuracy, 2 x 2 per question type (exact match, 95% bootstrap interval)", ""]
    groups = [*TYPES, "core", "all"]
    for m in models:
        lines += [f"### {m}", "", "| Type | S-free | S-tools | G-free | G-tools | n |", "|---|---|---|---|---|---|"]
        for g in groups:
            row = [cells.get(f"{m}|{a}|{g}") for a in ARMS]
            if not all(row):
                continue
            label = {"core": "T2+T3+T4", "all": "all"}.get(g, g)
            lines.append(
                f"| {label} | " + " | ".join(_ci(c) for c in row if c) + f" | {row[0]['n'] if row[0] else 0} |"
            )
        lines += [
            "",
            "Set F1 (secondary):",
            "",
            "| Type | S-free | S-tools | G-free | G-tools |",
            "|---|---|---|---|---|",
        ]
        for g in groups:
            row = [cells.get(f"{m}|{a}|{g}") for a in ARMS]
            if all(row):
                label = {"core": "T2+T3+T4", "all": "all"}.get(g, g)
                lines.append(f"| {label} | " + " | ".join(_pct(c["f1"]) for c in row if c) + " |")
        lines.append("")
    lines += ["## Store and interface effects", ""]
    if scores["attribution"]:
        for m in models:
            attr = scores["attribution"][m]
            lines += [f"### {m}", "", "| Effect | T2+T3+T4 | T1 | T5 | T6 | all |", "|---|---|---|---|---|---|"]
            for key in attr["core"]:
                lines.append(
                    f"| {key} | {_diff(attr['core'][key])} | {_diff(attr['T1'][key])} | {_diff(attr['T5'][key])} |"
                    f" {_diff(attr['T6'][key])} | {_diff(attr['all'][key])} |"
                )
            if tools_not_graph(attr["core"]):
                lines += [
                    "",
                    f"**For {m}, the win comes from the tools, not the graph**: on T2+T3+T4 a store effect's"
                    " interval includes 0 while an interface effect's does not.",
                ]
            lines.append("")
    lines += ["## Failure categories (wrong answers, all types)", ""]
    for m in models:
        lines += [f"### {m}", "", "| Category | " + " | ".join(ARMS) + " |", "|---|" + "---|" * len(ARMS)]
        for f in FAILURES:
            row = [cells.get(f"{m}|{a}|all", {}).get("failures", {}).get(f, 0) for a in ARMS]
            lines.append(f"| {f} | " + " | ".join(str(x) for x in row) + " |")
        lines.append("")
    lines += [
        "## Cost and latency",
        "",
        "| Model | Arm | Tool calls / question | Input | Cache read | Cache write | Output | USD | USD per correct |"
        " p50 s | p95 s |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in models:
        for a in ARMS:
            c = cells.get(f"{m}|{a}|all")
            if not c:
                continue
            t = c["tokens"]
            per = f"{float(c['usd_per_correct']):.4f}" if c["usd_per_correct"] else "n/a"
            lines.append(
                f"| {m} | {a} | {c['tool_calls_mean']:.1f} | {t.get('input_tokens', 0)}"
                f" | {t.get('cache_read_tokens', 0)} | {t.get('cache_write_tokens', 0)} | {t.get('output_tokens', 0)}"
                f" | {float(c['cost_usd']):.2f} | {per} | {c['latency_p50']:.1f} | {c['latency_p95']:.1f} |"
            )
    lines += ["", f"API spend recorded for the test runs: USD {meta.get('test_spend', 0):.2f}.", ""]
    lines += ["## AGE Cypher limitations hit", ""]
    lines += [
        "Found while building and probing (AGE 1.8.0 on PostgreSQL 18.6): no `shortestPath()`/`allShortestPaths()`;"
        " no `CALL { }` subqueries; no relationship type alternation `[:A|B]`; `cypher()` needs a column"
        " definition list matching the RETURN items (run_cypher derives it); one agtype parameter map only;"
        " no temporal types (dates are ISO strings). The tool backends work around each: find_path expands one"
        " hop at a time in both stores, either-relation queries use UNION.",
        "",
        "Cypher errors the G-free agents hit on test, by cause:",
        "",
        "| Cause | Calls |",
        "|---|---|",
    ]
    for label, n in age_errors(records).most_common():
        lines.append(f"| {label} | {n} |")
    lines += ["", "For comparison, SQL errors the S-free agents hit:", "", "| Error | Calls |", "|---|---|"]
    for label, n in sql_errors(records).most_common(12):
        lines.append(f"| {label} | {n} |")
    lines += ["", "## Worked examples (core types)", ""]
    for m in models[:1]:
        for qtype, cases in examples(records, questions, m).items():
            lines += [f"### {qtype} ({m})", ""]
            for title, case in cases.items():
                if case is None:
                    lines += [f"*{title}*: no such question on test.", ""]
                    continue
                qid, runs = case
                q = questions[qid]
                gold = q["gold"]
                lines += [f"*{title}*: `{qid}`. {q['text']}", "", f"Gold: {_gold_text(gold)}.", ""]
                for r in runs:
                    verdict_text = "correct" if r["correct"] else f"wrong ({r.get('failure')})"
                    lines += [f"- {r['arm']}: {verdict_text}, answer {_answer_text(r.get('answer'))}", "", "  ```"]
                    lines += [*_calls_text(r), "  ```", ""]
    lines += meta.get("pov", [])
    return "\n".join(lines).rstrip() + "\n"


def _short(i: str) -> str:
    return i.split("/monograph/", 1)[-1]


def _gold_text(gold: Mapping[str, Any]) -> str:
    if gold["kind"] == "ids":
        return ", ".join(_short(i) for i in gold["ids"])
    if gold["kind"] == "number":
        return str(gold["number"])
    return "none"


def _answer_text(answer: Mapping[str, Any] | None) -> str:
    if answer is None:
        return "(none given)"
    if answer["kind"] == "ids":
        ids = answer.get("ids") or []
        shown = ", ".join(_short(str(i)) for i in ids[:6])
        return shown + (f" and {len(ids) - 6} more" if len(ids) > 6 else "") if ids else "(empty set)"
    if answer["kind"] == "number":
        return str(answer.get("number"))
    return "none"
