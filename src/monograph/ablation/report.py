"""`monograph report h2_1`: docs/h2_1/RESULTS.md from results/h2_1/*.jsonl alone.

Sections: the three predictions and the refined-H2 verdict; an identity x descriptions x arm table per type;
the P2 interaction data; backend latency on deep paths; failure categories; AGE gaps; worked examples per hard
type; cost and latency; what this means for the PoV. Every number is computed from the JSONL records.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from monograph.ablation import analysis
from monograph.ablation.analysis import ID_OFF_DESC_ON, ID_ON_DESC_OFF, Table
from monograph.ablation.cells import BASELINE, CELLS, HARDEST, Cell
from monograph.ablation.questions import TYPES
from monograph.ablation.run import CROSSWALK, HAIKU, MODELS
from monograph.agent.arms import ARMS
from monograph.qa.report import _answer_text, _calls_text, _gold_text, age_errors, sql_errors
from monograph.qa.score import FAILURES

HARD_TYPES = ("T3", "T4", "T9", "T10", "T11")
DEEP_TYPES = ("T9", "T10", "T11")
FAILURE_ORDER = (*FAILURES[:2], CROSSWALK, *FAILURES[2:])


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _ci(c: Mapping[str, float]) -> str:
    if "mean" not in c:
        return "n/a"
    return f"{c['mean']:.2f} [{c['ci_low']:.2f}, {c['ci_high']:.2f}]"


def _diff(c: Mapping[str, float]) -> str:
    if "mean" not in c:
        return "n/a"
    return f"{c['mean']:+.2f} [{c['ci_low']:+.2f}, {c['ci_high']:+.2f}]"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(x) for x in row) + " |" for row in rows]
    return out


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def _cells_of(model: str) -> list[Cell]:
    return list(CELLS) if model == HAIKU else [BASELINE, HARDEST]


def _arms_of(t: Table, model: str) -> list[str]:
    return [a for a in ARMS if any(t.has(model, c, a) for c in CELLS)]


# ------------------------------------------------------------------------------------------- sections


def predictions(t: Table, excluded: Sequence[str], gaps: Sequence[str] = ()) -> tuple[list[str], dict[str, Any]]:
    p1 = analysis.p1(t)
    p1_sensitive = analysis.p1(t, exclude=gaps)
    p2 = analysis.p2(t)
    p2_off = analysis.p2(t, identity=False)
    p3 = analysis.p3(t, excluded)
    verdict = analysis.refined(p3)
    lines = [
        "## Verdicts against the PREREG",
        "",
        f"**Refined H2 (the graph store helps free-form agents on deep and variable-length paths): {verdict}.**",
        "",
        f"**P1, identity effect: {p1['verdict']}.** {HAIKU}, descriptions on, combined T3+T4, identity on minus"
        " identity off (confirmed when the drop is at least 0.20 in every arm, refuted when under 0.10 in any):",
        "",
        *_table(
            ["Arm", "Drop [95% CI]", "n", "Drop without the sensitivity set", "n"],
            [
                [a, _diff(d), d.get("n", 0), _diff(p1_sensitive["arms"][a]), p1_sensitive["arms"][a].get("n", 0)]
                for a, d in p1["arms"].items()
            ],
        ),
        "",
        f"Pre-registered sensitivity check (moiety questions whose recorded links may understate the gold:"
        f" {', '.join(gaps) or 'none'}): verdict without them {p1_sensitive['verdict']}.",
        "",
        f"**P2, description effect: {p2['verdict']}.** {HAIKU}, identity on, every type but T6: interaction"
        f" (free-form drop minus tools drop) {_diff(p2['interaction'])} (holds at 0.10 or more). Identity off"
        f" (secondary): {_diff(p2_off['interaction'])}.",
        "",
        f"**P3, store effect on hard paths: {p3['verdict']}.** Baseline cell, G-free minus S-free on"
        f" {'+'.join(p3['types']) or 'no type'} (holds when at least 0.10 with the interval above 0, for at least"
        " one model):",
        "",
        *_table(
            ["Model", "G-free minus S-free [95% CI]", "n"],
            [[m, _diff(d), d.get("n", 0)] for m, d in p3["models"].items()],
        ),
        "",
    ]
    if excluded:
        lines += [f"Non-informative on dev (ceiling rule), excluded from P3: {', '.join(excluded)}.", ""]
    return lines, {"p1": p1, "p2": p2, "p2_off": p2_off, "p3": p3, "refined": verdict}


def factor_tables(t: Table) -> list[str]:
    lines = ["## Identity x descriptions x arm, per type", ""]
    lines += [
        "Score with 95% bootstrap interval: exact-match accuracy, except T11 (set F1; exact match in the"
        " second table). Columns are cells: identity on/off, descriptions on/off.",
        "",
    ]
    for model in MODELS:
        cells = _cells_of(model)
        arms = _arms_of(t, model)
        if not arms:
            continue
        lines += [f"### {model}", ""]
        for qtype in [*TYPES, "all"]:
            types = TYPES if qtype == "all" else (qtype,)
            rows = []
            for arm in arms:
                row = [arm]
                for cell in cells:
                    values = list(t.of_types(model, cell, arm, types).values())
                    row.append(_ci(analysis.mean_ci(values)) if values else "n/a")
                rows.append(row)
            n = len(t.of_types(model, BASELINE, arms[0], types))
            lines += [f"**{qtype}** (n = {n})", "", *_table(["Arm", *(c.name for c in cells)], rows), ""]
        rows = []
        for arm in arms:
            rows.append([arm, *(_f(_mean(t.of_types(model, c, arm, ("T11",), exact=True))) for c in cells)])
        lines += ["T11 exact match (secondary):", "", *_table(["Arm", *(c.name for c in cells)], rows), ""]
    return lines


def _mean(values: Mapping[str, float]) -> float | None:
    return statistics.fmean(values.values()) if values else None


def interaction_table(t: Table) -> list[str]:
    lines = [
        "## P2 interaction data",
        "",
        f"{HAIKU}, every type but T6: score with descriptions on and off, and the drop, per arm.",
        "",
    ]
    for identity, (on, off) in ((True, (BASELINE, ID_ON_DESC_OFF)), (False, (ID_OFF_DESC_ON, HARDEST))):
        rows = []
        for arm in ARMS:
            a = list(t.of_types(HAIKU, on, arm, analysis.ALL_BUT_T6).values())
            b = list(t.of_types(HAIKU, off, arm, analysis.ALL_BUT_T6).values())
            d = analysis.p2(t, identity=identity)["drops"][arm]
            kind = "free-form" if arm.endswith("free") else "tools"
            rows.append(
                [arm, kind, _f(statistics.fmean(a) if a else None), _f(statistics.fmean(b) if b else None), _diff(d)]
            )
        label = "identity on (the prediction)" if identity else "identity off (secondary)"
        lines += [
            f"**{label}**",
            "",
            *_table(["Arm", "Interface", "Descriptions on", "Descriptions off", "Drop [95% CI]"], rows),
            "",
        ]
    return lines


def backend_latency(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Per tool and backend, on T9 to T11: execution latency of each call (all models and cells)."""
    times: dict[tuple[str, str], list[float]] = defaultdict(list)
    base: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in records:
        if r["type"] not in DEEP_TYPES:
            continue
        backend = "SQL" if r["arm"].startswith("S") else "Cypher"
        for c in r.get("calls", []):
            if c["name"] == "describe_schema":
                continue
            times[(c["name"], backend)].append(float(c["latency_s"]))
            if r["cell"] == BASELINE.name:
                base[(c["name"], backend)].append(float(c["latency_s"]))
    tools = sorted({k[0] for k in times})
    rows = []
    for tool in tools:
        for backend in ("SQL", "Cypher"):
            v = times.get((tool, backend), [])
            if not v:
                continue
            rows.append([tool, backend, len(v), _ms(_pct(v, 0.5)), _ms(_pct(v, 0.95))])
    lines = [
        "## Backend execution latency on deep paths (T9 to T11)",
        "",
        "Wall time of each tool call's execution (the store query plus the shared service code), all models and"
        " cells; S-tools and G-tools run the same service over different backends, so their difference is the"
        " store.",
        "",
        *_table(["Tool", "Backend", "calls", "p50 ms", "p95 ms"], rows),
        "",
    ]
    ratios = []
    for tool in tools:
        s, g = times.get((tool, "SQL"), []), times.get((tool, "Cypher"), [])
        if s and g:
            ps, pg = _pct(s, 0.5), _pct(g, 0.5)
            if ps and pg:
                ratios.append(f"{tool} {pg / ps:.1f}x")
    if ratios:
        lines += ["Cypher p50 as a multiple of SQL p50: " + ", ".join(ratios) + ".", ""]
    return lines


