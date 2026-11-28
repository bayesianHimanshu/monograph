"""The H1 warehouse: generated from the H6 source through an explicit mapping, legacy in representation,
identical in content (the round trip rebuilds every canonical table from it, row for row)."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import psycopg
import pytest

from monograph import wiring
from monograph.core.models import DosageForm, Route
from monograph.gen import warehouse as w
from monograph.h1 import build

from .conftest import ROOT
from .h2_support import mini_instances


def test_every_in_scope_property_is_mapped_exactly_once() -> None:
    wh = w.analyze(wiring.build_spec())
    assert {t.name for t in wh.tables} >= {"T_PRD_MSTR", "T_AE_HDR", "X_NDC_RXN", "T_RCL_PRD_DNRM"}


def test_a_layout_that_drops_a_property_is_refused() -> None:
    spec = wiring.build_spec()
    product = w.TABLE_BY_NAME["T_PRD_MSTR"]
    broken = replace(product, cols=tuple(c for c in product.cols if c.name != "DSG_FRM_CD"))
    layout = tuple(broken if t.name == "T_PRD_MSTR" else t for t in w.LAYOUT)
    with pytest.raises(ValueError, match="unmapped"):
        w.analyze(spec, layout)


def test_a_layout_that_maps_a_property_twice_is_refused() -> None:
    spec = wiring.build_spec()
    product = w.TABLE_BY_NAME["T_PRD_MSTR"]
    twice = replace(product, cols=(*product.cols, w.Col("PRD_NM2", "text", "value", "Product.name")))
    layout = tuple(twice if t.name == "T_PRD_MSTR" else t for t in w.LAYOUT)
    with pytest.raises(ValueError, match="mapped twice"):
        w.analyze(spec, layout)


def test_route_and_dosage_form_codes_are_the_nci_codes_in_the_source() -> None:
    routes = dict((label, code) for code, label in w.nci_codes(Route))
    forms = dict((label, code) for code, label in w.nci_codes(DosageForm))
    assert len(routes) == len(Route)  # every member, however many the model has
    assert len(forms) == len(DosageForm)
    assert routes["ORAL"] == "C38288"
    assert routes["INTRAVENOUS"] == "C38276"
    assert forms["TABLET"] == "C42998"


def test_the_representation_is_legacy() -> None:
    ddl = (wiring.generated_dir(ROOT) / w.DDL_FILE).read_text(encoding="utf-8")
    assert "COMMENT" not in ddl
    mapping = json.loads((wiring.generated_dir(ROOT) / w.MAPPING_FILE).read_text(encoding="utf-8"))
    cols = {(t["table"], c["name"]): c for t in mapping["tables"] for c in t["columns"]}
    assert cols[("T_AE_HDR", "RCPT_DT")]["transform"] == "date_int"
    assert cols[("T_AE_HDR", "SRS_FLG")]["transform"] == "flag"
    assert cols[("T_RCL_PRD_DNRM", "RCL_CLS_CD")]["code_domain"] == "RCL_CLS"
    assert cols[("T_PRD_MSTR", "OLD_PRD_CD")]["role"] == "legacy"
    assert "term" not in cols[("T_PRD_MSTR", "OLD_PRD_CD")]
    recall = next(t for t in mapping["tables"] if t["table"] == "T_RCL_PRD_DNRM")
    assert recall["grain"] == "Recall x Recall.products"
    # the hardening pass (docs/h1/PLAN.md, dev round 1): staging, stale, ETL and a second code table, unmapped
    unmapped = {t["table"] for t in mapping["tables"] if not t["mapped"]}
    assert unmapped == {"STG_PRD_LOAD", "STG_RCL_FEED", "T_LKP_CD"}
    for table, col in (
        ("T_PKG", "LBLR_NM"),
        ("T_PRD_MSTR", "DSG_FRM_TXT"),
        ("T_AE_HDR", "SRS_IND"),
        ("T_AE_HDR", "LD_DT"),
    ):
        assert cols[(table, col)]["role"] == "legacy"
        assert "term" not in cols[(table, col)]
        assert cols[(table, col)]["note"]


def test_the_mapping_is_versioned() -> None:
    mapping = json.loads((wiring.generated_dir(ROOT) / w.MAPPING_FILE).read_text(encoding="utf-8"))
    assert mapping["version"] == w.WAREHOUSE_VERSION


@pytest.mark.integration
def test_the_round_trip_is_exact_on_the_mini_fixture() -> None:
    admin = os.environ.get("MONOGRAPH_PG_DSN")
    if not admin:
        pytest.skip("MONOGRAPH_PG_DSN is not set (make it)")
    dsn = build.ensure_database(admin, "h1_test_rt")
    with psycopg.connect(dsn, autocommit=True) as conn:
        build.load_canonical(conn, mini_instances(), ROOT, lambda _: None)
        _, rows = build.build_warehouse(conn, ROOT)
        assert rows["product"] == 3
        assert rows["recall__products"] > 0
        assert set(rows) >= {"product__ingredients", "adverse_event_report__reactions", "label__sections"}
        # a difference is caught: break one warehouse value and compare again
        conn.execute("UPDATE wh.T_PRD_MSTR SET FNSHD_FLG = CASE FNSHD_FLG WHEN 'Y' THEN 'N' ELSE 'Y' END")
        _, problems = build.round_trip_differences(conn, w.eq_views(w.analyze(wiring.build_spec())))
    assert any(p.startswith("product:") for p in problems)
