"""H2.1 fairness: in every cell both stores hold identical data and the tool backends agree; descriptions-off
cells carry no comments anywhere an agent can read; each cell's roles reach only their own database; and with
identity off the resolver matches canonical identifiers and names exactly, as the PLAN specifies.

Runs against the AGE service (make age-it), on the H2 mini fixture loaded into one test database per cell.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from pydantic import BaseModel

from monograph.ablation.cells import CELLS, IDENTITY_FIELDS, Cell
from monograph.ablation.load import CellStore, cell_projection, load_cell
from monograph.agent.arms import tools_arm
from monograph.core.instances import Instantiator
from monograph.core.meta import DEFAULT_BASE
from monograph.graph.dataset import LOAD_ORDER, recalling_firm_slug
from monograph.tools.cypher_backend import CypherBackend
from monograph.tools.registry import RESOLVE_EXACT_DESCRIPTION, ToolError
from monograph.tools.service import ToolService
from monograph.tools.sql_backend import SqlBackend
from monograph.validate.pydantic_side import PydanticValidator
from monograph.wiring import build_spec, today

from .conftest import ROOT
from .h2_support import admin_dsn
from .test_h2_backends import CALLS

pytestmark = pytest.mark.age

B = DEFAULT_BASE
PFIZER = f"{B}organization/pfizer-laboratories-div-pfizer-inc"


def mini_records(identity: bool) -> dict[str, list[dict[str, Any]]]:
    """The H2 mini fixture, or the same records without the identity layer, by the builder's rules."""
    data: dict[str, list[dict[str, Any]]] = json.loads(
        (ROOT / "fixtures" / "h2" / "mini.json").read_text(encoding="utf-8")
    )
    if identity:
        return data
    out = copy.deepcopy(data)
    strip = {(c, f) for c, f in IDENTITY_FIELDS}
    for cls_name, records in out.items():
        for r in records:
            for c, f in strip:
                if c == cls_name:
                    r.pop(f, None)
    for r in out["AdverseEventReport"]:
        for d in r["drugs"]:
            d.pop("clinical_drug", None)
            d.pop("harmonized_clinical_drugs", None)
    names = {f"{B}organization/{o['slug']}": o["name"] for o in out["Organization"]}
    labelers = {p["labeler"] for p in out["PackagedProduct"]}
    orgs = [o for o in out["Organization"] if f"{B}organization/{o['slug']}" in labelers]
    firms: dict[str, str] = {}
    for r in out["Recall"]:
        name = names[r["recalling_firm"]]
        slug = recalling_firm_slug(name)
        firms.setdefault(slug, name)
        r["recalling_firm"] = f"{B}organization/{slug}"
    out["Organization"] = orgs + [{"slug": s, "name": n} for s, n in sorted(firms.items())]
    return out


def instances(identity: bool) -> dict[str, list[BaseModel]]:
    spec = build_spec()
    inst, validator = Instantiator(spec), PydanticValidator(today())
    out: dict[str, list[BaseModel]] = {}
    for name in LOAD_ORDER:
        items = []
        for record in mini_records(identity).get(name, []):
            instance = inst.instantiate(spec.by_name(name).cls, record)
            verdict = validator.check(instance)
            assert verdict.valid, (name, record, verdict.violations)
            items.append(instance)
        out[name] = items
    return out


def _store(cell: Cell) -> CellStore:
    return CellStore(f"h21t_{cell.short}", f"h21t_{cell.short}_sql", f"h21t_{cell.short}_graph")


_LOADED: set[str] = set()


def loaded(cell: Cell) -> CellStore:
    store = _store(cell)
    if cell.name not in _LOADED:
        load_cell(admin_dsn(), cell, store, instances(cell.identity), ROOT, lambda _: None)
        _LOADED.add(cell.name)
    return store


@pytest.fixture(scope="module", params=CELLS, ids=[c.name for c in CELLS])
def cell(request: pytest.FixtureRequest) -> Cell:
    c: Cell = request.param
    return c


