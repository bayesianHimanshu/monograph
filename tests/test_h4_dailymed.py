from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx

from monograph.extract.sources.dailymed import DailyMedClient, SplCache, parse_spl

from .conftest import ROOT

SPL_DIR = ROOT / "fixtures" / "h4" / "spl"


def test_parses_a_multi_product_multi_ingredient_label() -> None:
    doc = parse_spl((SPL_DIR / "2f29f67a-2918-4318-9cb1-5e0eb22b35a3.xml").read_bytes())
    assert doc.document_type.upper() == "HUMAN PRESCRIPTION DRUG LABEL"
    assert doc.products
    product = doc.products[0]
    assert len(product.active) >= 2
    assert all(i.unii and i.numerator_value is not None for i in product.active)
    assert all(p.count("-") == 2 for p in product.packages)
    assert not doc.legacy_schema


def test_strengths_keep_numerator_and_denominator() -> None:
    doc = parse_spl((SPL_DIR / "0b1d086f-7be1-5072-e063-6394a90a11e6.xml").read_bytes())
    ingredient = doc.products[0].active[0]
    assert isinstance(ingredient.numerator_value, Decimal)
    assert ingredient.denominator_unit is not None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now


def _client(handler: object, tmp_path: Path) -> DailyMedClient:
    clock = _Clock()
    http = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return DailyMedClient(http, SplCache(tmp_path), 0.5, clock.sleep, clock.clock)


def test_client_caches_and_treats_none_body_as_missing(tmp_path: Path) -> None:
    sentinel = (SPL_DIR / "sentinel.xml").read_bytes()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "missing" in request.url.path:
            return httpx.Response(200, content=b"None")
        if "broken" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, content=sentinel)

    client = _client(handler, tmp_path)
    assert client.spl("abc") == sentinel
    assert client.spl("abc") == sentinel
    assert client.spl("missing") is None
    assert client.spl("broken") is None
    assert client.server_errors == ["broken"]
    assert calls.count("/dailymed/services/v2/spls/abc.xml") == 1
    assert not (tmp_path / "broken.xml").exists()  # errors are not cached; a later run retries
