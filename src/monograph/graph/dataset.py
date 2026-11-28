"""Build the H2 dataset: validated H6 instances from the cached H6, H4 and frame data, plus the identity layer.

No network access: every input is read from `data/raw/`. Rules (see docs/h2/PLAN.md):

* Products come from NDC directory records (H6 mapper). When the record cannot pair UNIIs with
  ingredients (more than one ingredient), the ingredients come from the product's SPL structured part,
  which pairs name, UNII and strength. The source used is counted per product.
* Organizations are keyed by NDC labeler code: one firm per code, canonical name = the name on most of
  its products (ties by sort order). Other names for the code (NDC directory, RxNav) are aliases. A
  recalling firm is that labeler when its normalized name equals one of the labeler's names and the
  recall names one of the labeler's products; otherwise it is its own organization, as in H6.
* Every entity is validated by the Pydantic model; rejects are counted by reason and never repaired.
  A reference to an entity that was rejected or never fetched is dropped and counted, because the
  database's foreign keys would otherwise reject the referencing row.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from monograph.core.instances import Instantiator
from monograph.core.meta import DEFAULT_BASE
from monograph.core.spec import SchemaSpec
from monograph.extract.registry import nested_packages
from monograph.extract.sources.dailymed import GoldProduct, parse_spl
from monograph.graph.identity import labeler_code, ndc11, normalize_firm
from monograph.graph.spl import MoietyFact, moiety_facts
from monograph.ingest.openfda import OpenFdaMapper, RawCache, slugify
from monograph.ingest.rxnav import RxNavClient
from monograph.validate.pydantic_side import PydanticValidator

BRANDED_GENERIC = {"SBD": "SCD", "BPCK": "GPCK"}

# Load order: every reference points to a class earlier in this list (moiety and generic links point
# within their own class and are ordered inside it).
LOAD_ORDER = (
    "Substance",
    "Organization",
    "ClinicalDrug",
    "Product",
    "PackagedProduct",
    "Label",
    "AdverseEventReport",
    "Recall",
)


def iri(path: str, key: str) -> str:
    from urllib.parse import quote

    return f"{DEFAULT_BASE}{path}/{quote(key, safe='')}"


@dataclass(frozen=True, slots=True)
class RawInputs:
    ndc: list[dict[str, Any]]
    label: list[dict[str, Any]]
    event: list[dict[str, Any]]
    enforcement: list[dict[str, Any]]


def _dedupe(records: Iterable[dict[str, Any]], key: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        k = key(r)
        if k and k not in out:
            out[k] = r
    return [out[k] for k in sorted(out)]


def read_raw(raw_dir: Path) -> RawInputs:
    """H6 sample pages plus every cached search page (H4 registry and the H2 frame), deduplicated."""
    cache = RawCache(raw_dir)

    def everything(endpoint: str) -> list[dict[str, Any]]:
        out = list(cache.all(endpoint))
        for path in sorted((raw_dir / endpoint / "search").glob("*.json")):
            out += json.loads(path.read_text(encoding="utf-8"))
        return out

    return RawInputs(
        ndc=_dedupe(everything("ndc"), lambda r: f"{r.get('product_ndc', '')}"),
        label=_dedupe(everything("label"), lambda r: f"{str(r.get('set_id', '')).lower()}|{r.get('version', '')}"),
        event=_dedupe(everything("event"), lambda r: str(r.get("safetyreportid", ""))),
        enforcement=_dedupe(everything("enforcement"), lambda r: str(r.get("recall_number", ""))),
    )


@dataclass
class Dataset:
    instances: dict[str, list[BaseModel]] = field(default_factory=dict)
    rejected: Counter[str] = field(default_factory=Counter)
    dropped_refs: Counter[str] = field(default_factory=Counter)
    stats: Counter[str] = field(default_factory=Counter)

    def count(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.instances.items()}

    def summary(self) -> dict[str, Any]:
        return {
            "instances": self.count(),
            "rejected": dict(sorted(self.rejected.items())),
            "dropped_refs": dict(sorted(self.dropped_refs.items())),
            "stats": dict(sorted(self.stats.items())),
        }


def _spl_strength_unit(num_unit: str | None, den_value: Any, den_unit: str | None) -> str | None:
    """NDC directory style: mg/1, mg/mL, mg/5mL."""
    if not num_unit or den_value is None or not den_unit:
        return None
    den = den_unit if str(den_value) in ("1", "1.0") else f"{den_value}{den_unit}"
    return f"{num_unit}/{den}"


class DatasetBuilder:
    def __init__(
        self,
        spec: SchemaSpec,
        instantiator: Instantiator,
        validator: PydanticValidator,
        rxnav: RxNavClient,
        spl: Callable[[str], bytes | None],
        identity: bool = True,
    ) -> None:
        """`identity=False` builds the H2.1 identity-off data: the same records without the identity layer
        (`monograph.ablation.cells.IDENTITY_FIELDS`), and recalling firms never merged into labelers."""
        self._identity = identity
        self._spec = spec
        self._instantiator = instantiator
        self._validator = validator
        self._rxnav = rxnav
        self._spl = spl
        self._mapper = OpenFdaMapper()
        self._ndc_names: dict[str, Counter[str]] = defaultdict(Counter)

    # ---------------------------------------------------------------------------------- validation

    def _accept(self, ds: Dataset, cls_name: str, record: dict[str, Any]) -> BaseModel | None:
        cls = self._spec.by_name(cls_name).cls
        instance = self._instantiator.instantiate(cls, record)
        verdict = self._validator.check(instance)
        if not verdict.valid:
            v = verdict.violations[0]
            ds.rejected[f"{cls_name}: {v.path.split('.')[0]}: {v.kind}"] += 1
            return None
        return instance

    # ----------------------------------------------------------------------------------- building

    def build(self, raw: RawInputs) -> Dataset:
        ds = Dataset()
        rx_version = str(self._rxnav.version().get("version", "unknown"))
        spls = self._spl_documents(raw.ndc)
        facts = [f for data in spls.values() for f in moiety_facts(data)]
        gold = {set_id: parse_spl(data) for set_id, data in spls.items()}

        products, packages, product_sources = self._products(ds, raw.ndc, gold, rx_version)
        orgs = self._organizations(raw.ndc, products)
        recall_records = self._recalls(raw.enforcement, orgs)
        substances = self._substances(ds, products, gold, facts)
        clinical = self._clinical_drugs(ds, products, raw.event, rx_version)

        org_records = [orgs.record(slug) for slug in sorted(orgs.names)]
        if not self._identity:
            org_records = self._without_identity(ds, products, packages, substances, clinical, recall_records, orgs)

        accepted: dict[str, list[BaseModel]] = defaultdict(list)
        known: dict[str, set[str]] = defaultdict(set)

        def take(cls_name: str, records: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], str]) -> None:
            for record in records:
                instance = self._accept(ds, cls_name, record)
                if instance is not None:
                    accepted[cls_name].append(instance)
                    known[cls_name].add(key(record))

        take("Substance", substances, lambda r: iri("substance", r["unii"]))
        take("Organization", org_records, lambda r: iri("organization", r["slug"]))
        take("ClinicalDrug", clinical, lambda r: iri("clinical-drug", r["rxcui"]))
        products = [p for p in products if self._refs_ok(ds, "Product.ingredients", p, known)]
        take("Product", products, lambda r: iri("product", r["product_ndc"]))
        for p in packages:
            p["labeler"] = iri("organization", orgs.slug_for_code(labeler_code(p["ndc"])) or "")
        packages = [p for p in packages if self._package_ok(ds, p, known)]
        take("PackagedProduct", packages, lambda r: iri("packaged-product", r["ndc"]))
        labels = self._labels(ds, raw.label, known)
        take("Label", labels, lambda r: f"{r['set_id']}|{r['version']}")
        events = self._events(raw.event)
        if not self._identity:
            for e in events:
                e["drugs"] = [
                    {k: v for k, v in d.items() if k not in ("clinical_drug", "harmonized_clinical_drugs")}
                    for d in e["drugs"]
                ]
        take("AdverseEventReport", events, lambda r: iri("adverse-event-report", r["report_id"]))
        for r in recall_records:
            r["products"] = self._filter(ds, "Recall.products", list(r["products"]), known["Product"])
        take("Recall", recall_records, lambda r: iri("recall", r["recall_number"]))
        ds.instances = {k: accepted[k] for k in LOAD_ORDER}
        ds.stats.update(product_sources)
        if self._identity:
            ds.stats["organization aliases"] = sum(len(a) for a in orgs.aliases.values())
            ds.stats["recall firms resolved to a labeler"] = orgs.recall_matches
        return ds

    # ------------------------------------------------------------------------------- identity off

    def _without_identity(
        self,
        ds: Dataset,
        products: list[dict[str, Any]],
        packages: list[dict[str, Any]],
        substances: list[dict[str, Any]],
        clinical: list[dict[str, Any]],
        recalls: list[dict[str, Any]],
        orgs: _Orgs,
    ) -> list[dict[str, Any]]:
        """Strip the identity layer in place and return the organizations as each source identifies them.

        A labeler is identified as the NDC directory identifies it, by labeler code: the same organizations
        (canonical name, same IRI) as with identity on, without aliases. A recalling firm is identified as an
        enforcement record identifies it, by name: one organization per H6 ingest key (the slug of the name,
        within enforcement records), never merged with a labeler, under `organization/recalling-firm-<slug>`.
        """
        for p in products:
            p.pop("drug_mappings", None)
            p.pop("clinical_drug", None)
        for k in packages:
            k.pop("ndc11", None)
        for sub in substances:
            sub.pop("active_moiety", None)
        for c in clinical:
            c.pop("generic", None)
        labelers = sorted(set(orgs.code_slug.values()))
        out = [{"slug": slug, "name": orgs.names[slug], "aliases": []} for slug in labelers]
        firms: dict[str, Counter[str]] = defaultdict(Counter)
        for r in recalls:
            firm = r.pop("_firm")
            slug = recalling_firm_slug(firm)
            firms[slug][firm] += 1
            r["recalling_firm"] = iri("organization", slug)
        for slug in sorted(firms):
            name = sorted(firms[slug].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            out.append({"slug": slug, "name": name, "aliases": []})
        ds.stats["labeler organizations"] = len(labelers)
        ds.stats["recalling-firm organizations (identity off)"] = len(firms)
        return out

    @staticmethod
    def _filter(ds: Dataset, what: str, refs: Sequence[str], known: set[str]) -> list[str]:
        out = [r for r in refs if r in known]
        ds.dropped_refs[what] += len(refs) - len(out)
        return out

    @staticmethod
    def _refs_ok(ds: Dataset, what: str, product: dict[str, Any], known: Mapping[str, set[str]]) -> bool:
        missing = [i for i in product.get("ingredients", []) if i.get("substance") not in known["Substance"]]
        if missing:
            ds.dropped_refs[f"{what} (product dropped: substance not built)"] += 1
            return False
        return True

    @staticmethod
    def _package_ok(ds: Dataset, package: dict[str, Any], known: Mapping[str, set[str]]) -> bool:
        if package.get("product") not in known["Product"]:
            ds.dropped_refs["PackagedProduct.product (package dropped: product rejected)"] += 1
            return False
        if package.get("labeler") not in known["Organization"]:
            ds.dropped_refs["PackagedProduct.labeler (package dropped)"] += 1
            return False
        return True

    # ------------------------------------------------------------------------------------- SPLs

    def _spl_documents(self, ndc: Sequence[dict[str, Any]]) -> dict[str, bytes]:
        set_ids = sorted({str(s).lower() for r in ndc for s in (r.get("openfda") or {}).get("spl_set_id", [])})
        out = {}
        for set_id in set_ids:
            data = self._spl(set_id)
            if data is not None:
                out[set_id] = data
        return out

    # --------------------------------------------------------------------------------- products

    def _products(
        self, ds: Dataset, ndc: Sequence[dict[str, Any]], gold: Mapping[str, Any], rx_version: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
        products: list[dict[str, Any]] = []
        packages: dict[str, dict[str, Any]] = {}
        sources: Counter[str] = Counter()
        for record in ndc:
            mapped = self._mapper.ndc(record)
            for c in mapped:
                if c.cls == "Substance" and c.record.get("name"):
                    self._ndc_names[str(c.record["unii"])][str(c.record["name"])] += 1
            product = next(c.record for c in mapped if c.cls == "Product")
            product = dict(product)
            if any("substance" not in i for i in product.get("ingredients", [])):
                from_spl = self._spl_ingredients(record, gold)
                if from_spl is not None:
                    product["ingredients"] = from_spl
                    sources["product ingredients from SPL"] += 1
                else:
                    sources["product ingredients unpaired (rejected)"] += 1
            else:
                sources["product ingredients from NDC record"] += 1
            product["drug_mappings"] = self._mappings(record, rx_version)
            products.append(product)
            for c in [*mapped, *nested_packages(mapped, record)]:
                if c.cls == "PackagedProduct":
                    pkg = {k: v for k, v in c.record.items() if k != "labeler"}
                    computed = ndc11(str(pkg.get("ndc", "")))
                    if computed is not None:
                        pkg["ndc11"] = computed
                    packages.setdefault(str(pkg.get("ndc")), pkg)
        # Cross-check the derived 11-digit NDC against RxNav's ndcItem.
        for record in ndc:
            for p in self._rxnav.ndc_properties(str(record.get("product_ndc"))):
                ours = packages.get(str(p.get("ndc10")), {}).get("ndc11")
                if ours is not None:
                    sources["ndc11 checked against RxNav"] += 1
                    if ours != p.get("ndcItem"):
                        sources["ndc11 disagrees with RxNav"] += 1
        return products, [packages[k] for k in sorted(packages)], sources

    def _spl_ingredients(self, record: dict[str, Any], gold: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        product_ndc = record.get("product_ndc")
        for set_id in sorted(str(s).lower() for s in (record.get("openfda") or {}).get("spl_set_id", [])):
            doc = gold.get(set_id)
            if doc is None:
                continue
            match: list[GoldProduct] = [p for p in doc.products if p.product_ndc == product_ndc]
            if len(match) != 1 or not match[0].active:
                continue
            out = []
            for g in match[0].active:
                unit = _spl_strength_unit(g.numerator_unit, g.denominator_value, g.denominator_unit)
                if not g.unii or g.numerator_value is None or unit is None:
                    return None
                out.append(
                    {
                        "substance": iri("substance", g.unii),
                        "strength_value": str(g.numerator_value),
                        "strength_unit": unit,
                    }
                )
            return out
        return None

    def _mappings(self, record: dict[str, Any], rx_version: str) -> list[dict[str, Any]]:
        product_ndc = str(record.get("product_ndc"))
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for rxcui in sorted({str(x) for x in (record.get("openfda") or {}).get("rxcui", [])}):
            out[(rxcui, "openfda_ndc")] = {
                "clinical_drug": iri("clinical-drug", rxcui),
                "source": "openfda_ndc",
                "evidence": f"openFDA NDC directory record {record.get('product_id')}: openfda.rxcui",
            }
        by_rxcui: dict[str, set[str]] = defaultdict(set)
        for p in self._rxnav.ndc_properties(product_ndc):
            if p.get("rxcui"):
                by_rxcui[str(p["rxcui"])].add(str(p.get("ndc10")))
        for rxcui, ndcs in sorted(by_rxcui.items()):
            shown = ", ".join(sorted(ndcs)[:3]) + (" and more" if len(ndcs) > 3 else "")
            out[(rxcui, "rxnav_ndcproperties")] = {
                "clinical_drug": iri("clinical-drug", rxcui),
                "source": "rxnav_ndcproperties",
                "evidence": f"RxNav getNDCProperties id={product_ndc} ndcstatus=ALL (RxNorm {rx_version}): {shown}",
            }
        return [out[k] for k in sorted(out)]

    # ---------------------------------------------------------------------------- organizations

    def _organizations(self, ndc: Sequence[dict[str, Any]], products: Sequence[dict[str, Any]]) -> _Orgs:
        orgs = _Orgs()
        for record in ndc:
            name = record.get("labeler_name")
            product_ndc = str(record.get("product_ndc", ""))
            if isinstance(name, str) and name.strip() and product_ndc:
                orgs.see(labeler_code(product_ndc), name, "ndc_directory", f"NDC directory, product {product_ndc}")
        orgs.settle()
        for record in ndc:
            product_ndc = str(record.get("product_ndc", ""))
            for p in self._rxnav.ndc_properties(product_ndc):
                for prop in (p.get("propertyConceptList") or {}).get("propertyConcept") or []:
                    if prop.get("propName") == "LABELER" and p.get("ndc10"):
                        code = labeler_code(str(p["ndc10"]))
                        orgs.alias(
                            code, str(prop.get("propValue")), "rxnav", f"RxNav getNDCProperties, NDC {p['ndc10']}"
                        )
        return orgs

    def _recalls(self, enforcement: Sequence[dict[str, Any]], orgs: _Orgs) -> list[dict[str, Any]]:
        out = []
        for raw in enforcement:
            record = next(c.record for c in self._mapper.enforcement(raw) if c.cls == "Recall")
            record = dict(record)
            firm = str(raw.get("recalling_firm") or "")
            ndcs = sorted(str(n) for n in (raw.get("openfda") or {}).get("product_ndc", []))
            slug = orgs.recalling_firm(firm, ndcs, str(raw.get("recall_number")))
            record["recalling_firm"] = iri("organization", slug)
            if not self._identity:
                record["_firm"] = firm  # re-keyed as ingested in _without_identity
            out.append(record)
        return out

    # ------------------------------------------------------------------------------- substances

    def _substances(
        self, ds: Dataset, products: Sequence[dict[str, Any]], gold: Mapping[str, Any], facts: Sequence[MoietyFact]
    ) -> list[dict[str, Any]]:
        names: dict[str, Counter[str]] = defaultdict(Counter)
        for doc in gold.values():
            for p in doc.products:
                for g in p.active:
                    if g.unii and g.name:
                        names[g.unii][g.name] += 1
        for f in facts:
            names[f.unii][f.name] += 1
            names[f.moiety_unii][f.moiety_name] += 1
        for unii, counter in self._ndc_names.items():
            names[unii].update(counter)
        wanted = {
            str(i["substance"]).rsplit("/", 1)[1]
            for p in products
            for i in p.get("ingredients", [])
            if "substance" in i
        }
        moieties: dict[str, Counter[str]] = defaultdict(Counter)
        first_source: dict[tuple[str, str], str] = {}
        for f in sorted(facts, key=lambda f: (f.unii, f.moiety_unii, f.set_id)):
            if f.unii != f.moiety_unii:
                moieties[f.unii][f.moiety_unii] += 1
                first_source.setdefault((f.unii, f.moiety_unii), f"spl:{f.set_id}")
        out = []
        for unii in sorted(wanted | {m for u in wanted for m in moieties.get(u, {})}):
            seen = names.get(unii)
            if not seen:
                ds.stats["substances without a name (rejected)"] += 1
                continue
            name = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            record: dict[str, Any] = {"unii": unii, "name": name}
            options = moieties.get(unii)
            if options:
                if len(options) > 1:
                    ds.stats["substances with conflicting moieties (most frequent used)"] += 1
                moiety = sorted(options.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                record["active_moiety"] = {"moiety": iri("substance", moiety), "source": first_source[(unii, moiety)]}
            out.append(record)
        # A moiety link must point to a substance that is built; moieties are added above, so this holds
        # unless the moiety has no name anywhere.
        built = {r["unii"] for r in out}
        for r in out:
            link = r.get("active_moiety")
            if link is not None and link["moiety"].rsplit("/", 1)[1] not in built:
                del r["active_moiety"]
                ds.dropped_refs["Substance.active_moiety (moiety has no name)"] += 1
        # Moieties first, so the loader inserts link targets before the links.
        return sorted(out, key=lambda r: ("active_moiety" in r, r["unii"]))

    # -------------------------------------------------------------------------- clinical drugs

    def _clinical_drugs(
        self, ds: Dataset, products: Sequence[dict[str, Any]], events: Sequence[dict[str, Any]], rx_version: str
    ) -> list[dict[str, Any]]:
        crosswalk = {m["clinical_drug"].rsplit("/", 1)[1] for p in products for m in p.get("drug_mappings", [])}
        crosswalk |= {str(p["clinical_drug"]).rsplit("/", 1)[1] for p in products if p.get("clinical_drug")}
        harmonized = {
            str(x)
            for e in events
            for d in (e.get("patient") or {}).get("drug", []) or []
            if isinstance(d, dict)
            for x in (d.get("openfda") or {}).get("rxcui", [])
        }
        records: dict[str, dict[str, Any]] = {r: {"rxcui": r} for r in sorted(crosswalk | harmonized)}
        for rxcui in sorted(crosswalk):
            props = self._rxnav.properties(rxcui)
            if props is None:
                ds.stats["crosswalk RxCUIs unknown to RxNav"] += 1
                continue
            record = records[rxcui]
            if props.get("name"):
                record["name"] = str(props["name"])
            if props.get("tty"):
                record["term_type"] = str(props["tty"])
            generic_tty = BRANDED_GENERIC.get(str(props.get("tty")))
            if generic_tty is None:
                continue
            related = [c for c in self._rxnav.related_generic(rxcui) if c.get("tty") == generic_tty]
            if len(related) != 1:
                ds.stats[f"branded drugs with {'no' if not related else 'several'} {generic_tty}"] += 1
                continue
            target = str(related[0]["rxcui"])
            records.setdefault(target, {"rxcui": target})
            if related[0].get("name"):
                records[target].setdefault("name", str(related[0]["name"]))
            records[target].setdefault("term_type", generic_tty)
            record["generic"] = {
                "drug": iri("clinical-drug", target),
                "source": f"RxNav getRelatedByType rxcui={rxcui} tty=SCD+GPCK (RxNorm {rx_version})",
            }
        return sorted(records.values(), key=lambda r: ("generic" in r, r["rxcui"]))

    # ----------------------------------------------------------------------------------- labels

    def _labels(
        self, ds: Dataset, labels: Sequence[dict[str, Any]], known: Mapping[str, set[str]]
    ) -> list[dict[str, Any]]:
        out = []
        for raw in labels:
            record = dict(next(c.record for c in self._mapper.label(raw) if c.cls == "Label"))
            record["products"] = self._filter(ds, "Label.products", list(record.get("products", [])), known["Product"])
            out.append(record)
        return out

    # ----------------------------------------------------------------------------------- events

    def _events(self, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for raw in events:
            record = dict(next(c.record for c in self._mapper.event(raw) if c.cls == "AdverseEventReport"))
            drugs_raw = [d for d in (raw.get("patient") or {}).get("drug", []) or [] if isinstance(d, dict)]
            drugs = []
            for drug, d in zip(record.get("drugs", []), drugs_raw, strict=True):
                rxcuis = sorted({str(x) for x in (d.get("openfda") or {}).get("rxcui", [])})
                drugs.append({**drug, "harmonized_clinical_drugs": [iri("clinical-drug", x) for x in rxcuis]})
            record["drugs"] = drugs
            out.append(record)
        return out


RECALLING_FIRM_PREFIX = "recalling-firm-"


def recalling_firm_slug(firm: str) -> str:
    """Identity off: a recalling firm's own key, which cannot collide with a labeler's."""
    slug = slugify(firm)[: 120 - len(RECALLING_FIRM_PREFIX)].rstrip("-") or "unnamed"
    return RECALLING_FIRM_PREFIX + slug


@dataclass
class _Orgs:
    """Organizations keyed by NDC labeler code, with aliases."""

    by_code: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    evidence: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    code_slug: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # slug -> canonical name
    aliases: dict[str, dict[str, tuple[str, str]]] = field(default_factory=lambda: defaultdict(dict))
    recall_matches: int = 0

    def see(self, code: str, name: str, source: str, evidence: str) -> None:
        self.by_code[code][name] += 1
        self.evidence.setdefault((code, name), (source, f"{evidence}, labeler code {code}"))

    def settle(self) -> None:
        for code in sorted(self.by_code):
            canonical = sorted(self.by_code[code].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            slug = slugify(canonical)
            self.code_slug[code] = slug
            self.names.setdefault(slug, canonical)
            for name in sorted(self.by_code[code]):
                source, evidence = self.evidence[(code, name)]
                self._add_alias(slug, name, source, evidence)

    def _add_alias(self, slug: str, name: str, source: str, evidence: str) -> None:
        if name != self.names[slug] and name not in self.aliases[slug]:
            self.aliases[slug][name] = (source, evidence)

    def alias(self, code: str, name: str, source: str, evidence: str) -> None:
        slug = self.code_slug.get(code)
        if slug is not None and name.strip():
            self._add_alias(slug, name, source, f"{evidence}, labeler code {code}")

    def slug_for_code(self, code: str) -> str | None:
        return self.code_slug.get(code)

    def all_names(self, slug: str) -> set[str]:
        return {self.names[slug], *self.aliases.get(slug, {})}

    def recalling_firm(self, firm: str, ndcs: Sequence[str], recall_number: str) -> str:
        target = normalize_firm(firm)
        candidates = sorted(
            {s for n in ndcs if (s := self.code_slug.get(labeler_code(n))) is not None}
            & {s for s in self.names if target in {normalize_firm(x) for x in self.all_names(s)}}
        )
        if len(candidates) == 1:
            slug = candidates[0]
            self.recall_matches += 1
            code = next(labeler_code(n) for n in ndcs if self.code_slug.get(labeler_code(n)) == slug)
            self._add_alias(slug, firm, "enforcement", f"recall {recall_number} names a product of labeler code {code}")
            return slug
        slug = slugify(firm)
        if slug not in self.names:
            self.names[slug] = firm
        else:
            self._add_alias(slug, firm, "enforcement", f"recall {recall_number}: same normalized key ({slug})")
        return slug

    def record(self, slug: str) -> dict[str, Any]:
        return {
            "slug": slug,
            "name": self.names[slug],
            "aliases": [
                {"name": name, "source": source, "evidence": evidence}
                for name, (source, evidence) in sorted(self.aliases.get(slug, {}).items())
            ],
        }