def _ms(x: float | None) -> str:
    return "n/a" if x is None else f"{1000 * x:.0f}"


def failure_table(records: Sequence[Mapping[str, Any]]) -> list[str]:
    counts: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for r in records:
        if not r["correct"]:
            counts[(r["model"], r["cell"], r["arm"])][r["failure"]] += 1
    present = {(r["model"], r["cell"], r["arm"]) for r in records}
    lines = ["## Failure categories (wrong answers, first that applies)", ""]
    for model in MODELS:
        keys = [(model, c.name, a) for c in _cells_of(model) for a in ARMS if (model, c.name, a) in present]
        if not keys:
            continue
        header = ["Failure", *(f"{c} {a}" for _, c, a in keys)]
        rows = [[f, *(counts[k][f] for k in keys)] for f in FAILURE_ORDER]
        lines += [f"### {model}", "", *_table(header, rows), ""]
    return lines


def age_gaps(records: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "## AGE limitations hit",
        "",
        "Cypher errors the G-free agents hit on test, by AGE limitation (H2's classifier), per cell:",
        "",
    ]
    by_cell = {c.name: age_errors([r for r in records if r["cell"] == c.name]) for c in CELLS}
    kinds = sorted({k for v in by_cell.values() for k in v}, key=lambda k: -sum(v[k] for v in by_cell.values()))
    lines += (
        _table(["Cause", *by_cell], [[k, *(by_cell[c][k] for c in by_cell)] for k in kinds]) if kinds else ["None."]
    )
    sql = sql_errors(records)
    lines += ["", "SQL errors the S-free agents hit, for comparison (all cells):", ""]
    lines += _table(["Error", "Calls"], sql.most_common(12)) if sql else ["None."]
    return [*lines, ""]


