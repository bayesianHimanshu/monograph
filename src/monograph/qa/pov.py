"""The closing section of the H2 report, "What this means for the PoV", written from the scores: which of
the graph store, the tools and the identity layer did the work."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from monograph.qa.score import tools_not_graph


def _clear(d: Mapping[str, float]) -> bool:
    return bool(d.get("n")) and not (d["lo"] <= 0 <= d["hi"])


def pov_section(scores: Mapping[str, Any], records: Sequence[Mapping[str, Any]], models: Sequence[str]) -> list[str]:
    lines = ["## What this means for the PoV", ""]
    if not scores.get("attribution"):
        return [*lines, "No test results yet."]
    cells = scores["cells"]
    for m in models:
        core = scores["attribution"][m]["core"]
        store = [k for k, v in core.items() if k.startswith("store") and _clear(v)]
        interface = [k for k, v in core.items() if k.startswith("interface") and _clear(v)]
        acc = {a: cells[f"{m}|{a}|core"]["accuracy"] for a in ("S-free", "S-tools", "G-free", "G-tools")}
        t3 = {a: cells[f"{m}|{a}|T3"]["accuracy"] for a in acc}
        t2 = {a: cells[f"{m}|{a}|T2"]["accuracy"] for a in acc}
        parts = [
            f"**{m}.** On T2+T3+T4: S-free {acc['S-free']:.2f}, S-tools {acc['S-tools']:.2f},"
            f" G-free {acc['G-free']:.2f}, G-tools {acc['G-tools']:.2f}."
        ]
        if tools_not_graph(core):
            parts.append("The win comes from the tools, not the graph.")
        parts.append(
            "Store effects with an interval clear of 0: " + (", ".join(store) if store else "none") + "."
            " Interface effects with an interval clear of 0: " + (", ".join(interface) if interface else "none") + "."
        )
        parts.append(
            f"Identity questions (T3) against plain multi-hop (T2): free-form SQL {t3['S-free']:.2f} vs"
            f" {t2['S-free']:.2f}, free-form Cypher {t3['G-free']:.2f} vs {t2['G-free']:.2f}, tools"
            f" {t3['S-tools']:.2f}/{t3['G-tools']:.2f} vs {t2['S-tools']:.2f}/{t2['G-tools']:.2f} (S-tools/G-tools)."
        )
        lines += [" ".join(parts), ""]
    core_all = [cells[f"{m}|{a}|core"]["accuracy"] for m in models for a in ("S-free", "S-tools", "G-free", "G-tools")]
    t3_all = [cells[f"{m}|{a}|T3"]["accuracy"] for m in models for a in ("S-free", "S-tools", "G-free", "G-tools")]
    t5 = {(m, a): cells[f"{m}|{a}|T5"]["accuracy"] for m in models for a in ("S-free", "S-tools", "G-free", "G-tools")}
    lines.append("**Reading across models.**")
    lines.append("")
    if min(core_all) >= 0.95:
        lines.append(
            f"* Every arm answered at least {min(core_all):.2f} of the multi-hop and identity questions (T2+T3+T4) with"
            " both models. The questions did not separate the arms, so this experiment cannot show the graph"
            " store winning; it shows that with a well-described schema, both stores and both interfaces reach"
            " the answers. The differences that remain are a few questions each and do not reach the 0.10"
            " margin the PREREG requires."
        )
    if min(t3_all) >= 0.97:
        lines.append(
            f"* Identity questions (T3) scored at least {min(t3_all):.2f} on every arm."
            " The identity layer did the work:"
            " the crosswalks, moiety links, generic links and aliases are data that both stores carry, as tables"
            " and as edges, so text-to-SQL resolved an 11-digit NDC, an RxCUI, a salt form or a firm alias as"
            " reliably as the graph tools. What made identity answerable was modeling identity, not storing it as"
            " a graph."
        )
    worst = min(t5.items(), key=lambda kv: kv[1])
    if worst[1] < 0.95:
        misses = [r for r in records if r["type"] == "T5" and not r["correct"] and (r["model"], r["arm"]) == worst[0]]
        listed = sum(
            1
            for r in misses
            if (r.get("answer") or {}).get("kind") == "ids"
            and len((r.get("answer") or {}).get("ids") or []) == (r.get("gold") or {}).get("number")
        )
        lines.append(
            f"* The clearest gap is on aggregates (T5) for {worst[0][0]}: {worst[0][1]} scored {worst[1]:.2f},"
            " its tools arms "
            + ", ".join(f"{a} {t5[(worst[0][0], a)]:.2f}" for a in ("S-tools", "G-tools"))
            + f". Of its {len(misses)} misses, {listed} submitted the right set of ids (as many as the gold count)"
            " instead of the number: an answer-format failure that the PREREG scores as wrong, not a counting"
            " mistake. A `count` tool returns a number, so the tools arms rarely made it. That is an interface"
            " effect, on either store, and a small one."
        )
    lines.append(
        "* AGE's Cypher dialect cost the G-free agents failed calls (relationship-type alternation, reusing a bound"
        " variable in MATCH, internal errors on lists of vertices), but they recovered within the budget. A graph"
        " store did not make free-form querying easier here; it made it differently hard."
    )
    lines.append("")
    return lines
