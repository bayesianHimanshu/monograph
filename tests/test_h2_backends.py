"""Both tool backends must return identical results for every tool, or S-tools and G-tools are not
comparable. Runs against the AGE service (make age-it)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from monograph.core.meta import DEFAULT_BASE
from monograph.tools.cypher_backend import CypherBackend
from monograph.tools.registry import TOOLS, ToolError
from monograph.tools.service import ToolService
from monograph.tools.sql_backend import SqlBackend

from .h2_support import connections, load_test_db, mini_instances, projection

pytestmark = pytest.mark.age

B = DEFAULT_BASE
AMP, AMP_NA, SULB_NA, IBU = (f"{B}substance/{u}" for u in ("7C782967RD", "JFN36L5S8K", "DKQ4T82YE6", "WK2XYI10QM"))
P1, P2, P3 = f"{B}product/0069-1000", f"{B}product/0069-2000", f"{B}product/12345-678"
PFIZER, ACME = f"{B}organization/pfizer-laboratories-div-pfizer-inc", f"{B}organization/acme-repack-llc"
SCD, SBD, CD3 = f"{B}clinical-drug/1000", f"{B}clinical-drug/1001", f"{B}clinical-drug/2000"

CALLS: list[tuple[str, dict[str, Any]]] = [
    ("resolve_entity", {"text": "ampicillin sodium"}),
    ("resolve_entity", {"text": "Ampicillin"}),
    ("resolve_entity", {"text": "00069100001"}),
    ("resolve_entity", {"text": "0069-1000-01"}),
    ("resolve_entity", {"text": "0069-1000"}),
    ("resolve_entity", {"text": "1001"}),
    ("resolve_entity", {"text": "1001", "kind": "clinical_drug"}),
    ("resolve_entity", {"text": "Pfizer Laboratories Div. Pfizer, Inc."}),
    ("resolve_entity", {"text": "D-0002-2021"}),
    ("resolve_entity", {"text": "brandx"}),
    ("resolve_entity", {"text": "no such thing"}),
    (
        "get_entity",
        {"ids": [P1, AMP_NA, PFIZER, f"{B}recall/D-0001-2020", "product/0069-2000", f"{B}product/9999-9999"]},
    ),
    ("get_entity", {"ids": [f"{B}adverse-event-report/1001", SBD]}),
    ("neighbors", {"ids": [P1]}),
    ("neighbors", {"ids": [P1, P2], "relation": "INGREDIENT", "direction": "out"}),
    ("neighbors", {"ids": [AMP], "direction": "in"}),
    ("neighbors", {"ids": [PFIZER], "relation": "LABELER", "direction": "in"}),
    ("neighbors", {"ids": [PFIZER], "relation": "ALIAS"}),
    ("neighbors", {"ids": [SBD]}),
    ("neighbors", {"ids": [f"{B}adverse-event-report/1001"], "direction": "out"}),
    ("find_path", {"from_id": f"{B}recall/D-0001-2020", "to_id": AMP, "max_hops": 3}),
    ("find_path", {"from_id": f"{B}adverse-event-report/1002", "to_id": ACME, "max_hops": 4}),
    ("find_path", {"from_id": AMP, "to_id": IBU, "max_hops": 2}),
    ("products_with_substance", {"substance_id": AMP}),
    ("products_with_substance", {"substance_id": AMP, "include_moiety_matches": True}),
    ("products_with_substance", {"substance_id": AMP_NA, "include_moiety_matches": True}),
    ("events_for", {"entity_id": P1}),
    ("events_for", {"entity_id": AMP_NA, "serious_only": True}),
    ("events_for", {"entity_id": SBD, "date_from": "2020-01-01", "date_to": "2020-12-31"}),
    ("events_for", {"entity_id": f"{B}packaged-product/12345-678-90"}),
    ("events_for", {"entity_id": PFIZER}),
    ("recalls_for", {"entity_id": P1}),
    ("recalls_for", {"entity_id": AMP_NA}),
    ("recalls_for", {"entity_id": SCD}),
    ("recalls_for", {"entity_id": ACME}),
    ("recalls_for", {"entity_id": f"{B}packaged-product/0069-1000-01"}),
    ("count", {"what": "reports", "related_to": AMP_NA}),
    ("count", {"what": "recalls", "related_to": PFIZER, "classification": "II"}),
    ("count", {"what": "products", "related_to": AMP, "include_moiety_matches": True}),
    ("count", {"what": "products", "related_to": PFIZER}),
    ("count", {"what": "products", "related_to": SCD}),
]


@pytest.fixture(scope="module")
def services() -> Iterator[tuple[ToolService, ToolService]]:
    test_dsn = load_test_db(mini_instances())
    p = projection()
    with connections(test_dsn) as (sql_conn, graph_conn):
        sql_conn.execute("SET search_path = h2")
        yield ToolService(SqlBackend(sql_conn, p)), ToolService(CypherBackend(graph_conn, p))


def _result(service: ToolService, name: str, args: dict[str, Any]) -> Any:
    try:
        return service.call(name, args).model_dump(mode="json")
    except ToolError as exc:
        return {"error": str(exc)}


def test_the_battery_covers_every_tool() -> None:
    assert {name for name, _ in CALLS} == {t.name for t in TOOLS}


@pytest.mark.parametrize(("name", "args"), CALLS, ids=[f"{i}-{n}" for i, (n, _) in enumerate(CALLS)])
def test_backends_return_identical_results(
    services: tuple[ToolService, ToolService], name: str, args: dict[str, Any]
) -> None:
    sql_service, cypher_service = services
    assert _result(sql_service, name, args) == _result(cypher_service, name, args)


def test_semantics_on_the_mini_dataset(services: tuple[ToolService, ToolService]) -> None:
    for service in services:
        moiety = _result(service, "products_with_substance", {"substance_id": AMP, "include_moiety_matches": True})
        assert [(p["id"], p["via"]) for p in moiety["products"]] == [(P1, "moiety"), (P2, "moiety")]
        assert _result(service, "products_with_substance", {"substance_id": AMP})["products"] == []
        ndc11 = _result(service, "resolve_entity", {"text": "00069100001"})["candidates"]
        assert [(c["id"], c["match"]) for c in ndc11] == [(f"{B}packaged-product/0069-1000-01", "ndc11")]
        alias = _result(
            service, "resolve_entity", {"text": "PFIZER LABORATORIES DIV PFIZER INC.", "kind": "organization"}
        )
        assert {(c["id"], c["match"]) for c in alias["candidates"]} == {(PFIZER, "name"), (PFIZER, "alias")}
        assert _result(service, "events_for", {"entity_id": AMP_NA})["count"] == 1
        assert _result(service, "events_for", {"entity_id": P3})["count"] == 1
        recalls = _result(service, "recalls_for", {"entity_id": AMP_NA})["recalls"]
        assert [x["recall_number"] for x in recalls] == ["D-0001-2020"]
        path = _result(service, "find_path", {"from_id": f"{B}recall/D-0001-2020", "to_id": AMP, "max_hops": 3})
        assert path["length"] == 3
        assert path["paths"][0]["relations"] == ["RECALLS_PRODUCT>", "INGREDIENT>", "HAS_ACTIVE_MOIETY>"]
        assert "error" in _result(service, "events_for", {"entity_id": PFIZER})