@pytest.fixture(scope="module")
def services(cell: Cell) -> Iterator[tuple[ToolService, ToolService]]:
    store = loaded(cell)
    p = cell_projection(ROOT, cell)
    sql_conn = psycopg.connect(store.sql_dsn(admin_dsn()), autocommit=True)
    graph_conn = psycopg.connect(store.graph_dsn(admin_dsn()), autocommit=True)
    try:
        yield (
            ToolService(SqlBackend(sql_conn, p), identity=cell.identity),
            ToolService(CypherBackend(graph_conn, p), identity=cell.identity),
        )
    finally:
        sql_conn.close()
        graph_conn.close()


def _result(service: ToolService, name: str, args: dict[str, Any]) -> Any:
    try:
        return service.call(name, args).model_dump(mode="json")
    except ToolError as exc:
        return {"error": str(exc)}


def test_the_mini_cells_are_what_the_builder_would_make() -> None:
    on, off = instances(True), instances(False)
    for cls_name in LOAD_ORDER:
        if cls_name != "Organization":
            assert len(on[cls_name]) == len(off[cls_name])
    firms = {str(r.recalling_firm.iri) for r in off["Recall"]}  # type: ignore[attr-defined]
    assert firms
    assert all("/recalling-firm-" in f for f in firms)


def test_both_stores_hold_identical_data(cell: Cell) -> None:
    from monograph.graph.loader import store_differences

    store = loaded(cell)
    with psycopg.connect(store.admin_dsn(admin_dsn()), autocommit=True) as conn:
        assert store_differences(conn, cell_projection(ROOT, cell)) == []


@pytest.mark.parametrize(("name", "args"), CALLS, ids=[f"{i}-{n}" for i, (n, _) in enumerate(CALLS)])
def test_backends_return_identical_results_in_every_cell(
    services: tuple[ToolService, ToolService], name: str, args: dict[str, Any]
) -> None:
    sql_service, graph_service = services
    assert _result(sql_service, name, args) == _result(graph_service, name, args)


def test_descriptions_off_has_no_comments_anywhere(cell: Cell) -> None:
    store = loaded(cell)
    query = "SELECT count(*) FROM pg_description"
    with psycopg.connect(store.sql_dsn(admin_dsn()), autocommit=True) as conn:
        # every comment an agent's role can read in its database, on any object (pg_description is public)
        found = conn.execute(
            query + " d JOIN pg_class c ON c.oid = d.objoid JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'h2'"
        ).fetchone()
        assert found is not None
        assert (found[0] > 0) is cell.descriptions


def test_each_cells_roles_reach_only_their_own_database(cell: Cell) -> None:
    store = loaded(cell)
    for other in CELLS:
        if other == cell:
            continue
        target = loaded(other)
        foreign = CellStore(target.database, store.sql_role, store.graph_role)
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(foreign.sql_dsn(admin_dsn())).close()


# ------------------------------------------------------------------------------ resolver, identity off


def _off() -> Iterator[tuple[ToolService, ToolService]]:
    cell = next(c for c in CELLS if not c.identity and c.descriptions)
    store = loaded(cell)
    p = cell_projection(ROOT, cell)
    sql_conn = psycopg.connect(store.sql_dsn(admin_dsn()), autocommit=True)
    graph_conn = psycopg.connect(store.graph_dsn(admin_dsn()), autocommit=True)
    try:
        yield (
            ToolService(SqlBackend(sql_conn, p), identity=False),
            ToolService(CypherBackend(graph_conn, p), identity=False),
        )
    finally:
        sql_conn.close()
        graph_conn.close()


@pytest.fixture(scope="module")
def off_services() -> Iterator[tuple[ToolService, ToolService]]:
    yield from _off()


