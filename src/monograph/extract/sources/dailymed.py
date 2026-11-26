"""DailyMed SPL documents: a cached, rate-limited fetcher and a parser that splits each document into
narrative input (what the LLM may see) and gold (the structured product data in the same document).

The split is the core of H4's honesty: the LLM sees only the document title and the `<text>` of a fixed
list of narrative sections. Everything in the structured product data elements (`<subject>`,
`<manufacturedProduct>`, codes, quantities) is gold and never reaches a prompt. `tests/test_h4_leak.py`
proves it with sentinel values.

Endpoint (checked against https://dailymed.nlm.nih.gov/dailymed/app-support-web-services.cfm):
  GET /dailymed/services/v2/spls/{SETID}.xml     the SPL document for a set ID
  GET /dailymed/services/v2/spls.json?ndc=...     SPLs that list an NDC
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
NS = {"v3": "urn:hl7-org:v3"}
_V3 = "{urn:hl7-org:v3}"

UNII_SYSTEM = "2.16.840.1.113883.4.9"
NDC_SYSTEM = "2.16.840.1.113883.6.69"
ACTIVE_CLASSES = {"ACTIB", "ACTIM", "ACTIR"}  # active ingredient by basis of strength, moiety, reference

# Narrative sections the LLM may read, by LOINC code. Order is the order they appear in the prompt.
NARRATIVE_SECTIONS: dict[str, str] = {
    "34089-3": "DESCRIPTION",
    "43678-2": "DOSAGE FORMS AND STRENGTHS",
    "55106-9": "ACTIVE INGREDIENT",
    "34069-5": "HOW SUPPLIED",
    "44425-7": "STORAGE AND HANDLING",
    "51945-4": "PACKAGE LABEL PRINCIPAL DISPLAY PANEL",
}


# ------------------------------------------------------------------------------------------- fetch


class SplCache:
    """`data/raw/spl/<setid>.xml`, written once, then only read."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, set_id: str) -> Path:
        return self._directory / f"{set_id}.xml"

    def read(self, set_id: str) -> bytes | None:
        path = self.path(set_id)
        return path.read_bytes() if path.exists() else None

    def write(self, set_id: str, data: bytes) -> None:
        path = self.path(set_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)

    def lookup_path(self, ndc: str) -> Path:
        return self._directory / "ndc-lookup" / f"{ndc}.json"

    def listing_path(self, doctype: str, pagesize: int, page: int) -> Path:
        return self._directory / "listing" / f"{doctype}-{pagesize}-{page:05d}.json"


class DailyMedError(RuntimeError):
    """DailyMed did not return what the documented endpoint promises."""


class DailyMedClient:
    def __init__(
        self,
        http: httpx.Client,
        cache: SplCache,
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
        self.server_errors: list[str] = []

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        for attempt in range(4):
            wait = self._last + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            self.network_calls += 1
            response = self._http.get(url, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                self._sleep(2.0 * (attempt + 1))
                continue
            return response
        return response  # still failing after retries; the caller decides what that means

    def get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        response = self._get(url, params)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def spl(self, set_id: str) -> bytes | None:
        """The SPL XML for a set ID, or None if DailyMed has no such document."""
        cached = self._cache.read(set_id)
        if cached is not None:
            return cached
        response = self._get(f"{BASE}/spls/{set_id}.xml")
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            self.server_errors.append(set_id)  # recorded, not cached, so a later run can retry it
            return None
        response.raise_for_status()
        if response.content.strip() in (b"", b"None"):
            return None  # DailyMed answers 200 with the body "None" for some set IDs it does not hold
        if b"urn:hl7-org:v3" not in response.content[:2000]:
            raise DailyMedError(f"{set_id}: response is not an HL7 v3 SPL document; has the endpoint changed?")
        self._cache.write(set_id, response.content)
        return response.content

    def set_ids_for_ndc(self, ndc: str) -> list[str]:
        path = self._cache.lookup_path(ndc)
        if path.exists():
            data = json.loads(path.read_text())
        else:
            response = self._get(f"{BASE}/spls.json", params={"ndc": ndc})
            response.raise_for_status()
            data = response.json()
            if "data" not in data:
                raise DailyMedError("spls.json has no 'data' field; has the endpoint changed?")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, sort_keys=True))
        return sorted({str(item["setid"]) for item in data.get("data", [])})


# ------------------------------------------------------------------------------------------- parse


