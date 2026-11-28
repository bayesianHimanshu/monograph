"""Load the H2 dataset into Postgres (schema `h2`, the generated DDL) and project it into Apache AGE.

The graph is built from the Postgres tables only, following the generated projection
(`generated/monograph.age.json`), so both stores hold the same data by construction. After loading, every
vertex label and every edge source is counted on both sides; a mismatch raises.

Two read-only roles serve the agents, one per store, so a free-form arm can reach only its own store:

* `h2_sql`: USAGE on schema `h2`, SELECT on its tables;
* `h2_graph`: USAGE on `ag_catalog` and the graph schema, SELECT on the label tables.

Both have `default_transaction_read_only = on` and a fixed `statement_timeout`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel

from monograph.core.spec import SchemaSpec
from monograph.gen.age import EdgeSource, Projection, VertexLabel
from monograph.rdf.mapping import RdfMapper
from monograph.validate.postgres_side import PostgresLoader

SCHEMA = "h2"
SQL_ROLE = "h2_sql"
GRAPH_ROLE = "h2_graph"
STATEMENT_TIMEOUT = "10s"
BATCH = 2000


@dataclass(frozen=True, slots=True)
class LoadReport:
    rows: dict[str, int]
    vertices: dict[str, int]
    edges: dict[str, int]


def _agvalue(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_agvalue(v) for v in value]
    return value


def age_session(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SET search_path = ag_catalog, public")


class RelationalLoader:
    def __init__(self, conn: psycopg.Connection[Any], spec: SchemaSpec, mapper: RdfMapper, ddl: str) -> None:
        self._conn = conn
        self._spec = spec
        self._mapper = mapper
        self._ddl = ddl

    def load(self, instances: Mapping[str, Sequence[BaseModel]], log: Callable[[str], None]) -> dict[str, int]:
        c = self._conn
        c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
        c.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(SCHEMA)))
        c.execute(self._ddl.encode("utf-8"))
        loader = PostgresLoader(c, self._spec, self._mapper)
        with c.transaction():
            for cls_name, items in instances.items():
                for item in items:
                    loader.insert(item)
                log(f"load: {len(items)} {cls_name}")
        tables = [
            r[0]
            for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY 1", [SCHEMA]
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for t in tables:
            row = c.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(t))
            ).fetchone()
            counts[t] = int(row[0]) if row else 0
        return counts


class GraphLoader:
    """Builds the AGE graph from the `h2` tables, following the projection."""

    def __init__(self, conn: psycopg.Connection[Any], projection: Projection) -> None:
        self._conn = conn
        self._p = projection

    def _cypher(self, query: str, params: dict[str, Any]) -> None:
        from monograph.tools.cypher_backend import cypher_statement

        self._conn.execute(cypher_statement(self._p.graph, query, 1, True), [json.dumps(params)])

    def _rows(self, query: sql.Composed) -> list[tuple[Any, ...]]:
        return list(self._conn.execute(query).fetchall())

    def load(self, log: Callable[[str], None]) -> tuple[dict[str, int], dict[str, int]]:
        c = self._conn
        age_session(c)
        exists = c.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", [self._p.graph]).fetchone()
        if exists and exists[0]:
            c.execute("SELECT drop_graph(%s, true)", [self._p.graph])
        c.execute("SELECT create_graph(%s)", [self._p.graph])
        for v in self._p.vertices:
            c.execute("SELECT create_vlabel(%s, %s)", [self._p.graph, v.label])
            c.execute(
                sql.SQL("CREATE INDEX ON {}.{} USING gin (properties)").format(
                    sql.Identifier(self._p.graph), sql.Identifier(v.label)
                )
            )
        for e in self._p.edges:
            c.execute("SELECT create_elabel(%s, %s)", [self._p.graph, e.label])
        vertices = {v.label: self._vertices(v) for v in self._p.vertices}
        log(f"graph: {sum(vertices.values())} vertices in {len(vertices)} labels")
        edges: dict[str, int] = {}
        for e in self._p.edges:
            for s in e.sources:
                edges[f"{e.label} {s.from_label}->{s.to_label}"] = self._edges(e.label, s)
        log(f"graph: {sum(edges.values())} edges in {len(self._p.edges)} labels")
        self._verify(vertices, edges)
        return vertices, edges

    def _vertices(self, v: VertexLabel) -> int:
        columns = sql.SQL(", ").join(sql.Identifier(p.column) for p in v.properties)
        rows = self._rows(
            sql.SQL("SELECT {} FROM {}.{} ORDER BY iri").format(
                columns, sql.Identifier(SCHEMA), sql.Identifier(v.table)
            )
        )
        assignments = ", ".join(f"{p.name}: r.{p.name}" for p in v.properties)
        for i in range(0, len(rows), BATCH):
            batch = [
                {p.name: _agvalue(x) for p, x in zip(v.properties, row, strict=True)} for row in rows[i : i + BATCH]
            ]
            self._cypher(f"UNWIND $rows AS r CREATE (:{v.label} {{{assignments}}})", {"rows": batch})
        return len(rows)

    def _edges(self, label: str, s: EdgeSource) -> int:
        columns = [s.from_column, s.to_column, *(p.column for p in s.properties)]
        rows = self._rows(
            sql.SQL("SELECT {} FROM {}.{} WHERE {} IS NOT NULL ORDER BY 1, 2").format(
                sql.SQL(", ").join(sql.Identifier(x) for x in columns),
                sql.Identifier(SCHEMA),
                sql.Identifier(s.table),
                sql.Identifier(s.to_column),
            )
        )
        props = ", ".join(f"{p.name}: r.p.{p.name}" for p in s.properties)
        body = f" {{{props}}}" if props else ""
        query = (
            f"UNWIND $rows AS r MATCH (a:{s.from_label} {{iri: r.s}}), (b:{s.to_label} {{iri: r.d}})"
            f" CREATE (a)-[:{label}{body}]->(b)"
        )
        for i in range(0, len(rows), BATCH):
            batch = [
                {
                    "s": row[0],
                    "d": row[1],
                    "p": {p.name: _agvalue(x) for p, x in zip(s.properties, row[2:], strict=True)},
                }
                for row in rows[i : i + BATCH]
            ]
            self._cypher(query, {"rows": batch})
        return len(rows)

    def _count(self, query: str) -> int:
        from monograph.tools.cypher_backend import cypher_statement

        row = self._conn.execute(cypher_statement(self._p.graph, query, 1, False)).fetchone()
        return int(str(row[0])) if row else 0

    def _verify(self, vertices: Mapping[str, int], edges: Mapping[str, int]) -> None:
        problems = []
        for v in self._p.vertices:
            got = self._count(f"MATCH (n:{v.label}) RETURN count(n)")
            if got != vertices[v.label]:
                problems.append(f"{v.label}: {got} vertices, {vertices[v.label]} rows")
        for e in self._p.edges:
            for s in e.sources:
                key = f"{e.label} {s.from_label}->{s.to_label}"
                got = self._count(f"MATCH (:{s.from_label})-[r:{e.label}]->(:{s.to_label}) RETURN count(r)")
                if got != edges[key]:
                    problems.append(f"{key}: {got} edges, {edges[key]} rows")
        if problems:
            raise RuntimeError("graph does not match the tables: " + "; ".join(problems))


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return _agvalue(value)


def _canonical(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True)


def store_differences(conn: psycopg.Connection[Any], projection: Projection, limit: int = 20) -> list[str]:
    """Compare the two stores' full content, not only counts: every vertex label's (iri, properties) and every
    edge source's (from, to, edge properties) as multisets. Returns up to `limit` differences."""
    from collections import Counter

    from monograph.tools.cypher_backend import cypher_statement, parse_agtype

    def graph(query: str, columns: int) -> list[list[Any]]:
        rows = conn.execute(cypher_statement(projection.graph, query, columns, False)).fetchall()
        return [[parse_agtype(v) for v in row] for row in rows]

    age_session(conn)
    problems: list[str] = []
    for v in projection.vertices:
        cols = [p.column for p in v.properties]
        names = [p.name for p in v.properties]
        rows = conn.execute(
            sql.SQL("SELECT {} FROM {}.{}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in cols), sql.Identifier(SCHEMA), sql.Identifier(v.table)
            )
        ).fetchall()
        tables = Counter(_canonical({n: x for n, x in zip(names, row, strict=True) if x is not None}) for row in rows)
        found = graph(f"MATCH (n:{v.label}) RETURN properties(n)", 1)
        vertices = Counter(_canonical({k: x for k, x in row[0].items() if x is not None}) for row in found)
        if tables != vertices:
            extra, missing = vertices - tables, tables - vertices
            problems.append(
                f"{v.label}: {sum(missing.values())} rows not in the graph, {sum(extra.values())} extra vertices"
            )
    for e in projection.edges:
        for s in e.sources:
            cols = [s.from_column, s.to_column, *(p.column for p in s.properties)]
            names = [p.name for p in s.properties]
            rows = conn.execute(
                sql.SQL("SELECT {} FROM {}.{} WHERE {} IS NOT NULL").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                    sql.Identifier(SCHEMA),
                    sql.Identifier(s.table),
                    sql.Identifier(s.to_column),
                )
            ).fetchall()
            tables = Counter(
                _canonical([row[0], row[1], {n: x for n, x in zip(names, row[2:], strict=True) if x is not None}])
                for row in rows
            )
            found = graph(
                f"MATCH (a:{s.from_label})-[r:{e.label}]->(b:{s.to_label}) RETURN a.iri, b.iri, properties(r)", 3
            )
            edges = Counter(
                _canonical([a, b, {k: x for k, x in (props or {}).items() if x is not None}]) for a, b, props in found
            )
            if tables != edges:
                extra, missing = edges - tables, tables - edges
                problems.append(
                    f"{e.label} {s.from_label}->{s.to_label}: {sum(missing.values())} rows not in the graph,"
                    f" {sum(extra.values())} extra edges"
                )
    return problems[:limit]


