"""Free-form tools: the read-only guards, the RETURN parser, and equally detailed schema descriptions."""

from __future__ import annotations

import re

import pytest

from monograph.gen.age import Projection
from monograph.gen.artifacts import AGE_FILE, DDL_FILE
from monograph.tools.freeform import QueryIn, SqlRunner, graph_schema_text, return_columns, sql_schema_text
from monograph.tools.registry import ToolError
from monograph.wiring import generated_dir

from .conftest import ROOT


def _ddl() -> str:
    return (generated_dir(ROOT) / DDL_FILE).read_text(encoding="utf-8")


def _projection() -> Projection:
    return Projection.parse((generated_dir(ROOT) / AGE_FILE).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "query",
    ["SET statement_timeout = 0", "SELECT 1; SELECT 2", "DELETE FROM h2.recall", "-- c\nUPDATE x SET a = 1", "BEGIN"],
)
def test_sql_guard_refuses_anything_but_one_select(query: str) -> None:
    with pytest.raises(ToolError):
        SqlRunner(None, "").run_sql(QueryIn(query=query))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("query", "columns"),
    [
        ("MATCH (a:P)-[:X]->(b) RETURN a.iri, count(DISTINCT b) AS n ORDER BY n DESC LIMIT 5", ["a_iri", "n"]),
        (
            "MATCH (a) WITH a, [x IN [1, 2] | x] AS l RETURN DISTINCT a.name, {k: 1, j: 2}, l",
            ["a_name", "k_1_j_2", "l"],
        ),
        ("MATCH (a) RETURN a.iri UNION MATCH (b) RETURN b.iri", ["b_iri"]),
        ("MATCH (a) WHERE a.name = 'x, RETURN y' RETURN a", ["a"]),
    ],
)
def test_return_columns(query: str, columns: list[str]) -> None:
    assert return_columns(query) == columns


def test_return_is_required() -> None:
    with pytest.raises(ToolError):
        return_columns("MATCH (a) DELETE a")


def test_both_schema_descriptions_carry_the_same_field_descriptions() -> None:
    sql_text = sql_schema_text(_ddl())
    graph_text = graph_schema_text(_projection())
    comments = {
        m.group(3).replace("''", "'")
        for m in re.finditer(r"COMMENT ON COLUMN (\w+)\.(\w+) IS '((?:[^']|'')*)';", sql_text)
    }
    p = _projection()
    described = {x.description for v in p.vertices for x in v.properties}
    described |= {x.description for e in p.edges for s in e.sources for x in s.properties}
    for text in described:
        assert text in sql_text.replace("''", "'"), text
        assert text in graph_text, text
    assert len(comments) > 40
    assert "label_extraction" not in sql_text
    assert "LabelExtraction" not in graph_text
