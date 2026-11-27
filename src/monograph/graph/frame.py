"""The H2 frame: targeted, cached fetches that connect the H6 and H4 data.

H6 sampled each openFDA endpoint independently, so its recalls and events barely touch its products (0 of
200 H6 recalls name a loaded product). The frame adds, deterministically given the caches:

1. recalls that name product NDCs (`_exists_:openfda.product_ndc`), a fixed number of pages;
2. the NDC directory record of every product NDC those recalls name;
3. the SPL of every frame product (for ingredient pairing and active moieties);
4. RxNav: NDC properties for every frame product, then properties and generic links for every RxCUI the
   crosswalk reaches;
5. events: for a seeded sample of crosswalk RxCUIs, a fixed number of reports whose drug harmonization
   names that RxCUI.

Every step goes through a cached client, so a rerun with full caches makes no network calls.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from monograph.ingest.openfda import OpenFdaClient, RawCache
from monograph.ingest.rxnav import RxNavClient

RECALL_SEARCH = "_exists_:openfda.product_ndc"
NDC_BATCH = 20
BRANDED = {"SBD", "BPCK"}


@dataclass(frozen=True, slots=True)
class FrameConfig:
    recall_pages: int = 10
    page_size: int = 100
    event_rxcuis: int = 400
    events_per_rxcui: int = 5
    seed: int = 20261127


def ndc_search(product_ndcs: Sequence[str]) -> str:
    return " ".join(f'product_ndc:"{n}"' for n in product_ndcs)  # a space is OR; an encoded + is not


def event_search(rxcui: str) -> str:
    return f'patient.drug.openfda.rxcui:"{rxcui}"'


def recall_product_ndcs(recalls: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({str(n) for r in recalls for n in (r.get("openfda") or {}).get("product_ndc", [])})


@dataclass
class FrameReport:
    recalls: int = 0
    recall_product_ndcs: int = 0
    ndc_records: int = 0
    ndc_missing: list[str] = field(default_factory=list)
    spl_set_ids: int = 0
    spl_missing: list[str] = field(default_factory=list)
    rxnav_products: int = 0
    rxcuis: int = 0
    event_rxcuis: list[str] = field(default_factory=list)
    events: int = 0


class FrameFetcher:
    def __init__(
        self,
        openfda: OpenFdaClient,
        raw: RawCache,
        spl: Callable[[str], bytes | None],
        rxnav: RxNavClient,
        config: FrameConfig,
        log: Callable[[str], None],
    ) -> None:
        self._openfda = openfda
        self._raw = raw
        self._spl = spl
        self._rxnav = rxnav
        self._config = config
        self._log = log

    def recalls(self) -> list[dict[str, Any]]:
        c = self._config
        out: list[dict[str, Any]] = []
        for page in range(c.recall_pages):
            out += self._openfda.page("enforcement", page * c.page_size, c.page_size, search=RECALL_SEARCH)
        return out

    def ndc_records(self, product_ndcs: Sequence[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(product_ndcs), NDC_BATCH):
            out += self._openfda.page("ndc", 0, 100, search=ndc_search(product_ndcs[i : i + NDC_BATCH]))
        return out

    def fetch(self, base_ndc_records: Sequence[dict[str, Any]]) -> FrameReport:
        report = FrameReport()
        recalls = self.recalls()
        report.recalls = len(recalls)
        wanted = recall_product_ndcs(recalls)
        report.recall_product_ndcs = len(wanted)
        self._log(f"frame: {len(recalls)} recalls naming {len(wanted)} product NDCs")
        records = self.ndc_records(wanted)
        found = {str(r.get("product_ndc")) for r in records}
        report.ndc_missing = sorted(set(wanted) - found)
        products = {str(r.get("product_ndc")): r for r in [*base_ndc_records, *records] if r.get("product_ndc")}
        report.ndc_records = len(products)
        self._log(f"frame: {len(products)} NDC records ({len(report.ndc_missing)} recall NDCs not in the directory)")

        set_ids = sorted(
            {str(s).lower() for r in products.values() for s in (r.get("openfda") or {}).get("spl_set_id", [])}
        )
        for n, set_id in enumerate(set_ids, 1):
            if self._spl(set_id) is None:
                report.spl_missing.append(set_id)
            if n % 200 == 0:
                self._log(f"frame: SPL {n}/{len(set_ids)}")
        report.spl_set_ids = len(set_ids)

        rxcuis: set[str] = set()
        for n, product_ndc in enumerate(sorted(products), 1):
            rxcuis |= {str(p["rxcui"]) for p in self._rxnav.ndc_properties(product_ndc) if p.get("rxcui")}
            rxcuis |= {str(x) for x in (products[product_ndc].get("openfda") or {}).get("rxcui", [])}
            if n % 250 == 0:
                self._log(f"frame: RxNav NDC properties {n}/{len(products)}")
        report.rxnav_products = len(products)
        self._rxnav.version()
        for n, rxcui in enumerate(sorted(rxcuis), 1):
            props = self._rxnav.properties(rxcui)
            if props is not None and props.get("tty") in BRANDED:
                self._rxnav.related_generic(rxcui)
            if n % 250 == 0:
                self._log(f"frame: RxNav concepts {n}/{len(rxcuis)}")
        report.rxcuis = len(rxcuis)

        c = self._config
        sample = sorted(random.Random(c.seed).sample(sorted(rxcuis), min(c.event_rxcuis, len(rxcuis))))
        report.event_rxcuis = sample
        events: dict[str, dict[str, Any]] = {}
        for rxcui in sample:
            for e in self._openfda.page("event", 0, c.events_per_rxcui, search=event_search(rxcui)):
                events.setdefault(str(e.get("safetyreportid")), e)
        report.events = len(events)
        self._log(f"frame: {len(events)} event reports for {len(sample)} RxCUIs")
        return report
