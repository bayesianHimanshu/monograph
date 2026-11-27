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
    return lines