@dataclass(frozen=True, slots=True)
class NarrativeSection:
    code: str
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class GoldIngredient:
    unii: str
    name: str
    numerator_value: Decimal | None
    numerator_unit: str | None
    denominator_value: Decimal | None
    denominator_unit: str | None


@dataclass(frozen=True, slots=True)
class GoldProduct:
    product_ndc: str | None
    name: str
    dosage_form: str | None
    routes: tuple[str, ...]
    active: tuple[GoldIngredient, ...]
    inactive: tuple[tuple[str, str], ...]  # (UNII, name); used only to build the substance registry
    packages: tuple[str, ...]
    is_kit: bool


@dataclass(frozen=True, slots=True)
class SplDocument:
    set_id: str
    version: int | None
    document_type: str
    title: str
    sections: tuple[NarrativeSection, ...]
    products: tuple[GoldProduct, ...] = field(default=())
    legacy_schema: bool = False  # pre-2009 SPL (manufacturedMedicine); not parsed for gold, excluded from H4


def parse_spl(data: bytes) -> SplDocument:
    root = ET.fromstring(data)
    set_id_el = root.find("v3:setId", NS)
    set_id = set_id_el.get("root", "") if set_id_el is not None else ""
    version_el = root.find("v3:versionNumber", NS)
    version = (
        int(version_el.get("value", "")) if version_el is not None and version_el.get("value", "").isdigit() else None
    )
    doc_code = root.find("v3:code", NS)
    document_type = doc_code.get("displayName", "") if doc_code is not None else ""
    title_el = root.find("v3:title", NS)
    title = _clean(_render(title_el)) if title_el is not None else ""
    return SplDocument(
        set_id=set_id.lower(),
        version=version,
        document_type=document_type,
        title=title,
        sections=tuple(_narrative(root)),
        products=tuple(_products(root)),
        legacy_schema=next(root.iter(_V3 + "manufacturedMedicine"), None) is not None,
    )


def _narrative(root: ET.Element) -> Iterator[NarrativeSection]:
    """Top-most sections whose LOINC code is whitelisted, with all their subsections' text.

    Only `<title>` and `<text>` children are read. Structured children of a section (`<subject>`,
    `<entry>`) are never read.
    """
    seen: set[int] = set()
    found: list[tuple[int, NarrativeSection]] = []
    order = list(NARRATIVE_SECTIONS)
    for section in root.iter(_V3 + "section"):
        if id(section) in seen:
            continue
        code = section.find("v3:code", NS)
        loinc = code.get("code") if code is not None else None
        if loinc not in NARRATIVE_SECTIONS:
            continue
        for inner in section.iter(_V3 + "section"):
            seen.add(id(inner))
        text = _clean(_section_text(section))
        if text:
            title_el = section.find("v3:title", NS)
            title = _clean(_render(title_el)) if title_el is not None else NARRATIVE_SECTIONS[loinc]
            found.append((order.index(loinc), NarrativeSection(loinc, title or NARRATIVE_SECTIONS[loinc], text)))
    for _, section_ in sorted(found, key=lambda pair: pair[0]):
        yield section_


def _section_text(section: ET.Element) -> str:
    parts: list[str] = []
    for child in section:
        tag = child.tag.removeprefix(_V3)
        if tag == "text":
            parts.append(_render(child))
        elif tag == "component":
            inner = child.find("v3:section", NS)
            if inner is not None:
                title = inner.find("v3:title", NS)
                if title is not None:
                    parts.append(_render(title))
                parts.append(_section_text(inner))
    return "\n".join(p for p in parts if p.strip())


_BLOCK = {"paragraph", "item", "tr", "caption", "title", "br"}


def _render(element: ET.Element) -> str:
    """Narrative XHTML-like SPL text to plain text: paragraphs and list items on their own lines, table
    cells separated by ' | '. Footnote markers and styling are dropped; their text is kept."""
    out: list[str] = []

    def walk(el: ET.Element) -> None:
        tag = el.tag.removeprefix(_V3)
        if tag == "br":
            out.append("\n")
        if el.text:
            out.append(el.text)
        for child in el:
            walk(child)
            child_tag = child.tag.removeprefix(_V3)
            if child_tag in ("td", "th"):
                out.append(" | ")
            elif child_tag in _BLOCK:
                out.append("\n")
            if child.tail:
                out.append(child.tail)

    walk(element)
    return "".join(out)


_SPACES = re.compile("[ \t" + chr(0xA0) + "]+")  # space, tab, no-break space


def _clean(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip(" |") for line in text.splitlines()]
    return "\n".join(line.strip() for line in lines if line.strip())


