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


# docs/h2_1/DEVIATIONS.md, D2: test questions whose gold counts a product as sharing an ingredient with itself
# (the base prompt says "two products"). Reported post hoc only; no verdict uses this list.
D2_QUESTIONS = (
    "t9_labelers_of_ingredient_sharers-06",
    "t9_labelers_of_ingredient_sharers-07",
    "t9_labelers_of_ingredient_sharers-08",
    "t9_labelers_of_ingredient_sharers-12",
)


def _without(t: Table, qids: Sequence[str]) -> Table:
    skip = set(qids)
    return Table.of(r for rs in t.records.values() for r in rs if r["question_id"] not in skip)


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
        "Post hoc, not part of any verdict (D2): without the 4 T9 questions whose gold counts a product as sharing"
        " an ingredient with itself, G-free minus S-free is "
        + ", ".join(f"{m} {_diff(d)}" for m, d in analysis.p3(_without(t, D2_QUESTIONS), excluded)["models"].items())
        + ".",
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
            if not n:
                continue  # a type this model's test runs do not cover
            lines += [f"**{qtype}** (n = {n})", "", *_table(["Arm", *(c.name for c in cells)], rows), ""]
        rows = []
        for arm in arms:
            rows.append([arm, *(_f(_mean(t.of_types(model, c, arm, ("T11",), exact=True))) for c in cells)])
        lines += ["T11 exact match (secondary):", "", *_table(["Arm", *(c.name for c in cells)], rows), ""]
    return lines


def _mean(values: Mapping[str, float]) -> float | None:
    return statistics.fmean(values.values()) if values else None


IDENTITY_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "encoded in names or codes (salt forms, 11-digit NDCs)",
        ("t3_moiety_products", "t3_ndc11_labeler", "t4_recall_firms_by_moiety", "t4_recall_firms_by_ndc11"),
    ),
    (
        "recorded only in a crosswalk (RxCUI, brand to generic)",
        (
            "t3_rxcui_products",
            "t3_brand_generics",
            "t4_labelers_by_rxcui",
            "t4_recalls_of_generics",
        ),
    ),
    ("firm aliases", ("t3_alias_recalls", "t4_substances_recalled_by_alias")),
)


def _kind_scores(t: Table, cell: Cell) -> dict[str, dict[str, float | None]]:
    """Per identity kind and arm (Haiku): mean score over the kind's T3/T4 templates in one cell."""
    out: dict[str, dict[str, float | None]] = {}
    for kind, templates in IDENTITY_KINDS:
        row: dict[str, float | None] = {}
        for arm in ARMS:
            values = [
                analysis.score(r) for r in t.records.get((HAIKU, cell.name, arm), []) if r["template"] in templates
            ]
            row[arm] = statistics.fmean(values) if values else None
        out[kind] = row
    return out


def identity_kinds_table(t: Table) -> list[str]:
    on, off = _kind_scores(t, BASELINE), _kind_scores(t, ID_OFF_DESC_ON)
    rows = [[kind, *(f"{_f(on[kind][a])} / {_f(off[kind][a])}" for a in ARMS)] for kind, _ in IDENTITY_KINDS]
    return [
        "## What an agent can rebuild without the identity layer",
        "",
        f"{HAIKU}, descriptions on, T3+T4 by the kind of identity the question needs: score with identity on / off.",
        "",
        *_table(["Identity needed", *ARMS], rows),
        "",
    ]


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


CRASH = "server crash: the query killed the AGE backend (signal 11), and the calls after it found the connection closed"
_CRASH = re.compile(r"server closed the connection|connection is (closed|lost)|terminat\w* connection", re.I)


def _relabel(counts: Counter[str]) -> Counter[str]:
    out: Counter[str] = Counter()
    for k, v in counts.items():
        out[CRASH if _CRASH.search(k) else k] += v
    return out


