"""Runs every fixture through Postgres and classifies the result against the DDL's own inventory."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.gen.ddl import DdlResult
from monograph.rdf.mapping import RdfMapper
from monograph.validate.fixtures import Fixture, FixtureLoader
from monograph.validate.postgres_side import PgVerdict, PostgresLoader, PostgresProbe


@dataclass(frozen=True, slots=True)
class PgRow:
    fixture: str
    violates: str
    expected_by_ddl: bool  # the DDL claims to enforce this constraint kind for this field
    verdict: PgVerdict


@dataclass(frozen=True, slots=True)
class PgMatrix:
    valid_loaded: int
    valid_failed: tuple[tuple[str, str], ...]
    rows: tuple[PgRow, ...]
    probes: tuple[tuple[str, PgVerdict], ...]


@contextmanager
def scratch_schema(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection whose search_path is a fresh schema, dropped afterwards."""
    schema = f"h6_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.commit()
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            conn.commit()


class PostgresMatrixRun:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        spec: SchemaSpec,
        ddl: DdlResult,
        mapper: RdfMapper,
        fixtures: FixtureLoader,
        instantiator: Instantiator,
    ) -> None:
        self._conn = conn
        self._spec = spec
        self._ddl = ddl
        self._fixtures = fixtures
        self._instantiator = instantiator
        self._loader = PostgresLoader(conn, spec, mapper)
        self._probe = PostgresProbe(conn, self._loader)

    def run(self, extra_probes: list[Fixture]) -> PgMatrix:
        self._conn.execute(self._ddl.sql.encode("utf-8"))
        loaded, failed = 0, []
        order = {c.cls: i for i, c in enumerate(self._spec.entities())}
        for f in sorted(self._fixtures.valid(), key=lambda f: order[f.cls]):
            verdict = self._probe.try_insert(self._instance(f), replace=False)
            if verdict.accepted:
                self._loader.insert(self._instance(f))
                loaded += 1
            else:
                failed.append((f.name, verdict.error or ""))
        rows = []
        for f in self._fixtures.invalid():
            assert f.violates is not None
            rows.append(PgRow(f.name, f.violates, self._enforced(f), self._probe.try_insert(self._instance(f))))
        probes = [
            (f.name, self._probe.try_insert(self._instance(f))) for f in [*self._fixtures.probes(), *extra_probes]
        ]
        return PgMatrix(loaded, tuple(failed), tuple(rows), tuple(probes))

    def _instance(self, f: Fixture) -> Any:
        return self._instantiator.instantiate(f.cls, f.record)

    def _enforced(self, f: Fixture) -> bool:
        assert f.violates is not None
        top = (f.field or "").split(".")[0] or None
        for u in self._ddl.unenforced:
            if u.cls == f.cls.__name__ and u.kind == f.violates and u.field in (top, None):
                return False
        return True  # the DDL inventory lists every kind it cannot enforce, pydantic_only ones included
