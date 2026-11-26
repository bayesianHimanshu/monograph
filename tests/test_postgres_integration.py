"""Apply the DDL to Postgres 18, load the valid fixtures, and check what the invalid ones do.

Run with `make it` (starts docker compose) or `pytest -m integration` with MONOGRAPH_PG_DSN set.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.gen.ddl import DdlGenerator
from monograph.gen.regex import check_pattern
from monograph.rdf.mapping import RdfMapper
from monograph.validate.fixtures import Fixture, FixtureLoader
from monograph.validate.postgres_matrix import PgMatrix, PostgresMatrixRun, scratch_schema

pytestmark = pytest.mark.integration

DSN = os.environ.get("MONOGRAPH_PG_DSN", "postgresql://monograph:monograph@localhost:55432/monograph")

# Kinds the DDL enforces by column type. Postgres casts untyped text parameters to the column type,
# so text that parses ("3" -> 3, "20240501" -> a date, "true" -> boolean) is accepted. This is measured,
# not asserted; RESULTS.md reports it.
COERCIBLE = {"datatype"}


@pytest.fixture(scope="module")
def matrix(
    spec: SchemaSpec, mapper: RdfMapper, fixtures: FixtureLoader, instantiator: Instantiator
) -> Iterator[PgMatrix]:
    nul = Fixture("nul-in-name", spec.by_name("Substance").cls, {"unii": "0000000000", "name": "A\x00B"}, "agree_valid")
    with scratch_schema(DSN) as conn:
        ddl = DdlGenerator(spec, check_pattern).generate()
        yield PostgresMatrixRun(conn, spec, ddl, mapper, fixtures, instantiator).run([nul])


def test_all_valid_fixtures_load(matrix: PgMatrix, fixtures: FixtureLoader) -> None:
    assert matrix.valid_failed == ()
    assert matrix.valid_loaded == len(fixtures.valid())


def test_enforced_constraints_reject(matrix: PgMatrix) -> None:
    missed = [
        (r.fixture, r.violates)
        for r in matrix.rows
        if r.expected_by_ddl and r.violates not in COERCIBLE and r.verdict.accepted
    ]
    assert missed == []


def test_unenforced_constraints_are_really_accepted(matrix: PgMatrix) -> None:
    """The inventory of what Postgres cannot enforce must not overstate the leak."""
    surprising = [
        (r.fixture, r.violates, r.verdict.constraint)
        for r in matrix.rows
        if not r.expected_by_ddl and not r.verdict.accepted
    ]
    # One known case: the section IRI is minted from the LOINC code, so duplicate codes collide on the
    # child table's primary key, which is the same collision that SHACL sees as a merged RDF node.
    assert surprising == [
        ("Label/duplicate-section-code", "pydantic_only:label-section-codes-unique", "label__sections_pkey")
    ]


def test_dialect_probes_in_postgres(matrix: PgMatrix) -> None:
    probes: dict[str, Any] = dict(matrix.probes)
    assert not probes["Substance/trailing-newline-unii"].accepted  # Postgres agrees with Pydantic, not pySHACL
    assert not probes["Substance/nul-in-name"].accepted  # Postgres text cannot hold NUL; Pydantic and SHACL accept it


def test_ddl_matches_committed_file(spec: SchemaSpec) -> None:
    from monograph.gen.artifacts import DDL_FILE

    from .conftest import ROOT

    assert (ROOT / "generated" / DDL_FILE).read_text() == DdlGenerator(spec, check_pattern).render()
    assert wiring.generated_dir(ROOT).exists()
