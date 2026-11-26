from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from monograph import wiring
from monograph.ingest.openfda import OpenFdaClient, OpenFdaMapper, RawCache, fda_date, slugify

from .conftest import ROOT


def test_slugify() -> None:
    assert slugify("Baker's Best Health Products, LLC") == "baker-s-best-health-products-llc"
    assert slugify("  Laboratoires Biochimiques Néerlandais  ") == "laboratoires-biochimiques-neerlandais"


def test_fda_date_only_reencodes_well_formed_dates() -> None:
    assert fda_date("20240501") == "2024-05-01"
    assert fda_date("2024-05") == "2024-05"
    assert fda_date(None) is None


def _event(*codes: str) -> dict[str, Any]:
    return {
        "safetyreportid": "1",
        "receivedate": "20240101",
        "serious": "1",
        "seriousnesshospitalization": "1",
        "patient": {
            "drug": [{"drugcharacterization": c, "medicinalproduct": f"D{i}"} for i, c in enumerate(codes)],
            "reaction": [{"reactionmeddrapt": "Nausea"}],
        },
    }


def test_single_suspect_is_primary_and_several_are_not_guessed() -> None:
    [one] = OpenFdaMapper().event(_event("1", "2"))
    assert [d["role"] for d in one.record["drugs"]] == ["primary suspect", "concomitant"]
    [two] = OpenFdaMapper().event(_event("1", "1", "3"))
    assert [d["role"] for d in two.record["drugs"]] == ["suspect", "suspect", "interacting"]
    assert any("does not say which is primary" in n for n in two.notes)
    assert one.record["seriousness_criteria"] == ["hospitalization"]


def test_enforcement_mapping() -> None:
    raw = {
        "recall_number": "D-0001-2024",
        "classification": "Class III",
        "reason_for_recall": "x",
        "report_date": "20240102",
        "recalling_firm": "Acme Inc.",
        "openfda": {},
    }
    org, recall = OpenFdaMapper().enforcement(raw)
    assert org.record == {"slug": "acme-inc", "name": "Acme Inc."}
    assert recall.record["classification"] == "III"
    assert recall.record["recalling_firm"].endswith("/organization/acme-inc")


def test_label_sections_use_loinc_codes() -> None:
    raw = {
        "id": "x",
        "set_id": "s",
        "version": "2",
        "effective_time": "20200101",
        "indications_and_usage": ["Use it.", "Wisely."],
        "openfda": {"rxcui": ["1", "2"]},
    }
    [label] = OpenFdaMapper().label(raw)
    assert label.record["version"] == 2
    assert label.record["sections"] == [
        {"code": "34067-9", "title": "INDICATIONS AND USAGE", "text": "Use it.\nWisely."}
    ]
    assert len(label.record["products"]) == 2


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_client_caches_and_rate_limits(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"results": [{"n": len(calls)}]})

    fake = _FakeTime()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = OpenFdaClient(http, RawCache(tmp_path), "KEY", 0.5, fake.sleep, fake.clock)
        first = client.page("ndc", 0, 1)
        again = client.page("ndc", 0, 1)
        client.page("ndc", 1, 1)
    assert first == again == [{"n": 2}]
    assert len(calls) == 3  # one 429, one success, one new page; the repeat came from the cache
    assert "api_key=KEY" in calls[0]
    assert all(s > 0 for s in fake.sleeps)


def test_ci_never_needs_the_network() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network call in an offline test")

    with httpx.Client(transport=httpx.MockTransport(refuse)):
        records = wiring.openfda_fixtures(ROOT)
    assert sum(len(v) for v in records.values()) >= 45


def test_real_fixture_records_agree() -> None:
    spec = wiring.build_spec()
    report = wiring.conformance_run(spec, ROOT).run(wiring.openfda_fixtures(ROOT))
    agreed, total = report.agreement()
    assert total > 50
    assert agreed == total, report.disagreements
    assert report.classes["AdverseEventReport"].reasons["<record>: rule:has-primary-suspect"] > 0


@pytest.mark.parametrize("endpoint", ["ndc", "label", "event", "enforcement"])
def test_fixture_files_are_raw_openfda_records(endpoint: str) -> None:
    data = json.loads((ROOT / "fixtures" / "openfda" / f"{endpoint}.json").read_text())
    assert isinstance(data, list)
    assert data
