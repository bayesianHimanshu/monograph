"""H1 questions: templates, hand-written phrasings, deterministic gold, reference SQL (docs/h1/PLAN.md).

Gold is computed by code over the canonical instances (`H1Data`), never by an LLM and never from the warehouse.
Each template also carries hand-written reference SQL on the warehouse; generation keeps a candidate only when
that SQL, run on the warehouse, reproduces the gold (`monograph.h1.score.result_matches`): the equivalence of the
warehouse and the canonical model, question by question.

Each template names the ontology terms it needs (coverage is decided from them by code), the codes its gold
was computed with (for the "wrong or invalid code" failure category), and, for identity questions, the
crosswalk terms it needs (for "identity miss").
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from monograph.core.meta import DEFAULT_BASE
from monograph.core.models import (
    AdverseEventReport,
    ClinicalDrug,
    Label,
    Organization,
    PackagedProduct,
    Product,
    Recall,
    Substance,
)
from monograph.gen import warehouse as w
from monograph.h1.score import NONE_GOLD, number_gold, values_gold

TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6")
CORE_TYPES: tuple[str, ...] = ("Q2", "Q3", "Q4", "Q5")
PER_TYPE = 60
DEV_PER_TYPE = 15
SEED = 20261128
MAX_VALUES = 30
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
ROLES = ("primary suspect", "secondary suspect", "suspect", "concomitant", "interacting")


def _iri(path: str, key: str) -> str:
    return f"{DEFAULT_BASE}{path}/{quote(key, safe='')}"


def lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def code(domain: str, label: str) -> str:
    return w.domain(domain).code_of(label)


# ------------------------------------------------------------------------------------------- data


@dataclass
class Report:
    report_id: str
    received: date
    serious: bool
    criteria: frozenset[str]
    age: Decimal | None
    reactions: list[str]
    drugs: list[tuple[str, str, str | None, frozenset[str]]]  # (name, role, rxcui, harmonized rxcuis)


@dataclass
class H1Data:
    """The canonical instances, keyed by business keys."""

    products: dict[str, Product] = field(default_factory=dict)
    routes: dict[str, frozenset[str]] = field(default_factory=dict)
    ingredients: dict[str, list[str]] = field(default_factory=dict)  # product ndc -> UNIIs
    product_drugs: dict[str, set[str]] = field(default_factory=dict)  # product ndc -> RxCUIs (crosswalk)
    packages: dict[str, PackagedProduct] = field(default_factory=dict)
    package_product: dict[str, str] = field(default_factory=dict)
    package_labeler: dict[str, str] = field(default_factory=dict)  # package ndc -> firm slug
    orgs: dict[str, Organization] = field(default_factory=dict)
    substances: dict[str, Substance] = field(default_factory=dict)
    moiety: dict[str, str] = field(default_factory=dict)  # salt UNII -> moiety UNII
    drugs: dict[str, ClinicalDrug] = field(default_factory=dict)
    generic: dict[str, str] = field(default_factory=dict)  # brand RxCUI -> generic RxCUI
    reports: dict[str, Report] = field(default_factory=dict)
    recalls: dict[str, Recall] = field(default_factory=dict)
    recall_products: dict[str, frozenset[str]] = field(default_factory=dict)
    recall_firm: dict[str, str] = field(default_factory=dict)
    labels: dict[tuple[str, int], Label] = field(default_factory=dict)
    label_products: dict[tuple[str, int], frozenset[str]] = field(default_factory=dict)

    @staticmethod
    def build(instances: Mapping[str, Sequence[BaseModel]]) -> H1Data:
        d = H1Data()
        key: dict[str, str] = {}
        for s in instances.get("Substance", []):
            assert isinstance(s, Substance)
            d.substances[s.unii] = s
            key[_iri("substance", s.unii)] = s.unii
        for s in d.substances.values():
            if s.active_moiety is not None:
                d.moiety[s.unii] = key[s.active_moiety.moiety.iri]
        for o in instances.get("Organization", []):
            assert isinstance(o, Organization)
            d.orgs[o.slug] = o
            key[_iri("organization", o.slug)] = o.slug
        for c in instances.get("ClinicalDrug", []):
            assert isinstance(c, ClinicalDrug)
            d.drugs[c.rxcui] = c
            key[_iri("clinical-drug", c.rxcui)] = c.rxcui
        for c in d.drugs.values():
            if c.generic is not None:
                d.generic[c.rxcui] = key[c.generic.drug.iri]
        for p in instances.get("Product", []):
            assert isinstance(p, Product)
            d.products[p.product_ndc] = p
            key[_iri("product", p.product_ndc)] = p.product_ndc
            d.routes[p.product_ndc] = frozenset(r.value for r in p.routes)
            d.ingredients[p.product_ndc] = [key[i.substance.iri] for i in p.ingredients]
            d.product_drugs[p.product_ndc] = {key[m.clinical_drug.iri] for m in p.drug_mappings} | (
                {key[p.clinical_drug.iri]} if p.clinical_drug else set()
            )
        for k in instances.get("PackagedProduct", []):
            assert isinstance(k, PackagedProduct)
            d.packages[k.ndc] = k
            d.package_product[k.ndc] = key[k.product.iri]
            d.package_labeler[k.ndc] = key[k.labeler.iri]
        for r in instances.get("AdverseEventReport", []):
            assert isinstance(r, AdverseEventReport)
            d.reports[r.report_id] = Report(
                r.report_id,
                r.receipt_date,
                r.serious,
                frozenset(c.value for c in r.seriousness_criteria),
                r.patient_age_years,
                [x.term for x in r.reactions],
                [
                    (
                        x.drug_name,
                        x.role.value,
                        key[x.clinical_drug.iri] if x.clinical_drug else None,
                        frozenset(key[h.iri] for h in x.harmonized_clinical_drugs),
                    )
                    for x in r.drugs
                ],
            )
        for x in instances.get("Recall", []):
            assert isinstance(x, Recall)
            d.recalls[x.recall_number] = x
            d.recall_products[x.recall_number] = frozenset(key[p.iri] for p in x.products)
            d.recall_firm[x.recall_number] = key[x.recalling_firm.iri]
        for lb in instances.get("Label", []):
            assert isinstance(lb, Label)
            d.labels[(lb.set_id, lb.version)] = lb
            d.label_products[(lb.set_id, lb.version)] = frozenset(key[p.iri] for p in lb.products)
        return d

    # ----------------------------------------------------------------------------- relations

    def firm(self, slug: str) -> tuple[str, list[str]]:
        return slug, [self.orgs[slug].name]

    def substance(self, unii: str) -> tuple[str, list[str]]:
        return unii, [self.substances[unii].name]

    def products_of_firm(self, slug: str) -> set[str]:
        return {self.package_product[k] for k, o in self.package_labeler.items() if o == slug}

    def packages_of_firm(self, slug: str) -> list[str]:
        return [k for k, o in self.package_labeler.items() if o == slug]

    def unique_firm_name(self, slug: str) -> bool:
        name = " ".join(self.orgs[slug].name.split()).casefold()
        return sum(" ".join(o.name.split()).casefold() == name for o in self.orgs.values()) == 1

    def unique_substance_name(self, unii: str) -> bool:
        name = self.substances[unii].name.casefold()
        return sum(s.name.casefold() == name for s in self.substances.values()) == 1


@dataclass(frozen=True)
class Candidate:
    params: dict[str, str]
    gold: dict[str, Any]
    sql: str  # reference SQL on the warehouse
    codes: dict[str, list[str]] = field(default_factory=dict)  # ontology term -> labels the gold used
    naive: float | None = None  # Q5: what the naive count would give


Generator = Callable[[H1Data, random.Random], Iterator[Candidate]]


@dataclass(frozen=True)
class Template:
    id: str
    type: str
    phrasings: tuple[str, ...]
    needs: frozenset[str]  # ontology classes and terms the question needs
    generate: Generator
    identity: frozenset[str] = frozenset()  # crosswalk terms an identity question needs


def _years(lo: int, hi: int) -> list[int]:
    return list(range(lo, hi + 1))


def _between(year: int, month: int | None = None) -> str:
    if month is None:
        return f"BETWEEN {year}0101 AND {year}1231"
    return f"BETWEEN {year}{month:02d}01 AND {year}{month:02d}31"


def _in_month(day: date, year: int, month: int) -> bool:
    return day.year == year and day.month == month


def _shuffled[T](items: Sequence[T], rng: random.Random) -> list[T]:
    out = list(items)
    rng.shuffle(out)
    return out


def _fits(n: int) -> bool:
    return 1 <= n <= MAX_VALUES


# =========================================================================================== Q1


def q1_recalls_class_year(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos = [(y, c) for y in _years(2012, 2026) for c in ("I", "II", "III")]
    for year, cls in _shuffled(combos, rng):
        n = sum(1 for r in d.recalls.values() if r.classification.value == cls and r.report_date.year == year)
        if n < 1:
            continue
        sql = (
            f"SELECT COUNT(DISTINCT RCL_NO) FROM wh.T_RCL_PRD_DNRM WHERE RCL_CLS_CD = {lit(code('RCL_CLS', cls))}"
            f" AND RPT_DT {_between(year)}"
        )
        yield Candidate({"cls": cls, "year": str(year)}, number_gold(n), sql, {"Recall.classification": [cls]})


def q1_serious_reports_month(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    for m in _shuffled(range(1, 13), rng):
        n = sum(1 for r in d.reports.values() if r.serious and _in_month(r.received, 2014, m))
        if n < 1:
            continue
        sql = f"SELECT COUNT(*) FROM wh.T_AE_HDR WHERE SRS_FLG = 'Y' AND RCPT_DT {_between(2014, m)}"
        yield Candidate(
            {"month": MONTHS[m - 1], "year": "2014"}, number_gold(n), sql, {"AdverseEventReport.serious": ["true"]}
        )


def q1_death_reports_month(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    for m in _shuffled(range(1, 13), rng):
        n = sum(1 for r in d.reports.values() if "death" in r.criteria and _in_month(r.received, 2014, m))
        if n < 1:
            continue
        sql = f"SELECT COUNT(*) FROM wh.T_AE_HDR WHERE SRS_DTH_FLG = 'Y' AND RCPT_DT {_between(2014, m)}"
        yield Candidate(
            {"month": MONTHS[m - 1], "year": "2014"},
            number_gold(n),
            sql,
            {"AdverseEventReport.seriousness_criteria": ["death"]},
        )


def q1_average_age_period(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    periods: list[tuple[str, int, int]] = [(f"{MONTHS[m - 1]} 2014", m, m) for m in range(1, 13)]
    for label, lo, hi in _shuffled(periods, rng):
        ages = [
            r.age
            for r in d.reports.values()
            if r.age is not None and r.received.year == 2014 and lo <= r.received.month <= hi
        ]
        if len(ages) < 3:
            continue
        mean = sum(ages, Decimal(0)) / len(ages)
        sql = (
            f"SELECT AVG(PT_AGE_YRS) FROM wh.T_AE_HDR WHERE PT_AGE_YRS IS NOT NULL"
            f" AND RCPT_DT BETWEEN 2014{lo:02d}01 AND 2014{hi:02d}31"
        )
        yield Candidate({"period": label}, number_gold(mean), sql)


def q1_products_by_form(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    counts: dict[str, int] = defaultdict(int)
    for p in d.products.values():
        counts[p.dosage_form] += 1
    for form in _shuffled(sorted(counts), rng):
        n = counts[form]
        if n < 2:
            continue
        sql = f"SELECT COUNT(*) FROM wh.T_PRD_MSTR WHERE DSG_FRM_CD = {lit(code('DSG_FRM', form))}"
        yield Candidate({"form": form}, number_gold(n), sql, {"Product.dosage_form": [form]})


# =========================================================================================== Q2


def q2_firm_form_route_products(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for k, slug in d.package_labeler.items():
        p = d.products[d.package_product[k]]
        for route in d.routes[p.product_ndc]:
            combos[(slug, p.dosage_form, route)].add(p.product_ndc)
    keys = sorted(c for c, v in combos.items() if _fits(len(v)) and d.unique_firm_name(c[0]))
    for slug, form, route in _shuffled(keys, rng):
        gold = sorted(combos[(slug, form, route)])
        sql = (
            "SELECT DISTINCT p.PRD_CD FROM wh.T_PKG k JOIN wh.T_PRD_MSTR p ON p.PRD_SK = k.PRD_SK"
            " JOIN wh.T_ORG_MSTR o ON o.ORG_SK = k.LBLR_ORG_SK JOIN wh.T_PRD_RTE r ON r.PRD_SK = p.PRD_SK"
            f" WHERE o.ORG_CD = {lit(slug)} AND p.DSG_FRM_CD = {lit(code('DSG_FRM', form))}"
            f" AND r.RTE_CD = {lit(code('RTE', route))}"
        )
        yield Candidate(
            {"firm": d.orgs[slug].name, "form": form, "route": route},
            values_gold([(x, []) for x in gold]),
            sql,
            {"Product.dosage_form": [form], "Product.routes": [route]},
        )


def q2_class_form_recall_firms(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str], set[str]] = defaultdict(set)
    for number, r in d.recalls.items():
        for ndc in d.recall_products[number]:
            combos[(r.classification.value, d.products[ndc].dosage_form)].add(d.recall_firm[number])
    keys = sorted(c for c, v in combos.items() if _fits(len(v)) and all(d.unique_firm_name(s) for s in v))
    for cls, form in _shuffled(keys, rng):
        firms = sorted(combos[(cls, form)])
        sql = (
            "SELECT DISTINCT o.ORG_NM FROM wh.T_RCL_PRD_DNRM r JOIN wh.T_PRD_MSTR p ON p.PRD_SK = r.PRD_SK"
            " JOIN wh.T_ORG_MSTR o ON o.ORG_SK = r.FIRM_ORG_SK"
            f" WHERE r.RCL_CLS_CD = {lit(code('RCL_CLS', cls))} AND p.DSG_FRM_CD = {lit(code('DSG_FRM', form))}"
        )
        yield Candidate(
            {"cls": cls, "form": form},
            values_gold([d.firm(s) for s in firms]),
            sql,
            {"Recall.classification": [cls], "Product.dosage_form": [form]},
        )


def q2_route_class_year_recalls(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for number, r in d.recalls.items():
        for ndc in d.recall_products[number]:
            for route in d.routes[ndc]:
                combos[(route, r.classification.value, r.report_date.year)].add(number)
    keys = sorted(c for c, v in combos.items() if _fits(len(v)))
    for route, cls, year in _shuffled(keys, rng):
        gold = sorted(combos[(route, cls, year)])
        sql = (
            "SELECT DISTINCT r.RCL_NO FROM wh.T_RCL_PRD_DNRM r JOIN wh.T_PRD_RTE t ON t.PRD_SK = r.PRD_SK"
            f" WHERE r.RCL_CLS_CD = {lit(code('RCL_CLS', cls))} AND t.RTE_CD = {lit(code('RTE', route))}"
            f" AND r.RPT_DT {_between(year)}"
        )
        yield Candidate(
            {"route": route, "cls": cls, "year": str(year)},
            values_gold([(x, []) for x in gold]),
            sql,
            {"Product.routes": [route], "Recall.classification": [cls]},
        )


def q2_serious_role_drug_reports(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in d.reports.values():
        if not r.serious:
            continue
        for name, role, _, _ in r.drugs:
            if role in ("primary suspect", "concomitant"):
                combos[(name, role)].add(r.report_id)
    keys = sorted(c for c, v in combos.items() if 2 <= len(v) <= MAX_VALUES)
    for name, role in _shuffled(keys, rng):
        gold = sorted(combos[(name, role)])
        sql = (
            "SELECT DISTINCT a.RPT_ID FROM wh.T_AE_HDR a JOIN wh.T_AE_DRG g ON g.AE_SK = a.AE_SK"
            f" WHERE a.SRS_FLG = 'Y' AND g.DRG_NM = {lit(name)} AND g.DRG_ROLE_CD = {lit(code('DRG_ROLE', role))}"
        )
        yield Candidate(
            {"drug": name, "role": role},
            values_gold([(x, []) for x in gold]),
            sql,
            {"AdverseEventReport.drugs.role": [role], "AdverseEventReport.serious": ["true"]},
        )


def q2_substance_route_products(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ndc, uniis in d.ingredients.items():
        for unii in uniis:
            for route in d.routes[ndc]:
                combos[(unii, route)].add(ndc)
    keys = sorted(c for c, v in combos.items() if 2 <= len(v) <= MAX_VALUES and d.unique_substance_name(c[0]))
    for unii, route in _shuffled(keys, rng):
        gold = sorted(combos[(unii, route)])
        # only questions where the route actually narrows the substance's products
        if len({n for n, us in d.ingredients.items() if unii in us}) == len(gold):
            continue
        sql = (
            "SELECT DISTINCT p.PRD_CD FROM wh.T_PRD_INGR i JOIN wh.T_PRD_MSTR p ON p.PRD_SK = i.PRD_SK"
            " JOIN wh.T_SUBST_MSTR s ON s.SUBST_SK = i.SUBST_SK JOIN wh.T_PRD_RTE r ON r.PRD_SK = p.PRD_SK"
            f" WHERE s.UNII_CD = {lit(unii)} AND r.RTE_CD = {lit(code('RTE', route))}"
        )
        yield Candidate(
            {"substance": d.substances[unii].name, "route": route},
            values_gold([(x, []) for x in gold]),
            sql,
            {"Product.routes": [route]},
        )


# =========================================================================================== Q3


def q3_moiety_products(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    salts: dict[str, set[str]] = defaultdict(set)
    for salt, moiety in d.moiety.items():
        if salt != moiety:
            salts[moiety].add(salt)
    for moiety in _shuffled(sorted(salts), rng):
        if not d.unique_substance_name(moiety):
            continue
        forms = {moiety} | salts[moiety]
        gold = sorted(n for n, us in d.ingredients.items() if forms & set(us))
        direct = {n for n, us in d.ingredients.items() if moiety in us}
        if not _fits(len(gold)) or set(gold) == direct:
            continue
        sql = (
            "SELECT DISTINCT p.PRD_CD FROM wh.T_PRD_INGR i JOIN wh.T_PRD_MSTR p ON p.PRD_SK = i.PRD_SK"
            " JOIN wh.T_SUBST_MSTR s ON s.SUBST_SK = i.SUBST_SK"
            " LEFT JOIN wh.X_SALT_MOIETY m ON m.SUBST_SK = s.SUBST_SK"
            " LEFT JOIN wh.T_SUBST_MSTR ms ON ms.SUBST_SK = m.MOIETY_SUBST_SK"
            f" WHERE s.UNII_CD = {lit(moiety)} OR ms.UNII_CD = {lit(moiety)}"
        )
        yield Candidate({"substance": d.substances[moiety].name}, values_gold([(x, []) for x in gold]), sql)


def q3_rxcui_products(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    by_drug: dict[str, set[str]] = defaultdict(set)
    for ndc, rx in d.product_drugs.items():
        for x in rx:
            by_drug[x].add(ndc)
    keys = sorted(x for x, v in by_drug.items() if 2 <= len(v) <= MAX_VALUES)
    for rxcui in _shuffled(keys, rng):
        gold = sorted(by_drug[rxcui])
        sql = (
            "SELECT p.PRD_CD FROM wh.T_PRD_MSTR p JOIN wh.X_NDC_RXN x ON x.PRD_SK = p.PRD_SK"
            f" JOIN wh.T_CLIN_DRG c ON c.CLIN_DRG_SK = x.CLIN_DRG_SK WHERE c.RXCUI = {lit(rxcui)}"
            " UNION SELECT p.PRD_CD FROM wh.T_PRD_MSTR p JOIN wh.T_CLIN_DRG c ON c.CLIN_DRG_SK = p.RXN_CLIN_DRG_SK"
            f" WHERE c.RXCUI = {lit(rxcui)}"
        )
        yield Candidate({"rxcui": rxcui}, values_gold([(x, []) for x in gold]), sql)


def q3_generic_products(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    by_drug: dict[str, set[str]] = defaultdict(set)
    for ndc, rx in d.product_drugs.items():
        for x in rx:
            by_drug[x].add(ndc)
    names: dict[str, int] = defaultdict(int)
    for c in d.drugs.values():
        if c.name:
            names[c.name.casefold()] += 1
    for brand in _shuffled(sorted(d.generic), rng):
        c = d.drugs[brand]
        if not c.name or names[c.name.casefold()] != 1:
            continue
        gold = sorted(by_drug.get(d.generic[brand], set()))
        if not _fits(len(gold)):
            continue
        sql = (
            "SELECT p.PRD_CD FROM wh.T_CLIN_DRG b JOIN wh.X_BRND_GNRC g ON g.BRND_CLIN_DRG_SK = b.CLIN_DRG_SK"
            " JOIN wh.X_NDC_RXN x ON x.CLIN_DRG_SK = g.GNRC_CLIN_DRG_SK JOIN wh.T_PRD_MSTR p ON p.PRD_SK = x.PRD_SK"
            f" WHERE b.CLIN_DRG_NM = {lit(c.name)}"
            " UNION SELECT p.PRD_CD FROM wh.T_CLIN_DRG b JOIN wh.X_BRND_GNRC g ON g.BRND_CLIN_DRG_SK = b.CLIN_DRG_SK"
            " JOIN wh.T_PRD_MSTR p ON p.RXN_CLIN_DRG_SK = g.GNRC_CLIN_DRG_SK"
            f" WHERE b.CLIN_DRG_NM = {lit(c.name)}"
        )
        yield Candidate({"brand": c.name}, values_gold([(x, []) for x in gold]), sql)


def q3_alias_recalls(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    owners: dict[str, set[str]] = defaultdict(set)
    for o in d.orgs.values():
        for a in o.aliases:
            owners[" ".join(a.name.split()).casefold()].add(o.slug)
    names = {" ".join(o.name.split()).casefold() for o in d.orgs.values()}
    pairs = sorted(
        (a.name, o.slug)
        for o in d.orgs.values()
        for a in o.aliases
        if " ".join(a.name.split()).casefold() not in names and len(owners[" ".join(a.name.split()).casefold()]) == 1
    )
    for alias, slug in _shuffled(pairs, rng):
        gold = sorted(n for n, s in d.recall_firm.items() if s == slug)
        if not _fits(len(gold)):
            continue
        sql = (
            "SELECT DISTINCT r.RCL_NO FROM wh.X_ORG_ALIAS a JOIN wh.T_RCL_PRD_DNRM r ON r.FIRM_ORG_SK = a.ORG_SK"
            f" WHERE a.ALIAS_NM = {lit(alias)}"
        )
        yield Candidate({"alias": alias}, values_gold([(x, []) for x in gold]), sql)


def q3_ndc11_labeler(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    keys = sorted(k for k, p in d.packages.items() if p.ndc11 and p.ndc11 != k.replace("-", ""))
    for ndc in _shuffled(keys, rng):
        slug = d.package_labeler[ndc]
        ndc11 = d.packages[ndc].ndc11
        assert ndc11 is not None
        sql = (
            "SELECT o.ORG_NM FROM wh.T_PKG k JOIN wh.T_ORG_MSTR o ON o.ORG_SK = k.LBLR_ORG_SK"
            f" WHERE k.PKG_NDC11 = {lit(ndc11)}"
        )
        if not d.unique_firm_name(slug):
            continue
        yield Candidate({"ndc11": ndc11}, values_gold([d.firm(slug)]), sql)


def q3_rxcui_reports(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    by_drug: dict[str, set[str]] = defaultdict(set)
    for r in d.reports.values():
        for _, _, rx, harmonized in r.drugs:
            for x in ({rx} if rx else set()) | harmonized:
                by_drug[x].add(r.report_id)
    keys = sorted(x for x, v in by_drug.items() if 2 <= len(v) <= 200)
    for rxcui in _shuffled(keys, rng):
        n = len(by_drug[rxcui])
        sql = (
            "SELECT COUNT(DISTINCT a.AE_SK) FROM wh.T_AE_HDR a JOIN wh.T_AE_DRG g ON g.AE_SK = a.AE_SK"
            " LEFT JOIN wh.X_AE_DRG_RXN h ON h.AE_SK = g.AE_SK AND h.DRG_SEQ = g.DRG_SEQ"
            " JOIN wh.T_CLIN_DRG c ON c.CLIN_DRG_SK = g.CLIN_DRG_SK OR c.CLIN_DRG_SK = h.CLIN_DRG_SK"
            f" WHERE c.RXCUI = {lit(rxcui)}"
        )
        yield Candidate({"rxcui": rxcui}, number_gold(n), sql)


# =========================================================================================== Q4


def q4_label_section_products(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[int, str], set[str]] = defaultdict(set)
    for k, lb in d.labels.items():
        for s in lb.sections:
            combos[(lb.effective_date.year, s.title)].update(d.label_products[k])
    keys = sorted(c for c, v in combos.items() if _fits(len(v)))
    for year, title in _shuffled(keys, rng):
        gold = sorted(combos[(year, title)])
        sql = (
            "SELECT DISTINCT p.PRD_CD FROM wh.T_LBL l JOIN wh.T_LBL_SECT s ON s.LBL_SK = l.LBL_SK"
            " JOIN wh.T_LBL_PRD lp ON lp.LBL_SK = l.LBL_SK JOIN wh.T_PRD_MSTR p ON p.PRD_SK = lp.PRD_SK"
            f" WHERE EXTRACT(YEAR FROM l.EFF_DT) = {year} AND s.SECT_TTL = {lit(title)}"
        )
        yield Candidate({"year": str(year), "section": title}, values_gold([(x, []) for x in gold]), sql)


def q4_label_versions(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    keys = sorted(k for k, v in d.label_products.items() if _fits(len(v)))
    for set_id, version in _shuffled(keys, rng):
        gold = sorted(d.label_products[(set_id, version)])
        sql = (
            "SELECT p.PRD_CD FROM wh.T_LBL l JOIN wh.T_LBL_PRD lp ON lp.LBL_SK = l.LBL_SK"
            f" JOIN wh.T_PRD_MSTR p ON p.PRD_SK = lp.PRD_SK WHERE l.SET_ID = {lit(set_id)} AND l.VER_NO = {version}"
        )
        yield Candidate({"set_id": set_id, "version": str(version)}, values_gold([(x, []) for x in gold]), sql)


def q4_tty_name_count(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    words = ("Oral Tablet", "Oral Capsule", "Injection", "Topical", "Oral Solution", "Extended Release", "Pack")
    combos = [(t, wd) for t in ("SCD", "SBD", "GPCK", "BPCK") for wd in words]
    for tty, word in _shuffled(combos, rng):
        n = sum(1 for c in d.drugs.values() if c.term_type == tty and c.name and word.casefold() in c.name.casefold())
        if n < 1:
            continue
        sql = (
            f"SELECT COUNT(*) FROM wh.T_CLIN_DRG WHERE TTY_CD = {lit(tty)}"
            f" AND CLIN_DRG_NM ILIKE {lit('%' + word + '%')}"
        )
        yield Candidate({"tty": tty, "word": word}, number_gold(n), sql)


def q4_firm_labels_year(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    for k, lb in d.labels.items():
        for ndc in d.label_products[k]:
            for pk, slug in d.package_labeler.items():
                if d.package_product[pk] == ndc:
                    combos[(slug, lb.effective_date.year)].add(k)
    keys = sorted(c for c, v in combos.items() if v and d.unique_firm_name(c[0]))
    for slug, year in _shuffled(keys, rng):
        n = len(combos[(slug, year)])
        sql = (
            "SELECT COUNT(DISTINCT l.LBL_SK) FROM wh.T_LBL l JOIN wh.T_LBL_PRD lp ON lp.LBL_SK = l.LBL_SK"
            " JOIN wh.T_PKG k ON k.PRD_SK = lp.PRD_SK JOIN wh.T_ORG_MSTR o ON o.ORG_SK = k.LBLR_ORG_SK"
            f" WHERE o.ORG_CD = {lit(slug)} AND EXTRACT(YEAR FROM l.EFF_DT) = {year}"
        )
        yield Candidate({"firm": d.orgs[slug].name, "year": str(year)}, number_gold(n), sql)


# =========================================================================================== Q5


def q5_reports_by_reaction(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    terms = sorted({t for r in d.reports.values() for t in r.reactions})
    for term in _shuffled(terms, rng):
        distinct = sum(1 for r in d.reports.values() if term in r.reactions)
        naive = sum(r.reactions.count(term) for r in d.reports.values())
        if distinct < 2 or naive == distinct:
            continue
        sql = (
            "SELECT COUNT(DISTINCT a.AE_SK) FROM wh.T_AE_HDR a JOIN wh.T_AE_RXN x ON x.AE_SK = a.AE_SK"
            f" WHERE x.RXN_TRM = {lit(term)}"
        )
        yield Candidate({"term": term}, number_gold(distinct), sql, naive=float(naive))


def q5_reaction_role_reports(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str], tuple[int, int]] = {}
    for term in sorted({t for r in d.reports.values() for t in r.reactions}):
        for role in ("concomitant", "suspect", "primary suspect"):
            distinct = naive = 0
            for r in d.reports.values():
                k = r.reactions.count(term) * sum(1 for x in r.drugs if x[1] == role)
                if k:
                    distinct += 1
                    naive += k
            if distinct >= 2 and naive != distinct:
                combos[(term, role)] = (distinct, naive)
    for term, role in _shuffled(sorted(combos), rng):
        distinct, naive = combos[(term, role)]
        sql = (
            "SELECT COUNT(DISTINCT a.AE_SK) FROM wh.T_AE_HDR a JOIN wh.T_AE_RXN x ON x.AE_SK = a.AE_SK"
            f" JOIN wh.T_AE_DRG g ON g.AE_SK = a.AE_SK WHERE x.RXN_TRM = {lit(term)}"
            f" AND g.DRG_ROLE_CD = {lit(code('DRG_ROLE', role))}"
        )
        yield Candidate(
            {"term": term, "role": role},
            number_gold(distinct),
            sql,
            {"AdverseEventReport.drugs.role": [role]},
            float(naive),
        )


def q5_firm_route_product_count(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str], tuple[set[str], int]] = {}
    for slug in sorted(d.orgs):
        packs = d.packages_of_firm(slug)
        for route in sorted({r for k in packs for r in d.routes[d.package_product[k]]}):
            products = {d.package_product[k] for k in packs if route in d.routes[d.package_product[k]]}
            naive = sum(1 for k in packs if route in d.routes[d.package_product[k]])
            if len(products) >= 2 and naive != len(products) and d.unique_firm_name(slug):
                combos[(slug, route)] = (products, naive)
    for slug, route in _shuffled(sorted(combos), rng):
        products, naive = combos[(slug, route)]
        sql = (
            "SELECT COUNT(DISTINCT p.PRD_SK) FROM wh.T_PKG k JOIN wh.T_PRD_MSTR p ON p.PRD_SK = k.PRD_SK"
            " JOIN wh.T_ORG_MSTR o ON o.ORG_SK = k.LBLR_ORG_SK JOIN wh.T_PRD_RTE r ON r.PRD_SK = p.PRD_SK"
            f" WHERE o.ORG_CD = {lit(slug)} AND r.RTE_CD = {lit(code('RTE', route))}"
        )
        yield Candidate(
            {"firm": d.orgs[slug].name, "route": route},
            number_gold(len(products)),
            sql,
            {"Product.routes": [route]},
            float(naive),
        )


def q5_class_form_recall_count(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    combos: dict[tuple[str, str], tuple[int, int]] = {}
    forms = sorted({p.dosage_form for p in d.products.values()})
    for cls in ("I", "II", "III"):
        for form in forms:
            distinct = naive = 0
            for number, r in d.recalls.items():
                if r.classification.value != cls:
                    continue
                k = sum(1 for n in d.recall_products[number] if d.products[n].dosage_form == form)
                if k:
                    distinct += 1
                    naive += k
            if distinct >= 2 and naive != distinct:
                combos[(cls, form)] = (distinct, naive)
    for cls, form in _shuffled(sorted(combos), rng):
        distinct, naive = combos[(cls, form)]
        sql = (
            "SELECT COUNT(DISTINCT r.RCL_NO) FROM wh.T_RCL_PRD_DNRM r JOIN wh.T_PRD_MSTR p ON p.PRD_SK = r.PRD_SK"
            f" WHERE r.RCL_CLS_CD = {lit(code('RCL_CLS', cls))} AND p.DSG_FRM_CD = {lit(code('DSG_FRM', form))}"
        )
        yield Candidate(
            {"cls": cls, "form": form},
            number_gold(distinct),
            sql,
            {"Recall.classification": [cls], "Product.dosage_form": [form]},
            float(naive),
        )


def q5_average_age_reaction(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    terms = sorted({t for r in d.reports.values() for t in r.reactions})
    for term in _shuffled(terms, rng):
        rows = [
            (r.age, r.reactions.count(term)) for r in d.reports.values() if term in r.reactions and r.age is not None
        ]
        if len(rows) < 3:
            continue
        mean = sum((a for a, _ in rows), Decimal(0)) / len(rows)
        naive = sum((a * k for a, k in rows), Decimal(0)) / sum(k for _, k in rows)
        if abs(float(mean - naive)) <= max(0.01, 0.001 * abs(float(mean))):
            continue
        sql = (
            "SELECT AVG(a.PT_AGE_YRS) FROM wh.T_AE_HDR a WHERE a.AE_SK IN"
            f" (SELECT x.AE_SK FROM wh.T_AE_RXN x WHERE x.RXN_TRM = {lit(term)})"
        )
        yield Candidate({"term": term}, number_gold(mean), sql, naive=float(naive))


def q5_substance_labeler_count(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    for unii in _shuffled(sorted(d.substances), rng):
        if not d.unique_substance_name(unii):
            continue
        products = {n for n, us in d.ingredients.items() if unii in us}
        packs = [k for k, p in d.package_product.items() if p in products]
        firms = {d.package_labeler[k] for k in packs}
        if len(firms) < 2 or len(packs) == len(firms):
            continue
        sql = (
            "SELECT COUNT(DISTINCT k.LBLR_ORG_SK) FROM wh.T_PKG k JOIN wh.T_PRD_INGR i ON i.PRD_SK = k.PRD_SK"
            f" JOIN wh.T_SUBST_MSTR s ON s.SUBST_SK = i.SUBST_SK WHERE s.UNII_CD = {lit(unii)}"
        )
        yield Candidate({"substance": d.substances[unii].name}, number_gold(len(firms)), sql, naive=float(len(packs)))


# =========================================================================================== Q6

FAKE_FIRMS = (
    "Northbridge Pharmaceutical Holdings LLC",
    "Calderwood Therapeutics Inc",
    "Pemberton Valley Laboratories",
    "Ostrava Generics Corporation",
    "Hollin and Marsh Pharma Ltd",
    "Greywater Biologics Inc",
    "Tarrant Coastal Pharmacal",
    "Linfield Medicinals Co",
    "Avonmere Health Products LLC",
    "Quillon Specialty Drugs Inc",
    "Brackwell Pharmaceuticals",
    "Sunhollow Laboratories Inc",
    "Merriden Rx Partners",
    "Kestrel Ridge Pharma",
    "Elmstead Compounding Services",
    "Varrow Life Sciences Inc",
    "Draycott Pharmaceutical Group",
)
FAKE_TERMS = (
    "Spontaneous elbow luminescence",
    "Acute vowel displacement",
    "Chronic sock intolerance",
    "Retrograde whistling",
    "Transient map dyslexia",
    "Episodic kettle sensitivity",
    "Lateral umbrella aversion",
    "Paradoxical hiccup reversal",
    "Nocturnal staple fixation",
    "Benign confetti dermatosis",
    "Recurrent ladder phobia",
    "Idiopathic thimble syndrome",
    "Postprandial yodelling",
    "Seasonal button craving",
    "Focal wallpaper hallucination",
)


def _fake_ndcs(d: H1Data, rng: random.Random, n: int) -> list[str]:
    out: list[str] = []
    while len(out) < n:
        ndc = f"9{rng.randint(100, 999)}-{rng.randint(1000, 9999)}"
        if ndc not in d.products and ndc not in out:
            out.append(ndc)
    return out


def q6_fake_firm_recalls(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    names = {" ".join(o.name.split()).casefold() for o in d.orgs.values()}
    aliases = {" ".join(a.name.split()).casefold() for o in d.orgs.values() for a in o.aliases}
    for name in _shuffled(FAKE_FIRMS, rng):
        if name.casefold() in names | aliases:
            continue
        sql = (
            "SELECT DISTINCT r.RCL_NO FROM wh.T_RCL_PRD_DNRM r JOIN wh.T_ORG_MSTR o ON o.ORG_SK = r.FIRM_ORG_SK"
            f" WHERE o.ORG_NM = {lit(name)}"
        )
        yield Candidate({"firm": name}, NONE_GOLD, sql)


def q6_fake_product_packages(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    for ndc in _fake_ndcs(d, rng, 20):
        sql = (
            f"SELECT k.PKG_NDC FROM wh.T_PKG k JOIN wh.T_PRD_MSTR p ON p.PRD_SK = k.PRD_SK WHERE p.PRD_CD = {lit(ndc)}"
        )
        yield Candidate({"ndc": ndc}, NONE_GOLD, sql)


def q6_fake_reaction_reports(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    terms = {t.casefold() for r in d.reports.values() for t in r.reactions}
    for term in _shuffled(FAKE_TERMS, rng):
        if term.casefold() in terms:
            continue
        sql = (
            "SELECT COUNT(DISTINCT a.AE_SK) FROM wh.T_AE_HDR a JOIN wh.T_AE_RXN x ON x.AE_SK = a.AE_SK"
            f" WHERE a.SRS_FLG = 'Y' AND x.RXN_TRM = {lit(term)}"
        )
        yield Candidate({"term": term}, NONE_GOLD, sql)


def q6_fake_rxcui_products(d: H1Data, rng: random.Random) -> Iterator[Candidate]:
    seen: set[str] = set()
    while len(seen) < 20:
        rxcui = str(rng.randint(90000000, 99999999))
        if rxcui in d.drugs or rxcui in seen:
            continue
        seen.add(rxcui)
        sql = (
            "SELECT p.PRD_CD FROM wh.T_PRD_MSTR p JOIN wh.X_NDC_RXN x ON x.PRD_SK = p.PRD_SK"
            f" JOIN wh.T_CLIN_DRG c ON c.CLIN_DRG_SK = x.CLIN_DRG_SK WHERE c.RXCUI = {lit(rxcui)}"
        )
        yield Candidate({"rxcui": rxcui}, NONE_GOLD, sql)


# ===================================================================================== templates

R, P, K, S, ORG, A = "Recall", "Product", "PackagedProduct", "Substance", "Organization", "AdverseEventReport"


def _f(*terms: str) -> frozenset[str]:
    return frozenset(terms)


TEMPLATES: tuple[Template, ...] = (
    Template(
        "q1_recalls_class_year",
        "Q1",
        (
            "How many Class {cls} recalls were reported in {year}?",
            "In {year}, how many drug recalls were classified as Class {cls}?",
            "Count the Class {cls} recalls with a report date in {year}.",
        ),
        _f(R, "Recall.classification", "Recall.report_date"),
        q1_recalls_class_year,
    ),
    Template(
        "q1_serious_reports_month",
        "Q1",
        (
            "How many serious adverse event reports were received in {month} {year}?",
            "What was the number of serious adverse event cases received during {month} {year}?",
            "Count the adverse event reports flagged as serious that were received in {month} {year}.",
        ),
        _f(A, "AdverseEventReport.serious", "AdverseEventReport.receipt_date"),
        q1_serious_reports_month,
    ),
    Template(
        "q1_death_reports_month",
        "Q1",
        (
            "How many adverse event reports received in {month} {year} list death among their seriousness criteria?",
            "In {month} {year}, how many adverse event reports had death as a seriousness criterion?",
            "Count the fatal adverse event reports (seriousness criterion death) received in {month} {year}.",
        ),
        _f(A, "AdverseEventReport.seriousness_criteria", "AdverseEventReport.receipt_date"),
        q1_death_reports_month,
    ),
    Template(
        "q1_average_age_period",
        "Q1",
        (
            "What was the average patient age, in years, over the adverse event reports received in {period} that"
            " give an age?",
            "Among adverse event reports received in {period} with a patient age, what is the mean age in years?",
            "Give the average patient age (years) of the adverse event reports received in {period}, ignoring"
            " reports without an age.",
        ),
        _f(A, "AdverseEventReport.patient_age_years", "AdverseEventReport.receipt_date"),
        q1_average_age_period,
    ),
    Template(
        "q1_products_by_form",
        "Q1",
        (
            "How many products have the dosage form {form}?",
            "Count the drug products whose dosage form is {form}.",
            "What is the number of products listed with dosage form {form}?",
        ),
        _f(P, "Product.dosage_form"),
        q1_products_by_form,
    ),
    Template(
        "q2_firm_form_route_products",
        "Q2",
        (
            "Which products labeled by {firm} have dosage form {form} and the route of administration {route}? List"
            " their product NDCs.",
            "List the product NDCs of {firm}'s {form} products that are administered {route}.",
            "Give the product NDCs for products that {firm} labels, with dosage form {form}, given by the {route}"
            " route.",
        ),
        _f(P, K, ORG, "PackagedProduct.labeler", "PackagedProduct.product", "Product.dosage_form", "Product.routes"),
        q2_firm_form_route_products,
    ),
    Template(
        "q2_class_form_recall_firms",
        "Q2",
        (
            "Which firms were the recalling firm of a Class {cls} recall that names a product with dosage form"
            " {form}? Give the firm names.",
            "Name the firms that initiated Class {cls} recalls covering {form} products.",
            "List the recalling firms of Class {cls} recalls that include at least one product whose dosage form is"
            " {form}.",
        ),
        _f(R, P, ORG, "Recall.classification", "Recall.products", "Recall.recalling_firm", "Product.dosage_form"),
        q2_class_form_recall_firms,
    ),
    Template(
        "q2_route_class_year_recalls",
        "Q2",
        (
            "Which Class {cls} recalls reported in {year} name a product administered by the {route} route? List the"
            " recall numbers.",
            "List the recall numbers of the Class {cls} recalls from {year} that cover a product given {route}.",
            "Give the recall numbers for {year} Class {cls} recalls naming at least one {route} product.",
        ),
        _f(R, P, "Recall.classification", "Recall.report_date", "Recall.products", "Product.routes"),
        q2_route_class_year_recalls,
    ),
    Template(
        "q2_serious_role_drug_reports",
        "Q2",
        (
            "Which serious adverse event reports list {drug} as a {role} drug? Give the report ids.",
            "List the report ids of serious adverse event reports where {drug} is recorded with the role {role}.",
            "Give the ids of serious adverse event reports naming {drug} as {role}.",
        ),
        _f(
            A,
            "AdverseEventReport.serious",
            "AdverseEventReport.drugs",
            "AdverseEventReport.drugs.drug_name",
            "AdverseEventReport.drugs.role",
        ),
        q2_serious_role_drug_reports,
    ),
    Template(
        "q2_substance_route_products",
        "Q2",
        (
            "Which products containing {substance} are administered by the {route} route? List their product NDCs.",
            "List the product NDCs of products with {substance} as an active ingredient and route {route}.",
            "Give the product NDCs of the {route} products whose active ingredients include {substance}.",
        ),
        _f(P, S, "Product.ingredients", "Product.ingredients.substance", "Product.routes", "Substance.name"),
        q2_substance_route_products,
    ),
    Template(
        "q3_moiety_products",
        "Q3",
        (
            "Which products contain {substance}, counting its salt and ester forms? List their product NDCs.",
            "List the product NDCs of products whose active ingredient is {substance} or a salt or ester of it.",
            "Give the product NDCs of products containing {substance} as an active moiety, including salt forms.",
        ),
        _f(P, S, "Product.ingredients", "Product.ingredients.substance", "Substance.name", "Substance.active_moiety"),
        q3_moiety_products,
        _f("Substance.active_moiety"),
    ),
    Template(
        "q3_rxcui_products",
        "Q3",
        (
            "Which products are mapped to the RxNorm clinical drug with RxCUI {rxcui}? List their product NDCs.",
            "List the product NDCs of products that map to RxCUI {rxcui}.",
            "Give the product NDCs mapped to RxNorm concept {rxcui}.",
        ),
        _f(P, "ClinicalDrug", "Product.drug_mappings", "Product.clinical_drug", "ClinicalDrug.rxcui"),
        q3_rxcui_products,
        _f("Product.drug_mappings", "Product.clinical_drug"),
    ),
    Template(
        "q3_generic_products",
        "Q3",
        (
            "Which products are mapped to the generic equivalent of the brand clinical drug {brand}? List their"
            " product NDCs.",
            "List the product NDCs of the generic equivalents of {brand}.",
            "Give the product NDCs of products mapped to the generic clinical drug of the brand {brand}.",
        ),
        _f(
            P,
            "ClinicalDrug",
            "ClinicalDrug.generic",
            "ClinicalDrug.name",
            "Product.drug_mappings",
            "Product.clinical_drug",
        ),
        q3_generic_products,
        _f("ClinicalDrug.generic", "Product.drug_mappings", "Product.clinical_drug"),
    ),
    Template(
        "q3_alias_recalls",
        "Q3",
        (
            "Which recalls had the firm that also goes by the name '{alias}' as recalling firm? List the recall"
            " numbers.",
            "List the recall numbers of recalls initiated by the firm known as '{alias}'.",
            "Give the recall numbers for recalls whose recalling firm appears under the name '{alias}'.",
        ),
        _f(R, ORG, "Recall.recalling_firm", "Organization.aliases"),
        q3_alias_recalls,
        _f("Organization.aliases"),
    ),
    Template(
        "q3_ndc11_labeler",
        "Q3",
        (
            "Which firm labels the package whose 11-digit NDC is {ndc11}? Give the firm name.",
            "Name the labeler of the package with 11-digit NDC {ndc11}.",
            "Who is the labeler firm for the package identified by the 11-digit NDC {ndc11}?",
        ),
        _f(K, ORG, "PackagedProduct.ndc11", "PackagedProduct.labeler"),
        q3_ndc11_labeler,
        _f("PackagedProduct.ndc11"),
    ),
    Template(
        "q3_rxcui_reports",
        "Q3",
        (
            "How many adverse event reports involve the RxNorm clinical drug {rxcui}, through either a drug's clinical"
            " drug or its harmonized clinical drugs?",
            "Count the adverse event reports with a drug entry mapped to RxCUI {rxcui} (directly or by harmonization).",
            "How many adverse event reports name RxCUI {rxcui} for one of their drugs, as clinical drug or harmonized"
            " clinical drug?",
        ),
        _f(
            A,
            "ClinicalDrug",
            "AdverseEventReport.drugs",
            "AdverseEventReport.drugs.clinical_drug",
            "AdverseEventReport.drugs.harmonized_clinical_drugs",
        ),
        q3_rxcui_reports,
        _f("AdverseEventReport.drugs.clinical_drug", "AdverseEventReport.drugs.harmonized_clinical_drugs"),
    ),
    Template(
        "q4_label_section_products",
        "Q4",
        (
            "Which products are covered by a label effective in {year} that has a '{section}' section? List their"
            " product NDCs.",
            "List the product NDCs covered by labels with an effective date in {year} that include a section titled"
            " '{section}'.",
            "Give the product NDCs of products whose {year} labels contain a '{section}' section.",
        ),
        _f("Label", P, "Label.products", "Label.sections", "Label.effective_date"),
        q4_label_section_products,
    ),
    Template(
        "q4_label_versions",
        "Q4",
        (
            "Which products does version {version} of the label with set id {set_id} cover? List their product NDCs.",
            "List the product NDCs covered by label set {set_id}, version {version}.",
            "Give the product NDCs named by version {version} of label set id {set_id}.",
        ),
        _f("Label", P, "Label.products", "Label.set_id", "Label.version"),
        q4_label_versions,
    ),
    Template(
        "q4_tty_name_count",
        "Q4",
        (
            "How many RxNorm clinical drugs of term type {tty} have a name containing '{word}'?",
            "Count the RxNorm clinical drugs with term type {tty} whose name includes '{word}'.",
            "What is the number of {tty} clinical drugs (RxNorm term type) with '{word}' in the name?",
        ),
        _f("ClinicalDrug", "ClinicalDrug.term_type", "ClinicalDrug.name"),
        q4_tty_name_count,
    ),
    Template(
        "q4_firm_labels_year",
        "Q4",
        (
            "How many labels effective in {year} cover at least one product that {firm} labels a package of?",
            "Count the labels with an effective date in {year} that cover a product labeled by {firm}.",
            "How many labels with a {year} effective date cover a product with a package labeled by {firm}?",
        ),
        _f("Label", P, K, ORG, "Label.products", "Label.effective_date", "PackagedProduct.labeler"),
        q4_firm_labels_year,
    ),
    Template(
        "q5_reports_by_reaction",
        "Q5",
        (
            "How many adverse event reports list the reaction '{term}'?",
            "Count the adverse event reports that include '{term}' among their reactions.",
            "In how many adverse event reports does the reaction '{term}' appear?",
        ),
        _f(A, "AdverseEventReport.reactions", "AdverseEventReport.reactions.term"),
        q5_reports_by_reaction,
    ),
    Template(
        "q5_reaction_role_reports",
        "Q5",
        (
            "How many adverse event reports list the reaction '{term}' and at least one {role} drug?",
            "Count the adverse event reports that report '{term}' and include a drug with the role {role}.",
            "In how many adverse event reports does '{term}' appear together with a {role} drug?",
        ),
        _f(
            A,
            "AdverseEventReport.reactions",
            "AdverseEventReport.reactions.term",
            "AdverseEventReport.drugs",
            "AdverseEventReport.drugs.role",
        ),
        q5_reaction_role_reports,
    ),
    Template(
        "q5_firm_route_product_count",
        "Q5",
        (
            "How many products labeled by {firm} are administered by the {route} route?",
            "Count {firm}'s products that have {route} as a route of administration.",
            "How many of the products {firm} labels are given {route}?",
        ),
        _f(P, K, ORG, "PackagedProduct.labeler", "PackagedProduct.product", "Product.routes"),
        q5_firm_route_product_count,
    ),
    Template(
        "q5_class_form_recall_count",
        "Q5",
        (
            "How many Class {cls} recalls name at least one product with dosage form {form}?",
            "Count the Class {cls} recalls covering a {form} product.",
            "How many recalls classified Class {cls} include a product whose dosage form is {form}?",
        ),
        _f(R, P, "Recall.classification", "Recall.products", "Product.dosage_form"),
        q5_class_form_recall_count,
    ),
    Template(
        "q5_average_age_reaction",
        "Q5",
        (
            "What is the average patient age, in years, of the adverse event reports that list the reaction '{term}'"
            " and give an age?",
            "Among adverse event reports reporting '{term}' with a patient age, what is the mean age in years?",
            "Give the average patient age (years) across reports that include the reaction '{term}', ignoring"
            " reports without an age.",
        ),
        _f(
            A,
            "AdverseEventReport.reactions",
            "AdverseEventReport.reactions.term",
            "AdverseEventReport.patient_age_years",
        ),
        q5_average_age_reaction,
    ),
    Template(
        "q5_substance_labeler_count",
        "Q5",
        (
            "How many firms label a package of a product containing {substance}?",
            "Count the labelers of packages of products with {substance} as an active ingredient.",
            "How many different firms label packages for products whose active ingredients include {substance}?",
        ),
        _f(
            P,
            K,
            S,
            ORG,
            "Product.ingredients",
            "Product.ingredients.substance",
            "PackagedProduct.labeler",
            "Substance.name",
        ),
        q5_substance_labeler_count,
    ),
    Template(
        "q6_fake_firm_recalls",
        "Q6",
        (
            "Which recalls did {firm} initiate? List the recall numbers.",
            "List the recall numbers of recalls with {firm} as the recalling firm.",
            "Give the recall numbers for recalls initiated by {firm}.",
        ),
        _f(R, ORG, "Recall.recalling_firm", "Organization.name"),
        q6_fake_firm_recalls,
    ),
    Template(
        "q6_fake_product_packages",
        "Q6",
        (
            "Which packages belong to the product with product NDC {ndc}? List the package NDCs.",
            "List the package NDCs of product {ndc}.",
            "Give the package NDCs for the product whose product NDC is {ndc}.",
        ),
        _f(P, K, "PackagedProduct.product"),
        q6_fake_product_packages,
    ),
    Template(
        "q6_fake_reaction_reports",
        "Q6",
        (
            "How many serious adverse event reports list the reaction '{term}'?",
            "Count the serious adverse event reports that include the reaction '{term}'.",
            "In how many serious adverse event reports does '{term}' appear as a reaction?",
        ),
        _f(A, "AdverseEventReport.serious", "AdverseEventReport.reactions", "AdverseEventReport.reactions.term"),
        q6_fake_reaction_reports,
    ),
    Template(
        "q6_fake_rxcui_products",
        "Q6",
        (
            "Which products are mapped to RxCUI {rxcui}? List their product NDCs.",
            "List the product NDCs of products mapped to the RxNorm clinical drug {rxcui}.",
            "Give the product NDCs that map to RxNorm concept {rxcui}.",
        ),
        _f(P, "ClinicalDrug", "Product.drug_mappings", "Product.clinical_drug"),
        q6_fake_rxcui_products,
        _f("Product.drug_mappings", "Product.clinical_drug"),
    ),
)

TEMPLATE_BY_ID = {t.id: t for t in TEMPLATES}


def inside(template: Template, coverage: frozenset[str]) -> bool:
    """The coverage rule, by code: every class and term the template needs is in the semantic layer."""
    return template.needs <= coverage