def create_roles(
    conn: psycopg.Connection[Any],
    graph: str,
    passwords: Mapping[str, str],
    sql_role: str = SQL_ROLE,
    graph_role: str = GRAPH_ROLE,
) -> None:
    """(Re)create the two read-only agent roles (in this database; roles are cluster-wide, so tests use
    their own role names)."""
    SQL_ROLE, GRAPH_ROLE = sql_role, graph_role  # noqa: N806
    for role in (SQL_ROLE, GRAPH_ROLE):
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]).fetchone()
        if exists:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(passwords[role]))
        )
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                sql.Identifier(role), sql.Literal(STATEMENT_TIMEOUT)
            )
        )
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(SCHEMA), sql.Identifier(SQL_ROLE)))
    conn.execute(
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
            sql.Identifier(SCHEMA), sql.Identifier(SQL_ROLE)
        )
    )
    conn.execute(sql.SQL("ALTER ROLE {} SET search_path = {}").format(sql.Identifier(SQL_ROLE), sql.Identifier(SCHEMA)))
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA ag_catalog, {} TO {}").format(sql.Identifier(graph), sql.Identifier(GRAPH_ROLE))
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
            sql.Identifier(graph), sql.Identifier(GRAPH_ROLE)
        )
    )
    conn.execute(sql.SQL("ALTER ROLE {} SET search_path = ag_catalog, public").format(sql.Identifier(GRAPH_ROLE)))
    conn.execute(
        sql.SQL("REVOKE ALL ON SCHEMA public FROM {}, {}").format(sql.Identifier(SQL_ROLE), sql.Identifier(GRAPH_ROLE))
    )
