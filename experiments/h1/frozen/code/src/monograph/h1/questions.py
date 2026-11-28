"""Generate the H1 question set: 60 per type, 15 dev and 45 test, fixed seed, committed as one file.

A candidate is kept only when its template's reference SQL, run on the warehouse as the read-only agent role,
reproduces the gold computed over the canonical instances (`result_matches`). Types are filled round-robin
across their templates, so no template dominates a type. Every question is tagged inside or outside the
semantic layer's coverage by code (`h1_templates.inside`).
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from monograph.gen import semantic_layer as s
from monograph.h1.score import result_matches
from monograph.qa import h1_templates as t

QUESTIONS = Path("fixtures/h1/questions.json")
MAX_TRIES = 120  # candidates tried per template


def coverage(root: Path) -> frozenset[str]:
    from monograph import wiring

    mapping = json.loads((wiring.generated_dir(root) / s.MAPPING_FILE).read_text(encoding="utf-8"))
    return frozenset(mapping["coverage"])


def run_reference(conn: psycopg.Connection[Any], sql: str) -> tuple[list[str], list[list[Any]]]:
    with conn.transaction():
        cur = conn.execute(sql.encode("utf-8"))
        return [c.name for c in cur.description or []], [list(r) for r in cur.fetchall()]


def generate(
    data: t.H1Data,
    conn: psycopg.Connection[Any],
    cov: frozenset[str],
    log: Callable[[str], None] = print,
    seed: int = t.SEED,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for qtype in t.TYPES:
        templates = [x for x in t.TEMPLATES if x.type == qtype]
        pools: dict[str, list[dict[str, Any]]] = {}
        for tpl in templates:
            kept: list[dict[str, Any]] = []
            tried = 0
            texts: set[str] = set()
            for cand in tpl.generate(data, random.Random(f"{seed}:{tpl.id}")):
                if tried >= MAX_TRIES or len(kept) >= t.PER_TYPE:
                    break
                tried += 1
                columns, rows = run_reference(conn, cand.sql)
                if not result_matches(columns, rows, cand.gold):
                    raise RuntimeError(
                        f"{tpl.id} {cand.params}: the warehouse reference SQL does not reproduce the gold"
                    )
                text = rng.choice(tpl.phrasings).format(**cand.params)
                if text in texts:
                    continue
                texts.add(text)
                kept.append(
                    {
                        "type": qtype,
                        "template": tpl.id,
                        "text": text,
                        "params": cand.params,
                        "gold": cand.gold,
                        "coverage": "inside" if t.inside(tpl, cov) else "outside",
                        "needs": sorted(tpl.needs),
                        "codes": cand.codes,
                        "identity": sorted(tpl.identity),
                        "reference_sql": cand.sql,
                        **({"naive": cand.naive} if cand.naive is not None else {}),
                    }
                )
            pools[tpl.id] = kept
            log(f"{tpl.id}: {len(kept)} candidates (tried {tried})")
        chosen: list[dict[str, Any]] = []
        while len(chosen) < t.PER_TYPE and any(pools.values()):
            for tpl in templates:
                if pools[tpl.id] and len(chosen) < t.PER_TYPE:
                    chosen.append(pools[tpl.id].pop(0))
        if len(chosen) < t.PER_TYPE:
            raise RuntimeError(f"{qtype}: only {len(chosen)} questions, {t.PER_TYPE} needed")
        counters: dict[str, int] = {}
        for q in chosen:
            counters[q["template"]] = counters.get(q["template"], 0) + 1
            q["id"] = f"{q['template']}-{counters[q['template']]:02d}"
        order = list(range(len(chosen)))
        random.Random(f"{seed}:split:{qtype}").shuffle(order)
        dev = set(order[: t.DEV_PER_TYPE])
        for i, q in enumerate(chosen):
            q["split"] = "dev" if i in dev else "test"
        out += chosen
    return out


def write(root: Path, questions: list[dict[str, Any]]) -> Path:
    path = root / QUESTIONS
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"seed": t.SEED, "per_type": t.PER_TYPE, "dev_per_type": t.DEV_PER_TYPE, "questions": questions}
    path.write_text(json.dumps(body, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load(root: Path, split: str | None = None) -> list[dict[str, Any]]:
    data = json.loads((root / QUESTIONS).read_text(encoding="utf-8"))
    qs: list[dict[str, Any]] = data["questions"]
    return [q for q in qs if split is None or q["split"] == split]