def _examples(records: Sequence[Mapping[str, Any]]) -> list[str]:
    by: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for r in records:
        if r["model"] == HAIKU:
            by[(r["question_id"], r["cell"])][r["arm"]] = r
    lines = ["## Worked examples (hard types)", ""]
    for qtype in HARD_TYPES:
        qids = sorted(
            {
                q
                for q, _ in by
                if by[(q, BASELINE.name)] and next(iter(by[(q, BASELINE.name)].values()))["type"] == qtype
            }
        )
        picks: list[tuple[str, str, list[Mapping[str, Any]]]] = []
        for q in qids:
            b = by.get((q, BASELINE.name), {})
            if qtype in ("T3", "T4"):
                off = by.get((q, ID_OFF_DESC_ON.name), {})
                arm = next((a for a in ARMS if a in b and a in off and b[a]["correct"] and not off[a]["correct"]), None)
                if arm and not any(p[0] == "identity off broke it" for p in picks):
                    picks.append(("identity off broke it", q, [b[arm], off[arm]]))
                arm = next((a for a in ARMS if a in off and off[a]["correct"]), None)
                if arm and not any(p[0] == "answered without the identity layer" for p in picks):
                    picks.append(("answered without the identity layer", q, [off[arm]]))
            else:
                if "G-free" in b and "S-free" in b and b["G-free"]["correct"] != b["S-free"]["correct"]:
                    label = "graph free-form won" if b["G-free"]["correct"] else "graph free-form lost"
                    if not any(p[0] == label for p in picks):
                        picks.append((label, q, [b["G-free"], b["S-free"]]))
                if (
                    b
                    and not any(x["correct"] for x in b.values())
                    and not any(p[0] == "every arm failed" for p in picks)
                ):
                    picks.append(("every arm failed", q, list(b.values())))
            if len(picks) >= 2:
                break
        lines += [f"### {qtype}", ""]
        if not picks:
            lines += ["No contrasting question on test.", ""]
        for label, q, rs in picks[:2]:
            first = rs[0]
            lines += [f"*{label}*: `{q}`. {first['question']}", "", f"Gold: {_gold_text(first['gold'])}.", ""]
            for r in rs:
                verdict = "correct" if r["correct"] else f"wrong ({r['failure']})"
                lines += [f"- {r['cell']} {r['arm']}: {verdict}, answer {_answer_text(r.get('answer'))}", "", "  ```"]
                lines += [*_calls_text(r), "  ```", ""]
    return lines


