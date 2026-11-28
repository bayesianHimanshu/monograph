"""H2.1 templates on the mini fixture, in every cell: every candidate whose gold passes the size rules is
reproduced by its tool plan on both stores (or rejected as truncated), never contradicted; invariant types have
the same gold at both identity levels. Runs against the AGE service (make age-it)."""

from __future__ import annotations

import random
from collections.abc import Iterator

import psycopg
import pytest

from monograph.ablation.cells import CELLS
from monograph.ablation.load import cell_projection
from monograph.ablation.questions import INVARIANT_TYPES, TEMPLATES, TYPES, _gold_ok, check
from monograph.qa.index import Index
from monograph.tools.cypher_backend import CypherBackend
from monograph.tools.service import ToolService
from monograph.tools.sql_backend import SqlBackend

from .conftest import ROOT
from .h2_support import admin_dsn
from .test_h2_1_stores import instances, loaded

pytestmark = pytest.mark.age


@pytest.fixture(scope="module")
def services() -> Iterator[dict[str, dict[str, tuple[bool, ToolService]]]]:
    conns = []
    out: dict[str, dict[str, tuple[bool, ToolService]]] = {"sql": {}, "graph": {}}
    for cell in CELLS:
        store = loaded(cell)
        p = cell_projection(ROOT, cell)
        s = psycopg.connect(store.sql_dsn(admin_dsn()), autocommit=True)
        g = psycopg.connect(store.graph_dsn(admin_dsn()), autocommit=True)
        conns += [s, g]
        out["sql"][cell.name] = (cell.identity, ToolService(SqlBackend(s, p), identity=cell.identity))
        out["graph"][cell.name] = (cell.identity, ToolService(CypherBackend(g, p), identity=cell.identity))
    yield out
    for c in conns:
        c.close()


def test_every_type_has_templates() -> None:
    assert {t.type for t in TEMPLATES} == set(TYPES)
    assert len({t.id for t in TEMPLATES}) == len(TEMPLATES)
    for t in TEMPLATES:
        assert len(t.phrasings) >= 3
        assert len(set(t.phrasings)) == len(t.phrasings)


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
def test_plans_reproduce_the_gold_in_every_cell(
    services: dict[str, dict[str, tuple[bool, ToolService]]], template: object
) -> None:
    from monograph.ablation.questions import Template

    assert isinstance(template, Template)
    on, off = Index.build(instances(True)), Index.build(instances(False))
    seen = 0
    for n, c in enumerate(template.candidates(on, off, random.Random(1))):
        if n >= 40:
            break
        try:
            ok = _gold_ok(template, c)
        except AssertionError:
            pytest.fail(f"{template.id}: gold differs between identity levels for an invariant type")
        if not ok:
            continue
        seen += 1
        for store in ("sql", "graph"):
            assert check(c, services[store]) in ("ok", "truncated"), (template.id, store, c.params)
        if template.type in INVARIANT_TYPES:
            assert c.gold[True] == c.gold[False]
    EXERCISED[template.id] = seen


EXERCISED: dict[str, int] = {}


def test_the_mini_fixture_exercises_most_templates() -> None:
    # runs after the parametrized test above (file order); the real question set is checked at generation
    if len(EXERCISED) < len(TEMPLATES):
        pytest.skip("run with the whole module")
    assert sum(1 for n in EXERCISED.values() if n) >= len(TEMPLATES) // 2, EXERCISED
