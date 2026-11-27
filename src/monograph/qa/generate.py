"""Generate the H2 question set: 50 questions per type over its templates, gold by code, 10 dev and 40 test
per type (stratified by template), fixed seed.

A candidate is kept only if the template's tool plan, run on the SQL store, returns exactly the gold with
no truncated result. Every kept question is then re-checked on the Cypher store; any mismatch is reported
(and must be zero, since the backends are proven equivalent).
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from monograph.qa.index import Index
from monograph.qa.templates import TEMPLATES, Candidate, GenerationReport, Template, Truncated
from monograph.tools.registry import ToolError
from monograph.tools.service import ToolService

PER_TYPE = 50
DEV_PER_TYPE = 10
MAX_TRIES = 400  # candidates examined per template before giving up


def largest_remainder(total: int, weights: Sequence[int]) -> list[int]:
    whole = sum(weights)
    raw = [total * w / whole for w in weights]
    out = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: (-(raw[i] - out[i]), i))[: total - sum(out)]:
        out[i] += 1
    return out


def _normalized(gold: dict[str, Any]) -> dict[str, Any]:
    return {**gold, "ids": sorted(gold["ids"])} if "ids" in gold else gold


def _check(c: Candidate, service: ToolService) -> str:
    try:
        got = c.plan(service)
    except Truncated:
        return "truncated"
    except ToolError:
        return "mismatch"
    return "ok" if _normalized(got) == _normalized(c.gold) else "mismatch"


def generate(
    ix: Index, service: ToolService, seed: int, log: Callable[[str], None] = print
) -> tuple[list[dict[str, Any]], GenerationReport]:
    report = GenerationReport()
    by_type: dict[str, list[Template]] = defaultdict(list)
    for t in TEMPLATES:
        by_type[t.type].append(t)
    questions: list[dict[str, Any]] = []
    for qtype in sorted(by_type):
        templates = by_type[qtype]
        quotas = largest_remainder(PER_TYPE, [1] * len(templates))
        dev = largest_remainder(DEV_PER_TYPE, quotas)
        for template, quota, n_dev in zip(templates, quotas, dev, strict=True):
            rng = random.Random(f"{seed}:{template.id}")
            kept: list[Candidate] = []
            seen: set[tuple[str, ...]] = set()
            tried = 0
            for c in template.candidates(ix, rng):
                if len(kept) >= quota or tried >= MAX_TRIES:
                    break
                key = tuple(c.anchors) or tuple(sorted(c.params.values()))
                if key in seen:
                    continue
                seen.add(key)
                tried += 1
                verdict = _check(c, service)
                if verdict == "ok":
                    kept.append(c)
                elif verdict == "truncated":
                    report.rejected_truncated[template.id] = report.rejected_truncated.get(template.id, 0) + 1
                else:
                    report.rejected_mismatch[template.id] = report.rejected_mismatch.get(template.id, 0) + 1
            report.tried[template.id] = tried
            report.kept[template.id] = len(kept)
            log(f"{template.id}: kept {len(kept)}/{quota} after {tried} candidates")
            order = list(range(len(kept)))
            random.Random(f"{seed}:{template.id}:split").shuffle(order)
            dev_set = set(order[:n_dev])
            for k, c in enumerate(kept):
                text = template.phrasings[k % len(template.phrasings)].format(**c.params)
                questions.append(
                    {
                        "id": f"{template.id}-{k:02d}",
                        "type": qtype,
                        "template": template.id,
                        "phrasing": k % len(template.phrasings),
                        "text": text,
                        "params": c.params,
                        "gold": _normalized(c.gold),
                        "anchors": c.anchors,
                        "split": "dev" if k in dev_set else "test",
                    }
                )
    return questions, report


def recheck(ix: Index, questions: Sequence[dict[str, Any]], service: ToolService, seed: int) -> list[str]:
    """Rebuild each kept candidate and run its plan on another store; returns the ids that disagree."""
    by_id = {q["id"]: q for q in questions}
    bad = []
    for template in TEMPLATES:
        rng = random.Random(f"{seed}:{template.id}")
        wanted = {q["params"].__repr__(): q for q in by_id.values() if q["template"] == template.id}
        tried = 0
        for c in template.candidates(ix, rng):
            if not wanted or tried >= MAX_TRIES * 2:
                break
            tried += 1
            q = wanted.pop(c.params.__repr__(), None)
            if q is None:
                continue
            if _check(c, service) != "ok":
                bad.append(q["id"])
        bad += [q["id"] for q in wanted.values()]
    return sorted(bad)