def cost_table(records: Sequence[Mapping[str, Any]]) -> list[str]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in records:
        groups[(r["model"], r["cell"], r["arm"])].append(r)
    rows = []
    total = 0.0
    for (model, cell, arm), rs in sorted(
        groups.items(), key=lambda kv: (MODELS.index(kv[0][0]), kv[0][1], ARMS.index(kv[0][2]))
    ):
        usd = sum(float(r["cost_usd"]) for r in rs)
        total += usd
        correct = sum(1 for r in rs if r["correct"])
        lat = [float(r["latency_s"]) for r in rs]
        u = [r["usage"] for r in rs]
        rows.append(
            [
                model,
                cell,
                arm,
                f"{statistics.fmean(len(r['calls']) for r in rs):.1f}",
                sum(x["input_tokens"] for x in u),
                sum(x["cache_read_tokens"] for x in u),
                sum(x["cache_write_tokens"] for x in u),
                sum(x["output_tokens"] for x in u),
                f"{usd:.2f}",
                f"{usd / correct:.4f}" if correct else "n/a",
                _f(_pct(lat, 0.5)),
                _f(_pct(lat, 0.95)),
            ]
        )
    header = ["Model", "Cell", "Arm", "Tool calls / q", "Input", "Cache read", "Cache write", "Output", "USD"]
    header += ["USD per correct", "p50 s", "p95 s"]
    return [
        "## Cost and latency",
        "",
        *_table(header, rows),
        "",
        f"Spend recorded for the test runs: USD {total:.2f}.",
        "",
    ]


# ------------------------------------------------------------------------------------------------- PoV


