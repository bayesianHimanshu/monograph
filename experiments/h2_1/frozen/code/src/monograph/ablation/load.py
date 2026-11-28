"""Load one H2.1 cell: its own database in the AGE container, the cell's DDL and projection, its own roles.

Each cell is a separate database (`Cell.database`) holding schema `h2` and graph `drug`, exactly as H2 lays
them out, so the H2 loaders, backends and schema texts apply unchanged. Comments live in `pg_description`,
which any role can read in its own database, so a descriptions-off cell must not share one with a
descriptions-on cell. Each cell's two read-only roles may connect to that database only.

After loading, the two stores are compared in full (`store_differences`): every vertex and edge, with its
properties, against the rows it comes from. A difference aborts the load.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from pydantic import BaseModel

from monograph import wiring
from monograph.ablation.cells import Cell, ablate_spec
from monograph.gen.age import Projection
from monograph.gen.artifacts import AGE_FILE, DDL_FILE


def cell_projection(root: Path, cell: Cell) -> Projection:
    return Projection.parse((wiring.generated_dir(root) / cell.artifact(AGE_FILE)).read_text(encoding="utf-8"))


def cell_ddl(root: Path, cell: Cell) -> str:
    return (wiring.generated_dir(root) / cell.artifact(DDL_FILE)).read_text(encoding="utf-8")


def with_database(dsn: str, database: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None:
        host = parts.hostname or "localhost"
        netloc = f"{user}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, "/" + database, parts.query, parts.fragment))


@dataclass(frozen=True, slots=True)
class CellStore:
    """Where one cell lives: database and role names (tests use their own)."""

    database: str
    sql_role: str
    graph_role: str

    @staticmethod
    def of(cell: Cell) -> CellStore:
        return CellStore(cell.database, cell.sql_role, cell.graph_role)

    def password(self, role: str) -> str:
        return role  # local, read-only roles on a local container, as in H2

    def admin_dsn(self, admin: str) -> str:
        return with_database(admin, self.database)

    def sql_dsn(self, admin: str) -> str:
        return with_database(admin, self.database, self.sql_role, self.password(self.sql_role))

    def graph_dsn(self, admin: str) -> str:
        return with_database(admin, self.database, self.graph_role, self.password(self.graph_role))


@dataclass(frozen=True, slots=True)
class CellLoad:
    rows: dict[str, int]
    vertices: dict[str, int]
    edges: dict[str, int]


def load_cell(
    admin: str,
    cell: Cell,
    store: CellStore,
    instances: Mapping[str, Sequence[BaseModel]],
    root: Path,
    log: Callable[[str], None],
) -> CellLoad:
    from monograph.core.vocab import Vocabulary
    from monograph.graph.loader import GraphLoader, RelationalLoader, create_roles, store_differences

    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [store.database]).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(store.database)))
    spec = ablate_spec(wiring.build_spec(), cell.identity)
    projection = cell_projection(root, cell)
    with psycopg.connect(store.admin_dsn(admin), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        rows = RelationalLoader(conn, spec, wiring.mapper(spec, Vocabulary()), cell_ddl(root, cell)).load(
            instances, log
        )
        vertices, edges = GraphLoader(conn, projection).load(log)
        problems = store_differences(conn, projection)
        if problems:
            raise RuntimeError(f"{cell.name}: the stores differ: " + "; ".join(problems))
        log(f"{cell.name}: both stores hold identical data")
        roles = {store.sql_role: store.password(store.sql_role), store.graph_role: store.password(store.graph_role)}
        create_roles(conn, projection.graph, roles, store.sql_role, store.graph_role)
        db = sql.Identifier(store.database)
        conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(db))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                db, sql.Identifier(store.sql_role), sql.Identifier(store.graph_role)
            )
        )
    return CellLoad(rows, vertices, edges)


def connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, autocommit=True)
