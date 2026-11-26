"""openFDA ingest: a cached, rate-limited client and pure mappers from raw records to candidate records.

The client never runs in CI: CI reads the committed sample in `fixtures/openfda/`. The mappers turn raw
JSON into JSON-shaped candidate records (the same shape as the curated fixtures). They only re-encode
(for example `20240501` to `2024-05-01`) and select; they never repair data to make it pass. When the
source cannot fill a field unambiguously the field is left out and a note says why, so validation
reports the gap.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from monograph.core.meta import DEFAULT_BASE

API = "https://api.fda.gov/"
ENDPOINTS: dict[str, str] = {
    "label": "drug/label.json",
    "event": "drug/event.json",
    "ndc": "drug/ndc.json",
    "enforcement": "drug/enforcement.json",
}
SAMPLE_SIZES: dict[str, int] = {"label": 200, "event": 500, "ndc": 200, "enforcement": 200}

# openFDA label fields and their LOINC section codes (from the SPL section code system).
LABEL_SECTIONS: dict[str, tuple[str, str]] = {
    "boxed_warning": ("34066-1", "BOXED WARNING"),
    "indications_and_usage": ("34067-9", "INDICATIONS AND USAGE"),
    "dosage_and_administration": ("34068-7", "DOSAGE AND ADMINISTRATION"),
    "how_supplied": ("34069-5", "HOW SUPPLIED"),
    "contraindications": ("34070-3", "CONTRAINDICATIONS"),
    "warnings": ("34071-1", "WARNINGS"),
    "drug_interactions": ("34073-7", "DRUG INTERACTIONS"),
    "adverse_reactions": ("34084-4", "ADVERSE REACTIONS"),
    "overdosage": ("34088-5", "OVERDOSAGE"),
    "description": ("34089-3", "DESCRIPTION"),
    "clinical_pharmacology": ("34090-1", "CLINICAL PHARMACOLOGY"),
    "pregnancy": ("42228-7", "PREGNANCY"),
    "spl_unclassified_section": ("42229-5", "SPL UNCLASSIFIED SECTION"),
    "precautions": ("42232-9", "PRECAUTIONS"),
    "mechanism_of_action": ("43679-0", "MECHANISM OF ACTION"),
    "use_in_specific_populations": ("43684-0", "USE IN SPECIFIC POPULATIONS"),
    "warnings_and_cautions": ("43685-7", "WARNINGS AND PRECAUTIONS"),
    "storage_and_handling": ("44425-7", "STORAGE AND HANDLING"),
    "keep_out_of_reach_of_children": ("50565-1", "KEEP OUT OF REACH OF CHILDREN"),
    "stop_use": ("50566-9", "STOP USE"),
    "when_using": ("50567-7", "WHEN USING"),
    "ask_doctor": ("50569-3", "ASK DOCTOR"),
    "do_not_use": ("50570-1", "DO NOT USE"),
    "package_label_principal_display_panel": ("51945-4", "PACKAGE LABEL PRINCIPAL DISPLAY PANEL"),
    "inactive_ingredient": ("51727-6", "INACTIVE INGREDIENT"),
    "questions": ("53413-1", "QUESTIONS"),
    "pregnancy_or_breast_feeding": ("53414-9", "PREGNANCY OR BREAST FEEDING"),
    "purpose": ("55105-1", "PURPOSE"),
    "active_ingredient": ("55106-9", "ACTIVE INGREDIENT"),
}

SERIOUSNESS_FIELDS: dict[str, str] = {
    "seriousnessdeath": "death",
    "seriousnesslifethreatening": "life threatening",
    "seriousnesshospitalization": "hospitalization",
    "seriousnessdisabling": "disabling",
    "seriousnesscongenitalanomali": "congenital anomaly",
    "seriousnessother": "other",
}


# --------------------------------------------------------------------------------------------- client


class RawCache:
    """One JSON file per page under `data/raw/<endpoint>/`. Pages are written once and then only read."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, endpoint: str, skip: int, limit: int) -> Path:
        return self._directory / endpoint / f"skip-{skip:06d}-limit-{limit:04d}.json"

    def read(self, endpoint: str, skip: int, limit: int) -> list[dict[str, Any]] | None:
        path = self.path(endpoint, skip, limit)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        return data

    def write(self, endpoint: str, skip: int, limit: int, records: list[dict[str, Any]]) -> None:
        path = self.path(endpoint, skip, limit)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")

    def all(self, endpoint: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted((self._directory / endpoint).glob("skip-*.json")):
            out += json.loads(path.read_text(encoding="utf-8"))
        return out


class OpenFdaClient:
    """openFDA allows 240 requests per minute (and 1,000 per day without a key). We stay well under."""

    def __init__(
        self,
        http: httpx.Client,
        cache: RawCache,
        api_key: str | None,
        min_interval_s: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self._http = http
        self._cache = cache
        self._api_key = api_key
        self._min_interval = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last = float("-inf")
        self.network_calls = 0

    def page(self, endpoint: str, skip: int, limit: int) -> list[dict[str, Any]]:
        cached = self._cache.read(endpoint, skip, limit)
        if cached is not None:
            return cached
        params: dict[str, str | int] = {"limit": limit, "skip": skip}
        if self._api_key:
            params["api_key"] = self._api_key
        for attempt in range(5):
            wait = self._last + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            self.network_calls += 1
            response = self._http.get(API + ENDPOINTS[endpoint], params=params)
            if response.status_code == 429:
                self._sleep(2.0 * (attempt + 1))
                continue
            if response.status_code == 404:
                records: list[dict[str, Any]] = []  # openFDA answers 404 when skip runs past the results
                break
            response.raise_for_status()
            records = response.json()["results"]
            break
        else:
            raise RuntimeError(f"openFDA rate limit persisted for {endpoint} skip={skip}")
        self._cache.write(endpoint, skip, limit, records)
        return records

    def sample(self, endpoint: str, total: int, page_size: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for skip in range(0, total, page_size):
            page = self.page(endpoint, skip, min(page_size, total - skip))
            out += page
            if not page:
                break
        return out


# -------------------------------------------------------------------------------------------- mappers


@dataclass(frozen=True, slots=True)
class Candidate:
    cls: str
    record: dict[str, Any]
    source: str
    notes: tuple[str, ...] = ()


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def fda_date(value: object) -> object:
    """openFDA writes dates as YYYYMMDD. Re-encode to ISO; leave anything else untouched."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass
class _Notes:
    items: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.items.append(text)


class OpenFdaMapper:
    def __init__(self, base: str = DEFAULT_BASE) -> None:
        self._base = base

    def _iri(self, path: str, key: str) -> str:
        from urllib.parse import quote

        return f"{self._base}{path}/{quote(key, safe='')}"

    def _single_rxcui(self, openfda: object, notes: _Notes, what: str) -> str | None:
        rxcuis = _list(openfda.get("rxcui") if isinstance(openfda, dict) else None)
        if len(rxcuis) == 1:
            return str(rxcuis[0])
        notes.add(f"{what}: openfda.rxcui has {len(rxcuis)} values")
        return None

    def organization(self, name: object, source: str) -> Candidate:
        text = name if isinstance(name, str) else ""
        return Candidate("Organization", {"slug": slugify(text), "name": text}, source)

    def map(self, endpoint: str, raw: dict[str, Any]) -> list[Candidate]:
        return {"ndc": self.ndc, "label": self.label, "event": self.event, "enforcement": self.enforcement}[endpoint](
            raw
        )

    def ndc(self, raw: dict[str, Any]) -> list[Candidate]:
        source = f"ndc:{raw.get('product_id', raw.get('product_ndc', '?'))}"
        notes = _Notes()
        openfda = raw.get("openfda") or {}
        out = [self.organization(raw.get("labeler_name"), source)]
        uniis = [str(u) for u in _list(openfda.get("unii"))]
        active = [a for a in _list(raw.get("active_ingredients")) if isinstance(a, dict)]
        # NDC records carry UNIIs without names, in no stated order. Only a single ingredient with a single
        # UNII pairs unambiguously; anything else is left unresolved and noted.
        single = len(active) == 1 and len(uniis) == 1
        if single:
            out.append(Candidate("Substance", {"unii": uniis[0], "name": active[0].get("name")}, source))
        ingredients = []
        for a in active:
            ingredient: dict[str, Any] = {}
            if single:
                ingredient["substance"] = self._iri("substance", uniis[0])
            else:
                notes.add(f"ingredient: {len(active)} ingredients and {len(uniis)} UNIIs cannot be paired")
            strength = str(a.get("strength", ""))
            match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s+(\S.*?)\s*", strength)
            if match:
                ingredient["strength_value"] = match.group(1)
                ingredient["strength_unit"] = match.group(2)
            else:
                notes.add(f"ingredient {a.get('name')!r}: strength {strength!r} is not 'number unit'")
            ingredients.append(ingredient)
        rxcui = self._single_rxcui(openfda, notes, "product")
        product: dict[str, Any] = {
            "name": raw.get("generic_name") or raw.get("brand_name"),
            "dosage_form": raw.get("dosage_form"),
            "routes": _list(raw.get("route")),
            "ingredients": ingredients,
        }
        if rxcui is not None:
            product["rxcui"] = rxcui
        out.append(Candidate("Product", product, source, tuple(notes.items)))
        labeler = self._iri("organization", slugify(str(raw.get("labeler_name", ""))))
        for package in _list(raw.get("packaging")):
            record: dict[str, Any] = {"ndc": package.get("package_ndc"), "labeler": labeler}
            if rxcui is not None:
                record["product"] = self._iri("product", rxcui)
            if package.get("description") is not None:
                record["description"] = package.get("description")
            pkg_notes = () if rxcui is not None else ("product: no single RxCUI to reference",)
            out.append(Candidate("PackagedProduct", record, source, pkg_notes))
        return out

    def label(self, raw: dict[str, Any]) -> list[Candidate]:
        source = f"label:{raw.get('id', '?')}"
        version = raw.get("version")
        openfda = raw.get("openfda") or {}
        record: dict[str, Any] = {
            "set_id": raw.get("set_id"),
            "version": int(version) if isinstance(version, str) and version.isdigit() else version,
            "effective_date": fda_date(raw.get("effective_time")),
            "products": sorted({self._iri("product", str(r)) for r in _list(openfda.get("rxcui"))}),
            "sections": [
                {"code": code, "title": title, "text": "\n".join(str(t) for t in _list(raw.get(name))).strip()}
                for name, (code, title) in LABEL_SECTIONS.items()
                if name in raw
            ],
        }
        uniis = [str(u) for u in _list(openfda.get("unii"))]
        names = [str(n) for n in _list(openfda.get("substance_name"))]
        substances = []
        if len(uniis) == len(names) == 1:  # longer lists are not documented as aligned
            substances.append(Candidate("Substance", {"unii": uniis[0], "name": names[0]}, source))
        unmapped = sorted(k for k in raw if k not in LABEL_SECTIONS and isinstance(raw[k], list) and k != "openfda")
        notes = (f"label: {len(unmapped)} text fields without a LOINC mapping",) if unmapped else ()
        return [*substances, Candidate("Label", record, source, notes)]

    def event(self, raw: dict[str, Any]) -> list[Candidate]:
        source = f"event:{raw.get('safetyreportid', '?')}"
        notes = _Notes()
        patient = raw.get("patient") or {}
        drugs_raw = [d for d in _list(patient.get("drug")) if isinstance(d, dict)]
        suspects = sum(1 for d in drugs_raw if d.get("drugcharacterization") == "1")
        if suspects > 1:
            notes.add(f"drugs: {suspects} suspects and E2B does not say which is primary")
        drugs = []
        for d in drugs_raw:
            code = d.get("drugcharacterization")
            role = {
                "1": "primary suspect" if suspects == 1 else "suspect",
                "2": "concomitant",
                "3": "interacting",
            }.get(str(code), code)
            drug: dict[str, Any] = {"drug_name": d.get("medicinalproduct"), "role": role}
            rxcuis = _list((d.get("openfda") or {}).get("rxcui"))
            if len(rxcuis) == 1:
                drug["product"] = self._iri("product", str(rxcuis[0]))
            drugs.append(drug)
        serious = {"1": True, "2": False}.get(str(raw.get("serious")), raw.get("serious"))
        record: dict[str, Any] = {
            "report_id": raw.get("safetyreportid"),
            "receipt_date": fda_date(raw.get("receivedate")),
            "serious": serious,
            "seriousness_criteria": sorted(v for k, v in SERIOUSNESS_FIELDS.items() if raw.get(k) == "1"),
            "drugs": drugs,
            "reactions": [{"term": r.get("reactionmeddrapt")} for r in _list(patient.get("reaction"))],
        }
        age, unit = patient.get("patientonsetage"), patient.get("patientonsetageunit")
        if age is not None and unit == "801":
            record["patient_age_years"] = str(age)
        elif age is not None:
            notes.add(f"patient age: unit {unit!r} is not years; left out")
        return [Candidate("AdverseEventReport", record, source, tuple(notes.items))]

    def enforcement(self, raw: dict[str, Any]) -> list[Candidate]:
        source = f"enforcement:{raw.get('recall_number', '?')}"
        firm = raw.get("recalling_firm")
        classification = raw.get("classification")
        if isinstance(classification, str) and classification.startswith("Class "):
            classification = classification.removeprefix("Class ")
        openfda = raw.get("openfda") or {}
        record: dict[str, Any] = {
            "recall_number": raw.get("recall_number"),
            "classification": classification,
            "reason": raw.get("reason_for_recall"),
            "report_date": fda_date(raw.get("report_date")),
            "products": sorted({self._iri("product", str(r)) for r in _list(openfda.get("rxcui"))}),
            "recalling_firm": self._iri("organization", slugify(str(firm or ""))),
        }
        return [self.organization(firm, source), Candidate("Recall", record, source)]


FIXTURE_COUNTS: dict[str, int] = {"ndc": 12, "label": 10, "event": 16, "enforcement": 12}
_MAX_FIXTURE_BYTES = 20_000


def _source_id(endpoint: str, raw: dict[str, Any]) -> str:
    key = {"ndc": "product_id", "label": "id", "event": "safetyreportid", "enforcement": "recall_number"}[endpoint]
    return str(raw.get(key, ""))


def select_fixture_records(
    raw: dict[str, list[dict[str, Any]]],
    conforms: Callable[[str, dict[str, Any]], bool],
    counts: dict[str, int] = FIXTURE_COUNTS,
) -> dict[str, list[dict[str, Any]]]:
    """Pick a small, deterministic, representative subset for offline CI.

    Records are sorted by source ID and taken at even spacing, after dropping records over 20 kB (long
    labels) to keep the repository small. At least a quarter of each endpoint's pick is non-conformant
    when the sample has that many, so CI keeps exercising real failures.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for endpoint, n in counts.items():
        pool = sorted(
            (r for r in raw.get(endpoint, []) if len(json.dumps(r)) <= _MAX_FIXTURE_BYTES),
            key=lambda r: _source_id(endpoint, r),
        )
        bad = [r for r in pool if not conforms(endpoint, r)]
        good = [r for r in pool if conforms(endpoint, r)]
        want_bad = min(len(bad), max(1, n // 4))
        chosen = _spaced(bad, want_bad) + _spaced(good, n - want_bad)
        out[endpoint] = sorted(chosen, key=lambda r: _source_id(endpoint, r))
    return out


def _spaced(items: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]
