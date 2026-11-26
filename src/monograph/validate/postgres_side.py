"""Load unvalidated instances into the generated Postgres schema and see what Postgres rejects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel

from monograph.core.refs import Ref
from monograph.core.spec import Card, ClassSpec, FieldSpec, Kind, SchemaSpec
from monograph.gen.ddl import child_table, column_name, table_name
from monograph.rdf.mapping import RdfMapper


@dataclass(frozen=True, slots=True)
class PgVerdict:
    accepted: bool
    error: str | None = None
    constraint: str | None = None
    sqlstate: str | None = None


def _plain(value: Any) -> Any:
    """Pass Python values to psycopg as an application would: enum members as values, refs as IRIs."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Ref):
        return value.iri
    if isinstance(value, frozenset | set):
        return sorted((_plain(v) for v in value), key=repr)
    return value


class PostgresLoader:
    def __init__(self, conn: psycopg.Connection[Any], spec: SchemaSpec, mapper: RdfMapper) -> None:
        self._conn = conn
        self._spec = spec
        self._mapper = mapper

    def insert(self, instance: BaseModel) -> None:
        c = self._spec.of(type(instance))
        self._insert_row(table_name(c.cls), c, instance, {"iri": str(self._mapper.iri_of(instance))})

    def delete(self, instance: BaseModel) -> None:
        c = self._spec.of(type(instance))
        query = sql.SQL("DELETE FROM {} WHERE iri = %s").format(sql.Identifier(table_name(c.cls)))
        self._conn.execute(query, [str(self._mapper.iri_of(instance))])

    def _insert_row(self, table: str, c: ClassSpec, instance: BaseModel, base: dict[str, Any]) -> None:
        row = dict(base)
        later: list[tuple[FieldSpec, Any]] = []
        for f in c.fields:
            if f.name not in instance.__dict__:
                continue
            value = instance.__dict__[f.name]
            if f.kind is Kind.EMBEDDED or (f.kind is Kind.REF and f.is_collection):
                later.append((f, value))
            else:
                row[column_name(f)] = _plain(value)
        for key, value in (instance.__pydantic_extra__ or {}).items():
            row[key] = value  # an unknown column, which Postgres rejects
        self._execute(table, row)
        for f, value in later:
            self._children(table, row["iri"], f, value)

    def _children(self, owner: str, owner_iri: str, f: FieldSpec, value: Any) -> None:
        name = child_table(owner, f)
        items = list(value) if isinstance(value, list | tuple | set | frozenset) else [value]
        if f.kind is Kind.REF:
            for item in items:
                self._execute(name, {"owner_iri": owner_iri, "target_iri": _plain(item)})
            return
        assert f.target is not None
        target = self._spec.of(f.target)
        for index, item in enumerate(items):
            if not isinstance(item, f.target):
                self._execute(name, {"owner_iri": owner_iri, "iri": _plain(item)})
                continue
            base: dict[str, Any] = {"owner_iri": owner_iri}
            base["iri"] = str(self._mapper.child_iri(owner_iri, f, item, index))
            if f.card is Card.LIST:
                base["position"] = index
            self._insert_row(name, target, item, base)

    def _execute(self, table: str, row: dict[str, Any]) -> None:
        columns = list(row)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        self._conn.execute(query, [row[c] for c in columns])


class PostgresProbe:
    """Tries an insert inside a savepoint and reports whether Postgres (or the driver) rejected it."""

    def __init__(self, conn: psycopg.Connection[Any], loader: PostgresLoader) -> None:
        self._conn = conn
        self._loader = loader

    def try_insert(self, instance: BaseModel, replace: bool = True) -> PgVerdict:
        """Insert in a savepoint. With `replace`, a row with the same IRI is deleted first (its child rows
        cascade), and deferred foreign keys are checked before the savepoint is rolled back."""
        try:
            with self._conn.transaction():
                if replace:
                    self._conn.execute("SET CONSTRAINTS ALL DEFERRED")
                    self._loader.delete(instance)
                self._loader.insert(instance)
                self._conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
                raise _Rollback
        except _Rollback:
            return PgVerdict(True)
        except psycopg.Error as exc:
            diag = getattr(exc, "diag", None)
            return PgVerdict(
                False,
                str(exc).splitlines()[0],
                getattr(diag, "constraint_name", None),
                getattr(exc, "sqlstate", None),
            )


class _Rollback(Exception):
    """Used to roll back a successful probe insert."""