def _text_of(el: ET.Element | None) -> str:
    # An ElementTree element with no children is falsy, so `el or default` would drop it: test for None.
    return "" if el is None else _clean(_render(el))


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


PRODUCT_DATA_SECTION = "48780-1"  # SPL product data elements: the only place gold is read from


def _products(root: ET.Element) -> Iterator[GoldProduct]:
    for section in root.iter(_V3 + "section"):
        code = section.find("v3:code", NS)
        if code is None or code.get("code") != PRODUCT_DATA_SECTION:
            continue
        yield from _section_products(section)


def _section_products(section: ET.Element) -> Iterator[GoldProduct]:
    for subject in section.findall("v3:subject", NS):
        outer = subject.find("v3:manufacturedProduct", NS)
        if outer is None:
            continue
        product = outer.find("v3:manufacturedProduct", NS)
        if product is None:
            continue
        yield _product(outer, product)


def _code(el: ET.Element | None, system: str | None = None) -> str | None:
    if el is None:
        return None
    if system is not None and el.get("codeSystem") != system:
        return None
    return el.get("code")


def _product(outer: ET.Element, product: ET.Element) -> GoldProduct:
    ndc = _code(product.find("v3:code", NS), NDC_SYSTEM)
    name = _text_of(product.find("v3:name", NS))
    form = product.find("v3:formCode", NS)
    routes = tuple(sorted({r.get("displayName", "") for r in outer.iter(_V3 + "routeCode") if r.get("displayName")}))
    active: list[GoldIngredient] = []
    inactive: list[tuple[str, str]] = []
    parts = product.findall("v3:part", NS)
    containers = [product, *(p.find("v3:partProduct", NS) for p in parts)]
    for container in containers:
        if container is None:
            continue
        for ingredient in container.findall("v3:ingredient", NS):
            substance = ingredient.find("v3:ingredientSubstance", NS)
            if substance is None:
                continue
            unii = _code(substance.find("v3:code", NS), UNII_SYSTEM) or ""
            sname = _text_of(substance.find("v3:name", NS))
            if ingredient.get("classCode") in ACTIVE_CLASSES:
                num = ingredient.find("v3:quantity/v3:numerator", NS)
                den = ingredient.find("v3:quantity/v3:denominator", NS)
                active.append(
                    GoldIngredient(
                        unii,
                        sname,
                        _decimal(num.get("value")) if num is not None else None,
                        num.get("unit") if num is not None else None,
                        _decimal(den.get("value")) if den is not None else None,
                        den.get("unit") if den is not None else None,
                    )
                )
            elif unii:
                inactive.append((unii, sname))
    packages = sorted(
        {
            code
            for pkg in product.iter(_V3 + "containerPackagedProduct")
            if (code := _code(pkg.find("v3:code", NS), NDC_SYSTEM))
        }
    )
    return GoldProduct(
        product_ndc=ndc,
        name=name,
        dosage_form=form.get("displayName") if form is not None else None,
        routes=routes,
        active=tuple(active),
        inactive=tuple(inactive),
        packages=tuple(packages),
        is_kit=bool(parts),
    )


# ---------------------------------------------------------------------------------- prompt input


def narrative_text(doc: SplDocument) -> str:
    """The only text an extraction prompt may contain about a label. Built from title and sections."""
    blocks = [f"TITLE\n{doc.title}"] if doc.title else []
    blocks += [f"{s.title.upper()}\n{s.text}" for s in doc.sections]
    return "\n\n".join(blocks)


def to_json(doc: SplDocument) -> dict[str, Any]:
    """A plain, deterministic dict form (Decimals as strings) for fixtures and caches."""

    def plain(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, tuple | list):
            return [plain(v) for v in value]
        if hasattr(value, "__slots__"):
            return {k: plain(getattr(value, k)) for k in value.__slots__}
        return value

    return plain(doc)  # type: ignore[no-any-return]


def listing_page(client: DailyMedClient, doctype: str, page: int, pagesize: int, cache: SplCache) -> dict[str, Any]:
    """One page of `/spls.json?doctype=...`, cached under data/raw/spl/listing/."""
    path = cache.listing_path(doctype, pagesize, page)
    if path.exists():
        cached: dict[str, Any] = json.loads(path.read_text())
        return cached
    data = client.get_json(f"{BASE}/spls.json", {"doctype": doctype, "pagesize": str(pagesize), "page": str(page)})
    if "data" not in data or "metadata" not in data:
        raise DailyMedError("spls.json listing lacks data/metadata; has the endpoint changed?")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True))
    return data
