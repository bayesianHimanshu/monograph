"""The ontology check: parse a query with sqlglot, map every table and column back to ontology terms through the
mappings, and check it against the ontology before it runs (docs/h1/PLAN.md, "Stage 3").

1. join: every equi-join (in ON, in WHERE, or `x IN (SELECT y ...)`) links two columns that denote the same
   thing the same way: a reference and the identity it references, two references to one class, an embedded
   row and its owner, a business key and a reference by that key. A code table is joined with its domain.
   Two base tables in one SELECT with no join condition between them is a violation.
2. code: a literal compared with a coded column is a valid code (or label, in a view) for that property; a
   YYYYMMDD integer date is not compared with a date or a date string, nor taken apart as a date.
3. cardinality: COUNT(*), COUNT(x), SUM and AVG over rows of a table at a finer grain than its entity count
   that entity more than once; COUNT(DISTINCT name) counts names, not entities.
4. existence: every table and column exists and is mapped (an unused legacy column is not).

The checker is built from the catalog alone. It never sees a question, its gold or the database.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from monograph.sqlcheck.catalog import Catalog, ColumnInfo, TableInfo

RULES = ("join", "code", "cardinality", "existence")
SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")
MAX_LISTED = 12


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str  # one of RULES
    fragment: str  # the SQL it is about
    message: str


@dataclass
class CheckResult:
    parsed: bool
    violations: list[Violation] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations

    def rules(self) -> list[str]:
        return sorted({v.rule for v in self.violations})


def _sql(node: exp.Expression) -> str:
    return node.sql(dialect="postgres")


def _closest(value: str, options: dict[str, str], n: int = MAX_LISTED) -> list[tuple[str, str]]:
    """Up to n (code, label) pairs, the ones sharing the most words with the value first, then in order."""
    words = set(re.findall(r"[a-z0-9]+", value.lower()))
    scored = sorted(
        options.items(),
        key=lambda kv: (-len(words & set(re.findall(r"[a-z0-9]+", f"{kv[0]} {kv[1]}".lower()))), kv[1]),
    )
    return scored[:n]


class OntologyChecker:
    def __init__(self, catalog: Catalog, search_path: tuple[str, ...]) -> None:
        self._cat = catalog
        self._path = search_path

    # ------------------------------------------------------------------------------------ entry

    def check(self, query: str) -> CheckResult:
        try:
            tree = sqlglot.parse_one(query, dialect="postgres")
        except (ParseError, SqlglotError) as exc:
            return CheckResult(False, note=f"not parsed: {str(exc).splitlines()[0][:200]}")
        if not isinstance(tree, exp.Query):
            return CheckResult(False, note="not a query")
        out: list[Violation] = []
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if not table.name or table.db.lower() in SYSTEM_SCHEMAS or table.name.lower().startswith("pg_"):
                continue
            if not table.db and table.name.lower() in cte_names:
                continue
            info = self._cat.find(table.name, (table.db.lower(),) if table.db else self._path)
            if info is None:
                out.append(Violation("existence", table.name, f"There is no table or view {table.name!r}."))
                continue
            if not info.mapped:
                out.append(
                    Violation(
                        "existence",
                        table.name,
                        f"{info.label} is not mapped to the ontology ({info.note}); the mapped tables hold the data.",
                    )
                )
            if not table.db:
                table.set("db", exp.to_identifier(info.schema))
        try:
            qualified = qualify(
                tree,
                schema=self._cat.sqlglot_schema(self._path),
                dialect="postgres",
                validate_qualify_columns=False,
                expand_stars=False,
                quote_identifiers=False,
                identify=False,
            )
            scopes = list(traverse_scope(qualified))
        except SqlglotError as exc:
            first = str(exc).splitlines()[0]
            unknown = re.match(r"Unknown (column|table): (\S+)", first)
            if unknown:
                out.append(
                    Violation("existence", unknown.group(2), f"There is no {unknown.group(1)} {unknown.group(2)!r}.")
                )
            return CheckResult(True, out, note=f"not analysed: {first[:200]}")
        for scope in scopes:
            out += self._existence(scope)
            out += self._joins(scope)
            out += self._codes(scope)
            out += self._cardinality(scope)
        unique: dict[tuple[str, str], Violation] = {}
        for v in out:
            unique.setdefault((v.rule, v.fragment), v)
        return CheckResult(True, list(unique.values()))

    # ---------------------------------------------------------------------------------- helpers

    def _source(self, scope: Scope | None, alias: str) -> TableInfo | bool | None:
        """The base table an alias names (searching enclosing scopes), False if it is a derived table or CTE,
        None if unknown."""
        while scope is not None:
            if alias in scope.sources:
                src = scope.sources[alias]
                if isinstance(src, exp.Table):
                    if src.db.lower() in SYSTEM_SCHEMAS:
                        return False
                    return self._cat.find(src.name, (src.db.lower(),) if src.db else self._path)
                return False
            scope = scope.parent
        return None

    def _info(self, scope: Scope, col: exp.Column) -> ColumnInfo | None:
        if not col.table:
            return None
        src = self._source(scope, col.table)
        if isinstance(src, TableInfo):
            return src.columns.get(col.name.lower())
        return None

    def _own(self, scope: Scope, node: exp.Expression) -> bool:
        """The node belongs to this scope's SELECT, not to a nested one."""
        sel = node.find_ancestor(exp.Select)
        return sel is scope.expression

    def _nodes(self, scope: Scope, kind: type[exp.Expression]) -> Iterator[exp.Expression]:
        if not isinstance(scope.expression, exp.Select):
            return
        for node in scope.expression.find_all(kind):
            if self._own(scope, node):
                yield node

    def _base_sources(self, scope: Scope) -> dict[str, TableInfo] | None:
        """alias -> table for every source; None when one is derived (its grain is not known)."""
        out: dict[str, TableInfo] = {}
        for alias, src in scope.sources.items():
            if not isinstance(src, exp.Table):
                return None
            if src.db.lower() in SYSTEM_SCHEMAS:
                return None
            info = self._cat.find(src.name, (src.db.lower(),) if src.db else self._path)
            if info is None:
                return None
            out[alias] = info
        return out

    # --------------------------------------------------------------------------- rule 4: existence

    def _existence(self, scope: Scope) -> list[Violation]:
        out = []
        outputs = {
            e.alias.lower()
            for e in getattr(scope.expression, "expressions", [])
            if isinstance(e, exp.Alias)
            and not (isinstance(e.this, exp.Column) and e.this.name.lower() == e.alias.lower())
        }
        for col in scope.columns:
            if not col.table:
                bases = self._base_sources(scope)
                name = col.name.lower()
                if bases is None or name in outputs or name == "*":
                    continue
                if not any(name in t.columns for t in bases.values()):
                    where = ", ".join(sorted(t.label for t in bases.values()))
                    out.append(
                        Violation(
                            "existence", _sql(col), f"No table in this query ({where}) has a column {col.name!r}."
                        )
                    )
                continue
            src = self._source(scope, col.table)
            if not isinstance(src, TableInfo):
                continue
            if not src.mapped:
                continue  # reported once, for the table
            info = src.columns.get(col.name.lower())
            if info is None:
                out.append(
                    Violation(
                        "existence",
                        _sql(col),
                        f"{src.label} has no column {col.name!r}. Its columns: "
                        + ", ".join(c.name.upper() if src.schema == "wh" else c.name for c in src.columns.values())
                        + ".",
                    )
                )
            elif not info.mapped:
                why = info.note or "an unused legacy column"
                out.append(
                    Violation("existence", _sql(col), f"{info.label} is not mapped to any ontology property ({why}).")
                )
        return out

    # ------------------------------------------------------------------------------- rule 1: joins

    def _domain_filter(self, scope: Scope, alias: str) -> set[str]:
        """Code domains a code-table alias is restricted to in this scope (CD_DOM = 'X' or IN)."""
        out: set[str] = set()
        for node in self._nodes(scope, exp.EQ):
            assert isinstance(node, exp.EQ)
            col, lit = _col_lit(node)
            if col is not None and lit is not None and col.table == alias and col.name.lower() == "cd_dom":
                out.add(lit)
        for node in self._nodes(scope, exp.In):
            assert isinstance(node, exp.In)
            if isinstance(node.this, exp.Column) and node.this.table == alias and node.this.name.lower() == "cd_dom":
                out |= {e.name for e in node.expressions if isinstance(e, exp.Literal)}
        return out

    def _join_pair(self, scope: Scope, left: exp.Column, right: exp.Column, fragment: str) -> Violation | None:
        a, b = self._info(scope, left), self._info(scope, right)
        if a is None or b is None:
            return None
        for code_side, other, alias in ((a, b, right.table), (b, a, left.table)):
            if code_side.role == "code_value" and other.kind == "code" and other.domain:
                ref_alias = left.table if code_side is a else right.table
                domains = self._domain_filter(scope, ref_alias)
                if other.domain in domains:
                    return None
                if domains:
                    return Violation(
                        "join",
                        fragment,
                        f"{other.label} holds {_term(other)} codes of domain {other.domain!r}, but the code table is"
                        f" restricted to {sorted(domains)}.",
                    )
                return Violation(
                    "join",
                    fragment,
                    f"{other.label} holds codes of domain {other.domain!r}; the code table T_REF_CD holds every domain,"
                    f" so the join needs CD_DOM = '{other.domain}' as well.",
                )
            del alias
        if a.denotes is not None and a.denotes == b.denotes:
            return None
        return Violation("join", fragment, _join_message(a, b))

    def _joins(self, scope: Scope) -> list[Violation]:
        out: list[Violation] = []
        links: list[tuple[str, str]] = []
        for node in self._nodes(scope, exp.EQ):
            assert isinstance(node, exp.EQ)
            left, right = node.this, node.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            if not left.table or not right.table or left.table == right.table:
                continue
            links.append((left.table, right.table))
            v = self._join_pair(scope, left, right, _sql(node))
            if v is not None:
                out.append(v)
        for node in self._nodes(scope, exp.In):
            assert isinstance(node, exp.In)
            sub = node.args.get("query")
            if not isinstance(node.this, exp.Column) or sub is None:
                continue
            inner = sub.this if isinstance(sub, exp.Subquery) else sub
            if not isinstance(inner, exp.Select) or len(inner.expressions) != 1:
                continue
            proj = inner.expressions[0].unalias()
            if not isinstance(proj, exp.Column):
                continue
            inner_scope = next((s for s in scope.subquery_scopes if s.expression is inner), None)
            if inner_scope is None:
                continue
            a = self._info(scope, node.this)
            b = self._info(inner_scope, proj)
            if a is None or b is None:
                continue
            if b.role == "code_value" and a.kind == "code" and a.domain:
                domains = self._domain_filter(inner_scope, proj.table)
                if a.domain not in domains:
                    out.append(
                        Violation(
                            "join",
                            _sql(node)[:300],
                            f"{a.label} holds codes of domain {a.domain!r}; the code table lookup needs"
                            f" CD_DOM = '{a.domain}'.",
                        )
                    )
                continue
            if a.denotes is None or a.denotes != b.denotes:
                out.append(Violation("join", _sql(node)[:300], _join_message(a, b)))
        bases = self._base_sources(scope)
        if bases and len(bases) > 1:
            groups = {alias: {alias} for alias in bases}
            for x, y in links:
                if x in groups and y in groups and groups[x] is not groups[y]:
                    merged = groups[x] | groups[y]
                    for alias in merged:
                        groups[alias] = merged
            parts = {frozenset(g) for g in groups.values()}
            if len(parts) > 1:
                names = sorted(", ".join(sorted(bases[a].label for a in p)) for p in parts)
                out.append(
                    Violation(
                        "join",
                        " | ".join(names),
                        "These tables are not joined to each other by any condition (a cross product): "
                        + " ; ".join(names)
                        + ".",
                    )
                )
        return out

    # ------------------------------------------------------------------------------- rule 2: codes

    def _codes(self, scope: Scope) -> list[Violation]:
        out: list[Violation] = []
        for eq_kind in (exp.EQ, exp.NEQ):
            for node in self._nodes(scope, eq_kind):
                col, lit = _col_lit(node)
                if col is None:
                    continue
                info = self._info(scope, col)
                if info is None:
                    continue
                if lit is not None:
                    v = self._value(scope, col, info, [lit], node)
                    if v:
                        out.append(v)
                elif info.kind == "date_int" and _is_dateish(node):
                    out.append(_date_violation(info, node))
        for node in self._nodes(scope, exp.In):
            assert isinstance(node, exp.In)
            if not isinstance(node.this, exp.Column) or node.args.get("query") is not None:
                continue
            info = self._info(scope, node.this)
            lits = [e.name for e in node.expressions if isinstance(e, exp.Literal) and e.is_string]
            if info is not None and lits:
                v = self._value(scope, node.this, info, lits, node)
                if v:
                    out.append(v)
        for kind in (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
            for node in self._nodes(scope, kind):
                col = node.this if isinstance(node.this, exp.Column) else None
                if col is None and isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
                    col = node.expression if isinstance(node.expression, exp.Column) else None
                if col is None:
                    continue
                info = self._info(scope, col)
                if info is not None and info.kind == "date_int" and _is_dateish(node, col):
                    out.append(_date_violation(info, node))
        for fn in (exp.Extract, exp.DateTrunc, exp.TimestampTrunc, exp.Year, exp.Month):
            for node in self._nodes(scope, fn):
                for col in node.find_all(exp.Column):
                    info = self._info(scope, col)
                    parent = col.parent
                    if info is not None and info.kind == "date_int" and not isinstance(parent, exp.Cast):
                        out.append(_date_violation(info, node))
        return out

    def _value(
        self, scope: Scope, col: exp.Column, info: ColumnInfo, literals: list[str], node: exp.Expression
    ) -> Violation | None:
        allowed = info.allowed
        what = "the code domains of T_REF_CD" if info.role == "code_domain" else _term(info)
        if info.kind == "code_table" and info.role in ("code_value", "code_label"):
            domains = self._domain_filter(scope, col.table)
            if len(domains) != 1:
                return None
            (d,) = domains
            dom = self._cat.domains.get(d)
            if dom is None:
                return None
            allowed = frozenset(dom.codes) if info.role == "code_value" else frozenset(dom.codes.values())
            what = f"T_REF_CD domain {d!r} ({dom.enum})"
            options = dom.codes
        elif info.kind == "code" and info.domain:
            options = self._cat.domains[info.domain].codes
        elif info.kind == "label" and allowed is not None:
            options = {x: x for x in sorted(allowed)}
        elif info.kind == "bool":
            return Violation(
                "code",
                _sql(node),
                f"{info.label} is a boolean ({what}); compare it with TRUE or FALSE, not a string.",
            )
        else:
            options = {x: x for x in sorted(allowed or ())}
        if info.kind == "date_int":
            return _date_violation(info, node)
        if allowed is None:
            return None
        bad = [x for x in literals if x not in allowed]
        if not bad:
            return None
        listed = _closest(bad[0], options)
        valid = ", ".join(f"'{c}' ({lbl})" if c != lbl else f"'{c}'" for c, lbl in listed)
        more = len(options) - len(listed)
        return Violation(
            "code",
            _sql(node),
            f"{', '.join(repr(x) for x in bad)} is not a valid value of {what} in {info.label}. Valid values include:"
            f" {valid}" + (f", and {more} more" if more > 0 else "") + ".",
        )

    # ------------------------------------------------------------------------ rule 3: cardinality

    def _cardinality(self, scope: Scope) -> list[Violation]:
        if not isinstance(scope.expression, exp.Select):
            return []
        bases = self._base_sources(scope)
        if not bases:
            return []
        fine = {a: t for a, t in bases.items() if t.fine}
        entity_classes = {t.cls for t in bases.values() if not t.fine and t.cls}
        out: list[Violation] = []
        for agg_type in (exp.Count, exp.Sum, exp.Avg):
            for node in self._nodes(scope, agg_type):
                arg = node.this
                distinct = isinstance(arg, exp.Distinct)
                target = arg.expressions[0] if distinct and arg.expressions else arg
                col = target if isinstance(target, exp.Column) else None
                info = self._info(scope, col) if col is not None else None
                if distinct:
                    if isinstance(node, exp.Count) and info is not None and _is_name(self._cat, info):
                        out.append(
                            Violation(
                                "cardinality",
                                _sql(node),
                                f"{info.label} is {_term(info)}, which is not a key: different entities can share a"
                                f" value, so counting distinct values does not count {info.owner_class} entities."
                                f" Count DISTINCT its key ({_key_text(self._cat, info.owner_class or '')}).",
                            )
                        )
                    continue
                if fine:
                    own = (
                        len(fine) == 1
                        and col is not None
                        and info is not None
                        and col.table in fine
                        and _own_row(fine[col.table], info)
                    )
                    if own:
                        continue
                    tables = ", ".join(f"{t.label} ({_grain(t)})" for t in fine.values())
                    owners = sorted({t.cls for t in fine.values() if t.cls})
                    out.append(
                        Violation(
                            "cardinality",
                            _sql(node),
                            f"{_sql(node)} aggregates rows of {tables}, so each {', '.join(owners)} is counted once"
                            f" per row. Count DISTINCT the entity's key"
                            f" ({'; '.join(_key_text(self._cat, o) for o in owners)}) or aggregate before joining.",
                        )
                    )
                    continue
                if (
                    isinstance(node, exp.Count)
                    and info is not None
                    and info.denotes is not None
                    and info.denotes[1] != "seq"
                    and len(bases) > 1
                    and entity_classes - {info.denotes[0]}
                ):
                    cls = info.denotes[0]
                    out.append(
                        Violation(
                            "cardinality",
                            _sql(node),
                            f"{_sql(node)} counts {cls} once per joined row, and the join repeats a {cls} for every"
                            f" related {', '.join(sorted(entity_classes - {cls}))}. Count DISTINCT the key.",
                        )
                    )
        return out


# ---------------------------------------------------------------------------------- free helpers


def _col_lit(node: exp.Expression) -> tuple[exp.Column | None, str | None]:
    a, b = node.this, node.expression
    if isinstance(a, exp.Column) and isinstance(b, exp.Literal):
        return a, b.name
    if isinstance(b, exp.Column) and isinstance(a, exp.Literal):
        return b, a.name
    if isinstance(a, exp.Column):
        return a, None
    if isinstance(b, exp.Column):
        return b, None
    return None, None


_DATE_STRING = re.compile(r"^\d{4}-\d{2}(-\d{2})?")


def _is_dateish(node: exp.Expression, col: exp.Column | None = None) -> bool:
    """A comparison of a YYYYMMDD integer with a date, a date string, or a number that is not YYYYMMDD."""
    for e in node.iter_expressions():
        if e is col or isinstance(e, exp.Column):
            continue
        for sub in e.walk():
            if isinstance(sub, exp.Literal):
                if sub.is_string and _DATE_STRING.match(sub.name):
                    return True
                if not sub.is_string and sub.name.isdigit() and not (19000101 <= int(sub.name) <= 21001231):
                    return True
            if isinstance(sub, (exp.CurrentDate, exp.Date, exp.DateAdd, exp.DateSub, exp.StrToDate)):
                return True
            if isinstance(sub, exp.Cast) and sub.to.this in (exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP):
                return True
    return False


def _date_violation(info: ColumnInfo, node: exp.Expression) -> Violation:
    return Violation(
        "code",
        _sql(node),
        f"{info.label} ({_term(info)}) is an integer in YYYYMMDD form, not a date: compare it with integers such as"
        f" 20240131, or convert it with to_date({info.name.upper()}::text, 'YYYYMMDD').",
    )


def _grain(table: TableInfo) -> str:
    return table.grain if table.grain.startswith("one row per") else f"one row per {table.grain}"


def _own_row(table: TableInfo, info: ColumnInfo) -> bool:
    """The column is a property of the finer table's own rows (an ingredient's strength, a reaction's term, a
    route), not of the entity those rows repeat."""
    if table.row_term is None or info.term is None:
        return False
    return info.term == table.row_term or info.term.startswith(table.row_term + ".")


def _is_name(cat: Catalog, info: ColumnInfo) -> bool:
    """A non-key text property of an entity class (a name), which different entities can share."""
    if info.denotes is not None or info.term is None or info.kind != "text" or info.term.count(".") != 1:
        return False
    cls, prop = info.term.split(".")
    return cls in cat.keys and prop not in cat.keys[cls]


def _term(info: ColumnInfo) -> str:
    return info.term or "no ontology property"


def _key_text(cat: Catalog, cls: str) -> str:
    keys = cat.keys.get(cls, ())
    return f"{cls}.{', '.join(keys)}" if keys else cls


def _what(info: ColumnInfo) -> str:
    if info.denotes is None:
        return f"{info.label} holds {_term(info)}, which identifies no entity"
    cls, via = info.denotes
    if via == "sk":
        rel = "the identity of" if info.role == "identity" else f"a reference ({_term(info)}) to"
        return f"{info.label} is {rel} {cls} (surrogate key)"
    if via == "seq":
        return f"{info.label} is the position within {cls}"
    return f"{info.label} is {cls} by its business key {via.removeprefix('key:')}"


def _join_message(a: ColumnInfo, b: ColumnInfo) -> str:
    return (
        f"{_what(a)}; {_what(b)}. No ontology relation links them this way: join a reference to the identity it"
        " references (surrogate key to surrogate key of the same class, or business key to business key)."
    )
