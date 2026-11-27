"""H2.1 questions: templates, deterministic gold per identity level, tool plans, generation.

Phrasings are fixed strings written by hand. Gold is computed by code over the validated instances of each
identity level (`Index`), never by an LLM, with one rule for identity references (docs/h2_1/PLAN.md): the
identity reference of a T3 or T4 question is resolved with the identity layer, since the recorded pair
defines what the question refers to, and every other hop follows that cell's own data. T1, T9, T10, T11 and
T6 use only relations that are the same in every cell, so their gold is the same in every cell (checked).

Each candidate carries a tool plan per identity level. Generation keeps a candidate only when, in every
cell, the plan reproduces that cell's gold on the SQL store with no truncated result and no result longer
than what the agents are sent; every kept question is then re-checked on every cell's Cypher store.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from monograph.extract.resolve import normalize_name
from monograph.graph.identity import normalize_firm
from monograph.qa.generate import largest_remainder
from monograph.qa.index import Index
from monograph.tools import registry as r
from monograph.tools.registry import ToolError
from monograph.tools.service import ToolService

MAX_GOLD = 30
FANOUT_MIN, FANOUT_MAX = 50, 150
MAX_FANOUT = 300  # intermediate sets in a plan (6 neighbors calls of 50 ids)
PER_TYPE = 60
DEV_PER_TYPE = 15
MAX_TRIES = 600
RESULT_CHAR_CAP = 60000  # AgentSettings.result_char_cap for H2.1
RESOLVE_TEXT_MAX = 200  # ResolveEntityIn.text max_length

# The relations a "step" may follow (T9 to T11), in either direction: the same in every cell.
STEP_RELATIONS: tuple[str, ...] = ("PRODUCT", "LABELER", "INGREDIENT", "RECALLS_PRODUCT")

Gold = dict[str, Any]
Plan = Callable[[ToolService], Gold]


class Truncated(Exception):
    """A tool result was capped (rows or characters): not answerable within the caps, so not used."""


@dataclass(frozen=True)
class Candidate:
    params: dict[str, str]
    anchors: dict[bool, list[str]]  # identity level -> ids of the entities the question names
    gold: dict[bool, Gold]  # identity level -> gold
    plan: dict[bool, Plan]  # identity level -> tool plan


@dataclass(frozen=True)
class Template:
    id: str
    type: str
    phrasings: tuple[str, ...]
    candidates: Callable[[Index, Index, random.Random], Iterator[Candidate]]
    note: str = ""


def ids(values: Iterable[str]) -> Gold:
    found = sorted(set(values))
    return {"kind": "ids", "ids": found} if found else {"kind": "none"}


def number(n: int) -> Gold:
    return {"kind": "number", "number": n}


NONE: Gold = {"kind": "none"}


# --------------------------------------------------------------------------------------- tool helpers


def _call(s: ToolService, name: str, **kw: Any) -> Any:
    out = s.call(name, kw)
    if getattr(out, "truncated", False) or len(out.model_dump_json()) > RESULT_CHAR_CAP:
        raise Truncated(name)
    return out


def _resolve(s: ToolService, text: str, kind: str) -> list[str]:
    return sorted({c.id for c in _call(s, "resolve_entity", text=text, kind=kind).candidates})


def _hop(s: ToolService, start: Iterable[str], relation: str, direction: str, kind: str | None = None) -> set[str]:
    out: set[str] = set()
    items = sorted(set(start))
    if len(items) > MAX_FANOUT:
        raise Truncated(relation)
    for i in range(0, len(items), r.MAX_IDS):
        res = _call(s, "neighbors", ids=items[i : i + r.MAX_IDS], relation=relation, direction=direction)
        out |= {n.id for n in res.neighbors if kind is None or n.kind == kind}
    return out


def _exist(s: ToolService, found: Iterable[str]) -> Gold:
    """The plan for an identity-off cell when the identity hop is the whole question: the resolved ids must
    exist in that store (they are what the question asks for)."""
    items = sorted(set(found))
    present: set[str] = set()
    for i in range(0, len(items), r.MAX_IDS):
        present |= {e.id for e in _call(s, "get_entity", ids=items[i : i + r.MAX_IDS]).entities}
    return ids(present)


def _mapped_products(s: ToolService, drug: str) -> set[str]:
    return _hop(s, [drug], "DRUG_MAPPING", "in", "Product") | _hop(s, [drug], "CLINICAL_DRUG", "in", "Product")


def _same(plan: Plan) -> dict[bool, Plan]:
    return {True: plan, False: plan}


def _both(ix_on: Index, ix_off: Index, f: Callable[..., Gold]) -> dict[bool, Gold]:
    return {True: f(ix_on), False: f(ix_off)}


# ------------------------------------------------------------------------------------- index helpers


def _shuffled[T](items: Sequence[T], rng: random.Random) -> list[T]:
    out = sorted(items)  # type: ignore[type-var]
    rng.shuffle(out)
    return out


def _sub(ix: Index, iri: str) -> dict[str, str]:
    s = ix.substances[iri]
    return {"name": s.name, "unii": s.unii}


def _unique_firm_name(ix: Index, name: str) -> bool:
    """With identity on, the name (normalized) belongs to exactly one organization, by name or alias."""
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


def _named(ix: Index, name: str) -> set[str]:
    """Organizations whose name is exactly `name` (with identity off a labeler and a recalling firm can share a
    name; questions name the firm, so gold covers every organization with that name)."""
    return {iri for iri, o in ix.orgs.items() if o.name == name}


def _labelers(ix: Index) -> list[str]:
    return sorted(set(ix.package_labeler.values()))


@dataclass
class Steps:
    """The step graph (STEP_RELATIONS, either direction) over one identity level's index."""

    adj: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    kind: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def of(ix: Index) -> Steps:
        g = Steps()

        def edge(a: str, b: str, ka: str, kb: str) -> None:
            g.adj[a].add(b)
            g.adj[b].add(a)
            g.kind[a], g.kind[b] = ka, kb

        for k, p in ix.package_product.items():
            edge(k, p, "PackagedProduct", "Product")
        for k, o in ix.package_labeler.items():
            edge(k, o, "PackagedProduct", "Organization")
        for p, subs in ix.ingredients.items():
            for s in subs:
                edge(p, s, "Product", "Substance")
        for x in ix.recalls.values():
            for p in x.products:
                edge(x.iri, p, "Recall", "Product")
        return g

    def near(self, nodes: Iterable[str], kind: str) -> set[str]:
        return {m for n in nodes for m in self.adj.get(n, ()) if self.kind[m] == kind}

    def within(self, starts: Iterable[str], n: int, kind: str) -> set[str]:
        start = set(starts)
        seen, frontier = set(start), set(start)
        for _ in range(n):
            frontier = {m for x in frontier for m in self.adj.get(x, ())} - seen
            seen |= frontier
        return {x for x in seen - start if self.kind.get(x) == kind}

    def distance(self, starts: Iterable[str], targets: set[str], limit: int) -> int | None:
        start = set(starts)
        if start & targets:
            return 0
        seen, frontier = set(start), set(start)
        for d in range(1, limit + 1):
            frontier = {m for x in frontier for m in self.adj.get(x, ())} - seen
            if frontier & targets:
                return d
            seen |= frontier
        return None


