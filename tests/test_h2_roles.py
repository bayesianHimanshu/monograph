"""The agent roles are read-only, time-limited, and each sees only its own store (make age-it)."""

from __future__ import annotations

import psycopg
import pytest

from .h2_support import connections, load_test_db, mini_instances

pytestmark = pytest.mark.age


def test_roles_are_read_only_and_separated() -> None:
    test_dsn = load_test_db(mini_instances())
    with connections(test_dsn) as (sql_conn, graph_conn):
        assert sql_conn.execute("SELECT count(*) FROM h2.product").fetchone() == (3,)
        assert sql_conn.execute("SHOW statement_timeout").fetchone() == ("10s",)
        assert graph_conn.execute("SHOW statement_timeout").fetchone() == ("10s",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            sql_conn.execute("INSERT INTO h2.recall__products VALUES ('x', 'y')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            sql_conn.execute(
                "SELECT * FROM ag_catalog.cypher('drug', $$ MATCH (n) RETURN n $$) AS (n ag_catalog.agtype)"
            )
        rows = graph_conn.execute("SELECT * FROM cypher('drug', $$ MATCH (n:Product) RETURN count(n) $$) AS (n agtype)")
        assert rows.fetchone() == ("3",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            graph_conn.execute("SELECT * FROM cypher('drug', $$ CREATE (:Product {iri: 'x'}) $$) AS (n agtype)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            graph_conn.execute("SELECT count(*) FROM h2.product")
        with pytest.raises(psycopg.errors.QueryCanceled):
            sql_conn.execute("SELECT pg_sleep(11)")