def age_gaps(records: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "## AGE limitations hit",
        "",
        "Cypher errors the G-free agents hit on test, by AGE limitation (H2's classifier), per cell:",
        "",
    ]
    by_cell = {c.name: _relabel(age_errors([r for r in records if r["cell"] == c.name])) for c in CELLS}
    kinds = sorted({k for v in by_cell.values() for k in v}, key=lambda k: -sum(v[k] for v in by_cell.values()))
    lines += (
        _table(["Cause", *by_cell], [[k, *(by_cell[c][k] for c in by_cell)] for k in kinds]) if kinds else ["None."]
    )
    sql = sql_errors(records)
    lines += ["", "SQL errors the S-free agents hit, for comparison (all cells):", ""]
    lines += _table(["Error", "Calls"], sql.most_common(12)) if sql else ["None."]
    return [*lines, ""]


Pick = tuple[str, str, list[Mapping[str, Any]]]


def _pick(picks: list[Pick], labels: set[str], label: str, q: str, rs: list[Mapping[str, Any]]) -> None:
    """Keep at most two examples per type, with different labels and different questions."""
    if label not in labels and len(picks) < 2 and all(p[1] != q for p in picks):
        labels.add(label)
        picks.append((label, q, rs))


def _examples(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Two worked examples per hard type (Haiku): T3/T4, a question identity off broke and one answered without
    the layer; T9 to T11, a question where the graph free-form arm won or lost against SQL, or every arm failed."""
    by: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    types: dict[str, str] = {}
    for r in records:
        if r["model"] == HAIKU:
            by[(r["question_id"], r["cell"])][r["arm"]] = r
            types[r["question_id"]] = r["type"]
    lines = ["## Worked examples (hard types)", ""]
    for qtype in HARD_TYPES:
        qids = sorted(q for q, t in types.items() if t == qtype)
        picks: list[Pick] = []
        labels: set[str] = set()

        for q in qids:
            b = by.get((q, BASELINE.name), {})
            if qtype in ("T3", "T4"):
                off = by.get((q, ID_OFF_DESC_ON.name), {})
                arm = next((a for a in ARMS if a in b and a in off and b[a]["correct"] and not off[a]["correct"]), None)
                if arm:
                    _pick(picks, labels, "identity off broke it", q, [b[arm], off[arm]])
                arm = next((a for a in ARMS if a in off and off[a]["correct"]), None)
                if arm:
                    _pick(picks, labels, "answered without the identity layer", q, [off[arm]])
            else:
                if "G-free" in b and "S-free" in b and b["G-free"]["correct"] != b["S-free"]["correct"]:
                    label = "graph free-form won" if b["G-free"]["correct"] else "graph free-form lost"
                    _pick(picks, labels, label, q, [b["G-free"], b["S-free"]])
                if b and not any(x["correct"] for x in b.values()):
                    _pick(picks, labels, "every arm failed", q, list(b.values()))
            if len(picks) >= 2:
                break
        lines += [f"### {qtype}", ""]
        if not picks:
            lines += ["No contrasting question on test.", ""]
        for label, q, rs in picks:
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
    note = (
        f"Cost of the scored runs (the latest record per question; a replayed response is priced at its original"
        f" cost): USD {total:.2f}. API spend including the records the D1 reruns replaced is in"
        " `docs/h2_1/DEVIATIONS.md`."
    )
    return ["## Cost and latency", "", *_table(header, rows), "", note, ""]


# ------------------------------------------------------------------------------------------------- PoV


def _latency_ratio(records: Sequence[Mapping[str, Any]], tool: str) -> float | None:
    sql = [
        float(c["latency_s"])
        for r in records
        if r["type"] in DEEP_TYPES and r["arm"].startswith("S")
        for c in r.get("calls", [])
        if c["name"] == tool
    ]
    graph = [
        float(c["latency_s"])
        for r in records
        if r["type"] in DEEP_TYPES and r["arm"].startswith("G")
        for c in r.get("calls", [])
        if c["name"] == tool
    ]
    a, b = _pct(sql, 0.5), _pct(graph, 0.5)
    return b / a if a and b else None


def _clear(c: Mapping[str, float]) -> bool:
    return "ci_low" in c and (c["ci_low"] > 0 or c["ci_high"] < 0)


def _kinds_sentence(off: Mapping[str, Mapping[str, float | None]]) -> str:
    parts = []
    for kind, row in off.items():
        free = [x for a, x in row.items() if a.endswith("free") and x is not None]
        tools = [x for a, x in row.items() if a.endswith("tools") and x is not None]
        if free and tools:
            parts.append(f"{kind}: free-form {statistics.fmean(free):.2f}, tools {statistics.fmean(tools):.2f}")
    return "Without the layer, by the identity a question needs: " + "; ".join(parts) + "." if parts else ""


def pov(t: Table, v: Mapping[str, Any], records: Sequence[Mapping[str, Any]] = ()) -> list[str]:
    p1, p2, p3 = v["p1"], v["p2"], v["p3"]
    drops = {a: d.get("mean") for a, d in p1["arms"].items()}
    free_id = statistics.fmean([x for a, x in drops.items() if a.endswith("free") and x is not None] or [0.0])
    tools_id = statistics.fmean([x for a, x in drops.items() if a.endswith("tools") and x is not None] or [0.0])
    desc = {arm: d for arm, d in p2["drops"].items()}
    free_desc = statistics.fmean([d.get("mean", 0.0) for a, d in desc.items() if a.endswith("free")])
    tools_desc = statistics.fmean([d.get("mean", 0.0) for a, d in desc.items() if a.endswith("tools")])
    any_desc = any(_clear(d) for d in desc.values())
    store = {m: d for m, d in p3["models"].items()}

    def mean_of(arm: str, types: Sequence[str]) -> float | None:
        return _mean(t.of_types(HAIKU, BASELINE, arm, types))

    interface = {
        s: (mean_of(f"{s}-tools", analysis.PATH_TYPES) or 0.0) - (mean_of(f"{s}-free", analysis.PATH_TYPES) or 0.0)
        for s in ("S", "G")
    }
    identity_off = {arm: _mean(t.of_types(HAIKU, ID_OFF_DESC_ON, arm, analysis.IDENTITY_TYPES)) for arm in ARMS}
    ratios = {tool: _latency_ratio(records, tool) for tool in ("neighbors", "find_path", "recalls_for")}
    ratio_text = ", ".join(f"{k} {x:.0f}x" for k, x in ratios.items() if x is not None)
    effects = {
        "meaning in the data (the identity layer), on identity questions": statistics.fmean([free_id, tools_id]),
        "the interface (tools over free-form queries), on deep paths": statistics.fmean(interface.values()),
        "the store (graph over relational, free-form), on deep paths": statistics.fmean(
            [d["mean"] for d in store.values() if "mean" in d] or [0.0]
        ),
        "meaning in the schema (descriptions), free-form arms": free_desc,
    }
    ranked = sorted(effects.items(), key=lambda kv: -abs(kv[1]))
    store_text = ", ".join(f"{m} {_diff(d)}" for m, d in store.items()) or "not measured"
    identity_first = ranked[0][0].startswith("meaning in the data")
    rebuild = (
        "a free-form agent can partly rebuild identity from names and codes (pattern matching, padding an NDC),"
        " while a fixed tool set that resolves exactly cannot"
        if free_id < tools_id
        else "the tools arms held up at least as well as the free-form arms"
    )
    if p3["verdict"] == "holds":
        store_head = "The graph store helped free-form agents on deep paths."
    elif v["refined"] == "fails":
        store_head = "The graph store did not help free-form agents on deep paths."
    else:
        store_head = "The graph store's effect on deep paths is unclear."
    slower = [k for k, x in ratios.items() if x is not None and x > 1]
    speed_head = "The graph store was slower." if slower else "The graph store was not slower."
    desc_head = (
        "Descriptions did not matter here."
        if not any_desc and abs(p2["interaction"].get("mean", 0.0)) < analysis.P2_MARGIN / 2
        else "Descriptions moved some arms."
    )
    tools_won = min(interface.values()) > 0
    lines = [
        "## What this means for the PoV",
        "",
        f"* **{'Identity is the lever.' if identity_first else 'Identity.'}** Removing the identity layer cost"
        f" {_f(free_id)} of T3+T4 accuracy on average for free-form arms and {_f(tools_id)} for tools arms"
        f" ({HAIKU}, descriptions on; P1 {p1['verdict']}). Without the layer, free-form SQL and Cypher reached"
        f" {_f(identity_off['S-free'])} and {_f(identity_off['G-free'])} on those questions, the tools"
        f" {_f(identity_off['S-tools'])} and {_f(identity_off['G-tools'])}: {rebuild}. Identity is data both"
        " stores carry; how much an agent loses without it is what P1 measures. "
        + _kinds_sentence(_kind_scores(t, ID_OFF_DESC_ON)),
        f"* **{desc_head}** Turning descriptions off moved the free-form arms by {-free_desc:+.2f} and the tools"
        f" arms by {-tools_desc:+.2f} (identity on; interaction {_diff(p2['interaction'])}, P2 {p2['verdict']})"
        f"{'' if any_desc else '; no arm moved with an interval clear of 0'}.",
        f"* **{store_head}** G-free minus S-free on T9+T10 in the baseline: {store_text} (P3 {p3['verdict']};"
        f" refined H2 {v['refined']}). The AGE limitations table counts the failed Cypher calls behind it.",
        f"* **{speed_head}** Under the same tool service, Cypher's p50 execution time on T9 to T11 as a multiple of"
        f" SQL's: {ratio_text or 'n/a'}.",
        f"* **Tools {'beat' if tools_won else 'did not beat'} free-form queries on deep paths** ({HAIKU}, baseline,"
        f" T9+T10: tools minus free-form {interface['S']:+.2f} over SQL and {interface['G']:+.2f} over Cypher).",
        "",
        "**What made multi-hop and identity questions answerable, in order of measured effect:**",
        "",
    ]
    lines += [f"{i}. {name}: {value:+.2f}" for i, (name, value) in enumerate(ranked, 1)]
    gap = abs(ranked[0][1]) - abs(ranked[1][1]) if len(ranked) > 1 else 0.0
    lines += [
        "",
        "Effects are on the questions each lever is meant to move (identity on T3+T4, interface and store on"
        " T9+T10, descriptions on every type but T6), so the order compares how much each moved its own questions,"
        f" not one common scale. The first lever leads the second by {gap:.2f}.",
        "",
    ]
    return lines


def runs_section(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Scored questions per configuration (the latest record per question), against the pre-registered counts."""
    from monograph.ablation.run import configs, test_types

    expected = {"T1": 45, "T3": 45, "T4": 45, "T9": 45, "T10": 45, "T11": 45, "T6": 45}
    counts: Counter[tuple[str, str, str]] = Counter((r["model"], r["cell"], r["arm"]) for r in records)
    rows, short = [], []
    for c in configs():
        types = test_types(c.model) or tuple(expected)
        want = sum(expected[t] for t in types)
        got = counts.get((c.model, c.cell.name, c.arm), 0)
        rows.append([c.model, c.cell.name, c.arm, got, want])
        if got != want:
            short.append(f"{c.model} {c.cell.name} {c.arm} ({got} of {want})")
    return [
        "## Runs",
        "",
        "Scored test questions per configuration: the latest record per question (reruns of infrastructure"
        " failures replace the record they rerun; `docs/h2_1/DEVIATIONS.md`, D1).",
        "",
        *_table(["Model", "Cell", "Arm", "scored", "pre-registered"], rows),
        "",
        "Short of the pre-registered count: " + (", ".join(short) or "none") + ".",
        "",
    ]


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
        *runs_section(records),
        *factor_tables(t),
        *identity_kinds_table(t),
        *interaction_table(t),
        *backend_latency(records),
        *failure_table(records),
        *age_gaps(records),
        *_examples(records),
        *cost_table(records),
        *pov(t, verdicts, records),
    ]
    return "\n".join(lines).rstrip() + "\n"