_STEPS: dict[int, Steps] = {}


def steps(ix: Index) -> Steps:
    key = id(ix)
    if key not in _STEPS:
        _STEPS[key] = Steps.of(ix)
    return _STEPS[key]


# ------------------------------------------------------------------------------ step plans (tools)


def _step_once(s: ToolService, frontier: set[str]) -> set[str]:
    out: set[str] = set()
    for relation in STEP_RELATIONS:
        out |= _hop(s, frontier, relation, "both")
    return out


def _plan_within(s: ToolService, starts: set[str], n: int, kind: str) -> set[str]:
    seen, frontier = set(starts), set(starts)
    kinds: dict[str, str] = {}
    for _ in range(n):
        nxt: set[str] = set()
        items = sorted(frontier)
        if len(items) > MAX_FANOUT:
            raise Truncated("frontier")
        for relation in STEP_RELATIONS:
            for i in range(0, len(items), r.MAX_IDS):
                res = _call(s, "neighbors", ids=items[i : i + r.MAX_IDS], relation=relation, direction="both")
                for nb in res.neighbors:
                    kinds[nb.id] = nb.kind
                    nxt.add(nb.id)
        frontier = nxt - seen
        seen |= frontier
    return {x for x in seen - starts if kinds.get(x) == kind}


def _plan_distance(s: ToolService, starts: set[str], targets: set[str], limit: int) -> int | None:
    seen, frontier = set(starts), set(starts)
    for d in range(1, limit + 1):
        frontier = _step_once(s, frontier) - seen
        if frontier & targets:
            return d
        seen |= frontier
        if len(frontier) > MAX_FANOUT:
            raise Truncated("frontier")
    return None


# ---------------------------------------------------------------------------------------------- T1