def pov(t: Table, v: Mapping[str, Any]) -> list[str]:
    p1, p2, p3 = v["p1"], v["p2"], v["p3"]
    identity_drop = statistics.fmean([d["mean"] for d in p1["arms"].values() if "mean" in d]) if p1["arms"] else None
    desc = {arm: d.get("mean") for arm, d in p2["drops"].items()}
    free_desc = statistics.fmean([x for a, x in desc.items() if a.endswith("free") and x is not None] or [0.0])
    tools_desc = statistics.fmean([x for a, x in desc.items() if a.endswith("tools") and x is not None] or [0.0])
    store = {m: d.get("mean") for m, d in p3["models"].items()}

    # interface effect, baseline, Haiku, hard paths: tools minus free-form, per store
    def mean_of(arm: str, cell: Cell, types: Sequence[str]) -> float | None:
        return _mean(t.of_types(HAIKU, cell, arm, types))

    interface = {
        store_: (mean_of(f"{store_}-tools", BASELINE, analysis.PATH_TYPES) or 0.0)
        - (mean_of(f"{store_}-free", BASELINE, analysis.PATH_TYPES) or 0.0)
        for store_ in ("S", "G")
    }
    effects = {
        "meaning (the identity layer), on identity questions": identity_drop or 0.0,
        "meaning (schema descriptions), free-form arms": free_desc,
        "tools over free-form queries, on deep paths": statistics.fmean(interface.values()),
        "the graph store, free-form, on deep paths": statistics.fmean(
            [x for x in store.values() if x is not None] or [0.0]
        ),
    }
    ranked = sorted(effects.items(), key=lambda kv: -abs(kv[1]))
    lines = [
        "## What this means for the PoV",
        "",
        f"* **Identity.** Turning the identity layer off changed combined T3+T4 accuracy by"
        f" {', '.join(f'{a} {-d.get("mean", 0):+.2f}' for a, d in p1['arms'].items())} ({HAIKU}, descriptions on;"
        f" P1 {p1['verdict']}). The layer is data both stores carry; what an agent does without it is reconstruct"
        " identity from names and codes, and the drop measures how often it cannot.",
        f"* **Descriptions.** Turning schema descriptions off moved free-form arms by {-free_desc:+.2f} and tools arms"
        f" by {-tools_desc:+.2f} on average (identity on; P2 {p2['verdict']}). Tools carry their meaning in their"
        " definitions, which no cell removes.",
        f"* **Store.** On deep and variable-length paths in the baseline cell, G-free minus S-free was"
        f" {', '.join(f'{m} {x:+.2f}' for m, x in store.items() if x is not None) or 'not measured'} (P3"
        f" {p3['verdict']}; refined H2 {v['refined']}).",
        f"* **Interface.** On the same questions ({HAIKU}, baseline), tools minus free-form was"
        f" {interface['S']:+.2f} over SQL and {interface['G']:+.2f} over Cypher.",
        "",
        "**What made multi-hop and identity questions answerable, in order of measured effect size:**",
        "",
    ]
    lines += [f"{i}. {name}: {value:+.2f}" for i, (name, value) in enumerate(ranked, 1)]
    lines += [
        "",
        "Effects are on different question sets (identity on T3+T4, store and interface on T9+T10, descriptions"
        " on every type but T6), so the order compares how much each lever moved the questions it is meant to"
        " move, not one common scale.",
        "",
    ]
    return lines


# ------------------------------------------------------------------------------------------------ render


def render(records: Sequence[Mapping[str, Any]], dev_records: Sequence[Mapping[str, Any]], prereg_text: str) -> str:
    t = Table.of(records)
    dev = Table.of(dev_records)
    excluded = [q for q in analysis.non_informative(dev) if q in analysis.PATH_TYPES]
    marked = re.search(r"Excluded from P3: ([^.]*)\.", prereg_text)
    if marked:
        listed = [x.strip() for x in marked.group(1).split(",") if x.strip() and x.strip() != "none"]
        excluded = listed
    gaps_line = re.search(r"Sensitivity set: ([^\n]*)\.", prereg_text)
    gaps = re.findall(r"`([^`]+)`", gaps_line.group(1)) if gaps_line else []
    head, verdicts = predictions(t, excluded, gaps)
    lines = [
        "# H2.1 results: identity and description ablations of the H2 arms",
        "",
        "Generated by `monograph report h2_1` from `results/h2_1/*.jsonl`. Test split, each question run once per"
        " configuration (see `docs/h2_1/PREREG.md` for the frozen prompts, tools, questions and rules).",
        "",
        *head,
        *factor_tables(t),
        *interaction_table(t),
        *backend_latency(records),
        *failure_table(records),
        *age_gaps(records),
        *_examples(records),
        *cost_table(records),
        *pov(t, verdicts),
    ]
    return "\n".join(lines).rstrip() + "\n"
