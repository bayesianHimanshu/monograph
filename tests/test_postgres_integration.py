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
from monograph.core.regex import check_pattern
from monograph.core.spec import SchemaSpec
from monograph.gen.ddl import DdlGenerator, DdlProfile
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
        ddl = DdlGenerator(spec, check_pattern, DdlProfile.TEST).generate()
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
    assert surprising == []


def test_declared_uniqueness_is_enforced_by_the_generated_unique(matrix: PgMatrix) -> None:
    rows = {r.fixture: r for r in matrix.rows}
    duplicate = rows["Product/duplicate-substance"]
    assert duplicate.expected_by_ddl
    assert duplicate.verdict.constraint == "uq_product__ingredients_substance"
    sections = rows["Label/duplicate-section-code"]
    assert sections.expected_by_ddl
    assert sections.verdict.constraint == "label__sections_pkey"  # the section IRI is minted from the code


def test_charset_fixtures_are_rejected_by_check_or_type(matrix: PgMatrix) -> None:
    charset = [r for r in matrix.rows if r.violates == "charset"]
    assert charset
    for r in charset:
        assert not r.verdict.accepted, r.fixture
        assert (r.verdict.constraint or "").endswith("_charset") or "NUL" in (r.verdict.error or "")


def test_default_profile_applies_and_loads(
    spec: SchemaSpec, mapper: RdfMapper, fixtures: FixtureLoader, instantiator: Instantiator
) -> None:
    from monograph.validate.postgres_side import PostgresLoader

    with scratch_schema(DSN) as conn:
        conn.execute(DdlGenerator(spec, check_pattern, DdlProfile.DEFAULT).render().encode())
        loader = PostgresLoader(conn, spec, mapper)
        order = {c.cls: i for i, c in enumerate(spec.entities())}
        for f in sorted(fixtures.valid(), key=lambda f: order[f.cls]):
            loader.insert(instantiator.instantiate(f.cls, f.record))
        assert conn.execute("SELECT count(*) FROM product").fetchone() == (4,)


def test_dialect_probes_in_postgres(matrix: PgMatrix) -> None:
    probes: dict[str, Any] = dict(matrix.probes)
    assert all(not v.accepted for v in probes.values())
    assert not probes["Substance/nul-in-name"].accepted  # Postgres text cannot hold NUL; Pydantic and SHACL accept it


def test_ddl_matches_committed_file(spec: SchemaSpec) -> None:
    from monograph.gen.artifacts import DDL_FILE, DDL_TEST_FILE

    from .conftest import ROOT

    assert (ROOT / "generated" / DDL_FILE).read_text() == DdlGenerator(spec, check_pattern).render()
    assert (ROOT / "generated" / DDL_TEST_FILE).read_text() == DdlGenerator(
        spec, check_pattern, DdlProfile.TEST
    ).render()
    assert wiring.generated_dir(ROOT).exists()
