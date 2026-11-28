"""The H2.1 extension of the H2 frame: targeted, cached fetches for high fan-out questions (T11).

The H2 frame has few anchors with 50 or more answers (docs/h2_1/PLAN.md). The extension adds, deterministically
given the caches and the H2 frame:

1. NDC directory records for the frame substances in the most frame products: `openfda.unii:"<UNII>"`;
2. NDC directory records for the frame labeler codes on the most frame products: `product_ndc:<code>-*`;
3. RxNav NDC properties for every new product, then properties and generic links for every new RxCUI.

One page per anchor. No SPLs: a new multi-ingredient product whose SPL is not cached already cannot be
paired and is rejected by the H2 rule. openFDA pages go to `data/raw/h2_1/`, so the H2 build is unchanged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from monograph.graph.frame import BRANDED
from monograph.graph.identity import labeler_code
from monograph.ingest.openfda import OpenFdaClient
from monograph.ingest.rxnav import RxNavClient


@dataclass(frozen=True, slots=True)
class ExtensionConfig:
    substances: int = 30
    labelers: int = 30
    page_size: int = 100


@dataclass
class ExtensionReport:
    substance_anchors: list[str] = field(default_factory=list)
    labeler_anchors: list[str] = field(default_factory=list)
    records: int = 0
    new_products: int = 0
    new_rxcuis: int = 0


def substance_search(unii: str) -> str:
    return f'openfda.unii:"{unii}"'


def labeler_search(code: str) -> str:
    return f"product_ndc:{code}-*"


def top(counts: Counter[str], n: int) -> list[str]:
    """The n most frequent keys, ties broken by key."""
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


class ExtensionFetcher:
    def __init__(
        self, openfda: OpenFdaClient, rxnav: RxNavClient, config: ExtensionConfig, log: Callable[[str], None]
    ) -> None:
        self._openfda = openfda
        self._rxnav = rxnav
        self._config = config
        self._log = log

    def anchors(self, frame_ndc: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """Substances and labeler codes on the most frame products (from the frame's NDC records)."""
        substances: Counter[str] = Counter()
        labelers: Counter[str] = Counter()
        for r in frame_ndc:
            product_ndc = str(r.get("product_ndc") or "")
            if not product_ndc:
                continue
            labelers[labeler_code(product_ndc)] += 1
            for unii in sorted({str(u) for u in (r.get("openfda") or {}).get("unii", [])}):
                substances[unii] += 1
        return top(substances, self._config.substances), top(labelers, self._config.labelers)

    def fetch(self, frame_ndc: Sequence[dict[str, Any]]) -> ExtensionReport:
        c = self._config
        report = ExtensionReport()
        report.substance_anchors, report.labeler_anchors = self.anchors(frame_ndc)
        records: dict[str, dict[str, Any]] = {}
        searches = [substance_search(u) for u in report.substance_anchors]
        searches += [labeler_search(code) for code in report.labeler_anchors]
        for n, search in enumerate(searches, 1):
            for r in self._openfda.page("ndc", 0, c.page_size, search=search):
                if r.get("product_ndc"):
                    records.setdefault(str(r["product_ndc"]), r)
            if n % 10 == 0:
                self._log(f"extension: {n}/{len(searches)} searches, {len(records)} NDC records")
        report.records = len(records)
        known = {str(r.get("product_ndc")) for r in frame_ndc}
        new = sorted(set(records) - known)
        report.new_products = len(new)
        rxcuis: set[str] = set()
        for n, product_ndc in enumerate(new, 1):
            rxcuis |= {str(p["rxcui"]) for p in self._rxnav.ndc_properties(product_ndc) if p.get("rxcui")}
            rxcuis |= {str(x) for x in (records[product_ndc].get("openfda") or {}).get("rxcui", [])}
            if n % 250 == 0:
                self._log(f"extension: RxNav NDC properties {n}/{len(new)}")
        self._rxnav.version()
        for n, rxcui in enumerate(sorted(rxcuis), 1):
            props = self._rxnav.properties(rxcui)
            if props is not None and props.get("tty") in BRANDED:
                self._rxnav.related_generic(rxcui)
            if n % 250 == 0:
                self._log(f"extension: RxNav concepts {n}/{len(rxcuis)}")
        report.new_rxcuis = len(rxcuis)
        self._log(f"extension: {report.records} NDC records, {report.new_products} new products")
        return report
