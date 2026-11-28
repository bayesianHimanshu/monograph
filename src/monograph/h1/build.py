"""Build the H1 database: the canonical tables, the warehouse loaded from them, the round trip, the semantic
layer and the agents' read-only roles.

One database (`monograph_h1` by default) holds:

* schema `h2`: the canonical tables, loaded from the validated instances exactly as H2 and H2.1 load them;
* schema `wh`: the warehouse, created and loaded by the generated DDL and load SQL;
* schemas `h1_eq_wh` and `h1_eq_h2`: the round-trip views; the build aborts if they differ in any row;
* schema `sem`: the semantic layer's views over the warehouse (stage 2).

The agents' roles read only what their arm is given (docs/h1/PLAN.md), never `h2` or the round-trip views.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel

from monograph import wiring
from monograph.gen import warehouse as w

DATABASE = "monograph_h1"


@dataclass(frozen=True, slots=True)
class BuildReport:
    canonical_rows: dict[str, int]
    warehouse_rows: dict[str, int]
    round_trip: dict[str, int]  # canonical table -> rows compared


def generated(root: Path, name: str) -> str:
    return (wiring.generated_dir(root) / name).read_text(encoding="utf-8")


def round_trip_differences(
    conn: psycopg.Connection[Any], views: Sequence[w.EqView]
) -> tuple[dict[str, int], list[str]]:
    """(rows compared per table, differences). Compared as multisets, both ways."""
    rows: dict[str, int] = {}
    problems: list[str] = []
    for v in views:
        a, b = sql.Identifier(w.EQ_WH, v.name), sql.Identifier(w.EQ_H2, v.name)
        q = sql.SQL("SELECT count(*) FROM (SELECT * FROM {} EXCEPT ALL SELECT * FROM {}) x")
        extra = _one(conn, q.format(a, b))
        missing = _one(conn, q.format(b, a))
        rows[v.name] = _one(conn, sql.SQL("SELECT count(*) FROM {}").format(b))
        if extra or missing:
            problems.append(f"{v.name}: {extra} rows only in the warehouse, {missing} only in the canonical tables")
    return rows, problems


def _one(conn: psycopg.Connection[Any], query: sql.Composed) -> int:
    row = conn.execute(query).fetchone()
    return int(row[0]) if row else 0


def build_warehouse(conn: psycopg.Connection[Any], root: Path, log: Callable[[str], None]) -> BuildReport:
    """Load `wh` from `h2` (already loaded) and prove the round trip. Raises on any difference."""
    for schema in (w.SCHEMA, w.EQ_WH, w.EQ_H2):
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.execute(generated(root, w.DDL_FILE).encode("utf-8"))
    conn.execute(generated(root, w.LOAD_FILE).encode("utf-8"))
    conn.execute(generated(root, w.INVERSE_FILE).encode("utf-8"))
    spec = wiring.build_spec()
    wh = w.analyze(spec)
    counts = {
        t.name: _one(conn, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(w.SCHEMA, t.name.lower())))
        for t in wh.tables
    }
    rows, problems = round_trip_differences(conn, w.eq_views(wh))
    if problems:
        raise RuntimeError("the warehouse does not hold the canonical data: " + "; ".join(problems))
    log(f"warehouse: {sum(counts.values())} rows in {len(counts)} tables; round trip exact on {len(rows)} tables")
    canonical = {name: n for name, n in rows.items()}
    return BuildReport(canonical, counts, rows)


def load_canonical(
    conn: psycopg.Connection[Any], instances: Mapping[str, Sequence[BaseModel]], root: Path, log: Callable[[str], None]
) -> dict[str, int]:
    from monograph.core.vocab import Vocabulary
    from monograph.gen.artifacts import DDL_FILE
    from monograph.graph.loader import RelationalLoader

    spec = wiring.build_spec()
    ddl = generated(root, DDL_FILE)
    return RelationalLoader(conn, spec, wiring.mapper(spec, Vocabulary()), ddl).load(instances, log)


def ensure_database(admin: str, database: str) -> str:
    from monograph.ablation.load import with_database

    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [database]).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    return with_database(admin, database)


def build(
    admin: str,
    instances: Mapping[str, Sequence[BaseModel]],
    root: Path,
    log: Callable[[str], None],
    database: str = DATABASE,
) -> BuildReport:
    dsn = ensure_database(admin, database)
    with psycopg.connect(dsn, autocommit=True) as conn:
        load_canonical(conn, instances, root, log)
        conn.execute("SET search_path = public")
        return build_warehouse(conn, root, log)
