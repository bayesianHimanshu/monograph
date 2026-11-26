"""`monograph h4 fetch`: build the sampling frame from DailyMed, deterministically given the seed."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from monograph.extract.sample import DOCTYPES, FrameEntry, classify
from monograph.extract.sources.dailymed import DailyMedClient, SplCache, listing_page, parse_spl

PAGESIZE = 100


@dataclass
class FrameReport:
    entries: list[FrameEntry] = field(default_factory=list)
    gap_lookup: dict[str, list[str]] = field(default_factory=dict)  # product NDC -> set IDs found
    not_on_dailymed: list[str] = field(default_factory=list)

    def eligible_by_cell(self) -> Counter[str]:
        return Counter(e.stratum.key() for e in self.entries if e.exclusion is None)


class FrameBuilder:
    def __init__(self, client: DailyMedClient, cache: SplCache, seed: int, log: Callable[[str], None]) -> None:
        self._client = client
        self._cache = cache
        self._seed = seed
        self._log = log

    def build(self, gap_ndcs: Sequence[tuple[str, str]], pages_per_doctype: int, per_cell_target: int) -> FrameReport:
        """gap_ndcs: (product NDC, a package NDC) for each H6 ingredient-pairing failure."""
        report = FrameReport()
        seen: set[str] = set()
        for product_ndc, package_ndc in gap_ndcs:
            set_ids = self._client.set_ids_for_ndc(package_ndc) or self._client.set_ids_for_ndc(product_ndc)
            report.gap_lookup[product_ndc] = set_ids
            for set_id in set_ids:
                if set_id not in seen:
                    seen.add(set_id)
                    self._add(set_id, True, report)
        self._log(f"H6 gap: {sum(bool(v) for v in report.gap_lookup.values())} of {len(gap_ndcs)} products have an SPL")

        rng = random.Random(self._seed)
        for name, doctype in DOCTYPES.items():
            first = listing_page(self._client, doctype, 1, PAGESIZE, self._cache)
            total_pages = int(first["metadata"]["total_pages"])
            pages = sorted(rng.sample(range(1, total_pages + 1), min(pages_per_doctype, total_pages)))
            self._log(f"{name}: {total_pages} listing pages, sampling pages {pages}")
            for page in pages:
                for item in listing_page(self._client, doctype, page, PAGESIZE, self._cache)["data"]:
                    set_id = str(item["setid"]).lower()
                    if set_id in seen:
                        continue
                    seen.add(set_id)
                    cells = report.eligible_by_cell()
                    if cells and min(cells.get(k, 0) for k in _cells(name)) >= per_cell_target:
                        break
                    self._add(set_id, False, report)
        return report

    def top_up(self, set_ids: Sequence[str], report: FrameReport) -> None:
        """Add targeted candidates (for example from openFDA searches) to an existing frame."""
        seen = {e.set_id for e in report.entries} | set(report.not_on_dailymed)
        for set_id in sorted({s.lower() for s in set_ids} - seen):
            self._add(set_id, False, report)

    def _add(self, set_id: str, h6_gap: bool, report: FrameReport) -> None:
        data = self._client.spl(set_id)
        if data is None:
            report.not_on_dailymed.append(set_id)
            return
        report.entries.append(classify(parse_spl(data), h6_gap))


def _cells(regulatory: str) -> list[str]:
    return [f"{i}/{f}/{regulatory}" for i in ("single", "multi") for f in ("solid_oral", "liquid", "injectable")]
