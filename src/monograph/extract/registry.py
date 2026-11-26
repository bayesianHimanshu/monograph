"""Registry data for the reference graph: fetched once (cached), then loaded with no network access."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from monograph.core.meta import DEFAULT_BASE
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.extract.resolve import ReferenceGraph, SubstanceSource, substance_sources_from_spls
from monograph.extract.sources.dailymed import BASE, DailyMedClient, SplCache, SplDocument
from monograph.ingest.openfda import Candidate, OpenFdaClient, OpenFdaMapper, RawCache
from monograph.rdf.mapping import RdfMapper

UNII_PAGE_SIZE = 100


def ndc_query(set_id: str) -> str:
    return f'openfda.spl_set_id:"{set_id}"'


def label_query(set_id: str) -> str:
    return f'set_id:"{set_id}"'


class RegistryFetcher:
    def __init__(self, openfda: OpenFdaClient, dailymed: DailyMedClient, spl_cache: SplCache) -> None:
        self._openfda = openfda
        self._dailymed = dailymed
        self._spl_cache = spl_cache

    def fetch(self, set_ids: Sequence[str], log: Callable[[str], None]) -> None:
        for n, set_id in enumerate(set_ids, 1):
            self._openfda.page("ndc", 0, 100, search=ndc_query(set_id))
            self._openfda.page("label", 0, 1, search=label_query(set_id))
            path = self._spl_cache.listing_path(f"ndcs-{set_id}", 100, 1)
            if not path.exists():
                data = self._dailymed.get_json(f"{BASE}/spls/{set_id}/ndcs.json", {"pagesize": "100"})
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, sort_keys=True))
            if n % 50 == 0:
                log(f"registry: {n}/{len(set_ids)} labels")
        page, total = 1, None
        while total is None or page <= total:
            path = self._spl_cache.listing_path("uniis", UNII_PAGE_SIZE, page)
            if not path.exists():
                data = self._dailymed.get_json(
                    f"{BASE}/uniis.json", {"pagesize": str(UNII_PAGE_SIZE), "page": str(page)}
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, sort_keys=True))
            total = int(json.loads(path.read_text())["metadata"]["total_pages"])
            page += 1
        log(f"registry: DailyMed UNII list, {total} pages")


def load_reference(
    spec: SchemaSpec,
    vocab: Vocabulary,
    mapper: RdfMapper,
    raw: RawCache,
    spl_cache: SplCache,
    set_ids: Sequence[str],
    documents: Sequence[SplDocument],
    dailymed_links: Mapping[str, Sequence[str]],
) -> ReferenceGraph:
    """The reference graph from cached registry data only. Raises if a needed cache file is missing.

    dailymed_links: product NDC -> set IDs from DailyMed's NDC index (the frame's H6 gap lookup), for
    products whose openFDA record carries no spl_set_id."""
    reference = ReferenceGraph(spec, vocab, mapper)
    openfda = OpenFdaMapper()
    candidates = []
    ndc_records: list[dict[str, Any]] = list(raw.all("ndc"))
    label_records: list[dict[str, Any]] = list(raw.all("label"))
    label_uniis: dict[str, set[str]] = {}
    for set_id in set_ids:
        ndc_page = raw.read("ndc", 0, 100, search=ndc_query(set_id))
        label_page = raw.read("label", 0, 1, search=label_query(set_id))
        listing = spl_cache.listing_path(f"ndcs-{set_id}", 100, 1)
        if ndc_page is None or label_page is None or not listing.exists():
            raise FileNotFoundError(f"registry cache missing for {set_id}; run `monograph h4 fetch`")
        ndc_records += ndc_page
        label_records += label_page
        label_uniis[set_id] = {str(u) for r in ndc_page + label_page for u in (r.get("openfda") or {}).get("unii", [])}
        candidates += list(dailymed_ndcs(set_id, json.loads(listing.read_text())))
    for record in ndc_records:
        mapped = openfda.ndc(record)
        candidates += mapped + list(nested_packages(mapped, record)) + list(label_links(record))
    for product_ndc, linked in sorted(dailymed_links.items()):  # DailyMed's NDC index (H6 gap products)
        for set_id in linked:
            products = [f"{DEFAULT_BASE}product/{product_ndc}"]
            candidates.append(Candidate("Label", {"set_id": set_id, "products": products}, "dailymed", ("ndc index",)))
    for record in label_records:
        candidates += [c for c in openfda.label(record) if c.cls == "Label"]
    sources = [
        SubstanceSource(c.record["unii"], str(c.record.get("name") or ""), "openfda")
        for c in candidates
        if c.cls == "Substance"
    ]
    sources += substance_sources_from_spls(documents)
    for path in sorted(spl_cache.listing_path("uniis", UNII_PAGE_SIZE, 1).parent.glob("uniis-*.json")):
        sources += [
            SubstanceSource(str(d["unii_code"]), str(d["active_moiety"]), "dailymed-uniis")
            for d in json.loads(path.read_text())["data"]
        ]
    reference.add_substances(sources)
    reference.add_candidates(candidates)
    reference.label_uniis = {k: frozenset(v) for k, v in label_uniis.items()}
    return reference


_NDC_IN_TEXT = re.compile(r"\b(\d{4}-\d{4}-\d{2}|\d{5}-\d{3}-\d{2}|\d{5}-\d{4}-\d)\b")


def nested_packages(mapped: Sequence[Candidate], record: dict[str, Any]) -> Iterator[Candidate]:
    """Inner package NDCs. openFDA's `package_ndc` is the outer package only; the nested ones appear in
    the package description, for example "10 VIAL in 1 CARTON (71288-031-92) > 1 mL in 1 VIAL (71288-031-91)".
    They become PackagedProducts of the same product."""
    outer = {str(c.record.get("ndc")): c for c in mapped if c.cls == "PackagedProduct"}
    seen = set(outer)
    for package in record.get("packaging") or []:
        parent = outer.get(str(package.get("package_ndc")))
        if parent is None:
            continue
        for ndc in _NDC_IN_TEXT.findall(str(package.get("description", ""))):
            if ndc not in seen:
                seen.add(ndc)
                inner = {k: v for k, v in parent.record.items() if k != "description"} | {"ndc": ndc}
                yield Candidate("PackagedProduct", inner, parent.source, ("nested package from description",))


def label_links(record: dict[str, Any]) -> Iterator[Candidate]:
    """The NDC directory says which SPL lists a product (`openfda.spl_set_id`). That is the registry's own
    label-to-product link, so it becomes a Label node with `labelsProduct`, even without a version."""
    product_ndc = record.get("product_ndc")
    for set_id in (record.get("openfda") or {}).get("spl_set_id", []):
        if product_ndc:
            products = [f"{DEFAULT_BASE}product/{product_ndc}"]
            yield Candidate("Label", {"set_id": str(set_id).lower(), "products": products}, "ndc", ("label link",))


def dailymed_ndcs(set_id: str, listing: dict[str, Any]) -> Iterator[Candidate]:
    """DailyMed's NDC index for an SPL: each NDC becomes a PackagedProduct of the product named by its first
    two segments, and the label is linked to those products. Package NDCs only (three segments)."""
    products: set[str] = set()
    for item in (listing.get("data") or {}).get("ndcs", []):
        ndc = str(item.get("ndc", ""))
        if _NDC_IN_TEXT.fullmatch(ndc) is None:
            continue
        product = f"{DEFAULT_BASE}product/{ndc.rsplit('-', 1)[0]}"
        products.add(product)
        yield Candidate("PackagedProduct", {"ndc": ndc, "product": product}, "dailymed-ndcs", ("dailymed ndc index",))
    if products:
        yield Candidate("Label", {"set_id": set_id, "products": sorted(products)}, "dailymed-ndcs", ("ndc index",))


def spl_documents(spl_dir: Path, set_ids: Sequence[str]) -> list[SplDocument]:
    from monograph.extract.sources.dailymed import parse_spl

    return [parse_spl((spl_dir / f"{s}.xml").read_bytes()) for s in set_ids]
