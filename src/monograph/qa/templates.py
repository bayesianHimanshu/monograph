"""Question templates: phrasings written by hand (fixed strings here, never paraphrased by an LLM), candidate
parameters drawn from the index, gold computed by code over the index, and a tool plan.

The tool plan answers the question with the shared tool service. Generation keeps a candidate only when
the plan's answer equals the gold and no tool result was truncated: that checks the gold against an
independent path through the stores, and makes sure every question can be answered within the row caps.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from monograph.extract.resolve import normalize_name
from monograph.graph.identity import normalize_firm
from monograph.qa.index import Index
from monograph.tools import registry as r
from monograph.tools.service import ToolService

MAX_GOLD = 30
MAX_FANOUT = 150


class Truncated(Exception):
    """A tool result was capped: the question is not answerable within the caps, so it is not used."""


@dataclass(frozen=True)
class Candidate:
    params: dict[str, str]
    gold: dict[str, Any]  # {"kind": "ids", "ids": [...]} | {"kind": "number", "number": n} | {"kind": "none"}
    anchors: list[str]  # ids of the entities the question names (empty for nonexistent ones)
    plan: Callable[[ToolService], dict[str, Any]]


@dataclass(frozen=True)
class Template:
    id: str
    type: str
    phrasings: tuple[str, ...]
    candidates: Callable[[Index, random.Random], Iterator[Candidate]]
    note: str = ""


def ids(values: set[str]) -> dict[str, Any]:
    return {"kind": "ids", "ids": sorted(values)} if values else {"kind": "none"}


def number(n: int) -> dict[str, Any]:
    return {"kind": "number", "number": n}


# --------------------------------------------------------------------------------------- tool helpers


def _call(s: ToolService, name: str, **kw: Any) -> Any:
    out = s.call(name, kw)
    if getattr(out, "truncated", False):
        raise Truncated(name)
    return out


def _resolve(s: ToolService, text: str, kind: str) -> list[str]:
    return sorted({c.id for c in _call(s, "resolve_entity", text=text, kind=kind).candidates})


def _hop(s: ToolService, start: Iterable[str], relation: str, direction: str, kind: str | None = None) -> set[str]:
    out: set[str] = set()
    items = sorted(set(start))
    for i in range(0, len(items), r.MAX_IDS):
        res = _call(s, "neighbors", ids=items[i : i + r.MAX_IDS], relation=relation, direction=direction)
        out |= {n.id for n in res.neighbors if kind is None or n.kind == kind}
    return out


def _mapped_products(s: ToolService, drug: str) -> set[str]:
    return _hop(s, [drug], "DRUG_MAPPING", "in", "Product") | _hop(s, [drug], "CLINICAL_DRUG", "in", "Product")


def _as_gold(found: set[str]) -> dict[str, Any]:
    return ids(found)


# ------------------------------------------------------------------------------------- shared checks


def _unique_firm_name(ix: Index, name: str) -> bool:
    """The name (normalized) belongs to exactly one organization, by name or alias."""
    target = normalize_firm(name)
    owners = {
        iri
        for iri, o in ix.orgs.items()
        if normalize_firm(o.name) == target or any(normalize_firm(a.name) == target for a in o.aliases)
    }
    return len(owners) == 1


def _unique_substance_name(ix: Index, iri: str) -> bool:
    target = normalize_name(ix.substances[iri].name)
    return sum(1 for s in ix.substances.values() if normalize_name(s.name) == target) == 1


def _shuffled[T](items: Sequence[T], rng: random.Random) -> list[T]:
    out = sorted(items)  # type: ignore[type-var]
    rng.shuffle(out)
    return out


def _sub(ix: Index, iri: str) -> dict[str, str]:
    s = ix.substances[iri]
    return {"name": s.name, "unii": s.unii}


# ---------------------------------------------------------------------------------------------- T1


def t1_package_labeler(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for k in _shuffled(list(ix.packages), rng):
        ndc = ix.packages[k].ndc

        def plan(s: ToolService, ndc: str = ndc) -> dict[str, Any]:
            return _as_gold(_hop(s, _resolve(s, ndc, "package"), "LABELER", "out"))

        yield Candidate({"ndc": ndc}, ids({ix.package_labeler[k]}), [k], plan)


def t1_product_ingredients(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for p in _shuffled(list(ix.products), rng):
        subs = set(ix.ingredients[p])
        if not 1 <= len(subs) <= MAX_GOLD:
            continue
        ndc = ix.products[p].product_ndc

        def plan(s: ToolService, ndc: str = ndc) -> dict[str, Any]:
            return _as_gold(_hop(s, _resolve(s, ndc, "product"), "INGREDIENT", "out"))

        yield Candidate({"product_ndc": ndc}, ids(subs), [p], plan)


def t1_recall_products(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for x in _shuffled(list(ix.recalls), rng):
        fact = ix.recalls[x]
        if not 1 <= len(fact.products) <= MAX_GOLD:
            continue

        def plan(s: ToolService, n: str = fact.number) -> dict[str, Any]:
            return _as_gold(_hop(s, _resolve(s, n, "recall"), "RECALLS_PRODUCT", "out"))

        yield Candidate({"recall_number": fact.number}, ids(set(fact.products)), [x], plan)


# ---------------------------------------------------------------------------------------------- T2


def t2_recall_firms_by_substance(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for sub in _shuffled(list(ix.substances), rng):
        products = ix.products_containing(sub)
        firms = ix.firms_recalling(products)
        if not 1 <= len(firms) <= MAX_GOLD or len(products) > MAX_FANOUT:
            continue

        def plan(s: ToolService, unii: str = ix.substances[sub].unii) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, unii, "substance"):
                found |= {x.firm_id for x in _call(s, "recalls_for", entity_id=iri).recalls}
            return _as_gold(found)

        yield Candidate(_sub(ix, sub), ids(firms), [sub], plan)


def t2_labelers_by_substance(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for sub in _shuffled(list(ix.substances), rng):
        products = ix.products_containing(sub)
        firms = ix.labelers_of(products)
        if not 1 <= len(firms) <= MAX_GOLD or len(products) > MAX_FANOUT:
            continue

        def plan(s: ToolService, unii: str = ix.substances[sub].unii) -> dict[str, Any]:
            products: set[str] = set()
            for iri in _resolve(s, unii, "substance"):
                products |= {p.id for p in _call(s, "products_with_substance", substance_id=iri).products}
            packages = _hop(s, products, "PRODUCT", "in", "PackagedProduct")
            return _as_gold(_hop(s, packages, "LABELER", "out"))

        yield Candidate(_sub(ix, sub), ids(firms), [sub], plan)


def t2_substances_recalled_by_firm(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for org in _shuffled(list(ix.orgs), rng):
        name = ix.orgs[org].name
        recalls = ix.recalls_by(org)
        products = {p for x in recalls for p in ix.recalls[x].products}
        subs = ix.substances_of(products)
        if not 1 <= len(subs) <= MAX_GOLD or not _unique_firm_name(ix, name) or len(products) > MAX_FANOUT:
            continue

        def plan(s: ToolService, name: str = name) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, name, "organization"):
                rec = {x.id for x in _call(s, "recalls_for", entity_id=iri).recalls}
                found |= _hop(s, _hop(s, rec, "RECALLS_PRODUCT", "out"), "INGREDIENT", "out")
            return _as_gold(found)

        yield Candidate({"firm": name}, ids(subs), [org], plan)


def t2_recalls_of_labeled_products(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for org in _shuffled(list(ix.orgs), rng):
        name = ix.orgs[org].name
        products = ix.products_labeled_by(org)
        recalls = ix.recalls_naming(products)
        if not 1 <= len(recalls) <= MAX_GOLD or not _unique_firm_name(ix, name) or len(products) > MAX_FANOUT:
            continue

        def plan(s: ToolService, name: str = name) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, name, "organization"):
                packages = _hop(s, [iri], "LABELER", "in", "PackagedProduct")
                found |= _hop(s, _hop(s, packages, "PRODUCT", "out"), "RECALLS_PRODUCT", "in")
            return _as_gold(found)

        yield Candidate({"firm": name}, ids(recalls), [org], plan)


# ---------------------------------------------------------------------------------------------- T3


def _ndc11_packages(ix: Index, rng: random.Random) -> list[str]:
    return [k for k in _shuffled(list(ix.packages), rng) if ix.packages[k].ndc11]


def t3_ndc11_labeler(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for k in _ndc11_packages(ix, rng):
        ndc11 = str(ix.packages[k].ndc11)

        def plan(s: ToolService, ndc11: str = ndc11) -> dict[str, Any]:
            return _as_gold(_hop(s, _resolve(s, ndc11, "package"), "LABELER", "out"))

        yield Candidate({"ndc11": ndc11}, ids({ix.package_labeler[k]}), [k], plan)


def t3_rxcui_products(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for d in _shuffled(list(ix.drugs), rng):
        products = ix.products_mapped_to(d)
        if not 2 <= len(products) <= MAX_GOLD:
            continue
        rxcui = ix.drugs[d].rxcui

        def plan(s: ToolService, rxcui: str = rxcui) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, rxcui, "clinical_drug"):
                found |= _mapped_products(s, iri)
            return _as_gold(found)

        yield Candidate({"rxcui": rxcui}, ids(products), [d], plan)


def _salt_moieties(ix: Index, rng: random.Random) -> list[str]:
    return [m for m in _shuffled(sorted(set(ix.moiety.values())), rng) if _unique_substance_name(ix, m)]


def t3_moiety_products(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for m in _salt_moieties(ix, rng):
        direct, salts = ix.products_containing(m), ix.products_containing(m, True)
        if len(salts) <= len(direct) or len(salts) > MAX_GOLD:
            continue

        def plan(s: ToolService, name: str = ix.substances[m].name) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, name, "substance"):
                res = _call(s, "products_with_substance", substance_id=iri, include_moiety_matches=True)
                found |= {p.id for p in res.products}
            return _as_gold(found)

        yield Candidate({"name": ix.substances[m].name}, ids(salts), [m], plan)


def _aliases(ix: Index, rng: random.Random, need: Callable[[str], bool]) -> list[tuple[str, str]]:
    """(organization, alias): variants that differ from the canonical name even after normalization come
    first, then variants that differ only in case, punctuation or corporate suffix."""
    strong: list[tuple[str, str]] = []
    weak: list[tuple[str, str]] = []
    names = {iri: normalize_firm(o.name) for iri, o in ix.orgs.items()}
    for org in _shuffled(list(ix.orgs), rng):
        o = ix.orgs[org]
        for a in sorted(o.aliases, key=lambda a: a.name):
            if not need(org) or not _unique_firm_name(ix, a.name):
                continue
            # An alias that names another organization in the data ("Azurity (formerly Arbor Pharmaceuticals)"
            # while Arbor Pharmaceuticals is its own firm) makes the gold ambiguous: not used.
            text = normalize_firm(a.name)
            if any(other != org and len(n) >= 8 and n in text for other, n in names.items()):
                continue
            (strong if normalize_firm(a.name) != normalize_firm(o.name) else weak).append((org, a.name))
    return strong + weak


def t3_alias_recalls(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for org, alias in _aliases(ix, rng, lambda o: 1 <= len(ix.recalls_by(o)) <= MAX_GOLD):

        def plan(s: ToolService, alias: str = alias) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, alias, "organization"):
                found |= {x.id for x in _call(s, "recalls_for", entity_id=iri).recalls}
            return _as_gold(found)

        yield Candidate({"firm": alias}, ids(ix.recalls_by(org)), [org], plan)


def _brands(ix: Index, rng: random.Random) -> list[str]:
    names: dict[str, int] = {}
    for d in ix.drugs.values():
        if d.name:
            names[normalize_name(d.name)] = names.get(normalize_name(d.name), 0) + 1
    return [
        b
        for b in _shuffled(list(ix.generic), rng)
        if ix.drugs[b].name and names[normalize_name(str(ix.drugs[b].name))] == 1
    ]


def _generic_plan(s: ToolService, name: str) -> set[str]:
    found: set[str] = set()
    for iri in _resolve(s, name, "clinical_drug"):
        for g in _hop(s, [iri], "HAS_GENERIC", "out"):
            found |= _mapped_products(s, g)
    return found


def t3_brand_generics(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for b in _brands(ix, rng):
        products = ix.products_mapped_to(ix.generic[b])
        if not 1 <= len(products) <= MAX_GOLD:
            continue
        name = str(ix.drugs[b].name)

        def plan(s: ToolService, name: str = name) -> dict[str, Any]:
            return _as_gold(_generic_plan(s, name))

        yield Candidate({"brand": name}, ids(products), [b], plan)


# ---------------------------------------------------------------------------------------------- T4


def t4_recall_firms_by_moiety(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for m in _salt_moieties(ix, rng):
        salts = ix.products_containing(m, True)
        firms, direct = ix.firms_recalling(salts), ix.firms_recalling(ix.products_containing(m))
        if firms == direct or not 1 <= len(firms) <= MAX_GOLD or len(salts) > MAX_FANOUT:
            continue

        def plan(s: ToolService, name: str = ix.substances[m].name) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, name, "substance"):
                res = _call(s, "products_with_substance", substance_id=iri, include_moiety_matches=True)
                rec = _hop(s, {p.id for p in res.products}, "RECALLS_PRODUCT", "in")
                found |= _hop(s, rec, "RECALLING_FIRM", "out")
            return _as_gold(found)

        yield Candidate({"name": ix.substances[m].name}, ids(firms), [m], plan)


def t4_substances_recalled_by_alias(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    def subs_of(org: str) -> set[str]:
        return ix.substances_of({p for x in ix.recalls_by(org) for p in ix.recalls[x].products})

    for org, alias in _aliases(ix, rng, lambda o: 1 <= len(subs_of(o)) <= MAX_GOLD):

        def plan(s: ToolService, alias: str = alias) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, alias, "organization"):
                rec = {x.id for x in _call(s, "recalls_for", entity_id=iri).recalls}
                found |= _hop(s, _hop(s, rec, "RECALLS_PRODUCT", "out"), "INGREDIENT", "out")
            return _as_gold(found)

        yield Candidate({"firm": alias}, ids(subs_of(org)), [org], plan)


def t4_labelers_by_rxcui(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for d in _shuffled(list(ix.drugs), rng):
        products = ix.products_mapped_to(d)
        firms = ix.labelers_of(products)
        if not 2 <= len(firms) <= MAX_GOLD or len(products) > MAX_FANOUT:
            continue
        rxcui = ix.drugs[d].rxcui

        def plan(s: ToolService, rxcui: str = rxcui) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, rxcui, "clinical_drug"):
                packages = _hop(s, _mapped_products(s, iri), "PRODUCT", "in", "PackagedProduct")
                found |= _hop(s, packages, "LABELER", "out")
            return _as_gold(found)

        yield Candidate({"rxcui": rxcui}, ids(firms), [d], plan)


def t4_recall_firms_by_ndc11(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for k in _ndc11_packages(ix, rng):
        firms = ix.firms_recalling({ix.package_product[k]})
        if not firms:
            continue
        ndc11 = str(ix.packages[k].ndc11)

        def plan(s: ToolService, ndc11: str = ndc11) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, ndc11, "package"):
                found |= {x.firm_id for x in _call(s, "recalls_for", entity_id=iri).recalls}
            return _as_gold(found)

        yield Candidate({"ndc11": ndc11}, ids(firms), [k], plan)


def t4_recalls_of_generics(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for b in _brands(ix, rng):
        products = ix.products_mapped_to(ix.generic[b])
        recalls = ix.recalls_naming(products)
        if not 1 <= len(recalls) <= MAX_GOLD or len(products) > MAX_FANOUT:
            continue
        name = str(ix.drugs[b].name)

        def plan(s: ToolService, name: str = name) -> dict[str, Any]:
            return _as_gold(_hop(s, _generic_plan(s, name), "RECALLS_PRODUCT", "in"))

        yield Candidate({"brand": name}, ids(recalls), [b], plan)


# ---------------------------------------------------------------------------------------------- T5


def t5_serious_reports_by_substance(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for sub in _shuffled(list(ix.substances), rng):
        reports = [ix.reports[x] for x in ix.reports_involving(ix.products_containing(sub))]
        years = sorted({x.received.year for x in reports if x.serious})
        if not years:
            continue
        year = rng.choice(years)
        lo, hi = date(year, 1, 1), date(year, 12, 31)
        n = sum(1 for x in reports if x.serious and lo <= x.received <= hi)

        def plan(s: ToolService, unii: str = ix.substances[sub].unii, lo: date = lo, hi: date = hi) -> dict[str, Any]:
            total = 0
            for iri in _resolve(s, unii, "substance"):
                args = {"what": "reports", "related_to": iri, "date_from": lo, "date_to": hi, "serious_only": True}
                total += s.call("count", args).count  # type: ignore[attr-defined]
            return number(total)

        yield Candidate({**_sub(ix, sub), "from": lo.isoformat(), "to": hi.isoformat()}, number(n), [sub], plan)


def t5_recalls_by_class(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for org in _shuffled(list(ix.orgs), rng):
        name = ix.orgs[org].name
        classes = sorted({ix.recalls[x].classification for x in ix.recalls_by(org)})
        if not classes or not _unique_firm_name(ix, name):
            continue
        c = rng.choice(classes)
        n = sum(1 for x in ix.recalls_by(org) if ix.recalls[x].classification == c)

        def plan(s: ToolService, name: str = name, c: str = c) -> dict[str, Any]:
            iris = _resolve(s, name, "organization")
            args = {"what": "recalls", "classification": c}
            return number(sum(s.call("count", {**args, "related_to": i}).count for i in iris))  # type: ignore[attr-defined]

        yield Candidate({"firm": name, "class": c}, number(n), [org], plan)


def t5_reports_for_product(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for p in _shuffled(list(ix.products), rng):
        n = len(ix.reports_involving({p}))
        if n < 1:
            continue
        ndc = ix.products[p].product_ndc

        def plan(s: ToolService, ndc: str = ndc) -> dict[str, Any]:
            iris = _resolve(s, ndc, "product")
            return number(sum(s.call("count", {"what": "reports", "related_to": i}).count for i in iris))  # type: ignore[attr-defined]

        yield Candidate({"product_ndc": ndc}, number(n), [p], plan)


def t5_products_labeled(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for org in _shuffled(list(ix.orgs), rng):
        name = ix.orgs[org].name
        n = len(ix.products_labeled_by(org))
        if n < 2 or not _unique_firm_name(ix, name):
            continue

        def plan(s: ToolService, name: str = name) -> dict[str, Any]:
            iris = _resolve(s, name, "organization")
            return number(sum(s.call("count", {"what": "products", "related_to": i}).count for i in iris))  # type: ignore[attr-defined]

        yield Candidate({"firm": name}, number(n), [org], plan)


# ---------------------------------------------------------------------------------------------- T6

# Plausible firm names, checked at generation time to match no organization or alias after normalization.
FAKE_FIRMS = (
    "Northbridge Pharmaceuticals LLC",
    "Calder Ridge Laboratories, Inc.",
    "Westmoor Health Products Inc.",
    "Halvorsen Generics USA, Inc.",
    "Brightwater Pharma Corp.",
    "Kestrel Bay Pharmaceuticals, LLC",
    "Sable Point Medical Supply Inc",
    "Orchard Lane Laboratories LLC",
    "Marlowe & Pike Healthcare, Inc.",
    "Thornfield Pharmacal Company",
    "Bellweather Rx Solutions LLC",
    "Granite Harbor Pharmaceuticals Inc.",
)


def _none_plan(text: str, kind: str) -> Callable[[ToolService], dict[str, Any]]:
    def plan(s: ToolService) -> dict[str, Any]:
        return _as_gold(set(_resolve(s, text, kind)))

    return plan


def t6_fake_package(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    existing = {p.ndc for p in ix.packages.values()}
    for k in _shuffled(list(ix.packages), rng):
        ndc = ix.packages[k].ndc
        head, tail = ndc.rsplit("-", 1)
        options = [f"{head}-{str(v).zfill(len(tail))}" for v in range(10 ** len(tail))]
        fake = next((o for o in _shuffled(options, rng) if o not in existing), None)
        if fake is not None:
            yield Candidate({"ndc": fake}, {"kind": "none"}, [], _none_plan(fake, "package"))


def t6_fake_unii(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    existing = {s.unii for s in ix.substances.values()}
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    while True:
        fake = "".join(rng.choice(alphabet) for _ in range(10))
        if fake not in existing:
            yield Candidate({"unii": fake}, {"kind": "none"}, [], _none_plan(fake, "substance"))


def t6_substance_without_recalls(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    for sub in _shuffled(list(ix.substances), rng):
        products = ix.products_containing(sub)
        if not products or ix.firms_recalling(products) or len(products) > MAX_FANOUT:
            continue

        def plan(s: ToolService, unii: str = ix.substances[sub].unii) -> dict[str, Any]:
            found: set[str] = set()
            for iri in _resolve(s, unii, "substance"):
                found |= {x.firm_id for x in _call(s, "recalls_for", entity_id=iri).recalls}
            return _as_gold(found)

        yield Candidate(_sub(ix, sub), {"kind": "none"}, [sub], plan)


def t6_fake_firm(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    names = {normalize_firm(o.name) for o in ix.orgs.values()} | {
        normalize_firm(a.name) for o in ix.orgs.values() for a in o.aliases
    }
    for fake in _shuffled(list(FAKE_FIRMS), rng):
        if normalize_firm(fake) not in names:
            yield Candidate({"firm": fake}, {"kind": "none"}, [], _none_plan(fake, "organization"))


def t6_fake_rxcui(ix: Index, rng: random.Random) -> Iterator[Candidate]:
    existing = {d.rxcui for d in ix.drugs.values()}
    while True:
        fake = str(rng.randrange(100_000, 2_999_999))
        if fake not in existing:
            yield Candidate({"rxcui": fake}, {"kind": "none"}, [], _none_plan(fake, "clinical_drug"))


# -------------------------------------------------------------------------------------- the templates

TEMPLATES: tuple[Template, ...] = (
    Template(
        "t1_package_labeler",
        "T1",
        (
            "Which firm labels the package with NDC {ndc}?",
            "Who is the labeler of package NDC {ndc}?",
            "Name the firm that labels the package {ndc}.",
            "Which organization is the labeler of the package with NDC {ndc}?",
        ),
        t1_package_labeler,
    ),
    Template(
        "t1_product_ingredients",
        "T1",
        (
            "Which substances are the active ingredients of the product with product NDC {product_ndc}?",
            "What are the active ingredients of product NDC {product_ndc}?",
            "List the active ingredient substances of the product {product_ndc}.",
        ),
        t1_product_ingredients,
    ),
    Template(
        "t1_recall_products",
        "T1",
        (
            "Which products does recall {recall_number} name?",
            "What products are named in recall {recall_number}?",
            "List the products covered by recall {recall_number}.",
        ),
        t1_recall_products,
    ),
    Template(
        "t2_recall_firms_by_substance",
        "T2",
        (
            "Which firms recalled products that contain {name} (UNII {unii})?",
            "Which recalling firms issued recalls of products containing {name} (UNII {unii})?",
            "Name the firms that recalled a product with the active ingredient {name} (UNII {unii}).",
            "Which organizations recalled products that have {name} (UNII {unii}) as an active ingredient?",
        ),
        t2_recall_firms_by_substance,
    ),
    Template(
        "t2_labelers_by_substance",
        "T2",
        (
            "Which firms label products that contain {name} (UNII {unii})?",
            "Who are the labelers of products containing {name} (UNII {unii})?",
            "Name the firms that label a product with the active ingredient {name} (UNII {unii}).",
        ),
        t2_labelers_by_substance,
    ),
    Template(
        "t2_substances_recalled_by_firm",
        "T2",
        (
            "Which substances are active ingredients of products recalled by {firm}?",
            "What active ingredients are in the products that {firm} recalled?",
            "List the active ingredient substances of products recalled by {firm}.",
        ),
        t2_substances_recalled_by_firm,
    ),
    Template(
        "t2_recalls_of_labeled_products",
        "T2",
        (
            "Which recalls name products labeled by {firm}?",
            "Which recalls cover a product that {firm} labels?",
            "List the recalls that name a product labeled by {firm}.",
        ),
        t2_recalls_of_labeled_products,
    ),
    Template(
        "t3_ndc11_labeler",
        "T3",
        (
            "Which firm labels the package with 11-digit NDC {ndc11}?",
            "Who is the labeler of the package whose NDC in 11-digit form is {ndc11}?",
            "Name the labeler of package {ndc11} (11-digit NDC).",
        ),
        t3_ndc11_labeler,
    ),
    Template(
        "t3_rxcui_products",
        "T3",
        (
            "Which products are mapped to RxCUI {rxcui}?",
            "Which products correspond to the RxNorm clinical drug with RxCUI {rxcui}?",
            "List the products mapped to RxCUI {rxcui}.",
        ),
        t3_rxcui_products,
    ),
    Template(
        "t3_moiety_products",
        "T3",
        (
            "Which products contain {name}, counting salt forms?",
            "Which products contain {name} as an active ingredient, including its salt and ester forms?",
            "List the products whose active ingredients include {name}, counting salt forms.",
        ),
        t3_moiety_products,
    ),
    Template(
        "t3_alias_recalls",
        "T3",
        (
            "Which recalls were issued by {firm}?",
            "Which recalls did {firm} issue as recalling firm?",
            "List the recalls whose recalling firm is {firm}.",
        ),
        t3_alias_recalls,
    ),
    Template(
        "t3_brand_generics",
        "T3",
        (
            "Which products are generic equivalents of the brand drug {brand}?",
            "List the products that are generic equivalents of {brand}.",
            "Which products are mapped to the generic clinical drug of the brand {brand}?",
        ),
        t3_brand_generics,
    ),
    Template(
        "t4_recall_firms_by_moiety",
        "T4",
        (
            "Which firms recalled products that contain {name}, counting salt forms?",
            "Which recalling firms issued recalls of products containing {name}, including its salt and ester forms?",
            "Name the firms that recalled a product containing {name}, counting salt forms.",
        ),
        t4_recall_firms_by_moiety,
    ),
    Template(
        "t4_substances_recalled_by_alias",
        "T4",
        (
            "Which substances are active ingredients of products recalled by {firm}?",
            "What active ingredients are in the products that {firm} recalled?",
            "List the active ingredient substances of products recalled by {firm}.",
        ),
        t4_substances_recalled_by_alias,
    ),
    Template(
        "t4_labelers_by_rxcui",
        "T4",
        (
            "Which firms label packages of products mapped to RxCUI {rxcui}?",
            "Who are the labelers of products mapped to RxCUI {rxcui}?",
            "Name the firms that label a product mapped to the RxNorm clinical drug {rxcui}.",
        ),
        t4_labelers_by_rxcui,
    ),
    Template(
        "t4_recall_firms_by_ndc11",
        "T4",
        (
            "Which firms recalled the product packaged under 11-digit NDC {ndc11}?",
            "Which recalling firms issued recalls of the product whose package has the 11-digit NDC {ndc11}?",
            "Name the firms that recalled the product of package {ndc11} (11-digit NDC).",
        ),
        t4_recall_firms_by_ndc11,
    ),
    Template(
        "t4_recalls_of_generics",
        "T4",
        (
            "Which recalls name generic equivalents of the brand drug {brand}?",
            "Which recalls cover a product that is a generic equivalent of {brand}?",
            "List the recalls of products that are generic equivalents of the brand {brand}.",
        ),
        t4_recalls_of_generics,
    ),
    Template(
        "t5_serious_reports_by_substance",
        "T5",
        (
            "How many serious adverse event reports received from {from} to {to} involve products that contain"
            " {name} (UNII {unii})?",
            "Between {from} and {to}, how many serious adverse event reports involve products containing {name}"
            " (UNII {unii})?",
            "Count the serious adverse event reports received from {from} to {to} that involve a product with the"
            " active ingredient {name} (UNII {unii}).",
        ),
        t5_serious_reports_by_substance,
    ),
    Template(
        "t5_recalls_by_class",
        "T5",
        (
            "How many class {class} recalls did {firm} issue?",
            "How many recalls of class {class} have {firm} as recalling firm?",
            "Count the class {class} recalls issued by {firm}.",
        ),
        t5_recalls_by_class,
    ),
    Template(
        "t5_reports_for_product",
        "T5",
        (
            "How many adverse event reports involve the product with product NDC {product_ndc}?",
            "How many adverse event reports involve product NDC {product_ndc}?",
            "Count the adverse event reports that involve the product {product_ndc}.",
        ),
        t5_reports_for_product,
    ),
    Template(
        "t5_products_labeled",
        "T5",
        (
            "How many products does {firm} label?",
            "How many distinct products have a package labeled by {firm}?",
            "Count the products labeled by {firm}.",
        ),
        t5_products_labeled,
    ),
    Template(
        "t6_fake_package",
        "T6",
        (
            "Which firm labels the package with NDC {ndc}?",
            "Who is the labeler of package NDC {ndc}?",
            "Name the firm that labels the package {ndc}.",
        ),
        t6_fake_package,
        "an NDC of a real product with a package segment that does not exist",
    ),
    Template(
        "t6_fake_unii",
        "T6",
        (
            "Which firms recalled products that contain the substance with UNII {unii}?",
            "Which recalling firms issued recalls of products containing the substance with UNII {unii}?",
            "Name the firms that recalled a product whose active ingredient has UNII {unii}.",
        ),
        t6_fake_unii,
        "a well-formed UNII that is not in the data",
    ),
    Template(
        "t6_substance_without_recalls",
        "T6",
        (
            "Which firms recalled products that contain {name} (UNII {unii})?",
            "Which recalling firms issued recalls of products containing {name} (UNII {unii})?",
            "Name the firms that recalled a product with the active ingredient {name} (UNII {unii}).",
        ),
        t6_substance_without_recalls,
        "a real substance whose products were never recalled: no path",
    ),
    Template(
        "t6_fake_firm",
        "T6",
        (
            "Which recalls were issued by {firm}?",
            "Which recalls did {firm} issue as recalling firm?",
            "List the recalls whose recalling firm is {firm}.",
        ),
        t6_fake_firm,
        "a plausible firm name that matches no organization or alias",
    ),
    Template(
        "t6_fake_rxcui",
        "T6",
        (
            "Which products are mapped to RxCUI {rxcui}?",
            "Which products correspond to the RxNorm clinical drug with RxCUI {rxcui}?",
            "List the products mapped to RxCUI {rxcui}.",
        ),
        t6_fake_rxcui,
        "an RxCUI that is not in the data",
    ),
)


@dataclass
class GenerationReport:
    tried: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)
    rejected_mismatch: dict[str, int] = field(default_factory=dict)
    rejected_truncated: dict[str, int] = field(default_factory=dict)
