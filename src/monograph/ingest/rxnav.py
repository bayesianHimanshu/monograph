"""RxNav (NLM RxNorm API): a cached, rate-limited client. Responses are written once and then only read.

Endpoints, checked against https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html on 2026-11-27:

  GET /REST/ndcproperties.json?id={product or package NDC}&ndcstatus=ALL   getNDCProperties: per package,
      `ndcItem` (11 digits), `ndc10`, `rxcui`, `splSetIdItem` and properties such as LABELER
  GET /REST/rxcui/{rxcui}/properties.json                                  getRxConceptProperties: name, tty
  GET /REST/rxcui/{rxcui}/related.json?tty=SCD+GPCK                         getRelatedByType: the generic
      clinical drug (or generic pack) of a branded drug
  GET /REST/version.json                                                   RxNorm data set and API versions

NLM asks for at most 20 requests per second per IP; the client stays well under that.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

BASE = "https://rxnav.nlm.nih.gov/REST"


class RxNavCache:
    """`data/raw/rxnav/<endpoint>/<sha256 of the query>.json`."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, endpoint: str, query: str) -> Path:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]
        return self._directory / endpoint / f"{digest}.json"

    def read(self, endpoint: str, query: str) -> dict[str, Any] | None:
        path = self.path(endpoint, query)
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["response"]
        return data

    def write(self, endpoint: str, query: str, response: dict[str, Any]) -> None:
        path = self.path(endpoint, query)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps({"query": query, "response": response}, sort_keys=True), encoding="utf-8")


class RxNavClient:
    def __init__(
        self,
        http: httpx.Client,
        cache: RxNavCache,
        min_interval_s: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self._http = http
        self._cache = cache
        self._min_interval = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last = float("-inf")
        self.network_calls = 0

    def _get(self, endpoint: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self._cache.read(endpoint, query)
        if cached is not None:
            return cached
        for attempt in range(5):
            wait = self._last + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            self.network_calls += 1
            response = self._http.get(BASE + path, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                self._sleep(2.0 * (attempt + 1))
                continue
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            self._cache.write(endpoint, query, data)
            return data
        raise RuntimeError(f"RxNav kept failing for {query}")

    def ndc_properties(self, ndc: str) -> list[dict[str, Any]]:
        data = self._get("ndcproperties", "/ndcproperties.json", {"id": ndc, "ndcstatus": "ALL"})
        return list(((data.get("ndcPropertyList") or {}).get("ndcProperty")) or [])

    def properties(self, rxcui: str) -> dict[str, Any] | None:
        data = self._get("properties", f"/rxcui/{rxcui}/properties.json", {})
        props = data.get("properties")
        return props if isinstance(props, dict) else None

    def related_generic(self, rxcui: str) -> list[dict[str, Any]]:
        # A space, which the query string encodes as "+"; an encoded "%2B" is a 400.
        data = self._get("related", f"/rxcui/{rxcui}/related.json", {"tty": "SCD GPCK"})
        groups = (data.get("relatedGroup") or {}).get("conceptGroup") or []
        return [c for g in groups for c in (g.get("conceptProperties") or [])]

    def version(self) -> dict[str, Any]:
        return self._get("version", "/version.json", {})
