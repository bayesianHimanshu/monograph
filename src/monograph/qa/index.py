"""An in-memory index of the validated H2 instances, for computing gold by code.

This reads the Pydantic instances directly, not the stores and not the tool layer, so gold is independent
of both backends. The relations follow the glossary in the base prompt.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel

from monograph.core.meta import DEFAULT_BASE
from monograph.core.models import (
    AdverseEventReport,
    ClinicalDrug,
    Organization,
    PackagedProduct,
    Product,
    Recall,
    Substance,
)


def _iri(path: str, key: str) -> str:
    from urllib.parse import quote

    return f"{DEFAULT_BASE}{path}/{quote(key, safe='')}"


@dataclass(frozen=True, slots=True)
class Report:
    iri: str
    report_id: str
    received: date
    serious: bool
    drugs: frozenset[str]  # clinical drug iris any drug entry names


@dataclass(frozen=True, slots=True)
class RecallFact:
    iri: str
    number: str
    classification: str
    reported: date
    products: frozenset[str]
    firm: str


@dataclass
class Index:
    substances: dict[str, Substance] = field(default_factory=dict)
    moiety: dict[str, str] = field(default_factory=dict)  # salt iri -> moiety iri
    orgs: dict[str, Organization] = field(default_factory=dict)
    drugs: dict[str, ClinicalDrug] = field(default_factory=dict)
    generic: dict[str, str] = field(default_factory=dict)  # brand cd iri -> generic cd iri
    products: dict[str, Product] = field(default_factory=dict)
    ingredients: dict[str, list[str]] = field(default_factory=dict)  # product iri -> substance iris
    mapped: dict[str, set[str]] = field(default_factory=dict)  # product iri -> clinical drug iris
    packages: dict[str, PackagedProduct] = field(default_factory=dict)
    package_product: dict[str, str] = field(default_factory=dict)
    package_labeler: dict[str, str] = field(default_factory=dict)
    reports: dict[str, Report] = field(default_factory=dict)
    recalls: dict[str, RecallFact] = field(default_factory=dict)
    all_ids: set[str] = field(default_factory=set)

    @staticmethod
    def build(instances: Mapping[str, Sequence[BaseModel]]) -> Index:
        ix = Index()
        for s in instances.get("Substance", []):
            assert isinstance(s, Substance)
            iri = _iri("substance", s.unii)
            ix.substances[iri] = s
            if s.active_moiety is not None:
                ix.moiety[iri] = s.active_moiety.moiety.iri
        for o in instances.get("Organization", []):
            assert isinstance(o, Organization)
            ix.orgs[_iri("organization", o.slug)] = o
        for c in instances.get("ClinicalDrug", []):
            assert isinstance(c, ClinicalDrug)
            iri = _iri("clinical-drug", c.rxcui)
            ix.drugs[iri] = c
            if c.generic is not None:
                ix.generic[iri] = c.generic.drug.iri
        for p in instances.get("Product", []):
            assert isinstance(p, Product)
            iri = _iri("product", p.product_ndc)
            ix.products[iri] = p
            ix.ingredients[iri] = [i.substance.iri for i in p.ingredients]
            ix.mapped[iri] = {m.clinical_drug.iri for m in p.drug_mappings} | (
                {p.clinical_drug.iri} if p.clinical_drug else set()
            )
        for k in instances.get("PackagedProduct", []):
            assert isinstance(k, PackagedProduct)
            iri = _iri("packaged-product", k.ndc)
            ix.packages[iri] = k
            ix.package_product[iri] = k.product.iri
            ix.package_labeler[iri] = k.labeler.iri
        for r in instances.get("AdverseEventReport", []):
            assert isinstance(r, AdverseEventReport)
            iri = _iri("adverse-event-report", r.report_id)
            drugs = {d.clinical_drug.iri for d in r.drugs if d.clinical_drug} | {
                h.iri for d in r.drugs for h in d.harmonized_clinical_drugs
            }
            ix.reports[iri] = Report(iri, r.report_id, r.receipt_date, r.serious, frozenset(drugs))
        for x in instances.get("Recall", []):
            assert isinstance(x, Recall)
            iri = _iri("recall", x.recall_number)
            ix.recalls[iri] = RecallFact(
                iri,
                x.recall_number,
                x.classification.value,
                x.report_date,
                frozenset(p.iri for p in x.products),
                x.recalling_firm.iri,
            )
        ix.all_ids = (
            set(ix.substances)
            | set(ix.orgs)
            | set(ix.drugs)
            | set(ix.products)
            | set(ix.packages)
            | set(ix.reports)
            | set(ix.recalls)
        )
        return ix

    # ---------------------------------------------------------------------------------- relations

    def products_containing(self, substance: str, salts: bool = False) -> set[str]:
        return {
            p
            for p, subs in self.ingredients.items()
            if substance in subs or (salts and any(self.moiety.get(s) == substance for s in subs))
        }

    def recalls_naming(self, products: Iterable[str]) -> set[str]:
        ps = set(products)
        return {r.iri for r in self.recalls.values() if r.products & ps}

    def firms_recalling(self, products: Iterable[str]) -> set[str]:
        return {self.recalls[r].firm for r in self.recalls_naming(products)}

    def labelers_of(self, products: Iterable[str]) -> set[str]:
        ps = set(products)
        return {self.package_labeler[k] for k, p in self.package_product.items() if p in ps}

    def recalls_by(self, firm: str) -> set[str]:
        return {r.iri for r in self.recalls.values() if r.firm == firm}

    def substances_of(self, products: Iterable[str]) -> set[str]:
        return {s for p in products for s in self.ingredients.get(p, [])}

    def products_mapped_to(self, drug: str) -> set[str]:
        return {p for p, ds in self.mapped.items() if drug in ds}

    def reports_involving(self, products: Iterable[str]) -> set[str]:
        drugs = {d for p in products for d in self.mapped.get(p, set())}
        return {r.iri for r in self.reports.values() if r.drugs & drugs}

    def products_labeled_by(self, firm: str) -> set[str]:
        return {self.package_product[k] for k, o in self.package_labeler.items() if o == firm}

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = defaultdict(int)
        out["salt links"] = len(self.moiety)
        out["brand-generic links"] = len(self.generic)
        out["org aliases"] = sum(len(o.aliases) for o in self.orgs.values())
        return dict(out)