def _ids(service: ToolService, text: str, kind: str | None = None) -> list[str]:
    args: dict[str, Any] = {"text": text} | ({"kind": kind} if kind else {})
    return [c["id"] for c in service.call("resolve_entity", args).model_dump()["candidates"]]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0069-1000-01", [f"{B}packaged-product/0069-1000-01"]),  # canonical package NDC
        ("0069-1000", [f"{B}product/0069-1000"]),  # canonical product NDC
        ("7C782967RD", [f"{B}substance/7C782967RD"]),  # canonical UNII
        ("AMPICILLIN", [f"{B}substance/7C782967RD"]),  # exact canonical name
        (
            "Pfizer Laboratories Div Pfizer Inc",
            [PFIZER, f"{B}organization/recalling-firm-pfizer-laboratories-div-pfizer-inc"],
        ),
        ("pfizer-laboratories-div-pfizer-inc", [PFIZER]),  # exact slug
        ("00069100001", []),  # 11-digit NDC: a derived crosswalk
        ("7c782967rd", []),  # not as written in the data
        ("ampicillin", []),  # no case normalization
        ("Pfizer Laboratories Div. Pfizer, Inc.", []),  # no punctuation or suffix normalization
        ("PFIZER LABORATORIES DIV PFIZER INC.", []),  # a recorded alias with identity on
    ],
)
def test_identity_off_resolves_exactly(
    off_services: tuple[ToolService, ToolService], text: str, expected: list[str]
) -> None:
    for service in off_services:
        assert _ids(service, text) == sorted(expected), (service.backend, text)


def test_identity_on_still_resolves_through_the_layer() -> None:
    cell = next(c for c in CELLS if c.identity and c.descriptions)
    store = loaded(cell)
    p = cell_projection(ROOT, cell)
    with psycopg.connect(store.sql_dsn(admin_dsn()), autocommit=True) as conn:
        service = ToolService(SqlBackend(conn, p))
        assert _ids(service, "00069100001") == [f"{B}packaged-product/0069-1000-01"]
        assert PFIZER in _ids(service, "PFIZER LABORATORIES DIV PFIZER INC.")
        assert PFIZER in _ids(service, "Pfizer Laboratories Div. Pfizer, Inc.")


def test_identity_off_tools_find_no_crosswalk_or_moiety(off_services: tuple[ToolService, ToolService]) -> None:
    amp, amp_na = f"{B}substance/7C782967RD", f"{B}substance/JFN36L5S8K"
    for service in off_services:
        with_salts = service.call("products_with_substance", {"substance_id": amp, "include_moiety_matches": True})
        direct = service.call("products_with_substance", {"substance_id": amp})
        assert with_salts.model_dump() == direct.model_dump()
        assert service.call("events_for", {"entity_id": f"{B}product/0069-1000"}).model_dump()["count"] == 0
        assert service.call("recalls_for", {"entity_id": f"{B}clinical-drug/1000"}).model_dump()["recalls"] == []
        moiety = service.call("neighbors", {"ids": [amp_na], "relation": "HAS_ACTIVE_MOIETY"}).model_dump()
        assert moiety["neighbors"] == []
        firm = service.call("recalls_for", {"entity_id": PFIZER}).model_dump()["recalls"]
        assert firm == []  # the labeler issued no recall: recalling firms are their own organizations


def test_only_resolve_entity_is_described_differently(off_services: tuple[ToolService, ToolService]) -> None:
    on = next(c for c in CELLS if c.identity and c.descriptions)
    store = loaded(on)
    with psycopg.connect(store.sql_dsn(admin_dsn()), autocommit=True) as conn:
        on_defs = tools_arm("S-tools", ToolService(SqlBackend(conn, cell_projection(ROOT, on)))).tools
    off_defs = tools_arm("S-tools", off_services[0]).tools
    assert [d["name"] for d in on_defs] == [d["name"] for d in off_defs]
    for a, b in zip(on_defs, off_defs, strict=True):
        if a["name"] == "resolve_entity":
            assert b["description"] == RESOLVE_EXACT_DESCRIPTION
            assert a["input_schema"] == b["input_schema"]
        else:
            assert a == b