def t1_package_labeler(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for k in _shuffled(list(on.packages), rng):
        ndc = on.packages[k].ndc

        def plan(s: ToolService, ndc: str = ndc) -> Gold:
            return ids(_hop(s, _resolve(s, ndc, "package"), "LABELER", "out"))

        gold = _both(on, off, lambda ix, k=k: ids({ix.package_labeler[k]}))
        yield Candidate({"ndc": ndc}, {True: [k], False: [k]}, gold, _same(plan))


def t1_product_ingredients(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for p in _shuffled(list(on.products), rng):
        if not 1 <= len(set(on.ingredients[p])) <= MAX_GOLD:
            continue
        ndc = on.products[p].product_ndc

        def plan(s: ToolService, ndc: str = ndc) -> Gold:
            return ids(_hop(s, _resolve(s, ndc, "product"), "INGREDIENT", "out"))

        gold = _both(on, off, lambda ix, p=p: ids(ix.ingredients[p]))
        yield Candidate({"product_ndc": ndc}, {True: [p], False: [p]}, gold, _same(plan))


def t1_recall_products(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for x in _shuffled(list(on.recalls), rng):
        fact = on.recalls[x]
        if not 1 <= len(fact.products) <= MAX_GOLD:
            continue

        def plan(s: ToolService, n: str = fact.number) -> Gold:
            return ids(_hop(s, _resolve(s, n, "recall"), "RECALLS_PRODUCT", "out"))

        gold = _both(on, off, lambda ix, x=x: ids(ix.recalls[x].products))
        yield Candidate({"recall_number": fact.number}, {True: [x], False: [x]}, gold, _same(plan))


# ---------------------------------------------------------------------------------------------- T3


def _ndc11_packages(ix: Index, rng: random.Random) -> list[str]:
    return [k for k in _shuffled(list(ix.packages), rng) if ix.packages[k].ndc11]


def t3_ndc11_labeler(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for k in _ndc11_packages(on, rng):
        ndc11 = str(on.packages[k].ndc11)

        def plan_on(s: ToolService, ndc11: str = ndc11) -> Gold:
            return ids(_hop(s, _resolve(s, ndc11, "package"), "LABELER", "out"))

        def plan_off(s: ToolService, k: str = k) -> Gold:
            return ids(_hop(s, [k], "LABELER", "out"))

        gold = _both(on, off, lambda ix, k=k: ids({ix.package_labeler[k]}))
        yield Candidate({"ndc11": ndc11}, {True: [k], False: [k]}, gold, {True: plan_on, False: plan_off})


def t3_rxcui_products(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for d in _shuffled(list(on.drugs), rng):
        products = on.products_mapped_to(d)
        if not 2 <= len(products) <= MAX_GOLD:
            continue
        rxcui = on.drugs[d].rxcui

        def plan_on(s: ToolService, rxcui: str = rxcui) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, rxcui, "clinical_drug"):
                found |= _mapped_products(s, iri)
            return ids(found)

        def plan_off(s: ToolService, products: set[str] = products) -> Gold:
            return _exist(s, products)

        gold = {True: ids(products), False: ids(products)}
        yield Candidate({"rxcui": rxcui}, {True: [d], False: [d]}, gold, {True: plan_on, False: plan_off})


def _salt_moieties(ix: Index, rng: random.Random) -> list[str]:
    return [m for m in _shuffled(sorted(set(ix.moiety.values())), rng) if _unique_substance_name(ix, m)]


def t3_moiety_products(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for m in _salt_moieties(on, rng):
        direct, salts = on.products_containing(m), on.products_containing(m, True)
        if len(salts) <= len(direct) or len(salts) > MAX_GOLD:
            continue

        def plan_on(s: ToolService, name: str = on.substances[m].name) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, name, "substance"):
                res = _call(s, "products_with_substance", substance_id=iri, include_moiety_matches=True)
                found |= {p.id for p in res.products}
            return ids(found)

        def plan_off(s: ToolService, salts: set[str] = salts) -> Gold:
            return _exist(s, salts)

        gold = {True: ids(salts), False: ids(salts)}
        yield Candidate(
            {"name": on.substances[m].name}, {True: [m], False: [m]}, gold, {True: plan_on, False: plan_off}
        )


def _aliases(ix: Index, rng: random.Random, need: Callable[[str], bool]) -> list[tuple[str, str]]:
    """(organization, alias), as in H2: variants that differ even after normalization first."""
    strong: list[tuple[str, str]] = []
    weak: list[tuple[str, str]] = []
    names = {iri: normalize_firm(o.name) for iri, o in ix.orgs.items()}
    for org in _shuffled(list(ix.orgs), rng):
        o = ix.orgs[org]
        for a in sorted(o.aliases, key=lambda a: a.name):
            if not need(org) or not _unique_firm_name(ix, a.name):
                continue
            text = normalize_firm(a.name)
            if any(other != org and len(n) >= 8 and n in text for other, n in names.items()):
                continue
            (strong if normalize_firm(a.name) != normalize_firm(o.name) else weak).append((org, a.name))
    return strong + weak


def _firm_anchors(on: Index, off: Index, org: str) -> dict[bool, list[str]]:
    """With identity off, a merged firm is several organizations: the labeler (same IRI) and the recalling
    firms of its recalls."""
    split = {off.recalls[x].firm for x in on.recalls_by(org) if x in off.recalls}
    if org in off.orgs:
        split.add(org)
    return {True: [org], False: sorted(split)}


def t3_alias_recalls(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for org, alias in _aliases(on, rng, lambda o: 1 <= len(on.recalls_by(o)) <= MAX_GOLD):
        recalls = on.recalls_by(org)

        def plan_on(s: ToolService, alias: str = alias) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, alias, "organization"):
                found |= {x.id for x in _call(s, "recalls_for", entity_id=iri).recalls}
            return ids(found)

        def plan_off(s: ToolService, recalls: set[str] = recalls) -> Gold:
            return _exist(s, recalls)

        gold = {True: ids(recalls), False: ids(recalls)}
        yield Candidate({"firm": alias}, _firm_anchors(on, off, org), gold, {True: plan_on, False: plan_off})


def _brands(ix: Index, rng: random.Random) -> list[str]:
    names: dict[str, int] = {}
    for d in ix.drugs.values():
        if d.name:
            names[normalize_name(d.name)] = names.get(normalize_name(d.name), 0) + 1
    return [
        b
        for b in _shuffled(list(ix.generic), rng)
        if ix.drugs[b].name
        and names[normalize_name(str(ix.drugs[b].name))] == 1
        and len(str(ix.drugs[b].name)) <= RESOLVE_TEXT_MAX  # what resolve_entity accepts
    ]


def _generic_plan(s: ToolService, name: str) -> set[str]:
    found: set[str] = set()
    for iri in _resolve(s, name, "clinical_drug"):
        for g in _hop(s, [iri], "HAS_GENERIC", "out"):
            found |= _mapped_products(s, g)
    return found


def t3_brand_generics(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for b in _brands(on, rng):
        products = on.products_mapped_to(on.generic[b])
        if not 1 <= len(products) <= MAX_GOLD:
            continue
        name = str(on.drugs[b].name)

        def plan_on(s: ToolService, name: str = name) -> Gold:
            return ids(_generic_plan(s, name))

        def plan_off(s: ToolService, products: set[str] = products) -> Gold:
            return _exist(s, products)

        gold = {True: ids(products), False: ids(products)}
        yield Candidate({"brand": name}, {True: [b], False: [b]}, gold, {True: plan_on, False: plan_off})


# ---------------------------------------------------------------------------------------------- T4


def t4_recall_firms_by_moiety(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for m in _salt_moieties(on, rng):
        salts = on.products_containing(m, True)
        firms, direct = on.firms_recalling(salts), on.firms_recalling(on.products_containing(m))
        firms_off = off.firms_recalling(salts)
        if firms == direct or not 1 <= len(firms) <= MAX_GOLD or len(salts) > MAX_FANOUT:
            continue
        if not 1 <= len(firms_off) <= MAX_GOLD:
            continue

        def plan_on(s: ToolService, name: str = on.substances[m].name) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, name, "substance"):
                res = _call(s, "products_with_substance", substance_id=iri, include_moiety_matches=True)
                rec = _hop(s, {p.id for p in res.products}, "RECALLS_PRODUCT", "in")
                found |= _hop(s, rec, "RECALLING_FIRM", "out")
            return ids(found)

        def plan_off(s: ToolService, salts: set[str] = salts) -> Gold:
            return ids(_hop(s, _hop(s, salts, "RECALLS_PRODUCT", "in"), "RECALLING_FIRM", "out"))

        gold = {True: ids(firms), False: ids(firms_off)}
        yield Candidate(
            {"name": on.substances[m].name}, {True: [m], False: [m]}, gold, {True: plan_on, False: plan_off}
        )


def t4_substances_recalled_by_alias(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    def products_of(org: str) -> set[str]:
        return {p for x in on.recalls_by(org) for p in on.recalls[x].products}

    for org, alias in _aliases(on, rng, lambda o: 1 <= len(on.substances_of(products_of(o))) <= MAX_GOLD):
        recalls = on.recalls_by(org)

        def plan_on(s: ToolService, alias: str = alias) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, alias, "organization"):
                rec = {x.id for x in _call(s, "recalls_for", entity_id=iri).recalls}
                found |= _hop(s, _hop(s, rec, "RECALLS_PRODUCT", "out"), "INGREDIENT", "out")
            return ids(found)

        def plan_off(s: ToolService, recalls: set[str] = recalls) -> Gold:
            return ids(_hop(s, _hop(s, recalls, "RECALLS_PRODUCT", "out"), "INGREDIENT", "out"))

        gold = _both(
            on, off, lambda ix, rec=recalls: ids(ix.substances_of({p for x in rec for p in ix.recalls[x].products}))
        )
        yield Candidate({"firm": alias}, _firm_anchors(on, off, org), gold, {True: plan_on, False: plan_off})


def t4_labelers_by_rxcui(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for d in _shuffled(list(on.drugs), rng):
        products = on.products_mapped_to(d)
        firms = on.labelers_of(products)
        if not 2 <= len(firms) <= MAX_GOLD or len(products) > MAX_FANOUT:
            continue
        rxcui = on.drugs[d].rxcui

        def plan_on(s: ToolService, rxcui: str = rxcui) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, rxcui, "clinical_drug"):
                packages = _hop(s, _mapped_products(s, iri), "PRODUCT", "in", "PackagedProduct")
                found |= _hop(s, packages, "LABELER", "out")
            return ids(found)

        def plan_off(s: ToolService, products: set[str] = products) -> Gold:
            return ids(_hop(s, _hop(s, products, "PRODUCT", "in", "PackagedProduct"), "LABELER", "out"))

        gold = _both(on, off, lambda ix, ps=products: ids(ix.labelers_of(ps)))
        yield Candidate({"rxcui": rxcui}, {True: [d], False: [d]}, gold, {True: plan_on, False: plan_off})


def t4_recall_firms_by_ndc11(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for k in _ndc11_packages(on, rng):
        product = on.package_product[k]
        firms, firms_off = on.firms_recalling({product}), off.firms_recalling({product})
        if not firms or not firms_off:
            continue
        ndc11 = str(on.packages[k].ndc11)

        def plan_on(s: ToolService, ndc11: str = ndc11) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, ndc11, "package"):
                found |= {x.firm_id for x in _call(s, "recalls_for", entity_id=iri).recalls}
            return ids(found)

        def plan_off(s: ToolService, k: str = k) -> Gold:
            return ids({x.firm_id for x in _call(s, "recalls_for", entity_id=k).recalls})

        gold = {True: ids(firms), False: ids(firms_off)}
        yield Candidate({"ndc11": ndc11}, {True: [k], False: [k]}, gold, {True: plan_on, False: plan_off})


def t4_recalls_of_generics(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for b in _brands(on, rng):
        products = on.products_mapped_to(on.generic[b])
        recalls = on.recalls_naming(products)
        if not 1 <= len(recalls) <= MAX_GOLD or len(products) > MAX_FANOUT:
            continue
        name = str(on.drugs[b].name)

        def plan_on(s: ToolService, name: str = name) -> Gold:
            return ids(_hop(s, _generic_plan(s, name), "RECALLS_PRODUCT", "in"))

        def plan_off(s: ToolService, products: set[str] = products) -> Gold:
            return ids(_hop(s, products, "RECALLS_PRODUCT", "in"))

        gold = _both(on, off, lambda ix, ps=products: ids(ix.recalls_naming(ps)))
        yield Candidate({"brand": name}, {True: [b], False: [b]}, gold, {True: plan_on, False: plan_off})


# ---------------------------------------------------------------------------------------------- T9


def _products_of_firms(st: Steps, firms: set[str]) -> set[str]:
    return st.near(st.near(firms, "PackagedProduct"), "Product")


def _firms_of_products(st: Steps, products: set[str]) -> set[str]:
    return st.near(st.near(products, "PackagedProduct"), "Organization")


def t9_recalls_sharing_an_ingredient(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    """4 hops: recall -> products -> substances -> products -> recalls (other than the start)."""

    def gold_of(ix: Index, x: str) -> Gold:
        st = steps(ix)
        return ids(st.near(st.near(st.near(st.near({x}, "Product"), "Substance"), "Product"), "Recall") - {x})

    for x in _shuffled(list(on.recalls), rng):
        g = gold_of(on, x)
        if g["kind"] != "ids" or len(g["ids"]) > MAX_GOLD:
            continue
        number_ = on.recalls[x].number

        def plan(s: ToolService, n: str = number_) -> Gold:
            start = set(_resolve(s, n, "recall"))
            products = _hop(
                s, _hop(s, _hop(s, start, "RECALLS_PRODUCT", "out"), "INGREDIENT", "out"), "INGREDIENT", "in"
            )
            return ids(_hop(s, products, "RECALLS_PRODUCT", "in") - start)

        yield Candidate(
            {"recall_number": number_},
            {True: [x], False: [x]},
            _both(on, off, lambda ix, x=x: gold_of(ix, x)),
            _same(plan),
        )


def t9_recalls_of_colabeled_products(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    """5 hops: product -> packages -> labelers -> packages -> products -> recalls."""

    def gold_of(ix: Index, p: str) -> Gold:
        st = steps(ix)
        return ids(st.near(_products_of_firms(st, _firms_of_products(st, {p})), "Recall"))

    for p in _shuffled(list(on.products), rng):
        g = gold_of(on, p)
        if g["kind"] != "ids" or len(g["ids"]) > MAX_GOLD:
            continue
        ndc = on.products[p].product_ndc

        def plan(s: ToolService, ndc: str = ndc) -> Gold:
            packages = _hop(s, _resolve(s, ndc, "product"), "PRODUCT", "in")
            firms = _hop(s, packages, "LABELER", "out")
            products = _hop(s, _hop(s, firms, "LABELER", "in"), "PRODUCT", "out")
            return ids(_hop(s, products, "RECALLS_PRODUCT", "in"))

        yield Candidate(
            {"product_ndc": ndc}, {True: [p], False: [p]}, _both(on, off, lambda ix, p=p: gold_of(ix, p)), _same(plan)
        )


def t9_labelers_of_ingredient_sharers(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    """5 hops: product -> substances -> products -> packages -> labelers."""

    def gold_of(ix: Index, p: str) -> Gold:
        st = steps(ix)
        return ids(_firms_of_products(st, st.near(st.near({p}, "Substance"), "Product")))

    for p in _shuffled(list(on.products), rng):
        g = gold_of(on, p)
        if g["kind"] != "ids" or len(g["ids"]) > MAX_GOLD:
            continue
        ndc = on.products[p].product_ndc

        def plan(s: ToolService, ndc: str = ndc) -> Gold:
            products = _hop(s, _hop(s, _resolve(s, ndc, "product"), "INGREDIENT", "out"), "INGREDIENT", "in")
            return ids(_hop(s, _hop(s, products, "PRODUCT", "in"), "LABELER", "out"))

        yield Candidate(
            {"product_ndc": ndc}, {True: [p], False: [p]}, _both(on, off, lambda ix, p=p: gold_of(ix, p)), _same(plan)
        )


def t9_substances_of_colabeled_recall_products(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    """6 hops: recall -> products -> packages -> labelers -> packages -> products -> substances."""

    def gold_of(ix: Index, x: str) -> Gold:
        st = steps(ix)
        return ids(st.near(_products_of_firms(st, _firms_of_products(st, st.near({x}, "Product"))), "Substance"))

    for x in _shuffled(list(on.recalls), rng):
        g = gold_of(on, x)
        if g["kind"] != "ids" or len(g["ids"]) > MAX_GOLD:
            continue
        number_ = on.recalls[x].number

        def plan(s: ToolService, n: str = number_) -> Gold:
            products = _hop(s, _resolve(s, n, "recall"), "RECALLS_PRODUCT", "out")
            firms = _hop(s, _hop(s, products, "PRODUCT", "in"), "LABELER", "out")
            others = _hop(s, _hop(s, firms, "LABELER", "in"), "PRODUCT", "out")
            return ids(_hop(s, others, "INGREDIENT", "out"))

        yield Candidate(
            {"recall_number": number_},
            {True: [x], False: [x]},
            _both(on, off, lambda ix, x=x: gold_of(ix, x)),
            _same(plan),
        )


# --------------------------------------------------------------------------------------------- T10


def _firm_names(on: Index, rng: random.Random) -> list[tuple[str, str]]:
    """(labeler, name) for labelers whose name is unique with identity on (by name or alias, normalized)."""
    out = []
    for org in _shuffled(_labelers(on), rng):
        name = on.orgs[org].name
        if _unique_firm_name(on, name):
            out.append((org, name))
    return out


def t10_firms_within_six_steps(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    def gold_of(ix: Index, name: str) -> Gold:
        starts = _named(ix, name)
        return ids(steps(ix).within(starts, 6, "Organization") - starts)

    for org, name in _firm_names(on, rng):
        g = gold_of(on, name)
        if g["kind"] != "ids" or len(g["ids"]) > MAX_GOLD:
            continue

        def plan(s: ToolService, name: str = name) -> Gold:
            starts = set(_resolve(s, name, "organization"))
            return ids(_plan_within(s, starts, 6, "Organization") - starts)

        anchors = {True: [org], False: sorted(_named(off, name))}
        yield Candidate({"firm": name}, anchors, _both(on, off, lambda ix, n=name: gold_of(ix, n)), _same(plan))


def t10_substances_within_four_steps(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    def gold_of(ix: Index, x: str) -> Gold:
        return ids(steps(ix).within({x}, 4, "Substance"))

    for x in _shuffled(list(on.recalls), rng):
        g = gold_of(on, x)
        direct = steps(on).within({x}, 2, "Substance")
        if g["kind"] != "ids" or len(g["ids"]) > MAX_GOLD or set(g["ids"]) == direct:
            continue  # the 4-step set must reach past the recall's own products' ingredients
        number_ = on.recalls[x].number

        def plan(s: ToolService, n: str = number_) -> Gold:
            return ids(_plan_within(s, set(_resolve(s, n, "recall")), 4, "Substance"))

        yield Candidate(
            {"recall_number": number_},
            {True: [x], False: [x]},
            _both(on, off, lambda ix, x=x: gold_of(ix, x)),
            _same(plan),
        )


def t10_recalls_within_four_steps(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    def gold_of(ix: Index, p: str) -> Gold:
        return ids(steps(ix).within({p}, 4, "Recall"))

    for p in _shuffled(list(on.products), rng):
        g = gold_of(on, p)
        direct = steps(on).within({p}, 1, "Recall")
        if g["kind"] != "ids" or len(g["ids"]) > MAX_GOLD or set(g["ids"]) == direct:
            continue
        ndc = on.products[p].product_ndc

        def plan(s: ToolService, ndc: str = ndc) -> Gold:
            return ids(_plan_within(s, set(_resolve(s, ndc, "product")), 4, "Recall"))

        yield Candidate(
            {"product_ndc": ndc}, {True: [p], False: [p]}, _both(on, off, lambda ix, p=p: gold_of(ix, p)), _same(plan)
        )


def t10_fewest_steps_substance_to_firm(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    """The fewest steps between a substance and a labeler: 5 or 7 (3 would be a labeler of the substance's own
    products, a fixed path)."""
    firms = _firm_names(on, rng)
    for sub in _shuffled([s for s in on.substances if _unique_substance_name(on, s)], rng):
        st = steps(on)
        ring = st.within({sub}, 7, "Organization") - st.within({sub}, 3, "Organization")
        choices = [(o, n) for o, n in firms if o in ring]
        if not choices:
            continue
        org, name = choices[0]

        def gold_of(ix: Index, sub: str = sub, name: str = name) -> Gold:
            d = steps(ix).distance({sub}, _named(ix, name), 8)
            return number(d) if d is not None else NONE

        if gold_of(on)["kind"] != "number":
            continue

        def plan(s: ToolService, unii: str = on.substances[sub].unii, name: str = name) -> Gold:
            d = _plan_distance(s, set(_resolve(s, unii, "substance")), set(_resolve(s, name, "organization")), 8)
            return number(d) if d is not None else NONE

        params = {**_sub(on, sub), "firm": name}
        anchors = {True: [sub, org], False: [sub, *sorted(_named(off, name))]}
        yield Candidate(params, anchors, _both(on, off, gold_of), _same(plan))


# --------------------------------------------------------------------------------------------- T11


def _fanout(values: set[str]) -> bool:
    return FANOUT_MIN <= len(values) <= FANOUT_MAX


def t11_products_with_substance(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for sub in _shuffled(list(on.substances), rng):
        if not _fanout(on.products_containing(sub)):
            continue

        def plan(s: ToolService, unii: str = on.substances[sub].unii) -> Gold:
            return ids(_hop(s, _resolve(s, unii, "substance"), "INGREDIENT", "in"))

        gold = _both(on, off, lambda ix, sub=sub: ids(ix.products_containing(sub)))
        yield Candidate(_sub(on, sub), {True: [sub], False: [sub]}, gold, _same(plan))


def t11_packages_of_substance(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    def packages(ix: Index, sub: str) -> set[str]:
        ps = ix.products_containing(sub)
        return {k for k, p in ix.package_product.items() if p in ps}

    for sub in _shuffled(list(on.substances), rng):
        if not _fanout(packages(on, sub)) or len(on.products_containing(sub)) > MAX_FANOUT:
            continue

        def plan(s: ToolService, unii: str = on.substances[sub].unii) -> Gold:
            return ids(_hop(s, _hop(s, _resolve(s, unii, "substance"), "INGREDIENT", "in"), "PRODUCT", "in"))

        gold = _both(on, off, lambda ix, sub=sub: ids(packages(ix, sub)))
        yield Candidate(_sub(on, sub), {True: [sub], False: [sub]}, gold, _same(plan))


def t11_products_of_labeler(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for org, name in _firm_names(on, rng):
        if not _fanout(on.products_labeled_by(org)):
            continue

        def plan(s: ToolService, name: str = name) -> Gold:
            return ids(_hop(s, _hop(s, _resolve(s, name, "organization"), "LABELER", "in"), "PRODUCT", "out"))

        gold = _both(on, off, lambda ix, n=name: ids(p for o in _named(ix, n) for p in ix.products_labeled_by(o)))
        yield Candidate({"firm": name}, {True: [org], False: sorted(_named(off, name))}, gold, _same(plan))


def t11_packages_of_labeler(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    def packages(ix: Index, name: str) -> set[str]:
        firms = _named(ix, name)
        return {k for k, o in ix.package_labeler.items() if o in firms}

    for org, name in _firm_names(on, rng):
        if not _fanout(packages(on, name)):
            continue

        def plan(s: ToolService, name: str = name) -> Gold:
            return ids(_hop(s, _resolve(s, name, "organization"), "LABELER", "in"))

        gold = _both(on, off, lambda ix, n=name: ids(packages(ix, n)))
        yield Candidate({"firm": name}, {True: [org], False: sorted(_named(off, name))}, gold, _same(plan))


# ---------------------------------------------------------------------------------------------- T6


FAKE_SEED_NDC = "9"


def _none_plan(text: str, kind: str) -> Plan:
    def plan(s: ToolService) -> Gold:
        return ids(_resolve(s, text, kind))

    return plan


def t6_fake_package(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    known = {k.ndc for k in on.packages.values()}
    for p in _shuffled(list(on.products), rng):
        ndc = on.products[p].product_ndc
        segments = ndc.split("-")
        width = 10 - len(segments[0]) - len(segments[1])
        for n in (99, 98, 97, 9, 8):
            fake = f"{ndc}-{str(n).zfill(width)[-width:]}"
            if fake not in known:
                yield Candidate(
                    {"ndc": fake}, {True: [], False: []}, {True: NONE, False: NONE}, _same(_none_plan(fake, "package"))
                )
                break


def t6_fake_unii(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    known = {s.unii for s in on.substances.values()}
    for _ in range(MAX_TRIES):
        fake = "".join(rng.choice(alphabet) for _ in range(10))
        if fake not in known:
            yield Candidate(
                {"unii": fake}, {True: [], False: []}, {True: NONE, False: NONE}, _same(_none_plan(fake, "substance"))
            )


def t6_substance_without_recalls(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    for sub in _shuffled(list(on.substances), rng):
        products = on.products_containing(sub)
        if not products or on.firms_recalling(products) or off.firms_recalling(products):
            continue
        if len(products) > MAX_FANOUT:
            continue

        def plan(s: ToolService, unii: str = on.substances[sub].unii) -> Gold:
            found: set[str] = set()
            for iri in _resolve(s, unii, "substance"):
                found |= {x.firm_id for x in _call(s, "recalls_for", entity_id=iri).recalls}
            return ids(found)

        yield Candidate(_sub(on, sub), {True: [sub], False: [sub]}, {True: NONE, False: NONE}, _same(plan))


FAKE_FIRM_WORDS = (
    ("Northgate", "Bellmont", "Crestline", "Harbor Ridge", "Silverleaf", "Oakhaven", "Westbrook", "Pinecrest"),
    ("Pharmaceuticals", "Laboratories", "Pharma", "Therapeutics", "Health"),
    ("Inc.", "LLC", "Corp.", "Ltd."),
)


def t6_fake_firm(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    taken = {normalize_firm(o.name) for ix in (on, off) for o in ix.orgs.values()}
    taken |= {normalize_firm(a.name) for o in on.orgs.values() for a in o.aliases}
    a, b, c = FAKE_FIRM_WORDS
    for x, y, z in _shuffled([(x, y, z) for x in a for y in b for z in c], rng):
        name = f"{x} {y} {z}"
        if not any(normalize_firm(name) in t or t in normalize_firm(name) for t in taken if len(t) >= 6):
            yield Candidate(
                {"firm": name},
                {True: [], False: []},
                {True: NONE, False: NONE},
                _same(_none_plan(name, "organization")),
            )


def t6_fake_rxcui(on: Index, off: Index, rng: random.Random) -> Iterator[Candidate]:
    known = {d.rxcui for d in on.drugs.values()}
    for _ in range(MAX_TRIES):
        fake = str(rng.randint(3_000_000, 3_999_999))
        if fake not in known:
            yield Candidate(
                {"rxcui": fake},
                {True: [], False: []},
                {True: NONE, False: NONE},
                _same(_none_plan(fake, "clinical_drug")),
            )


# ------------------------------------------------------------------------------------- the templates

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
        "t9_recalls_sharing_an_ingredient",
        "T9",
        (
            "Which recalls other than {recall_number} name a product that shares an active ingredient with a"
            " product named in recall {recall_number}?",
            "Apart from {recall_number} itself, which recalls name a product containing one of the active"
            " ingredients of the products in recall {recall_number}?",
            "List the recalls, other than {recall_number}, that name a product sharing an active ingredient with"
            " a product of recall {recall_number}.",
        ),
        t9_recalls_sharing_an_ingredient,
        "4 hops: recall, product, substance, product, recall",
    ),
    Template(
        "t9_recalls_of_colabeled_products",
        "T9",
        (
            "Which recalls name a product labeled by a firm that labels a package of product NDC {product_ndc}?",
            "Take the firms that label packages of the product {product_ndc}: which recalls name any product"
            " those firms label?",
            "List the recalls that name a product labeled by one of the labelers of product NDC {product_ndc}.",
        ),
        t9_recalls_of_colabeled_products,
        "5 hops: product, package, labeler, package, product, recall",
    ),
    Template(
        "t9_labelers_of_ingredient_sharers",
        "T9",
        (
            "Which firms label a product that shares an active ingredient with the product {product_ndc}?",
            "Who labels products containing any of the active ingredients of product NDC {product_ndc}?",
            "List the labelers of products that share an active ingredient with product NDC {product_ndc}.",
        ),
        t9_labelers_of_ingredient_sharers,
        "5 hops: product, substance, product, package, labeler",
    ),
    Template(
        "t9_substances_of_colabeled_recall_products",
        "T9",
        (
            "Which substances are active ingredients of products labeled by the firms that label a product"
            " named in recall {recall_number}?",
            "Take the labelers of the products in recall {recall_number}: which active ingredients are in the"
            " products those firms label?",
            "List the active ingredient substances of every product labeled by a labeler of a product named in"
            " recall {recall_number}.",
        ),
        t9_substances_of_colabeled_recall_products,
        "6 hops: recall, product, package, labeler, package, product, substance",
    ),
    Template(
        "t10_firms_within_six_steps",
        "T10",
        (
            "Which other firms can be reached from {firm} in at most 6 steps?",
            "Which firms, other than {firm} itself, are within 6 steps of {firm}?",
            "List the firms other than {firm} that are at most 6 steps away from it.",
        ),
        t10_firms_within_six_steps,
    ),
    Template(
        "t10_substances_within_four_steps",
        "T10",
        (
            "Which substances are within 4 steps of recall {recall_number}?",
            "Which substances can be reached from recall {recall_number} in at most 4 steps?",
            "List the substances at most 4 steps away from recall {recall_number}.",
        ),
        t10_substances_within_four_steps,
    ),
    Template(
        "t10_recalls_within_four_steps",
        "T10",
        (
            "Which recalls are within 4 steps of the product with product NDC {product_ndc}?",
            "Which recalls can be reached from product NDC {product_ndc} in at most 4 steps?",
            "List the recalls at most 4 steps away from the product {product_ndc}.",
        ),
        t10_recalls_within_four_steps,
    ),
    Template(
        "t10_fewest_steps_substance_to_firm",
        "T10",
        (
            "What is the fewest number of steps between {name} (UNII {unii}) and the firm {firm}?",
            "How many steps does the shortest connection from {name} (UNII {unii}) to {firm} take?",
            "In how few steps can {name} (UNII {unii}) be connected to the firm {firm}?",
        ),
        t10_fewest_steps_substance_to_firm,
    ),
    Template(
        "t11_products_with_substance",
        "T11",
        (
            "Which products contain {name} (UNII {unii})?",
            "List every product that has {name} (UNII {unii}) as an active ingredient.",
            "Which products have the active ingredient {name} (UNII {unii})?",
        ),
        t11_products_with_substance,
    ),
    Template(
        "t11_packages_of_substance",
        "T11",
        (
            "Which packages belong to products that contain {name} (UNII {unii})?",
            "List every package NDC of the products containing {name} (UNII {unii}).",
            "Which packages are packages of a product with the active ingredient {name} (UNII {unii})?",
        ),
        t11_packages_of_substance,
    ),
    Template(
        "t11_products_of_labeler",
        "T11",
        (
            "Which products does {firm} label?",
            "List every product that has a package labeled by {firm}.",
            "Which products are labeled by {firm}?",
        ),
        t11_products_of_labeler,
    ),
    Template(
        "t11_packages_of_labeler",
        "T11",
        (
            "Which packages does {firm} label?",
            "List every package whose labeler is {firm}.",
            "Which package NDCs are labeled by {firm}?",
        ),
        t11_packages_of_labeler,
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
        "a real substance whose products were never recalled, in either identity level",
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
        "a plausible firm name that matches no organization or alias in either identity level",
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

TYPES: tuple[str, ...] = ("T1", "T3", "T4", "T9", "T10", "T11", "T6")
INVARIANT_TYPES = frozenset({"T1", "T9", "T10", "T11", "T6"})  # gold equal at both identity levels


# --------------------------------------------------------------------------------------------- generation


@dataclass
class GenerationReport:
    tried: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)
    rejected_truncated: dict[str, int] = field(default_factory=dict)
    rejected_mismatch: dict[str, int] = field(default_factory=dict)
    rejected_gold_size: dict[str, int] = field(default_factory=dict)
    redistributed: dict[str, int] = field(default_factory=dict)  # type -> questions moved to other templates


def _normalized(gold: Gold) -> Gold:
    return {**gold, "ids": sorted(gold["ids"])} if "ids" in gold else gold


def check(c: Candidate, services: Mapping[str, tuple[bool, ToolService]]) -> str:
    """'ok' when every cell's plan reproduces that cell's gold on the given store."""
    for _cell, (identity, service) in sorted(services.items()):
        try:
            got = c.plan[identity](service)
        except Truncated:
            return "truncated"
        except (ToolError, ValidationError):
            return "mismatch"
        if _normalized(got) != _normalized(c.gold[identity]):
            return "mismatch"
    return "ok"


def _gold_ok(template: Template, c: Candidate) -> bool:
    for gold in c.gold.values():
        if template.type == "T6":
            if gold["kind"] != "none":
                return False
            continue
        if gold["kind"] == "none":
            return False
        if gold["kind"] == "ids":
            n = len(gold["ids"])
            if template.type == "T11" and not FANOUT_MIN <= n <= FANOUT_MAX:
                return False
            if template.type != "T11" and not 1 <= n <= MAX_GOLD:
                return False
    if template.type in INVARIANT_TYPES and _normalized(c.gold[True]) != _normalized(c.gold[False]):
        raise AssertionError(f"{template.id}: gold differs between identity levels for {c.params}")
    return True


def _collect(
    template: Template,
    quota: int,
    on: Index,
    off: Index,
    services: Mapping[str, tuple[bool, ToolService]],
    seed: int,
    report: GenerationReport,
) -> list[Candidate]:
    """The template's first `quota` candidates that pass (deterministic: a larger quota keeps these and adds)."""
    rng = random.Random(f"{seed}:{template.id}")
    kept: list[Candidate] = []
    seen_anchor: set[tuple[str, ...]] = set()
    seen_gold: set[str] = set()
    tried = 0
    report.rejected_truncated.pop(template.id, None)
    report.rejected_mismatch.pop(template.id, None)
    report.rejected_gold_size.pop(template.id, None)
    for c in template.candidates(on, off, rng):
        if len(kept) >= quota or tried >= MAX_TRIES:
            break
        key = tuple(c.anchors[True]) or tuple(sorted(c.params.values()))
        if key in seen_anchor:
            continue
        seen_anchor.add(key)
        tried += 1
        if not _gold_ok(template, c):
            report.rejected_gold_size[template.id] = report.rejected_gold_size.get(template.id, 0) + 1
            continue
        gold_key = repr(_normalized(c.gold[True]))
        if template.type == "T11" and gold_key in seen_gold:
            continue  # no two T11 questions share a gold set
        verdict = check(c, services)
        if verdict == "ok":
            kept.append(c)
            seen_gold.add(gold_key)
        elif verdict == "truncated":
            report.rejected_truncated[template.id] = report.rejected_truncated.get(template.id, 0) + 1
        else:
            report.rejected_mismatch[template.id] = report.rejected_mismatch.get(template.id, 0) + 1
    report.tried[template.id] = tried
    report.kept[template.id] = len(kept)
    return kept


def generate(
    on: Index,
    off: Index,
    services: Mapping[str, tuple[bool, ToolService]],
    seed: int,
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], GenerationReport]:
    """`services`: cell name -> (identity, SQL tool service of that cell). PER_TYPE questions per type, split
    evenly over its templates; when a template runs out of candidates, the type's other templates take the
    shortfall, so every type keeps PER_TYPE questions (DEV_PER_TYPE dev, stratified by template)."""
    report = GenerationReport()
    by_type: dict[str, list[Template]] = defaultdict(list)
    for t in TEMPLATES:
        by_type[t.type].append(t)
    questions: list[dict[str, Any]] = []
    for qtype in TYPES:
        templates = by_type[qtype]
        quotas = dict(zip((t.id for t in templates), largest_remainder(PER_TYPE, [1] * len(templates)), strict=True))
        kept = {t.id: _collect(t, quotas[t.id], on, off, services, seed, report) for t in templates}
        short = PER_TYPE - sum(len(v) for v in kept.values())
        if short > 0:
            full = [t for t in templates if len(kept[t.id]) == quotas[t.id]]
            for t, extra in zip(full, largest_remainder(short, [1] * len(full)), strict=True):
                if extra:
                    quotas[t.id] += extra
                    kept[t.id] = _collect(t, quotas[t.id], on, off, services, seed, report)
            report.redistributed[qtype] = short
        for t in templates:
            log(f"{t.id}: kept {len(kept[t.id])}/{quotas[t.id]} after {report.tried[t.id]} candidates")
        dev = largest_remainder(DEV_PER_TYPE, [len(kept[t.id]) for t in templates])
        for template, n_dev in zip(templates, dev, strict=True):
            mine = kept[template.id]
            order = list(range(len(mine)))
            random.Random(f"{seed}:{template.id}:split").shuffle(order)
            dev_set = set(order[:n_dev])
            for k, c in enumerate(mine):
                text = template.phrasings[k % len(template.phrasings)].format(**c.params).replace("..", ".")
                questions.append(
                    {
                        "id": f"{template.id}-{k:02d}",
                        "type": qtype,
                        "template": template.id,
                        "phrasing": k % len(template.phrasings),
                        "text": text,
                        "params": c.params,
                        "gold": {"identity_on": _normalized(c.gold[True]), "identity_off": _normalized(c.gold[False])},
                        "anchors": {"identity_on": c.anchors[True], "identity_off": c.anchors[False]},
                        "split": "dev" if k in dev_set else "test",
                    }
                )
    return questions, report


def recheck(
    on: Index,
    off: Index,
    questions: Sequence[Mapping[str, Any]],
    services: Mapping[str, tuple[bool, ToolService]],
    seed: int,
) -> list[str]:
    """Rebuild each kept candidate and run its plans on other stores (every cell); ids that disagree."""
    bad: list[str] = []
    for template in TEMPLATES:
        wanted = {repr(q["params"]): q for q in questions if q["template"] == template.id}
        rng = random.Random(f"{seed}:{template.id}")
        for tried, c in enumerate(template.candidates(on, off, rng)):
            if not wanted or tried >= MAX_TRIES * 4:
                break
            q = wanted.pop(repr(c.params), None)
            if q is not None and check(c, services) != "ok":
                bad.append(str(q["id"]))
        bad += [str(q["id"]) for q in wanted.values()]
    return sorted(bad)


def gold_for(question: Mapping[str, Any], identity: bool) -> Gold:
    gold: Gold = question["gold"]["identity_on" if identity else "identity_off"]
    return gold


def anchors_for(question: Mapping[str, Any], identity: bool) -> list[str]:
    anchors: list[str] = question["anchors"]["identity_on" if identity else "identity_off"]
    return anchors
