"""The free-form arms' tools: `run_sql` or `run_cypher` (read-only, one statement, statement timeout, row cap)
and `describe_schema`. Both schema descriptions are generated from the H6 source: the DDL with its column
comments for SQL, the projection's labels, properties and edges with the same descriptions for Cypher."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from monograph.gen.age import Projection, Property
from monograph.graph.loader import STATEMENT_TIMEOUT
from monograph.tools.cypher_backend import QUOTE, cypher_statement, parse_agtype
from monograph.tools.registry import ROW_CAP, ToolError, canon

H2_TABLE_PREFIXES_EXCLUDED = ("label_extraction",)


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=8000, description="One read-only query.")


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False


SQL_DESCRIPTION = (
    "Run one read-only SQL query (PostgreSQL 18, schema h2) and get up to 200 rows. A single SELECT or WITH"
    " statement; the statement timeout is 10 s."
)
CYPHER_DESCRIPTION = (
    "Run one read-only openCypher query (Apache AGE 1.8.0, graph drug) and get up to 200 rows. Write only the"
    " Cypher: it is wrapped in cypher() for you and the result columns are read from the final RETURN. The"
    " statement timeout is 10 s."
)
SCHEMA_DESCRIPTION = "Describe the data: every table or label with its columns or properties and what they mean."


def _rows(values: Sequence[Sequence[Any]]) -> list[list[Any]]:
    return [[canon(v) for v in row] for row in values]


_LEADING = re.compile(r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*", re.S)


def _strip_strings(text: str) -> str:
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", text)


class SqlRunner:
    def __init__(self, conn: psycopg.Connection[Any], ddl: str) -> None:
        self._c = conn
        self._ddl = ddl

    def run_sql(self, a: QueryIn) -> QueryOut:
        query = a.query.strip().rstrip(";").strip()
        body = _strip_strings(_LEADING.sub("", query))
        if ";" in body:
            raise ToolError("one statement per call")
        if not re.match(r"(?i)^(select|with)\b", body):
            raise ToolError("only a SELECT or WITH query is allowed")
        try:
            with self._c.transaction():
                self._c.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                cur = self._c.execute(query.encode("utf-8"))
                columns = [d.name for d in cur.description or []]
                rows = cur.fetchmany(ROW_CAP + 1)
        except psycopg.errors.QueryCanceled as exc:
            raise ToolError(f"timeout: {str(exc).splitlines()[0]}") from exc
        except psycopg.Error as exc:
            raise ToolError(f"query error: {str(exc).strip()}") from exc
        return QueryOut(columns=columns, rows=_rows(rows[:ROW_CAP]), truncated=len(rows) > ROW_CAP)

    def describe_schema(self, a: NoInput) -> str:
        return sql_schema_text(self._ddl)


def sql_schema_text(ddl: str) -> str:
    """The generated DDL with comments, without the H4 extraction tables and without CHECK constraints
    (they restate the column comments as regular expressions and would only cost tokens)."""
    # One block per table; split on CREATE TABLE, since a table comment can itself contain blank lines.
    blocks = [b for b in re.split(r"\n\n(?=CREATE TABLE )", ddl) if b.strip()]
    keep = []
    for block in blocks:
        m = re.search(r"CREATE TABLE (\w+)", block)
        if m and m.group(1).startswith(H2_TABLE_PREFIXES_EXCLUDED):
            continue
        if not m:
            continue
        create, _, rest = block.partition("\n);")
        head, *items = create.splitlines()
        items = [ln.rstrip(",") for ln in items if "CHECK (" not in ln]
        keep.append(head + "\n" + ",\n".join(items) + "\n);" + rest)
    return "PostgreSQL 18. All tables are in schema h2 (the search path).\n\n" + "\n\n".join(keep) + "\n"


# --------------------------------------------------------------------------------------------- Cypher


def _top_level(text: str) -> list[tuple[int, str]]:
    """(position, character) for characters outside strings and brackets."""
    out, depth, quote = [], 0, ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            out.append((i, ch))
        i += 1
    return out


def return_columns(query: str) -> list[str]:
    """The column names of the final top-level RETURN, for AGE's column definition list."""
    top = {i for i, _ in _top_level(query)}
    visible = "".join(ch if i in top else " " for i, ch in enumerate(query))
    matches = list(re.finditer(r"(?i)\bRETURN\b", visible))
    if not matches:
        raise ToolError("the query has no RETURN clause")
    start = matches[-1].end()
    stop = re.search(r"(?i)\b(ORDER\s+BY|SKIP|LIMIT|UNION)\b", visible[start:])
    end = start + stop.start() if stop else len(query)
    distinct = re.match(r"(?i)\s*DISTINCT\b", visible[start:end])
    if distinct:
        start += distinct.end()
    pieces, prev = [], start
    for i in range(start, end):
        if visible[i] == ",":
            pieces.append(query[prev:i].strip())
            prev = i + 1
    pieces.append(query[prev:end].strip())
    if not all(pieces):
        raise ToolError("could not read the RETURN items")
    names = []
    for n, piece in enumerate(pieces):
        alias = re.search(r"(?i)\bAS\s+`?(\w+)`?\s*$", piece)
        names.append(alias.group(1) if alias else (re.sub(r"\W+", "_", piece).strip("_") or f"c{n}"))
    return names


class CypherRunner:
    def __init__(self, conn: psycopg.Connection[Any], projection: Projection) -> None:
        self._c = conn
        self._p = projection

    def run_cypher(self, a: QueryIn) -> QueryOut:
        query = a.query.strip().rstrip(";").strip()
        if QUOTE in query:
            raise ToolError(f"the query must not contain {QUOTE}")
        columns = return_columns(query)
        try:
            with self._c.transaction():
                self._c.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                cur = self._c.execute(cypher_statement(self._p.graph, query, len(columns), False))
                rows = cur.fetchmany(ROW_CAP + 1)
        except psycopg.errors.QueryCanceled as exc:
            raise ToolError(f"timeout: {str(exc).splitlines()[0]}") from exc
        except psycopg.Error as exc:
            raise ToolError(f"query error: {str(exc).strip()}") from exc
        parsed = [[parse_agtype(v) for v in row] for row in rows[:ROW_CAP]]
        return QueryOut(columns=columns, rows=_rows(parsed), truncated=len(rows) > ROW_CAP)

    def describe_schema(self, a: NoInput) -> str:
        return graph_schema_text(self._p)


def _property_line(x: Property) -> str:
    return f"  {x.name}: {x.type}." + (f" {x.description}" if x.description else "")


def graph_schema_text(p: Projection) -> str:
    lines = [
        f"Apache AGE 1.8.0 (openCypher subset), graph {p.graph}. Not supported by AGE: shortestPath(),",
        "CALL { } subqueries, relationship type alternation [:A|B]. Dates are ISO strings (YYYY-MM-DD).",
        "",
        "Vertex labels:",
    ]
    for v in p.vertices:
        lines.append(f"\n(:{v.label}) {v.description.splitlines()[0] if v.description else ''}".rstrip())
        lines += [_property_line(x) for x in v.properties]
    lines.append("\nEdge labels:")
    for e in p.edges:
        for s in e.sources:
            lines.append(f"\n(:{s.from_label})-[:{e.label}]->(:{s.to_label}) {s.description}".rstrip())
            lines += [_property_line(x) for x in s.properties]
    return "\n".join(lines) + "\n"
