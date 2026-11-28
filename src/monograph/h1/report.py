"""`monograph report h1`: docs/h1/RESULTS.md from results/h1/*.jsonl and the pinned question set alone.

Every number comes from the run records; every sentence that interprets them is chosen by a fixed rule from
those numbers, printed next to them, so the text cannot drift from the data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from monograph.ablation.analysis import paired
from monograph.h1 import analysis as a
from monograph.h1.arms import ARMS
from monograph.h1.record import FAILURES
from monograph.h1.run import MODELS
from monograph.qa.h1_templates import CORE_TYPES, TYPES

EXAMPLES_PER_TYPE = 2
SQL_CHARS = 900


def _f(x: Any, digits: int = 2) -> str:
    return "n/a" if x is None else f"{float(x):.{digits}f}"


def _ci(m: Mapping[str, Any]) -> str:
    if not m or not m.get("n"):
        return "n/a"
    return f"{m['mean']:+.2f} [{m['ci_low']:+.2f}, {m['ci_high']:+.2f}]"


def _acc(m: Mapping[str, Any] | None) -> str:
    if not m or not m.get("n"):
        return ""
    return f"{m['mean']:.2f}"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return out + ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in rows]


def _verdict_lines(title: str, v: Mapping[str, Any]) -> list[str]:
    lines = [f"**{title}: H1 {v['verdict']}.** Types: {', '.join(v['types'])}.", ""]
    rows = []
    for c in v["comparisons"]:
        rows.append(
            [
                c["model"],
                _acc(c["ont_oc"]),
                _acc(c["sv"]),
                _ci(c["difference"]),
                f"{_ci(c['q1_difference'])} ({c['q1_model']})",
                "yes" if c["holds"] else "no",
                "yes" if c["sv_within"] else "no",
            ]
        )
    lines += _table(
        ["Model", "ONT+OC", "SV", "ONT+OC - SV (95% CI)", "Q1: ONT+OC - SV", "holds", "SV within 0.05 or ahead"], rows
    )
    return lines


def _example(r: Mapping[str, Any]) -> list[str]:
    queries = r.get("queries", [])
    blocked = [e for e in queries if e["blocked"]]
    outcome = "correct" if r["correct"] else f"wrong: {r['failure']}"
    lines = [f"*{r['model']} {r['arm']}, {r['question_id']}* ({outcome}):"]
    lines.append(f"> {r['question']}")
    lines.append("")
    if blocked:
        first = blocked[0]
        lines += ["Blocked query:", "```sql", first["query"][:SQL_CHARS], "```", "Feedback:"]
        for v in first["violations"]:
            lines.append(f"* [{v['rule']}] {v['message'][:400]}")
        after = next((e for e in queries if e["index"] > first["index"] and e["executed"]), None)
        if after is not None:
            lines += ["Repair (executed):", "```sql", after["query"][:SQL_CHARS], "```"]
        if first.get("gold_equivalent"):
            lines.append("The blocked query's shadow result already matched the gold.")
    else:
        ev = [e for e in queries if e["index"] in r.get("evidence_queries", [])]
        if ev:
            lines += ["Answer query:", "```sql", ev[-1]["query"][:SQL_CHARS], "```"]
            if ev[-1]["violations"]:
                lines.append("Shadow check: " + "; ".join(f"[{v['rule']}]" for v in ev[-1]["violations"]))
    lines.append("")
    return lines


def _examples(runs: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for qtype in CORE_TYPES:
        pool = [r for r in runs if r["type"] == qtype and r["arm"].endswith("+OC")]
        repaired = sorted(
            (r for r in pool if r.get("blocked_queries")),
            key=lambda r: (not r["correct"], r["arm"] != "ONT+OC", r["question_id"], r["model"]),
        )
        chosen = repaired[:1] + [r for r in repaired if not r["correct"]][:1]
        if len(chosen) < EXAMPLES_PER_TYPE:
            chosen += [r for r in repaired if r not in chosen][: EXAMPLES_PER_TYPE - len(chosen)]
        if len(chosen) < EXAMPLES_PER_TYPE:
            plain = sorted(pool, key=lambda r: (r["correct"], r["question_id"]))
            chosen += [r for r in plain if r not in chosen][: EXAMPLES_PER_TYPE - len(chosen)]
        lines += [f"### {qtype}", ""]
        if not chosen:
            lines += ["No OC-arm runs of this type.", ""]
        for r in chosen[:EXAMPLES_PER_TYPE]:
            lines += _example(r)
    return lines


def _pov(primary: Mapping[str, Any], effects: Sequence[Mapping[str, Any]], t: a.Table) -> list[str]:
    """Fixed rules from the numbers to the sentences."""

    def effect(model: str, arms: str) -> Mapping[str, Any]:
        return next((e["core"] for e in effects if e["model"] == model and e["arms"] == arms), {})

    def sig(m: Mapping[str, Any]) -> str:
        if not m or not m.get("n"):
            return "not measured"
        if m["ci_low"] > 0:
            return "helps"
        if m["ci_high"] < 0:
            return "hurts"
        return "makes no detectable difference"

    lines = []
    v = primary["verdict"]
    lines.append(
        {
            "holds": "* **An ontology with a check adds measurable accuracy beyond a well-built semantic layer** on"
            " this warehouse, for at least one model, without losing the semantic layer's home ground (Q1).",
            "fails": "* **On this warehouse a well-built semantic layer is enough:** ontology-checked text-to-SQL did"
            " not beat semantic views by more than 0.05 on the core questions for either model.",
            "partly holds": "* **The ontology with a check helps in part:** it beat the semantic layer somewhere, but"
            " not by the pre-registered margin with a clear interval, or it lost ground on Q1.",
        }[str(v)]
    )
    for model in MODELS:
        ctx, chk = effect(model, "ONT - SV"), effect(model, "ONT+OC - ONT")
        if ctx or chk:
            lines.append(
                f"* {model}: the ontology as context {sig(ctx)} ({_ci(ctx)} against SV); the check on top of the"
                f" ontology {sig(chk)} ({_ci(chk)} against ONT)."
            )
    for model in MODELS:
        inside = paired(
            t.of_types(model, "ONT+OC", CORE_TYPES, "inside"), t.of_types(model, "SV", CORE_TYPES, "inside")
        )
        outside = paired(
            t.of_types(model, "ONT+OC", CORE_TYPES, "outside"), t.of_types(model, "SV", CORE_TYPES, "outside")
        )
        if inside.get("n") or outside.get("n"):
            lines.append(
                f"* {model}: ONT+OC minus SV inside semantic coverage {_ci(inside)}, outside it {_ci(outside)};"
                " where the gap sits outside coverage, the value is the ontology modelling what the layer omits"
                " (crosswalks, labels, clinical drugs), not better reasoning."
            )
    floor = [e for e in effects if e["effect"] == "floor" and e["arms"].startswith("SV -")]
    for e in floor:
        lines.append(f"* {e['model']}: any meaning layer against the raw warehouse (SV - W-raw): {_ci(e['core'])}.")
    return lines


def render(records: Iterable[Mapping[str, Any]], questions_sha256: str, excluded: Sequence[str]) -> str:
    runs = a.latest_runs(records, "test", questions_sha256)
    t = a.Table.of(runs)
    primary = a.primary(t)
    secondary = a.secondary(t)
    sensitivity = a.primary(t, excluded) if excluded else None
    effects = a.attribution(t)
    lines = [
        "# H1 results",
        "",
        "Generated by `monograph report h1` from `results/h1/*.jsonl` alone (test split, the pinned question set;"
        " the latest record per model, arm and question). Decision rules: `docs/h1/PREREG.md`.",
        "",
        f"Non-informative types (ceiling rule, dev): {', '.join(excluded) if excluded else 'none'}.",
        "",
        "## Verdicts",
        "",
        *_verdict_lines("Primary (Q2 to Q5)", primary),
        "",
        *_verdict_lines("Inside coverage only (Q2 and Q5 inside semantic coverage)", secondary),
        "",
        *(
            [*_verdict_lines("Sensitivity, without the types marked non-informative", sensitivity), ""]
            if sensitivity is not None
            else []
        ),
        "## Accuracy, per type and model (2 x 2 plus the floor)",
        "",
    ]
    acc = a.accuracy(t)
    lines += _table(
        ["Model", "Arm", *TYPES, "Core (Q2-Q5)", "Inside coverage", "Outside coverage"],
        [
            [
                row["model"],
                row["arm"],
                *(_acc(row.get(q)) for q in TYPES),
                _acc(row["core"]),
                _acc(row["inside"]),
                _acc(row["outside"]),
            ]
            for row in acc
        ],
    )
    lines += ["", "## Context and check effects", ""]
    lines += _table(
        ["Model", "Effect", "Arms", "Core (95% CI)", *TYPES],
        [
            [e["model"], e["effect"], e["arms"], _ci(e["core"]), *(_ci(e["by_type"].get(q, {})) for q in TYPES)]
            for e in effects
        ],
    )
    lines += ["", "## Inside and outside semantic coverage", ""]
    lines += _table(
        ["Model", "Arm", "Inside", "Outside"],
        [
            [row["model"], row["arm"], _ci(row["inside"]).replace("+", ""), _ci(row["outside"]).replace("+", "")]
            for row in acc
        ],
    )
    lines += [
        "",
        "## The check itself",
        "",
        "Precision: of the questions whose evidence queries the check flags, the share answered wrongly. Recall: of"
        " the wrong answers, the share the check flags. In the non-OC arms the check ran in shadow. A false block"
        " is a blocked query whose shadow result already answered correctly, in a question that ended wrong.",
        "",
    ]
    lines += _table(
        [
            "Model",
            "Arm",
            "Questions",
            "Flagged",
            "Precision",
            "Recall",
            "Blocked queries",
            "Gold-equivalent blocks",
            "False blocks",
            "False-block rate",
            "Repair rounds",
        ],
        [
            [
                q["model"],
                q["arm"],
                q["questions"],
                q["flagged"],
                _f(q["precision"]),
                _f(q["recall"]),
                q["blocked_queries"],
                q["gold_equivalent_blocks"],
                q["false_blocks"],
                _f(q["false_block_rate"]),
                q["repair_rounds"],
            ]
            for q in a.check_quality(t)
        ],
    )
    lines += ["", "## Failure categories", ""]
    lines += _table(
        ["Model", "Arm", *FAILURES], [[f["model"], f["arm"], *(f[x] for x in FAILURES)] for f in a.failures(t)]
    )
    lines += ["", "## Cost and latency", ""]
    lines += _table(
        [
            "Model",
            "Arm",
            "Questions",
            "USD",
            "USD per correct",
            "Tool calls",
            "Input tokens",
            "Output tokens",
            "p50 s",
            "p95 s",
        ],
        [
            [
                c["model"],
                c["arm"],
                c["questions"],
                _f(c["usd"]),
                _f(c["usd_per_correct"], 4),
                _f(c["tool_calls"], 1),
                c["input_tokens"],
                c["output_tokens"],
                _f(c["latency_p50"], 1),
                _f(c["latency_p95"], 1),
            ]
            for c in a.costs(t)
        ],
    )
    lines += ["", "## Worked examples (OC arms: the SQL, the check's feedback, the repair)", ""]
    lines += _examples(runs)
    lines += ["## What this means for the PoV", ""]
    lines += _pov(primary, effects, t)
    lines.append("")
    return "\n".join(lines)


__all__ = ["ARMS", "render"]
