"""SchemaSpec -> Postgres DDL.

* one table per entity, keyed by `iri`, with a UNIQUE constraint on the public key;
* NOT NULL from cardinality; CHECK constraints for patterns (safe subset), lengths, bounds and enums;
* foreign keys for references; join tables for reference sets;
* child tables for embedded values (position column for lists, UNIQUE key for keyed sets);
* text[] columns for enum sets, where CHECK can enforce membership and cardinality;
* RequiredWhen rules become a CHECK when both fields live in the same table.

Everything the DDL cannot enforce is listed in `DdlResult.unenforced`, with the reason. That list is
part of the leak inventory. Constraint names are deterministic so the integration test can tell which
constraint rejected a row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from monograph.core.meta import Charset
from monograph.core.refs import IRI_PATTERN
from monograph.core.rules import AtLeast, RequiredWhen, UniqueWithin
from monograph.core.spec import Card, ClassSpec, FieldSpec, Kind, SchemaError, SchemaSpec

SQL_TYPE: dict[type, str] = {
    str: "text",
    int: "bigint",
    Decimal: "numeric",
    bool: "boolean",
    date: "date",
    datetime: "timestamptz",
}


class DdlProfile(StrEnum):
    """DEFAULT: plain foreign keys, checked per statement. TEST: deferrable foreign keys, so the
    integration probe can replace a row inside a savepoint and still have references checked."""

    DEFAULT = "default"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class Unenforced:
    cls: str
    field: str | None
    kind: str
    reason: str


@dataclass(frozen=True, slots=True)
class DdlResult:
    sql: str
    unenforced: tuple[Unenforced, ...]
    partial: tuple[Unenforced, ...] = ()


def forbidden_class_sql(charset: Charset) -> str:
    """The forbidden characters as a Postgres ARE class. NUL is left out: Postgres text cannot hold it,
    so the column type already rejects it."""
    parts = [f"\\x{max(lo, 1):02x}-\\x{hi:02x}" for lo, hi in charset.forbidden_ranges()]
    return "[" + "".join(parts) + "]"


def table_name(model: type[BaseModel]) -> str:
    name = model.__name__
    return "".join(f"_{ch.lower()}" if ch.isupper() and i else ch.lower() for i, ch in enumerate(name))


def child_table(owner: str, f: FieldSpec) -> str:
    return f"{owner}__{f.name}"


def column_name(f: FieldSpec) -> str:
    return f"{f.name}_iri" if f.kind is Kind.REF else f.name


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


@dataclass
class _Table:
    name: str
    columns: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)

    def check(self, label: str, expression: str) -> None:
        name = f"ck_{self.name}_{label}"
        if len(name) > 63:
            raise SchemaError(f"constraint name {name!r} exceeds the Postgres 63-character identifier limit")
        self.constraints.append(f"CONSTRAINT {name} CHECK ({expression})")

    def render(self) -> str:
        body = ",\n".join(f"    {line}" for line in [*self.columns, *self.constraints])
        return f"CREATE TABLE {self.name} (\n{body}\n);"


class DdlGenerator:
    def __init__(
        self, spec: SchemaSpec, check_pattern: Callable[[str], None], profile: DdlProfile = DdlProfile.DEFAULT
    ) -> None:
        self._spec = spec
        self._check_pattern = check_pattern
        self._profile = profile
        self._fk = " DEFERRABLE INITIALLY IMMEDIATE" if profile is DdlProfile.TEST else ""

    def render(self) -> str:
        return self.generate().sql

    def generate(self) -> DdlResult:
        tables: list[_Table] = []
        unenforced: list[Unenforced] = []
        self._partial: list[Unenforced] = []
        for c in self._spec.entities():
            tables += self._entity(c, unenforced)
        header = [
            f"-- MONOGRAPH drug product model {self._spec.version}, {self._profile.value} profile."
            " Generated from core/models.py; do not edit.",
            "-- Run inside an empty schema. Constraint names are stable and used by the integration test.",
        ]
        sql = "\n".join(header) + "\n\n" + "\n\n".join(t.render() for t in tables) + "\n"
        return DdlResult(sql, tuple(unenforced), tuple(self._partial))

    # -----------------------------------------------------------------------------------------------

    def _entity(self, c: ClassSpec, unenforced: list[Unenforced]) -> list[_Table]:
        table = _Table(table_name(c.cls))
        self._iri_column(table)
        extra = self._fields(table, c, unenforced)
        keys = ", ".join(column_name(c.field(k)) for k in c.meta.key)
        table.constraints.append(f"CONSTRAINT uq_{table.name}_key UNIQUE ({keys})")
        self._rules(table, c, unenforced)
        for p in c.pydantic_only:
            if p.unique is not None:
                self._unique(c, p.unique, extra)
            else:
                unenforced.append(
                    Unenforced(c.cls.__name__, None, f"pydantic_only:{p.id}", "arbitrary Python has no DDL form")
                )
        return [table, *extra]

    def _unique(self, c: ClassSpec, unique: UniqueWithin, tables: list[_Table]) -> None:
        f = c.field(unique.field)
        assert f.target is not None
        target = self._spec.of(f.target)
        name = child_table(table_name(c.cls), f)
        child = next(t for t in tables if t.name == name)
        columns = ", ".join(["owner_iri", *(column_name(target.field(k)) for k in unique.keys)])
        if any(line.endswith(f"UNIQUE ({columns})") for line in child.constraints):
            return  # the keyed-set UNIQUE already says exactly this
        child.constraints.append(f"CONSTRAINT uq_{name}_{'_'.join(unique.keys)} UNIQUE ({columns})")

    def _iri_column(self, table: _Table) -> None:
        self._check_pattern(IRI_PATTERN)
        table.columns.append("iri text PRIMARY KEY")
        table.check("iri_pattern", f"iri ~ {_literal(IRI_PATTERN)}")

    def _fields(self, table: _Table, c: ClassSpec, unenforced: list[Unenforced]) -> list[_Table]:
        extra: list[_Table] = []
        for f in c.fields:
            if f.kind is Kind.EMBEDDED:
                extra += self._embedded(table.name, c, f, unenforced)
            elif f.kind is Kind.REF and f.is_collection:
                extra.append(self._join_table(table.name, c, f, unenforced))
            elif f.kind is Kind.ENUM and f.is_collection:
                self._enum_array(table, f)
            else:
                self._column(table, c, f)
        return extra

    def _column(self, table: _Table, c: ClassSpec, f: FieldSpec) -> None:
        col = column_name(f)
        not_null = " NOT NULL" if f.card is Card.ONE else ""
        if f.kind is Kind.REF:
            assert f.target is not None
            target = table_name(f.target)
            table.columns.append(f"{col} text{not_null} REFERENCES {target} (iri){self._fk}")
            table.check(f"{col}_pattern", f"{col} ~ {_literal(IRI_PATTERN)}")
            return
        if f.kind is Kind.ENUM:
            assert f.enum is not None
            table.columns.append(f"{col} text{not_null}")
            values = ", ".join(_literal(m.value) for m in f.enum)
            table.check(f"{col}_enum", f"{col} IN ({values})")
            return
        assert f.scalar is not None
        table.columns.append(f"{col} {SQL_TYPE[f.scalar]}{not_null}")
        self._partial.append(
            Unenforced(
                c.cls.__name__,
                f.name,
                "datatype",
                "the column type rejects values that do not cast, but Postgres casts text parameters,"
                " so text that parses ('3', '20240501', 'true') is accepted",
            )
        )
        if f.scalar is Decimal:
            table.check(f"{col}_finite", f"{col} NOT IN ('NaN', 'Infinity', '-Infinity')")
        k = f.constraints
        if k.charset is not None:
            table.check(f"{col}_charset", f"{col} !~ {_literal(forbidden_class_sql(k.charset))}")
        if k.pattern is not None:
            self._check_pattern(k.pattern)
            table.check(f"{col}_pattern", f"{col} ~ {_literal(k.pattern)}")
        if k.min_length is not None:
            table.check(f"{col}_min_length", f"char_length({col}) >= {k.min_length}")
        if k.max_length is not None:
            table.check(f"{col}_max_length", f"char_length({col}) <= {k.max_length}")
        for label, op, bound in (
            ("min_inclusive", ">=", k.ge),
            ("max_inclusive", "<=", k.le),
            ("min_exclusive", ">", k.gt),
            ("max_exclusive", "<", k.lt),
        ):
            if bound is not None:
                table.check(f"{col}_{label}", f"{col} {op} {bound}")

    def _enum_array(self, table: _Table, f: FieldSpec) -> None:
        assert f.enum is not None
        col = f.name
        values = ", ".join(_literal(m.value) for m in f.enum)
        table.columns.append(f"{col} text[] NOT NULL DEFAULT '{{}}'")
        table.check(f"{col}_enum", f"{col} <@ ARRAY[{values}]::text[]")
        if f.min_count > 0:
            table.check(f"{col}_min_count", f"cardinality({col}) >= {f.min_count}")
        if f.max_count is not None:
            table.check(f"{col}_max_count", f"cardinality({col}) <= {f.max_count}")

    def _join_table(self, owner: str, c: ClassSpec, f: FieldSpec, unenforced: list[Unenforced]) -> _Table:
        assert f.target is not None
        t = _Table(child_table(owner, f))
        t.columns.append(f"owner_iri text NOT NULL REFERENCES {owner} (iri) ON DELETE CASCADE{self._fk}")
        t.columns.append(f"target_iri text NOT NULL REFERENCES {table_name(f.target)} (iri){self._fk}")
        t.check("target_iri_pattern", f"target_iri ~ {_literal(IRI_PATTERN)}")
        t.constraints.append(f"CONSTRAINT pk_{t.name} PRIMARY KEY (owner_iri, target_iri)")
        self._row_counts(c, f, unenforced)
        return t

    def _embedded(self, owner: str, c: ClassSpec, f: FieldSpec, unenforced: list[Unenforced]) -> list[_Table]:
        assert f.target is not None
        target = self._spec.of(f.target)
        t = _Table(child_table(owner, f))
        self._iri_column(t)
        t.columns.append(f"owner_iri text NOT NULL REFERENCES {owner} (iri) ON DELETE CASCADE{self._fk}")
        if f.card is Card.LIST:
            t.columns.append("position integer NOT NULL")
            t.check("position_min_inclusive", "position >= 0")
            t.constraints.append(f"CONSTRAINT uq_{t.name}_position UNIQUE (owner_iri, position)")
        elif f.card is Card.SET:
            keys = ", ".join(column_name(target.field(k)) for k in target.meta.key)
            t.constraints.append(f"CONSTRAINT uq_{t.name}_key UNIQUE (owner_iri, {keys})")
        else:
            t.constraints.append(f"CONSTRAINT uq_{t.name}_owner UNIQUE (owner_iri)")
        extra = self._fields(t, target, unenforced)
        self._rules(t, target, unenforced)
        if f.card is Card.ONE:
            unenforced.append(
                Unenforced(c.cls.__name__, f.name, "required", "a required child row needs a constraint trigger")
            )
        self._row_counts(c, f, unenforced)
        return [t, *extra]

    def _row_counts(self, c: ClassSpec, f: FieldSpec, unenforced: list[Unenforced]) -> None:
        if not f.is_collection:
            return
        reason = "a row count in another table needs a constraint trigger, not a CHECK"
        if f.min_count > 0:
            unenforced.append(Unenforced(c.cls.__name__, f.name, "min_count", reason))
        if f.max_count is not None:
            unenforced.append(Unenforced(c.cls.__name__, f.name, "max_count", reason))

    def _rules(self, table: _Table, c: ClassSpec, unenforced: list[Unenforced]) -> None:
        for rule in c.rules:
            if isinstance(rule, RequiredWhen):
                when, then = c.field(rule.when), c.field(rule.then)
                if then.kind is Kind.EMBEDDED or (then.kind is Kind.REF and then.is_collection):
                    unenforced.append(
                        Unenforced(c.cls.__name__, None, f"rule:{rule.id}", "`then` lives in another table")
                    )
                    continue
                value = _literal(str(rule.equals)) if isinstance(rule.equals, str) else str(rule.equals).lower()
                present = (
                    f"cardinality({column_name(then)}) >= 1"
                    if then.is_collection
                    else f"{column_name(then)} IS NOT NULL"
                )
                table.check(
                    f"rule_{rule.id.replace('-', '_')}", f"{column_name(when)} IS DISTINCT FROM {value} OR {present}"
                )
            elif isinstance(rule, AtLeast):
                unenforced.append(
                    Unenforced(
                        c.cls.__name__, None, f"rule:{rule.id}", "counting child rows needs a constraint trigger"
                    )
                )
