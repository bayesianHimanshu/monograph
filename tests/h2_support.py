"""Shared helpers for the H2 tests that need the AGE service (marker `age`)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from pydantic import BaseModel

from monograph.core.instances import Instantiator
from monograph.core.vocab import Vocabulary
from monograph.gen.age import Projection
from monograph.gen.artifacts import AGE_FILE, DDL_FILE
from monograph.graph.dataset import LOAD_ORDER
from monograph.graph.loader import GraphLoader, RelationalLoader, create_roles
from monograph.validate.pydantic_side import PydanticValidator
from monograph.wiring import build_spec, generated_dir, mapper, today

from .conftest import ROOT

TEST_DB = "h2_test"
TEST_ROLES = {"h2t_sql": "h2t_sql", "h2t_graph": "h2t_graph"}


def admin_dsn() -> str:
    dsn = os.environ.get("MONOGRAPH_AGE_DSN")
    if not dsn:
        pytest.skip("MONOGRAPH_AGE_DSN is not set (make age-it)")
    return dsn


def with_db(dsn: str, db: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None:
        host = parts.hostname or "localhost"
        netloc = f"{user}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, "/" + db, parts.query, parts.fragment))


def projection() -> Projection:
    return Projection.parse((generated_dir(ROOT) / AGE_FILE).read_text(encoding="utf-8"))


def mini_instances() -> dict[str, list[BaseModel]]:
    spec = build_spec()
    data = json.loads((ROOT / "fixtures" / "h2" / "mini.json").read_text(encoding="utf-8"))
    inst = Instantiator(spec)
    validator = PydanticValidator(today())
    out: dict[str, list[BaseModel]] = {}
    for name in LOAD_ORDER:
        items = []
        for record in data.get(name, []):
            instance = inst.instantiate(spec.by_name(name).cls, record)
            verdict = validator.check(instance)
            assert verdict.valid, (name, record, verdict.violations)
            items.append(instance)
        out[name] = items
    return out


def load_test_db(instances: dict[str, list[BaseModel]]) -> str:
    """(Re)build the test database from `instances`; returns its admin DSN."""
    dsn = admin_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [TEST_DB]).fetchone():
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    test_dsn = with_db(dsn, TEST_DB)
    spec = build_spec()
    ddl = (generated_dir(ROOT) / DDL_FILE).read_text(encoding="utf-8")
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        RelationalLoader(conn, spec, mapper(spec, Vocabulary()), ddl).load(instances, lambda _: None)
        GraphLoader(conn, projection()).load(lambda _: None)
        create_roles(conn, projection().graph, TEST_ROLES, "h2t_sql", "h2t_graph")
    return test_dsn


@contextmanager
def connections(test_dsn: str) -> Iterator[tuple[psycopg.Connection[Any], psycopg.Connection[Any]]]:
    sql_conn = psycopg.connect(with_db(test_dsn, TEST_DB, "h2t_sql", "h2t_sql"), autocommit=True)
    graph_conn = psycopg.connect(with_db(test_dsn, TEST_DB, "h2t_graph", "h2t_graph"), autocommit=True)
    try:
        yield sql_conn, graph_conn
    finally:
        sql_conn.close()
        graph_conn.close()
