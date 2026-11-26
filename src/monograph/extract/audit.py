"""Audit of the allowed values sent in graph-context feedback: does each one have a source independent of the
label it was offered for? Reads the logged rounds, so it checks what was actually sent."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from monograph.extract.resolve import ReferenceGraph, normalize_name

_ALLOWED = re.compile(r"allowed: ([^\n]*)")
_MORE = re.compile(r" \(and \d+ more\)$")


def candidates_in(feedback: str, reference: ReferenceGraph) -> tuple[list[str], int]:
    """Allowed values listed in a feedback message, matched greedily against the graph's known names and
    NDCs (names may contain commas). Returns the values and the number of pieces that matched nothing."""
    out: list[str] = []
    unmatched = 0
    for segment in _ALLOWED.findall(feedback):
        parts = _MORE.sub("", segment).split(", ")
        i = 0
        while i < len(parts):
            for j in range(len(parts), i, -1):
                value = ", ".join(parts[i:j])
                if value in reference.ndc_links or normalize_name(value) in reference.name_links:
                    out.append(value)
                    i = j
                    break
            else:
                unmatched += 1
                i += 1
    return out, unmatched


def audit_feedback(
    rounds: Mapping[tuple[str, str, str], Iterable[Mapping[str, Any]]], reference: ReferenceGraph
) -> dict[str, dict[str, Any]]:
    """Per arm: allowed values offered, how many have no source independent of the label (must be 0 for A3),
    and pieces that matched nothing in the graph."""
    out: dict[str, dict[str, Any]] = {}
    for (model, arm, set_id), sequence in sorted(rounds.items()):
        entry = out.setdefault(f"{model} {arm}", {"offered": 0, "label_only": 0, "unmatched": 0, "examples": []})
        for r in sequence:
            values, unmatched = candidates_in(r["details"].get("feedback") or "", reference)
            entry["offered"] += len(values)
            entry["unmatched"] += unmatched
            for value in values:
                links = reference.ndc_links.get(value) or reference.name_links.get(normalize_name(value), set())
                if not links - {set_id}:
                    entry["label_only"] += 1
                    if len(entry["examples"]) < 5:
                        entry["examples"].append([set_id, value])
    return out
